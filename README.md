# Invoice Tracker (IMAP + OpenAI)

**VIBE CODED - USE AT YOUR OWN RISK**

This project ingests emails from an IMAP inbox, detects invoices/receipts, extracts key fields with OpenAI, and stores everything in SQLite.

## Features

- Connects to IMAP (`INBOX` or configured mailbox).
- Reads email body and attachments.
- Detects invoices/receipts from PDF text first, then email text.
- Converts each PDF page to images and sends page images to the LLM (not PDF files).
- Extracts:
  - `date`
  - `product`
  - `company`
  - `price`
  - `vat` (or similar tax values)
- If no PDF attachment exists:
  - attempts direct-download links from the email
  - otherwise stores dashboard links
- Stores raw email, files, links, and extracted structured data in a SQLite database.
- Includes a local web dashboard for browsing extracted records.
- Fully configurable through YAML.
- Resume support (continue from last successful IMAP UID).
- Parallel processing for parsing/extraction.
- LLM pre-filter: non-invoice/non-receipt emails are skipped and not stored.
- Reply/answer thread emails (`In-Reply-To` or `Re:`/`AW:` style subjects) are skipped.

## Setup

1. Create a config file:

```powershell
Copy-Item config.example.yaml config.yaml
```

2. Set required environment variables.

`src/main.py` automatically loads a local `.env` file (if present), so you can either export variables in your shell or place them in `.env`.

```powershell
$env:IMAP_PASSWORD="your-imap-password"
$env:OPENAI_API_KEY="your-openai-api-key"
```

3. Adjust `config.yaml` values (IMAP host/user/mailbox, storage paths, model, limits).

4. Install dependencies and run:

```powershell
uv sync
uv run python src/main.py --config config.yaml
```

Launch the dashboard against your configured database:

```powershell
uv run python src/main.py --config config.yaml --dashboard
```

Default dashboard URL: `http://127.0.0.1:8787`

## YAML Configuration

Use [`config.example.yaml`](/e:/invoice_tracker/config.example.yaml) as the template.

Important keys:

- `imap.*`: server, credentials env var, mailbox, search criteria.
  - `imap.auth_method`: `password` or `oauth2`
  - `imap.oauth.*`: OAuth token env names and optional refresh-token flow settings
- `openai.*`: model, API key env var, optional `base_url` for OpenAI-compatible providers.
  - `openai.base_url` must be the provider API root (example for OpenRouter: `https://openrouter.ai/api/v1`), not a full endpoint like `/chat/completions`.
- `storage.*`: database and file paths.
- `processing.*`: char limits, link download behavior, dashboard keywords.
  - `processing.pdf_image_dpi`: render DPI for PDF page images
  - `processing.max_pdf_pages_for_llm`: max number of page images sent to LLM per email
  - `processing.parallel_workers`: number of worker threads for message parsing/extraction
  - `processing.max_in_flight`: cap for queued/running messages
  - `processing.resume_enabled`: skip already-processed UIDs using saved checkpoint
  - `processing.resume_state_path`: JSON checkpoint file path

## IMAP OAuth2

This app supports both:

1. Direct access token from env (`imap.oauth.access_token_env`)
2. Automatic refresh-token exchange (`refresh_token_env + client_id_env + client_secret_env + token_url`)

OAuth providers do not normally give you a token from a single dashboard button. You create an OAuth app in the dashboard, then run an OAuth authorization flow once to get `access_token` and `refresh_token`.

### Gmail / Google Workspace (IMAP)

1. Open Google dashboards:
   - OAuth clients: https://console.cloud.google.com/auth/clients
   - OAuth consent setup: https://console.cloud.google.com/auth/overview
   - Gmail API page: https://console.cloud.google.com/apis/library/gmail.googleapis.com
2. In Google Cloud, create/select a project and enable Gmail API.
3. Configure OAuth consent screen in the Auth dashboard.
4. Create an OAuth client (Desktop app is simplest for manual token bootstrap).
5. Open OAuth Playground: https://developers.google.com/oauthplayground/
6. In OAuth Playground settings (gear icon), enable "Use your own OAuth credentials" and paste your client ID + client secret.
7. Request scope `https://mail.google.com/`, authorize, then exchange the code for tokens.
8. Copy the returned `access_token` and `refresh_token`.
9. Put them in `.env` and `config.yaml`.

Example `.env`:

```env
IMAP_ACCESS_TOKEN=...
GOOGLE_REFRESH_TOKEN=...
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
```

Example `config.yaml` OAuth block:

```yaml
imap:
  auth_method: "oauth2"
  oauth:
    access_token_env: "IMAP_ACCESS_TOKEN"
    refresh_token_env: "GOOGLE_REFRESH_TOKEN"
    client_id_env: "GOOGLE_CLIENT_ID"
    client_secret_env: "GOOGLE_CLIENT_SECRET"
    token_url: "https://oauth2.googleapis.com/token"
    scope: "https://mail.google.com/"
```

### One-command Gmail browser login (implemented in this project)

If you already have `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, you can let this app open the consent page and capture tokens automatically.

1. Add your OAuth client credentials to `.env`:

```env
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
```

2. Ensure your Google OAuth client allows redirect URI:

```text
http://127.0.0.1:53682/oauth2/callback
```

3. Run:

```powershell
uv run python src/main.py --setup-gmail-oauth
```

4. Browser opens Google consent screen. Approve access.
5. Tool writes tokens to `.env` by default:
   - `IMAP_ACCESS_TOKEN`
   - `GOOGLE_REFRESH_TOKEN` (if returned)

Optional flags:

```powershell
uv run python src/main.py --setup-gmail-oauth --redirect-port 53683 --no-save-env
```

### Microsoft 365 / Outlook (IMAP)

1. Open Microsoft dashboards:
   - App registrations list: https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade
   - Entra admin center: https://entra.microsoft.com/
2. Register a new app in App registrations.
3. Add a redirect URI for your authorization-code flow (for example `http://localhost:53682/`).
4. Create a client secret in Certificates & secrets.
5. Add delegated API permissions:
   - `IMAP.AccessAsUser.All` (Office 365 Exchange Online)
   - `offline_access`
6. Build and open this authorize URL (replace placeholders):

```text
https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http%3A%2F%2Flocalhost%3A53682%2F&response_mode=query&scope=offline_access%20https%3A%2F%2Foutlook.office.com%2FIMAP.AccessAsUser.All
```

7. Copy the returned authorization `code` from your redirect URL and exchange it for tokens:

```bash
curl -X POST "https://login.microsoftonline.com/common/oauth2/v2.0/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "client_secret=YOUR_CLIENT_SECRET" \
  -d "grant_type=authorization_code" \
  -d "code=AUTH_CODE" \
  -d "redirect_uri=http://localhost:53682/" \
  -d "scope=offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
```

8. Save `access_token` and `refresh_token` in `.env`.

Example `.env`:

```env
IMAP_ACCESS_TOKEN=...
MS_REFRESH_TOKEN=...
MS_CLIENT_ID=...
MS_CLIENT_SECRET=...
```

Example `config.yaml` OAuth block:

```yaml
imap:
  auth_method: "oauth2"
  oauth:
    access_token_env: "IMAP_ACCESS_TOKEN"
    refresh_token_env: "MS_REFRESH_TOKEN"
    client_id_env: "MS_CLIENT_ID"
    client_secret_env: "MS_CLIENT_SECRET"
    token_url: "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    scope: "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
```

## Resume and parallel processing

- Resume writes a checkpoint file (default: `data/resume_state.json`) keyed by mailbox.
- On restart, emails with UID `<= last_uid` are skipped when `processing.resume_enabled: true`.
- Checkpoint advances only after successful processing/saving, so failed earlier emails are retried on next run.
- Parallel execution is controlled by:
  - `processing.parallel_workers`
  - `processing.max_in_flight`

## Dashboard

The dashboard is a built-in local web UI that reads from the same SQLite database
configured in `storage.database_path`.

Run:

```powershell
uv run python src/main.py --config config.yaml --dashboard
```

Optional host/port overrides:

```powershell
uv run python src/main.py --config config.yaml --dashboard --dashboard-host 0.0.0.0 --dashboard-port 9000
```

It provides:

- Snapshot totals (emails, invoices, receipts, documents, links)
- Spend-by-currency chips
- Monthly spend trend chart
- Search and filters (type, company, currency, sorting)
- Paginated record table with detail panel
- Direct open/download links for saved documents and raw `.eml` files

## Storage behavior

- Each email is first sent to the LLM using subject/body only (pre-filter stage).
- Reply/answer emails are skipped before the LLM pre-filter, so only original emails are considered.
- If LLM says it is not an invoice/receipt, the email is skipped and nothing is written to the DB.
- Only emails classified as invoice/receipt continue to full parsing (attachments, PDF page rendering, links) and persistence.
- For PDFs, each page is rendered as an image (`.png`) and those images are sent to the LLM.

## Database schema

The SQLite database contains:

- `emails`: metadata + normalized body text + raw `.eml` path
- `documents`: attachments/downloads + extracted PDF text
- `links`: extracted links and type (`direct_download`, `dashboard`, `other`)
- `extractions`: structured invoice/receipt result and raw JSON response
