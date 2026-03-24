# BetterWiser Legal-Tech AI Briefing Agent — Setup Guide

## Overview

The agent runs **locally on your machine** (or any server). It is **not deployed in Azure** — Azure AD is used only for OAuth2 authentication with the Microsoft 365 email API (Microsoft Graph).

```
Your Machine  →  Anthropic API (Claude)
              →  Azure AD (OAuth only) → Microsoft Graph → Your M365 Mailbox
              →  Jina / Spider / Crawl4AI (web scraping)
              →  Tavily (research)
```

---

## Step 1: Python Environment (Required)

⚠️ **Use Python 3.12, not 3.14.** Some packages (`weasyprint`, `crawl4ai`) do not yet have Python 3.14 wheels.

```bash
# Option A: conda (recommended)
conda create -n bw-briefing python=3.12
conda activate bw-briefing

# Option B: venv
python3.12 -m venv .venv
source .venv/bin/activate    # Linux/Mac
.venv\Scripts\activate       # Windows
```

---

## Step 2: Install Dependencies

```bash
cd betterwiser_briefs_agent
pip install -r requirements.txt

# Install playwright browsers (needed for Crawl4AI Tier 3 scraping)
python -m playwright install chromium --with-deps
```

---

## Step 3: Configure API Keys (Minimum: Anthropic only)

```bash
cp .env.example .env
```

Edit `.env` and add your keys:

```env
# REQUIRED — minimum to run dry-run
ANTHROPIC_API_KEY=sk-ant-...

# OPTIONAL — for inbox reading and email sending (see Step 4)
AZURE_TENANT_ID=
AZURE_CLIENT_ID=
AZURE_CLIENT_SECRET=
AZURE_USER_EMAIL=ai-briefing@betterwiser.com

# OPTIONAL — Tier 2 web scraping (pay-as-you-go, fallback to Jina if missing)
SPIDER_API_KEY=

# OPTIONAL — Thought leadership deep research Wave 4
TAVILY_API_KEY=
```

---

## Step 4: Test Without Azure (Works Immediately)

With only `ANTHROPIC_API_KEY` set, the agent gathers from the web only (no inbox):

```bash
# Generate Track C (Thought Leadership) for current month — dry run
python -m src.orchestrator --track C --dry-run

# Generate all tracks
python -m src.orchestrator --dry-run

# Specify a month
python -m src.orchestrator --month 2026-03 --dry-run
```

Briefings are saved to: `runs/{run_id}/delivery/track_A.html` etc.

---

## Step 5: Azure AD Setup (For Email Features)

You need an **Azure AD App Registration** to:
- Read emails from the agent's dedicated inbox (inbox intelligence)
- Send briefings via email (delivery)

### 5.1 Register an Azure AD Application

1. Go to [portal.azure.com](https://portal.azure.com)
2. Navigate to: **Azure Active Directory** → **App registrations** → **New registration**
3. Fill in:
   - **Name**: `BetterWiser Briefing Agent`
   - **Supported account types**: `Accounts in this organizational directory only`
   - **Redirect URI**: Leave blank (we use client credentials flow — no browser login)
4. Click **Register**
5. Note down:
   - **Application (client) ID** → this is your `AZURE_CLIENT_ID`
   - **Directory (tenant) ID** → this is your `AZURE_TENANT_ID`

### 5.2 Create a Client Secret

1. In the app registration → **Certificates & secrets** → **New client secret**
2. Description: `briefing-agent-secret`
3. Expiry: `24 months`
4. Click **Add**
5. **Copy the secret VALUE immediately** — it is only shown once
6. This is your `AZURE_CLIENT_SECRET`

### 5.3 Grant API Permissions

1. In the app registration → **API permissions** → **Add a permission**
2. Choose: **Microsoft Graph** → **Application permissions**
3. Add these permissions:
   - `Mail.Read` — to read the inbox
   - `Mail.Send` — to send briefing emails
   - `Files.ReadWrite.All` — optional, for SharePoint archiving
4. Click **Grant admin consent for [your tenant]** (requires Global Admin role)
5. Verify all permissions show **Granted** status

### 5.4 Create the Dedicated Mailbox

1. In Microsoft 365 Admin Center, create a shared mailbox: `ai-briefing@betterwiser.com`
   (or use a licensed user mailbox)
2. Set `AZURE_USER_EMAIL=ai-briefing@betterwiser.com` in `.env`
3. Subscribe the mailbox to all newsletters listed in `config/newsletter_subscriptions.yaml`
   (takes ~15 minutes manually)

### 5.5 Update .env

```env
AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_SECRET=your-secret-value
AZURE_USER_EMAIL=ai-briefing@betterwiser.com
```

---

## Step 6: Run with Email Sending

```bash
# Send all tracks for current month
python -m src.orchestrator --send

# Send specific track for a specific month
python -m src.orchestrator --month 2026-03 --track C --send
```

---

## Step 7: Monthly Scheduling

### Option A: Windows Task Scheduler (runs locally)
1. Open Task Scheduler → Create Basic Task
2. Name: `BetterWiser Briefing Agent`
3. Trigger: Monthly (1st of each month, 08:00 AM SGT)
4. Action: `python -m src.orchestrator --send`
5. Start in: `C:\Users\chuan\betterwiser_briefs_agent`

### Option B: Cron (Mac/Linux)
```bash
# Run at 08:00 on the 1st of each month (Singapore time = UTC+8 = UTC 00:00)
0 0 1 * * cd /path/to/betterwiser_briefs_agent && .venv/bin/python -m src.orchestrator --send
```

### Option C: GitHub Actions (runs in the cloud, free for private repos)
See `.github/workflows/monthly_briefing.yml` (create this file if needed).

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `ANTHROPIC_API_KEY not set` | Add to `.env` file and retry |
| `Azure AD credentials incomplete` | Inbox + email disabled; briefings still generate to disk |
| `All scrapers failed for URL` | Check if URL is accessible; some sites block bots |
| `Grounding below threshold` | Briefing saved to disk but NOT sent; review at `runs/.../delivery/` |
| `weasyprint not installed` | Normal — HTML output still works; weasyprint only needed for PDF |
| `crawl4ai` playwright error | Run `python -m playwright install chromium --with-deps` |
| `msgraph-sdk` import error | Run `pip install msgraph-sdk>=1.2.0` |

---

## Project Structure

```
betterwiser_briefs_agent/
├── config/
│   ├── briefing_config.yaml          ← Master config (edit recipients here)
│   ├── betterwiser_context.txt       ← Company context for Track C
│   ├── newsletter_subscriptions.yaml ← Inbox filtering rules
│   ├── vendor_watchlist.yaml         ← Vendor + thought leader watchlists
│   └── prompt_templates/             ← Track-specific Claude system prompts
├── src/
│   ├── orchestrator.py               ← CLI entry point: python -m src.orchestrator
│   ├── schemas.py                    ← All Pydantic v2 data models
│   ├── gatherers/                    ← Phase 2: data gathering (5 sub-pipelines)
│   ├── synthesis/                    ← Phase 3: 6-pass synthesis pipeline
│   ├── delivery/                     ← Phase 5: email + archive
│   └── utils/                        ← Shared utilities
├── runs/                             ← Output directory (auto-created)
│   └── 2026-03_run_20260301T080000/
│       ├── run.log
│       ├── raw_data/
│       ├── synthesis/
│       └── delivery/
│           ├── track_A.html          ← Your briefing!
│           ├── track_B.html
│           └── track_C.html
├── tests/
├── .env                              ← Your credentials (never commit)
├── .env.example                      ← Template
├── requirements.txt
└── Dockerfile
```

---

## Costs

| Component | Cost per run |
|-----------|-------------|
| Claude API (claude-opus-4-6) | ~$15–$20 (standard) / ~$8–$10 (batch API) |
| Claude web searches (150–250) | ~$1.50–$2.50 |
| Tavily deep research | ~$0.50–$1.00 |
| Jina Reader | Free |
| Spider API (~50 pages) | ~$0.02 |
| Microsoft Graph | Free |
| **Total per monthly run** | **~$17–$24** |

To reduce costs: use `--track C` to run only Track C (the most expensive due to deep research).
