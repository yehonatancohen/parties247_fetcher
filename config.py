import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_MANAGER_CHAT_ID: str = os.environ["TELEGRAM_MANAGER_CHAT_ID"]
MONGODB_URI: str = os.environ["MONGODB_URI"]

# Backend URL for approve/reject/scrape-party API calls
BACKEND_URL: str = os.environ["BACKEND_URL"].rstrip("/")

# Shared secret for service-to-service auth (backend must have the same value)
SERVICE_TOKEN: str = os.environ["SERVICE_TOKEN"]

# Go-Out account credentials
GOOUT_ACCOUNTS: list[dict] = []
for _idx in ("1", "2"):
    _email = os.environ.get(f"GOOUT_ACCOUNT{_idx}_EMAIL", "")
    _password = os.environ.get(f"GOOUT_ACCOUNT{_idx}_PASSWORD", "")
    _referral = os.environ.get(f"GOOUT_ACCOUNT{_idx}_REFERRAL", "")
    if _email and _password:
        GOOUT_ACCOUNTS.append({
            "account_id": f"account{_idx}",
            "email": _email,
            "password": _password,
            "referral": _referral,
        })

GOOUT_SCRAPE_HOUR: int = int(os.environ.get("GOOUT_SCRAPE_HOUR", "6"))
API_PORT: int = int(os.environ.get("API_PORT", "5001"))
