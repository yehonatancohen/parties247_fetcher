# GoOut endOne relay (Cloudflare Worker)

## Why this exists

GoOut's real per-event data (views, revenue, sales-per-date, buyer list, expenses, etc.) lives
at `www.go-out.co/endOne/*`. Calling that domain directly from the Oracle Cloud VPS fails with
`net::ERR_FAILED` — confirmed to be specific to that VPS's datacenter IP/ASN (the *other* GoOut
domain the scraper already uses successfully, `api.fe.prod.go-out.co`, works fine from the same
VPS; this is not a blanket GoOut-vs-VPS block).

`www.go-out.co` is confirmed Cloudflare-fronted (its own frontend calls `/cdn-cgi/rum`, a
Cloudflare-only path). Routing the same request through a Cloudflare Worker instead of directly
from the VPS reliably gets a real `200` response instead of a connection failure — verified
2026-08-07 by curl against this exact Worker. This Worker is a dumb relay only: it does not log
in or hold a GoOut session.

**Auth is a JWT bearer token, not cookies.** GoOut's panel carries no real session cookie at
all — `context.cookies()` on a fully logged-in session returns only marketing/analytics cookies
(Stripe, Facebook, TikTok, Bing, GA, `AWSALB`). The actual auth token lives in
`localStorage.user.token` (a JWT) and must be sent as `Authorization: Bearer <token>`. Confirmed
by direct testing 2026-08-07: cookie-only relay calls silently returned `{"status":false}` on
every endpoint; adding the bearer token immediately produced real data. `scraper.py` reads this
via `_auth_header()` (`page.evaluate` against `localStorage`) and passes it through the
`x-relay-auth` header; `x-relay-cookie` is still sent too (harmless, not required for auth as
far as tested, kept in case some endpoint turns out to need it).

## Files

- `worker.js` — the relay. Only forwards requests whose `target` query param starts with
  `https://www.go-out.co/endOne/` (hardcoded allowlist, nothing else is reachable through it).
  Requires `x-relay-secret` header matching the `RELAY_SECRET` Worker secret, or returns 403.
- `wrangler.toml` — points at the `Tough Language` Cloudflare account
  (`account_id = 4f52492ac247390986bd5d8807f89a1b`) created 2026-08-07 when this was first
  tested, then claimed under `coheyeho@gmail.com`.

## Deploying / redeploying

```
cd cf-relay
npx wrangler deploy
```

Requires `npx wrangler login` once per machine (OAuth via browser — note: this failed twice
from a sandboxed/headless shell with "Timed out waiting for authorization code" before finally
succeeding on the third attempt; if that happens again, an API token
(`dash.cloudflare.com` → My Profile → API Tokens → "Edit Cloudflare Workers" template →
`CLOUDFLARE_API_TOKEN` env var) is the more reliable fallback for automation).

## Rotating the secret

```
cd cf-relay
npx wrangler secret put RELAY_SECRET
```

Then update `CF_RELAY_SECRET` in `goout-scraper/.env` (local) and the VPS's
`/home/ubuntu/parties247_fetcher/.env`, and recreate the container so it picks up the new value.

## Usage from scraper.py

```
POST {CF_RELAY_URL}/?target=https://www.go-out.co/endOne/<path>
Headers:
  x-relay-secret: {CF_RELAY_SECRET}
  x-relay-auth:   Bearer <JWT from localStorage.user.token>   (required — this is what authenticates)
  x-relay-cookie: <the Playwright context's go-out.co cookies, "name=value; name2=value2">  (sent, not required)
Body: whatever JSON the real endpoint expects, e.g. {"eventId": "<mongo _id>"}
```

The Worker always POSTs to the upstream regardless of the inbound method, sets `Origin`/
`Referer: https://www.go-out.co(/businesspage)` (some endpoints appear to check this), and
returns the upstream's exact status/body back to the caller unchanged.

## Confirmed endpoint list (body: `{"eventId": "<event's mongo _id>"}` unless noted)

The mongo `_id` needed here already rides along for free in every event object returned by the
`myEvents` API the scraper already calls (`api.fe.prod.go-out.co`) — no separate lookup needed.

| Endpoint | Returns |
|---|---|
| `getEventViews` | `{"Views": N, "mediaViews": {...}}` |
| `getUserTicketStatistics/` (trailing slash matters) | `{"Accepted","Pending","Rejected","Abandoned","Total",...}` |
| `getEventStatistics/getRevenueData` | `{"revenue": {"total_revenue","own_revenue",...}}` |
| `getEventStatistics/SalesPerDate` | `{"dates": {...}, "dates_pending": {...}, "dates_rejected": {...}}` — per-day breakdown |
| `getXLeadingSalesman` | body also takes `numberOfUsers` (int) — per-salesperson breakdown |
| `getXLastAcceptedUsers` | body also takes `numberOfUsers` (int) — the real buyer list; not yet seen populated with actual buyers (every event tested so far had 0 accepted tickets), but confirmed to authenticate correctly (empty `{"status":false,"users":[]}` on a zero-sale event matches the real panel's own behavior) |
| `getTotalExpenses` | `{"threeDS","internalFees","SMS","Email","AffiliatesFees",...}` |
| `getTopTickets` | ticket-tier breakdown |
| `eventManagement/lastDayData` | `{"todayRevenue","acceptedToday"}` |
| `eventManagement/finnacialSummary` (sic — typo is GoOut's, not ours) | richer summary; returns `{"status":false,"err":"no purchase to calc - FS"}` when the event has zero sales |

Captured 2026-08-07 via Playwright network interception against a real logged-in session
(`goout-scraper/_diag_endone.py`, since deleted — this table is the durable record).
