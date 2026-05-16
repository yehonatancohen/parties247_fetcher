"""
Entry point for the GoOut Scraper VM service.

Starts:
  1. Telegram bot (async, in its own thread)
  2. APScheduler daily scrape job
  3. Flask API server (blocking, main thread)
"""

import logging
import threading
from datetime import datetime

import pymongo
from apscheduler.schedulers.background import BackgroundScheduler

import config
import api_server
from orchestrator import run_daily_scrape
from scraper import GoOutAccount
from telegram_bot import TelegramManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _build_accounts() -> list[GoOutAccount]:
    accounts = []
    for cfg in config.GOOUT_ACCOUNTS:
        accounts.append(
            GoOutAccount(
                account_id=cfg["account_id"],
                email=cfg["email"],
                password=cfg["password"],
                referral=cfg.get("referral", ""),
            )
        )
    return accounts


def main():
    # MongoDB
    mongo_client = pymongo.MongoClient(config.MONGODB_URI)
    db = mongo_client.get_default_database()

    accounts = _build_accounts()
    if not accounts:
        logger.error("No GoOut accounts configured — check GOOUT_ACCOUNT1_EMAIL etc.")

    # Telegram manager
    telegram_mgr = TelegramManager(
        token=config.TELEGRAM_BOT_TOKEN,
        manager_chat_id=config.TELEGRAM_MANAGER_CHAT_ID,
        db=db,
    )

    # Pass references into the API server module
    api_server.init(accounts, db, telegram_mgr)

    # APScheduler — daily scrape
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        func=lambda: run_daily_scrape(accounts, db, telegram_mgr, force_send=False),
        trigger="cron",
        hour=config.GOOUT_SCRAPE_HOUR,
        minute=0,
        id="daily_goout_scrape",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started — daily scrape at {config.GOOUT_SCRAPE_HOUR:02d}:00 UTC")

    # Telegram bot runs in its own daemon thread
    bot_thread = threading.Thread(
        target=telegram_mgr.run_polling,
        daemon=True,
        name="telegram-bot",
    )
    bot_thread.start()
    logger.info("Telegram bot thread started")

    # Flask API (blocking)
    logger.info(f"Starting API server on port {config.API_PORT}")
    api_server.run_server()


if __name__ == "__main__":
    main()
