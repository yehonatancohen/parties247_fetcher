"""
One-shot script to find duplicate parties (same name + same start time) and
remove the extra copies, redirecting the dead slug(s) to the survivor.

Two parties are considered duplicates when their normalized name AND full
date/time string match exactly (this catches the same real-world event
listed twice under different GoOut event IDs — confirmed to happen when a
promoter re-lists an event, e.g. "Revival Summer Festival 13-14.8" existed
as three separate GoOut listings, 2026-08-07). If a duplicate group includes
an account1 party, account1 is always kept regardless of revenue/price,
since account1 is the priority/highest-value account (flat-fee, easiest to
convert — see .claude/commands/seo-update.md in the workspace root for the
revenue-priority rationale this mirrors). Otherwise, the party with the most
real revenue (from /api/admin/analytics/sales) is kept — NOT the cheapest
price as before 2026-08-07: keeping the cheapest listing would have deleted
an already-earning duplicate in favor of a zero-revenue one, exactly
backwards for revenue purposes. Price is only a fallback tiebreaker when
neither has any recorded revenue.

Every deleted party's slug is redirected to the keeper's slug via
DELETE /api/admin/delete-party/<id>?redirectTo=<keeper-slug> so the dead
URL's SEO/traffic signal isn't just lost — parties247-website's proxy.ts
serves a 308 redirect for any slug with a recorded mapping.

Usage:
    python dedupe_parties.py            # dry run, just prints what would happen
    python dedupe_parties.py --apply    # actually deletes/updates
"""
import sys
import io
import re
import argparse
import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import config

BACKEND = config.BACKEND_URL
ACCOUNT1_REFERRAL = next(
    (a["referral"] for a in config.GOOUT_ACCOUNTS if a["account_id"] == "account1"),
    None,
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


def fetch_revenue_by_slug(token: str) -> dict[str, float]:
    """
    partySlug -> lifetime totalRevenue (our commission, not GoOut's raw
    numbers), summed across every account tracking that slug. Used to pick
    the actual-earning duplicate over an arbitrary/cheap one.
    """
    resp = requests.get(
        f"{BACKEND}/api/admin/analytics/sales",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    revenue: dict[str, float] = {}
    for row in rows:
        slug = row.get("partySlug")
        if not slug:
            continue
        revenue[slug] = revenue.get(slug, 0.0) + float(row.get("totalRevenue") or 0.0)
    return revenue


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def party_price(p: dict) -> float:
    price = p.get("ticketPrice")
    return price if isinstance(price, (int, float)) else float("inf")


def party_revenue(p: dict, revenue_by_slug: dict[str, float]) -> float:
    slug = p.get("slug")
    return revenue_by_slug.get(slug, 0.0) if slug else 0.0


def best_by_revenue_then_price(parties: list[dict], revenue_by_slug: dict[str, float]) -> dict:
    return max(
        parties,
        key=lambda p: (party_revenue(p, revenue_by_slug), -party_price(p)),
    )


def run_dedupe(apply: bool, log=print) -> dict:
    """
    Find duplicate parties and (if apply) delete the losers, redirecting each
    to the keeper. Returns a summary dict — callable both from the CLI below
    and from orchestrator.py's daily scrape flow (see `main.py`/`CLAUDE.md`
    "Fixed 2026-08-07 (sixth pass)" for why this runs automatically now: a
    one-shot manual script wasn't enough, duplicates kept accumulating
    between runs).
    """
    log("Logging in...")
    token = login()
    headers = {"Authorization": f"Bearer {token}"}

    log("Fetching all parties...")
    resp = requests.get(f"{BACKEND}/api/parties", timeout=30)
    resp.raise_for_status()
    parties = resp.json()
    log(f"  {len(parties)} parties total")

    log("Fetching revenue-by-party (to pick the earning duplicate, not the cheapest)...")
    revenue_by_slug = fetch_revenue_by_slug(token)

    groups: dict[tuple[str, str], list[dict]] = {}
    for p in parties:
        pid = p.get("_id") or p.get("id")
        if not pid:
            continue
        name_key = normalize_name(p.get("name", ""))
        date_key = (p.get("date") or "").strip()
        if not name_key or not date_key:
            continue
        groups.setdefault((name_key, date_key), []).append(p)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    log(f"  {len(dup_groups)} duplicate group(s) found "
        f"({sum(len(v) for v in dup_groups.values())} parties involved)")

    to_delete: list[tuple[dict, dict]] = []  # (loser, keeper)
    for (name_key, date_key), dupes in dup_groups.items():
        account1_dupes = [p for p in dupes if ACCOUNT1_REFERRAL and p.get("referralCode") == ACCOUNT1_REFERRAL]
        if account1_dupes:
            # account1 always wins regardless of revenue/price — it's the
            # priority account (flat-fee, easiest to convert).
            candidates = account1_dupes
            tag = " [account1 priority]"
        else:
            candidates = dupes
            tag = ""

        keeper = best_by_revenue_then_price(candidates, revenue_by_slug)
        losers = [p for p in dupes if p is not keeper]

        log(f"\n'{keeper.get('name')}' @ {date_key}{tag}")
        log(f"  KEEP   id={keeper.get('_id') or keeper.get('id')} slug={keeper.get('slug')} "
            f"revenue={party_revenue(keeper, revenue_by_slug)} price={keeper.get('ticketPrice')} "
            f"ref={keeper.get('referralCode')}")
        for l in losers:
            log(f"  DELETE id={l.get('_id') or l.get('id')} slug={l.get('slug')} "
                f"revenue={party_revenue(l, revenue_by_slug)} price={l.get('ticketPrice')} "
                f"ref={l.get('referralCode')}  -> redirect to {keeper.get('slug')}")
            to_delete.append((l, keeper))

    if not to_delete:
        log("\nNo duplicates to remove.")
        return {"groups": len(dup_groups), "deleted": 0, "failed": 0}

    log(f"\n{len(to_delete)} part(y/ies) would be deleted.")
    if not apply:
        log("Dry run only — re-run with --apply to actually delete.")
        return {"groups": len(dup_groups), "deleted": 0, "failed": 0, "dry_run": True}

    deleted = 0
    failed = 0
    for loser, keeper in to_delete:
        pid = loser.get("_id") or loser.get("id")
        keeper_slug = keeper.get("slug")
        try:
            r = requests.delete(
                f"{BACKEND}/api/admin/delete-party/{pid}",
                headers=headers,
                json={"redirectTo": keeper_slug} if keeper_slug else None,
                timeout=15,
            )
            if r.status_code == 200:
                deleted += 1
            else:
                log(f"  Failed to delete {pid}: {r.status_code} {r.text[:200]}")
                failed += 1
        except Exception as exc:
            log(f"  Error deleting {pid}: {exc}")
            failed += 1

    log(f"Deleted {deleted}/{len(to_delete)} duplicate parties.")
    return {"groups": len(dup_groups), "deleted": deleted, "failed": failed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually delete duplicates")
    args = parser.parse_args()
    run_dedupe(apply=args.apply)


if __name__ == "__main__":
    main()
