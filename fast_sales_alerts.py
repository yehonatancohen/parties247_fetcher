"""Fast, browser-free alerts for newly confirmed GoOut ticket sales.

The regular sales tracker remains the source of truth for revenue and runs
every four hours. This watcher polls only the authenticated organizer's
Accepted ticket count for upcoming site events every 20 minutes, then sends
one grouped Telegram message when counts rise. It uses the existing cf-relay
and saved GoOut JWT; it does not log in or invoke an undocumented public API.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

import requests

import config
from endone_relay import extract_token_from_storage_state, fetch_endone_stats_sync

logger = logging.getLogger(__name__)


def select_upcoming_events(parties: list[dict], sales_docs: list[dict]) -> list[dict]:
    """Join public upcoming site parties to known GoOut events.

    When an event is tracked under both organizer accounts, prefer account1,
    matching the sales/referral attribution rule used by sales_tracker.
    """
    sales_by_event: dict[str, dict] = {}
    for doc in sales_docs:
        event_id = str(doc.get("go_out_id") or "")
        if not event_id or not doc.get("mongo_id"):
            continue
        if event_id not in sales_by_event or doc.get("account_id") == "account1":
            sales_by_event[event_id] = doc

    events: dict[str, dict] = {}
    for party in parties or []:
        event_id = str(party.get("goOutEventId") or "")
        known = sales_by_event.get(event_id)
        if not event_id or not known:
            continue
        events.setdefault(event_id, {
            "go_out_id": event_id,
            "account_id": known["account_id"],
            "mongo_id": known["mongo_id"],
            "event_name": party.get("name") or known.get("event_name") or event_id,
        })
    return list(events.values())


def run_fast_sales_alerts(db, telegram_mgr=None, *, http=requests,
                          now: datetime | None = None) -> dict:
    """Poll upcoming parties and notify once, in aggregate, for count increases."""
    now = now or datetime.now(timezone.utc)
    result = {"checked": 0, "sales": 0, "failed": 0}
    if db is None or not (config.CF_RELAY_URL and config.CF_RELAY_SECRET):
        return result

    try:
        response = http.get(f"{config.BACKEND_URL}/api/parties?upcoming=true", timeout=20)
        response.raise_for_status()
        parties = response.json()
        if not isinstance(parties, list):
            raise ValueError("upcoming parties response was not a list")
        sales_docs = list(db.goout_sales.find(
            {}, {"go_out_id": 1, "account_id": 1, "mongo_id": 1, "event_name": 1, "confirmed_count": 1}
        ))
    except Exception as exc:
        logger.warning("fast_sales_alerts: couldn't load upcoming events: %s", exc)
        return result

    events = select_upcoming_events(parties, sales_docs)
    result["checked"] = len(events)
    if not events:
        logger.info("fast_sales_alerts: no upcoming events with known GoOut IDs")
        return result

    sales_by_key = {(str(d.get("go_out_id")), d.get("account_id")): d for d in sales_docs}
    session_docs: dict[str, dict] = {}
    for account_id in {event["account_id"] for event in events}:
        session_docs[account_id] = db.goout_sessions.find_one({"account_id": account_id}) or {}

    pending_alerts = []
    failed_accounts: set[str] = set()
    for event in events:
        account_id = event["account_id"]
        if account_id in failed_accounts:
            continue
        token = extract_token_from_storage_state(session_docs[account_id].get("storage_state"))
        if not token:
            result["failed"] += 1
            failed_accounts.add(account_id)
            logger.warning("fast_sales_alerts: saved GoOut session missing for %s; skipping its remaining events", account_id)
            continue

        stats = fetch_endone_stats_sync(
            event["mongo_id"], auth_header=f"Bearer {token}",
            relay_url=config.CF_RELAY_URL, relay_secret=config.CF_RELAY_SECRET,
            fields=("ticket_stats",), session=http, retries=1,
        )
        accepted = (stats.get("ticket_stats") or {}).get("Accepted")
        if accepted is None:
            result["failed"] += 1
            failed_accounts.add(account_id)
            logger.warning("fast_sales_alerts: organizer stats unavailable for %s; skipping its remaining events", account_id)
            continue
        try:
            accepted = max(0, int(accepted))
        except (TypeError, ValueError):
            result["failed"] += 1
            failed_accounts.add(account_id)
            continue

        state_key = {"account_id": account_id, "go_out_id": event["go_out_id"]}
        state = db.goout_fast_sales_state.find_one(state_key)
        if state:
            last_alerted = int(state.get("last_alerted_count") or 0)
        else:
            baseline = sales_by_key.get((event["go_out_id"], account_id), {})
            last_alerted = int(baseline.get("confirmed_count") or 0)
        if accepted > last_alerted:
            pending_alerts.append({**event, "delta": accepted - last_alerted, "accepted": accepted})

        # Persist observed count, but keep last_alerted_count unchanged until a
        # Telegram send succeeds. A transient Telegram outage therefore retries
        # the same grouped alert on the next poll rather than losing it.
        db.goout_fast_sales_state.update_one(
            state_key,
            {"$set": {**state_key, "event_name": event["event_name"],
                       "observed_count": accepted, "last_checked": now},
             "$setOnInsert": {"last_alerted_count": last_alerted}},
            upsert=True,
        )

    result["sales"] = sum(row["delta"] for row in pending_alerts)
    if pending_alerts and telegram_mgr:
        lines = ["🎟️ זוהו מכירות חדשות:"]
        for sale in pending_alerts:
            lines.append(f"• {html.escape(str(sale['event_name']))}: +{sale['delta']} כרטיסים")
        message = "\n".join(lines)
        sent = telegram_mgr.send_message_sync(message, parse_mode="HTML")
        if sent:
            for sale in pending_alerts:
                db.goout_fast_sales_state.update_one(
                    {"account_id": sale["account_id"], "go_out_id": sale["go_out_id"]},
                    {"$set": {"last_alerted_count": sale["accepted"], "last_alerted_at": now}},
                )
        else:
            logger.warning("fast_sales_alerts: Telegram delivery failed; will retry next tick")

    logger.info("fast_sales_alerts: checked=%d new_tickets=%d failed=%d",
                result["checked"], result["sales"], result["failed"])
    return result
