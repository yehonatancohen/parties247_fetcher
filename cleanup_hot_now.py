"""
One-shot script to set the hot-now carousel to ONLY account1 parties.
Run once to fix the 142-party accumulation.

Usage:
    python cleanup_hot_now.py
"""
import sys
import requests

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


def main():
    if not ACCOUNT1_REFERRAL:
        print("ERROR: account1 not found in GOOUT_ACCOUNTS config")
        sys.exit(1)

    print("Logging in...")
    token = login()
    headers = {"Authorization": f"Bearer {token}"}

    print("Fetching carousels...")
    carousels = requests.get(f"{BACKEND}/api/carousels", timeout=10).json()
    carousel = next((c for c in carousels if "חם עכשיו" in (c.get("title") or "")), None)
    if not carousel:
        print("ERROR: hot-now carousel not found")
        sys.exit(1)

    carousel_id = carousel["id"]
    current_ids = set(str(p) for p in carousel.get("partyIds", []))
    print(f"Hot-now carousel '{carousel['title']}' currently has {len(current_ids)} parties")

    print("Fetching all parties...")
    parties = requests.get(f"{BACKEND}/api/parties?upcoming=true", timeout=15).json()

    account1_ids = []
    for p in parties:
        if p.get("referralCode") != ACCOUNT1_REFERRAL:
            continue
        pid = str(p.get("_id") or p.get("id", ""))
        if pid:
            account1_ids.append(pid)

    print(f"  Account1 upcoming parties: {len(account1_ids)}")
    print(f"  Will remove {len(current_ids - set(account1_ids))} non-account1 / stale entries")

    confirm = input("Apply? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    resp = requests.put(
        f"{BACKEND}/api/admin/carousels/{carousel_id}/parties",
        json={"partyIds": account1_ids},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    print(f"Done! Hot-now carousel now has {len(account1_ids)} account1 parties.")


if __name__ == "__main__":
    main()
