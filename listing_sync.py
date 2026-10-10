"""Listing Guard courier.

Every rule about what a party listing says (price, name, location, duplicates,
what needs a human) lives in the backend's listings.py. This module only does
what the backend can't do well from Render: fetch GoOut and our own site, and
hand the raw results over.

  run_listing_sync(full=False)  every 30 min — real ticket tiers per party
  run_listing_sync(full=True)   every 6 h    — event page + tiers
  run_listing_audit(...)        daily        — site render check + backend audit,
                                               then one Telegram line if anything
                                               is waiting for a decision

Browser-free (plain requests, no GoOut login), so it can never trigger 2FA.

Ticket tiers come from GET www.go-out.co/endOne/loadEventTickets — public, and
reachable directly from this VPS (checked 2026-10-09), unlike the authenticated
endOne/* stats endpoints that need cf-relay. The `Tickets` array inside the
event page is an identical placeholder on every event and is never sent.
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import config

logger = logging.getLogger(__name__)

GO_OUT_TIERS_URL = "https://www.go-out.co/endOne/loadEventTickets"
SITE_BASE_URL = "https://www.parties247.co.il"
ISSUES_URL = "https://admin.parties247.co.il/issues"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
}
_NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property="og:image"[^>]+content="([^"]+)"|content="([^"]+)"[^>]+property="og:image")'
)
_JSON_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)

# What the backend's parse_source() reads. Keeps the POST small — a full
# pageProps.event is ~15 KB of pixels, terms and marketing config per party.
_EVENT_KEYS = (
    "Title", "StartingDate", "EndingDate", "Adress", "EnglishAddress", "Location",
    "EventPublicity", "OrganizerID", "creatorId", "ProducersName", "Blurhash",
    "EventSerial", "Url", "MinimumAge", "Description",
)
CHUNK_SIZE = 25
WORKERS = 3


def _service_headers() -> dict:
    return {"X-Service-Token": config.SERVICE_TOKEN}


def trim_event(event: dict) -> dict:
    return {key: event[key] for key in _EVENT_KEYS if key in event}


def parse_event_page(html: str) -> tuple[dict | None, str | None]:
    """(pageProps.event, og:image) from a GoOut event page, or (None, None)."""
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return None, None
    try:
        event = json.loads(match.group(1)).get("props", {}).get("pageProps", {}).get("event")
    except (ValueError, AttributeError):
        return None, None
    if not isinstance(event, dict) or not event:
        return None, None
    og = _OG_IMAGE_RE.search(html)
    return event, (og.group(1) or og.group(2)) if og else None


def fetch_tiers(url_id: str | None, http=requests) -> list | None:
    """Real ticket tiers, or None when they couldn't be read (the backend
    counts consecutive failures and stops vouching for the price after three)."""
    if not url_id:
        return None
    try:
        resp = http.get(
            GO_OUT_TIERS_URL,
            params={"eventUrl": url_id},
            headers={**_HEADERS, "Referer": "https://www.go-out.co/"},
            timeout=15,
        )
        payload = resp.json()
        if payload.get("status") and isinstance(payload.get("tickets"), list):
            return payload["tickets"]
    except Exception as exc:
        logger.debug("listing_sync: tiers failed for %s: %s", url_id, exc)
    return None


def build_sync_item(target: dict, full: bool, http=requests) -> dict:
    """Fetch one party's GoOut data. A party the backend has never seen a page
    for gets a full fetch regardless, so a tiers-only run can't leave it bare."""
    item: dict = {"partyId": target["partyId"]}
    url_id = target.get("urlId")
    if full or not target.get("hasSource"):
        try:
            resp = http.get(target["url"], headers=_HEADERS, timeout=20)
            event, og_image = parse_event_page(resp.text) if resp.status_code == 200 else (None, None)
            if event:
                item["event"] = trim_event(event)
                if og_image:
                    item["ogImage"] = og_image
                url_id = str(event.get("Url") or "") or url_id
            else:
                # Page loads but has no event = removed/unpublished on GoOut.
                item["pageStatus"] = resp.status_code if resp.status_code != 200 else 404
        except Exception as exc:
            # A network blip is not "the event is gone" — say nothing about the page.
            logger.debug("listing_sync: page failed for %s: %s", target.get("url"), exc)
    item["tiers"] = fetch_tiers(url_id, http=http)
    return item


def _get_targets(http=requests) -> dict:
    resp = http.get(
        f"{config.BACKEND_URL}/api/internal/listings/targets",
        headers=_service_headers(),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run_listing_sync(full: bool = False, dry_run: bool = False, http=requests) -> dict:
    """Fetch every upcoming party from GoOut and post the raw data to the
    backend in chunks. Returns the merged summary; never raises into the
    scheduler."""
    summary = {"full": full, "targets": 0, "synced": 0, "changed": 0, "failed_chunks": 0, "results": []}
    try:
        payload = _get_targets(http=http)
    except Exception as exc:
        logger.warning("listing_sync: could not load targets: %s", exc)
        return summary

    targets = payload.get("targets") or []
    # The admin's "update prices" / "refresh content" buttons ask for this.
    full = full or bool(payload.get("fullRequested"))
    summary.update({"full": full, "targets": len(targets)})
    if not targets:
        return summary

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        items = list(pool.map(lambda target: build_sync_item(target, full, http=http), targets))

    chunks = [items[i:i + CHUNK_SIZE] for i in range(0, len(items), CHUNK_SIZE)]
    for index, chunk in enumerate(chunks):
        body = {"items": chunk, "dryRun": dry_run}
        if full and index == len(chunks) - 1 and summary["failed_chunks"] == 0:
            body["fullDone"] = True
        try:
            resp = http.post(
                f"{config.BACKEND_URL}/api/internal/listings/sync",
                json=body,
                headers=_service_headers(),
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            summary["synced"] += data.get("synced", 0)
            summary["changed"] += data.get("changed", 0)
            if dry_run:
                summary["results"].extend(r for r in data.get("results", []) if r.get("changes"))
        except Exception as exc:
            summary["failed_chunks"] += 1
            logger.warning("listing_sync: chunk %d/%d failed: %s", index + 1, len(chunks), exc)

    no_tiers = sum(1 for item in items if item.get("tiers") is None)
    logger.info(
        "listing_sync: %s run, %d parties, %d changed, %d without tiers, %d failed chunk(s)%s",
        "full" if full else "tiers", summary["targets"], summary["changed"], no_tiers,
        summary["failed_chunks"], " [dry run]" if dry_run else "",
    )
    return summary


def parse_site_page(html: str) -> dict:
    """name / startDate / price as our own event page states them in JSON-LD."""
    for block in _JSON_LD_RE.findall(html or ""):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            kinds = node.get("@type")
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if not any(isinstance(kind, str) and kind.endswith("Event") for kind in kinds):
                continue
            offers = node.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            offers = offers if isinstance(offers, dict) else {}
            return {
                "name": node.get("name"),
                "startDate": node.get("startDate"),
                "price": offers.get("price", offers.get("lowPrice")),
                "availability": offers.get("availability"),
            }
    return {}


def check_site(targets: list[dict], http=requests, pause: float = 1.0) -> list[dict]:
    """Open every listed party's page on the live site, one per second, and
    report what it actually rendered — the backend compares that with the
    database (stale cache, broken redirect, 404)."""
    checks = []
    for target in targets:
        if target.get("listingStatus") != "live" or not target.get("slug"):
            continue
        check = {"partyId": target["partyId"], "slug": target["slug"]}
        try:
            resp = http.get(f"{SITE_BASE_URL}/event/{target['slug']}", headers=_HEADERS, timeout=30)
            check.update({"status": resp.status_code, "finalUrl": resp.url})
            if resp.status_code == 200:
                check.update(parse_site_page(resp.text))
        except Exception as exc:
            logger.debug("listing_sync: site check failed for %s: %s", target["slug"], exc)
            continue  # our own site being slow is not a listing problem
        checks.append(check)
        if pause:
            time.sleep(pause)
    return checks


def run_listing_audit(accounts, telegram_mgr=None, dry_run: bool = False, site_check: bool = True,
                      http=requests) -> dict:
    """Daily: full sync, look at the live site, then let the backend find
    duplicates and everything else that needs a person. One Telegram line,
    only when something is waiting."""
    result: dict = {}
    try:
        # The audit reads what the sync stored, so a dry-run audit previews
        # against the database as it is now rather than pretending to sync.
        if not dry_run:
            run_listing_sync(full=True, http=http)
        targets = (_get_targets(http=http).get("targets") or []) if site_check else []
        # In a dry run nothing was written, so the site can't match yet — skip it.
        site_checks = check_site(targets, http=http) if site_check and not dry_run else []
        account1 = next((a for a in accounts or [] if a.account_id == "account1"), None)
        resp = http.post(
            f"{config.BACKEND_URL}/api/internal/listings/audit",
            json={
                "account1Referral": account1.referral if account1 else "",
                "siteChecks": site_checks,
                "dryRun": dry_run,
            },
            headers=_service_headers(),
            timeout=180,
        )
        resp.raise_for_status()
        result = resp.json()
    except Exception as exc:
        logger.error("listing_audit failed: %s", exc)
        return result

    waiting = int(result.get("waiting") or 0)
    logger.info(
        "listing_audit: %s parties checked, %d merged automatically, %d new issue(s), %d waiting%s",
        result.get("checked"), len(result.get("merges") or []), int(result.get("new") or 0), waiting,
        " [dry run]" if dry_run else "",
    )
    if waiting and telegram_mgr and not dry_run:
        telegram_mgr.send_message_sync(
            f"🧾 {waiting} listing issue{'s' if waiting != 1 else ''} waiting for you → {ISSUES_URL}",
            parse_mode=None,
        )
    return result


if __name__ == "__main__":
    # Manual run: `python listing_sync.py [--full] [--audit] [--apply]`.
    # Dry run unless --apply, like the other maintenance scripts here.
    import argparse
    import sys

    from scraper import GoOutAccount

    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="fetch event pages too, not only tiers")
    parser.add_argument("--audit", action="store_true", help="also run the duplicate/issue audit")
    parser.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.audit:
        accounts = [
            GoOutAccount(account_id=c["account_id"], email=c["email"], password=c["password"],
                         referral=c.get("referral", ""))
            for c in config.GOOUT_ACCOUNTS
        ]
        out = run_listing_audit(accounts, dry_run=not args.apply)
    else:
        out = run_listing_sync(full=args.full, dry_run=not args.apply)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
