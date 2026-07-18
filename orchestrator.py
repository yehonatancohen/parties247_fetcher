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
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

import requests as http_requests

import config
from utils import normalize_url, normalized_or_none_for_dedupe, apply_default_referral, slugify_party
from scraper import GoOutScraper, GoOutAccount
from carousel_suggester import suggest_carousel_assignments, suggest_carousels_for_party

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


_TEST_INDICATORS = frozenset([
    "test", "copy of", "בדיקה", "אירוע בדיקתי", "העתק של", "demo", "dummy",
    "העתק", "טסט",
])


def _is_test_event(name: str) -> bool:
    """Return True if the event name looks like a test/draft/copy event."""
    if not name:
        return False
    name_lower = name.lower()
    return any(indicator in name_lower for indicator in _TEST_INDICATORS)


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
    # (name, date YYYY-MM-DD, imageUrl, party_id, referralCode, url) for name+date duplicate detection
    known_name_date: list[tuple[str, str, str, str, str, str]] = []
    try:
        resp = http_requests.get(f"{config.BACKEND_URL}/api/parties", timeout=15)
        if resp.status_code == 200:
            for p in resp.json():
                name = p.get("name", "")
                party_url = ""
                for field in ("canonicalUrl", "goOutUrl", "originalUrl"):
                    u = p.get(field)
                    if u:
                        known_parties[normalize_url(u)] = name
                        party_url = party_url or u
                date = (p.get("date") or "")[:10]
                image = p.get("imageUrl") or p.get("image") or ""
                pid = str(p.get("_id") or p.get("id") or "")
                ref = p.get("referralCode") or ""
                if name and date:
                    known_name_date.append((name, date, image, pid, ref, party_url))
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
        for k_name, k_date, k_image, k_id, k_ref, k_url in known_name_date:
            if k_date != party_date:
                continue
            if name.lower() == k_name.lower():
                return {"name": k_name, "imageUrl": k_image, "source": "approved", "exact": True,
                        "party_id": k_id, "referralCode": k_ref, "url": k_url}
            if _name_similarity(name, k_name) >= 0.6:
                return {"name": k_name, "imageUrl": k_image, "source": "approved", "exact": False,
                        "party_id": k_id, "referralCode": k_ref, "url": k_url}

        # Check parties already queued in this run
        if pending_coll is not None:
            try:
                for doc in pending_coll.find(
                    {"party_data.date": {"$regex": f"^{party_date}"}},
                    {"party_data.name": 1, "party_data.imageUrl": 1, "party_db_id": 1},
                ):
                    pd = doc.get("party_data", {})
                    p_name = pd.get("name", "")
                    if not p_name:
                        continue
                    p_id = str(doc.get("party_db_id") or "")
                    if p_name.lower() == name.lower():
                        return {"name": p_name, "imageUrl": pd.get("imageUrl", ""), "source": "pending",
                                "exact": True, "party_id": p_id, "referralCode": None, "url": None}
                    if _name_similarity(name, p_name) >= 0.6:
                        return {"name": p_name, "imageUrl": pd.get("imageUrl", ""), "source": "pending",
                                "exact": False, "party_id": p_id, "referralCode": None, "url": None}
            except Exception as exc:
                logger.warning(f"Duplicate pending check failed: {exc}")

        return None

    def _reattribute_to_account1(party_id: str, base_url: str, account1: GoOutAccount) -> bool:
        """Swap an existing party's referral/link over to account1 (priority account)."""
        if not party_id or not base_url or not account1.referral:
            return False
        try:
            headers = telegram_mgr._auth_headers()
            parsed = urlparse(base_url.split("?ref=")[0].split("&ref=")[0])
            qs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != "ref"]
            qs.append(("ref", account1.referral))
            new_url = urlunparse(parsed._replace(query=urlencode(qs)))
            r = http_requests.put(
                f"{config.BACKEND_URL}/api/admin/update-party/{party_id}",
                json={"referralCode": account1.referral, "goOutUrl": new_url, "originalUrl": new_url},
                headers=headers,
                timeout=15,
            )
            return r.status_code == 200
        except Exception as exc:
            logger.warning(f"Failed to reattribute party {party_id} to account1: {exc}")
            return False

    async def _scrape_one_account(account: GoOutAccount) -> list[str]:
        scraper = GoOutScraper(account, db=db, telegram_mgr=telegram_mgr)
        added_lines: list[str] = []
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

                # Early filter: skip test/demo events by name from discovery
                if _is_test_event(entry.get("name", "")):
                    logger.info(f"[{account.account_id}] Skipping test event: '{entry.get('name')}'")
                    continue

                canonical = normalize_url(event_url)

                # Skip if already approved in backend
                existing_name = _party_exists(canonical)
                if existing_name:
                    if force_send and telegram_mgr:
                        telegram_mgr.send_message_sync(f"✅ Already in DB: *{existing_name}*")
                    continue

                # Skip if already in pending (prevents daily re-sends of unapproved parties)
                if pending_coll is not None:
                    try:
                        existing_pending = pending_coll.find_one(
                            {"goOutUrl": canonical, "status": {"$ne": "rejected"}}
                        )
                        if existing_pending:
                            logger.info(f"[{account.account_id}] Already in pending, skipping: {canonical}")
                            continue
                    except Exception as exc:
                        logger.warning(f"[{account.account_id}] Pending dedup check failed: {exc}")

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

                # Second filter: skip test events found in full scraped name
                if _is_test_event(party_data.get("name", "")):
                    logger.info(f"[{account.account_id}] Skipping test event (full name): '{party_data.get('name')}'")
                    continue

                party_data["referralCode"] = account.referral
                apply_default_referral(party_data, account.referral)
                party_data.setdefault(
                    "slug", slugify_party(party_data.get("name"), party_data.get("date"))
                )

                dup = _find_duplicate(party_data.get("name", ""), party_data.get("date", ""))
                if dup and dup.get("exact"):
                    logger.info(
                        f"[{account.account_id}] Skipping exact duplicate: "
                        f"'{party_data.get('name')}' == '{dup['name']}' ({dup['source']})"
                    )
                    # Same event on both accounts: account1 is the priority account,
                    # so re-attribute the existing party to it (referral + hot-now).
                    if (
                        account.account_id == "account1"
                        and dup.get("source") == "approved"
                        and dup.get("referralCode") != account.referral
                        and dup.get("party_id") and dup.get("url")
                    ):
                        if _reattribute_to_account1(dup["party_id"], dup["url"], account):
                            logger.info(
                                f"[account1] Re-attributed '{dup['name']}' to account1 referral."
                            )
                    known_parties[canonical] = dup["name"]  # avoid re-checking in same run
                    continue
                if dup:
                    party_data["possible_duplicate"] = dup
                    logger.info(
                        f"[{account.account_id}] Possible duplicate: "
                        f"'{party_data.get('name')}' ~ '{dup['name']}' ({dup['source']})"
                    )

                # Auto-approve: call backend directly without human review
                try:
                    clean_url = canonical.split("?ref=")[0].split("&ref=")[0]
                    headers = telegram_mgr._auth_headers()

                    r1 = http_requests.post(
                        f"{config.BACKEND_URL}/api/admin/add-party",
                        json={"url": clean_url},
                        headers=headers,
                        timeout=60,
                    )
                    if r1.status_code not in (200, 201, 409):
                        logger.warning(
                            f"[{account.account_id}] Auto-approve returned {r1.status_code} "
                            f"for {clean_url}: {r1.text[:200]}"
                        )
                        await asyncio.sleep(2)
                        continue

                    d1 = r1.json()
                    party_db_id = (d1.get("party") or {}).get("_id") or d1.get("id")

                    # Apply account referral code to the party URL
                    if party_db_id and account.referral:
                        parsed = urlparse(clean_url)
                        qs = parse_qsl(parsed.query, keep_blank_values=True)
                        if not any(k.lower() == "ref" for k, _ in qs):
                            qs.append(("ref", account.referral))
                        goout_with_ref = urlunparse(parsed._replace(query=urlencode(qs)))
                        http_requests.put(
                            f"{config.BACKEND_URL}/api/admin/update-party/{party_db_id}",
                            json={
                                "referralCode": account.referral,
                                "goOutUrl": goout_with_ref,
                                "originalUrl": goout_with_ref,
                            },
                            headers=headers,
                            timeout=15,
                        )

                    # Auto-assign to matching carousels
                    applied_carousel_titles: list[str] = []
                    if party_db_id and telegram_mgr:
                        carousels = telegram_mgr._get_all_carousels()
                        suggested_ids = suggest_carousels_for_party(party_data, carousels)
                        for cid in suggested_ids:
                            if telegram_mgr._add_party_to_carousel(cid, party_db_id):
                                c = next(
                                    (c for c in carousels
                                     if str(c.get("id") or c.get("_id", "")) == cid),
                                    None,
                                )
                                if c:
                                    applied_carousel_titles.append(c.get("title", cid))

                    # Log to pending for audit trail
                    if pending_coll is not None:
                        try:
                            pending_coll.insert_one(_sanitize_doc({
                                "party_data": party_data,
                                "account_id": account.account_id,
                                "scraped_at": datetime.now(timezone.utc),
                                "status": "auto_approved",
                                "goOutUrl": canonical,
                                "party_db_id": party_db_id,
                                "applied_carousels": applied_carousel_titles,
                            }))
                        except Exception as exc:
                            logger.warning(f"Could not log auto-approve to pending: {exc}")

                    # Collect for the end-of-run summary message
                    name_str = party_data.get("name") or "?"
                    date_str = (party_data.get("date") or "?")[:10]
                    dup_flag = " ⚠️ possible dup" if dup else ""
                    carousel_str = (", ".join(applied_carousel_titles)
                                    if applied_carousel_titles else "none")
                    added_lines.append(
                        f"• *{name_str}* — 📅 {date_str}{dup_flag} — 🎠 {carousel_str}"
                    )

                    known_parties[canonical] = name_str  # prevent re-adding in same run

                except Exception as exc:
                    logger.error(f"[{account.account_id}] Auto-approve error for {canonical}: {exc}")

                await asyncio.sleep(2)

            logger.info(f"[{account.account_id}] Auto-approved {len(added_lines)} new events")
        except Exception as exc:
            logger.error(f"[{account.account_id}] Scrape error: {exc}")
            if telegram_mgr:
                telegram_mgr.send_message_sync(f"❌ Error scraping *{account.account_id}*: {exc}")
        finally:
            await scraper.close()
        return added_lines

    results = await asyncio.gather(
        *[_scrape_one_account(a) for a in accounts], return_exceptions=True
    )
    all_added: list[str] = []
    for r in results:
        if isinstance(r, list):
            all_added.extend(r)

    summary = (
        f"✅ *Scrape complete!*\n"
        f"Found {len(all_added)} new events across {len(accounts)} accounts."
    )
    if all_added:
        summary += "\n\n" + "\n".join(all_added)

    def _send_summary(text: str):
        try:
            http_requests.post(
                f"https://api.telegram.org/bot{telegram_mgr.token}/sendMessage",
                json={
                    "chat_id": telegram_mgr.manager_chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
        except Exception:
            if telegram_mgr:
                telegram_mgr.send_message_sync(text)

    # Telegram caps messages at 4096 chars — split on line boundaries if needed
    chunk = ""
    for line in summary.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > 3900:
            _send_summary(chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        _send_summary(chunk)


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

    # Build the correct set: ALL upcoming account1 parties
    account1_ids: set[str] = set()
    for party in all_parties:
        party_id = str(party.get("_id") or party.get("id", ""))
        if not party_id:
            continue
        if party.get("referralCode") == account1.referral:
            account1_ids.add(party_id)

    new_ids: list[str] = list(account1_ids)

    changed = set(new_ids) != set(current_ids)
    if changed:
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
                changed = False
        except Exception as exc:
            logger.error(f"[HOT-NOW] Failed to update carousel: {exc}")
            changed = False

    removed = len(current_ids) - len([p for p in current_ids if p in set(new_ids)])
    added = len([p for p in new_ids if p not in set(current_ids)])
    logger.info(f"[HOT-NOW] Hot Now updated: +{added} added, -{removed} removed ({len(new_ids)} total)")
    if changed and telegram_mgr:
        telegram_mgr.send_message_sync(
            f"🔥 *Hot Now* synced: +{added} added, -{removed} removed ({len(new_ids)} total)."
        )
