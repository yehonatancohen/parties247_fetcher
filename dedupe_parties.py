"""
Daily Parties247 catalog quality pass.

This script is intentionally conservative about destructive changes:
- obvious test/demo events can be removed automatically;
- high-confidence duplicate listings are merged automatically;
- suspicious metadata is reported for review, not guessed.

Duplicate detection uses three signals:
1. same GoOut event id / normalized purchase URL;
2. same venue + near-identical start time, even when organizers use unrelated titles;
3. the legacy fuzzy brand-prefix + calendar-day rule for recurring listings.

When duplicate sellers exist, the keeper is chosen by expected commission per ticket
(account1 = flat ₪25, account2 = 6% of ticket price), then by real lifetime
commission already recorded, then by metadata quality. This is more accurate than
"keep the most expensive ticket" or "account1 always wins".

Usage:
    python dedupe_parties.py            # dry run
    python dedupe_parties.py --apply    # apply safe deletes/merges
"""

import sys
import io
import re
import argparse
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from zoneinfo import ZoneInfo

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import config

BACKEND = config.BACKEND_URL
ACCOUNT1_REFERRAL = next(
    (a["referral"] for a in config.GOOUT_ACCOUNTS if a["account_id"] == "account1"),
    None,
)

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
ACCOUNT1_FLAT_FEE = 25.0
ACCOUNT2_PCT = 0.06
DEFAULT_TICKET_PRICE = 100.0
MAX_SUSPICIOUS_PRICE = 2000.0

_PLACEHOLDER_LOCATIONS = {
    "",
    "unknown location",
    "unknown",
    "tba",
    "to be announced",
    "מיקום לא ידוע",
    "יעודכן בהמשך",
}

_GENERIC_LOCATION_TOKENS = {
    "tel", "aviv", "yafo", "israel", "ישראל", "תל", "אביב", "יפו",
    "street", "st", "רחוב", "דרך", "road", "club", "bar", "מועדון",
}

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)
_PUNCT_RE = re.compile(r"[|:\-/.,!*?\"'()[\]{}]+")
_TEST_RE = re.compile(
    r"^\s*(?:test(?:\s+event)?|demo(?:\s+event)?|טסט|בדיקה)(?:\s|$|[-_/])",
    flags=re.IGNORECASE,
)


def login() -> str:
    resp = requests.post(
        f"{BACKEND}/api/admin/login",
        json={"password": config.ADMIN_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.json().get("token")
    if not token:
        raise RuntimeError("No token in login response")
    return token


def fetch_sales_context(token: str) -> tuple[dict[str, float], dict[str, set[str]]]:
    """Return (lifetime commission by party slug, account ids by party slug)."""
    resp = requests.get(
        f"{BACKEND}/api/admin/analytics/sales",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()

    revenue: dict[str, float] = {}
    accounts: dict[str, set[str]] = {}
    for row in resp.json().get("data", []):
        slug = row.get("partySlug")
        if not slug:
            continue
        revenue[slug] = revenue.get(slug, 0.0) + float(row.get("totalRevenue") or 0.0)
        account_id = row.get("accountId")
        if account_id:
            accounts.setdefault(slug, set()).add(str(account_id))
    return revenue, accounts


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _location_text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("address") or "")
    return str(value or "")


def normalize_location(value) -> str:
    text = _location_text(value).lower().strip()
    text = _EMOJI_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _location_tokens(value) -> set[str]:
    return {
        token for token in normalize_location(value).split()
        if len(token) >= 2 and token not in _GENERIC_LOCATION_TOKENS
    }


def location_similarity(a, b) -> float:
    """Venue/address similarity tuned for noisy Hebrew/English GoOut strings."""
    na = normalize_location(a)
    nb = normalize_location(b)
    if na in _PLACEHOLDER_LOCATIONS or nb in _PLACEHOLDER_LOCATIONS:
        return 0.0
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    ta, tb = _location_tokens(a), _location_tokens(b)
    if ta and tb:
        shared = ta & tb
        union = ta | tb
        jaccard = len(shared) / len(union)
        # A distinctive shared venue token is a strong signal even when one
        # string contains a full address and the other only the venue name.
        distinctive = any(len(token) >= 4 for token in shared)
    else:
        jaccard = 0.0
        distinctive = False

    sequence = SequenceMatcher(None, na, nb).ratio()
    if distinctive:
        return max(sequence, jaccard, 0.88)
    return max(sequence, jaccard)


def _parse_party_datetime(party: dict) -> datetime | None:
    raw = party.get("date") or party.get("startsAt")
    if not raw:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.fromisoformat(text[:19])
            except ValueError:
                return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ISRAEL_TZ)
    return dt.astimezone(ISRAEL_TZ)


def same_venue_time(a: dict, b: dict, max_minutes: int = 90) -> bool:
    """High-confidence same-event signal independent of title wording."""
    da = _parse_party_datetime(a)
    db = _parse_party_datetime(b)
    if da is None or db is None or da.date() != db.date():
        return False

    delta_minutes = abs((da - db).total_seconds()) / 60.0
    if delta_minutes > max_minutes:
        return False

    sim = location_similarity(a.get("location"), b.get("location"))
    if sim >= 0.88:
        return True

    # Exact normalized venue/address can tolerate a little more start-time drift.
    la = normalize_location(a.get("location"))
    lb = normalize_location(b.get("location"))
    return bool(la and la == lb and delta_minutes <= 120)


def brand_prefix(name: str) -> str:
    name = _EMOJI_RE.sub("", name or "")
    match = re.match(r"^([^0-9]+)", name)
    prefix = match.group(1) if match else name
    prefix = _PUNCT_RE.sub(" ", prefix)
    return re.sub(r"\s+", " ", prefix).strip().upper()


def calendar_day(date_str: str) -> str:
    return (date_str or "")[:10]


def dedupe_key(party: dict) -> tuple | None:
    """Legacy recurring-event signal kept as a secondary dedupe path."""
    name_key = normalize_name(party.get("name", ""))
    date_key = str(party.get("date") or party.get("startsAt") or "").strip()
    if not name_key or not date_key:
        return None

    prefix = brand_prefix(party.get("name", ""))
    if len(re.sub(r"\s+", "", prefix)) >= 3:
        return ("fuzzy", prefix, calendar_day(date_key))
    return ("exact", name_key, date_key)


def _normalized_purchase_url(party: dict) -> str | None:
    raw = next(
        (party.get(k) for k in ("goOutUrl", "originalUrl", "canonicalUrl") if party.get(k)),
        None,
    )
    if not raw:
        return None
    try:
        parsed = urlparse(str(raw).strip())
        query = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in {"ref", "aff", "fbclid", "gclid"} and not k.lower().startswith("utm_")
        ]
        return urlunparse(
            parsed._replace(
                scheme=(parsed.scheme or "https").lower(),
                netloc=parsed.netloc.lower().removeprefix("www."),
                path=parsed.path.rstrip("/") or "/",
                query=urlencode(query, doseq=True),
                fragment="",
            )
        )
    except Exception:
        return str(raw).strip()


def duplicate_reason(a: dict, b: dict) -> str | None:
    a_event = str(a.get("goOutEventId") or "").strip()
    b_event = str(b.get("goOutEventId") or "").strip()
    if a_event and b_event and a_event == b_event:
        return "same_goout_event"

    a_url = _normalized_purchase_url(a)
    b_url = _normalized_purchase_url(b)
    if a_url and b_url and a_url == b_url:
        return "same_purchase_url"

    if same_venue_time(a, b):
        return "same_venue_time"

    a_key = dedupe_key(a)
    b_key = dedupe_key(b)
    if a_key is not None and a_key == b_key:
        return "same_name_day"

    return None


def find_duplicate_groups(parties: list[dict]) -> list[dict]:
    """Connected components of pairwise duplicate signals."""
    n = len(parties)
    parent = list(range(n))
    edge_reasons: dict[tuple[int, int], str] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            reason = duplicate_reason(parties[i], parties[j])
            if reason:
                union(i, j)
                edge_reasons[(i, j)] = reason

    components: dict[int, list[int]] = {}
    for i in range(n):
        components.setdefault(find(i), []).append(i)

    groups: list[dict] = []
    for indexes in components.values():
        if len(indexes) < 2:
            continue
        reasons = {
            edge_reasons[(i, j)]
            for i in indexes
            for j in indexes
            if i < j and (i, j) in edge_reasons
        }
        groups.append({
            "parties": [parties[i] for i in indexes],
            "reasons": sorted(reasons),
        })
    return groups


def party_price(party: dict) -> float | None:
    raw = party.get("ticketPrice")
    try:
        price = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return price if price is not None and price >= 0 else None


def party_revenue(party: dict, revenue_by_slug: dict[str, float]) -> float:
    slug = party.get("slug")
    return revenue_by_slug.get(slug, 0.0) if slug else 0.0


def party_tier(party: dict, accounts_by_slug: dict[str, set[str]]) -> str:
    slug = party.get("slug")
    accounts = accounts_by_slug.get(slug, set()) if slug else set()
    if any("account1" in account.lower() for account in accounts):
        return "account1"
    if ACCOUNT1_REFERRAL and party.get("referralCode") == ACCOUNT1_REFERRAL:
        return "account1"
    return "account2"


def expected_commission_per_ticket(party: dict, accounts_by_slug: dict[str, set[str]]) -> float:
    tier = party_tier(party, accounts_by_slug)
    if tier == "account1":
        return ACCOUNT1_FLAT_FEE
    price = party_price(party)
    if price is None:
        price = DEFAULT_TICKET_PRICE
    return round(price * ACCOUNT2_PCT, 2)


def _metadata_quality(party: dict) -> int:
    score = 0
    if normalize_name(party.get("name", "")) not in {"", "unknown event"}:
        score += 1
    if _parse_party_datetime(party) is not None:
        score += 1
    if normalize_location(party.get("location")) not in _PLACEHOLDER_LOCATIONS:
        score += 1
    if _normalized_purchase_url(party):
        score += 1
    if party_price(party) is not None:
        score += 1
    return score


def best_party(
    parties: list[dict],
    revenue_by_slug: dict[str, float],
    accounts_by_slug: dict[str, set[str]],
) -> dict:
    """Choose the listing that maximizes future expected commission."""
    return max(
        parties,
        key=lambda p: (
            expected_commission_per_ticket(p, accounts_by_slug),
            party_revenue(p, revenue_by_slug),
            _metadata_quality(p),
            party_price(p) if party_price(p) is not None else -1.0,
        ),
    )


def is_obvious_test_party(party: dict) -> bool:
    return bool(_TEST_RE.search(str(party.get("name") or "")))


def audit_party(party: dict) -> list[dict]:
    issues: list[dict] = []
    pid = str(party.get("_id") or party.get("id") or "")
    name = str(party.get("name") or "")

    def add(kind: str, severity: str, message: str):
        issues.append({
            "type": kind,
            "severity": severity,
            "partyId": pid,
            "slug": party.get("slug"),
            "name": name,
            "message": message,
        })

    if is_obvious_test_party(party):
        add("test_party", "auto", "Obvious test/demo event")
    if not normalize_name(name) or normalize_name(name) == "unknown event":
        add("missing_name", "review", "Missing/placeholder title")
    if _parse_party_datetime(party) is None:
        add("invalid_time", "review", "Missing or invalid party date/time")

    loc = normalize_location(party.get("location"))
    if loc in _PLACEHOLDER_LOCATIONS:
        add("missing_location", "review", "Missing/placeholder venue")

    raw_price = party.get("ticketPrice")
    if raw_price is not None:
        try:
            price = float(raw_price)
            if price < 0 or price > MAX_SUSPICIOUS_PRICE:
                add("suspicious_price", "review", f"Suspicious ticket price: {raw_price}")
        except (TypeError, ValueError):
            add("suspicious_price", "review", f"Non-numeric ticket price: {raw_price}")

    if not _normalized_purchase_url(party):
        add("missing_purchase_url", "review", "No purchase URL")

    return issues


def run_dedupe(apply: bool, log=print) -> dict:
    """
    Audit the catalog, remove obvious test entries, and merge high-confidence
    duplicate listings. Party mutations go through the backend API so redirects,
    revalidation and audit logging keep working.
    """
    log("Logging in...")
    token = login()
    headers = {"Authorization": f"Bearer {token}"}

    log("Fetching all parties...")
    resp = requests.get(f"{BACKEND}/api/parties", timeout=30)
    resp.raise_for_status()
    parties = resp.json()
    log(f"  {len(parties)} parties total")

    log("Fetching sales context...")
    revenue_by_slug, accounts_by_slug = fetch_sales_context(token)

    issues = [issue for party in parties for issue in audit_party(party)]
    test_ids = {
        issue["partyId"] for issue in issues
        if issue["type"] == "test_party" and issue.get("partyId")
    }

    active_for_dedupe = [
        party for party in parties
        if str(party.get("_id") or party.get("id") or "") not in test_ids
    ]
    duplicate_groups = find_duplicate_groups(active_for_dedupe)
    log(
        f"  {len(duplicate_groups)} duplicate group(s) found "
        f"({sum(len(g['parties']) for g in duplicate_groups)} parties involved)"
    )

    to_delete: list[tuple[dict, dict | None, str]] = []
    merges: list[dict] = []

    # Obvious test events are safe to remove without redirecting them into a real event.
    for party in parties:
        pid = str(party.get("_id") or party.get("id") or "")
        if pid and pid in test_ids:
            log(f"  TEST DELETE id={pid} name={party.get('name')!r}")
            to_delete.append((party, None, "test_party"))

    for group in duplicate_groups:
        dupes = group["parties"]
        keeper = best_party(dupes, revenue_by_slug, accounts_by_slug)
        losers = [party for party in dupes if party is not keeper]
        keeper_expected = expected_commission_per_ticket(keeper, accounts_by_slug)

        log(
            f"\nKEEP '{keeper.get('name')}' "
            f"expected=₪{keeper_expected:.2f}/ticket "
            f"revenue=₪{party_revenue(keeper, revenue_by_slug):.2f} "
            f"reasons={','.join(group['reasons'])}"
        )

        merge_row = {
            "keeperId": str(keeper.get("_id") or keeper.get("id") or ""),
            "keeperSlug": keeper.get("slug"),
            "keeperName": keeper.get("name"),
            "keeperExpectedPerTicket": keeper_expected,
            "reasons": group["reasons"],
            "losers": [],
        }

        for loser in losers:
            loser_expected = expected_commission_per_ticket(loser, accounts_by_slug)
            log(
                f"  DELETE id={loser.get('_id') or loser.get('id')} "
                f"name={loser.get('name')!r} "
                f"expected=₪{loser_expected:.2f}/ticket "
                f"revenue=₪{party_revenue(loser, revenue_by_slug):.2f} "
                f"-> {keeper.get('slug')}"
            )
            to_delete.append((loser, keeper, "duplicate"))
            merge_row["losers"].append({
                "partyId": str(loser.get("_id") or loser.get("id") or ""),
                "slug": loser.get("slug"),
                "name": loser.get("name"),
                "expectedPerTicket": loser_expected,
            })
        merges.append(merge_row)

    review_issues = [issue for issue in issues if issue["severity"] == "review"]
    log(f"\nQuality audit: {len(review_issues)} item(s) need review, {len(test_ids)} test item(s).")

    if not to_delete:
        log("No automatic cleanup actions needed.")
        return {
            "groups": len(duplicate_groups),
            "deleted": 0,
            "failed": 0,
            "dry_run": not apply,
            "issues": review_issues,
            "merges": merges,
        }

    log(f"{len(to_delete)} automatic delete/merge action(s) identified.")
    if not apply:
        log("Dry run only — re-run with --apply to apply safe actions.")
        return {
            "groups": len(duplicate_groups),
            "deleted": 0,
            "failed": 0,
            "dry_run": True,
            "issues": review_issues,
            "merges": merges,
            "wouldDelete": len(to_delete),
        }

    deleted = 0
    failed = 0
    for loser, keeper, reason in to_delete:
        pid = loser.get("_id") or loser.get("id")
        keeper_slug = keeper.get("slug") if keeper else None
        try:
            body = {"redirectTo": keeper_slug} if keeper_slug else None
            response = requests.delete(
                f"{BACKEND}/api/admin/delete-party/{pid}",
                headers=headers,
                json=body,
                timeout=15,
            )
            if response.status_code == 200:
                deleted += 1
            else:
                log(f"  Failed to delete {pid}: {response.status_code} {response.text[:200]}")
                failed += 1
        except Exception as exc:
            log(f"  Error deleting {pid} ({reason}): {exc}")
            failed += 1

    log(f"Applied {deleted}/{len(to_delete)} automatic cleanup action(s).")
    return {
        "groups": len(duplicate_groups),
        "deleted": deleted,
        "failed": failed,
        "dry_run": False,
        "issues": review_issues,
        "merges": merges,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply safe deletes/merges")
    args = parser.parse_args()
    run_dedupe(apply=args.apply)


if __name__ == "__main__":
    main()
