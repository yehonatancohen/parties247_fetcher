# goout-scraper

Standalone Python service, deployed via Docker on the Oracle VPS (see workspace root
`CLAUDE.md` for SSH/deploy steps). It logs into two GoOut ("go-out.co") organizer-panel
accounts with Playwright, discovers events, auto-approves them into the parties247 backend,
tracks ticket sales/revenue every 4 hours, and runs a Telegram bot for admin control (2FA
relay, manual approve/reject, carousel management, sales reporting).

Not on Vercel/Render — this is the one service that lives entirely on the VPS in Docker.

## Entry point & scheduling

`main.py` wires everything and blocks on `telegram_mgr.run_polling()` (main thread). An
`APScheduler BackgroundScheduler(daemon=True)` runs two jobs in a background thread:

- `daily_goout_scrape` — cron, `GOOUT_SCRAPE_HOUR:00 UTC` (env-configurable, **actual default
  is 6**, not 8 — the comment in `.env.example` was wrong until this doc pass; fixed there too).
  Calls `run_daily_scrape(accounts, db, telegram_mgr, force_send=False)`.
- `sales_update` — interval, every 4 hours (fires once at scheduler start, then every 4h from
  then — not aligned to clock hours). Calls `run_sales_update(accounts, db, telegram_mgr)`.

Both are also triggerable manually from Telegram (`/scrape [account_id]`, `/sales_update`).

The maintenance scripts (`dedupe_parties.py`, `backfill_goout_event_id.py`,
`cleanup_hot_now.py`) are **manual one-shot CLI tools only** — not scheduled, not wired to the
bot. Run by hand with `python <script>.py [--apply]` (dry-run by default except
`cleanup_hot_now.py`, which prompts interactively instead — not safe for unattended use).

## Daily scrape flow (`orchestrator.py::run_daily_scrape`)

1. Fresh asyncio loop → `_async_daily_scrape`.
2. One `GET /api/parties` to build an in-memory dedup index (normalized URL → party, plus a
   name+date fuzzy index) used for the whole run.
3. Per account, concurrently (`asyncio.gather`), each with its own Playwright browser context:
   1. `ensure_session()` — restore saved cookies (`goout_sessions` in Mongo) if still valid,
      else full login. Login can block up to ~10 minutes waiting on a Telegram 2FA relay (see
      below). If login fails, that account is skipped (others still run).
   2. `discover_events()` — scrape the organizer panel (see "Scraping mechanics" below),
      filtered to events not clearly in the past.
   3. Per event: skip test/demo events (name blocklist) → skip if already a known/approved
      party (`normalize_url` dedup key; account1 can "steal" referral attribution from another
      account since it's the priority account) → skip if already sitting undecided in
      `goout_pending` (prevents re-notifying daily) → fetch full details via
      `POST /api/internal/scrape-party` (backend does the real scrape), falling back to a
      direct public-page `__NEXT_DATA__` scrape, falling back to bare discovery metadata → fuzzy
      duplicate check (word-overlap ≥0.6, flags but doesn't block) → auto-approve via
      `POST /api/admin/add-party`, apply referral code, auto-assign matching carousels
      (`carousel_suggester`), log an audit doc into `goout_pending` with
      `status: "auto_approved"`. `await asyncio.sleep(2)` between events (only throttling that
      exists).
   4. `finally: scraper.close()` — always re-persists cookies to `goout_sessions`, even on
      failure, so the next run doesn't need to log in again.
4. Telegram summary message (raw HTTP to Bot API, not via the bot's own loop — this runs from a
   scheduler thread), chunked to Telegram's 4096-char limit.
5. **Always** runs afterward, even if the scrape itself raised: `run_hot_now_update` (rebuilds
   the "חם עכשיו" carousel as an exact full-replace = account1's current upcoming parties) then
   `run_carousel_auto_assign` (keyword-based bulk carousel matching across *all* upcoming
   parties, not just newly scraped ones).

## Scraping mechanics (`scraper.py`) — read this before touching anything here

GoOut has no public API contract for the organizer panel; everything is reverse-engineered from
observed network traffic and DOM structure. Expect it to be fragile.

- **Event discovery** works by intercepting the panel's own `**/myEvents**` XHR responses
  (rewriting `limit=` up to 500) rather than the DOM, then supplementing with
  `__NEXT_DATA__` JSON and a DOM chip-click fallback (`#12345`-style event IDs) for events the
  API silently omits — specifically team-member/co-organizer events.
- **`activeEvents:true` vs `false` is deliberately different between discovery and sales
  scraping** and this is *not* a bug: discovery needs `true` to see team-member events (`false`
  switches to a different query mode that drops them); sales scraping needs `false` to include
  ended events. Do not "fix" one to match the other.
- Almost none of the panel UI (tab clicks, filter dropdown, cookie consent) has stable
  selectors — it's matched by fuzzy Hebrew/English innerText search across generic elements.
  A GoOut copy change can silently zero out discovery with no exception raised — just watch for
  "Still waiting" log lines and 0-event runs. Login form fields
  (`#register_email`/`#register_password`/`#register_button`) are the only stable IDs.
  Hardcoded desktop Chrome/120 user-agent, no rotation.
- **No CAPTCHA handling at all.** If GoOut ever serves one, it just reads as "login failed" and
  re-triggers the Telegram 2FA relay pointlessly, every day, until someone notices.
- **2FA relay can block a whole account's scrape for up to ~10 minutes** waiting for a human to
  tap "I'm available" and then type a code into Telegram. During the unattended 6 AM run, if
  nobody's watching, that account silently fails for the day — there's no secondary alert beyond
  the scrape summary itself.
- Debug artifacts (`scratch/login_failed.{html,png}`, `scratch/2fa_page.{html,png}`,
  `scratch/debug_{account}_{i}.png`) are written on every failure with **no cleanup/rotation** —
  the directory grows unbounded. Worth a periodic manual clear.
- Sales scraping (`scrape_sales_data`, used every 4h) reads both the intercepted API responses
  *and* the DOM (`_scrape_sales_from_dom`, which parses four different date formats seen in the
  panel) and merges them, API taking priority per field. Ticket price / revenue is a chain of
  approximations — see "Known sharp edges" below.

## MongoDB collections (db name: `party247` — see root `CLAUDE.md`, do not use `parties247`)

| Collection | Written by | Purpose |
|---|---|---|
| `goout_sessions` | `scraper.py` | One doc per `account_id`: Playwright `storage_state` (cookies/localStorage), `session_valid`, `last_login`, `last_checked`. Avoids re-login (and re-2FA) every run. |
| `goout_pending` | `orchestrator.py`, `telegram_bot.py` | Despite the name, mostly an **audit trail** now — auto-approve inserts docs already `status: "auto_approved"`. Still used for the (mostly vestigial) manual `/pending` review flow and as a same-day dedup check. |
| `goout_sales` | `sales_tracker.py` | Latest snapshot per `(account_id, go_out_id)`: confirmed/pending counts, ticket price, event revenue. |
| `goout_sales_log` | `sales_tracker.py` | Append-only delta log, one row per 4h period where something changed. Source of truth for `/sales`, `/sales_monthly` aggregation. |

`parties` and `carousels` belong to the **backend**, never touched directly by this service —
always via its HTTP API (`/api/parties`, `/api/admin/add-party`, `/api/admin/update-party/{id}`,
`/api/carousels`, `/api/admin/carousels/{id}/parties`).

`goOutEventId` on a party doc is the numeric GoOut `EventSerial` — the join key to
`goout_sales`/`goout_sales_log`. Older parties predate this field; `backfill_goout_event_id.py`
fixes that retroactively.

## Backend API auth

Two different auth mechanisms, don't mix them up:
- **Service token** (`X-Service-Token: SERVICE_TOKEN` header) — for the internal
  `/api/internal/scrape-party` endpoint only, used by both `orchestrator.py` and
  `backfill_goout_event_id.py`.
- **Admin JWT** (`Authorization: Bearer <token>`, obtained via `POST /api/admin/login` with
  `ADMIN_PASSWORD`) — for everything else under `/api/admin/*`. `telegram_bot.py` fetches a
  **fresh JWT on every single call**, no caching — every approve/carousel-update does an extra
  login round-trip. Worth caching with the ~30-day expiry noted in the root `CLAUDE.md` if
  call volume ever becomes a problem.

## Telegram bot (`telegram_bot.py`)

Single authorized chat only — every command/callback checks
`update.effective_chat.id == TELEGRAM_MANAGER_CHAT_ID`. In-process session state
(`_tfa_requests`, `_edit_sessions`, `_carousel_selections`, etc.) is module-level and **not
persisted** — lost on restart.

Commands: `/start`, `/help`, `/status`, `/scrape [account_id]`, `/pending`, `/approve_all`,
`/sessions`, `/sales`, `/sales_monthly [YYYY-MM]`, `/sales_update`, `/cancel`.
Callbacks: `approve:`, `reject:`, `edit:`, `2fa:{account}:{ready|later}`, `cshow:`, `ctoggle:`,
`cdone:`, `cskip:`. A bare 6-digit text message is treated as a 2FA code; otherwise, if mid
edit-session, text is parsed as a JSON party-field-overrides object.

## Sales/revenue rules (`sales_tracker.py`)

- **account1**: flat ₪25 per new confirmed ticket (`ACCOUNT1_FLAT_FEE`).
- **account2**: 6% of gross event revenue delta (`ACCOUNT2_PCT`), falling back to
  `delta_confirmed × ticket_price × 6%` when gross revenue isn't exposed to that account's role.
- Dispatch is by substring match on `account_id` (`"account1" in account_id`) — **hardcoded to
  exactly these two accounts**. A third account would silently earn ₪0.
- **First-time revenue-delta bug risk**: when `event_revenue` is seen for an event for the first
  time (no previous snapshot), the *entire* current gross value is treated as that period's
  delta. For an event with real revenue history before it had a `goOutEventId`/was first joined,
  this over-credits account2 with 6% of the event's *lifetime* revenue in one 4-hour window.
  Worth checking if account2's numbers ever look implausibly high right after a backfill.

## Carousel logic (`carousel_suggester.py`)

Pure keyword matching against a carousel's *title* (music genre, city, event type, age,
temporal, name keywords — all Hebrew+English tables in the file) — no ML, no stable IDs, so
renaming a carousel changes what it auto-matches. `LOCATION_DAYS_CAP = 60`: city carousels only
pull in parties within 60 days. The "חם עכשיו" (Hot Now) carousel is explicitly excluded from
this keyword logic — it's exclusively managed by `run_hot_now_update`'s full-replace rebuild.

## Known sharp edges / candidates for the "improve scraping" effort

Ranked roughly by how much they'd affect reliability or data accuracy:

1. **Fuzzy, copy-dependent selectors are the single biggest reliability risk.** Any GoOut
   wording change to the panel UI can zero out discovery silently. There's currently no
   alerting on "0 events found" as an anomaly — worth adding a sanity check/alert if a
   previously-nonzero account suddenly discovers 0 events.
2. **No CAPTCHA detection** — would surface as endless daily 2FA-relay pings with no actual
   progress.
3. **10-minute 2FA blocking window with no escalation** if nobody answers Telegram in time —
   the account just silently fails for that day.
4. **Ticket price/revenue is approximated**, not exact, especially for account2 (flat pricing
   assumption ignores multiple ticket tiers actually sold; revenue-calc method can silently
   switch between "real gross revenue" and "confirmed × price × 6%" run to run).
5. **First-run revenue-delta over-attribution** described above.
6. **JWT re-fetched per call** in `telegram_bot.py` — not a correctness bug, but adds needless
   backend load/latency on every admin action.
7. **`scratch/` debug dumps never cleaned up.**
8. **`API_PORT` config var is dead** (read, never used) — harmless but confusing; likely
   copy-pasted from another service's `.env`.

## Fixed 2026-08-06 (this pass)

- `telegram_bot.py::_find_party_db_id` was hardcoding `self._db.client["party247"].parties`
  instead of using `self._db` (already the correctly-configured database per
  `MONGODB_DB_NAME`). Harmless today only because `party247` happens to be the currently
  configured name — but it silently bypassed config and would have reintroduced the exact
  parties/party247 split bug (see root `CLAUDE.md`) if the db name ever changed again. Changed
  to `self._db.parties`.
- `.env.example`'s comment claimed `GOOUT_SCRAPE_HOUR` defaults to 8; `config.py`'s actual
  default is 6. Comment corrected.
