"""
Orchestrates daily scraping across all Go-Out accounts.

Calls the backend HTTP API for:
- scrape_party_details (/api/internal/scrape-party)

Writes directly to MongoDB for:
- goout_pending (storing parties awaiting approval)
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta

import requests as http_requests

import config
from utils import normalize_url, normalized_or_none_for_dedupe, apply_default_referral, slugify_party
from scraper import GoOutScraper, GoOutAccount
from carousel_suggester import suggest_carousel_assignments

logger = logging.getLogger(__name__)


def _sanitize_doc(obj):
    """Recursively replace surrogate-containing strings so MongoDB won't reject them."""
    if isinstance(obj, dict):
        return {k: _sanitize_doc(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_doc(v) for v in obj]
    if isinstance(obj, str):
        return obj.encode("utf-8", "replace").decode("utf-8")
    return obj


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


def _scrape_event_page(url: str) -> dict | None:
    """Fetch a public Go-Out event page and extract party details from __NEXT_DATA__."""
    try:
        resp = http_requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"_scrape_event_page: {resp.status_code} for {url}")
            return None

        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text, re.DOTALL)
        if not m:
            return None

        data = json.loads(m.group(1))

        def _find_evt(obj, depth=0):
            if depth > 10:
                return None
            if isinstance(obj, dict):
                if obj.get("EventSerial") and obj.get("StartingDate"):
                    return obj
                for v in obj.values():
                    r = _find_evt(v, depth + 1)
                    if r:
                        return r
            elif isinstance(obj, list):
                for item in obj:
                    r = _find_evt(item, depth + 1)
                    if r:
                        return r
            return None

        evt = _find_evt(data)
        if not evt:
            return None

        date_raw = evt.get("StartingDate") or evt.get("startingDate") or ""
        date_str = date_raw[:10] if date_raw else None

        price = None
        for ticket in (evt.get("Tickets") or []):
            if ticket.get("Active", True):
                t_price = ticket.get("Price")
                if t_price and (price is None or t_price < price):
                    price = t_price
        if price is None:
            price = evt.get("Price")

        location = evt.get("Adress") or evt.get("EnglishAddress")

        image_url = None
        schema = evt.get("schemaOrg")
        if isinstance(schema, list) and schema:
            imgs = schema[0].get("image") or []
            if imgs:
                image_url = imgs[0]
        if not image_url:
            eid = evt.get("_id")
            ts = evt.get("CoverImageTimestamp")
            if eid and ts:
                image_url = f"https://images.go-out.co/{eid}{ts}_coverImage.jpg"

        result = {
            "name": evt.get("Title") or evt.get("Name"),
            "date": date_str,
            "source": "go-out",
        }
        if price is not None:
            result["ticketPrice"] = price
        if location:
            result["location"] = location
        if image_url:
            result["imageUrl"] = image_url
        if evt.get("MusicType"):
            result["musicType"] = evt["MusicType"]
        if evt.get("EventType"):
            result["eventType"] = evt["EventType"]
        if evt.get("MinimumAge"):
            result["age"] = str(evt["MinimumAge"])
        if evt.get("Description"):
            result["description"] = evt["Description"]
        return result
    except Exception as exc:
        logger.warning(f"_scrape_event_page failed for {url}: {exc}")
    return None


def _name_similarity(a: str, b: str) -> float:
    """Word-overlap ratio between two party names (0.0–1.0)."""
    a_words = set(a.lower().split())
    b_words = set(b.lower().split())
    if not a_words or not b_words:
        return 0.0
    return len(a_words & b_words) / max(len(a_words), len(b_words))


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

    run_hot_now_update(accounts, db, telegram_mgr)
    run_carousel_auto_assign(telegram_mgr)


async def _async_daily_scrape(accounts: list[GoOutAccount], db, telegram_mgr, force_send: bool = False):
    if telegram_mgr and force_send:
        telegram_mgr.send_message_sync("🔄 *Manual scan starting — sending all found parties...*")

    pending_coll = db.goout_pending if db is not None else None

    # Build a URL→name lookup from the backend once for the whole scrape run
    known_parties: dict[str, str] = {}  # normalized_url -> party name
    # (name, date YYYY-MM-DD, imageUrl) for name+date duplicate detection
    known_name_date: list[tuple[str, str, str]] = []
    try:
        resp = http_requests.get(f"{config.BACKEND_URL}/api/parties", timeout=15)
        if resp.status_code == 200:
            for p in resp.json():
                name = p.get("name", "")
                for field in ("canonicalUrl", "goOutUrl", "originalUrl"):
                    u = p.get(field)
                    if u:
                        known_parties[normalize_url(u)] = name
                date = (p.get("date") or "")[:10]
                image = p.get("imageUrl") or p.get("image") or ""
                if name and date:
                    known_name_date.append((name, date, image))
            logger.info(f"Loaded {len(known_parties)} known party URLs from backend")
    except Exception as exc:
        logger.warning(f"Could not prefetch parties list: {exc}")

    def _party_exists(canonical: str) -> str | None:
        """Return party name if already in DB, else None."""
        return known_parties.get(canonical)

    def _find_duplicate(name: str, date: str) -> dict | None:
        """Check for same-event different-account duplicates by name+date similarity."""
        if not name or not date:
            return None
        party_date = date[:10]

        # Check approved parties from backend
        for k_name, k_date, k_image in known_name_date:
            if k_date != party_date:
                continue
            if _name_similarity(name, k_name) >= 0.6 and name.lower() != k_name.lower():
                return {"name": k_name, "imageUrl": k_image, "source": "approved"}

        # Check parties already queued in this run
        if pending_coll is not None:
            try:
                for doc in pending_coll.find(
                    {"party_data.date": {"$regex": f"^{party_date}"}},
                    {"party_data.name": 1, "party_data.imageUrl": 1},
                ):
                    pd = doc.get("party_data", {})
                    p_name = pd.get("name", "")
                    if not p_name or p_name.lower() == name.lower():
                        continue
                    if _name_similarity(name, p_name) >= 0.6:
                        return {"name": p_name, "imageUrl": pd.get("imageUrl", ""), "source": "pending"}
            except Exception as exc:
                logger.warning(f"Duplicate pending check failed: {exc}")

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

                # Try to scrape full event details via backend, then direct page scrape
                party_data = _call_scrape_party(event_url)

                if not party_data:
                    party_data = _scrape_event_page(event_url)
                    if party_data:
                        party_data["goOutUrl"] = canonical
                        party_data["canonicalUrl"] = canonical
                        logger.info(f"[{account.account_id}] Used direct page scrape for {event_url}")

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

                dup = _find_duplicate(party_data.get("name", ""), party_data.get("date", ""))
                if dup:
                    party_data["possible_duplicate"] = dup
                    logger.info(
                        f"[{account.account_id}] Possible duplicate detected: "
                        f"'{party_data.get('name')}' ~ '{dup['name']}' ({dup['source']})"
                    )

                if pending_coll is not None:
                    try:
                        pending_doc = _sanitize_doc({
                            "party_data": party_data,
                            "account_id": account.account_id,
                            "scraped_at": datetime.now(timezone.utc),
                            "status": "pending",
                            "goOutUrl": canonical,
                        })

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


def run_carousel_auto_assign(telegram_mgr):
    """Auto-assign upcoming parties to existing carousels based on title heuristics."""
    try:
        suggestions = suggest_carousel_assignments(config.BACKEND_URL)
    except Exception as exc:
        logger.error(f"[CAROUSEL-AUTO] Failed to fetch suggestions: {exc}")
        return

    total_added = 0
    for cid, info in suggestions.items():
        to_add = info.get("to_add", [])
        if not to_add:
            continue

        # Fetch current carousel state to avoid stale partyIds
        try:
            resp = http_requests.get(f"{config.BACKEND_URL}/api/carousels", timeout=10)
            carousels = resp.json() if resp.status_code == 200 else []
        except Exception as exc:
            logger.error(f"[CAROUSEL-AUTO] Failed to refresh carousels: {exc}")
            continue

        carousel = next((c for c in carousels if str(c.get("id") or c.get("_id", "")) == cid), None)
        if not carousel:
            continue

        current_ids = [str(pid) for pid in (carousel.get("partyIds") or [])]
        current_set = set(current_ids)
        new_ids = current_ids + [p["id"] for p in to_add if p["id"] not in current_set]

        if len(new_ids) == len(current_ids):
            continue

        try:
            headers = telegram_mgr._auth_headers() if telegram_mgr else {}
            resp = http_requests.put(
                f"{config.BACKEND_URL}/api/admin/carousels/{cid}/parties",
                json={"partyIds": new_ids},
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                added = len(new_ids) - len(current_ids)
                total_added += added
                logger.info(f"[CAROUSEL-AUTO] '{info['title']}': +{added} parties")
            else:
                logger.error(f"[CAROUSEL-AUTO] Failed for '{info['title']}': {resp.status_code} {resp.text[:200]}")
        except Exception as exc:
            logger.error(f"[CAROUSEL-AUTO] Error updating '{info['title']}': {exc}")

    logger.info(f"[CAROUSEL-AUTO] Done. Total parties added across all carousels: {total_added}")
    if total_added and telegram_mgr:
        telegram_mgr.send_message_sync(f"🎠 *Carousel auto-assign*: {total_added} parties added across carousels.")


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
