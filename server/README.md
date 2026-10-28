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

The codes are sent through the Gmail API (see "Sending email: Gmail API only"). Until a Gmail account is connected, the endpoints answer 503, and the app falls back to the browser-based Google identity sign-in.

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
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` | The OAuth client (type **Web application**) that all email is sent with, through the Gmail API. The account itself is connected in the admin panel |
| `GMAIL_REFRESH_TOKEN` | Optional: an account given by environment instead of the panel (from `python -m kabala_server.gmail_api authorize` or billing's `npm run gmail:authorize`) |
| `KABALA_SERVER_URL` | This server's public address (`https://…`), for Google's redirect back after connecting Gmail. Default: taken from the request |
| `KABALA_OWNER_EMAILS` | Admin panel owners, comma-separated (more admins and roles are managed in the panel) |
| `KABALA_BILLING_URL` | The PayPal billing server (`../billing`), so the admin panel shows its subscribers and payments |
| `KABALA_BILLING_ADMIN_KEY` | The panel's Ed25519 private key (from `python -m kabala_server.admin_billing keygen`); its public half goes in billing's `ADMIN_PANEL_PUBLIC_KEY` |
| `MAIL_FROM` | The sender name shown next to the connected Gmail address (default `Kabala`) |
| `KABALA_PROXY_HOPS` | How many reverse proxies sit in front of the server (Render/Fly/Railway: `1`, plus Cloudflare: `2`). `0` (default) ignores `X-Forwarded-For` |
| `KABALA_RATE_LIMIT` | Requests per minute per IP across all routes (default `120`) |
| `KABALA_ALLOW_IPS` | Comma-separated IPs that skip all limits (e.g. uptime monitoring) |
| `KABALA_MAX_CONNECTIONS` | Open connections before uvicorn answers 503 (default `200`) |
| `KABALA_SHIELD` | `0` turns the protection off (not recommended) |
| `SELLER_NAME`, `SELLER_TAX_ID`, `SELLER_ADDRESS`, `SELLER_EMAIL` | The business details printed on Stripe receipts |
| `BUSINESS_TYPE` | `exempt` (עוסק פטור, default: "קבלה") or `licensed` (עוסק מורשה: "חשבונית מס/קבלה" with VAT broken out) |
| `VAT_RATE_PERCENT` | VAT rate for licensed dealers (default `18`) |
| `RECEIPT_NUMBER_START` | First Stripe receipt number (default `500001`, a range separate from PayPal's) |
| `RECEIPT_COPY_TO` | Default hidden copy (Bcc) of every receipt, e.g. the accountant; can be changed in the panel |
| `ADMIN_EMAIL` | Alerted by email when a receipt fails after all retries |

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

- **PayPal page:** shows subscribers, payments, this month's revenue and license keys from the PayPal billing server. For keys it shows who has one, how many times it was activated and when it was last used, with a revoke button for admins. It shows only the last group of each key, never the full key.
- **Customer receipts page:** shows every receipt the PayPal server issued after a payment (the PDF and its email are sent automatically, with retries).
  - Each receipt shows its status and its latest email; its details window shows the full email history.
  - An admin can send it again to the customer or to another address (for example the accountant), download the PDF, or retry a receipt that failed.
  - Each panel request with a body is signed together with that body (`bh`), so a captured token can't send to another address.
- **Automatic sending page:** controls how receipts are emailed. Settings are stored on the PayPal server and apply from the next receipt.
  - Turn automatic sending to the customer on or off. When it's off, the PDF is created and waits for a manual send.
  - A hidden copy of every receipt to the accountant or the business archive.
  - The subject and opening line in Hebrew and English, with a live preview, `{{variables}}` and a "send me a test email" button.
  - Failed receipts are retried automatically, up to 3 times an hour apart.
  - Templates are [mustache](https://github.com/janl/mustache.js), and only plain `{{variables}}` from a fixed list are allowed.
- **Live data:** the open page re-reads its data every 30 seconds, so new users, payments and keys appear without reloading.
  - Each request is a 60-second, single-use JWT for one path, signed by the panel with Ed25519 using [PyJWT](https://github.com/jpadilla/pyjwt), copied into `vendor/jwt/` (changes in its `KABALA_CHANGES.md`).
  - The billing server verifies it with `jose` and holds only the public key.
  - Setup: `python -m kabala_server.admin_billing keygen`, then set `KABALA_BILLING_URL`.
- **Enforcement:** the server checks the role on every request, so removing an admin takes effect immediately. Every action is recorded in the audit log.

## Automatic receipts for Stripe payments

Every paid Stripe invoice (`invoice.paid`, the first payment and each renewal) becomes one numbered receipt PDF, emailed to the customer automatically. The behaviour matches the PayPal server's receipts, and both are run from the same panel pages with a **Stripe | PayPal** switch.

- **Receipt pages:** "קבלות ללקוחות" lists receipts, shows the emails sent, downloads the PDF, and resends to the customer or to another address. "שליחה אוטומטית" sets auto-send on/off, auto-retry, a Bcc copy, and the email subject and opening line (Hebrew and English) with a live preview and a test email.
- **PDF:** built with [fpdf2](https://github.com/py-pdf/fpdf2) (LGPL, used as a dependency) with HarfBuzz shaping for Hebrew, in `kabala_server/receipts_pdf.py`. PDFs are stored encrypted (Fernet).
- **Templates:** [chevron](https://github.com/noahmorrison/chevron) (mustache for Python, MIT), copied into `vendor/chevron/` (changes in its `KABALA_CHANGES.md`).
  - Only plain `{{name}} {{number}} {{amount}} {{month}} {{plan}} {{business}}` tags are allowed.
  - Partials never read files, and line breaks are removed from the subject.
- **Reliability:** a background worker (`kabala_server/receipts.py`) sends with up to 5 attempts and backoff, then up to 3 hourly automatic retries. After that it alerts `ADMIN_EMAIL`. Each invoice gets at most one receipt, even if Stripe redelivers it.
- **Numbering:** sequential from `RECEIPT_NUMBER_START`, assigned inside a database transaction, with no gaps or duplicates. **Confirm the numbering range and the document type (receipt vs. tax invoice) with your accountant** before going live.
- **Setup:** connect Gmail (see below), set the `SELLER_*` variables, and add `invoice.paid` to the Stripe webhook events (see Deployment).

## Sending email: Gmail API only

Every email the server sends goes through the Gmail API (`kabala_server/gmail_api.py`): sign-in codes, Stripe receipts with their PDF and the Bcc copy, and admin alerts. There is no SMTP. The OAuth permission is `gmail.send` only, so the server can send but never read the mailbox. No password is stored; the refresh token is kept encrypted in the database.

**Connecting the account (admin panel → Gmail):**

1. In Google Cloud Console:
   - Enable the **Gmail API**.
   - Set up the OAuth consent screen and publish it ("In production"). In "Testing", Google expires the connection after 7 days.
   - Create an OAuth client of type **Web application**. Add the **Authorized redirect URI** the panel's Gmail page shows (`https://<server>/admin/api/gmail/callback`).
2. Set `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` on the server, and restart it.
3. Open `/admin`. While no account is connected, nobody can get a sign-in code, so the sign-in page shows **"חבר Gmail והיכנס"**. Sign in with an owner's Google account (`KABALA_OWNER_EMAILS`) and approve. This connects Gmail and signs you in.
4. Later, on the panel's **Gmail** page, an owner can connect another account (for example `billing@your-business.com`), send a test email, or disconnect. Every change is written to the audit log, and a replaced token is revoked at Google.

- **Without the panel:** set `GMAIL_REFRESH_TOKEN` instead, from `python -m kabala_server.gmail_api authorize`. That command uses a Desktop app client, or the desktop app's `google_client.json`. The PayPal billing server reads the same variables (`npm run gmail:authorize`), so one token can serve both.
- **Limits:** Gmail's daily limit applies: about 500 recipients a day for a regular account, 2,000 for Google Workspace.
- **Failures:** failed sends are retried by the receipt worker as usual. The last error from Google shows on the Gmail page and in the receipts.

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
   `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid` (for automatic receipts).
4. In the desktop app, set the server address: in a `kabala_server.json` file next to `google_client.json`:
   `{"url": "https://<server>"}`. Without it the app works in "unmanaged" mode, where every feature is open (good for development).

## Tests

```bash
python -m pytest tests
```
