"""
Targeted, fast ticket-sales polling for parties a WhatsApp campaign just
touched — the fetcher's normal sales_update job only runs every 4 hours,
too coarse to tell whether an 18:00 send or a 21:00 send drove a sale. This
module polls just the handful of events the backend flags as "queued" (need
a pre-send baseline) or "recently sent" (need the post-send curve), every
20 minutes (see main.py), and writes append-only snapshots to
`goout_sales_snapshots` for parties247_backend/wa_facts.py to diff.

Deliberately browser-free: it reuses the JWT already saved on
`goout_sessions.storage_state` (see endone_relay.py) rather than driving
Playwright, so it can never trigger a login or 2FA. If a session has no
token yet (never logged in) or the relay call fails, that account is
skipped for this tick — no retry storm, no fallback to a real login.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

import config
from endone_relay import extract_token_from_storage_state, fetch_endone_stats_sync

logger = logging.getLogger(__name__)

# Skip an account for this long after a failed/empty relay call, so a
# CAPTCHA or an expired session doesn't turn into a call-per-event storm
# every 20 minutes. The normal 4h sales_update job is unaffected.
BACKOFF_COOLDOWN = timedelta(hours=2)

# One alert per account per this interval, same shape as
# alerts.should_send_captcha_alert but a distinct field so the two never
# clobber each other's throttle state on the same goout_sessions doc.
ALERT_MIN_INTERVAL = timedelta(hours=6)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def select_watchlist_events(watchlist: list[dict], sales_by_event: dict[str, dict]) -> list[dict]:
    """
    Join the backend's watchlist (goOutEventId + why it's watched) against
    what we actually know about that event from `goout_sales` (which
    account owns it, and its GoOut mongo _id — required by endOne/*).
    Events we've never scraped a sales snapshot for yet (no mongo_id) are
    dropped; the next 4h sales_update will pick them up and this watch can
    catch them from their next tick onward.
    """
    events = []
    for entry in watchlist or []:
        go_out_id = str(entry.get("goOutEventId") or "")
        known = sales_by_event.get(go_out_id)
        if not known or not known.get("mongo_id"):
            continue
        events.append({
            "go_out_id": go_out_id,
            "account_id": known["account_id"],
            "mongo_id": known["mongo_id"],
            "reasons": entry.get("reasons") or [],
        })
    return events


def should_back_off(last_failure_at: datetime | None, now: datetime,
                     cooldown: timedelta = BACKOFF_COOLDOWN) -> bool:
    """True while a prior failure for this account is still within its
    cooldown window. Naive `last_failure_at` is treated as UTC, matching
    every other datetime this fetcher stores in Mongo."""
    if last_failure_at is None:
        return False
    if last_failure_at.tzinfo is None:
        last_failure_at = last_failure_at.replace(tzinfo=timezone.utc)
    return now - last_failure_at < cooldown


def should_send_watch_alert(last_alert_at: datetime | None, now: datetime,
                            min_interval: timedelta = ALERT_MIN_INTERVAL) -> bool:
    if last_alert_at is None:
        return True
    if last_alert_at.tzinfo is None:
        last_alert_at = last_alert_at.replace(tzinfo=timezone.utc)
    return now - last_alert_at >= min_interval


def build_snapshot_doc(account_id: str, go_out_id: str, at: datetime, endone_stats: dict) -> dict:
    """One row for `goout_sales_snapshots` — append-only, so
    parties247_backend/wa_facts.py can diff any two points in time instead
    of only ever seeing the latest state (what `goout_sales` gives)."""
    ticket_stats = endone_stats.get("ticket_stats") or {}
    revenue = (endone_stats.get("revenue") or {}).get("revenue") or {}
    return {
        "account_id": account_id,
        "go_out_id": go_out_id,
        "at": at,
        "accepted": ticket_stats.get("Accepted"),
        "pending": ticket_stats.get("Pending"),
        "ownRevenue": revenue.get("own_revenue"),
        # Raw last-accepted-users payload, kept as-is: we don't yet know its
        # shape on an event with real sales (per-buyer timestamps would let
        # a future pass replace this whole window-diff approach with real
        # sale times — see the fetcher CLAUDE.md's "Endpoint discovery"
        # notes), so nothing here should throw it away.
        "lastAcceptedRaw": endone_stats.get("last_accepted"),
    }


def get_watchlist(*, backend_url: str, service_token: str, http=requests, timeout: float = 15.0) -> list[dict]:
    resp = http.get(
        f"{backend_url}/api/internal/wa/watchlist",
        headers={"X-Service-Token": service_token},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("watchlist", [])


def trigger_facts_rebuild(*, backend_url: str, service_token: str, http=requests, timeout: float = 30.0) -> None:
    """Best-effort: ask the backend to recompute waSendFacts now that fresh
    snapshots exist. A failure here just means the dashboard is one tick
    stale, never a data-loss risk (the rebuild is idempotent and re-runs on
    every subsequent tick and after every 4h sales run)."""
    try:
        http.post(
            f"{backend_url}/api/internal/wa/rebuild-facts",
            headers={"X-Service-Token": service_token},
            json={"days": 14},
            timeout=timeout,
        )
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning(f"Failed to trigger wa facts rebuild: {exc}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_wa_sales_watch(db, telegram_mgr=None, *, http=requests, now: datetime | None = None) -> dict:
    """One tick: fetch the watchlist, snapshot each watched event via the
    relay (using each account's already-saved session token), back off per
    account on failure, then ask the backend to rebuild waSendFacts."""
    now = now or datetime.now(timezone.utc)
    summary = {"watched": 0, "snapshotted": 0, "skipped_backoff": 0, "failed": []}

    if db is None:
        return summary

    if not (config.CF_RELAY_URL and config.CF_RELAY_SECRET):
        logger.info("CF_RELAY_URL/CF_RELAY_SECRET not configured — skipping wa sales watch.")
        return summary

    try:
        watchlist = get_watchlist(backend_url=config.BACKEND_URL, service_token=config.SERVICE_TOKEN, http=http)
    except Exception as exc:
        logger.warning(f"wa_sales_watch: failed to fetch watchlist: {exc}")
        return summary

    if not watchlist:
        logger.info("wa_sales_watch: nothing on the backend's watchlist this tick.")
        return summary

    sales_by_event: dict[str, dict] = {}
    try:
        for doc in db.goout_sales.find({}, {"go_out_id": 1, "account_id": 1, "mongo_id": 1}):
            if doc.get("go_out_id") and doc.get("mongo_id"):
                sales_by_event[str(doc["go_out_id"])] = doc
    except Exception as exc:
        logger.warning(f"wa_sales_watch: failed to read goout_sales: {exc}")
        return summary

    events = select_watchlist_events(watchlist, sales_by_event)
    summary["watched"] = len(events)
    if not events:
        # The backend flagged campaigns to watch, but none had a matching
        # goout_sales doc with a mongo_id yet — normal for a party just
        # queued moments ago, before the next 4h sales_update has scraped
        # it at all. Worth distinguishing from "nothing on the watchlist"
        # (above) if this ever shows up more than transiently.
        logger.info(f"wa_sales_watch: {len(watchlist)} watchlist entr{'y' if len(watchlist) == 1 else 'ies'}, "
                    "none resolvable to a known goout_sales doc yet.")
        return summary

    tokens_by_account: dict[str, str | None] = {}
    session_docs: dict[str, dict] = {}
    for account_id in {e["account_id"] for e in events}:
        session_doc = db.goout_sessions.find_one({"account_id": account_id}) or {}
        session_docs[account_id] = session_doc
        tokens_by_account[account_id] = extract_token_from_storage_state(session_doc.get("storage_state"))

    for event in events:
        account_id = event["account_id"]
        session_doc = session_docs.get(account_id, {})

        if should_back_off(session_doc.get("wa_watch_last_failure_at"), now):
            summary["skipped_backoff"] += 1
            continue

        token = tokens_by_account.get(account_id)
        if not token:
            _record_watch_failure(db, account_id, now)
            summary["failed"].append(event["go_out_id"])
            continue

        stats = fetch_endone_stats_sync(
            event["mongo_id"], auth_header=f"Bearer {token}",
            relay_url=config.CF_RELAY_URL, relay_secret=config.CF_RELAY_SECRET,
            session=http,
        )
        if not stats:
            _record_watch_failure(db, account_id, now)
            if should_send_watch_alert(session_doc.get("wa_watch_last_alert_at"), now):
                _record_watch_alert(db, account_id, now)
                logger.warning(
                    f"[{account_id}] WhatsApp sales-watch couldn't reach the endOne relay "
                    f"(checked while snapshotting a recently-promoted party). Backing off {BACKOFF_COOLDOWN}."
                )
            summary["failed"].append(event["go_out_id"])
            continue

        snapshot = build_snapshot_doc(account_id, event["go_out_id"], now, stats)
        try:
            db.goout_sales_snapshots.insert_one(snapshot)
            summary["snapshotted"] += 1
        except Exception as exc:  # pragma: no cover - best effort persistence
            logger.error(f"wa_sales_watch: failed to insert snapshot for {event['go_out_id']}: {exc}")

    trigger_facts_rebuild(backend_url=config.BACKEND_URL, service_token=config.SERVICE_TOKEN, http=http)
    # A summary on every tick, not just failures — the only other signal
    # this job ran at all was silence, which is indistinguishable from a
    # stuck scheduler or a caught-but-unlogged exception (found while
    # verifying the 2026-09-13 watchlist fix live: even a fully successful
    # run left zero trace in the logs).
    logger.info(
        f"wa_sales_watch: watched={summary['watched']} snapshotted={summary['snapshotted']} "
        f"skipped_backoff={summary['skipped_backoff']} failed={len(summary['failed'])}"
    )
    return summary


def _record_watch_failure(db, account_id: str, now: datetime) -> None:
    try:
        db.goout_sessions.update_one(
            {"account_id": account_id},
            {"$set": {"wa_watch_last_failure_at": now}},
            upsert=True,
        )
    except Exception:  # pragma: no cover - best effort
        pass


def _record_watch_alert(db, account_id: str, now: datetime) -> None:
    try:
        db.goout_sessions.update_one(
            {"account_id": account_id},
            {"$set": {"wa_watch_last_alert_at": now}},
            upsert=True,
        )
    except Exception:  # pragma: no cover - best effort
        pass
