"""
One-shot script to backfill the `goOutEventId` field (GoOut's numeric
EventSerial panel ID) onto existing party documents that predate it.

This field is the join key between `parties_collection` and the sales
tracker's `goout_sales`/`goout_sales_log` collections (keyed by the same
numeric id — see goout-scraper/scraper.py `scrape_sales_data`). Without it,
per-party ticket-sales data can't be matched back to a party record.

For each party missing `goOutEventId`, this re-fetches the party's own
originalUrl/canonicalUrl via the internal scrape endpoint (which now
extracts EventSerial) and PATCHes it onto the existing party document.
Nothing is deleted; only a currently-empty field is filled in.

Usage:
    python backfill_goout_event_id.py            # dry run, just prints what would happen
    python backfill_goout_event_id.py --apply    # actually PATCHes the parties
"""
import sys
import io
import argparse
import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import config

BACKEND = config.BACKEND_URL


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


def scrape_event_id(url: str) -> str | None:
    resp = requests.post(
        f"{BACKEND}/api/internal/scrape-party",
        json={"url": url},
        headers={"X-Service-Token": config.SERVICE_TOKEN},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"    scrape failed ({resp.status_code}): {resp.text[:150]}")
        return None
    return resp.json().get("goOutEventId")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually PATCH the parties")
    args = parser.parse_args()

    print("Fetching all parties...")
    resp = requests.get(f"{BACKEND}/api/parties", timeout=30)
    resp.raise_for_status()
    parties = resp.json()
    print(f"  {len(parties)} parties total")

    missing = [p for p in parties if not p.get("goOutEventId")]
    print(f"  {len(missing)} missing goOutEventId")

    if not missing:
        print("Nothing to backfill.")
        return

    token = None
    if args.apply:
        print("Logging in...")
        token = login()

    updated = 0
    failed = 0
    for p in missing:
        pid = p.get("_id") or p.get("id")
        url = p.get("originalUrl") or p.get("canonicalUrl") or p.get("goOutUrl")
        name = p.get("name", "?")
        if not pid or not url:
            print(f"  SKIP {name!r} (id={pid}) — no id or url")
            continue

        event_id = scrape_event_id(url)
        if not event_id:
            print(f"  NO-ID {name!r} (id={pid}) @ {url}")
            failed += 1
            continue

        print(f"  {'WOULD SET' if not args.apply else 'SET'} {name!r} (id={pid}) -> goOutEventId={event_id}")
        if args.apply:
            headers = {"Authorization": f"Bearer {token}"}
            r = requests.put(
                f"{BACKEND}/api/admin/update-party/{pid}",
                json={"goOutEventId": event_id},
                headers=headers,
                timeout=15,
            )
            if r.status_code == 200:
                updated += 1
            else:
                print(f"    update failed ({r.status_code}): {r.text[:150]}")
                failed += 1

    if args.apply:
        print(f"\nUpdated {updated}/{len(missing)} parties ({failed} failed).")
    else:
        print(f"\nDry run only — {len(missing)} parties would be checked. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
