# Kabala Billing (PayPal)

The server for subscriptions and payments: prices, PayPal Subscriptions, webhooks, signed licenses for the desktop app, invoices and email.
Node 20+ / TypeScript · Fastify · PostgreSQL + Drizzle (PGlite for local development) · the official PayPal SDK.

> The Stripe server (`../server`) stays alongside it. The desktop app chooses which one to work with through `kabala_server.json` (`"provider": "paypal"` or `"stripe"`).

## Libraries (checked in October 2026)

| Purpose | Package | Version | License |
|---|---|---|---|
| Plans and subscriptions on the server | `@paypal/paypal-server-sdk` (official) | 2.5.0 | MIT |
| PayPal buttons in the browser | `@paypal/paypal-js` (official) | 11.3.0 | Apache-2.0 |
| Products (Catalog) + webhook verification | REST API with `fetch` + `zod` (missing from the SDK) | — | — |

The old libraries `checkout-server-sdk` and `paypal-rest-sdk` are officially deprecated and are not used.
`@paypal/react-paypal-js` was not needed, because the payment pages are plain HTML/JS. `paypal-js` is the same official loader that it wraps.

## Encryption in transit and at rest

- **PostgreSQL:** TLS is required in production (`ssl: "require"`, for pg-boss too). `?sslmode=disable` in `DATABASE_URL` is the only way to turn it off, for a database on the same private host.
- **SMTP:** `requireTLS`, so `smtp://` must upgrade with STARTTLS or the email isn't sent. Plain text is allowed only for a local test server such as Mailpit.
- **S3:** receipt PDFs are uploaded with `ServerSideEncryption: AES256` on AWS. R2 always encrypts, so it's off by default with `S3_ENDPOINT`. Override with `S3_SSE`.
- **Access logs:** the signature of `/r/…` receipt links is replaced with `[redacted]`, because the link alone downloads the PDF.
- **`.env`:** `npm run setup` and `gmail:authorize` write it owner-only (0600), since it holds the license signing key and the Gmail refresh token.
- **Forged webhooks:** recorded without their body.

## License keys (`src/licenses/keys.ts`)

Every buyer gets a key like `KBL-8B98Z-25XBR-R6S2E-TGEH8` with their first payment. Later payments keep the same key.
- **Where they see it:** it arrives by email and is also on the "My subscription" page, with Copy and New key buttons.
- **No key needed after paying in the app:** the payment belongs to the signed-in account. When PayPal returns to the app (`kabala://payment-success`), it keeps checking the server until Pro is active, up to about a minute (`Backend.activate_after_payment`).
- **On another computer:** Subscription → "I have a license key (from another computer)" activates Pro without signing in (`POST /api/license/activate`).

**Generation:** [nanoid](https://github.com/ai/nanoid), copied into `src/vendor/nanoid/` (changes in its `KABALA_CHANGES.md`).
- 100 random bits over the Crockford alphabet, so 0/O and 1/I can't be confused.
- Typing is forgiving: lower case and spaces work.

**Storage:** the key is never stored in plain text.
- `key_hash` (HMAC-SHA256) finds it on activation.
- `key_encrypted` (AES-256-GCM) lets its owner see it again.
- The admin panel only gets the last group.
- Both use keys derived from `LICENSE_KEY_SECRET`, or from the license signing key if that is empty.

**Protection:**
- Activation allows 10 tries per 10 minutes per IP.
- "New key" (the user) or "Revoke" (an admin in the panel) stops the old key at once.

## Admin panel API (`src/admin/admin-api.ts`)

The admin panel at `server/ /admin` reads `GET /api/admin/stats`, `/subscribers` and `/payments` to show PayPal subscribers, payments and revenue. These endpoints are read-only.

- **When it's on:** only when `ADMIN_PANEL_PUBLIC_KEY` is set (the panel's Ed25519 public key, from `python -m kabala_server.admin_billing keygen`). Without it the endpoints don't exist.
- **Authentication:** every request is a JWT signed by the panel, checked with `jose`:
  - issuer and audience
  - EdDSA only
  - at most 60 seconds old
  - bound to the method and path
  - single-use (`jti`)
- **Viewers:** a viewer token gets masked emails and no PayPal ids.
- **Receipts:** `GET /api/admin/receipts[/:id[/pdf]]`, plus `POST /api/admin/receipts/:id/send {to?}` and `POST /api/admin/receipts/:id/retry`.
  - The body is bound to the token (`bh`).
  - Typed addresses go through validator.js `isEmail` (copied into `src/vendor/validator/`), with stricter options in `src/email/address.ts`: no display names, no IP domains, and no `,` `;` `<` `>`, since recipients are stored comma-joined.

## Single source of truth for prices

Everything is defined in **[`src/config/pricing.config.ts`](src/config/pricing.config.ts)**: Free (50 receipts/month, one account) and Pro at ₪29/month or ₪279/year (about 20% off). USD prices are ready but disabled until you add `"USD"` to `currencies`.

```bash
npm run sync-pricing     # make PayPal match the file and verify
npm run pricing:check    # verify only; exits with code 1 on a mismatch (for CI / deploy)
```

- **First run:** creates a Product, plus a monthly and a yearly Billing Plan for each currency, and stores the `plan_id`s in the `plans` table.
- **Price change:** edit the number in the file and run `sync-pricing`. A **new** plan is created (an existing plan's price is never changed) and the old plan is deactivated.
  - Existing subscribers keep paying the old price (grandfathering).
  - New subscribers get the new price.
  - With `grandfather: false`, the script lists the subscriptions that need a revise (that requires the customer's consent at PayPal).
- **One source for every display:** `GET /api/pricing` feeds the pricing page, the checkout page and the desktop app. There are no prices written in the UI.

## Payment flow

1. In the app, "Upgrade to Pro" calls `POST /api/links`, which returns a link valid for 30 minutes. It opens in Chrome, not in a webview.
2. The `/checkout` page shows monthly/yearly with the price from the API, plus the PayPal Subscriptions button.
3. `createSubscription` asks the server for the plan (`/api/checkout/start`). **The amount is chosen on the server.**
4. After approval in PayPal's window, `/api/checkout/confirm` checks the subscription against the **PayPal API**:
   - The plan is ours, in the same mode.
   - `custom_id` is this user.
   - The status is `ACTIVE`.

   Only then does the user get Pro.
5. The browser opens `kabala://payment-success`. The app registers the protocol and receives it, even when it's already open, and updates to Pro immediately.
6. `PAYMENT.SALE.COMPLETED` records the payment, issues a PDF invoice with a running number, and sends it by email.

## Webhooks: `POST /api/webhooks/paypal`

- **Signature verification:** every event is checked with the [Verify Webhook Signature API](https://developer.paypal.com/api/rest/webhooks/rest/). An event that isn't verified is rejected with 400 and stored in the audit table under a separate key.
- **Audit and idempotency:** each event is stored in `webhook_events` before it's processed. `event_id` is the primary key, so a duplicate event returns `duplicate` and isn't processed again. A second event about the same sale doesn't create a second payment either, because `paypal_sale_id` is unique.
- **Authoritative state:** subscription events fetch the subscription from PayPal again, so it doesn't matter if events arrive out of order.
- **Amount checks:** every amount is checked against our plan. A mismatch isn't processed and is logged with an error.
- **Retries:** if PayPal is unavailable we return 500, and PayPal retries.

| Event | What happens |
|---|---|
| `BILLING.SUBSCRIPTION.ACTIVATED` | active/trial (Pro) |
| `BILLING.SUBSCRIPTION.PAYMENT.FAILED` | past_due + 7 days of grace + email (once) |
| `BILLING.SUBSCRIPTION.SUSPENDED` | past_due (end of grace → Free) |
| `BILLING.SUBSCRIPTION.CANCELLED` | cancelled: Pro until the end of the paid period |
| `BILLING.SUBSCRIPTION.EXPIRED` | expired |
| `PAYMENT.SALE.COMPLETED` | payment + invoice + email, back to active |
| `PAYMENT.SALE.REFUNDED` | full refund → expired; partial → recorded |

**Daily reconcile:** `npm run reconcile`, scheduled once a day. It compares every active subscription against PayPal and fixes drift. For example, a subscription that's "ACTIVE" in PayPal but has failed charges counts as past_due for us. It also expires subscriptions whose grace or paid period is over.

**Ending a subscription doesn't delete anything.** The receipts already filed on the user's computer stay theirs. Only new scans are limited.

## Automatic receipts

After every verified payment (first payment and every renewal) the customer automatically gets a receipt by email, with a PDF attached. A refund automatically gets a refund document.

```
verified PAYMENT.SALE.COMPLETED ─▶ receipt (pending, running number) ─▶ job receipt-generate
  ─▶ provider issues the PDF ─▶ storage ─▶ generated ─▶ job receipt-email ─▶ customer (+ accountant) ─▶ sent
```

| Part | Where | Notes |
|---|---|---|
| Trigger | `src/services/billing.ts` | Only `PAYMENT.SALE.COMPLETED` / `PAYMENT.SALE.REFUNDED` from a verified webhook. `BILLING.SUBSCRIPTION.ACTIVATED` alone is not a payment, so it doesn't trigger a receipt. |
| Numbering and idempotency | `src/receipts/service.ts`, `numbering.ts` | One document per `paypal_transaction_id` (unique). The number is taken in the same transaction, with a row lock, so there are no gaps and no duplicates. |
| Amounts | `src/receipts/amounts.ts` | From the actual PayPal payment, checked against the plan (`pricing.config.ts`). Exempt dealer: no VAT. Licensed dealer: VAT is broken out of the price. |
| Queue | `src/queue/queue.ts` (pg-boss) | On the same PostgreSQL, no Redis. 5 attempts with exponential backoff, then `failed` plus an email to `ADMIN_EMAIL`. |
| Safety net | `ReceiptPipeline.sweep()` | Every 5 minutes, and in `npm run reconcile`: a payment without a receipt gets one, and a receipt stuck in pending/generated is queued again. |
| PDF | `src/pdf/receipt-document.tsx` | @react-pdf/renderer with embedded Heebo. Hebrew and English. |
| Email | `src/email/templates/receipt-email.tsx` | React Email: right-to-left, dark mode, HTML plus plain text. Subject: "הקבלה שלך מ-Kabala – <month year>". |
| Storage | `src/storage/` | `local` or `s3` (S3/R2), never public. PDFs are served only to the account owner or through a signed link (HMAC, 30 days in emails). |

### Choosing a provider (`RECEIPT_PROVIDER`)

- `internal`: our own PDF. For development, tests, and customers outside Israel.
- `morning`: **Morning (Green Invoice)** issues the legal document (400 receipt for an exempt dealer, 320 tax invoice-receipt for a licensed dealer, 330 credit), and Kabala stores and emails it.
  1. Create an API key in Morning: Settings → Developer tools → API keys. Put it in `MORNING_API_KEY_ID` / `MORNING_API_SECRET`.
  2. The **sandbox is a separate account** at `app.sandbox.d.greeninvoice.co.il` with its own keys. Production keys return 401 there.
  3. Morning has no external-id field, so the server saves Morning's document id immediately after creation, and a retry reuses it. The Kabala id is also written in the document's remarks field.
  4. For an exempt dealer, a refund document is issued internally (Morning has no credit invoice for an exempt dealer). **Check the right document for refunds with your accountant.**

### Sending domain (so it doesn't land in spam)

`MAIL_FROM` must be on a domain you own (for example `billing@kabala.app`). Add these records at your DNS provider:

| Record | Host | Value |
|---|---|---|
| SPF (TXT) | `@` | `v=spf1 include:<your sending service> ~all` (Resend: `include:amazonses.com` on the `send` subdomain, as shown in their dashboard) |
| DKIM (TXT/CNAME) | from the sending service | the key the service gives you |
| DMARC (TXT) | `_dmarc` | `v=DMARC1; p=quarantine; rua=mailto:dmarc@<domain>` (start with `p=none` for a week) |

- **Without your own domain yet:** `SMTP_URL=smtps://you@gmail.com:<app-password>@smtp.gmail.com:465` sends from your Gmail, signed by Google, up to about 500 emails a day.
- **Resend:** `RESEND_API_KEY` plus a domain verified in their dashboard.

### Local testing

```bash
mailpit                                   # SMTP on :1025, inbox at http://localhost:8025 (github.com/axllent/mailpit)
# .env: SMTP_URL=smtp://127.0.0.1:1025
npm run demo:receipt -- dana@example.com he yearly   # a simulated payment through the whole flow
npm run preview:receipt                   # sample PDFs (Hebrew/English, exempt/licensed, refund) in .data/preview
npm run email:dev                         # live preview of the email template at http://localhost:3001
```

## License for the desktop app

- **What it is:** a JWT signed with **Ed25519**. The private key exists only on the server. It contains the plan, limits, `paidUntil` and expiry.
- **Offline:** the app checks it with the public key (bundled in the app as `license_public_key.pem`) and works offline for up to 7 days. After that it needs a renewal (`GET /api/license`).
- **Identity:** the user is identified with a Google ID token from the Gmail sign-in. There are no passwords.
- **Secrets:** no PayPal secret ever reaches the app. The app only knows the server's address.

## Setting up the PayPal Sandbox (one-time)

1. **Create an app:** go to [developer.paypal.com](https://developer.paypal.com), open **Apps & Credentials**, choose **Sandbox**, then **Create App** (Merchant). Copy the **Client ID** and **Secret** into `PAYPAL_SANDBOX_CLIENT_ID` / `PAYPAL_SANDBOX_CLIENT_SECRET`.
2. **Test accounts:** under **Testing Tools**, then **Sandbox Accounts**, there's a Personal account (the buyer) and a Business account (you). Log in to checkout with the Personal one.
3. **Keys and env:**
   ```bash
   npm install
   cp .env.example .env            # fill in the values
   npm run keys:generate           # LICENSE_* into .env; the public key → ../license_public_key.pem
   npm run db:migrate
   npm run sync-pricing
   npm run dev
   ```
4. **Public address for webhooks:** with ngrok, `ngrok http 8080` and put the `https://…` address in `PUBLIC_URL`.
5. **Register the webhook:** in the app at developer.paypal.com, go to **Webhooks**, then **Add Webhook**: `https://<PUBLIC_URL>/api/webhooks/paypal`. Select the 7 events above and copy the **Webhook ID** into `PAYPAL_SANDBOX_WEBHOOK_ID`.
6. **Connect the desktop app:** in `kabala-py/kabala_server.json`:
   ```json
   {"url": "https://<PUBLIC_URL>", "provider": "paypal"}
   ```
7. **Full test:** Kabala → Plan → "Upgrade to Pro" → pay with the Sandbox Personal account → the app updates to Pro.

> **Simulating webhooks:** PayPal's Webhook Simulator sends "mock" events, and PayPal itself states that those can't be verified with the Verify API. So the server will reject them, as it should. For a real test, use a real Sandbox subscription: create one, cancel it from the Sandbox Personal account, or run a refund from the Sandbox Business account. Those send real, signed events. All the processing logic is covered by unit tests with a simulated PayPal.

## Tests

```bash
npm test               # 30 unit/integration tests (PGlite + simulated PayPal)
npm run test:sandbox   # against the real PayPal Sandbox (runs only when credentials are set)
npm run typecheck
npm run preview        # local preview of the pages with sample data, no PayPal needed
```

## Going Live

1. Create a **Live** app at developer.paypal.com. This requires an approved PayPal Business account.
2. Set `PAYPAL_LIVE_CLIENT_ID/SECRET/WEBHOOK_ID` and register a Live webhook at the production address.
3. Set `PAYPAL_MODE=live`, `NODE_ENV=production`, a `PUBLIC_URL` with https, and a real PostgreSQL `DATABASE_URL`.
4. Run `npm run db:migrate && npm run sync-pricing`. Live has separate plans; the `plans` table keeps a separate row per mode.
5. Schedule `npm run reconcile` daily, and add `npm run pricing:check` to your deployment.
6. Receipts: an official Israeli receipt or tax invoice needs a certified service. Set `RECEIPT_PROVIDER=morning` with production keys and `MORNING_API_URL=https://api.greeninvoice.co.il/api/v1`, plus a sending domain with SPF, DKIM and DMARC (see "Automatic receipts").

## Security

- **Full separation between Sandbox and Live:** different variables, selected only by `PAYPAL_MODE`.
- **Secrets only on the server:** the app and the browser only receive the Client ID.
- **Network:** HTTPS is enforced in production (requests without https are refused), with HSTS and a CSP that only allows PayPal. Rate limiting is applied to every endpoint, and more strictly to the sensitive ones.
- **Logging:** logs exclude the `Authorization` header, the webhook signature, request bodies, and SDK request/response bodies.
- **Browser links:** the token is in the URL fragment (`#t=`), so it doesn't reach any server, log or Referer. It's valid for 30 minutes.
