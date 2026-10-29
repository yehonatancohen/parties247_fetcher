"""
One-shot fix for goout_sales_log rows that re-booked an event's whole lifetime revenue.

Happened 2026-10-29: the 10-28 20:05 sales run had a dead GoOut token, wrote
event_revenue=None into every snapshot, and the first run after the re-login
(10-29 ~08:05 UTC) counted each event's full own_revenue as new. sales_tracker.py
now keeps the last known value, so this only cleans up what was already written.

A row is a re-count when its revenue delta is at least everything already booked
for that event before it. The real new part is `delta - earlier sum`; the row is
corrected in place (original values kept under `double_count_fix`), never deleted.

    python fix_double_revenue.py                 # dry run, 2026-10-29 window
    python fix_double_revenue.py --apply         # write the corrections
    python fix_double_revenue.py --all-history   # report only: same pattern in any month
"""

import argparse
import os
from datetime import datetime, timezone

from pymongo import MongoClient

from sales_tracker import _calc_revenue

WINDOW = (datetime(2026, 10, 29, 8, 0), datetime(2026, 10, 29, 8, 30))


def find_recounts(db, start=None, end=None):
    query = {"delta_event_revenue": {"$gt": 0}, "double_count_fix": {"$exists": False}}
    if start:
        query["recorded_at"] = {"$gte": start, "$lt": end}
    for row in db.goout_sales_log.find(query).sort("recorded_at", 1):
        earlier = db.goout_sales_log.find({
            "account_id": row["account_id"], "go_out_id": row["go_out_id"],
            "recorded_at": {"$lt": row["recorded_at"]},
        })
        booked = sum(r.get("delta_event_revenue") or 0 for r in earlier)
        if booked > 0 and row["delta_event_revenue"] >= booked - 0.01:
            yield row, booked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--all-history", action="store_true")
    args = parser.parse_args()

    db = MongoClient(os.environ["MONGODB_URI"])[os.environ.get("MONGODB_DB_NAME", "party247")]
    print(f"db: {db.name}")
    window = (None, None) if args.all_history else WINDOW
    total_before = total_after = 0.0
    count = 0
    for row, booked in find_recounts(db, *window):
        new_delta = round(row["delta_event_revenue"] - booked, 2)
        new_earned = _calc_revenue(row["account_id"], row.get("delta_confirmed") or 0,
                                   new_delta, row.get("ticket_price"))
        old_earned = row.get("revenue_earned") or 0
        total_before += old_earned
        total_after += new_earned
        count += 1
        print(f"{row['recorded_at']:%Y-%m-%d %H:%M} {row['account_id']:<9} "
              f"{(row.get('event_name') or row['go_out_id'])[:40]:<40} "
              f"delta ₪{row['delta_event_revenue']:.2f} (already booked ₪{booked:.2f}) "
              f"→ ₪{new_delta:.2f} | earned ₪{old_earned:.2f} → ₪{new_earned:.2f}")
        if args.apply and not args.all_history:
            db.goout_sales_log.update_one({"_id": row["_id"]}, {"$set": {
                "delta_event_revenue": new_delta,
                "revenue_earned": new_earned,
                "double_count_fix": {
                    "delta_event_revenue": row["delta_event_revenue"],
                    "revenue_earned": old_earned,
                    "fixed_at": datetime.now(timezone.utc),
                },
            }})
    print(f"\n{count} rows, earned ₪{total_before:.2f} → ₪{total_after:.2f} "
          f"(over-credit ₪{total_before - total_after:.2f})"
          + ("" if args.apply and not args.all_history else "  [dry run]"))


if __name__ == "__main__":
    main()
