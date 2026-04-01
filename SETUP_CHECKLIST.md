# BetterWiser Briefing Agent — Complete Setup Checklist

Everything you need, in order, from zero to a running agent.

---

## What You Actually Need

```
MINIMUM — verify pipeline works (demo run):
  ✅ Python 3.12 environment
  ✅ Anthropic API key  (~$0.10 for demo, ~$8–14/month for real runs)
  → Run: python demo_run.py

MINIMUM — generate real briefings saved to disk:
  ✅ Python 3.12 environment
  ✅ Anthropic API key  (~$8–14/month in API costs)
  (RSS feeds and Wayback CDX link verification are free, no key needed)

TO READ NEWSLETTERS FROM INBOX + SEND EMAIL:
  + Microsoft Azure AD app registration (free setup)
  + A dedicated Microsoft 365 mailbox

OPTIONAL — improve content quality:
  + Tavily API key     (Track C deep research, ~$0.50/run)
  + Spider API key     (better web scraping fallback, ~$0.02/run)

TO RUN AUTOMATICALLY EVERY MONTH WITH NO INTERVENTION:
  + GitHub account (free) + push code to a GitHub repo
```

---

## PART 1 — Python Environment

### 1.1 Install Miniconda

Download and install from: https://docs.conda.io/en/latest/miniconda.html
(choose the Windows 64-bit installer — accept all defaults)

### 1.2 Create the Python 3.12 environment

Open **Anaconda Prompt** (search in the Windows Start menu) and run:

```bash
conda create -n bw-briefing python=3.12
conda activate bw-briefing
```

> **Why 3.12?** Two packages (`weasyprint`, `crawl4ai`) do not yet publish Python 3.14 wheels.
> Using 3.12 avoids installation failures.

### 1.3 Install Python dependencies

```bash
cd c:\Users\chuan\betterwiser_briefs_agent
pip install -r requirements.txt
```

This takes 3–5 minutes. Some warnings are normal.

### 1.4 Install browser for web scraping

```bash
python -m playwright install chromium --with-deps
```

### 1.5 Tell VS Code which Python to use (optional)

1. Open VS Code in the project folder
2. Press `Ctrl+Shift+P` → type **Python: Select Interpreter**
3. Choose the `bw-briefing` conda environment

> The package warnings in VS Code's editor ("not installed") will disappear after this step.

---

## PART 2 — Anthropic API Key (Required)

### 2.1 Create your API key

1. Go to: https://console.anthropic.com
2. Sign in (or create a free account)
3. Go to **API Keys** → **Create Key**
4. Name it: `bw-briefing-agent`
5. Copy the key — it starts with `sk-ant-...`

### 2.2 Set up billing

1. In the Anthropic Console → **Billing** → add a payment method
2. Set a monthly **usage limit** of $30 as a safety cap
   (Actual usage: ~$8–14/month for all 3 tracks with the two-model strategy;
   RSS and Wayback CDX API calls are free and not billed here)

### 2.3 Create your .env file

```bash
cd c:\Users\chuan\betterwiser_briefs_agent
copy .env.example .env
```

Open `.env` in Notepad and fill in:

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

---

## PART 3 — First Test Run

At this point you can already run the pipeline. Start with the demo script — it verifies all code paths work in under 4 minutes for under $0.10.

**Option A — Demo run (recommended first step):**
```bash
conda activate bw-briefing
cd c:\Users\chuan\betterwiser_briefs_agent
python demo_run.py
```
Or double-click `RUN_DEMO.bat`.

The demo runs the full 6-pass synthesis pipeline on all 3 tracks using pre-built synthetic data. No real web scraping. No inbox access needed. No Tavily needed.

**Expected output — all tracks should show `[PASS]`:**
```
Track A  [PASS]
  ✓ Phase 2 (demo data)
  ✓ Pass 0 (clusters=3)
  ✓ Pass 1 (sorted clusters=3)
  ✓ Pass 2 (output=~1200 chars)
  ✓ Pass 3
  ✓ Pass 3.5 (grounding=100%)
  ✓ Pass 4 (dead_links=3)     ← expected, demo URLs are placeholders
  ✓ Phase 5 — saved to runs\...

All systems operational. Pipeline is working correctly.
```

If any track shows `[FAIL]`, check the error message — it's almost always a missing `ANTHROPIC_API_KEY`.

**Option B — Single track real dry run:**
```bash
conda activate bw-briefing
cd c:\Users\chuan\betterwiser_briefs_agent
python -m src.orchestrator --month 2026-03 --track C --dry-run
```

**Option C — Web Dashboard (easiest for non-developers):**
```bash
conda activate bw-briefing
cd c:\Users\chuan\betterwiser_briefs_agent
python dashboard.py
```
Open http://localhost:5000 in your browser, pick March 2026, click **Generate Briefing**.

**Where to find output:** `runs\2026-03_run_[timestamp]\delivery\track_C.html`
Open the HTML file in Chrome or Edge.

If the demo passes and you can open the HTML briefing, the agent is working. Continue below to add email.

---

## PART 4 — Microsoft Azure AD (Email Features)

You need this to read newsletters from the inbox and send briefings via email.

> **Azure is NOT used to host the agent.** It is only used as an authentication
> gateway so the agent can connect to Microsoft 365. The agent runs on your machine
> (or GitHub Actions). Azure costs nothing for this use.

### 4.1 Create a dedicated mailbox

1. Go to: https://admin.microsoft.com (sign in as M365 admin)
2. **Teams & groups** → **Shared mailboxes** → **Add a shared mailbox**
   - Display name: `BW Briefing Agent`
   - Email: `ai-briefing@betterwiser.com`
3. Note the full email address — this is `AZURE_USER_EMAIL`

### 4.2 Register an Azure AD Application

1. Go to: https://portal.azure.com (sign in with your M365 admin account)
2. Search **"App registrations"** in the top search bar → click it
3. Click **+ New registration**
4. Fill in:
   - **Name**: `BetterWiser Briefing Agent`
   - **Supported account types**: `Accounts in this organizational directory only`
   - **Redirect URI**: leave blank
5. Click **Register**
6. Note down from the overview page:
   - **Application (client) ID** → `AZURE_CLIENT_ID`
   - **Directory (tenant) ID** → `AZURE_TENANT_ID`

### 4.3 Create a Client Secret

1. Left menu → **Certificates & secrets** → **New client secret**
2. Description: `briefing-agent-secret`, Expiry: `24 months`
3. Click **Add**
4. **Copy the VALUE column immediately** — it is only shown once
   - This is `AZURE_CLIENT_SECRET`

### 4.4 Grant API Permissions

1. Left menu → **API permissions** → **Add a permission**
2. Choose **Microsoft Graph** → **Application permissions**
3. Search and add:
   - `Mail.Read`
   - `Mail.Send`
4. Click **Grant admin consent for BetterWiser** (requires Global Admin)
5. Confirm all permissions show a green **Granted** checkmark

### 4.5 Update your .env file

Open `.env` and add the four Azure values:

```env
AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_SECRET=your-secret-value-here
AZURE_USER_EMAIL=ai-briefing@betterwiser.com
```

### 4.6 Subscribe the mailbox to newsletters

Subscribe `ai-briefing@betterwiser.com` to these newsletters:

| Newsletter | Where to subscribe |
|------------|-------------------|
| Artificial Lawyer | artificiallawyer.com → Newsletter |
| LawNext (Bob Ambrogi) | lawnext.com → Subscribe |
| Harvey AI | harvey.ai → follow blog / newsletter |
| Luminance | luminance.com → News |
| Singapore Law Gazette | lawgazette.com.sg |
| SAL Legal Updates | sal.org.sg → Resources |
| PDPC Announcements | pdpc.gov.sg → News |
| EU AI Office | digital-strategy.ec.europa.eu → Newsletter |
| MAS News | mas.gov.sg → subscribe |
| ALITA | alita.asia |

(~15 minutes. The agent works without inbox access — newsletters improve quality but are not required.)

> **RSS feeds are configured separately** — they are fetched automatically via `config/briefing_config.yaml` under `rss_feeds` and require no inbox subscription. The 9 pre-configured feeds (Artificial Lawyer, LawNext, Legal Futures, PDPC, MAS, MinLaw, etc.) are active by default.

---

## PART 5 — Update Recipients

Edit [config/briefing_config.yaml](config/briefing_config.yaml) to set who receives each track:

```yaml
recipients:
  A:
    - email: "lynette@betterwiser.com"
      name: "Lynette Ooi"
  B:
    - email: "lynette@betterwiser.com"
      name: "Lynette Ooi"
  C:
    - email: "lynette@betterwiser.com"
      name: "Lynette Ooi"
```

Add more people by adding `- email: / name:` entries under any track.

---

## PART 6 — Optional: Tavily API

Improves Track C Wave 4 deep research. Without it, Track C still works well.

1. Go to: https://tavily.com → Sign up
2. Dashboard → **API Keys** → copy your key
3. Free tier: 1,000 searches/month (more than enough)
4. Add to `.env`:
   ```env
   TAVILY_API_KEY=tvly-your-key-here
   ```

---

## PART 7 — Optional: Spider API

Better web scraping fallback when Jina Reader can't extract a page.

1. Go to: https://spider.cloud → Sign up
2. Account → **API Key** → copy your key
3. Add to `.env`:
   ```env
   SPIDER_API_KEY=sp-your-key-here
   ```

---

## PART 8 — GitHub Actions (Fully Automatic Monthly Runs)

This is the recommended production setup. The agent runs automatically on the 1st of every month — no machine needs to be on, no one needs to click anything.

### 8.1 Push your code to a GitHub repository

```bash
cd c:\Users\chuan\betterwiser_briefs_agent

# If you don't have a GitHub repo yet:
git init
git add .
git commit -m "Initial commit"

# Create a new PRIVATE repo at github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/betterwiser-briefing-agent.git
git push -u origin master
```

> The `.gitignore` already excludes `.env` and `runs/` — your secrets and briefing content will NOT be uploaded.

### 8.2 Add secrets to GitHub

1. Go to your repo on GitHub
2. **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
3. Add each of these:

| Secret name | Value |
|-------------|-------|
| `ANTHROPIC_API_KEY` | Your Anthropic key (`sk-ant-...`) |
| `AZURE_TENANT_ID` | From Azure AD setup |
| `AZURE_CLIENT_ID` | From Azure AD setup |
| `AZURE_CLIENT_SECRET` | From Azure AD setup |
| `AZURE_USER_EMAIL` | `ai-briefing@betterwiser.com` |
| `TAVILY_API_KEY` | Optional |
| `SPIDER_API_KEY` | Optional |

### 8.3 The workflow runs automatically

The file [.github/workflows/monthly_briefing.yml](.github/workflows/monthly_briefing.yml) is already in the repo. Once your secrets are set, it will:
- Run on the **1st of every month at 08:00 SGT**
- Generate all 3 tracks and send emails via Microsoft 365
- Upload the briefing HTML files as downloadable artifacts (kept for 30 days)

### 8.4 To trigger manually (e.g. test a month)

1. Go to your repo → **Actions** tab
2. Click **Monthly Briefing Agent** in the left sidebar
3. Click **Run workflow** (grey button, top right)
4. Enter a month (`2026-03`), choose tracks, pick dry-run or send
5. Click the green **Run workflow** button

The run takes 15–30 minutes. You'll see live logs in the Actions tab.

---

## PART 9 — Web Dashboard Setup

The dashboard lets anyone on your team generate briefings without the command line.

### 9.1 Start the dashboard

```bash
conda activate bw-briefing
cd c:\Users\chuan\betterwiser_briefs_agent
python dashboard.py
```

Open http://localhost:5000 in Chrome or Edge.

### 9.2 Keep the dashboard running (optional)

To keep it running in the background without a terminal window:

**Windows Task Scheduler:**
1. Open Task Scheduler → **Create Basic Task**
2. Name: `BW Dashboard`
3. Trigger: **When the computer starts**
4. Action: Start a program
   - Program: `C:\Users\chuan\anaconda3\envs\bw-briefing\python.exe`
   - Arguments: `dashboard.py`
   - Start in: `C:\Users\chuan\betterwiser_briefs_agent`
5. Finish

The dashboard will then be available at http://localhost:5000 whenever the machine is on.

### 9.3 Make it accessible on your local network (optional)

The dashboard already listens on `0.0.0.0:5000`, so anyone on the same WiFi can access it at `http://[your-PC-IP]:5000`. To find your IP: run `ipconfig` and look for IPv4 Address.

---

## Final Checklist

### Minimum Working Setup
- [ ] Miniconda installed
- [ ] `conda create -n bw-briefing python=3.12` done
- [ ] `pip install -r requirements.txt` completed
- [ ] `python -m playwright install chromium --with-deps` done
- [ ] `.env` created from `.env.example`
- [ ] `ANTHROPIC_API_KEY` set in `.env`
- [ ] Anthropic billing configured with $30 cap
- [ ] **Demo run passes:** `python demo_run.py` → all 3 tracks show `[PASS]`
- [ ] Demo HTML opens in browser (`runs\..._DEMO_...\delivery\track_A.html`)

### Full Email Setup
- [ ] M365 shared mailbox `ai-briefing@betterwiser.com` created
- [ ] Azure AD app registration created
- [ ] Client secret created and saved
- [ ] `Mail.Read` + `Mail.Send` granted with admin consent
- [ ] All 4 Azure env vars in `.env`
- [ ] Recipients updated in `config/briefing_config.yaml`
- [ ] Mailbox subscribed to newsletters (Part 4.6)
- [ ] Tested with Send mode in dashboard — briefings arrive in Lynette's inbox

### Fully Automated (GitHub Actions)
- [ ] Code pushed to private GitHub repo
- [ ] All 5 secrets added to GitHub (Settings → Secrets)
- [ ] Manually triggered one workflow run and verified it completes
- [ ] Monthly schedule confirmed active (Actions tab → workflow shows schedule)

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| VS Code shows package warnings | Press `Ctrl+Shift+P` → Python: Select Interpreter → choose `bw-briefing` conda env |
| `ANTHROPIC_API_KEY not set` | Check `.env` exists and has the key |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` inside the `bw-briefing` conda env |
| Demo shows `[FAIL] Pass 2` | `ANTHROPIC_API_KEY` is invalid or has no credit — check Anthropic Console billing |
| Demo shows 3 dead links per track | Expected — demo uses placeholder URLs; the link validator marks them dead correctly |
| Demo passes but no email sent | Normal without `--send-email` flag, or Azure credentials not yet configured |
| Dashboard shows "Azure AD not configured" | Inbox/email disabled — briefings still save to disk |
| Track shown as ⚠ (held for review) | Grounding below 95% — open the HTML to review manually |
| Playwright error | Run `python -m playwright install chromium --with-deps` |
| Demo run takes > 5 min | Check for API rate limiting — Haiku calls should complete quickly |
| Full run takes 30+ min | Normal for Track C (7-wave deep research incl. Wave 7 contrarian). A+B take ~10 min |
| GitHub Actions run fails | Check the run logs in the Actions tab; most issues are missing secrets |
