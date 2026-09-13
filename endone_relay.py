"""
Shared shape for calling www.go-out.co/endOne/* through the cf-relay Worker
(direct calls from the VPS are blocked — see cf-relay/README.md and this
repo's CLAUDE.md "Fixed 2026-08-07 (third pass)").

The endpoint map used to live only in scraper.py, called through Playwright's
async `context.request` (it needs a live browser context for cookies). This
module holds the map itself plus a synchronous, browser-free caller for
wa_sales_watch.py, which polls a handful of already-known events on a plain
`requests` session — no Playwright, no login, so it can never trigger 2FA.
scraper.py imports ENDONE_STATS_ENDPOINTS from here so the two call sites
can't drift apart; its own async fetch loop is unchanged.
"""

from __future__ import annotations

import json
import logging
import time

import requests

GO_OUT_BASE = "https://www.go-out.co"

# field name -> (relay path under /endOne/, extra body fields beyond {"eventId": mongo_id})
ENDONE_STATS_ENDPOINTS: dict[str, tuple[str, dict]] = {
    "views":             ("getEventViews", {}),
    "ticket_stats":      ("getUserTicketStatistics/", {}),
    "revenue":           ("getEventStatistics/getRevenueData", {}),
    "sales_per_date":    ("getEventStatistics/SalesPerDate", {}),
    "leading_salesman":  ("getXLeadingSalesman", {"numberOfUsers": 10}),
    "last_accepted":     ("getXLastAcceptedUsers", {"numberOfUsers": 25}),
    "expenses":          ("getTotalExpenses", {}),
    "top_tickets":       ("getTopTickets", {}),
    "last_day":          ("eventManagement/lastDayData", {}),
    "financial_summary": ("eventManagement/finnacialSummary", {}),
}

logger = logging.getLogger(__name__)


def extract_token_from_storage_state(storage_state: dict | None) -> str | None:
    """The JWT endOne/* auth needs lives in localStorage under key "user"
    (`{"token": "..."}`), not in a cookie — same fact scraper.py::_auth_header
    already relies on. `storage_state` is a Playwright storage_state dict
    (`{"cookies": [...], "origins": [{"origin", "localStorage": [...]}]}`),
    e.g. what's saved on `goout_sessions.storage_state`."""
    if not storage_state:
        return None
    for origin in storage_state.get("origins", []):
        for entry in origin.get("localStorage", []):
            if entry.get("name") == "user":
                try:
                    token = json.loads(entry.get("value") or "{}").get("token")
                except (TypeError, ValueError):
                    continue
                if token:
                    return str(token)
    return None


def fetch_endone_stats_sync(
    mongo_id: str,
    *,
    auth_header: str,
    relay_url: str,
    relay_secret: str,
    fields: tuple[str, ...] = ("ticket_stats", "revenue", "last_accepted"),
    session: "requests.Session | None" = None,
    timeout: float = 15.0,
    retries: int = 2,
    sleep: float = 0.5,
) -> dict:
    """Synchronous, browser-free counterpart to
    scraper.py::GoOutScraper._fetch_endone_stats — same relay, same endpoint
    shapes, but for callers with only a saved JWT and no live Playwright
    context (a cookie header isn't required: 2026-08-07 confirmed GoOut's
    panel carries no auth cookie, only the Authorization Bearer token).
    Best-effort per field; one field failing never blocks the others."""
    http = session or requests
    results: dict = {}
    for field in fields:
        path, extra_body = ENDONE_STATS_ENDPOINTS[field]
        target = f"{GO_OUT_BASE}/endOne/{path}"
        body = {"eventId": mongo_id, **extra_body}
        for attempt in range(retries):
            try:
                resp = http.post(
                    relay_url,
                    params={"target": target},
                    headers={
                        "x-relay-secret": relay_secret,
                        "x-relay-auth": auth_header or "",
                        "content-type": "application/json",
                    },
                    data=json.dumps(body),
                    timeout=timeout,
                )
                if resp.ok:
                    results[field] = resp.json()
                    break
                logger.debug("endOne/%s for %s: HTTP %s (attempt %d)", path, mongo_id, resp.status_code, attempt + 1)
            except Exception as exc:  # pragma: no cover - network flakiness
                logger.debug("endOne/%s for %s failed (attempt %d): %s", path, mongo_id, attempt + 1, exc)
            if attempt < retries - 1:
                time.sleep(sleep)
    return results
