"""
Orchestrates daily scraping across all Go-Out accounts.

Calls the backend HTTP API for:
- scrape_party_details (/api/internal/scrape-party)

Writes directly to MongoDB for:
- goout_pending (storing parties awaiting approval)
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import requests as http_requests

import config
from utils import normalize_url, normalized_or_none_for_dedupe, apply_default_referral, slugify_party
from scraper import GoOutScraper, GoOutAccount

logger = logging.getLogger(__name__)


def _call_scrape_party(url: str) -> dict | None:
    """Call backend to scrape a party's full details."""
    try:
        resp = http_requests.post(
            f"{config.BACKEND_URL}/api/internal/scrape-party",
            json={"url": url},
            headers={"X-Service-Token": config.SERVICE_TOKEN},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"[SCRAPER] Backend returned {resp.status_code} for {url}: {resp.text[:200]}")
    except Exception as exc:
        logger.warning(f"[SCRAPER] Failed to call backend scrape-party for {url}: {exc}")
    return None


def run_daily_scrape(accounts: list[GoOutAccount], db, telegram_mgr, force_send: bool = False):
    """Synchronous entry point for the daily scrape job."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            _async_daily_scrape(accounts, db, telegram_mgr, force_send=force_send)
        )
    except Exception as exc:
        logger.error(f"Daily scrape failed: {exc}")
        if telegram_mgr:
            telegram_mgr.send_message_sync(f"❌ Daily scrape error: {exc}")
    finally:
        loop.close()


async def _async_daily_scrape(accounts: list[GoOutAccount], db, telegram_mgr, force_send: bool = False):
    if telegram_mgr and force_send:
        telegram_mgr.send_message_sync("🔄 *Manual scan starting — sending all found parties...*")

    pending_coll = db.goout_pending if db is not None else None
    parties_coll = db.parties if db is not None else None

    async def _scrape_one_account(account: GoOutAccount) -> int:
        scraper = GoOutScraper(account, db=db, telegram_mgr=telegram_mgr)
        new_count = 0
        try:
            session_ok = await scraper.ensure_session()
            if not session_ok:
                if telegram_mgr:
                    telegram_mgr.send_message_sync(
                        f"⚠️ Could not log in to *{account.account_id}*. Skipping."
                    )
                return 0

            event_entries = await scraper.discover_events()
            logger.info(f"[{account.account_id}] Discovered {len(event_entries)} event(s)")

            for entry in event_entries:
                event_url = entry.get("url", "")
                if not event_url:
                    continue

                canonical = normalize_url(event_url)

                if not force_send and parties_coll is not None:
                    try:
                        existing = parties_coll.find_one({
                            "$or": [
                                {"canonicalUrl": canonical},
                                {"goOutUrl": canonical},
                            ]
                        })
                        if existing:
                            continue
                    except Exception:
                        pass

                # Try to scrape full event details via backend
                party_data = _call_scrape_party(event_url)

                if not party_data:
                    logger.info(f"Using discovery metadata for {event_url}")
                    party_data = {
                        "name": entry.get("name") or "Unknown Event",
                        "goOutUrl": canonical,
                        "canonicalUrl": canonical,
                        "date": entry.get("date") or datetime.now().strftime("%Y-%m-%d"),
                        "image": entry.get("image") or "",
                        "source": "go-out",
                    }

                party_data["referralCode"] = account.referral
                apply_default_referral(party_data, account.referral)
                party_data.setdefault(
                    "slug", slugify_party(party_data.get("name"), party_data.get("date"))
                )

                if pending_coll is not None:
                    try:
                        pending_doc = {
                            "party_data": party_data,
                            "account_id": account.account_id,
                            "scraped_at": datetime.now(timezone.utc),
                            "status": "pending",
                            "goOutUrl": canonical,
                        }

                        pending_coll.insert_one(pending_doc)

                        if telegram_mgr:
                            telegram_mgr.send_party_for_approval_sync(pending_doc)
                        new_count += 1
                    except Exception as exc:
                        logger.error(f"Failed to handle party: {exc}")

                await asyncio.sleep(2)

            logger.info(f"[{account.account_id}] Processed {new_count} new events")
        except Exception as exc:
            logger.error(f"[{account.account_id}] Scrape error: {exc}")
            if telegram_mgr:
                telegram_mgr.send_message_sync(f"❌ Error scraping *{account.account_id}*: {exc}")
        finally:
            await scraper.close()
        return new_count

    results = await asyncio.gather(
        *[_scrape_one_account(a) for a in accounts], return_exceptions=True
    )
    total_new = sum(r for r in results if isinstance(r, int))

    summary = (
        f"✅ *Scrape complete!*\n"
        f"Found {total_new} new events across {len(accounts)} accounts."
    )
    try:
        resp = http_requests.post(
            f"https://api.telegram.org/bot{telegram_mgr.token}/sendMessage",
            json={
                "chat_id": telegram_mgr.manager_chat_id,
                "text": summary,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
    except Exception:
        if telegram_mgr:
            telegram_mgr.send_message_sync(summary)


def run_hot_now_update(accounts: list[GoOutAccount], db, telegram_mgr):
    """Add account1's upcoming parties (within 7 days) to the 'hot now' carousel."""
    if db is None:
        logger.warning("[HOT-NOW] No database — skipping")
        return

    account1 = next((a for a in accounts if a.account_id == "account1"), None)
    if not account1:
        logger.warning("[HOT-NOW] No account1 found in accounts list")
        return

    try:
        carousel = db.carousels.find_one({"title": {"$regex": r"hot.?now", "$options": "i"}})
    except Exception as exc:
        logger.error(f"[HOT-NOW] Failed to find hot-now carousel: {exc}")
        return

    if not carousel:
        logger.warning("[HOT-NOW] No 'hot now' carousel found in DB")
        return

    carousel_id = carousel["_id"]
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=7)
    today_str = now.strftime("%Y-%m-%d")
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    try:
        all_parties = list(db.parties.find({"referralCode": account1.referral}, {"_id": 1, "date": 1}))
    except Exception as exc:
        logger.error(f"[HOT-NOW] Failed to fetch account1 parties: {exc}")
        return

    added = 0
    for party in all_parties:
        raw_date = party.get("date")
        if not raw_date:
            continue
        if isinstance(raw_date, datetime):
            date_str = raw_date.strftime("%Y-%m-%d")
        else:
            date_str = str(raw_date)[:10]
        if today_str <= date_str <= cutoff_str:
            try:
                result = db.carousels.update_one(
                    {"_id": carousel_id},
                    {"$addToSet": {"partyIds": party["_id"]}},
                )
                if result.modified_count:
                    added += 1
            except Exception as exc:
                logger.warning(f"[HOT-NOW] Failed to add party {party['_id']}: {exc}")

    logger.info(f"[HOT-NOW] Added {added} new account1 parties to 'Hot Now' carousel")
    if added and telegram_mgr:
        telegram_mgr.send_message_sync(f"🔥 *Hot Now* updated: {added} upcoming account1 parties added.")
