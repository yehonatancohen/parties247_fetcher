"""
One-shot script to find duplicate parties (same name + same start time) and
remove the extra copies.

Two parties are considered duplicates when their normalized name AND full
date/time string match exactly. If a duplicate group includes an account1
party, account1 is always kept (and re-attributed if needed) regardless of
price, since account1 is the priority/highest-value account. Otherwise the
cheaper ticketPrice is kept.

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


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def party_price(p: dict) -> float:
    price = p.get("ticketPrice")
    return price if isinstance(price, (int, float)) else float("inf")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually delete duplicates")
    args = parser.parse_args()

    print("Fetching all parties...")
    resp = requests.get(f"{BACKEND}/api/parties", timeout=30)
    resp.raise_for_status()
    parties = resp.json()
    print(f"  {len(parties)} parties total")

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
    print(f"  {len(dup_groups)} duplicate group(s) found "
          f"({sum(len(v) for v in dup_groups.values())} parties involved)")

    to_delete: list[dict] = []
    for (name_key, date_key), dupes in dup_groups.items():
        account1_dupes = [p for p in dupes if ACCOUNT1_REFERRAL and p.get("referralCode") == ACCOUNT1_REFERRAL]
        if account1_dupes:
            dupes_sorted = sorted(account1_dupes, key=party_price)
            keeper = dupes_sorted[0]
            losers = [p for p in dupes if p is not keeper]
            tag = " [account1 priority]"
        else:
            dupes_sorted = sorted(dupes, key=party_price)
            keeper = dupes_sorted[0]
            losers = dupes_sorted[1:]
            tag = ""

        print(f"\n'{keeper.get('name')}' @ {date_key}{tag}")
        print(f"  KEEP   id={keeper.get('_id') or keeper.get('id')} "
              f"price={keeper.get('ticketPrice')} ref={keeper.get('referralCode')}")
        for l in losers:
            print(f"  DELETE id={l.get('_id') or l.get('id')} "
                  f"price={l.get('ticketPrice')} ref={l.get('referralCode')}")
            to_delete.append(l)

    if not to_delete:
        print("\nNo duplicates to remove.")
        return

    print(f"\n{len(to_delete)} part(y/ies) would be deleted.")
    if not args.apply:
        print("Dry run only — re-run with --apply to actually delete.")
        return

    print("Logging in...")
    token = login()
    headers = {"Authorization": f"Bearer {token}"}

    deleted = 0
    for p in to_delete:
        pid = p.get("_id") or p.get("id")
        try:
            r = requests.delete(f"{BACKEND}/api/admin/delete-party/{pid}", headers=headers, timeout=15)
            if r.status_code == 200:
                deleted += 1
            else:
                print(f"  Failed to delete {pid}: {r.status_code} {r.text[:200]}")
        except Exception as exc:
            print(f"  Error deleting {pid}: {exc}")

    print(f"Deleted {deleted}/{len(to_delete)} duplicate parties.")


if __name__ == "__main__":
    main()
