from datetime import datetime, timedelta, timezone

import best_sellers as bs


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _row(go_out_id, revenue, tickets, days_ago):
    return {
        "go_out_id": go_out_id,
        "revenue_earned": revenue,
        "delta_confirmed": tickets,
        "recorded_at": NOW - timedelta(days=days_ago),
    }


def test_title_detection_avoids_temporal_keywords():
    assert bs.is_best_sellers_title("הכי נמכרים 🔥")
    assert bs.is_best_sellers_title("Best Sellers")
    assert not bs.is_best_sellers_title("חם עכשיו")
    assert not bs.is_best_sellers_title(None)
    # carousel_suggester treats "שבוע"/"עכשיו" as temporal matchers — the default
    # title must not contain them or keyword auto-assign would fill this carousel.
    assert "שבוע" not in bs.BEST_SELLERS_TITLE and "עכשיו" not in bs.BEST_SELLERS_TITLE


def test_aggregate_recent_sales_windows_and_ignores_regressions():
    rows = [
        _row("1", 25.0, 1, 1),
        _row("1", 50.0, 2, 5),
        _row("1", 999.0, 9, 20),        # outside the 14-day window
        _row("2", -25.0, -1, 2),        # scrape regression, must not subtract
        _row("2", 4.8, 1, 3),
        {"go_out_id": "3", "revenue_earned": 1, "delta_confirmed": 1, "recorded_at": "2026-09-07T10:00:00Z"},
        {"go_out_id": "", "revenue_earned": 1, "delta_confirmed": 1, "recorded_at": NOW},
    ]
    totals = bs.aggregate_recent_sales(rows, now=NOW)
    assert totals["1"] == {"revenue": 75.0, "tickets": 3}
    assert totals["2"] == {"revenue": 4.8, "tickets": 1}
    assert totals["3"] == {"revenue": 1.0, "tickets": 1}  # ISO string recorded_at handled
    assert "" not in totals


def test_rank_orders_by_commission_then_tickets_then_account1():
    parties = [
        {"_id": "a", "goOutEventId": "1", "date": "2026-09-12", "referralCode": "ref2"},
        {"_id": "b", "goOutEventId": "2", "date": "2026-09-10", "referralCode": "ref2"},
        {"_id": "c", "goOutEventId": "3", "date": "2026-09-11", "referralCode": "ref1"},
        {"_id": "d", "goOutEventId": "4", "date": "2026-09-09", "referralCode": "ref2"},  # no sales
        {"_id": "e", "date": "2026-09-09"},                                              # no goOutEventId
    ]
    rows = [
        _row("1", 30.0, 5, 1),
        _row("2", 30.0, 6, 1),   # same revenue, more tickets -> ahead of "a"
        _row("3", 30.0, 6, 1),   # same as "b" but account1 -> ahead of "b"
    ]
    assert bs.rank_best_sellers(parties, rows, now=NOW, account1_referral="ref1") == ["c", "b", "a"]


def test_rank_limit_and_empty():
    parties = [{"_id": str(i), "goOutEventId": str(i), "date": "2026-09-10"} for i in range(5)]
    rows = [_row(str(i), float(i + 1), 1, 1) for i in range(5)]
    assert bs.rank_best_sellers(parties, rows, now=NOW, limit=2) == ["4", "3"]
    assert bs.rank_best_sellers(parties, [], now=NOW) == []
    assert bs.rank_best_sellers([], rows, now=NOW) == []
