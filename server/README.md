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
