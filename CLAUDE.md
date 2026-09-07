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
- `sales_update` — interval, every 4 hours, `next_run_time` forced to fire immediately on
  scheduler start (fixed 2026-08-07 — APScheduler's default for an `IntervalTrigger` with no
  `start_date` is start+interval, *not* immediately; confirmed via VPS logs showing a ~4h gap
  between scheduler start and the first sales-related log line). Not aligned to clock hours.
  Calls `run_sales_update(accounts, db, telegram_mgr)`.

Both are also triggerable manually from Telegram (`/scrape [account_id]`, `/sales_update`).

The maintenance scripts (`backfill_goout_event_id.py`, `cleanup_hot_now.py`) are **manual
one-shot CLI tools only** — not scheduled, not wired to the bot. Run by hand with
`python <script>.py [--apply]` (dry-run by default except `cleanup_hot_now.py`, which prompts
interactively instead — not safe for unattended use). `dedupe_parties.py` is the exception as
of 2026-08-07 — see "Fixed 2026-08-07 (sixth pass)" below, it's now also wired into the daily
scrape via `orchestrator.py::run_dedupe_pass`, though it can still be run standalone too.

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
- **`ticket_price`/`event_revenue` are currently always `null` — no working data source.**
  Confirmed/pending ticket counts (`confirmed_count`/`pending_count` in `goout_sales`) *are*
  real and reliable (from the `myEvents` API + DOM merge). But every account currently earns
  ₪0 in practice, since there is no working price/revenue source — see "Fixed 2026-08-07" below
  for what was tried and ruled out. Don't re-attempt the `api.fe.prod.go-out.co` URL-guessing
  approach that used to live in `_fetch_ticket_price_via_api` — confirmed dead (0/522 stored
  events ever got a price via it before removal).

## Carousel logic (`carousel_suggester.py`)

Pure keyword matching against a carousel's *title* (music genre, city, event type, age,
temporal, name keywords — all Hebrew+English tables in the file) — no ML, no stable IDs, so
renaming a carousel changes what it auto-matches. `LOCATION_DAYS_CAP = 60`: city carousels only
pull in parties within 60 days. The "חם עכשיו" (Hot Now) carousel is explicitly excluded from
this keyword logic — it's exclusively managed by `run_hot_now_update`'s full-replace rebuild.

## Added 2026-09-07 — alerts, CAPTCHA bail-out, best-sellers carousel, unit tests

- `alerts.py` (pure, no `config` import): `detect_captcha(html)` recognises *rendered*
  challenges (Cloudflare interstitial/turnstile, hCaptcha/reCAPTCHA iframes) but not a bare
  reCAPTCHA `<script>` include; `discovery_alert(account, found, previous)` fires on 0
  events with any baseline, or a ≥80% drop from a baseline ≥10. Baseline lives in the
  account's `goout_sessions` doc (`last_discovery_count`, only updated by non-zero runs so a
  dark account keeps alerting daily). `should_send_captcha_alert` throttles to one Telegram
  alert per account per 12h (`last_captcha_alert_at`).
- `scraper.py::_abort_if_captcha(stage)` runs on the login page *before* the 2FA
  availability ask and again after submit — closes sharp edge #2 (endless daily 2FA pings).
- `orchestrator.py`: discovery-count check right after `discover_events()` (sharp edge #1);
  new `run_best_sellers_update` — exact full-replace of the "הכי נמכרים 🔥" carousel with
  upcoming parties ranked by **our commission** in `goout_sales_log` over the last 14 days
  (`best_sellers.py::rank_best_sellers`; ties → tickets → account1 first). Creates the
  carousel on first run. Runs after `run_hot_now_update` in the daily scrape and after every
  4h sales update (`main.py::_sales_job`). `carousel_suggester` skips this carousel like it
  skips Hot Now — its title must never contain the temporal keywords ("שבוע", "עכשיו").
- `tests/` + `pytest.ini` (`testpaths = tests`, so the live `test_sales.py` script is not
  collected). Run with `.venv/bin/python -m pytest`. Pure modules only; nothing here needs
  env vars, GoOut, Mongo or Telegram.

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
4. **Ticket price/revenue has no working data source at all** (not merely approximated —
   see "Fixed 2026-08-07" below). account1's flat fee and account2's percentage both
   currently compute to ₪0 for every event, even though confirmed-ticket counts are real.
5. **First-run revenue-delta over-attribution** described above.
6. **JWT re-fetched per call** in `telegram_bot.py` — not a correctness bug, but adds needless
   backend load/latency on every admin action.
7. **`scratch/` debug dumps never cleaned up.**
8. **`API_PORT` config var is dead** (read, never used) — harmless but confusing; likely
   copy-pasted from another service's `.env`.

## Fixed 2026-08-07 (sixth pass — duplicate GoOut listings fragmenting revenue/SEO)

Found while running `/seo-update` with its new revenue-optimization scope: the same
real-world event sometimes gets scraped as **multiple separate GoOut listings** (different
`goOutEventId`s, not just a DB glitch — confirmed via `/api/parties`, 7 duplicate name+date
groups found in one pass). Worst case: "Revival Summer Festival 13-14.8" existed as 3
separate listings across 2 site URLs, splitting a real-earning event's traffic/SEO signal
and sales tracking three ways — one listing had actual ticket sales (₪38.28), the other two
had zero, yet the fuzzy duplicate check in `orchestrator.py::_find_duplicate` only *flags*,
never blocks, so all three got auto-approved as separate parties.

`dedupe_parties.py` already existed as a manual one-shot fix for this exact case, but had two
gaps closed this pass:
1. **No redirect on delete** — it just called `DELETE /api/admin/delete-party/<id>`, leaving
   the dead slug 404ing and losing any accumulated SEO/traffic signal. Fixed: the endpoint now
   accepts an optional `redirectTo=<keeper-slug>` and records the mapping in a new
   `party_redirects` Mongo collection (`parties247_backend/app.py`); `parties247-website`'s
   `proxy.ts` checks the new public `GET /api/redirects/<slug>` endpoint on a 404 and serves a
   308 to the survivor instead of a dead link.
2. **Wrong tiebreaker** — when no account1 duplicate existed, it kept the *cheapest*
   `ticketPrice`, which in the Revival case would have deleted the already-earning listing in
   favor of a zero-revenue one. Fixed: now keeps whichever duplicate has the most real revenue
   (`/api/admin/analytics/sales`, our commission figure), falling back to cheapest price only
   when revenue is tied/zero on all candidates. account1 still always wins over account2
   duplicates regardless of revenue, per the site's stated revenue-priority order.

Also refactored `main()` into an importable `run_dedupe(apply, log)` and wired it into
`orchestrator.py::run_daily_scrape` (new `run_dedupe_pass`, runs after `run_hot_now_update`/
`run_carousel_auto_assign`, best-effort — a failure here doesn't affect the rest of the day's
scrape, which has already completed by that point) — so duplicates get caught and redirected
automatically every day instead of needing someone to remember to run the script by hand.

## Fixed 2026-08-06 (this pass)

- `telegram_bot.py::_find_party_db_id` was hardcoding `self._db.client["party247"].parties`
  instead of using `self._db` (already the correctly-configured database per
  `MONGODB_DB_NAME`). Harmless today only because `party247` happens to be the currently
  configured name — but it silently bypassed config and would have reintroduced the exact
  parties/party247 split bug (see root `CLAUDE.md`) if the db name ever changed again. Changed
  to `self._db.parties`.
- `.env.example`'s comment claimed `GOOUT_SCRAPE_HOUR` defaults to 8; `config.py`'s actual
  default is 6. Comment corrected.

## Fixed 2026-08-07 (this pass)

- `sales_tracker.py`: guarded against a transient confirmed-count regression (a bad scrape
  reading 0 for an event that previously had real sales would both log a false negative delta
  *and* cause a spurious double-charge next run when the real count reappeared).
- `sales_tracker.py`: stopped discarding a legitimately free (₪0) stored `ticket_price` in
  favor of a re-fetch (falsy-zero `or` bug — `existing.get("ticket_price") or live_price`).
- `scraper.py`: removed `_fetch_ticket_price_via_api`/`_fetch_finance_revenue_parallel` — the
  guessed `api.fe.prod.go-out.co` URL patterns never actually worked in production.

**Revenue/views investigation (not solved).** GoOut's real per-event data lives at
`POST www.go-out.co/endOne/getEventViews` and `POST www.go-out.co/endOne/getEventStatistics/
getRevenueData`, both keyed by the event's Mongo `_id` (`obj["_id"]` in the `myEvents`
response — *not* `EventSerial` or `Url`). Confirmed working from a real residential-IP browser
session (₪420 revenue, 12 views, matched the panel UI exactly). But every call to these
specific endpoints from the VPS fails with `net::ERR_FAILED`/`TypeError: Failed to fetch`,
and each of the following was tested directly and ruled out as the cause:
- Headless Chromium fingerprinting (tested real non-headless Chromium via Xvfb — still fails)
- Cross-origin/CORS or cookie-domain mismatch between `go-out.co` and `www.go-out.co` (redirect
  and cookie scoping both confirmed correct; the app's own frontend makes this exact
  cross-origin call successfully)
- Missing a required call sequence (tested calling `eventManagement/initialEvent` first, like
  the real page does on navigation — still fails, including `initialEvent` itself)

Remaining likely causes: IP-reputation blocking of the VPS's datacenter IP, or deeper
automation fingerprinting (e.g. Cloudflare detecting the CDP protocol Playwright uses to drive
the browser, which persists regardless of headless state). Both need materially more
investment than the rest of this fix (a residential proxy, or a non-CDP automation approach)
— don't re-attempt Xvfb/headless-spoofing tricks without new evidence, both were tested and
don't help. `mongo_id` extraction and the `/endOne` call code were removed rather than left as
a non-functional dead path; if revisited, the endpoints/payloads/response shapes documented
above are already correct and tested.

**Endpoint discovery, round 2 (2026-08-07, via claude-in-chrome on the user's real logged-in
browser — no VPS/2FA involved).** All confirmed live and working under `www.go-out.co/endOne/`
by patching `fetch`/`XMLHttpRequest` in-page and clicking into a real event. Same domain as
above — still blocked from the VPS, this only expands what's *available* once that's solved.
Response shapes (captured on a zero-sales event, so mostly empty/zero, but field names are
real):
- `getEventViews` → `{"status":true,"Views":2,"mediaViews":{}}` — `Views` is the real page-view
  counter shown in the panel's "מידע כללי" tab.
- `getEventStatistics/getRevenueData` → `{"revenue":{"total_revenue":0,"own_revenue":0,
  "own_table_revenue":0,"total_table_revenue":0,"own_amount_of_sales":0,
  "total_amount_of_sales":0}}`.
- `getEventStatistics/SalesPerDate` → `{"dates":{},"dates_pending":{},"dates_rejected":{}}` —
  **not previously known.** `dates` is (from the key names) almost certainly a dict keyed by
  calendar date with a per-day sold count — i.e. GoOut's own server-side record of *which day*
  each ticket sold, independent of when we happened to poll. If the shape holds on an event with
  real sales, this replaces the need to infer sale timing from our own snapshot-diff deltas
  entirely — it would directly fix the "sold long time ago, counted just now" timestamp problem
  documented above, no residential proxy workaround needed for *this specific* piece, just for
  getting the VPS able to reach `www.go-out.co/endOne/` at all.
- `getXLastAcceptedUsers` → `{"status":false,"users":[]}` on this event (0 sales, hence
  `status:false`). Strongly implied by the name to be the actual **buyer list** — exactly what
  was asked for (who bought, when). Not yet confirmed non-empty; need to capture it on an event
  with real accepted tickets to see the per-user shape (name/timestamp/price fields).
- `getUserTicketStatistics` → `{"Accepted":0,"Pending":0,"Rejected":0,"Abandoned":0,
  "TableAccepted":0,"TablePending":0,"TableRejected":0,"Total":0,"TotalTables":0,"Failed":0}`.
- `getXLeadingSalesman` → per-salesperson breakdown (`own_amount_of_sales`, role, join date).
- Also seen but not inspected: `getTotalExpenses`, `getTopTickets`, `loadEventTicketsTest`,
  `eventManagement/lastDayData`, `eventManagement/finnacialSummary`, `eventManagement/
  getSeatsManager`.

## Fixed 2026-08-07 (fifth pass — 139 of 145 active/future parties never scanned for sales at all)

User reported knowing about real ticket sales on currently-active parties that weren't showing
anywhere in the dashboard. Confirmed far worse than a display bug: **139 of 145 future-dated
parties with a `goOutEventId` had zero `goout_sales` document at all** — not stale, not zeroed,
literally never created — across *both* accounts (account1: 10/10 missing, account2: 129/135
missing), so purchases on those events were invisible however far back you queried. Every
`goout_sales` doc with real `confirmed_count > 0` was for a party whose date had already passed.

Root cause: `scraper.py::_extract_sales_from_obj()` dropped any object lacking all four of
confirmed/pending/event_revenue/event_date, on the theory that a bare `EventSerial` with nothing
else was probably an irrelevant nested object caught by the recursive JSON walk. Wrong in
practice — GoOut's `myEvents` payload for an event still weeks away frequently omits these
fields entirely (they populate as the event nears), so a real, valid event was silently
discarded instead of getting a `confirmed=0` baseline doc that later scrapes would update as
real data appeared. Fixed: keep any object with a valid `EventSerial` and a name/title (a real
`EventSerial` is already strong evidence of a genuine event; requiring a name filters actual
noise). Not yet verified whether this alone fully closes the gap for the small number of
future events dated many months out (2027-03-25 was among the missing set) — worth spot-
checking again after a few scrape cycles; if some are still missing, the `myEvents` request's
`limit=500` cap combined with total event volume per account (some near/over 500) is the next
suspect, since sort order for `activeEvents:false` isn't confirmed to put far-future events
within that cap.

Two more bugs found the same day, from a user report that active parties showed real GoOut
purchase-clicks but 0 GoOut views, and that "revenue" was showing GoOut's own number instead
of our commission calculation:

1. **`sales_tracker.py`'s revenue delta pipeline was fed a field that's always null.**
   `live_event_revenue = item.get("event_revenue")` (הכנסות לאירוע from the myEvents API) is
   dead — confirmed always `None` across all 535 tracked events. `_calc_revenue()`'s account2
   6%-of-revenue branch therefore always fell through to the `ticket_price × delta_confirmed`
   fallback, which was *also* always null (ticket_price never populated either — see "Known
   sharp edges" above), so account2 always earned ₪0 despite real ticket sales happening.
   Fixed: `live_event_revenue` now falls back to the real `own_revenue` figure from
   `endone_stats` (GoOut's own endOne/getRevenueData, confirmed non-zero and consistent with
   ticket counts — e.g. 8 confirmed tickets → ₪910) when `event_revenue` is null. Reuses the
   existing delta-per-poll machinery already built for ticket counts — no new collection
   needed.

   First shipped with a guard that zeroed the "first time seeing revenue" delta (to avoid
   attributing an event's whole historical revenue to one 4h window) — this was wrong and
   reverted same day: the very first (guarded) run already wrote the real `own_revenue` value
   into each doc's `event_revenue` field with a 0 logged delta, which permanently erased that
   revenue — the *next* run then saw `prev == live` (no change) and logged nothing either,
   since there was no "first sight" moment left. Reverted to the same lump-sum-on-first-sight
   convention `delta_confirmed` already uses (documented risk, not a new one). The already-
   contaminated docs from the guarded run needed a one-time manual backfill (10 `goout_sales_log`
   entries, `"backfill": true`, ₪151.25 total for account2 — account1 is unaffected since its
   flat-fee revenue never depended on `event_revenue`) to recover the lost delta; script was not
   kept, pattern is straightforward to reproduce if this class of bug recurs: for any
   `goout_sales` doc with `event_revenue > 0` and no existing `goout_sales_log` entry with a
   nonzero `delta_event_revenue` for that `(account_id, go_out_id)`, insert one now with
   `delta_event_revenue = event_revenue` (the full current value).
2. **Sales scraping's `activeEvents:false` rewrite silently drops team-member/co-organizer
   events forever, not just temporarily.** `discover_events()` already documents (see
   "Scraping mechanics" above) that `activeEvents:false` "switches to a different query mode
   that returns only events this account itself *owns*, silently dropping team-member events."
   `scrape_sales_data()` forces this rewrite unconditionally for every event, including still-
   upcoming ones — so a team/co-organizer event that was discovered fine (discovery uses
   `activeEvents:true`) simply never appears in any subsequent sales-tracking cycle again.
   Confirmed in production: an active event dated 12+ days out had 15 real site redirect-
   clicks but a `goout_sales` doc frozen 2+ days stale with no `endone_stats` at all — i.e.
   permanently invisible to sales/views tracking despite being live on the site. Fixed by
   capturing the page's own natural (untouched, `activeEvents:true`) myEvents request URL and
   replaying it directly via `context.request.get()` after the main false-mode scroll/DOM pass,
   merging any recovered events into the same `api_sales` list (already dedup-merges by
   `go_out_id`). One extra direct API call — myEvents itself isn't blocked from the VPS, only
   `endOne/*` is.

## Fixed 2026-08-07 (third pass — revenue/views blocker actually solved)

The VPS-only block on `www.go-out.co/endOne/*` (documented above and in project memory
`project_goout_revenue_views_blocker` since 2026-08-07) is resolved, not just further
investigated. Solution: a Cloudflare Worker relay (`cf-relay/`) — `www.go-out.co` is itself
Cloudflare-fronted (confirmed via its own `/cdn-cgi/rum` beacon), and routing these specific
calls through Cloudflare's network instead of directly from the VPS's Oracle datacenter IP gets
a normal `200` instead of `net::ERR_FAILED`. No residential proxy purchase needed. Full
rationale, deploy steps, and secret-rotation instructions: `cf-relay/README.md`.

Second bug found and fixed in the same pass: the relay calls were silently returning
`{"status":false}` even with cookies passed through, because **GoOut's panel doesn't use a
session cookie at all** — `context.cookies()` on a fully authenticated session contains only
marketing/analytics cookies (Stripe, TikTok, GA, `AWSALB`, etc.), no auth cookie. The real
session lives as a JWT in `localStorage.user.token`, sent as `Authorization: Bearer <token>`.
Once `scraper.py::_auth_header()` was added to read that and pass it through as `x-relay-auth`,
every endpoint started returning real data instead of `status:false`.

`scraper.py::scrape_sales_data()` now calls `_fetch_endone_stats()` for every event that has a
`mongo_id` (captured for free from the `myEvents` response — no extra lookup), pulling all ten
known `/endOne/*` fields per event with one retry each. Stored on the sales item as
`endone_stats`, and `sales_tracker.py` writes it (plus flattened `views`/`real_total_revenue`/
`real_own_revenue` convenience fields) onto each `goout_sales` document.

**Timing, measured locally 2026-08-07:** ~1.1–1.2s per event for the full 10-endpoint pass
(sequential, 0.3s pause between events). Account1 (22 events after the date filter): ~26s.
Account2 (227 events): ~4.5 min. Both accounts scrape concurrently in production
(`asyncio.gather` in `sales_tracker.py`), so a real 4-hour cycle costs roughly the larger of the
two — comfortably inside the 4h interval, no parallelization needed.

Not yet seen populated with real data (every event tested so far had zero actual ticket sales,
so these come back as legitimate empty/false rather than broken): `getXLastAcceptedUsers`
(the real buyer list), `getTotalExpenses`, `getTopTickets`, `eventManagement/finnacialSummary`.
Worth spot-checking their shape once an event with real sales gets scraped.

## Fixed 2026-08-07 (second pass — "41 sales in 7 days" was false, active events never tracked)

Two real, evidenced bugs found and fixed after the user reported the dashboard showing 41
GoOut sales in the last 7 days (actual 7-day delta from `goout_sales_log` was 5) and sales only
ever appearing to register once an event had already ended:

1. **`scraper.py::scrape_sales_data()` silently dropped every currently-active event.** The
   30-day trailing date filter (`item.get("event_date") and item["event_date"] >= cutoff`)
   treated a *missing* `event_date` as "stale" and dropped the item — but a missing date more
   often means date extraction (API `StartingDate` / DOM row-text regex) simply failed for that
   item, which happens disproportionately for still-active events. A direct query of
   `goout_sales` confirmed every single event with `confirmed_count > 0` had a **past** party
   date — zero exceptions — which is exactly what you'd see if active events never got
   tracked until they ended and a fuller payload (with a working date) became available. Fixed
   to only drop items with a *known* stale date, not an unknown one. This was the real cause of
   "sales only counted once the party goes active→inactive."
2. **`parties247_backend/app.py::build_party_funnel()` reported lifetime totals as if they were
   within the requested day window.** `purchases`/`revenue` were sourced from
   `build_sales_by_party()`, which aggregates `goout_sales_log` with no date filter at all
   (genuinely intended as an all-time summary for a different endpoint). The funnel's `views`/
   `redirects` *were* correctly filtered to `days`, so the two numbers were never comparable —
   "41 sales in the last 7 days" was actually all-time confirmed tickets across every tracked
   event. Fixed by adding `_sales_totals_by_event_id(cutoff)` and using it with the funnel's
   own cutoff instead of the lifetime helper.
3. **`main.py`'s `sales_update` job didn't fire immediately on scheduler start** (see above) —
   compounds bug #1's effect after every redeploy.

Not fixed this pass (need live investigation, not a code read — see project memory
`project_goout_revenue_views_blocker`): whether a per-order/participant endpoint with a real
purchase timestamp exists (would replace `recorded_at` = "time we noticed" with the real sale
time), and the VPS-only `net::ERR_FAILED` on the revenue/views endpoints.
