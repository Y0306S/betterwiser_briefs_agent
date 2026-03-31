# BetterWiser Legal-Tech AI Briefing Agent

> **Fully autonomous monthly intelligence briefings for the legal AI ecosystem.**
> Replaces manual research with a Claude-powered pipeline that gathers, synthesises, validates, and delivers three separate briefings every month — automatically.

---

## What It Does

Every month, this agent scans the legal AI landscape and delivers three polished intelligence briefings to your team via email:

| Track | Name | Contents |
|-------|------|----------|
| **A** | Vendor & Customer Intelligence | Harvey, Luminance, vLex, Singapore law firm AI adoption — 10–15 dated bullet items |
| **B** | Global AI Policy & Regulatory Watch | EU AI Act, Singapore MinLaw, UK ICO, US NIST — 6–8 thematic summaries |
| **C** | Thought Leadership Digest | Deep research on named thought leaders, firm perspectives, BetterWiser relevance commentary |

---

## Ways to Run It

### Option 0 — Demo / Smoke Test (Start here after setup)
Runs the **full pipeline with synthetic data** to verify all code paths work before spending real API credits on a production run. Uses Claude Haiku instead of Opus, injects pre-built demo sources, and skips real web scraping. Sends a `[DEMO]` email if Azure credentials are present.

```bash
python demo_run.py              # all 3 tracks, saves HTML only (~$0.05)
python demo_run.py --track C    # single track, fastest
python demo_run.py --send-email # also sends demo email via MS Graph
```
Or double-click `RUN_DEMO.bat`.

What it verifies: Pydantic schema construction · Pass 0–4 synthesis pipeline · HTML formatting · link validation · delivery / archiving · email send path

### Option 1 — Web Dashboard (Recommended for team use)
A browser-based UI. No command line needed. Anyone on the team can trigger a run, watch live progress, and open the finished briefings.

```
python dashboard.py
→ Open http://localhost:5000
```

### Option 2 — GitHub Actions (Fully automatic, zero interaction)
Runs on the 1st of every month in the cloud. Briefings are emailed automatically and also available as downloadable artifacts in GitHub. No machine needs to be on.

See [.github/workflows/monthly_briefing.yml](.github/workflows/monthly_briefing.yml) — set it up once, forget about it.

### Option 3 — Command Line (For developers)
```bash
# Dry run (default): generate briefings, save to disk, no email sent
python -m src.orchestrator --month 2026-03

# Send emails: --send automatically disables dry-run
python -m src.orchestrator --month 2026-03 --send
```

---

## System Architecture

### High-Level Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                    BETTERWISER BRIEFING AGENT                       │
│                                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌────────────┐   │
│  │ PHASE 0  │───▶│ PHASE 1  │───▶│ PHASE 2  │───▶│  PHASE 3   │   │
│  │ CONTEXT  │    │ TRIGGER  │    │  GATHER  │    │ SYNTHESISE │   │
│  │ UPDATE   │    │          │    │          │    │            │   │
│  └──────────┘    └──────────┘    └──────────┘    └────────────┘   │
│  LinkedIn +      Build context   5 sub-pipelines  6-pass pipeline   │
│  web search      Load config     run in parallel  per track         │
│  refresh                                               │            │
│  context.txt                                     ┌──────────┐      │
│                                                  │ PHASE 4  │      │
│                                                  │ VALIDATE │      │
│                                                  └────┬─────┘      │
│                                                       │            │
│                                                  ┌──────────┐      │
│                                                  │ PHASE 5  │      │
│                                                  │ DELIVER  │      │
│                                                  └──────────┘      │
│                                                  Save HTML +       │
│                                                  Send via email    │
└─────────────────────────────────────────────────────────────────────┘
```

### Phase 0: Automatic Context Update

Before each monthly run, the agent checks Lynette Ooi's LinkedIn profile
(`https://www.linkedin.com/in/lynetteooi/`) and runs targeted web searches to
detect any changes since the last update:

- New roles, publications, speaking engagements, advisory board appointments
- New BetterWiser services, partnerships, or client segments
- Updated strategic priorities or ecosystem positions

Claude compares the gathered intelligence against the current
`config/betterwiser_context.txt` and rewrites only the sections that reflect
verified new facts — tone, structure, and unaffected content are left untouched.

**Audit trail:** A timestamped backup is written to `config/context_backups/`
before every change.  The update is idempotent — re-running the same month skips
the check entirely.

**GitHub Actions:** Any context change is automatically committed back to the
repository with a `[skip ci]` commit, so the repo always reflects the latest
profile state.

**To skip** the context update on a specific run:
```bash
python -m src.orchestrator --skip-context-update --month 2026-03
```

### Phase 2: Intelligence Gathering (5 Sub-Pipelines in Parallel)

```
                          ┌──────────────────────┐
                          │   GATHER PHASE       │
                          │  (all run at once)   │
                          └─────────┬────────────┘
              ┌──────────┬──────────┼──────────┬──────────┐
              │          │          │          │          │
              ▼          ▼          ▼          ▼          ▼
        ┌──────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
        │  INBOX   │ │  WEB   │ │CLAUDE  │ │THOUGHT │ │HISTORY │
        │ READER   │ │SCRAPER │ │DISCOV. │ │LEADER. │ │LOADER  │
        │          │ │        │ │        │ │WAVES   │ │        │
        │ MS Graph │ │Jina →  │ │web_    │ │(Track C│ │Previous│
        │ Azure AD │ │Spider→ │ │search  │ │ only)  │ │month   │
        │ Optional │ │Crawl4AI│ │queries │ │6 waves │ │context │
        └──────────┘ └────────┘ └────────┘ └────────┘ └────────┘
              │          │          │          │          │
              └──────────┴──────────┴──────────┴──────────┘
                                    │
                              GatheredData
                           (Pydantic v2 model)
```

> **Graceful degradation**: If inbox credentials are missing → web-only.
> If Spider API key missing → falls back to Jina (free). Each sub-pipeline
> failure is logged and the pipeline continues regardless.

### Phase 3: Six-Pass Synthesis (per Track)

```
  GatheredData
       │
       ▼
  ┌─────────────────────────────────────────────────────────┐
  │                    SYNTHESIS PIPELINE                    │
  │                                                         │
  │  Pass 0  ──▶  Pass 1  ──▶  Pass 2  ──▶  Pass 3         │
  │  Cluster      Triage       DRAFT         Fact-check     │
  │  & Dedup      & Sort       (Opus 4.6     (Sonnet 4.6    │
  │  (thefuzz     (authority   extended      Citations API  │
  │   match)      tiers)       thinking)     re-verifies    │
  │                                          claims)        │
  │                                │                        │
  │                         Pass 3.5  ──▶  Pass 4          │
  │                         Grounding      Format HTML      │
  │                         Verify         (Outlook-safe    │
  │                         (fuzzy ≥ 0.95) table layout,    │
  │                                        link validate)   │
  └─────────────────────────────────────────────────────────┘
       │
       ▼
  ValidatedBriefing
```

### Phase 5: Delivery Decision Tree

```
  ValidatedBriefing
         │
         ▼
  ┌─────────────────┐
  │ held_for_review?│──YES──▶ Save to disk only (grounding failed)
  └────────┬────────┘
           │ NO
           ▼
  ┌─────────────────┐
  │  --send flag?   │──NO───▶ Save HTML to runs/ (dry-run)
  └────────┬────────┘
           │ YES
           ▼
  ┌─────────────────┐
  │ Azure creds?    │──NO───▶ Save to disk + warn
  └────────┬────────┘
           │ YES
           ▼
     Send via MS Graph API
     (Microsoft 365 email)
```

---

## AI Models & External Services

### Two-Model Strategy

The pipeline uses two Claude models to balance quality and cost. Model selection is controlled by `config/briefing_config.yaml` under the `model` and `research_model` keys.

```
┌──────────────────────────────────────────────────────────────────┐
│  CLAUDE OPUS 4.6          │  CLAUDE SONNET 4.6                   │
│  Pass 2 draft ONLY        │  Everything else                     │
│  ───────────────────────  │  ─────────────────────────────────── │
│  Extended thinking        │  Pass 3 factcheck (Citations API)    │
│  (budget 10k tokens)      │  Discovery queries (all 3 tracks)    │
│  Citations API            │  TL Waves 1–6 (50–75 calls/run)      │
│  30-source context        │  Phase 0 context update              │
│                           │                                      │
│  Where quality matters:   │  Where volume matters:               │
│  multi-source editorial   │  JSON extraction, web search,        │
│  synthesis + judgement    │  structured verification             │
└──────────────────────────────────────────────────────────────────┘
```

**Why this split?** Opus's extended thinking and multi-document reasoning justify the premium for Pass 2, where editorial judgement determines the final briefing quality. Every other call is structured extraction or JSON output — tasks Sonnet handles reliably at ~5× lower cost.

### External Services

```
┌────────────────────────────────────────────────────────────────────┐
│                       YOUR MACHINE / GITHUB                        │
│                                                                    │
│   src/orchestrator.py  (or GitHub Actions runner)                 │
│          │                                                         │
│    ┌─────┼──────────────────────────────┐                         │
│    │     │                              │                         │
│    ▼     ▼                              ▼                         │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────┐                 │
│  │  Anthropic  │  │   Jina       │  │  Azure   │                 │
│  │     API     │  │  Reader      │  │    AD    │                 │
│  │  REQUIRED   │  │   FREE       │  │ OPTIONAL │                 │
│  │ Opus 4.6 +  │  │ r.jina.ai    │  └────┬─────┘                 │
│  │ Sonnet 4.6  │  └──────┬───────┘       │                       │
│  └─────────────┘         │               ▼                       │
│                           │    ┌──────────────────┐              │
│                    ┌──────┘    │  Microsoft Graph  │              │
│                    │           │  Email Read/Send  │              │
│                    ▼           └──────────────────┘              │
│             ┌─────────────┐                                       │
│             │   Spider    │  OPTIONAL — fallback after Jina       │
│             │     API     │                                       │
│             └──────┬──────┘                                       │
│                    │                                              │
│                    ▼                                              │
│             ┌─────────────┐                                       │
│             │   Tavily    │  OPTIONAL — Track C deep research     │
│             └─────────────┘                                       │
└────────────────────────────────────────────────────────────────────┘
```

---

## Web Dashboard

The dashboard provides a browser-based interface for non-technical users.

```
┌──────────────────────────────────────────────────────────────┐
│  BetterWiser · Briefing Agent                                │
├──────────────────────────────────────────────────────────────┤
│  Generate New Briefing                                       │
│                                                              │
│  Month: [2026-03]    Tracks: [vA] [vB] [vC]                 │
│  Mode:  [Save to Disk]  [Send via Email]                     │
│                                                              │
│  [ >> Generate Briefing ]                                    │
├──────────────────────────────────────────────────────────────┤
│  Run History                                                 │
│                                                              │
│  2026-03  v Done    [A] [B] [C!]  View logs                  │
│  2026-02  v Done    [A] [B] [C]   View logs                  │
└──────────────────────────────────────────────────────────────┘
```

**Start the dashboard:**
```bash
conda activate bw-briefing
python dashboard.py
# Open http://localhost:5000
```

Features:
- Live log streaming while a run is in progress
- One-click briefing viewer (opens HTML in browser tab)
- Visual status: saved / sent / ⚠ held for review
- No command line knowledge needed

---

## GitHub Actions (Fully Automatic)

The workflow at [.github/workflows/monthly_briefing.yml](.github/workflows/monthly_briefing.yml) runs on the 1st of every month at 08:00 SGT (00:00 UTC).

**What happens automatically each month:**
1. GitHub spins up a cloud machine
2. Installs all dependencies
3. **Phase 0:** Checks Lynette Ooi's LinkedIn profile and updates `config/betterwiser_context.txt` if needed, committing any changes back to the repo
4. Runs the full pipeline and sends emails
5. Uploads the HTML briefings as downloadable artifacts
6. Machine shuts down — you pay nothing

**To trigger manually** (e.g. test a specific month):
- Go to your repo on GitHub
- Click **Actions** → **Monthly Briefing Agent** → **Run workflow**
- Enter a month (e.g. `2026-03`) and click the green button

**Setup required:** Add all API keys as GitHub Secrets (repo Settings → Secrets and variables → Actions). See [SETUP_CHECKLIST.md](SETUP_CHECKLIST.md) Part 8.

---

## Track Descriptions

### Track A — Vendor & Customer Intelligence
10–15 dated bullet items across three segments:
1. Primary legal AI vendors (Harvey, Luminance, vLex, Legora, Anthropic)
2. Singapore law firms adopting AI
3. Singapore government / SAL initiatives

### Track B — Global AI Policy & Regulatory Watch
6–8 thematic summaries covering:
- Singapore: MinLaw, PDPC, MAS, AGC
- EU: AI Office, EU AI Act enforcement
- UK: ICO, DSIT
- US: NIST, FTC, White House OSTP

### Track C — Thought Leadership Digest
6-wave deep research process:
```
Wave 1  Extract thought leaders from newsletters
Wave 2  Per-person deep search (4+ queries each)
Wave 3  Retrieve firm insights pages (PwC, McKinsey, EY, Deloitte…)
Wave 4  Tavily advanced research for strategic themes
Wave 5  Semantic similarity expansion
Wave 6  Conference speaker mining → extend watchlist
```
Each article gets: Summary · Opinion Takeaway · BetterWiser Relevance

---

## File Structure

```
betterwiser_briefs_agent/
│
├── demo_run.py                    ← Smoke test / demo run (start here after setup)
├── RUN_DEMO.bat                   ← Double-click to run demo (all 3 tracks, ~$0.05)
├── RUN_BRIEFING_DRY_RUN.bat       ← Double-click for a full dry-run (save HTML)
├── RUN_BRIEFING_SEND_EMAIL.bat    ← Double-click to generate + send real email
│
├── dashboard.py                   ← Web dashboard (python dashboard.py)
├── templates/                     ← Dashboard + email preview HTML templates
│   ├── dashboard.html
│   ├── run_detail.html
│   └── email_preview_option_A.html ← Production email format reference
│
├── .github/workflows/
│   └── monthly_briefing.yml       ← GitHub Actions auto-scheduler
│
├── config/                        ← Edit these to customise behaviour
│   ├── briefing_config.yaml       ← Recipients, model, thresholds, queries
│   ├── betterwiser_context.txt    ← Company context for Track C (auto-updated monthly)
│   ├── context_backups/           ← Timestamped backups before each context change
│   ├── newsletter_subscriptions.yaml
│   ├── vendor_watchlist.yaml
│   └── prompt_templates/
│
├── src/
│   ├── orchestrator.py            ← CLI entry point
│   ├── schemas.py                 ← All Pydantic v2 data models
│   ├── gatherers/                 ← Phase 0 + Phase 2 data gathering
│   │   ├── profile_updater.py     ← Phase 0: LinkedIn + web search context refresh
│   ├── synthesis/                 ← Phase 3: 6-pass synthesis
│   ├── delivery/                  ← Phase 5: archive + email
│   └── utils/                     ← Shared helpers
│
├── runs/                          ← Output (auto-created)
│   └── 2026-03_run_20260301T080000/
│       ├── run.log
│       └── delivery/
│           ├── track_A.html       ← Your briefing
│           ├── track_B.html
│           └── track_C.html
│
├── .env                           ← API keys (never commit)
├── .env.example                   ← Copy this to create .env
├── requirements.txt
├── SETUP_CHECKLIST.md             ← Start here for setup
└── SETUP.md                       ← Azure AD detail guide
```

---

## Quality Safeguards

```
Layer 1: CITATIONS         Every claim must be traceable to a
──────────────────         scraped source (Anthropic Citations API)

Layer 2: GROUNDING         95%+ of claims must fuzzy-match source
──────────────────         text (configurable in briefing_config.yaml)

Layer 3: HELD FOR REVIEW   Below 95% → saved to disk, NOT emailed,
────────────────────────   flagged ⚠ in the dashboard for human review
```

---

## Cost

| Component | Per Monthly Run |
|-----------|----------------|
| Claude Opus 4.6 (Pass 2 synthesis — 3 calls/run) | ~$4–6 |
| Claude Sonnet 4.6 (research, factcheck, discovery — 80–110 calls/run) | ~$2–4 |
| Claude web searches (150–255) | ~$1.50–2.55 |
| Phase 0: context update (~5 queries) | ~$0.05 |
| Tavily deep research | ~$0.50–1.00 |
| Jina Reader | Free |
| Spider API | ~$0.02 |
| Microsoft Graph | Free |
| GitHub Actions | Free (private repo) |
| **Total** | **~$8–14 / month** |

**Demo run cost:** under $0.10 total for all 3 tracks (Claude Haiku, no extended thinking, synthetic data only).

> **Two-model savings:** Opus is used only for Pass 2 (3 calls/run — one per track). All other ~80–110 calls use Sonnet 4.6 at ~5× lower cost. Estimated saving vs. Opus-only: 50–60% (~$9–10/month).
