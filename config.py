import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_MANAGER_CHAT_ID: str = os.environ["TELEGRAM_MANAGER_CHAT_ID"]
MONGODB_URI: str = os.environ["MONGODB_URI"]

# Backend URL for approve/reject/scrape-party API calls
BACKEND_URL: str = os.environ["BACKEND_URL"].rstrip("/")

# Admin password for the backend (used to obtain a JWT for API calls)
ADMIN_PASSWORD: str = os.environ["ADMIN_PASSWORD"]

# Shared secret for scrape-party internal endpoint
SERVICE_TOKEN: str = os.environ["SERVICE_TOKEN"]

# MongoDB database name (required when URI has no default db in the path)
MONGODB_DB_NAME: str = os.environ.get("MONGODB_DB_NAME", "parties247")

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
