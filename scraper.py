"""
Go-Out Scraper — Playwright-based scraper for the Go-Out organizer panel.

Handles:
- Login with email/password + 2FA via Telegram relay
- Persistent session storage in MongoDB
- Event discovery from the organizer panel at /businesspage
"""

import json
import asyncio
import logging
import re
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

GO_OUT_BASE = "https://www.go-out.co"
GO_OUT_LOGIN_URL = f"{GO_OUT_BASE}/loginpage"
GO_OUT_PANEL_URL = f"{GO_OUT_BASE}/businesspage"
GO_OUT_EVENT_BASE = f"{GO_OUT_BASE}/event/"

LOGIN_EMAIL_SEL = "#register_email"
LOGIN_PASSWORD_SEL = "#register_password"
LOGIN_BUTTON_SEL = "#register_button"
ORGANIZER_PANEL_TEXT = "פאנל מארגנים"


class GoOutAccount:
    def __init__(self, account_id: str, email: str, password: str, referral: str):
        self.account_id = account_id
        self.email = email
        self.password = password
        self.referral = referral


class GoOutScraper:
    def __init__(self, account: GoOutAccount, db=None, telegram_mgr=None):
        self.account = account
        self._db = db
        self._telegram = telegram_mgr
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None

    # ------------------------------------------------------------------
    # Session persistence (MongoDB)
    # ------------------------------------------------------------------

    def _sessions_coll(self):
        if self._db is not None:
            return self._db.goout_sessions
        return None

    def _load_storage_state(self) -> dict | None:
        coll = self._sessions_coll()
        if coll is None:
            return None
        try:
            doc = coll.find_one({"account_id": self.account.account_id})
            if doc and doc.get("session_valid") and doc.get("storage_state"):
                return doc["storage_state"]
        except Exception as exc:
            logger.warning(f"[{self.account.account_id}] Failed to load session: {exc}")
        return None

    def _save_storage_state(self, state: dict):
        coll = self._sessions_coll()
        if coll is None:
            return
        try:
            coll.update_one(
                {"account_id": self.account.account_id},
                {"$set": {
                    "account_id": self.account.account_id,
                    "email": self.account.email,
                    "storage_state": state,
                    "session_valid": True,
                    "last_login": datetime.now(timezone.utc).isoformat(),
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                }},
                upsert=True,
            )
        except Exception as exc:
            logger.error(f"[{self.account.account_id}] Failed to save session: {exc}")

    def _mark_session_invalid(self):
        coll = self._sessions_coll()
        if coll is None:
            return
        try:
            coll.update_one(
                {"account_id": self.account.account_id},
                {"$set": {
                    "session_valid": False,
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                }},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    async def _launch(self):
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        saved = self._load_storage_state()
        ctx_kwargs = dict(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        if saved:
            logger.info(f"[{self.account.account_id}] Restoring saved session...")
            ctx_kwargs["storage_state"] = saved
        self._context = await self._browser.new_context(**ctx_kwargs)
        self._page = await self._context.new_page()
        self._page.on("console", lambda msg: logger.debug(f"[{self.account.account_id}] PAGE: {msg.text}"))
        await self._dismiss_cookie_dialog()

    async def _dismiss_cookie_dialog(self):
        try:
            selectors = [
                ".ch2-allow-all-btn",
                "button:has-text('Allow all')",
                "button:has-text('Allow all cookies')",
                "button:has-text('אשר הכל')",
                "button:has-text('Got it')",
                "#hs-eu-confirmation-button",
            ]
            for sel in selectors:
                try:
                    btn = await self._page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await self._page.wait_for_timeout(1000)
                        break
                except Exception:
                    continue
        except Exception:
            pass

    async def close(self):
        try:
            if self._context:
                state = await self._context.storage_state()
                self._save_storage_state(state)
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------

    async def _is_logged_in(self) -> bool:
        try:
            await self._page.goto(GO_OUT_PANEL_URL, wait_until="domcontentloaded", timeout=30000)
            await self._page.wait_for_timeout(3000)
            current = self._page.url
            if "/loginpage" in current or "/login" in current:
                logger.info(f"[{self.account.account_id}] Redirected to login → not logged in")
                return False
            heading = await self._page.query_selector('text="ניהול אירועים"')
            if heading:
                logger.info(f"[{self.account.account_id}] Panel loaded — session valid")
                return True
            login_btn = await self._page.query_selector('text="התחברות/הרשמה"')
            if login_btn and await login_btn.is_visible():
                return False
            if "/businesspage" in current:
                return True
            return False
        except Exception as exc:
            logger.warning(f"[{self.account.account_id}] Login check failed: {exc}")
            return False

    async def _perform_login(self) -> bool:
        logger.info(f"[{self.account.account_id}] Navigating to login page...")
        try:
            await self._page.goto(GO_OUT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            await self._page.wait_for_timeout(2000)
        except Exception as exc:
            logger.error(f"[{self.account.account_id}] Failed to load login page: {exc}")
            return False

        try:
            await self._page.wait_for_selector(LOGIN_EMAIL_SEL, timeout=10000)
            await self._page.fill(LOGIN_EMAIL_SEL, self.account.email)
            await self._page.wait_for_timeout(500)
        except Exception as exc:
            logger.error(f"[{self.account.account_id}] Email field not found: {exc}")
            return False

        try:
            await self._page.fill(LOGIN_PASSWORD_SEL, self.account.password)
            await self._page.wait_for_timeout(500)
        except Exception as exc:
            logger.error(f"[{self.account.account_id}] Password field not found: {exc}")
            return False

        if self._telegram:
            logger.info(f"[{self.account.account_id}] Asking manager 2FA availability...")
            available = self._telegram.ask_2fa_availability_sync(self.account.account_id, timeout=600)
            if not available:
                logger.warning(f"[{self.account.account_id}] Manager not available for 2FA; aborting.")
                self._mark_session_invalid()
                return False

        try:
            await self._page.click(LOGIN_BUTTON_SEL)
            await self._page.wait_for_timeout(5000)
        except Exception as exc:
            logger.error(f"[{self.account.account_id}] Login button click failed: {exc}")
            return False

        needs_2fa = await self._check_for_2fa()
        if needs_2fa:
            success = await self._handle_2fa(already_confirmed_available=True)
            if not success:
                self._mark_session_invalid()
                return False

        await self._page.wait_for_timeout(2000)
        logged_in = await self._is_logged_in()
        if logged_in:
            state = await self._context.storage_state()
            self._save_storage_state(state)
            logger.info(f"[{self.account.account_id}] Login successful, session saved.")
        else:
            self._mark_session_invalid()
            logger.error(f"[{self.account.account_id}] Login failed after attempt.")
            try:
                os.makedirs("scratch", exist_ok=True)
                with open("scratch/login_failed.html", "w", encoding="utf-8") as f:
                    f.write(await self._page.content())
                await self._page.screenshot(path="scratch/login_failed.png")
            except Exception:
                pass
        return logged_in

    async def _check_for_2fa(self) -> bool:
        try:
            os.makedirs("scratch", exist_ok=True)
            with open("scratch/2fa_page.html", "w", encoding="utf-8") as f:
                f.write(await self._page.content())
            await self._page.screenshot(path="scratch/2fa_page.png")
        except Exception:
            pass
        try:
            for selector in [
                'input[maxlength="6"]', 'input[type="tel"]',
                'input[placeholder*="קוד"]', 'input[placeholder*="code"]',
                'input[name*="otp"]', 'input[name*="code"]',
                '[class*="otp"]', '[class*="verification"]',
            ]:
                elem = await self._page.query_selector(selector)
                if elem and await elem.is_visible():
                    logger.info(f"[{self.account.account_id}] 2FA input detected: {selector}")
                    return True
            content = await self._page.content()
            for indicator in ["קוד אימות", "verification code", "enter code", "אימות דו"]:
                if indicator.lower() in content.lower():
                    return True
        except Exception:
            pass
        return False

    async def _handle_2fa(self, already_confirmed_available: bool = False) -> bool:
        if not self._telegram:
            logger.error(f"[{self.account.account_id}] 2FA required but no Telegram manager.")
            return False
        if not already_confirmed_available:
            available = self._telegram.ask_2fa_availability_sync(self.account.account_id, timeout=600)
            if not available:
                return False

        code = self._telegram.request_2fa_code_sync(self.account.account_id, timeout=600)
        if not code:
            return False

        otp_filled = False
        for selector in [
            'input[maxlength="6"]', 'input[type="tel"]',
            'input[placeholder*="קוד"]', 'input[name*="otp"]', 'input[name*="code"]',
        ]:
            try:
                elem = await self._page.query_selector(selector)
                if elem and await elem.is_visible():
                    await elem.type(code, delay=100)
                    otp_filled = True
                    break
            except Exception:
                continue

        if not otp_filled:
            try:
                await self._page.keyboard.type(code)
                otp_filled = True
            except Exception:
                pass

        if not otp_filled:
            logger.error(f"[{self.account.account_id}] Could not fill 2FA code.")
            return False

        await self._page.wait_for_timeout(500)
        for btn_text in ["אימות", "verify", "submit", "אישור", "confirm"]:
            try:
                btn = await self._page.query_selector(f'button:has-text("{btn_text}")')
                if btn and await btn.is_visible():
                    await btn.click()
                    break
            except Exception:
                continue
        try:
            await self._page.keyboard.press("Enter")
        except Exception:
            pass

        await self._page.wait_for_timeout(3000)
        self._telegram.send_message_sync(f"✅ 2FA code entered for {self.account.account_id}.")
        return True

    # ------------------------------------------------------------------
    # Session ensure
    # ------------------------------------------------------------------

    async def ensure_session(self) -> bool:
        await self._launch()
        if await self._is_logged_in():
            return True
        logger.info(f"[{self.account.account_id}] Session invalid, attempting login...")
        return await self._perform_login()

    # ------------------------------------------------------------------
    # Event discovery
    # ------------------------------------------------------------------

    async def discover_events(self) -> list[dict]:
        api_events: list[dict] = []
        events: list[dict] = []
        try:
            events = await self._scrape_organizer_panel(api_events)
            if not events and api_events:
                logger.info(f"[{self.account.account_id}] Falling back to {len(api_events)} intercepted events.")
                events = api_events
            elif events and api_events:
                seen_ids = {e["go_out_id"] for e in events if "go_out_id" in e}
                for ae in api_events:
                    eid = ae.get("go_out_id")
                    if eid and eid not in seen_ids:
                        events.append(ae)
                        seen_ids.add(eid)
        except Exception as exc:
            logger.error(f"[{self.account.account_id}] Organizer panel discovery failed: {exc}")
            if api_events and not events:
                logger.info(f"[{self.account.account_id}] Recovering {len(api_events)} intercepted events despite error.")
                events = api_events

        try:
            if self._context:
                state = await self._context.storage_state()
                self._save_storage_state(state)
        except Exception:
            pass

        logger.info(f"[{self.account.account_id}] Discovery complete. Total: {len(events)}")
        return events

    async def _scrape_organizer_panel(self, api_events_found: list) -> list[dict]:
        logger.info(f"[{self.account.account_id}] Loading organizer panel...")

        async def intercept_response(response):
            try:
                ct = response.headers.get("content-type", "").lower()
                if response.ok and ("json" in ct or "text/plain" in ct or "application/octet-stream" in ct):
                    url = response.url
                    if "userway" in url or "facebook" in url or "google" in url or "tiktok" in url:
                        return
                    try:
                        data = await response.json()
                    except Exception:
                        try:
                            data = json.loads(await response.text())
                        except Exception:
                            return

                    if "events" in url or "business" in url or "myEvents" in url:
                        logger.info(f"[{self.account.account_id}] Potential Event API: {url}")

                    def find_slugs(obj):
                        if isinstance(obj, dict):
                            eid = obj.get("EventSerial") or obj.get("_id") or obj.get("id") or obj.get("Id")
                            slug = (
                                obj.get("Url") or obj.get("url") or obj.get("slug") or
                                obj.get("Slug") or obj.get("slugName") or
                                obj.get("link") or obj.get("publicUrl")
                            )
                            if eid and slug and isinstance(slug, (str, int)) and "eventmanagement" not in str(slug):
                                slug_str = str(slug)
                                full_url = slug_str if "go-out.co" in slug_str else f"https://go-out.co/event/{slug_str}"
                                if not any(e.get("go_out_id") == str(eid) for e in api_events_found):
                                    logger.info(f"[{self.account.account_id}] API INTERCEPT: '{slug_str}' for #{eid}")
                                    api_events_found.append({
                                        "url": full_url,
                                        "go_out_id": str(eid),
                                        "name": obj.get("Title") or obj.get("Name"),
                                        "source": "api_intercept",
                                    })
                            for v in obj.values():
                                if isinstance(v, (dict, list)):
                                    find_slugs(v)
                        elif isinstance(obj, list):
                            for item in obj:
                                find_slugs(item)

                    find_slugs(data)
            except Exception:
                pass

        self._page.on("response", intercept_response)

        await self._page.goto(GO_OUT_PANEL_URL, wait_until="domcontentloaded", timeout=30000)
        await self._page.wait_for_timeout(3000)

        try:
            events_tab_selectors = [
                'div[role="button"]:has(img[src*="MyEvents/globe"])',
                'button:has(img[src*="MyEvents/globe"])',
                'a[href="/businesspage"]',
                'img[src*="MyEvents/globe"]',
                'text="אירועים"',
                'text="Events"',
            ]
            logger.info(f"[{self.account.account_id}] Current URL: {self._page.url}")
            for sel in events_tab_selectors:
                try:
                    tab = await self._page.query_selector(sel)
                    if tab:
                        await tab.click(force=True)
                        await self._page.wait_for_timeout(2000)
                        break
                except Exception:
                    pass
            await self._page.wait_for_selector(
                'tr, [role="row"], .MuiTableRow-root', timeout=15000, state="attached"
            )
        except Exception as e:
            logger.warning(f"[{self.account.account_id}] Could not ensure Events tab: {e}")

        for _ in range(3):
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self._page.wait_for_timeout(2000)
        await self._page.evaluate("window.scrollTo(0, 0)")

        logger.info(f"[{self.account.account_id}] Waiting for API responses (up to 72s)...")
        for i in range(24):
            if len(api_events_found) >= 1:
                await self._page.wait_for_timeout(3000)
                logger.info(f"[{self.account.account_id}] Caught {len(api_events_found)} events via API. Returning FAST.")
                return api_events_found

            if i > 0 and i % 7 == 0:
                logger.info(f"[{self.account.account_id}] Still no API data. Retrying Events tab click...")
                try:
                    os.makedirs("scratch", exist_ok=True)
                    await self._page.screenshot(path=f"scratch/debug_{self.account.account_id}_{i}.png")
                    await self._page.evaluate("""
                        () => {
                            const targets = Array.from(document.querySelectorAll('button, div, a, li, span, img'));
                            const eventsBtn = targets.find(el => {
                                const txt = (el.innerText || "").trim();
                                const src = (el.src || "").toLowerCase();
                                return (txt === "אירועים" || txt === "Events" || src.includes("globe"));
                            });
                            if (eventsBtn) { eventsBtn.scrollIntoView(); eventsBtn.click(); }
                        }
                    """)
                except Exception:
                    pass

            await self._page.wait_for_timeout(3000)
            if i % 7 == 0:
                logger.info(f"[{self.account.account_id}] Still waiting ({len(api_events_found)} found so far...)")

        if api_events_found:
            return api_events_found

        events = await self._extract_from_next_data()
        seen_ids_from_api = {e["go_out_id"] for e in api_events_found if "go_out_id" in e}
        events_strategy2 = await self._extract_by_clicking_rows(seen_ids_from_api)

        all_found = api_events_found + events + events_strategy2
        seen: set = set()
        final: list[dict] = []
        for e in all_found:
            eid = e.get("go_out_id")
            if eid and eid not in seen:
                final.append(e)
                seen.add(eid)
        return final

    async def _extract_from_next_data(self) -> list[dict]:
        events: list[dict] = []
        try:
            next_data = await self._page.evaluate("""
                () => {
                    const el = document.querySelector('#__NEXT_DATA__');
                    if (el) return JSON.parse(el.textContent);
                    if (window.__NEXT_DATA__) return window.__NEXT_DATA__;
                    return null;
                }
            """)
            if not next_data:
                return []
            self._walk_json_for_events(next_data, events, set())
            return events
        except Exception as exc:
            logger.warning(f"[{self.account.account_id}] __NEXT_DATA__ extraction failed: {exc}")
            return []

    def _walk_json_for_events(self, data: Any, events: list, seen: set):
        if isinstance(data, dict):
            event_id = data.get("_id") or data.get("id") or data.get("Id")
            url_val = data.get("Url") or data.get("url") or data.get("slug") or data.get("Slug")
            if event_id and url_val and str(event_id) not in seen:
                seen.add(str(event_id))
                url_str = str(url_val)
                if url_str.startswith("http"):
                    event_url = url_str
                elif url_str.startswith("/"):
                    event_url = GO_OUT_BASE + url_str
                else:
                    event_url = f"{GO_OUT_EVENT_BASE}{url_str}"
                event_info: dict = {"url": event_url, "go_out_id": str(event_id), "source": "next_data"}
                for key in ("Title", "title", "Name", "name"):
                    if key in data and data[key]:
                        event_info["name"] = str(data[key])
                        break
                for key in ("StartingDate", "startingDate", "date"):
                    if key in data and data[key]:
                        event_info["date"] = str(data[key])
                        break
                for key in ("CoverImage", "coverImage"):
                    if key in data and isinstance(data[key], dict):
                        img_url = data[key].get("Url") or data[key].get("url")
                        if img_url:
                            event_info["image"] = img_url
                        break
                events.append(event_info)
            for v in data.values():
                self._walk_json_for_events(v, events, seen)
        elif isinstance(data, list):
            for item in data:
                self._walk_json_for_events(item, events, seen)

    async def _extract_by_clicking_rows(self, already_found_ids: set) -> list[dict]:
        events: list[dict] = []
        try:
            row_data = await self._page.evaluate("""
                () => {
                    const results = [];
                    const elements = Array.from(document.querySelectorAll('*'));
                    for (const el of elements) {
                        try {
                            if (el.children.length === 0 && el.innerText && /#\d{5,6}/.test(el.innerText)) {
                                const idMatch = el.innerText.match(/#(\d{5,6})/);
                                const id = idMatch[1];
                                let row = el;
                                while (row && row.parentElement && row.offsetWidth < 200) {
                                    row = row.parentElement;
                                }
                                if (row && !results.find(r => r.id === id)) {
                                    results.push({ id: id, name: (row.innerText || "").split('\\n')[0].trim() });
                                }
                            }
                        } catch(e) {}
                    }
                    return results;
                }
            """)
            event_ids = list(dict.fromkeys(r["id"] for r in row_data))
            logger.info(f"[{self.account.account_id}] Found {len(event_ids)} event IDs via DOM: {event_ids}")
        except Exception as exc:
            logger.error(f"[{self.account.account_id}] Failed to extract event IDs: {exc}")
            return []

        for eid in event_ids:
            if eid in already_found_ids:
                continue
            try:
                event_info = await self._get_event_url_via_share(eid)
                if not event_info:
                    row_clicked = await self._page.evaluate(f"""
                        (id) => {{
                            const elements = Array.from(document.querySelectorAll('*'));
                            const el = elements.find(e => e.children.length === 0 && e.innerText && e.innerText.includes(id));
                            if (!el) return false;
                            let row = el;
                            while (row && row.parentElement && row.offsetWidth < 200) row = row.parentElement;
                            if (row) {{ row.scrollIntoView(); row.click(); return true; }}
                            return false;
                        }}
                    """, eid)
                    if row_clicked:
                        await self._page.wait_for_timeout(5000)
                        event_info = await self._get_event_url_from_dashboard(eid)
                        await self._page.goto(GO_OUT_PANEL_URL, wait_until="domcontentloaded")
                        await self._page.wait_for_timeout(3000)
                if not event_info:
                    event_info = await self._get_event_url_from_dashboard(eid)
                if event_info:
                    events.append(event_info)
                await asyncio.sleep(1)
            except Exception as exc:
                logger.warning(f"[{self.account.account_id}] Error processing #{eid}: {exc}")
                await self._page.goto(GO_OUT_PANEL_URL, wait_until="domcontentloaded")
                await self._page.wait_for_timeout(3000)
        return events

    async def _get_event_url_via_share(self, event_id: str) -> dict | None:
        try:
            clicked = await self._page.evaluate(f"""
                (id) => {{
                    const rows = Array.from(document.querySelectorAll('tr, [role="row"], .MuiTableRow-root, div'));
                    const row = rows.find(r => r.innerText.includes(id) && r.offsetWidth > 100);
                    if (row) {{
                        row.scrollIntoView();
                        const btns = Array.from(row.querySelectorAll('button, [role="button"]'));
                        const btn = btns.find(b => b.getAttribute('aria-haspopup') === 'true' || b.innerHTML.includes('svg') || b.innerText.includes('...'));
                        if (btn) {{ btn.click(); return true; }}
                    }}
                    return false;
                }}
            """, event_id)

            if not clicked:
                return None

            await self._page.wait_for_timeout(2000)

            share_clicked = await self._page.evaluate("""
                () => {
                    const elements = Array.from(document.querySelectorAll('li, [role="menuitem"], button, div, span'));
                    const share = elements.find(el => {
                        const txt = (el.innerText || "").toLowerCase();
                        return (txt.includes("שיתוף") || txt.includes("share")) && el.offsetWidth > 0;
                    });
                    if (share) { share.click(); return true; }
                    return false;
                }
            """)

            if not share_clicked:
                await self._page.mouse.click(0, 0)
                return None

            await self._page.wait_for_timeout(3000)
            live_url = await self._extract_url_from_share_dialog()
            await self._page.keyboard.press("Escape")
            await self._page.wait_for_timeout(500)

            if live_url:
                if live_url.startswith("/"):
                    live_url = GO_OUT_BASE + live_url
                logger.info(f"[{self.account.account_id}] Got URL via share for #{event_id}: {live_url}")
                return {"url": live_url, "go_out_id": event_id, "source": "share_button"}
        except Exception as exc:
            logger.warning(f"[{self.account.account_id}] Share flow failed for #{event_id}: {exc}")
        return None

    async def _extract_url_from_share_dialog(self) -> str | None:
        return await self._page.evaluate("""
            () => {
                const results = [];
                Array.from(document.querySelectorAll('input')).forEach(i => {
                    if (i.value.includes('go-out.co')) results.push(i.value);
                });
                for (const a of Array.from(document.querySelectorAll('a'))) {
                    const href = a.href || "";
                    if (href.includes('wa.me') || href.includes('whatsapp.com')) {
                        const match = href.match(/(https?:\\/\\/(?:www\\.)?go-out\\.co\\/(?:event|i)\\/[^&\\s]+)/);
                        if (match) results.push(match[1]);
                    }
                    if ((href.includes('go-out.co/event/') || href.includes('go-out.co/i/')) &&
                        !href.includes('eventmanagement') && !href.includes('businesspage')) {
                        results.push(href);
                    }
                }
                const bodyText = document.body.innerText;
                const matches = bodyText.match(/(https?:\\/\\/(?:www\\.)?go-out\\.co\\/(?:event|i)\\/[^\\s\\n\\r]+)/g);
                if (matches) results.push(...matches);
                if (results.length === 0) return null;
                const slugs = results.filter(r => {
                    const parts = r.split('/');
                    const last = parts[parts.length - 1].split('?')[0];
                    return /[^0-9]/.test(last);
                });
                return slugs.length > 0 ? slugs[0] : results[0];
            }
        """)

    async def _get_event_url_from_dashboard(self, event_id: str) -> dict | None:
        possible_urls = [
            f"https://www.go-out.co/businesspage/event/{event_id}",
            f"https://www.go-out.co/eventmanagement?eventId={event_id}",
            f"https://www.go-out.co/businesspage/{event_id}",
        ]
        dashboard_loaded = False
        for dashboard_url in possible_urls:
            try:
                await self._page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30000)
                await self._page.wait_for_timeout(10000)
                page_text = await self._page.evaluate("document.body.innerText")
                if any(k in page_text for k in ["Manage", "ניהול", "Share", "שיתוף"]):
                    dashboard_loaded = True
                    break
            except Exception as e:
                logger.warning(f"[{self.account.account_id}] Failed to load dashboard {dashboard_url}: {e}")

        if not dashboard_loaded:
            return None

        live_url = None
        try:
            next_data_str = await self._page.evaluate("""
                () => {
                    const el = document.querySelector('#__NEXT_DATA__');
                    return el ? el.textContent : (window.__NEXT_DATA__ ? JSON.stringify(window.__NEXT_DATA__) : null);
                }
            """)
            if next_data_str:
                match = re.search(r'"slug":"([^"]+)"', next_data_str) or re.search(r'"publicUrl":"([^"]+)"', next_data_str)
                if match:
                    live_url = match.group(1)

            if not live_url:
                share_clicked = await self._page.evaluate("""
                    () => {
                        const elements = Array.from(document.querySelectorAll('button, [role="button"], a, span, div'));
                        const share = elements.find(el => {
                            const txt = (el.innerText || "").trim();
                            return (txt === "שיתוף" || txt === "Share") && el.offsetWidth > 0;
                        });
                        if (share) { share.click(); return true; }
                        return false;
                    }
                """)
                if share_clicked:
                    await self._page.wait_for_timeout(3000)
                    live_url = await self._extract_url_from_share_dialog()
                    await self._page.keyboard.press("Escape")
        except Exception as exc:
            logger.warning(f"[{self.account.account_id}] Dashboard extraction failed: {exc}")

        if not live_url:
            return None
        if live_url.startswith("/"):
            live_url = GO_OUT_BASE + live_url
        logger.info(f"[{self.account.account_id}] Got URL for #{event_id}: {live_url}")
        return {"url": live_url, "go_out_id": event_id, "source": "dashboard_extraction"}
