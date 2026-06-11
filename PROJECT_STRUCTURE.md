# Project Structure — Quick Reference

> One-screen cheat sheet. See [`docs/system-design.md`](docs/system-design.md)
> for the full architecture, data-flow, and design rationale.

```
automate-workflow-py/
│
├── conftest.py                    Pytest lifecycle — browser, artifacts, session teardown
├── pytest.ini                     Test markers + log format
├── pyproject.toml                 Project metadata
├── requirements.txt               playwright, pytest, requests
├── README.md                      How to run
├── PROJECT_STRUCTURE.md           ◀── this file
│
├── tests/                         Test files (one concern per file)
│   ├── _flozic_common.py          Shared driver for the 10 flozic apps
│   ├── flozic_prompts.json        Hardcoded prompt config per app
│   ├── test_flozic_*.py           10 hardcoded flozic.ai entry-point tests
│   ├── test_flozic_diversified.py Parametrized: N AI-generated variants per app
│   ├── test_*.py                  Other (homepage / pricing / app-pairing / …)
│   └── marketing/                 Marketing-page tests
│
├── pages/                         Page Objects (UI abstractions)
│   ├── flozic_landing_page.py     /integrate/apps/<slug>/integrations entry
│   ├── auth_helper.py             accounts.appypie.com login (env-var creds)
│   ├── authv2_helper.py           authv2.flozic.ai Cognito second-stage
│   ├── signup_to_login_switch.py  gohighlevel /register → /login workaround
│   ├── copilot_panel.py           Copilot "Connect created!" detection
│   ├── connect_canvas.py          Canvas populated check + screenshot
│   ├── dashboard_page.py          Dashboard landing
│   ├── wait_utils.py              Generic Playwright waits
│   └── marketing/                 Marketing pages
│
├── utils/                         Domain logic + AI helpers
│   ├── snapshot_writer.py         TestRecord → JSON snapshot
│   ├── health_tracker.py          Pass/fail aggregator + scoring
│   ├── layered_health_scores.py   Product / Infra / Framework split
│   ├── risk_interpreter.py        READY / WARNING / AT_RISK / BLOCKED
│   ├── error_clusterer.py         Within-run JS error clustering
│   ├── failure_clustering.py      Cross-run failure clustering
│   ├── js_console_monitor.py      Page console error subscription
│   ├── dashboard_builder.py       Self-contained HTML dashboard
│   ├── pdf_report_builder.py      Printable HTML / PDF
│   ├── test_category.py           @test_category decorator
│   ├── config.py
│   │
│   ├── ai_prompt_generator.py     GPT: workflow prompts per app
│   ├── ai_validator.py            GPT vision: canvas vs prompt
│   ├── ai_triage.py               GPT: failure root-cause classification
│   ├── ai_visual_diff.py          GPT vision: semantic visual regression
│   ├── ai_exec_summary.py         GPT: dashboard executive summary
│   ├── ai_pr_review.py            GPT: code review on git diff
│   ├── ai_page_object.py          GPT: Page Object scaffold from DOM
│   └── ai_form_data.py            GPT: synthetic form data (validate-before-cache)
│
├── scripts/                       Stand-alone CLIs
│   ├── promote_baseline.py        Promote canvas.png → baselines/
│   ├── ai_pr_review.py            CLI wrapper for PR review
│   └── generate_page_object.py    CLI: URL/HTML → draft Page Object
│
├── docs/                          Architecture + ops docs
│   ├── system-design.md           ◀── start here
│   ├── migration-execution-plan.md
│   ├── observation-log.md
│   ├── observation-protocol.md
│   ├── risk-register.md
│   └── snapshot-contract.md
│
├── baselines/                     ◀── tracked in git
│   └── <test_id>/canvas.png       Visual-regression baselines
│
├── cache/                         ◀── gitignored
│   ├── ai_prompts/                Optional GPT prompt cache
│   └── synthetic_data/            Optional form-data cache
│
└── reports/                       ◀── gitignored, regenerated each run
    ├── recordings/<test_id>/      PASS path — video, canvas, verdict, prompt
    ├── failures/<test_id>_<ts>/   FAIL path — screenshot, DOM, URL, video, triage
    └── trend/
        ├── dashboard.html         Self-contained dashboard
        ├── report-printable.html  PDF-friendly
        ├── trend-history.json     Last 30 runs summary
        ├── python-health-snapshot.json   Latest run
        └── snapshots/             ◀── failure-clustering history accrues here
            └── <YYYYMMDD_HHMMSS>.json
```

## Key file counts

| Area | Files | Purpose |
|---|---:|---|
| `tests/` (flozic) | 11 | 10 hardcoded apps + 1 diversified |
| `tests/` (other) | 14 | Older homepage / pricing / app-pairing tests |
| `pages/` | 14 | Page Objects |
| `utils/` | 21 | Snapshot, scoring, AI helpers, dashboard, PDF |
| `scripts/` | 3 | Promote baselines, PR review, page-object gen |
| `docs/` | 6 | Architecture, ops, risk register |

## What lives in git vs not

| Path | Tracked? | Why |
|---|---|---|
| Source code (`tests/`, `pages/`, `utils/`, `scripts/`, `conftest.py`, `docs/`) | ✓ | Standard |
| `tests/flozic_prompts.json` | ✓ | Test config |
| `baselines/<test_id>/canvas.png` | ✓ | Visual-regression source of truth |
| `requirements.txt`, `pyproject.toml`, `pytest.ini`, `README.md` | ✓ | |
| `.gitignore` | ✓ | Documents what *not* to track |
| `reports/` | ✗ | Regenerated every run |
| `cache/` | ✗ | Regeneratable |
| `.venv/` | ✗ | Per-machine |
| `__pycache__/` | ✗ | Per-machine |
| `.env`, `.env.local` | ✗ | Credentials |

## First-time setup

```powershell
pip install -r requirements.txt
playwright install chromium

# Required env vars (no defaults — empty falls back to manual login)
$env:AUTOMATE_EMAIL    = "<your-test-account>@appypiellp.com"
$env:AUTOMATE_PASSWORD = "<password>"

# Optional — unlocks the AI features
$env:OPENAI_API_KEY = "sk-..."
$env:OPENAI_MODEL   = "gpt-4o"

# Run the 10-app smoke suite (~5 min)
python -m pytest (Get-ChildItem tests/test_flozic_*.py -Exclude _*,test_flozic_diversified.py).FullName --headed -v

# After a known-good run, lock visual-regression baselines
python scripts/promote_baseline.py --all

# Full diversified run (~30 min, ~$0.50–$0.80 in OpenAI usage)
python -m pytest (Get-ChildItem tests/test_flozic_*.py -Exclude _*).FullName --headed -v
```

## Where to look for what

| If you want to … | Open |
|---|---|
| Understand the system | `docs/system-design.md` |
| Run the tests | `README.md` |
| See last run's results | `reports/trend/dashboard.html` |
| See a specific failure | `reports/failures/<test_id>_<ts>/` (screenshot, DOM, video, triage) |
| Update a visual baseline | `python scripts/promote_baseline.py <test_id>` |
| Add a new flozic app test | Copy `tests/test_flozic_chatgpt.py`, change the slug |
| Tune AI behaviour | `utils/ai_*.py` (system prompts at the top of each file) |
| Tune the dashboard | `utils/dashboard_builder.py` |
| Add a Page Object for a new screen | `python scripts/generate_page_object.py <url-or-html-file>` |
