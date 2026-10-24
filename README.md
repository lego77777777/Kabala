# Kabala

Kabala finds the receipts and invoices in your mailbox, downloads them, and files them into folders by company and date. It is a desktop app written in **Python + PySide6 (Qt)**, with full Hebrew/RTL support and dark mode.

> **Status: stage 4.** Connecting, scanning, detecting and saving are **real** for a signed-in Gmail account. "Demo mode" (no Google setup) still uses a simulated mailbox (`kabala/demo.py`).

## How a scan works (`kabala/scanner.py`)

1. **Search:** a single Gmail query (see "Receipt detection"). The first scan covers **the last 12 months**; later scans only cover what's new since the previous scan (plus a 3-day overlap).
2. **Download in parallel:** 4 workers fetch the messages. Rate limits (429) and server errors are retried with backoff, honoring `Retry-After`.
3. **Detection:** local rules, then Claude (or Ollama), then merging.
4. **Saving** (`kabala/files.py`):
   - A PDF attachment is saved as-is. If there are several, the ones named invoice/receipt/קבלה/חשבונית are preferred.
   - A receipt in the email body is converted to PDF with Qt (no browser and no external tools). Remote images are not loaded, to avoid tracking pixels.
   - The path is `<folder>/<company>/<year>/<year-month>/<date>_<company>_<amount><currency>.pdf` (or date first). An existing name gets `_2`; an identical file is not written twice. The write is atomic.
5. **No duplicates:** the same email (by `Message-ID`) or the same document (by SHA-256 hash, e.g. a forwarded copy) is saved only once.
6. **Index** (`kabala/store.py`): SQLite in the app data folder. It holds only extracted fields (company, date, amount, file path), never the email contents. Every message that was handled is recorded, so a rescan skips it.
7. **Manual review:** a receipt without an amount, or below the confidence threshold, goes to the "Needs review" screen. "It's a receipt" downloads it at that point and files it.

**Read-only by design:** the Gmail client only performs `GET`, and the scope is `gmail.readonly`.

## Exporting for your accountant (`kabala/export.py`)

The "Export" button in the dashboard lets you pick a period (everything / this month / last month / this year / last year) and a format:
- **Excel**:
  - **Receipts sheet**: the amounts are real numbers, each row links to its PDF, and it has a filter.
  - **Monthly summary sheet**: a column per currency, with a SUM row.
  - **By company sheet**: totals per company.
  - In Hebrew the sheets are right-to-left.
- **CSV**: UTF-8 with a BOM, so Hebrew opens correctly in Excel. File paths are relative to the receipts folder.

## Automatic scanning (`kabala/autoscan.py`)

Settings → Automatic scanning:
- **Scan in the background**: every 6/12/24/72 hours. Closing the window keeps the app running next to the clock (tray), with a menu (open / scan now / quit). A notification shows when new receipts are found.
- **Start with Windows**: adds an entry to `HKCU\...\Run` (no admin rights needed), launched with `--background` so no window opens. Turning off background scanning removes the entry. macOS will come with the installer stage.

## Connecting to Gmail: how it works

`kabala/google_auth.py` follows Google's recommendations and RFC 8252:

1. Clicking "Connect Gmail" opens Google's official consent page **in the default browser** (Chrome or whichever browser is set). There is no webview inside the app.
2. The app listens on `http://127.0.0.1:<random port>/` (loopback). After you approve, Google sends the browser back there, and the browser shows a "Connected" page.
3. The flow is protected with **PKCE (S256)** and a random `state` value. A response with a mismatched state is rejected.
4. The only scope is **`gmail.readonly`**, so the app is technically unable to delete or change mail.
5. The refresh token is stored in **Windows Credential Manager / macOS Keychain** (the `keyring` library), never in a plain file. "Disconnect" and "Disconnect & delete all data" also revoke the grant at Google.

Libraries: [google-auth-oauthlib](https://pypi.org/project/google-auth-oauthlib/) (Apache-2.0) for PKCE and the code exchange, and [keyring](https://github.com/jaraco/keyring) (MIT) for secure storage. The loopback server is written by us rather than `run_local_server`, so the user can cancel while waiting.

### Alternative: an app password, with no "unverified app" screen (`kabala/imap_gmail.py`)

Until Google verifies the app, the consent page above shows "Google hasn't verified this app", because `gmail.readonly` is a restricted scope. Code can't remove that screen. **"Connect with an app password"** avoids it:

1. The user turns on 2-Step Verification and creates an app password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
2. Kabala connects to `imap.gmail.com` with that password. The password is stored only in the OS credential vault.
3. **Read-only by construction:** the mailbox is opened with `EXAMINE` and bodies are fetched with `BODY.PEEK`, so nothing is marked as read, moved or deleted. The tests fail if any other IMAP command is sent.
4. The search is the same Gmail query (the `X-GM-RAW` extension), and message IDs are the same as the Gmail API's (`X-GM-MSGID`). The scanner, detection and filing don't change, and switching between the two methods doesn't create duplicates.
5. **Payments:** an IMAP account has no Google token, so "Upgrade to Pro" first opens a basic Google sign-in with `openid` + `email` only. These are non-sensitive scopes, so there's no warning screen. It only identifies the payer, and nothing is stored.

## Receipt detection (`kabala/detect/`)

| Stage | File | What it does |
|---|---|---|
| Search | `query.py` | A single Gmail query: Hebrew and English keywords (receipt, invoice, tax invoice, payment confirmation…), `filename:pdf`, and known billing senders |
| Parsing | `message.py` | Gmail API message → subject, sender, date, readable text (HTML→text), attachments |
| Local rules | `heuristics.py` | Score + extraction of amount, currency and invoice number, offline and at no cost. Below 0.15 the email is dropped without going to the model |
| Smart verification | `llm.py` | **Claude Haiku 5.5** (official `anthropic` SDK, structured JSON output) or **Ollama** locally. Only the subject, sender domain, date and a redacted excerpt are sent (card numbers, emails and ID numbers are masked). Attachments are never sent |
| Merging | `pipeline.py` | The model decides, the rules fill in gaps, and disagreement lowers the confidence. Below the threshold → "Needs review" |
| Company names | `normalize.py` | `Netflix.com` / `NETFLIX INC` / `Netflix` → **Netflix**; `בזק בע"מ` → **בזק**; a generic sender ("no-reply") → the domain |

The Anthropic API key is entered in Settings and stored in the OS credential vault. Without a key, the app works with local rules only.

**Accuracy:** on the 32 labeled examples in `tests/samples.py`, the local rules alone reach 31/32 (97%) on the receipt / not-receipt decision and 20/20 on amounts. These are synthetic examples I wrote. Real accuracy needs to be measured on an anonymized sample of real mail.

## Setting up Google Cloud (one-time, done by the developer)

The end user never sees these steps. They are needed once to create the app's identity with Google.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a new project (for example, `Kabala`).
2. **APIs & Services → Library**: find **Gmail API** and click **Enable**.
3. **Google Auth Platform → Branding** (the OAuth consent screen): enter an app name, a support email and a logo.
4. **Audience**: choose **External**. While the app is in **Testing**, add your own email under **Test users** (up to 100 testers).
5. **Data Access → Add or remove scopes**: add `https://www.googleapis.com/auth/gmail.readonly`.
6. **Clients → Create client**: choose type **Desktop app** and click **Create**.
7. Click **Download JSON** and save the file as **`google_client.json`** in one of these locations:
   - The project folder (`kabala-py/google_client.json`) for development
   - The app settings folder: `%APPDATA%\Kabala\Kabala\` (the "Open settings folder" button in the app takes you there)
   - Or a path set in the `KABALA_GOOGLE_CLIENT` environment variable
8. Click "Check again" in the app (or restart it) and connect.

> The file is in `.gitignore`. Don't upload it to GitHub. For a Desktop client, Google states the secret is not truly secret, but it is still better not to publish it.

### Current status of the Kabala project in Google Cloud

- Client: "Kabala Desktop" (Desktop app).
- Publishing status: **In production**. Any Google account can connect, with an "unverified app" warning, up to 100 users.
- Home page and privacy policy: [serewf191-tech.github.io/kabala](https://serewf191-tech.github.io/kabala/). The source is in `site/` and in the repository [serewf191-tech/kabala](https://github.com/serewf191-tech/kabala).

### Google verification before a public release

`gmail.readonly` is a **restricted scope**. As long as the app is in Testing:
- Only listed test users can connect, and Google shows them an "unverified app" warning.
- **The refresh token expires after 7 days**, so you need to reconnect.

To publish the app to everyone, you need to:
1. Move the app to **In production** and submit it for **OAuth verification**, with a privacy policy, a home page on a verified domain, and a demo video of the flow.
2. Pass an annual **security assessment (CASA)** by a third party approved by Google, as required for restricted Gmail scopes.
3. Show that the app follows the **Google API Services User Data Policy (Limited Use)**. Kabala's architecture, with local processing and no storage of emails on a server, makes this significantly easier.

Expect the process to take several weeks.

## Running (Windows)

Double-click `run.bat`. On the first run it creates a virtual environment and installs the dependencies (about 1–2 minutes).

Or from the terminal:

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m kabala
```

On macOS/Linux, replace `.venv\Scripts\python` with `.venv/bin/python`.

## Plans and subscriptions (stage 6)

- **Free:** 50 receipts saved per calendar month, one account, no export and no background scan.
- **Pro:** unlimited, with export to Excel/CSV and background scanning.
- **Plan server:** the plan is set by the server in [`server/`](server/README.md) (FastAPI + Stripe). The app identifies the user to it with a Google ID token only. No Gmail access token or email content ever reaches the server.
- **Upgrading:** "Upgrade to Pro" opens Stripe Checkout in Chrome. After payment Stripe sends a webhook to the server, and "Refresh status" in the app updates the plan.
- **PayPal (recommended):** the server in [`billing/`](billing/README.md) (Node/TypeScript + PayPal Subscriptions). Prices come only from `GET /api/pricing`; the license is a signed JWT, checked in the app with `license_public_key.pem` and valid offline for up to 7 days. After payment the browser opens `kabala://payment-success` and the app updates to Pro immediately. Pick the provider with `{"url": "https://<server>", "provider": "paypal"}` (or `"stripe"`).
- **Development mode:** with no `kabala_server.json` file (`{"url": "https://<server>"}`), the app runs in "development mode" and every feature is open.

## Building an installer (stage 7)

```bash
.venv\Scripts\python build_installer.py
```

- **Windows:** `dist/Kabala-Setup-<version>.exe`, built with Inno Setup 6. It installs per-user (no admin rights), in Hebrew/English, with a Start menu entry, an optional desktop shortcut, and an uninstaller that also removes the "start with Windows" entry.
- **macOS:** the same command on a Mac produces `dist/Kabala-<version>.dmg`.
- **Code signing (recommended before distribution):** set `SIGN_CMD` (for example `signtool sign /fd sha256 /a {file}`) or `MAC_SIGN_IDENTITY`. Without a signature, Windows SmartScreen shows a warning.
- **Bundled configuration:** `google_client.json` and `kabala_server.json`, if present, are included in the installation automatically.

## Tests

```bash
.venv\Scripts\python -m pytest
```

## Structure

```
kabala/
  app.py           Entry point: QApplication, font, theme
  backend.py       App state and long-running work (scan in a background thread), exposed through Qt signals
  demo.py          Simulated mailbox (stage 1 only)
  models.py        Data classes: Receipt, ScanProgress, ScanSummary…
  paths.py         Folder structure and safe file names (Windows/Hebrew)
  settings.py      User preferences (JSON in the app data folder)
  theme.py         Colors for light/dark, QSS stylesheet, following the system theme
  i18n.py          Hebrew/English strings, currency and date formatting
  ui/
    widgets.py     Design system: Button, Card, Segmented, Badge, EmptyState…
    icons.py       SVG icons that render in any color
    components.py  Folder preview, monthly bar chart
    sidebar.py, welcome.py, scan.py, dashboard.py, pages.py, main_window.py
tests/             pytest
```

## Keyboard shortcuts

- `Ctrl+1`–`Ctrl+6`: switch between screens
- `/` or `Ctrl+F`: search in the dashboard
- `Tab` / `Enter` / `Space`: navigate and activate everything, including the sidebar and lists
- `Esc`: close a dialog
