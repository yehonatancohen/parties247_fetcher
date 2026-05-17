"""
Telegram Manager Bot for the Go-Out scraper service.

- 2FA code relay for Go-Out login
- New party approval notifications with inline buttons
- Carousel assignment flow
- Approve/reject calls the backend HTTP API
"""

import json
import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

import requests as http_requests
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config

logger = logging.getLogger(__name__)

_tfa_requests: dict[str, asyncio.Event] = {}
_tfa_codes: dict[str, str | None] = {}
_edit_sessions: dict[int, str] = {}
_carousel_selections: dict[str, list[str]] = {}


class TelegramManager:
    def __init__(self, token: str, manager_chat_id: str, db=None):
        self.token = token
        self.manager_chat_id = int(manager_chat_id)
        self._db = db
        self._app: Application | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self.on_scrape_requested: Callable | None = None

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    @property
    def _pending_collection(self):
        return self._db.goout_pending if self._db is not None else None

    @property
    def _sessions_collection(self):
        return self._db.goout_sessions if self._db is not None else None

    @property
    def _carousels_collection(self):
        return self._db.carousels if self._db is not None else None

    @property
    def _parties_collection(self):
        return self._db.parties if self._db is not None else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run_polling(self):
        """Block the calling thread running the bot (use in main thread)."""
        self._started = True
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start_polling())

    def start_in_background(self):
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info("Telegram bot started in background thread.")

    def _run_forever(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start_polling())

    async def _start_polling(self):
        builder = Application.builder().token(self.token)
        self._app = builder.build()

        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("scrape", self._cmd_scrape))
        self._app.add_handler(CommandHandler("pending", self._cmd_pending))
        self._app.add_handler(CommandHandler("approve_all", self._cmd_approve_all))
        self._app.add_handler(CommandHandler("sessions", self._cmd_sessions))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("cancel", self._cmd_cancel))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))

        await self._app.initialize()
        await self._app.start()

        try:
            await self._app.bot.delete_webhook(drop_pending_updates=True)
        except Exception as exc:
            logger.warning(f"delete_webhook failed (non-fatal): {exc}")

        await self._app.updater.start_polling(drop_pending_updates=True)

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _is_manager(self, update: Update) -> bool:
        return update.effective_chat and update.effective_chat.id == self.manager_chat_id

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_manager(update):
            await update.message.reply_text("⛔ Unauthorized.")
            return
        await update.message.reply_text(
            "🎉 *Parties 24/7 Manager Bot*\n\nUse /help to see available commands.",
            parse_mode="Markdown",
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_manager(update):
            return
        await update.message.reply_text(
            "📋 *Available Commands*\n\n"
            "/status — Scraper status & pending count\n"
            "/scrape — Trigger immediate scrape\n"
            "/pending — List pending parties\n"
            "/approve\\_all — Approve all pending\n"
            "/sessions — Go-Out session status\n"
            "/cancel — Cancel current edit session\n"
            "/help — Show this message",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_manager(update):
            return
        pending_count = 0
        sessions_info = []
        try:
            coll = self._pending_collection
            if coll is not None:
                pending_count = coll.count_documents({"status": "pending"})
        except Exception:
            pass
        try:
            sess_coll = self._sessions_collection
            if sess_coll is not None:
                for doc in sess_coll.find({}):
                    sessions_info.append(
                        f"  • {doc.get('account_id', '?')}: "
                        f"{'✅ Valid' if doc.get('session_valid') else '❌ Expired'} "
                        f"(last: {doc.get('last_checked', 'never')})"
                    )
        except Exception:
            pass
        sessions_text = "\n".join(sessions_info) if sessions_info else "  No sessions found."
        await update.message.reply_text(
            f"📊 *Scraper Status*\n\nPending: {pending_count}\nSessions:\n{sessions_text}",
            parse_mode="Markdown",
        )

    async def _cmd_scrape(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_manager(update):
            return
        await update.message.reply_text("🔄 Triggering manual scan...")
        if self.on_scrape_requested:
            def _run():
                try:
                    self.on_scrape_requested()
                except Exception as exc:
                    self.send_message_sync(f"❌ Scrape failed: {exc}")
            threading.Thread(target=_run, daemon=True).start()

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_manager(update):
            return
        coll = self._pending_collection
        if coll is None:
            await update.message.reply_text("⚠️ Database unavailable.")
            return
        try:
            docs = list(coll.find({"status": "pending"}).limit(20))
        except Exception as exc:
            await update.message.reply_text(f"❌ Error: {exc}")
            return
        if not docs:
            await update.message.reply_text("✅ No pending parties!")
            return
        for doc in docs:
            await self._send_pending_party_message(doc)

    async def _cmd_approve_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_manager(update):
            return
        coll = self._pending_collection
        if coll is None:
            await update.message.reply_text("⚠️ Database unavailable.")
            return
        try:
            docs = list(coll.find({"status": "pending"}))
        except Exception:
            docs = []
        if not docs:
            await update.message.reply_text("✅ No pending parties to approve.")
            return
        approved = 0
        for doc in docs:
            result = self._call_backend_approve(str(doc["_id"]))
            if result:
                approved += 1
        await update.message.reply_text(f"✅ Approved {approved}/{len(docs)} parties.")

    async def _cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_manager(update):
            return
        sess_coll = self._sessions_collection
        if sess_coll is None:
            await update.message.reply_text("⚠️ Database unavailable.")
            return
        try:
            docs = list(sess_coll.find({}))
        except Exception as exc:
            await update.message.reply_text(f"❌ Error: {exc}")
            return
        if not docs:
            await update.message.reply_text("No sessions stored yet.")
            return
        lines = []
        for doc in docs:
            valid = "✅ Valid" if doc.get("session_valid") else "❌ Expired"
            lines.append(
                f"*{doc.get('account_id', '?')}*\n"
                f"  Email: {doc.get('email', '?')}\n"
                f"  Status: {valid}\n"
                f"  Last login: {doc.get('last_login', 'never')}\n"
                f"  Last checked: {doc.get('last_checked', 'never')}"
            )
        await update.message.reply_text(
            "🔑 *Go-Out Sessions*\n\n" + "\n\n".join(lines), parse_mode="Markdown"
        )

    async def _cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_manager(update):
            return
        chat_id = update.effective_chat.id
        if chat_id in _edit_sessions:
            _edit_sessions.pop(chat_id)
            await update.message.reply_text("✅ Edit cancelled.")
        else:
            await update.message.reply_text("Nothing to cancel.")

    # ------------------------------------------------------------------
    # Callback query handler
    # ------------------------------------------------------------------

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return
        await query.answer()
        if not self._is_manager(update):
            await query.edit_message_text("⛔ Unauthorized.")
            return

        data = query.data or ""

        if data.startswith("approve:"):
            pending_id = data.split(":", 1)[1]
            party_data = self._get_pending_party_data(pending_id)
            result = self._call_backend_approve(pending_id, party_data)
            if result:
                party_db_id = result.get("party_db_id")
                msg_text = (query.message.caption or query.message.text or "")
                suffix = "\n\n✅ *APPROVED*"
                try:
                    if query.message.photo:
                        await query.edit_message_caption(msg_text + suffix, parse_mode="Markdown")
                    else:
                        await query.edit_message_text(msg_text + suffix, parse_mode="Markdown")
                except Exception:
                    pass
                await self._send_carousel_selection(pending_id, party_data, party_db_id)
            else:
                try:
                    await query.edit_message_text(
                        (query.message.text or "") + "\n\n❌ *Failed to approve*",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

        elif data.startswith("reject:"):
            pending_id = data.split(":", 1)[1]
            success = self._call_backend_reject(pending_id)
            msg_text = (query.message.caption or query.message.text or "")
            suffix = "\n\n❌ *REJECTED*" if success else "\n\n⚠️ *Failed to reject*"
            try:
                if query.message.photo:
                    await query.edit_message_caption(msg_text + suffix, parse_mode="Markdown")
                else:
                    await query.edit_message_text(msg_text + suffix, parse_mode="Markdown")
            except Exception:
                pass

        elif data.startswith("edit:"):
            pending_id = data.split(":", 1)[1]
            _edit_sessions[update.effective_chat.id] = pending_id
            msg_text = (query.message.caption or query.message.text or "")
            edit_prompt = (
                msg_text + "\n\n✏️ *EDIT MODE*\n"
                "Send me the fields to change as JSON:\n"
                '`{"name": "...", "location": {...}, "date": "...", "tags": [...], '
                '"musicType": "...", "eventType": "...", "region": "...", "age": "...", '
                '"ticketPrice": 0, "imageUrl": "...", "referralCode": "..."}`'
            )
            try:
                if query.message.photo:
                    await query.edit_message_caption(edit_prompt, parse_mode="Markdown")
                else:
                    await query.edit_message_text(edit_prompt, parse_mode="Markdown")
            except Exception:
                pass

        elif data.startswith("2fa:"):
            parts = data.split(":", 2)
            account_id = parts[1] if len(parts) > 1 else ""
            action = parts[2] if len(parts) > 2 else ""
            if action == "ready":
                await query.edit_message_text(
                    f"🔐 Great! Click login for *{account_id}* — reply with the 6-digit code when it arrives.",
                    parse_mode="Markdown",
                )
                key = f"2fa_avail_{account_id}"
                if key in _tfa_requests:
                    _tfa_codes[key] = "ready"
                    _tfa_requests[key].set()
            elif action == "later":
                await query.edit_message_text(
                    f"⏳ OK, I'll try again later for *{account_id}*.", parse_mode="Markdown"
                )
                key = f"2fa_avail_{account_id}"
                if key in _tfa_requests:
                    _tfa_codes[key] = None
                    _tfa_requests[key].set()

        elif data.startswith("ctoggle:"):
            _, pending_id, carousel_id = data.split(":", 2)
            selections = _carousel_selections.get(pending_id, [])
            if carousel_id in selections:
                selections.remove(carousel_id)
            else:
                selections.append(carousel_id)
            _carousel_selections[pending_id] = selections
            carousels = self._get_all_carousels()
            keyboard = self._build_carousel_keyboard(pending_id, carousels, selections)
            try:
                await query.edit_message_reply_markup(reply_markup=keyboard)
            except Exception:
                pass

        elif data.startswith("cdone:"):
            _, pending_id = data.split(":", 1)
            selections = _carousel_selections.pop(pending_id, [])
            party_db_id = self._find_party_db_id(pending_id)
            added_to = []
            if party_db_id and selections:
                for cid in selections:
                    if self._add_party_to_carousel(cid, party_db_id):
                        try:
                            from bson.objectid import ObjectId
                            coll_c = self._carousels_collection
                            cdoc = coll_c.find_one({"_id": ObjectId(cid)}, {"title": 1}) if coll_c else None
                            added_to.append(cdoc.get("title", cid) if cdoc else cid)
                        except Exception:
                            added_to.append(cid)
            if added_to:
                await query.edit_message_text(f"✅ Added to: {', '.join(added_to)}", parse_mode="Markdown")
            else:
                await query.edit_message_text("✅ Saved (no carousels selected).")

        elif data.startswith("cskip:"):
            _carousel_selections.pop(data.split(":", 1)[1], None)
            await query.edit_message_text("⏭ Carousel assignment skipped.")

    # ------------------------------------------------------------------
    # Text handler (2FA codes and edit JSON)
    # ------------------------------------------------------------------

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_manager(update):
            return
        text = (update.message.text or "").strip()
        chat_id = update.effective_chat.id

        if text.isdigit() and len(text) == 6:
            for key, event in list(_tfa_requests.items()):
                if key.startswith("2fa_code_") and not event.is_set():
                    _tfa_codes[key] = text
                    event.set()
                    await update.message.reply_text(
                        f"✅ 2FA code `{text}` received.", parse_mode="Markdown"
                    )
                    return
            await update.message.reply_text("ℹ️ No pending 2FA request. Code ignored.")
            return

        if chat_id in _edit_sessions:
            pending_id = _edit_sessions.pop(chat_id)
            try:
                edits = json.loads(text)
                if not isinstance(edits, dict):
                    raise ValueError("Must be a JSON object")

                orig_party_data = self._get_pending_party_data(pending_id)
                orig_party_data.update({k: v for k, v in edits.items()})

                result = self._call_backend_edit_approve(pending_id, edits)
                if result:
                    party_db_id = result.get("party_db_id")
                    await update.message.reply_text("✅ Party updated and approved!")
                    await self._send_carousel_selection(pending_id, orig_party_data, party_db_id)
                else:
                    await update.message.reply_text("❌ Failed to update party.")
            except (json.JSONDecodeError, ValueError) as exc:
                _edit_sessions[chat_id] = pending_id
                await update.message.reply_text(
                    f"❌ Invalid JSON: {exc}\nTry again or send /cancel to abort."
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_message_sync(self, text: str, parse_mode: str = "Markdown"):
        if not self._loop or not self._app:
            logger.warning("Telegram bot not started; cannot send message.")
            return
        asyncio.run_coroutine_threadsafe(self._send_text(text, parse_mode), self._loop)

    async def _send_text(self, text: str, parse_mode: str = "Markdown"):
        try:
            await self._app.bot.send_message(
                chat_id=self.manager_chat_id, text=text, parse_mode=parse_mode
            )
        except Exception as exc:
            logger.error(f"Failed to send Telegram message: {exc}")

    def send_party_for_approval_sync(self, pending_doc: dict):
        if not self._loop or not self._app:
            return
        asyncio.run_coroutine_threadsafe(
            self._send_pending_party_message(pending_doc), self._loop
        )

    async def _send_pending_party_message(self, pending_doc: dict):
        party = pending_doc.get("party_data", {})
        pending_id = str(pending_doc.get("_id", ""))
        account = pending_doc.get("account_id", "?")

        name = party.get("name", "Unknown Party")
        date_str = party.get("date", "Unknown Date")
        location = party.get("location", "Unknown Location")
        price = party.get("ticketPrice")
        sold_out = party.get("soldOut", False)
        url = party.get("originalUrl") or party.get("goOutUrl", "")
        image_url = party.get("imageUrl", "")

        price_text = "🎫 Sold Out" if sold_out else (
            f"💰 ₪{price:.0f}" if price else "💰 Free / Unknown"
        )

        text = (
            f"🎉 *New Party Found!*\n"
            f"Account: {account}\n\n"
            f"📛 *{self._escape_md(name)}*\n"
            f"📅 {self._escape_md(str(date_str))}\n"
            f"📍 {self._escape_md(str(location))}\n"
            f"{price_text}\n"
            f"🔗 [Go-Out Link]({url})"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{pending_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{pending_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{pending_id}"),
        ]])

        try:
            if image_url:
                try:
                    await self._app.bot.send_photo(
                        chat_id=self.manager_chat_id,
                        photo=image_url,
                        caption=text,
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
                    return
                except Exception:
                    pass
            await self._app.bot.send_message(
                chat_id=self.manager_chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
                disable_web_page_preview=False,
            )
        except Exception as exc:
            logger.error(f"Failed to send party for approval: {exc}")

    # ------------------------------------------------------------------
    # 2FA coordination
    # ------------------------------------------------------------------

    def ask_2fa_availability_sync(self, account_id: str, timeout: float = 600) -> bool:
        if not self._loop or not self._app:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._ask_2fa_availability(account_id, timeout), self._loop
        )
        try:
            return future.result(timeout=timeout + 30)
        except Exception:
            return False

    async def _ask_2fa_availability(self, account_id: str, timeout: float) -> bool:
        key = f"2fa_avail_{account_id}"
        event = asyncio.Event()
        _tfa_requests[key] = event
        _tfa_codes[key] = None
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ I'm available", callback_data=f"2fa:{account_id}:ready"),
            InlineKeyboardButton("⏳ Not now", callback_data=f"2fa:{account_id}:later"),
        ]])
        try:
            await self._app.bot.send_message(
                chat_id=self.manager_chat_id,
                text=(
                    f"🔐 *2FA Required for {account_id}*\n\n"
                    "The Go-Out session has expired and needs re-authentication.\n"
                    "Are you available to enter the 2FA code?"
                ),
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.error(f"Failed to ask 2FA availability: {exc}")
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _tfa_requests.pop(key, None)
            _tfa_codes.pop(key, None)
            return False
        result = _tfa_codes.pop(key, None)
        _tfa_requests.pop(key, None)
        return result == "ready"

    def request_2fa_code_sync(self, account_id: str, timeout: float = 600) -> str | None:
        if not self._loop or not self._app:
            return None
        future = asyncio.run_coroutine_threadsafe(
            self._request_2fa_code(account_id, timeout), self._loop
        )
        try:
            return future.result(timeout=timeout + 30)
        except Exception:
            return None

    async def _request_2fa_code(self, account_id: str, timeout: float) -> str | None:
        key = f"2fa_code_{account_id}"
        event = asyncio.Event()
        _tfa_requests[key] = event
        _tfa_codes[key] = None
        try:
            await self._app.bot.send_message(
                chat_id=self.manager_chat_id,
                text=(
                    f"🔢 *Enter 2FA Code for {account_id}*\n\n"
                    "Please check your email/SMS and reply with the 6-digit code."
                ),
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error(f"Failed to request 2FA code: {exc}")
            return None
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _tfa_requests.pop(key, None)
            _tfa_codes.pop(key, None)
            try:
                await self._app.bot.send_message(
                    chat_id=self.manager_chat_id,
                    text=f"⏰ 2FA code request for *{account_id}* timed out.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            return None
        code = _tfa_codes.pop(key, None)
        _tfa_requests.pop(key, None)
        return code

    # ------------------------------------------------------------------
    # Backend HTTP calls — auth via admin JWT
    # ------------------------------------------------------------------

    def _get_admin_jwt(self) -> str | None:
        """Log in to the backend and return a JWT token."""
        try:
            resp = http_requests.post(
                f"{config.BACKEND_URL}/api/admin/login",
                json={"password": config.ADMIN_PASSWORD},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("token")
            logger.warning(f"Backend login returned {resp.status_code}")
        except Exception as exc:
            logger.error(f"Backend login failed: {exc}")
        return None

    def _auth_headers(self) -> dict:
        token = self._get_admin_jwt()
        if not token:
            raise RuntimeError("Could not obtain admin JWT from backend.")
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    @staticmethod
    def _append_ref(url: str, referral: str) -> str:
        parsed = urlparse(url)
        qs = parse_qsl(parsed.query, keep_blank_values=True)
        if any(k.lower() == "ref" for k, _ in qs):
            return url
        qs.append(("ref", referral))
        return urlunparse(parsed._replace(query=urlencode(qs)))

    def _call_backend_approve(self, pending_id: str, party_data: dict | None = None) -> dict | None:
        if not party_data:
            party_data = self._get_pending_party_data(pending_id)

        # Resolve URL and referral — check party_data first, then the top-level pending doc
        url = party_data.get("canonicalUrl") or party_data.get("goOutUrl") or party_data.get("originalUrl")
        referral = party_data.get("referralCode")
        if not url:
            try:
                from bson.objectid import ObjectId
                coll = self._pending_collection
                doc = coll.find_one({"_id": ObjectId(pending_id)}) if coll else None
                if doc:
                    url = doc.get("goOutUrl")
                    if not referral:
                        referral = (doc.get("party_data") or {}).get("referralCode")
            except Exception:
                pass
        if not url:
            logger.error(f"No URL found in pending doc {pending_id}")
            return None

        # Strip any existing ref param so canonicalUrl is clean
        clean_url = url.split("?ref=")[0].split("&ref=")[0]

        try:
            headers = self._auth_headers()

            # Step 1: add the party (backend scrapes full details)
            r1 = http_requests.post(
                f"{config.BACKEND_URL}/api/admin/add-party",
                json={"url": clean_url},
                headers=headers,
                timeout=60,
            )
            if r1.status_code not in (200, 201, 409):
                logger.warning(f"Backend add-party returned {r1.status_code}: {r1.text[:200]}")
                return None

            d1 = r1.json()
            party_db_id = (d1.get("party") or {}).get("_id") or d1.get("id")

            # Step 2: apply the account-specific referral code
            if party_db_id and referral:
                goout_with_ref = self._append_ref(clean_url, referral)
                http_requests.put(
                    f"{config.BACKEND_URL}/api/admin/update-party/{party_db_id}",
                    json={"referralCode": referral, "goOutUrl": goout_with_ref, "originalUrl": goout_with_ref},
                    headers=headers,
                    timeout=15,
                )

            self._mark_pending_approved(pending_id)
            return {"party_db_id": party_db_id}
        except Exception as exc:
            logger.error(f"Failed to call backend approve: {exc}")
        return None

    def _mark_pending_approved(self, pending_id: str):
        from bson.objectid import ObjectId
        coll = self._pending_collection
        if not coll or not pending_id:
            return
        try:
            coll.update_one(
                {"_id": ObjectId(pending_id)},
                {"$set": {"status": "approved", "approved_at": datetime.now(timezone.utc)}},
            )
        except Exception:
            pass

    def _call_backend_reject(self, pending_id: str) -> bool:
        try:
            resp = http_requests.post(
                f"{config.BACKEND_URL}/api/admin/goout/reject/{pending_id}",
                headers=self._auth_headers(),
                timeout=15,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.error(f"Failed to call backend reject: {exc}")
        return False

    def _call_backend_edit_approve(self, pending_id: str, edits: dict) -> dict | None:
        try:
            resp = http_requests.post(
                f"{config.BACKEND_URL}/api/admin/goout/edit-approve/{pending_id}",
                json={"edits": edits},
                headers=self._auth_headers(),
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"Backend edit-approve returned {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            logger.error(f"Failed to call backend edit-approve: {exc}")
        return None

    # ------------------------------------------------------------------
    # Carousel helpers (direct DB access)
    # ------------------------------------------------------------------

    def _get_all_carousels(self) -> list:
        coll = self._carousels_collection
        if coll is None:
            return []
        try:
            return list(coll.find({}).sort("order", 1))
        except Exception:
            return []

    def _suggest_carousels(self, party_data: dict, carousels: list) -> list[str]:
        tags = {t.lower() for t in party_data.get("tags", [])}
        music = (party_data.get("musicType") or "").lower()
        etype = (party_data.get("eventType") or "").lower()
        region = (party_data.get("region") or "").lower()
        location = party_data.get("location") or {}
        if isinstance(location, dict):
            location = (location.get("name") or "").lower()
        else:
            location = str(location).lower()
        keywords = tags | {music, etype, region, location}
        keywords.discard("")
        suggested = []
        for c in carousels:
            title_lower = (c.get("title") or "").lower()
            if any(kw and kw in title_lower for kw in keywords):
                suggested.append(str(c["_id"]))
        return suggested

    def _build_carousel_keyboard(self, pending_id: str, carousels: list, selected_ids: list[str]) -> InlineKeyboardMarkup:
        selected_set = set(selected_ids)
        rows = []
        for c in carousels:
            cid = str(c["_id"])
            mark = "✅" if cid in selected_set else "⬜"
            rows.append([InlineKeyboardButton(
                f"{mark} {c.get('title', cid)}",
                callback_data=f"ctoggle:{pending_id}:{cid}",
            )])
        rows.append([
            InlineKeyboardButton("✅ Done", callback_data=f"cdone:{pending_id}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"cskip:{pending_id}"),
        ])
        return InlineKeyboardMarkup(rows)

    def _add_party_to_carousel(self, carousel_id: str, party_id: str) -> bool:
        from bson.objectid import ObjectId
        coll = self._carousels_collection
        if coll is None:
            return False
        try:
            result = coll.update_one(
                {"_id": ObjectId(carousel_id)},
                {"$addToSet": {"partyIds": ObjectId(party_id)}},
            )
            return result.matched_count > 0
        except Exception as exc:
            logger.error(f"Failed to add party to carousel: {exc}")
            return False

    def _get_pending_party_data(self, pending_id: str) -> dict:
        try:
            from bson.objectid import ObjectId
            coll = self._pending_collection
            doc = coll.find_one({"_id": ObjectId(pending_id)}) if coll else None
            return dict(doc.get("party_data", {})) if doc else {}
        except Exception:
            return {}

    def _find_party_db_id(self, pending_id: str) -> str | None:
        try:
            from bson.objectid import ObjectId
            coll = self._pending_collection
            doc = coll.find_one({"_id": ObjectId(pending_id)}) if coll else None
            if not doc:
                return None
            goout_url = doc.get("goOutUrl") or doc.get("party_data", {}).get("goOutUrl")
            if goout_url:
                party = self._parties_collection.find_one({"goOutUrl": goout_url}, {"_id": 1})
                if party:
                    return str(party["_id"])
        except Exception as e:
            logger.error(f"find_party_db_id failed: {e}")
        return None

    async def _send_carousel_selection(self, pending_id: str, party_data: dict, party_db_id: str | None = None):
        carousels = self._get_all_carousels()
        if not carousels:
            await self._app.bot.send_message(
                chat_id=self.manager_chat_id,
                text="ℹ️ No carousels configured — party added without carousel assignment.",
                parse_mode="Markdown",
            )
            return
        suggested = self._suggest_carousels(party_data, carousels)
        _carousel_selections[pending_id] = list(suggested)
        keyboard = self._build_carousel_keyboard(pending_id, carousels, suggested)
        party_name = party_data.get("name", "party")
        suggestion_note = f" ({len(suggested)} pre-selected based on tags)" if suggested else ""
        await self._app.bot.send_message(
            chat_id=self.manager_chat_id,
            text=f"🎪 *Add to carousels*{suggestion_note}\n_{self._escape_md(party_name)}_",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _escape_md(text: str) -> str:
        if not text:
            return ""
        for ch in ("_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"):
            text = text.replace(ch, f"\\{ch}")
        return text
