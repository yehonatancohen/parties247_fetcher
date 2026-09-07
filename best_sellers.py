"""
Revenue-weighted "best sellers" carousel ranking.

Pure ranking logic for the carousel that `orchestrator.run_best_sellers_update`
rebuilds. The homepage carousels are otherwise hand-curated or keyword-matched
with no revenue signal at all (SEO-ROADMAP "deferred idea #7"); this one is
ordered by what actually sold recently, in *our commission* terms — which
automatically puts account1 parties (₪25/ticket) ahead of account2 ones (6%)
for the same ticket count, matching the business priority.

Title must NOT contain carousel_suggester's temporal keywords ("שבוע", "עכשיו")
or the keyword auto-assign would start pouring unrelated parties into it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

BEST_SELLERS_TITLE = "הכי נמכרים 🔥"
BEST_SELLERS_KEYWORDS = ("הכי נמכרים", "best sellers", "רבי מכר")

DEFAULT_WINDOW_DAYS = 14
DEFAULT_LIMIT = 10


def is_best_sellers_title(title: str | None) -> bool:
    t = (title or "").lower()
    return any(k in t for k in BEST_SELLERS_KEYWORDS)


def _as_utc(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def aggregate_recent_sales(sales_log_rows, *, now: datetime, window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, dict]:
    """
    {go_out_id: {"revenue": ₪ earned by us, "tickets": confirmed delta}} for log rows
    recorded within the window. Negative deltas (scrape regressions) are ignored.
    """
    cutoff = now - timedelta(days=window_days)
    totals: dict[str, dict] = {}
    for row in sales_log_rows:
        recorded = _as_utc(row.get("recorded_at"))
        if recorded is None or recorded < cutoff:
            continue
        go_out_id = str(row.get("go_out_id") or "")
        if not go_out_id:
            continue
        entry = totals.setdefault(go_out_id, {"revenue": 0.0, "tickets": 0})
        entry["revenue"] += max(0.0, float(row.get("revenue_earned") or 0.0))
        entry["tickets"] += max(0, int(row.get("delta_confirmed") or 0))
    return totals


def rank_best_sellers(upcoming_parties, sales_log_rows, *, now: datetime,
                      window_days: int = DEFAULT_WINDOW_DAYS, limit: int = DEFAULT_LIMIT,
                      account1_referral: str | None = None) -> list[str]:
    """
    Ordered party ids for the carousel: highest recent commission first, then most
    tickets, then account1 before account2, then soonest date. Parties with no
    recent sales are excluded — an empty result means "clear the carousel".
    """
    recent = aggregate_recent_sales(sales_log_rows, now=now, window_days=window_days)
    if not recent:
        return []

    ranked: list[tuple] = []
    for party in upcoming_parties:
        party_id = str(party.get("_id") or party.get("id") or "")
        event_id = str(party.get("goOutEventId") or "")
        if not party_id or not event_id:
            continue
        stats = recent.get(event_id)
        if not stats or (stats["revenue"] <= 0 and stats["tickets"] <= 0):
            continue
        is_account1 = bool(account1_referral) and party.get("referralCode") == account1_referral
        date_key = str(party.get("date") or party.get("startsAt") or "9999")
        ranked.append((-stats["revenue"], -stats["tickets"], 0 if is_account1 else 1, date_key, party_id))

    ranked.sort()
    return [entry[-1] for entry in ranked[:limit]]
