"""
Entry point for the GoOut Scraper VM service.

Starts:
  1. APScheduler daily scrape job (background thread)
  2. Telegram bot (runs in the main thread via run_polling)
"""

import logging

import pymongo
from apscheduler.schedulers.background import BackgroundScheduler

import config
from orchestrator import run_daily_scrape
from scraper import GoOutAccount
from telegram_bot import TelegramManager
from sales_tracker import run_sales_update

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _build_accounts() -> list[GoOutAccount]:
    return [
        GoOutAccount(
            account_id=cfg["account_id"],
            email=cfg["email"],
            password=cfg["password"],
            referral=cfg.get("referral", ""),
        )
        for cfg in config.GOOUT_ACCOUNTS
    ]


def main():
    mongo_client = pymongo.MongoClient(config.MONGODB_URI)
    db = mongo_client[config.MONGODB_DB_NAME]

    # Quick connectivity check
    try:
        mongo_client.admin.command("ping")
        logger.info(f"Connected to MongoDB at {config.MONGODB_URI.split('@')[-1]}")
    except Exception as exc:
        logger.error(f"Could not connect to MongoDB: {exc}")
        # We continue anyway as the app might recover, or fail later with better context

    accounts = _build_accounts()
    if not accounts:
        logger.error("No GoOut accounts configured — check GOOUT_ACCOUNT1_EMAIL etc.")

    telegram_mgr = TelegramManager(
        token=config.TELEGRAM_BOT_TOKEN,
        manager_chat_id=config.TELEGRAM_MANAGER_CHAT_ID,
        db=db,
    )

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        func=lambda: run_daily_scrape(accounts, db, telegram_mgr, force_send=False),
        trigger="cron",
        hour=config.GOOUT_SCRAPE_HOUR,
        minute=0,
        id="daily_goout_scrape",
        replace_existing=True,
    )
    # Sales data update every 4 hours (tracks confirmed/pending tickets and revenue)
    scheduler.add_job(
        func=lambda: run_sales_update(accounts, db, telegram_mgr),
        trigger="interval",
        hours=4,
        id="sales_update",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        f"Scheduler started — daily scrape at {config.GOOUT_SCRAPE_HOUR:02d}:00 UTC, "
        "sales update every 4 hours"
    )

    # Wire /scrape command to trigger a manual scan
    telegram_mgr.on_scrape_requested = lambda: run_daily_scrape(
        accounts, db, telegram_mgr, force_send=True
    )

    def _scrape_single_account(account_id: str):
        matched = [a for a in accounts if a.account_id == account_id]
        if not matched:
            telegram_mgr.send_message_sync(f"⚠️ Unknown account: *{account_id}*")
            return
        run_daily_scrape(matched, db, telegram_mgr, force_send=True)

    telegram_mgr.on_scrape_account_requested = _scrape_single_account
    telegram_mgr.on_sales_update_requested = lambda: run_sales_update(accounts, db, telegram_mgr)

    logger.info("Starting Telegram bot (polling)...")
    telegram_mgr.run_polling()


if __name__ == "__main__":
    main()
