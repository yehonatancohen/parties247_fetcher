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
from datetime import datetime, timezone, timedelta
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
    # Sales data scraping
    # ------------------------------------------------------------------

    async def scrape_sales_data(self) -> list[dict]:
        """
        Scrape sales data from the business panel for every event (active and inactive).

        Returns a list of dicts:
            {go_out_id, event_name, confirmed, pending, ticket_price, event_revenue}

        event_revenue = הכנסות לאירוע (total gross revenue shown on the panel).
        For account2 the caller should use event_revenue × 6% directly.
        ticket_price is the lowest active price, kept as a fallback.
        """
        api_sales: list[dict] = []

        def _extract_sales_from_obj(obj):
            if not isinstance(obj, dict):
                return
            # EventSerial is the numeric "#XXXXX" panel ID; prefer it over _id (MongoDB ObjectId)
            eid = obj.get("EventSerial") or obj.get("eventSerial")
            if not eid:
                return
            eid = str(eid)

            # statistics sub-object: {"Accepted": N, "Pending": N, "Today": N, "Rejected": N}
            stats = obj.get("statistics") or obj.get("Statistics") or {}
            if not isinstance(stats, dict):
                stats = {}

            confirmed = None
            for key in ("ConfirmedTickets", "confirmedTickets", "Approved", "approved",
                        "ApprovedCount", "approvedCount", "ConfirmedCount", "confirmedCount",
                        "TotalConfirmed", "totalConfirmed", "SoldTickets", "soldTickets"):
                if obj.get(key) is not None:
                    try:
                        confirmed = int(obj[key])
                    except (TypeError, ValueError):
                        pass
                    break
            if confirmed is None and stats.get("Accepted") is not None:
                try:
                    confirmed = int(stats["Accepted"])
                except (TypeError, ValueError):
                    pass

            pending = None
            for key in ("PendingTickets", "pendingTickets", "PendingCount", "pendingCount",
                        "TotalPending", "totalPending", "WaitingTickets", "waitingTickets"):
                if obj.get(key) is not None:
                    try:
                        pending = int(obj[key])
                    except (TypeError, ValueError):
                        pass
                    break
            if pending is None and stats.get("Pending") is not None:
                try:
                    pending = int(stats["Pending"])
                except (TypeError, ValueError):
                    pass

            # StartingDate is ISO: "2026-05-08T23:30:00.000" → "2026-05-08"
            event_date = None
            raw_date = obj.get("StartingDate") or obj.get("startingDate")
            if isinstance(raw_date, str) and len(raw_date) >= 10:
                event_date = raw_date[:10]

            # Url is Go-Out's numeric public event identifier (e.g. "1780565778053")
            event_url_id = obj.get("Url") or obj.get("url") or obj.get("EventUrl") or ""
            if event_url_id:
                event_url_id = str(event_url_id)

            # הכנסות לאירוע — total event revenue (may not be accessible for salesperson roles)
            event_revenue = None
            for key in ("TotalRevenue", "totalRevenue", "EventRevenue", "eventRevenue",
                        "Income", "income", "Incomes", "Revenue", "revenue",
                        "TotalIncome", "totalIncome", "GrossRevenue", "grossRevenue"):
                if obj.get(key) is not None:
                    try:
                        event_revenue = float(obj[key])
                    except (TypeError, ValueError):
                        pass
                    break

            if confirmed is None and pending is None and event_revenue is None and event_date is None:
                return

            name = obj.get("Title") or obj.get("Name") or obj.get("name") or obj.get("title")

            # Lowest active ticket price
            price = None
            for ticket in (obj.get("Tickets") or obj.get("tickets") or []):
                if isinstance(ticket, dict) and ticket.get("Active", True):
                    t_price = ticket.get("Price") or ticket.get("price")
                    if t_price is not None:
                        try:
                            t_price = float(t_price)
                            if price is None or t_price < price:
                                price = t_price
                        except (TypeError, ValueError):
                            pass
            if price is None:
                raw = obj.get("Price") or obj.get("price") or obj.get("TicketPrice") or obj.get("ticketPrice")
                if raw is not None:
                    try:
                        price = float(raw)
                    except (TypeError, ValueError):
                        pass

            existing = next((s for s in api_sales if s["go_out_id"] == eid), None)
            if existing:
                if confirmed is not None:
                    existing["confirmed"] = confirmed
                if pending is not None:
                    existing["pending"] = pending
                if price is not None and existing.get("ticket_price") is None:
                    existing["ticket_price"] = price
                if event_revenue is not None:
                    existing["event_revenue"] = event_revenue
                if event_date and not existing.get("event_date"):
                    existing["event_date"] = event_date
                if event_url_id and not existing.get("event_url_id"):
                    existing["event_url_id"] = event_url_id
            else:
                api_sales.append({
                    "go_out_id":     eid,
                    "event_name":    str(name) if name else None,
                    "confirmed":     confirmed if confirmed is not None else 0,
                    "pending":       pending if pending is not None else 0,
                    "ticket_price":  price,
                    "event_revenue": event_revenue,
                    "event_date":    event_date,
                    "event_url_id":  event_url_id,
                })

        def _walk_for_sales(data, depth=0):
            if depth > 12:
                return
            if isinstance(data, dict):
                _extract_sales_from_obj(data)
                for v in data.values():
                    if isinstance(v, (dict, list)):
                        _walk_for_sales(v, depth + 1)
            elif isinstance(data, list):
                for item in data:
                    _walk_for_sales(item, depth)

        async def _intercept_sales(response):
            try:
                ct = response.headers.get("content-type", "").lower()
                if not (response.ok and ("json" in ct or "text/plain" in ct)):
                    return
                url = response.url
                if any(x in url for x in ("userway", "facebook", "google", "tiktok")):
                    return
                try:
                    data = await response.json()
                except Exception:
                    return
                _walk_for_sales(data)
            except Exception:
                pass

        self._page.on("response", _intercept_sales)

        # Rewrite myEvents API requests: bump limit and remove activeEvents:true filter
        async def route_my_events_sales(route):
            url = route.request.url
            new_url = re.sub(r'(limit=)\d+', r'\g<1>500', url)
            if not re.search(r'[?&]limit=', new_url):
                new_url += ('&' if '?' in new_url else '?') + 'limit=500'
            # Remove activeEvents:true so ended events are included
            # The colon may or may not be URL-encoded (%3A vs :)
            new_url = re.sub(r'%22activeEvents%22(%3A|:)true', r'%22activeEvents%22\1false', new_url)
            new_url = re.sub(r'"activeEvents"(:)true', r'"activeEvents"\1false', new_url)
            if new_url != url:
                logger.info(f"[{self.account.account_id}] Rewrote myEvents → {new_url}")
            await route.continue_(url=new_url)

        await self._page.route("**/myEvents**", route_my_events_sales)

        await self._page.goto(GO_OUT_PANEL_URL, wait_until="domcontentloaded", timeout=30000)
        await self._page.wait_for_timeout(3000)

        # Click the Events tab
        events_tab_selectors = [
            'div[role="button"]:has(img[src*="MyEvents/globe"])',
            'button:has(img[src*="MyEvents/globe"])',
            'a[href="/businesspage"]',
            'img[src*="MyEvents/globe"]',
            'text="אירועים"',
            'text="Events"',
        ]
        for sel in events_tab_selectors:
            try:
                tab = await self._page.query_selector(sel)
                if tab:
                    await tab.click(force=True)
                    await self._page.wait_for_timeout(2000)
                    break
            except Exception:
                pass

        await self._dismiss_cookie_dialog()

        # Wait for event-ID chips to appear
        try:
            await self._page.wait_for_function(
                "() => document.body.innerText.match(/#\\d{4,8}/)",
                timeout=15000,
            )
        except Exception:
            pass

        # Try to switch the panel filter to show ALL events (active + ended)
        try:
            await self._page.evaluate("""
                () => {
                    const triggers = Array.from(document.querySelectorAll('button, div[role="button"], select'));
                    const filter = triggers.find(el => {
                        const t = (el.innerText || el.value || "").toLowerCase();
                        return t.includes("active & pending") || t.includes("active and pending") ||
                               t.includes("categorize") || t.includes("filter") ||
                               t === "active" || t.includes("סינון") || t.includes("פעיל");
                    });
                    if (filter) { filter.scrollIntoView(); filter.click(); }
                }
            """)
            await self._page.wait_for_timeout(1000)
            await self._page.evaluate("""
                () => {
                    const opts = Array.from(document.querySelectorAll(
                        'li, [role="option"], [role="menuitem"], option, div, button'
                    ));
                    const all = opts.find(el => {
                        const t = (el.innerText || el.value || "").trim().toLowerCase();
                        return t === "all" || t === "all events" || t === "הכל" ||
                               t.includes("ended") || t.includes("past") || t.includes("הסתיים");
                    });
                    if (all) { all.scrollIntoView(); all.click(); }
                }
            """)
            await self._page.wait_for_timeout(2000)
        except Exception:
            pass

        # Stabilised scroll loop — stop when visible DOM event count is stable for 4 ticks
        last_dom_count = 0
        stable_ticks = 0
        for i in range(60):
            await self._page.wait_for_timeout(2000)
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            # Count unique event-ID chips visible in the DOM (faster signal than API intercept)
            current_dom = await self._page.evaluate(r"""
                () => {
                    const chips = Array.from(document.querySelectorAll('*')).filter(el => {
                        const t = (el.innerText || '').trim();
                        return /^#\d{4,8}$/.test(t) && el.offsetParent !== null;
                    });
                    return new Set(chips.map(c => c.innerText.trim())).size;
                }
            """)
            if current_dom > last_dom_count:
                stable_ticks = 0
                last_dom_count = current_dom
                logger.info(f"[{self.account.account_id}] {current_dom} unique events visible, scrolling...")
            else:
                stable_ticks += 1
                if stable_ticks >= 4 and i >= 3:
                    logger.info(f"[{self.account.account_id}] DOM stable at {current_dom} events after {i+1} scrolls.")
                    break

        # DOM scrape — catches events not returned by the API
        dom_sales = await self._scrape_sales_from_dom()
        logger.info(f"[{self.account.account_id}] DOM found {len(dom_sales)} events")

        # Merge DOM into API results
        api_by_id: dict[str, dict] = {s["go_out_id"]: s for s in api_sales}
        for dom_row in dom_sales:
            eid = dom_row.get("go_out_id")
            if not eid:
                continue
            if eid in api_by_id:
                api_row = api_by_id[eid]
                for field in ("ticket_price", "event_revenue", "confirmed", "pending", "event_date"):
                    if api_row.get(field) is None and dom_row.get(field) is not None:
                        api_row[field] = dom_row[field]
                api_row.setdefault("finance_url", dom_row.get("finance_url"))
                api_row.setdefault("status", dom_row.get("status"))
            else:
                api_sales.append(dom_row)
                api_by_id[eid] = dom_row

        if not api_sales and dom_sales:
            api_sales = dom_sales

        # Keep events from the last 30 days backwards AND all future events.
        # Lower bound drops stale completed events; no upper bound so upcoming
        # parties with pre-sold tickets are always included.
        _now = datetime.now(timezone.utc)
        _d_start = (_now - timedelta(days=30)).date()
        _range_s = str(_d_start)
        before = len(api_sales)
        api_sales = [
            item for item in api_sales
            if item.get("event_date") and item["event_date"] >= _range_s
        ]
        logger.info(
            f"[{self.account.account_id}] Date filter {_range_s}..future: "
            f"{len(api_sales)} of {before} events"
        )

        # Fetch הכנסות לאירוע — discover the finance API via one page visit, then batch-fetch
        await self._fetch_finance_revenue_parallel(api_sales)

        logger.info(f"[{self.account.account_id}] Sales data: {len(api_sales)} events")
        return api_sales

    async def _fetch_ticket_price_via_api(self, event_url_id: str, go_out_id: str) -> float | None:
        """
        Fetch the public event details to extract the lowest ticket price.
        Uses the event's Url field (numeric string like "1780565778053") as identifier.
        Returns the lowest ticket price, or None if not found.
        """
        if not event_url_id:
            return None
        api_base = "https://api.fe.prod.go-out.co"
        url_patterns = [
            f"{api_base}/events/{event_url_id}",
            f"{api_base}/events/getEvent/{event_url_id}",
            f"{api_base}/events/event/{event_url_id}",
            f"{api_base}/events/{go_out_id}",
        ]
        result = await self._page.evaluate(
            r"""
            async (urls) => {
                for (const url of urls) {
                    try {
                        const r = await fetch(url, {credentials: 'include'});
                        if (!r.ok) continue;
                        const ct = r.headers.get('content-type') || '';
                        if (!ct.includes('json')) continue;
                        const data = await r.json();
                        const j = JSON.stringify(data);
                        // Look for ticket price
                        const ticketKeys = ['"Price":', '"price":', '"TicketPrice":', '"ticketPrice":', '"amount":'];
                        for (const k of ticketKeys) {
                            let idx = j.indexOf(k);
                            if (idx >= 0) {
                                const rest = j.slice(idx + k.length).trimStart();
                                const m = rest.match(/^([\d.]+)/);
                                if (m && parseFloat(m[1]) > 0) {
                                    return {url, price: parseFloat(m[1]), raw: j.slice(0, 200)};
                                }
                            }
                        }
                        return {url, price: null, raw: j.slice(0, 200)};
                    } catch(e) {}
                }
                return null;
            }
            """,
            url_patterns,
        )
        if result and result.get("price"):
            logger.info(
                f"[{self.account.account_id}] #{go_out_id} ticket_price={result['price']} from {result['url']}"
            )
            return float(result["price"])
        if result:
            logger.debug(
                f"[{self.account.account_id}] #{go_out_id}: API {result['url']} no price. Raw: {result.get('raw','')!r}"
            )
        return None

    async def _fetch_finance_revenue_parallel(self, sales: list[dict]):
        """
        Try to fill in ticket_price for events missing it, then derive event_revenue.
        Revenue = confirmed_tickets × ticket_price (best available proxy for salesperson accounts).
        """
        needs_price = [item for item in sales
                       if item.get("ticket_price") is None
                       and item.get("event_revenue") is None
                       and item.get("confirmed", 0) > 0]
        if not needs_price:
            # Derive event_revenue from already-known ticket_price
            for item in sales:
                if item.get("event_revenue") is None and item.get("ticket_price") and item.get("confirmed", 0) > 0:
                    item["event_revenue"] = item["confirmed"] * item["ticket_price"]
            return

        logger.info(
            f"[{self.account.account_id}] Fetching ticket prices for {len(needs_price)} events..."
        )
        for item in needs_price:
            gid = item["go_out_id"]
            url_id = item.get("event_url_id", "")
            price = await self._fetch_ticket_price_via_api(url_id, gid)
            if price is not None:
                item["ticket_price"] = price

        # Derive event_revenue from ticket_price × confirmed
        for item in sales:
            if item.get("event_revenue") is None and item.get("ticket_price") and item.get("confirmed", 0) > 0:
                item["event_revenue"] = item["confirmed"] * item["ticket_price"]

        found = sum(1 for item in sales if item.get("event_revenue") is not None)
        logger.info(
            f"[{self.account.account_id}] Revenue derived for {found}/{len(sales)} events"
        )

    async def _scrape_sales_from_dom(self) -> list[dict]:
        """
        Parse the Go-Out organizer panel event list.

        The panel shows each event row with a "Ticket sales" cell containing:
            Today X   Accepted X   Pending X
        and a "View Finance" link.  Accepted = מאושרים, Pending = ממתינים.
        """
        # Dismiss cookie banner first
        await self._dismiss_cookie_dialog()
        await self._page.wait_for_timeout(1000)

        try:
            result = await self._page.evaluate(r"""
                () => {
                    const parseNum = (text) => {
                        if (!text) return null;
                        const clean = text.replace(/[,\s₪]/g, "");
                        const n = parseFloat(clean);
                        return isNaN(n) ? null : n;
                    };

                    // Each event row is a direct child container with a visible event name
                    // The panel uses divs, not a <table>, so we look for rows that
                    // contain an event ID chip like "#44650"
                    const allEls = Array.from(document.querySelectorAll('*'));

                    // Find elements that look like the event ID chip (#XXXXX)
                    const idChips = allEls.filter(el => {
                        const t = (el.innerText || "").trim();
                        return /^#\d{4,8}$/.test(t) && el.offsetParent !== null;
                    });

                    const sales = [];

                    for (const chip of idChips) {
                        const goOutId = chip.innerText.trim().replace('#', '');

                        // Walk up to the row container (wide enough to hold all columns)
                        let row = chip;
                        while (row && row.parentElement && row.offsetWidth < 600) {
                            row = row.parentElement;
                        }
                        if (!row) continue;

                        const rowText = row.innerText || "";

                        // Extract event name: the largest text block in the row
                        // (Name is the biggest element before the ID chip)
                        let eventName = "";
                        const nameEl = Array.from(row.querySelectorAll('*')).find(el => {
                            if (el.children.length > 0) return false;
                            const t = (el.innerText || "").trim();
                            return t.length > 3 && !/^#\d/.test(t) &&
                                   !["Today","Accepted","Pending","Active","Ended",
                                     "Team member","View Finance","Refresh page"].includes(t);
                        });
                        if (nameEl) eventName = nameEl.innerText.trim();

                        // Parse "Accepted X" from the ticket-sales cell
                        const acceptedMatch = rowText.match(/Accepted\s+(\d+)/i);
                        const pendingMatch  = rowText.match(/Pending\s+(\d+)/i);
                        const todayMatch    = rowText.match(/Today\s+(\d+)/i);

                        const accepted = acceptedMatch ? parseInt(acceptedMatch[1], 10) : 0;
                        const pending  = pendingMatch  ? parseInt(pendingMatch[1],  10) : 0;
                        const today    = todayMatch    ? parseInt(todayMatch[1],    10) : 0;

                        // Status (Active / Ended / etc.)
                        const statusMatch = rowText.match(/\b(Active|Ended|Draft|Cancelled)\b/i);
                        const status = statusMatch ? statusMatch[1] : "";

                        // "View Finance" link href — may contain event ID or finance path
                        const financeLink = Array.from(row.querySelectorAll('a')).find(a =>
                            (a.innerText || "").includes("Finance") ||
                            (a.href || "").includes("finance")
                        );
                        const financeUrl = financeLink ? financeLink.href : null;

                        // Extract event date from row text (YYYY-MM-DD)
                        // Panel shows dates as "DD.MM.YYYY", "DD.MM.YY", "YYYY-MM-DD",
                        // or just "DD.MM" (no year — infer from current date).
                        let eventDate = null;
                        const nowJs = new Date();
                        const curYr = nowJs.getFullYear();
                        const curMo = nowJs.getMonth() + 1;
                        const isoM  = rowText.match(/\b(\d{4})-(\d{2})-(\d{2})\b/);
                        const dotM  = rowText.match(/\b(\d{1,2})\.(\d{1,2})\.(2\d{3})\b/);
                        const shrtM = rowText.match(/\b(\d{1,2})\.(\d{1,2})\.(2\d)\b/);
                        const noYrM = rowText.match(/\b(\d{1,2})\.(\d{1,2})\b(?!\.)/);
                        if (isoM) {
                            eventDate = isoM[0];
                        } else if (dotM) {
                            eventDate = `${dotM[3]}-${dotM[2].padStart(2,'0')}-${dotM[1].padStart(2,'0')}`;
                        } else if (shrtM) {
                            const yr = 2000 + parseInt(shrtM[3]);
                            eventDate = `${yr}-${shrtM[2].padStart(2,'0')}-${shrtM[1].padStart(2,'0')}`;
                        } else if (noYrM) {
                            const mo = parseInt(noYrM[2]);
                            const dy = parseInt(noYrM[1]);
                            if (mo >= 1 && mo <= 12 && dy >= 1 && dy <= 31) {
                                // month <= current month → this year, else last year
                                const yr = mo <= curMo ? curYr : curYr - 1;
                                eventDate = `${yr}-${noYrM[2].padStart(2,'0')}-${noYrM[1].padStart(2,'0')}`;
                            }
                        }

                        sales.push({
                            go_out_id:     goOutId,
                            event_name:    eventName,
                            confirmed:     accepted,
                            today:         today,
                            pending:       pending,
                            status:        status,
                            finance_url:   financeUrl,
                            ticket_price:  null,
                            event_revenue: null,
                            event_date:    eventDate,
                        });
                    }

                    return sales;
                }
            """)
            return result or []
        except Exception as exc:
            logger.warning(f"[{self.account.account_id}] DOM sales scrape failed: {exc}")
            return []

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

        # Safety net: drop anything with a clearly-past date. Events with no
        # date yet (resolved later via full scrape) are kept.
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        before = len(events)
        events = [
            e for e in events
            if not e.get("date") or str(e["date"])[:10] >= today_str
        ]
        if len(events) != before:
            logger.info(
                f"[{self.account.account_id}] Dropped {before - len(events)} past-dated event(s) from discovery."
            )

        logger.info(f"[{self.account.account_id}] Discovery complete. Total: {len(events)}")
        return events

    async def _scrape_organizer_panel(self, api_events_found: list) -> list[dict]:
        logger.info(f"[{self.account.account_id}] Loading organizer panel...")

        def _parse_events(data):
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
                            date_raw = obj.get("StartingDate") or obj.get("startingDate") or ""
                            api_events_found.append({
                                "url": full_url,
                                "go_out_id": str(eid),
                                "name": obj.get("Title") or obj.get("Name"),
                                "date": date_raw[:10] if date_raw else None,
                                "location": obj.get("Adress") or obj.get("EnglishAddress"),
                                "source": "api_intercept",
                            })
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            find_slugs(v)
                elif isinstance(obj, list):
                    for item in obj:
                        find_slugs(item)
            find_slugs(data)

        # Intercept myEvents requests: bump limit way up and drop the
        # activeEvents:true filter. Confirmed via live run that
        # activeEvents:true is go-out's own "tickets on sale" flag, not a
        # date-window filter — it silently excludes valid future events
        # that aren't flagged "active" yet (this was the actual cause of
        # account1 events going missing: 6 vs 140 for account2 on the same
        # call). But activeEvents:false alone isn't enough either — results
        # come back oldest-first, so with a small limit the response is
        # entirely past events and future ones never make it into the page
        # at all (confirmed: limit=200 → 0 future events for account1).
        # Use a much larger limit so future events actually make it into
        # the response; discover_events() then applies a hard past-date
        # filter to drop the old ones we don't want.
        async def route_my_events(route):
            url = route.request.url
            new_url = re.sub(r'limit=\d+', 'limit=5000', url)
            if not re.search(r'[?&]limit=', new_url):
                new_url += ('&' if '?' in new_url else '?') + 'limit=5000'
            new_url = re.sub(r'%22activeEvents%22(%3A|:)true', r'%22activeEvents%22\1false', new_url)
            new_url = re.sub(r'"activeEvents"(:)true', r'"activeEvents"\1false', new_url)
            if new_url != url:
                logger.info(f"[{self.account.account_id}] Rewrote myEvents → {new_url}")
            await route.continue_(url=new_url)

        await self._page.route("**/myEvents**", route_my_events)

        async def intercept_response(response):
            try:
                ct = response.headers.get("content-type", "").lower()
                if not (response.ok and ("json" in ct or "text/plain" in ct or "application/octet-stream" in ct)):
                    return
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
                _parse_events(data)
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

        # Ensure the panel filter is set to "Active & Pending" (future/on-sale
        # events only — NOT "All"/"Ended", we don't want old events showing up).
        try:
            await self._page.evaluate("""
                () => {
                    const triggers = Array.from(document.querySelectorAll('button, div[role="button"], select'));
                    const filter = triggers.find(el => {
                        const t = (el.innerText || el.value || "").toLowerCase();
                        return t.includes("categorize") || t.includes("filter") ||
                               t.includes("סינון") || t.includes("פעיל");
                    });
                    if (filter) { filter.scrollIntoView(); filter.click(); }
                }
            """)
            await self._page.wait_for_timeout(1000)
            await self._page.evaluate("""
                () => {
                    const opts = Array.from(document.querySelectorAll(
                        'li, [role="option"], [role="menuitem"], option, div, button'
                    ));
                    const activePending = opts.find(el => {
                        const t = (el.innerText || el.value || "").trim().toLowerCase();
                        return t.includes("active & pending") || t.includes("active and pending") ||
                               t === "active";
                    });
                    if (activePending) { activePending.scrollIntoView(); activePending.click(); }
                }
            """)
            await self._page.wait_for_timeout(2000)
        except Exception:
            pass

        # Fallback scroll strategy
        last_count = 0
        stable_ticks = 0
        for i in range(50):
            await self._page.wait_for_timeout(3000)
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            current_count = len(api_events_found)

            if current_count > last_count:
                stable_ticks = 0
                last_count = current_count
                logger.info(f"[{self.account.account_id}] {current_count} events so far, still loading...")
            elif current_count >= 1:
                stable_ticks += 1
                if stable_ticks >= 5:
                    logger.info(f"[{self.account.account_id}] Stable at {current_count} events. Done.")
                    return api_events_found
            else:
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
                if i % 7 == 0:
                    logger.info(f"[{self.account.account_id}] Still waiting (0 found so far...)")

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
                            if (el.children.length === 0 && el.innerText && /#\\d{5,6}/.test(el.innerText)) {
                                const idMatch = el.innerText.match(/#(\\d{5,6})/);
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
