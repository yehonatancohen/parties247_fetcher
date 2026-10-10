"""Listing Guard courier: what gets fetched and what gets posted. The rules
themselves are tested in the backend (tests/test_listings_*.py)."""

import json
from types import SimpleNamespace

import listing_sync as ls

EVENT = {
    "Title": "FRIDAY MAINSTREAM | 16.10",
    "StartingDate": "2026-10-16T23:00:00.000",
    "Url": "1790000000000",
    "EventSerial": 48000,
    "Description": "x",
    # Placeholder GoOut puts on every public page — must never be forwarded.
    "Tickets": [{"Title": "", "Price": 200, "Commision": 5, "Amount": "150"}],
    "purchaseConfig": {"blocklist": False},
}
TIERS = [{"Title": "כניסה", "Price": 80, "Commision": 9.25, "display": "ACTIVE"}]


def page(event=EVENT):
    data = json.dumps({"props": {"pageProps": {"event": event}}})
    return (
        '<html><head><meta property="og:image" content="https://images.go-out.co/events/a_whatsappImage.jpg"/></head>'
        f'<script id="__NEXT_DATA__" type="application/json">{data}</script></html>'
    )


class FakeHttp:
    """Routes by URL; records every call."""

    def __init__(self, targets=(), page_html=None, page_status=200, tiers=TIERS, full_requested=False,
                 site_html="", fail_sync_chunks=()):
        self.targets, self.page_html, self.page_status = list(targets), page_html, page_status
        self.tiers, self.full_requested, self.site_html = tiers, full_requested, site_html
        self.fail_sync_chunks = set(fail_sync_chunks)
        self.gets, self.posts = [], []

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append(url)
        if url.endswith("/api/internal/listings/targets"):
            assert headers["X-Service-Token"] == ls.config.SERVICE_TOKEN
            body = {"targets": self.targets, "fullRequested": self.full_requested}
            return SimpleNamespace(status_code=200, json=lambda: body, raise_for_status=lambda: None)
        if url == ls.GO_OUT_TIERS_URL:
            if self.tiers is None:
                raise TimeoutError("no answer")
            body = {"status": True, "tickets": self.tiers}
            return SimpleNamespace(status_code=200, json=lambda: body)
        if url.startswith(ls.SITE_BASE_URL):
            return SimpleNamespace(status_code=200, text=self.site_html, url=url)
        return SimpleNamespace(status_code=self.page_status, text=self.page_html or "")

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json))
        index = sum(1 for u, _ in self.posts if u.endswith("/sync")) - 1
        if url.endswith("/sync") and index in self.fail_sync_chunks:
            raise ConnectionError("render asleep")
        body = {"synced": len((json or {}).get("items", [])), "changed": 1, "results": [], "waiting": 2,
                "checked": 5, "merges": [], "new": 1}
        return SimpleNamespace(status_code=200, json=lambda: body, raise_for_status=lambda: None)


def target(pid="p1", has_source=True, status="live"):
    return {"partyId": pid, "slug": f"slug-{pid}", "url": f"https://go-out.co/event/{pid}",
            "urlId": f"url-{pid}", "listingStatus": status, "hasSource": has_source}


def test_tiers_run_fetches_only_tiers_for_known_parties():
    http = FakeHttp(page_html=page())
    item = ls.build_sync_item(target(), full=False, http=http)
    assert item == {"partyId": "p1", "tiers": TIERS}
    assert http.gets == [ls.GO_OUT_TIERS_URL]


def test_full_run_sends_trimmed_event_and_never_the_placeholder_tickets():
    item = ls.build_sync_item(target(), full=True, http=FakeHttp(page_html=page()))
    assert item["event"]["Title"] == "FRIDAY MAINSTREAM | 16.10"
    assert "Tickets" not in item["event"] and "purchaseConfig" not in item["event"]
    assert item["ogImage"].endswith("a_whatsappImage.jpg")
    assert item["tiers"] == TIERS


def test_party_never_synced_gets_a_full_fetch_even_on_a_tiers_run():
    item = ls.build_sync_item(target(has_source=False), full=False, http=FakeHttp(page_html=page()))
    assert "event" in item


def test_removed_event_reports_page_status_but_a_network_error_does_not():
    gone = ls.build_sync_item(target(), full=True, http=FakeHttp(page_html="<html>no data</html>"))
    assert gone["pageStatus"] == 404 and "event" not in gone
    missing = ls.build_sync_item(target(), full=True, http=FakeHttp(page_status=404))
    assert missing["pageStatus"] == 404

    class Down(FakeHttp):
        def get(self, url, **kwargs):
            if "go-out.co/event/" in url:
                raise ConnectionError("blip")
            return super().get(url, **kwargs)

    blip = ls.build_sync_item(target(), full=True, http=Down())
    assert "pageStatus" not in blip and "event" not in blip


def test_unreadable_tiers_are_sent_as_none_not_dropped():
    item = ls.build_sync_item(target(), full=False, http=FakeHttp(tiers=None))
    assert item == {"partyId": "p1", "tiers": None}


def test_sync_posts_in_chunks_and_marks_a_complete_full_run():
    http = FakeHttp(targets=[target(f"p{i}") for i in range(60)], page_html=page())
    summary = ls.run_listing_sync(full=True, http=http)
    posts = [body for url, body in http.posts if url.endswith("/sync")]
    assert [len(body["items"]) for body in posts] == [25, 25, 10]
    assert [bool(body.get("fullDone")) for body in posts] == [False, False, True]
    assert summary["synced"] == 60 and summary["failed_chunks"] == 0


def test_full_run_is_not_marked_done_when_a_chunk_failed():
    http = FakeHttp(targets=[target(f"p{i}") for i in range(30)], page_html=page(), fail_sync_chunks={0})
    summary = ls.run_listing_sync(full=True, http=http)
    assert summary["failed_chunks"] == 1
    assert not any(body.get("fullDone") for url, body in http.posts)


def test_admin_requested_full_sync_upgrades_a_tiers_run():
    http = FakeHttp(targets=[target()], page_html=page(), full_requested=True)
    ls.run_listing_sync(full=False, http=http)
    assert "event" in http.posts[0][1]["items"][0] and http.posts[0][1]["fullDone"] is True


def test_site_page_json_ld_is_read():
    html = ('<script type="application/ld+json">' + json.dumps({
        "@type": "MusicEvent", "name": "Big Party", "startDate": "2026-10-16T23:00:00+03:00",
        "offers": {"@type": "Offer", "price": "87.4", "availability": "https://schema.org/InStock"},
    }) + "</script>")
    assert ls.parse_site_page(html) == {"name": "Big Party", "startDate": "2026-10-16T23:00:00+03:00",
                                        "price": "87.4", "availability": "https://schema.org/InStock"}
    assert ls.parse_site_page("<html></html>") == {}


def test_audit_checks_only_listed_parties_and_sends_one_digest_line(monkeypatch):
    monkeypatch.setattr(ls.time, "sleep", lambda seconds: None)
    sent = []
    telegram = SimpleNamespace(send_message_sync=lambda text, parse_mode="Markdown": sent.append(text))
    http = FakeHttp(targets=[target("p1"), target("p2", status="hidden")], page_html=page())
    accounts = [SimpleNamespace(account_id="account1", referral="a1ref"),
                SimpleNamespace(account_id="account2", referral="a2ref")]

    result = ls.run_listing_audit(accounts, telegram, http=http)

    audit = next(body for url, body in http.posts if url.endswith("/audit"))
    assert audit["account1Referral"] == "a1ref" and audit["dryRun"] is False
    assert [check["partyId"] for check in audit["siteChecks"]] == ["p1"]
    assert result["waiting"] == 2
    assert sent == ["🧾 2 listing issues waiting for you → https://admin.parties247.co.il/issues"]


def test_dry_run_audit_does_not_sync_check_the_site_or_message():
    sent = []
    telegram = SimpleNamespace(send_message_sync=lambda text, parse_mode="Markdown": sent.append(text))
    http = FakeHttp(targets=[target("p1")], page_html=page())
    ls.run_listing_audit([], telegram, dry_run=True, http=http)
    assert [url.rsplit("/", 1)[-1] for url, _ in http.posts] == ["audit"]
    assert http.posts[0][1]["dryRun"] is True and http.posts[0][1]["siteChecks"] == []
    assert sent == []
