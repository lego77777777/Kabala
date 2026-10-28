# Kabala Server

A small server for the commercial side of the product: user accounts, plans (Free/Pro), Stripe subscriptions and version updates.
**It never receives or stores emails or receipts.** Those stay on the user's computer.

## How a user is identified

The app signs in to Google with `openid email` scopes, in addition to `gmail.readonly`, and sends the server only the **Google ID token**.
The server checks the token's signature against Google, along with its expiry and that it was issued to our OAuth client. It then returns a signed session token (HMAC) that is valid for 30 days.
There are no passwords, and no Gmail access token ever reaches the server.

## Sign-in by email code

Accounts connected over IMAP (Gmail with an app password) have no Google ID token, so the app proves the address another way: the server emails a 6-digit code to it, the user types it in the app, and the server returns a session for that address. The plan is then looked up by the address, exactly like a Google sign-in.

| Endpoint | What it does |
|---|---|
| `POST /api/email/start` `{email}` | Emails a code, valid for 10 minutes |
| `POST /api/email/verify` `{email, code}` | Returns the session token and the plan (same as `/api/session`) |

The codes come from [pyotp](https://github.com/pyauth/pyotp) (MIT), copied into `kabala_server/vendor/pyotp/` (see its `KABALA_CHANGES.md`): HOTP (RFC 4226) with a fresh random secret per challenge and a constant-time check.

Limits:
- 5 wrong tries end a challenge.
- A resend makes the previous code stop working.
- At most 5 codes per address per hour, and 5 per IP per 10 minutes.
- Failed tries count toward the anti-DDoS ban.

Without `SMTP_URL` the endpoints answer 503, and the app falls back to the browser-based Google identity sign-in.

## Environment variables

| Variable | What it is |
|---|---|
| `KABALA_SECRET` | A random string of at least 32 characters; the encryption key for sessions and stored sign-in secrets (e.g. `python -c "import secrets;print(secrets.token_urlsafe(48))"`) |
| `KABALA_OLD_SECRETS` | After changing `KABALA_SECRET`: the previous value(s), comma-separated, so existing sessions and data stay readable |
| `KABALA_REQUIRE_HTTPS` | Default `1`: behind a proxy (`KABALA_PROXY_HOPS` > 0) plain-http requests get 403 and responses carry HSTS. `0` turns it off |
| `GOOGLE_CLIENT_ID` | The app's OAuth client ID (from `google_client.json`) |
| `STRIPE_SECRET_KEY` | Stripe secret key (`sk_test_…` for testing, `sk_live_…` for production) |
| `STRIPE_PRICE_PRO` | The Price ID of the Pro subscription (`price_…`), created in Stripe → Products |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` from Stripe → Developers → Webhooks |
| `PUBLIC_URL` | Where to redirect after payment (default: the GitHub Pages site) |
| `LATEST_VERSION`, `DOWNLOAD_URL` | For update notifications |
| `KABALA_DB` | Path to the SQLite file |
| `SMTP_URL` | For sign-in codes: `smtp://user:password@host:587` (STARTTLS) or `smtps://user:password@host:465`. Encode `@` in the user as `%40`. Gmail works with an app password; also SES, Postmark, Mailgun |
| `KABALA_OWNER_EMAILS` | Admin panel owners, comma-separated (more admins and roles are managed in the panel) |
| `MAIL_FROM` | Sender of the codes (default `Kabala <no-reply@kabala.app>`) |
| `KABALA_PROXY_HOPS` | How many reverse proxies sit in front of the server (Render/Fly/Railway: `1`, plus Cloudflare: `2`). `0` (default) ignores `X-Forwarded-For` |
| `KABALA_RATE_LIMIT` | Requests per minute per IP across all routes (default `120`) |
| `KABALA_ALLOW_IPS` | Comma-separated IPs that skip all limits (e.g. uptime monitoring) |
| `KABALA_MAX_CONNECTIONS` | Open connections before uvicorn answers 503 (default `200`) |
| `KABALA_SHIELD` | `0` turns the protection off (not recommended) |

## Anti-DDoS protection

Two layers, both in front of every route and the database:

**Rate limits: [slowapi](https://github.com/laurents/slowapi)** (MIT, the standard rate limiter for FastAPI), copied into `kabala_server/vendor/slowapi/` and adapted for Kabala. Every change is listed in [`KABALA_CHANGES.md`](kabala_server/vendor/slowapi/KABALA_CHANGES.md) and marked `# Kabala:` in the code; the most important one closes slowapi's spoofable `X-Forwarded-For` handling. It uses the `limits` package with the moving-window strategy, per IP:

| Route | Limit (same as `../billing`) |
|---|---|
| everything | 120/minute (`KABALA_RATE_LIMIT`) |
| `POST /api/session` | 20/minute |
| `POST /api/checkout`, `POST /api/portal` | 10/minute |
| `POST /api/stripe/webhook` | 600/minute |

Responses carry `X-RateLimit-*` and `Retry-After`; `/health` is exempt.

**Shield (`kabala_server/shield.py`)**, for what slowapi doesn't do:

1. **Ban (fail2ban-style jail):** 10 strikes within 10 minutes → 403 for 15 minutes, doubled on each repeat (up to 24 hours). A strike is any 429, a failed sign-in (401) or a webhook with a bad signature (400).
2. **Body size:** over 256 KB → 413, also for chunked bodies without `Content-Length`.
3. **Concurrency:** more than 10 in-flight requests from one IP → 429.
4. **Connection level** (`__main__.py`): uvicorn caps open connections, closes idle keep-alive after 5 seconds (slowloris) and limits header size.

State is in memory and bounded. Only the right-most `KABALA_PROXY_HOPS` entries of `X-Forwarded-For` are trusted. With several workers each one counts separately; volumetric (network-level) floods still need a CDN such as Cloudflare in front.

## Admin panel (`/admin`)

Open `https://<server>/admin` in Chrome. The spec it was built from is [`docs/admin-panel-prompt.md`](../docs/admin-panel-prompt.md).

- **Sign-in:** enter your email and you get a code by email. Only addresses in the admin list get a code; the first owners come from `KABALA_OWNER_EMAILS`. The session is an encrypted HttpOnly cookie that lasts 8 hours.
- **Screens:**
  - **Dashboard:** users, Pro users, paying users, new and active this week, past-due subscriptions, and a 30-day signup chart.
  - **Users** and **Payments:** search and filters, a details window with a link to Stripe, manual Pro, and CSV export.
  - **Admins & roles**
  - **Security:** banned IPs, with a button to lift a ban.
  - **Audit log**
- **Roles:**

| Role | Can |
|---|---|
| viewer | Stats and the user list, with masked emails |
| admin | Everything a viewer can, plus full emails, CSV export, manual Pro, lifting bans and the audit log |
| owner | Everything an admin can, plus managing admins |

- **Enforcement:** the server checks the role on every request, so removing an admin takes effect immediately. Every action is recorded in the audit log.

## Encryption

[Fernet](https://github.com/pyca/cryptography) (AES + HMAC) from pyca/cryptography, copied into `kabala_server/vendor/fernet/` (changes in its `KABALA_CHANGES.md`), used through `kabala_server/crypto.py`. Each purpose gets its own key derived from `KABALA_SECRET` with HKDF.

- **Session tokens:** encrypted, so the email and user id inside them can't be read. Tokens issued before this change still work until they expire.
- **Sign-in code secrets:** encrypted in the database, and erased once the code is used.
- **Database file:** created owner-only (0600). With `secure_delete`, deleted rows are zeroed on disk.
- **In transit:** TLS is required behind the proxy, with HSTS.
- **Secrets in output:** never shown in a printed config or in the logs.

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
