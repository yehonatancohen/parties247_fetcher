from datetime import datetime, timezone
from types import SimpleNamespace

import fast_sales_alerts

NOW = datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc)


def _matches(doc, query):
    return all(doc.get(k) == v for k, v in query.items())


class FakeCollection:
    def __init__(self, docs=None, fail_insert=False):
        self.docs = [dict(d) for d in (docs or [])]
        self.fail_insert = fail_insert

    def find(self, query=None, projection=None):
        return [dict(d) for d in self.docs if _matches(d, query or {})]

    def find_one(self, query):
        found = self.find(query)
        return found[0] if found else None

    def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if _matches(doc, query):
                doc.update(update.get("$set", {}))
                return SimpleNamespace(modified_count=1)
        if upsert:
            self.docs.append({**query, **update.get("$setOnInsert", {}), **update.get("$set", {})})
        return SimpleNamespace(modified_count=0)

    def insert_one(self, doc):
        if self.fail_insert:
            raise RuntimeError("insert failed")
        self.docs.append(dict(doc))


def _db(sales, log=None, state=None, sessions=None):
    return SimpleNamespace(
        goout_sales=FakeCollection(sales),
        goout_sales_log=log or FakeCollection(),
        goout_fast_sales_state=FakeCollection(state),
        goout_sessions=FakeCollection(sessions),
    )


def _snapshot(**over):
    doc = {"_id": "s1", "account_id": "account1", "go_out_id": "111", "event_name": "Moon",
           "confirmed_count": 4, "event_revenue": 400.0, "ticket_price": None, "mongo_id": "m111"}
    doc.update(over)
    return doc


def test_record_fast_sale_logs_new_tickets_and_advances_snapshot():
    db = _db([_snapshot()])
    row = fast_sales_alerts.record_fast_sale(db, _snapshot(), "account1", 6, 600.0, NOW)
    assert row["delta_confirmed"] == 2
    assert row["revenue_earned"] == 30.0  # account1: ₪15 per ticket
    assert row["recorded_at"] == NOW and row["year_month"] == "2026-10"
    assert db.goout_sales_log.docs == [row]
    assert db.goout_sales.docs[0]["confirmed_count"] == 6
    assert db.goout_sales.docs[0]["event_revenue"] == 600.0


def test_record_fast_sale_account2_uses_revenue_delta():
    snap = _snapshot(account_id="account2")
    db = _db([snap])
    row = fast_sales_alerts.record_fast_sale(db, snap, "account2", 5, 500.0, NOW)
    assert row["delta_event_revenue"] == 100.0
    assert row["revenue_earned"] == 6.0


def test_record_fast_sale_leaves_first_revenue_lump_to_the_tracker():
    snap = _snapshot(account_id="account2", event_revenue=None)
    db = _db([snap])
    row = fast_sales_alerts.record_fast_sale(db, snap, "account2", 5, 500.0, NOW)
    assert row["delta_event_revenue"] is None
    assert db.goout_sales.docs[0]["event_revenue"] is None


def test_record_fast_sale_nothing_new():
    db = _db([_snapshot()])
    assert fast_sales_alerts.record_fast_sale(db, _snapshot(), "account1", 4, 400.0, NOW) is None
    assert fast_sales_alerts.record_fast_sale(db, _snapshot(), "account1", 3, 400.0, NOW) is None
    assert fast_sales_alerts.record_fast_sale(db, None, "account1", 9, 400.0, NOW) is None
    assert db.goout_sales_log.docs == []


def test_record_fast_sale_skips_when_tracker_moved_snapshot_first():
    stale = _snapshot()
    db = _db([_snapshot(confirmed_count=6)])  # the 4h tracker already logged up to 6
    assert fast_sales_alerts.record_fast_sale(db, stale, "account1", 6, 600.0, NOW) is None
    assert db.goout_sales_log.docs == []
    assert db.goout_sales.docs[0]["confirmed_count"] == 6


def test_record_fast_sale_restores_snapshot_if_log_write_fails():
    db = _db([_snapshot()], log=FakeCollection(fail_insert=True))
    try:
        fast_sales_alerts.record_fast_sale(db, _snapshot(), "account1", 6, 600.0, NOW)
    except RuntimeError:
        pass
    assert db.goout_sales.docs[0]["confirmed_count"] == 4
    assert db.goout_sales.docs[0]["event_revenue"] == 400.0


def test_run_records_sale_even_when_telegram_fails(monkeypatch):
    monkeypatch.setattr(fast_sales_alerts.config, "CF_RELAY_URL", "https://relay.test")
    monkeypatch.setattr(fast_sales_alerts.config, "CF_RELAY_SECRET", "s")
    monkeypatch.setattr(fast_sales_alerts, "extract_token_from_storage_state", lambda _s: "tok")
    monkeypatch.setattr(
        fast_sales_alerts, "fetch_endone_stats_sync",
        lambda *a, **k: {"ticket_stats": {"Accepted": 7}, "revenue": {"revenue": {"own_revenue": 700}}},
    )

    class Http:
        def get(self, *a, **k):
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: [{"goOutEventId": "111", "name": "Moon"}])

    telegram = SimpleNamespace(send_message_sync=lambda *a, **k: False)
    db = _db([_snapshot()], sessions=[{"account_id": "account1", "storage_state": {}}])
    result = fast_sales_alerts.run_fast_sales_alerts(db, telegram, http=Http(), now=NOW)

    assert result["sales"] == 3 and result["recorded"] == 1
    assert [r["delta_confirmed"] for r in db.goout_sales_log.docs] == [3]
    assert db.goout_sales.docs[0]["confirmed_count"] == 7
    # Telegram failed, so the alert is still owed next tick.
    assert db.goout_fast_sales_state.docs[0]["last_alerted_count"] == 4

    # Next tick: same count, Telegram works -> alert goes out, nothing logged twice.
    telegram.send_message_sync = lambda *a, **k: True
    result = fast_sales_alerts.run_fast_sales_alerts(db, telegram, http=Http(), now=NOW)
    assert result["sales"] == 3 and result["recorded"] == 0
    assert len(db.goout_sales_log.docs) == 1
