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

import requests as http_requests

import config
from scraper import GoOutScraper, GoOutAccount
from orchestrator import reattribute_party_to_account1

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
    finally:
        loop.close()


async def _async_sales_update(accounts: list[GoOutAccount], db, telegram_mgr=None):
    results = await asyncio.gather(
        *[_update_account(account, db, telegram_mgr) for account in accounts],
        return_exceptions=True,
    )
    new_sales = []
    for account, result in zip(accounts, results):
        if isinstance(result, Exception):
            logger.error(f"[{account.account_id}] Sales update error: {result}")
        else:
            new_sales.extend(result or [])

    if telegram_mgr and new_sales:
        try:
            msg = format_new_sales_telegram_summary(new_sales)
            telegram_mgr.send_message_sync(msg)
        except Exception as exc:
            logger.error(f"Failed to send sales summary to Telegram: {exc}")

    _reconcile_dual_account_referrals(accounts, db, telegram_mgr)


def _reconcile_dual_account_referrals(accounts: list[GoOutAccount], db, telegram_mgr=None):
    """
    Safety net for orchestrator.py's discovery-time reattribution: when the same
    GoOut event is visible to (and tracked by) both accounts, the live party on
    the site must carry account1's referral/hot-now attribution, never account2's
    — even if account2 discovered/created it first. Discovery already tries to
    fix this the moment it re-encounters the event in that day's active-event
    list, but that only works while the event is still upcoming; if discovery
    never got the chance (URL/name-date matching gap, or the event went inactive
    first), the wrong referral sticks forever. Sales tracking, unlike discovery,
    already computes go_out_id/account overlap every cycle regardless of whether
    the event is still active — so it can catch and fix what discovery missed.

    Confirmed live 2026-08-07: 10 of 51 dual-tracked events had the wrong
    (account2) referral, all past events discovery would never revisit.
    """
    if db is None or telegram_mgr is None:
        return
    account1 = next((a for a in accounts if a.account_id == "account1"), None)
    if not account1 or not account1.referral:
        return

    try:
        pipeline = [
            {"$group": {"_id": "$go_out_id", "accounts": {"$addToSet": "$account_id"}}},
            {"$match": {"accounts": "account1"}},
        ]
        dual_go_out_ids = {
            str(row["_id"]) for row in db.goout_sales.aggregate(pipeline)
            if len(row.get("accounts") or []) > 1
        }
    except Exception as exc:
        logger.warning(f"Dual-account referral reconciliation: aggregation failed: {exc}")
        return
    if not dual_go_out_ids:
        return

    try:
        resp = http_requests.get(f"{config.BACKEND_URL}/api/parties", timeout=15)
        resp.raise_for_status()
        parties = resp.json()
    except Exception as exc:
        logger.warning(f"Dual-account referral reconciliation: could not fetch parties: {exc}")
        return

    fixed = []
    for party in parties:
        event_id = party.get("goOutEventId")
        if event_id is None or str(event_id) not in dual_go_out_ids:
            continue
        if party.get("referralCode") == account1.referral:
            continue
        party_id = str(party.get("_id") or party.get("id") or "")
        base_url = party.get("originalUrl") or party.get("goOutUrl") or party.get("canonicalUrl")
        if not party_id or not base_url:
            continue
        if reattribute_party_to_account1(
            party_id, base_url, account1.referral, telegram_mgr._auth_headers()
        ):
            fixed.append(party.get("name") or party_id)
            logger.info(f"[reconcile] Re-attributed '{party.get('name')}' to account1 referral.")

    if fixed:
        logger.info(
            f"[reconcile] Fixed referral attribution for {len(fixed)} dual-tracked "
            f"event(s), now on account1: {', '.join(fixed)}"
        )


async def _update_account(account: GoOutAccount, db, telegram_mgr=None):
    scraper = GoOutScraper(account, db=db, telegram_mgr=telegram_mgr)
    new_sales = []
    try:
        ok = await scraper.ensure_session()
        if not ok:
            logger.warning(f"[{account.account_id}] Could not log in for sales update")
            return new_sales

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

            # Extra per-event stats pulled via the cf-relay Worker (views, real revenue
            # breakdown, per-date sales, buyer list, expenses, ...) — see
            # scraper.py::_fetch_endone_stats / cf-relay/README.md. Optional: empty dict
            # when CF_RELAY_URL isn't configured or every endpoint failed for this event.
            endone_stats = item.get("endone_stats") or {}
            views = (endone_stats.get("views") or {}).get("Views")
            real_revenue = (endone_stats.get("revenue") or {}).get("revenue") or {}

            # הכנסות לאירוע ("event_revenue") from the myEvents API is a dead field —
            # always null in practice, see goout-scraper/CLAUDE.md. The real, working
            # revenue source is GoOut's own endOne/getRevenueData own_revenue (a
            # cumulative counter, same shape as confirmed ticket counts) — prefer it.
            live_event_revenue = item.get("event_revenue")
            if live_event_revenue is None:
                live_event_revenue = real_revenue.get("own_revenue")

            existing = sales_coll.find_one(
                {"account_id": account.account_id, "go_out_id": go_out_id}
            )

            if existing:
                prev_confirmed      = existing.get("confirmed_count", 0)
                prev_event_revenue  = existing.get("event_revenue")
                # Reuse the price stored on first encounter (buyer paid that price).
                # `is not None` (not `or`) so a legitimately free ticket (₪0) isn't
                # discarded in favor of a re-fetched live_price.
                existing_price      = existing.get("ticket_price")
                stored_price        = existing_price if existing_price is not None else live_price
            else:
                prev_confirmed     = 0
                prev_event_revenue = None
                stored_price       = live_price

            # Guard against a transient scrape glitch reading 0 (or a lower count)
            # for an event that previously had real confirmed sales — trusting it
            # would both log a false "-N" delta now and a spurious "+N" delta next
            # run when the real count reappears (double-charging account1's flat fee).
            if confirmed_now < prev_confirmed:
                logger.warning(
                    f"[{account.account_id}] {event_name or go_out_id}: confirmed count "
                    f"regressed {prev_confirmed} → {confirmed_now}, treating as a transient "
                    f"read and keeping the previous snapshot value"
                )
                confirmed_now = prev_confirmed

            delta_confirmed     = confirmed_now - prev_confirmed
            delta_event_revenue = None
            if live_event_revenue is not None and prev_event_revenue is not None:
                delta_event_revenue = live_event_revenue - prev_event_revenue
            elif live_event_revenue is not None and prev_event_revenue is None:
                # First time real revenue data is available for this event (either a
                # brand-new event, or an existing one seeing its own_revenue populated
                # for the first time after the 2026-08-07 fix). Treat the full amount
                # as this period's delta — same lump-sum convention already used for
                # delta_confirmed above. This is real money that was never tracked
                # before, not a spurious spike; the alternative (baselining to 0) would
                # permanently lose every ticket sold before this fix shipped.
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
                new_sales.append({
                    "account_id":          account.account_id,
                    "event_name":          event_name or go_out_id,
                    "delta_confirmed":     delta_confirmed,
                    "delta_event_revenue": delta_event_revenue,
                    "revenue_earned":      rev,
                })

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
                    # GoOut's own Mongo _id for this event — already captured for
                    # free from myEvents (see scraper.py::_extract_sales_from_obj)
                    # and required by the endOne/* relay endpoints. Persisted here
                    # so wa_sales_watch.py's targeted fast polling doesn't need a
                    # separate lookup per watched event.
                    "mongo_id":        item.get("mongo_id"),
                    # Real per-event data from www.go-out.co/endOne/* (via cf-relay) —
                    # separate from the fields above, which come from the myEvents API.
                    "views":              views,
                    "real_total_revenue": real_revenue.get("total_revenue"),
                    "real_own_revenue":   real_revenue.get("own_revenue"),
                    "endone_stats":       endone_stats,
                }},
                upsert=True,
            )

    finally:
        await scraper.close()

    return new_sales


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


def format_new_sales_telegram_summary(new_sales: list[dict]) -> str:
    """Build a Telegram message reporting only the new sales seen this check."""
    lines = ["🎉 *New sale(s) detected*\n"]
    total_revenue = 0.0
    total_tickets = 0

    by_account: dict[str, list[dict]] = {}
    for sale in new_sales:
        by_account.setdefault(sale["account_id"], []).append(sale)

    for aid in sorted(by_account.keys()):
        lines.append(f"🔹 *{aid}*")
        for sale in by_account[aid]:
            delta_confirmed = sale["delta_confirmed"]
            rev = sale["revenue_earned"]
            total_revenue += rev
            total_tickets += max(0, delta_confirmed)
            name = sale["event_name"][:45]
            confirmed_str = f"+{delta_confirmed}" if delta_confirmed else "±0"
            lines.append(f"  • {name}  ✅{confirmed_str}  →₪{rev:.2f}")
        lines.append("")

    lines.append(f"💼 *Total: ₪{total_revenue:.2f}* | {total_tickets} tickets")

    return "\n".join(lines)


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
