"""
Sales tracking for Go-Out accounts.

Periodically scrapes confirmed (מאושרים), pending (ממתינים), and total event
revenue (הכנסות לאירוע) from the business panel and stores snapshots + revenue
deltas in MongoDB — for both active and inactive/past events.

Revenue rules
-------------
- account1 : flat ₪25 per new confirmed ticket (delta in confirmed count)
- account2 : 6% of הכנסות לאירוע delta (change in total gross event revenue).
              Falls back to 6% × ticket_price × delta_confirmed when
              הכנסות לאירוע is unavailable.
"""

import asyncio
import logging
from datetime import datetime, timezone

from scraper import GoOutScraper, GoOutAccount

logger = logging.getLogger(__name__)

ACCOUNT1_FLAT_FEE = 25.0   # ₪ per confirmed ticket
ACCOUNT2_PCT      = 0.06   # 6 % of gross event revenue


def _year_month(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _revenue_account1(delta_confirmed: int) -> float:
    return max(0, delta_confirmed) * ACCOUNT1_FLAT_FEE


def _revenue_account2(delta_event_revenue: float | None,
                      delta_confirmed: int,
                      ticket_price: float | None) -> float:
    """
    Prefer הכנסות לאירוע delta × 6%.
    Fall back to confirmed_delta × ticket_price × 6% when revenue field absent.
    """
    if delta_event_revenue is not None and delta_event_revenue > 0:
        return delta_event_revenue * ACCOUNT2_PCT
    if ticket_price and delta_confirmed > 0:
        return delta_confirmed * ticket_price * ACCOUNT2_PCT
    return 0.0


def _calc_revenue(account_id: str,
                  delta_confirmed: int,
                  delta_event_revenue: float | None,
                  ticket_price: float | None) -> float:
    if "account1" in account_id:
        return _revenue_account1(delta_confirmed)
    if "account2" in account_id:
        return _revenue_account2(delta_event_revenue, delta_confirmed, ticket_price)
    return 0.0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_sales_update(accounts: list[GoOutAccount], db, telegram_mgr=None):
    """Synchronous entry point — scrape sales for every account and store deltas."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_async_sales_update(accounts, db, telegram_mgr))
    except Exception as exc:
        logger.error(f"Sales update failed: {exc}")
        if telegram_mgr:
            telegram_mgr.send_message_sync(f"❌ Sales update error: {exc}")
    finally:
        loop.close()


async def _async_sales_update(accounts: list[GoOutAccount], db, telegram_mgr=None):
    results = await asyncio.gather(
        *[_update_account(account, db, telegram_mgr) for account in accounts],
        return_exceptions=True,
    )
    any_error = False
    for account, result in zip(accounts, results):
        if isinstance(result, Exception):
            any_error = True
            logger.error(f"[{account.account_id}] Sales update error: {result}")
            if telegram_mgr:
                telegram_mgr.send_message_sync(
                    f"⚠️ Sales update failed for *{account.account_id}*: {result}"
                )
    if telegram_mgr and not any_error:
        try:
            msg = format_sales_telegram_summary(db)
            telegram_mgr.send_message_sync(msg)
        except Exception as exc:
            logger.error(f"Failed to send sales summary to Telegram: {exc}")


async def _update_account(account: GoOutAccount, db, telegram_mgr=None):
    scraper = GoOutScraper(account, db=db, telegram_mgr=telegram_mgr)
    try:
        ok = await scraper.ensure_session()
        if not ok:
            logger.warning(f"[{account.account_id}] Could not log in for sales update")
            return

        live_sales = await scraper.scrape_sales_data()
        now = datetime.now(timezone.utc)
        ym  = _year_month(now)

        sales_coll = db.goout_sales      # latest state per (account, event)
        sales_log  = db.goout_sales_log  # per-period revenue delta log

        for item in live_sales:
            go_out_id = item.get("go_out_id")
            if not go_out_id:
                continue

            event_name         = item.get("event_name") or ""
            confirmed_now      = int(item.get("confirmed")     or 0)
            pending_now        = int(item.get("pending")       or 0)
            live_price         = item.get("ticket_price")
            live_event_revenue = item.get("event_revenue")  # הכנסות לאירוע

            existing = sales_coll.find_one(
                {"account_id": account.account_id, "go_out_id": go_out_id}
            )

            if existing:
                prev_confirmed      = existing.get("confirmed_count", 0)
                prev_event_revenue  = existing.get("event_revenue")
                # Reuse the price stored on first encounter (buyer paid that price)
                stored_price        = existing.get("ticket_price") or live_price
            else:
                prev_confirmed     = 0
                prev_event_revenue = None
                stored_price       = live_price

            delta_confirmed     = confirmed_now - prev_confirmed
            delta_event_revenue = None
            if live_event_revenue is not None and prev_event_revenue is not None:
                delta_event_revenue = live_event_revenue - prev_event_revenue
            elif live_event_revenue is not None and prev_event_revenue is None:
                # First time we see the revenue field — treat full amount as this period's delta
                delta_event_revenue = live_event_revenue

            # Only log when something actually changed
            has_change = delta_confirmed > 0 or (delta_event_revenue is not None and delta_event_revenue > 0)
            if has_change:
                rev = _calc_revenue(account.account_id, delta_confirmed,
                                    delta_event_revenue, stored_price)
                sales_log.insert_one({
                    "account_id":          account.account_id,
                    "go_out_id":           go_out_id,
                    "event_name":          event_name,
                    "delta_confirmed":     delta_confirmed,
                    "delta_event_revenue": delta_event_revenue,
                    "ticket_price":        stored_price,
                    "revenue_earned":      rev,
                    "recorded_at":         now,
                    "year_month":          ym,
                })
                logger.info(
                    f"[{account.account_id}] {event_name or go_out_id}: "
                    f"+{delta_confirmed} confirmed, "
                    f"revenue delta=₪{delta_event_revenue or 0:.2f} → "
                    f"earned ₪{rev:.2f}"
                )

            # Upsert the latest snapshot
            sales_coll.update_one(
                {"account_id": account.account_id, "go_out_id": go_out_id},
                {"$set": {
                    "account_id":     account.account_id,
                    "go_out_id":      go_out_id,
                    "event_name":     event_name,
                    "confirmed_count": confirmed_now,
                    "pending_count":   pending_now,
                    "ticket_price":    stored_price,
                    "event_revenue":   live_event_revenue,
                    "last_updated":    now,
                }},
                upsert=True,
            )

    finally:
        await scraper.close()


# ---------------------------------------------------------------------------
# Reporting helpers (used by Telegram bot)
# ---------------------------------------------------------------------------

def get_sales_summary(db) -> list[dict]:
    """
    Per-event totals across all accounts (all-time / lifetime).

    Returns list of:
        {account_id, go_out_id, event_name, confirmed_count, pending_count,
         ticket_price, event_revenue, total_revenue_earned, last_updated}
    """
    sales_coll = db.goout_sales
    sales_log  = db.goout_sales_log

    rows = list(sales_coll.find({}, {"_id": 0}))

    # Sum lifetime revenue per (account, event) from the log
    pipeline = [
        {"$group": {
            "_id": {"account_id": "$account_id", "go_out_id": "$go_out_id"},
            "total_revenue_earned": {"$sum": "$revenue_earned"},
            "total_confirmed":      {"$sum": "$delta_confirmed"},
        }}
    ]
    rev_map: dict[tuple, dict] = {}
    for doc in sales_log.aggregate(pipeline):
        k = (doc["_id"]["account_id"], doc["_id"]["go_out_id"])
        rev_map[k] = {
            "total_revenue_earned": doc["total_revenue_earned"],
            "total_confirmed_delta": doc["total_confirmed"],
        }

    for row in rows:
        k = (row.get("account_id", ""), row.get("go_out_id", ""))
        agg = rev_map.get(k, {})
        row["total_revenue_earned"] = agg.get("total_revenue_earned", 0.0)

    rows.sort(key=lambda r: r.get("account_id", ""))
    return rows


def get_lifetime_total(db) -> dict:
    """
    Grand total across all accounts and all time.

    Returns:
        {total_revenue, by_account: {account_id: revenue}}
    """
    sales_log = db.goout_sales_log
    pipeline = [
        {"$group": {
            "_id": "$account_id",
            "revenue": {"$sum": "$revenue_earned"},
            "tickets": {"$sum": "$delta_confirmed"},
        }}
    ]
    by_account = {}
    total = 0.0
    for doc in sales_log.aggregate(pipeline):
        by_account[doc["_id"]] = {"revenue": doc["revenue"], "tickets": doc["tickets"]}
        total += doc["revenue"]
    return {"total_revenue": total, "by_account": by_account}


def get_monthly_report(db, year_month: str | None = None) -> dict:
    """
    Revenue breakdown for a given month (defaults to current month).
    Includes both active and past events that had sales activity in that period.

    Returns:
        {year_month, by_account: {account_id: {revenue, tickets, events:[...]}},
         total_revenue, total_tickets}
    """
    if year_month is None:
        year_month = _year_month(datetime.now(timezone.utc))

    sales_log = db.goout_sales_log
    pipeline = [
        {"$match": {"year_month": year_month}},
        {"$group": {
            "_id": {
                "account_id": "$account_id",
                "go_out_id":  "$go_out_id",
                "event_name": "$event_name",
            },
            "tickets":            {"$sum": "$delta_confirmed"},
            "revenue":            {"$sum": "$revenue_earned"},
            "ticket_price":       {"$first": "$ticket_price"},
            "delta_event_revenue":{"$sum": "$delta_event_revenue"},
        }},
    ]
    docs = list(sales_log.aggregate(pipeline))

    by_account: dict[str, dict] = {}
    for doc in docs:
        aid   = doc["_id"]["account_id"]
        ename = doc["_id"]["event_name"] or doc["_id"]["go_out_id"]
        if aid not in by_account:
            by_account[aid] = {"revenue": 0.0, "tickets": 0, "events": []}
        by_account[aid]["revenue"]  += doc["revenue"]
        by_account[aid]["tickets"]  += doc["tickets"]
        by_account[aid]["events"].append({
            "event_name":          ename,
            "go_out_id":           doc["_id"]["go_out_id"],
            "tickets":             doc["tickets"],
            "revenue":             doc["revenue"],
            "ticket_price":        doc.get("ticket_price"),
            "delta_event_revenue": doc.get("delta_event_revenue"),
        })

    total_revenue = sum(v["revenue"] for v in by_account.values())
    total_tickets = sum(v["tickets"] for v in by_account.values())

    return {
        "year_month":    year_month,
        "by_account":    by_account,
        "total_revenue": total_revenue,
        "total_tickets": total_tickets,
    }


def get_available_months(db) -> list[str]:
    """Sorted list of year_month strings that have sales log entries."""
    pipeline = [
        {"$group": {"_id": "$year_month"}},
        {"$sort": {"_id": 1}},
    ]
    return [doc["_id"] for doc in db.goout_sales_log.aggregate(pipeline)]


def format_sales_telegram_summary(db, year_month: str | None = None) -> str:
    """
    Build a Telegram-ready Markdown summary of current-state sales.

    Uses goout_sales (live snapshot) for per-event ticket counts,
    and goout_sales_log for month revenue totals.
    """
    if year_month is None:
        year_month = _year_month(datetime.now(timezone.utc))

    # Month revenue from log
    monthly = get_monthly_report(db, year_month)

    # Live per-event state grouped by account
    rows = list(db.goout_sales.find({}, {"_id": 0}))
    by_account: dict[str, list[dict]] = {}
    for row in rows:
        aid = row.get("account_id", "unknown")
        by_account.setdefault(aid, []).append(row)

    lines = ["📊 *Sales Update — Last 30 days*\n"]

    grand_total_revenue = 0.0
    grand_total_tickets = 0

    for aid in sorted(by_account.keys()):
        events = by_account[aid]
        month_info = monthly["by_account"].get(aid, {"revenue": 0.0, "tickets": 0})
        month_rev = month_info["revenue"]
        grand_total_revenue += month_rev

        has_tickets = [e for e in events if (e.get("confirmed_count") or 0) > 0]
        total_tickets = sum(e.get("confirmed_count", 0) for e in has_tickets)
        grand_total_tickets += total_tickets

        lines.append(f"🔹 *{aid}* — ₪{month_rev:.2f} this month")

        if has_tickets:
            for ev in sorted(has_tickets, key=lambda x: x.get("confirmed_count", 0), reverse=True):
                name = (ev.get("event_name") or ev.get("go_out_id") or "?")[:45]
                confirmed = ev.get("confirmed_count", 0)
                pending = ev.get("pending_count", 0)
                price = ev.get("ticket_price")
                pending_str = f"  ⏳{pending}" if pending else ""
                # Per-event earnings from monthly log
                ev_log = next(
                    (e for e in month_info.get("events", []) if e["go_out_id"] == ev.get("go_out_id")),
                    None,
                )
                rev_str = f"  →₪{ev_log['revenue']:.0f}" if ev_log and ev_log.get("revenue") else ""
                lines.append(f"  • {name}  ✅{confirmed}{pending_str}{rev_str}")
        else:
            total_events = len(events)
            lines.append(f"  _(0 tickets across {total_events} events)_")

        lines.append("")

    lines.append(f"💼 *Total {year_month}: ₪{grand_total_revenue:.2f}* | {grand_total_tickets} tickets")

    return "\n".join(lines)
