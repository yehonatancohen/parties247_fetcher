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
    parties_coll = db.client["party247"].parties if db is not None else None

    def _party_exists(canonical: str) -> str | None:
        """Return party name if already in DB, else None."""
        if parties_coll is None:
            return None
        try:
            doc = parties_coll.find_one(
                {"$or": [{"canonicalUrl": canonical}, {"goOutUrl": canonical}]},
                {"name": 1},
            )
            return doc.get("name") if doc else None
        except Exception:
            return None

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
                existing_name = _party_exists(canonical)

                if existing_name:
                    if force_send and telegram_mgr:
                        telegram_mgr.send_message_sync(
                            f"✅ Already in DB: *{existing_name}*"
                        )
                    continue

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
    account1 = next((a for a in accounts if a.account_id == "account1"), None)
    if not account1:
        logger.warning("[HOT-NOW] No account1 found in accounts list")
        return

    try:
        resp = http_requests.get(f"{config.BACKEND_URL}/api/carousels", timeout=10)
        carousels = resp.json() if resp.status_code == 200 else []
    except Exception as exc:
        logger.error(f"[HOT-NOW] Failed to fetch carousels: {exc}")
        return

    carousel = next((c for c in carousels if "חם עכשיו" in (c.get("title") or "")), None)
    if not carousel:
        logger.warning("[HOT-NOW] No 'hot now' carousel found")
        return

    carousel_id = carousel["id"]
    current_ids = [str(pid) for pid in carousel.get("partyIds", [])]

    try:
        resp = http_requests.get(f"{config.BACKEND_URL}/api/parties?upcoming=true", timeout=15)
        all_parties = resp.json() if resp.status_code == 200 else []
    except Exception as exc:
        logger.error(f"[HOT-NOW] Failed to fetch parties: {exc}")
        return

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=7)
    today_str = now.strftime("%Y-%m-%d")
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    new_ids = list(current_ids)
    added = 0
    for party in all_parties:
        if party.get("referralCode") != account1.referral:
            continue
        raw_date = party.get("date") or party.get("startsAt", "")
        date_str = str(raw_date)[:10]
        if not (today_str <= date_str <= cutoff_str):
            continue
        party_id = str(party.get("_id") or party.get("id", ""))
        if party_id and party_id not in new_ids:
            new_ids.append(party_id)
            added += 1

    if added:
        try:
            headers = telegram_mgr._auth_headers()
            resp = http_requests.put(
                f"{config.BACKEND_URL}/api/admin/carousels/{carousel_id}/parties",
                json={"partyIds": new_ids},
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                logger.error(f"[HOT-NOW] Failed to update carousel: {resp.status_code} {resp.text[:200]}")
                added = 0
        except Exception as exc:
            logger.error(f"[HOT-NOW] Failed to update carousel: {exc}")
            added = 0

    logger.info(f"[HOT-NOW] Added {added} new account1 parties to 'Hot Now' carousel")
    if added and telegram_mgr:
        telegram_mgr.send_message_sync(f"🔥 *Hot Now* updated: {added} upcoming account1 parties added.")
