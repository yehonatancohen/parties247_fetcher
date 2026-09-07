from datetime import datetime, timedelta, timezone

import alerts


# --------------------------------------------------------------------------- CAPTCHA

def test_detect_captcha_recognises_rendered_challenges():
    assert alerts.detect_captcha('<div id="cf-challenge-running"></div>') == "cloudflare-challenge"
    assert alerts.detect_captcha("<title>Just a moment...</title>") == "cloudflare-interstitial"
    assert alerts.detect_captcha('<div class="cf-turnstile" data-sitekey="x"></div>') == "turnstile"
    assert alerts.detect_captcha('<iframe src="https://newassets.hcaptcha.com/captcha/v1/"></iframe>') == "hcaptcha-widget"
    assert alerts.detect_captcha('<iframe title="reCAPTCHA" src="https://www.google.com/recaptcha/api2/anchor"></iframe>') == "recaptcha-widget"
    assert alerts.detect_captcha("<p>אני לא רובוט</p>") == "generic-captcha-prompt"


def test_detect_captcha_ignores_invisible_recaptcha_script_and_normal_login_page():
    login_page = """
    <html><head>
      <script src="https://www.google.com/recaptcha/api.js?render=explicit" async></script>
    </head><body>
      <form><input id="register_email"><input id="register_password"><button id="register_button">התחברות</button></form>
    </body></html>
    """
    assert alerts.detect_captcha(login_page) is None
    assert alerts.detect_captcha("") is None
    assert alerts.detect_captcha(None) is None


def test_captcha_message_mentions_account_marker_and_stage():
    msg = alerts.captcha_message("account2", "turnstile", "post-submit")
    assert "account2" in msg and "turnstile" in msg and "post-submit" in msg
    assert "2FA" in msg


# --------------------------------------------------------------------------- discovery

def test_discovery_alert_needs_a_baseline():
    assert alerts.discovery_alert("account1", 0, None) is None
    assert alerts.discovery_alert("account1", 0, 0) is None


def test_discovery_alert_on_zero_with_baseline():
    msg = alerts.discovery_alert("account1", 0, 12)
    assert msg and "0 events" in msg and "12" in msg and "account1" in msg


def test_discovery_alert_on_large_drop_only_from_meaningful_baseline():
    assert alerts.discovery_alert("account2", 2, 40) is not None      # -95%
    assert alerts.discovery_alert("account2", 30, 40) is None         # -25%
    assert alerts.discovery_alert("account1", 1, 5) is None           # tiny baseline, not a drop alert
    msg = alerts.discovery_alert("account2", 4, 40)
    assert "−90%" in msg


class FakeSessions:
    def __init__(self, docs=None):
        self.docs = {d["account_id"]: d for d in (docs or [])}
        self.updates = []

    def find_one(self, filter, projection=None):
        return self.docs.get(filter.get("account_id"))

    def update_one(self, filter, update, upsert=False):
        self.updates.append((filter, update, upsert))
        doc = self.docs.setdefault(filter["account_id"], {"account_id": filter["account_id"]})
        doc.update(update.get("$set", {}))


class FakeDb:
    def __init__(self, sessions):
        self.goout_sessions = sessions


def test_discovery_count_round_trip():
    db = FakeDb(FakeSessions())
    assert alerts.previous_discovery_count(db, "account1") is None
    alerts.record_discovery_count(db, "account1", 17)
    assert alerts.previous_discovery_count(db, "account1") == 17
    assert db.goout_sessions.updates[0][2] is True  # upsert, so a fresh account works too
    assert alerts.previous_discovery_count(None, "account1") is None


def test_captcha_alert_throttled_per_account():
    db = FakeDb(FakeSessions())
    now = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
    assert alerts.should_send_captcha_alert(db, "account1", now=now) is True
    assert alerts.should_send_captcha_alert(db, "account1", now=now + timedelta(hours=4)) is False
    assert alerts.should_send_captcha_alert(db, "account2", now=now + timedelta(hours=4)) is True
    assert alerts.should_send_captcha_alert(db, "account1", now=now + timedelta(hours=13)) is True
    assert alerts.should_send_captcha_alert(None, "account1") is True
