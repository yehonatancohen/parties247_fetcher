"""
Operational alerts for the scraper — pure helpers plus tiny Mongo state readers.

Two silent failure modes documented in CLAUDE.md "Known sharp edges" finally get a
voice here:

1. **Discovery collapse.** GoOut's panel has no stable selectors; a copy change can
   make `discover_events()` return 0 events with no exception. We remember the last
   non-trivial count per account (in the account's `goout_sessions` doc) and alert
   when a previously healthy account suddenly finds nothing (or almost nothing).

2. **CAPTCHA / bot challenge.** A challenge page used to read as "login failed" and
   re-trigger the Telegram 2FA relay every day. `detect_captcha()` recognises the
   common interstitials so the login flow can alert and bail out instead.

This module deliberately imports neither `config` nor Playwright so it can be
unit-tested without any environment.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# Markers that indicate a *rendered* challenge, not merely a captcha script include
# (GoOut's normal login page may load reCAPTCHA JS invisibly — matching on the bare
# word "recaptcha" would false-positive on every login).
CAPTCHA_MARKERS: tuple[tuple[str, str], ...] = (
    ("cloudflare-challenge", r"cf-challenge|challenge-platform|cf_chl_|/cdn-cgi/challenge-platform"),
    ("cloudflare-interstitial", r"just a moment\.{0,3}|checking your browser|verify you are human|אמת שאתה אנושי|אנא המתן"),
    ("turnstile", r"cf-turnstile|challenges\.cloudflare\.com/turnstile"),
    ("hcaptcha-widget", r"<iframe[^>]+hcaptcha\.com|class=[\"']h-captcha"),
    ("recaptcha-widget", r"<iframe[^>]+recaptcha|class=[\"']g-recaptcha(?![^\"']*hidden)|rc-anchor-container"),
    ("generic-captcha-prompt", r"enter the characters|i'm not a robot|אני לא רובוט|הזן את התווים"),
)

_COMPILED = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in CAPTCHA_MARKERS]


def detect_captcha(html: str | None) -> str | None:
    """Return the name of the first challenge marker found in the page HTML, else None."""
    if not html:
        return None
    for name, regex in _COMPILED:
        if regex.search(html):
            return name
    return None


# ---------------------------------------------------------------------------
# Discovery-count anomaly
# ---------------------------------------------------------------------------

def discovery_alert(account_id: str, found: int, previous: int | None, *,
                    drop_ratio: float = 0.8, min_previous_for_drop: int = 10) -> str | None:
    """
    Decide whether this run's discovery count is anomalous compared to the last one.

    - previous None/0 -> no baseline, never alert (first run, or account was already dark).
    - found == 0 with any positive baseline -> alert (the classic silent-zero failure).
    - found dropped by >= drop_ratio from a baseline of at least min_previous_for_drop -> alert.
    """
    if not previous or previous <= 0:
        return None
    if found == 0:
        return (
            f"🚨 *{account_id}*: discovery found *0 events* (last run: {previous}). "
            "GoOut panel copy/layout may have changed — check the scraper logs for "
            "'Still waiting' lines before the next 06:00 run."
        )
    if previous >= min_previous_for_drop and found <= previous * (1 - drop_ratio):
        return (
            f"⚠️ *{account_id}*: discovery dropped to *{found}* events "
            f"(last run: {previous}, −{round((1 - found / previous) * 100)}%). "
            "Possible partial scrape — worth a manual `/scrape` to confirm."
        )
    return None


def previous_discovery_count(db, account_id: str) -> int | None:
    """Last recorded discovery count for the account, or None if never recorded."""
    if db is None:
        return None
    try:
        doc = db.goout_sessions.find_one({"account_id": account_id}, {"last_discovery_count": 1})
    except Exception:
        return None
    if not doc:
        return None
    value = doc.get("last_discovery_count")
    return int(value) if isinstance(value, (int, float)) else None


def record_discovery_count(db, account_id: str, count: int, now: datetime | None = None) -> None:
    """Persist this run's count so the next run has a baseline. Best-effort."""
    if db is None:
        return
    now = now or datetime.now(timezone.utc)
    try:
        db.goout_sessions.update_one(
            {"account_id": account_id},
            {"$set": {"last_discovery_count": int(count), "last_discovery_at": now}},
            upsert=True,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CAPTCHA alert throttling
# ---------------------------------------------------------------------------

def captcha_message(account_id: str, marker: str, stage: str) -> str:
    return (
        f"🛑 *{account_id}*: GoOut is serving a bot challenge (`{marker}`) at the {stage} step. "
        "Login was aborted *without* triggering the 2FA relay. Automated logins will keep "
        "failing until this clears — try logging in manually from a browser, and if it "
        "persists the VPS IP may need a residential proxy/relay."
    )


def should_send_captcha_alert(db, account_id: str, now: datetime | None = None,
                              min_interval: timedelta = timedelta(hours=12)) -> bool:
    """
    True at most once per `min_interval` per account (sales runs every 4h would
    otherwise repeat the same alert six times a day). Records the send time.
    Without a db, always True.
    """
    if db is None:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        doc = db.goout_sessions.find_one({"account_id": account_id}, {"last_captcha_alert_at": 1}) or {}
        last = doc.get("last_captcha_alert_at")
        if isinstance(last, datetime):
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < min_interval:
                return False
        db.goout_sessions.update_one(
            {"account_id": account_id},
            {"$set": {"last_captcha_alert_at": now}},
            upsert=True,
        )
    except Exception:
        return True
    return True
