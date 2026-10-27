# Kabala Server

A small server for the commercial side of the product: user accounts, plans (Free/Pro), Stripe subscriptions and version updates.
**It never receives or stores emails or receipts.** Those stay on the user's computer.

## How a user is identified

The app signs in to Google with `openid email` scopes, in addition to `gmail.readonly`, and sends the server only the **Google ID token**.
The server checks the token's signature against Google, along with its expiry and that it was issued to our OAuth client. It then returns a signed session token (HMAC) that is valid for 30 days.
There are no passwords, and no Gmail access token ever reaches the server.

## Environment variables

| Variable | What it is |
|---|---|
| `KABALA_SECRET` | A random string of at least 32 characters for signing sessions (e.g. `python -c "import secrets;print(secrets.token_urlsafe(48))"`) |
| `GOOGLE_CLIENT_ID` | The app's OAuth client ID (from `google_client.json`) |
| `STRIPE_SECRET_KEY` | Stripe secret key (`sk_test_…` for testing, `sk_live_…` for production) |
| `STRIPE_PRICE_PRO` | The Price ID of the Pro subscription (`price_…`), created in Stripe → Products |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` from Stripe → Developers → Webhooks |
| `PUBLIC_URL` | Where to redirect after payment (default: the GitHub Pages site) |
| `LATEST_VERSION`, `DOWNLOAD_URL` | For update notifications |
| `KABALA_DB` | Path to the SQLite file |
| `KABALA_PROXY_HOPS` | How many reverse proxies sit in front of the server (Render/Fly/Railway: `1`, plus Cloudflare: `2`). `0` (default) ignores `X-Forwarded-For` |
| `KABALA_RATE_LIMIT` | Requests per minute per IP across all routes (default `120`) |
| `KABALA_ALLOW_IPS` | Comma-separated IPs that skip all limits (e.g. uptime monitoring) |
| `KABALA_MAX_CONNECTIONS` | Open connections before uvicorn answers 503 (default `200`) |
| `KABALA_SHIELD` | `0` turns the protection off (not recommended) |

## Anti-DDoS protection (`kabala_server/shield.py`)

An ASGI middleware with no extra dependencies, adapted from the designs of slowapi/limits (rate limits, sliding window), fail2ban (bans) and `@fastify/rate-limit` (the same numbers as `../billing`). It runs before any route or database work:

1. **Ban (jail):** 10 strikes within 10 minutes → 403 for 15 minutes, doubled on each repeat (up to 24 hours). A strike is a 429, a failed sign-in (401) or a webhook with a bad signature (400).
2. **Body size:** over 256 KB → 413, also for chunked bodies without `Content-Length`.
3. **Concurrency:** more than 10 in-flight requests from one IP → 429.
4. **Rate limits** (sliding window, per IP): 120/minute globally; `POST /api/session` 20, `/api/checkout` and `/api/portal` 10, `/api/stripe/webhook` 600. Responses carry `X-RateLimit-*` and `Retry-After`.
5. **Connection level** (`__main__.py`): uvicorn caps open connections, closes idle keep-alive after 5 seconds (slowloris) and limits header size.

State is in memory and bounded, so spoofed addresses can't exhaust it. Only the right-most `KABALA_PROXY_HOPS` entries of `X-Forwarded-For` are trusted, so a client can't dodge limits by inventing an IP. With several workers each one counts separately; volumetric (network-level) floods still need a CDN such as Cloudflare in front.

## Running locally

```bash
pip install -r requirements.txt
python -m kabala_server
```

## Deployment (example: Render / Railway / Fly.io)

1. Deploy the `server/` folder with the `Dockerfile`. Attach a persistent volume at `/data`.
2. Set the environment variables.
3. In Stripe → Webhooks, add an endpoint at `https://<server>/api/stripe/webhook` with these events:
   `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`.
4. In the desktop app, set the server address: in a `kabala_server.json` file next to `google_client.json`:
   `{"url": "https://<server>"}`. Without it the app works in "unmanaged" mode, where every feature is open (good for development).

## Tests

```bash
python -m pytest tests
```
