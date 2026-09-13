from datetime import datetime, timedelta, timezone

import wa_sales_watch
from endone_relay import extract_token_from_storage_state


# ---------------------------------------------------------------------------
# extract_token_from_storage_state (endone_relay, exercised via the watch module's import)
# ---------------------------------------------------------------------------

def _storage_state(token="abc123"):
    return {
        "cookies": [{"name": "AWSALB", "value": "x", "domain": "go-out.co"}],
        "origins": [{
            "origin": "https://www.go-out.co",
            "localStorage": [{"name": "user", "value": f'{{"token":"{token}"}}'}],
        }],
    }


def test_extract_token_from_storage_state_finds_user_token():
    assert extract_token_from_storage_state(_storage_state("xyz")) == "xyz"


def test_extract_token_from_storage_state_handles_missing_or_malformed():
    assert extract_token_from_storage_state(None) is None
    assert extract_token_from_storage_state({}) is None
    assert extract_token_from_storage_state({"origins": [{"localStorage": [{"name": "user", "value": "not json"}]}]}) is None
    assert extract_token_from_storage_state({"origins": [{"localStorage": [{"name": "other", "value": "{}"}]}]}) is None


# ---------------------------------------------------------------------------
# select_watchlist_events
# ---------------------------------------------------------------------------

def test_select_watchlist_events_joins_and_drops_unknown_mongo_id():
    watchlist = [
        {"goOutEventId": "111", "reasons": ["queued"]},
        {"goOutEventId": "222", "reasons": ["recentlySent"]},  # no goout_sales doc at all
        {"goOutEventId": "333", "reasons": ["queued"]},  # goout_sales doc exists but no mongo_id yet
    ]
    sales_by_event = {
        "111": {"account_id": "account1", "mongo_id": "m111"},
        "333": {"account_id": "account2", "mongo_id": None},
    }
    events = wa_sales_watch.select_watchlist_events(watchlist, sales_by_event)
    assert len(events) == 1
    assert events[0] == {"go_out_id": "111", "account_id": "account1", "mongo_id": "m111", "reasons": ["queued"]}


def test_select_watchlist_events_empty_watchlist():
    assert wa_sales_watch.select_watchlist_events([], {"111": {"account_id": "a", "mongo_id": "m"}}) == []


# ---------------------------------------------------------------------------
# back-off / alert throttling
# ---------------------------------------------------------------------------

def test_should_back_off_within_and_after_cooldown():
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    assert wa_sales_watch.should_back_off(None, now) is False
    assert wa_sales_watch.should_back_off(now - timedelta(hours=1), now) is True
    assert wa_sales_watch.should_back_off(now - timedelta(hours=3), now) is False


def test_should_back_off_treats_naive_datetime_as_utc():
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    naive_recent = datetime(2026, 9, 10, 11, 0)  # 1h ago, no tzinfo
    assert wa_sales_watch.should_back_off(naive_recent, now) is True


def test_should_send_watch_alert_throttles_like_captcha_alert():
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    assert wa_sales_watch.should_send_watch_alert(None, now) is True
    assert wa_sales_watch.should_send_watch_alert(now - timedelta(hours=1), now) is False
    assert wa_sales_watch.should_send_watch_alert(now - timedelta(hours=7), now) is True


# ---------------------------------------------------------------------------
# build_snapshot_doc
# ---------------------------------------------------------------------------

def test_build_snapshot_doc_extracts_accepted_pending_revenue():
    at = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
    stats = {
        "ticket_stats": {"Accepted": 12, "Pending": 3},
        "revenue": {"revenue": {"own_revenue": 910, "total_revenue": 1200}},
        "last_accepted": {"status": True, "users": [{"name": "x"}]},
    }
    doc = wa_sales_watch.build_snapshot_doc("account2", "111", at, stats)
    assert doc == {
        "account_id": "account2",
        "go_out_id": "111",
        "at": at,
        "accepted": 12,
        "pending": 3,
        "ownRevenue": 910,
        "lastAcceptedRaw": {"status": True, "users": [{"name": "x"}]},
    }


def test_build_snapshot_doc_handles_empty_stats():
    at = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
    doc = wa_sales_watch.build_snapshot_doc("account1", "222", at, {})
    assert doc["accepted"] is None
    assert doc["pending"] is None
    assert doc["ownRevenue"] is None
    assert doc["lastAcceptedRaw"] is None


# ---------------------------------------------------------------------------
# run_wa_sales_watch orchestration, with fakes (same style as tests/test_alerts.py)
# ---------------------------------------------------------------------------

class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.inserted = []
        self.updates = []

    def find(self, query, projection=None):
        return list(self.docs)

    def find_one(self, query):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None

    def insert_one(self, doc):
        self.inserted.append(doc)

    def update_one(self, filt, update, upsert=False):
        self.updates.append((filt, update, upsert))
        for d in self.docs:
            if all(d.get(k) == v for k, v in filt.items()):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = dict(filt)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)


class FakeDb:
    def __init__(self, sales=None, sessions=None, snapshots=None):
        self.goout_sales = FakeCollection(sales)
        self.goout_sessions = FakeCollection(sessions)
        self.goout_sales_snapshots = FakeCollection(snapshots)


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, watchlist=None, endone_stats=None):
        self._watchlist = watchlist or []
        self._endone_stats = endone_stats or {}
        self.posts = []

    def get(self, url, headers=None, timeout=None):
        if "watchlist" in url:
            return FakeResponse({"watchlist": self._watchlist})
        raise AssertionError(f"Unexpected GET {url}")

    def post(self, url, params=None, headers=None, data=None, json=None, timeout=None):
        self.posts.append((url, params, headers, data, json))
        if "rebuild-facts" in url:
            return FakeResponse({"message": "Rebuilt."})
        # endOne relay call: params["target"] identifies which field it's for
        target = (params or {}).get("target", "")
        for field, payload in self._endone_stats.items():
            if field in target or (field == "ticket_stats" and "getUserTicketStatistics" in target) \
               or (field == "revenue" and "getRevenueData" in target) \
               or (field == "last_accepted" and "getXLastAcceptedUsers" in target):
                return FakeResponse(payload)
        return FakeResponse({}, status_code=404)


def _patch_config(monkeypatch, **overrides):
    defaults = dict(
        CF_RELAY_URL="https://relay.example.com", CF_RELAY_SECRET="secret",
        BACKEND_URL="https://backend.example.com", SERVICE_TOKEN="svc-token",
    )
    defaults.update(overrides)
    for key, value in defaults.items():
        monkeypatch.setattr(wa_sales_watch.config, key, value, raising=False)


def test_run_wa_sales_watch_snapshots_watched_event(monkeypatch):
    _patch_config(monkeypatch)
    db = FakeDb(
        sales=[{"go_out_id": "111", "account_id": "account1", "mongo_id": "m111"}],
        sessions=[{"account_id": "account1", "storage_state": _storage_state("tok1")}],
    )
    http = FakeHttp(
        watchlist=[{"goOutEventId": "111", "reasons": ["recentlySent"]}],
        endone_stats={
            "ticket_stats": {"Accepted": 5, "Pending": 1},
            "revenue": {"revenue": {"own_revenue": 400}},
            "last_accepted": {"status": True, "users": []},
        },
    )
    now = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
    summary = wa_sales_watch.run_wa_sales_watch(db, http=http, now=now)

    assert summary == {"watched": 1, "snapshotted": 1, "skipped_backoff": 0, "failed": []}
    assert len(db.goout_sales_snapshots.inserted) == 1
    snap = db.goout_sales_snapshots.inserted[0]
    assert snap["go_out_id"] == "111" and snap["accepted"] == 5
    # rebuild-facts was triggered afterward
    assert any("rebuild-facts" in p[0] for p in http.posts)


def test_run_wa_sales_watch_skips_without_relay_config(monkeypatch):
    _patch_config(monkeypatch, CF_RELAY_URL="", CF_RELAY_SECRET="")
    db = FakeDb()
    http = FakeHttp(watchlist=[{"goOutEventId": "111", "reasons": ["queued"]}])
    summary = wa_sales_watch.run_wa_sales_watch(db, http=http)
    assert summary["watched"] == 0
    assert http.posts == []  # never even tried


def test_run_wa_sales_watch_backs_off_account_with_missing_token(monkeypatch):
    _patch_config(monkeypatch)
    db = FakeDb(
        sales=[{"go_out_id": "111", "account_id": "account1", "mongo_id": "m111"}],
        sessions=[{"account_id": "account1", "storage_state": None}],  # never logged in
    )
    http = FakeHttp(watchlist=[{"goOutEventId": "111", "reasons": ["queued"]}])
    now = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
    summary = wa_sales_watch.run_wa_sales_watch(db, http=http, now=now)

    assert summary["snapshotted"] == 0
    assert summary["failed"] == ["111"]
    session_doc = db.goout_sessions.find_one({"account_id": "account1"})
    assert session_doc["wa_watch_last_failure_at"] == now


def test_run_wa_sales_watch_respects_existing_backoff(monkeypatch):
    _patch_config(monkeypatch)
    now = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
    db = FakeDb(
        sales=[{"go_out_id": "111", "account_id": "account1", "mongo_id": "m111"}],
        sessions=[{
            "account_id": "account1",
            "storage_state": _storage_state("tok1"),
            "wa_watch_last_failure_at": now - timedelta(minutes=30),
        }],
    )
    http = FakeHttp(watchlist=[{"goOutEventId": "111", "reasons": ["queued"]}])
    summary = wa_sales_watch.run_wa_sales_watch(db, http=http, now=now)

    assert summary["skipped_backoff"] == 1
    assert summary["snapshotted"] == 0
    assert http.posts == [p for p in http.posts if "rebuild-facts" in p[0]]  # no endOne calls made


def test_run_wa_sales_watch_no_events_does_not_call_rebuild(monkeypatch):
    _patch_config(monkeypatch)
    db = FakeDb()
    http = FakeHttp(watchlist=[])
    summary = wa_sales_watch.run_wa_sales_watch(db, http=http)
    assert summary["watched"] == 0
    assert http.posts == []
