"""
Sales scraper dry-run test.

Logs into each configured Go-Out account, scrapes sales data from the
business panel, prints what was found, then (optionally) saves to MongoDB.

Usage:
    cd goout-scraper
    python test_sales.py            # dry run — no DB writes
    python test_sales.py --save     # actually write snapshots to MongoDB
"""

import sys
import os
import asyncio
import logging
import io

# Force UTF-8 output on Windows so Hebrew/emoji print without errors
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_sales")

from dotenv import load_dotenv
load_dotenv()

import pymongo
import config
from scraper import GoOutScraper, GoOutAccount
from sales_tracker import (
    run_sales_update,
    get_sales_summary,
    get_lifetime_total,
    get_monthly_report,
    get_available_months,
    ACCOUNT1_FLAT_FEE,
    ACCOUNT2_PCT,
)


def _build_accounts() -> list[GoOutAccount]:
    return [
        GoOutAccount(
            account_id=cfg["account_id"],
            email=cfg["email"],
            password=cfg["password"],
            referral=cfg.get("referral", ""),
        )
        for cfg in config.GOOUT_ACCOUNTS
    ]


async def _scrape_one(account: GoOutAccount, db) -> list[dict]:
    scraper = GoOutScraper(account, db=db)
    try:
        ok = await scraper.ensure_session()
        if not ok:
            print(f"  ❌ Could not log in to {account.account_id}")
            return []
        print(f"  ✅ Logged in as {account.account_id}")
        sales = await scraper.scrape_sales_data()
        return sales
    finally:
        await scraper.close()


def _print_sales(account_id: str, sales: list[dict]):
    SEP = "─" * 70
    print(f"\n{SEP}")
    print(f"  {account_id}  ({len(sales)} events found)")
    print(SEP)

    if not sales:
        print("  (no sales data)")
        return

    for item in sorted(sales, key=lambda x: x.get("confirmed", 0), reverse=True):
        name      = item.get("event_name") or item.get("go_out_id") or "?"
        confirmed = item.get("confirmed", 0)
        pending   = item.get("pending", 0)
        price     = item.get("ticket_price")
        gross     = item.get("event_revenue")  # הכנסות לאירוע

        # Revenue preview
        if "account1" in account_id:
            rev = confirmed * ACCOUNT1_FLAT_FEE
            rev_note = f"₪{rev:.2f}  ({confirmed} × ₪{ACCOUNT1_FLAT_FEE:.0f})"
        elif "account2" in account_id:
            if gross is not None:
                rev = gross * ACCOUNT2_PCT
                rev_note = f"₪{rev:.2f}  (₪{gross:.0f} הכנסות × 6%)"
            elif price and confirmed:
                rev = confirmed * price * ACCOUNT2_PCT
                rev_note = f"₪{rev:.2f}  ({confirmed} × ₪{price:.0f} × 6%) [fallback]"
            elif confirmed == 0:
                rev = 0.0
                rev_note = "₪0  (0 tickets sold)"
            else:
                rev = 0.0
                rev_note = "₪0  (no price data)"
        else:
            rev = 0.0
            rev_note = "unknown account type"

        gross_str = f"  הכנסות: ₪{gross:.0f}" if gross else ""
        price_str = f"  ticket: ₪{price:.0f}" if price else ""

        print(f"\n  📅 {name[:55]}")
        print(f"     ✅ {confirmed} מאושרים  ⏳ {pending} ממתינים{price_str}{gross_str}")
        print(f"     💰 My earnings: {rev_note}")


def _print_db_reports(db):
    print("\n" + "═" * 70)
    print("  DATABASE REPORTS")
    print("═" * 70)

    lifetime = get_lifetime_total(db)
    print(f"\n💼 Lifetime total: ₪{lifetime['total_revenue']:.2f}")
    for aid, info in sorted(lifetime["by_account"].items()):
        print(f"   • {aid}: ₪{info['revenue']:.2f}  ({info['tickets']} ticket increments logged)")

    months = get_available_months(db)
    if months:
        print(f"\n📅 Available months in DB: {', '.join(months)}")
        latest = months[-1]
        report = get_monthly_report(db, latest)
        print(f"\n   Monthly report — {latest}:")
        for aid, info in sorted(report["by_account"].items()):
            print(f"   {aid}: {info['tickets']} tickets · ₪{info['revenue']:.2f}")
            for ev in sorted(info["events"], key=lambda x: x["revenue"], reverse=True):
                gross = ev.get("delta_event_revenue")
                gross_str = f" (הכנסות Δ=₪{gross:.0f})" if gross else ""
                print(f"      • {ev['event_name'][:45]}: {ev['tickets']} tickets · ₪{ev['revenue']:.2f}{gross_str}")
    else:
        print("\n  No monthly data yet (run with --save to populate)")


def main():
    save_to_db = "--save" in sys.argv

    mongo_client = pymongo.MongoClient(config.MONGODB_URI)
    db = mongo_client[config.MONGODB_DB_NAME]

    try:
        mongo_client.admin.command("ping")
        print(f"✅ Connected to MongoDB")
    except Exception as exc:
        print(f"⚠️  MongoDB ping failed: {exc} — continuing anyway")

    accounts = _build_accounts()
    if not accounts:
        print("❌ No accounts configured (check .env)")
        return

    print(f"\n{'═'*70}")
    print(f"  GO-OUT SALES TEST  ({'SAVE MODE' if save_to_db else 'DRY RUN — no DB writes'})")
    print(f"  accounts: {[a.account_id for a in accounts]}")
    print(f"{'═'*70}\n")

    loop = asyncio.new_event_loop()

    all_results: dict[str, list[dict]] = {}
    for account in accounts:
        print(f"→ Scraping {account.account_id}...")
        try:
            sales = loop.run_until_complete(_scrape_one(account, db))
            all_results[account.account_id] = sales
        except Exception as exc:
            print(f"  ❌ Error: {exc}")
            all_results[account.account_id] = []

    loop.close()

    # Print what we found
    for account_id, sales in all_results.items():
        _print_sales(account_id, sales)

    if save_to_db:
        print(f"\n\n{'─'*70}")
        print("  Saving to MongoDB...")
        run_sales_update(accounts, db)
        print("  Done. Fetching DB reports...\n")
        _print_db_reports(db)
    else:
        print(f"\n\n{'─'*70}")
        print("  DRY RUN complete. Re-run with --save to write to MongoDB and see DB reports.")
        # Still show existing DB reports if data already there
        existing = get_sales_summary(db)
        if existing:
            print("  (showing existing DB data)\n")
            _print_db_reports(db)

    print()


if __name__ == "__main__":
    main()
