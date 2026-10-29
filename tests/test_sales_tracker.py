import asyncio
from types import SimpleNamespace

import sales_tracker
from test_fast_sales_alerts import FakeCollection


class FakeScraper:
    runs = []

    def __init__(self, account, db=None, telegram_mgr=None):
        pass

    async def ensure_session(self):
        return True

    async def scrape_sales_data(self):
        return FakeScraper.runs.pop(0)

    async def close(self):
        pass


def _item(own_revenue=None, confirmed=10):
    item = {"go_out_id": "222", "event_name": "Sunset", "confirmed": confirmed,
            "pending": 0, "ticket_price": 100.0, "mongo_id": "m222"}
    if own_revenue is not None:
        item["endone_stats"] = {"views": {"Views": 50},
                                "revenue": {"revenue": {"own_revenue": own_revenue,
                                                        "total_revenue": own_revenue}}}
    return item


def _run(db, *runs, monkeypatch):
    monkeypatch.setattr(sales_tracker, "GoOutScraper", FakeScraper)
    FakeScraper.runs = list(runs)
    account = SimpleNamespace(account_id="account2")
    for _ in runs:
        asyncio.run(sales_tracker._update_account(account, db))


def _db():
    return SimpleNamespace(goout_sales=FakeCollection(), goout_sales_log=FakeCollection(),
                           goout_fast_sales_state=FakeCollection())


def test_run_without_stats_keeps_revenue_baseline(monkeypatch):
    # Good read, then a dead-token run with no stats, then a good read again
    # with the same cumulative revenue: only the first read is booked.
    db = _db()
    _run(db, [_item(555.0)], [_item(None)], [_item(555.0)], monkeypatch=monkeypatch)

    assert [r["delta_event_revenue"] for r in db.goout_sales_log.docs] == [555.0]
    snap = db.goout_sales.docs[0]
    assert snap["event_revenue"] == 555.0
    assert snap["views"] == 50
    assert snap["real_own_revenue"] == 555.0


def test_new_revenue_after_missing_stats_books_only_the_difference(monkeypatch):
    db = _db()
    _run(db, [_item(555.0)], [_item(None)], [_item(655.0, confirmed=11)],
         monkeypatch=monkeypatch)

    assert [r["delta_event_revenue"] for r in db.goout_sales_log.docs] == [555.0, 100.0]
    assert round(db.goout_sales_log.docs[1]["revenue_earned"], 2) == 6.0
