import dedupe_parties as d


def party(**overrides):
    base = {
        "_id": "1",
        "name": "Party A",
        "date": "2026-10-10T23:00:00",
        "location": "Gagarin Club, Tel Aviv",
        "ticketPrice": 100,
        "slug": "party-a",
        "goOutUrl": "https://go-out.co/event/1",
    }
    base.update(overrides)
    return base


def test_same_venue_and_near_time_matches_even_with_different_names():
    a = party(name="Organizer A presents: X", goOutUrl="https://go-out.co/event/1")
    b = party(
        _id="2",
        name="Completely Different Branding",
        date="2026-10-10T23:45:00",
        location="Gagarin, Tel-Aviv",
        goOutUrl="https://go-out.co/event/2",
    )
    assert d.duplicate_reason(a, b) == "same_venue_time"


def test_same_venue_but_far_apart_does_not_match():
    a = party(date="2026-10-10T18:00:00")
    b = party(
        _id="2",
        name="Other Event",
        date="2026-10-10T23:30:00",
        goOutUrl="https://go-out.co/event/2",
    )
    assert d.duplicate_reason(a, b) is None


def test_fuzzy_same_brand_day_requires_more_evidence():
    a = party(
        name="THURSDAY MOON | MAINSTREAM | 06.10",
        location="Gagarin Club, Tel Aviv",
        goOutUrl="https://go-out.co/event/1",
    )
    b = party(
        _id="2",
        name="THURSDAY MOON | MAINSTREAM | 6.10",
        date="2026-10-10T02:30:00",
        location="Haoman 17, Jerusalem",
        imageUrl="https://example.com/other.jpg",
        goOutUrl="https://go-out.co/event/2",
    )
    assert d.duplicate_reason(a, b) is None


def test_keeper_uses_expected_commission_not_ticket_price_alone(monkeypatch):
    monkeypatch.setattr(d, "ACCOUNT1_REFERRAL", "flat-ref")
    flat = party(
        name="Flat account",
        slug="flat",
        referralCode="flat-ref",
        ticketPrice=100,
    )
    percent = party(
        _id="2",
        name="Percent account",
        slug="percent",
        referralCode="other-ref",
        ticketPrice=300,
        goOutUrl="https://go-out.co/event/2",
    )
    keeper = d.best_party([flat, percent], {}, {})
    assert keeper is flat
    assert d.expected_commission_per_ticket(flat, {}) == 25
    assert d.expected_commission_per_ticket(percent, {}) == 18


def test_high_price_percentage_listing_can_beat_flat_fee(monkeypatch):
    monkeypatch.setattr(d, "ACCOUNT1_REFERRAL", "flat-ref")
    flat = party(slug="flat", referralCode="flat-ref", ticketPrice=100)
    percent = party(
        _id="2",
        slug="percent",
        referralCode="other-ref",
        ticketPrice=500,
        goOutUrl="https://go-out.co/event/2",
    )
    keeper = d.best_party([flat, percent], {}, {})
    assert keeper is percent
    assert d.expected_commission_per_ticket(percent, {}) == 30


def test_obvious_test_party_is_auto_issue():
    p = party(name="TEST EVENT 123")
    issues = d.audit_party(p)
    assert any(i["type"] == "test_party" and i["severity"] == "auto" for i in issues)


def test_missing_location_and_invalid_time_are_review_only():
    p = party(date="not-a-date", location="Unknown Location")
    issues = d.audit_party(p)
    kinds = {(i["type"], i["severity"]) for i in issues}
    assert ("invalid_time", "review") in kinds
    assert ("missing_location", "review") in kinds
