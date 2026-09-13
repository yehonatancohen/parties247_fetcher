"""
Fake, harmless env vars so importing `config` (required by any module that
needs config.BACKEND_URL/SERVICE_TOKEN/etc, e.g. wa_sales_watch.py) doesn't
crash during test collection. Never real credentials, never touched over
the network — tests that need specific values monkeypatch `<module>.config`
attributes directly (see test_wa_sales_watch.py). Existing pure modules
(alerts.py, best_sellers.py) don't import config at all and are unaffected.
"""

import os

_DEFAULTS = {
    "TELEGRAM_BOT_TOKEN": "test-token",
    "TELEGRAM_MANAGER_CHAT_ID": "0",
    "MONGODB_URI": "mongodb://localhost/test",
    "BACKEND_URL": "https://backend.test",
    "ADMIN_PASSWORD": "test-password",
    "SERVICE_TOKEN": "test-service-token",
}
for _key, _value in _DEFAULTS.items():
    os.environ.setdefault(_key, _value)
