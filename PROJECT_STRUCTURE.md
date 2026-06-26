# Project Structure — Quick Reference

> One-screen cheat sheet. See [`docs/system-design.md`](docs/system-design.md)
> for the full architecture, data-flow, and design rationale.

```
FlowGuard/
│
├── conftest.py                    Pytest lifecycle — browser, artifacts, session teardown
├── pytest.ini                     Test markers + log format
├── pyproject.toml                 Project metadata
├── requirements.txt               playwright, pytest, requests
├── README.md                      How to run
├── PROJECT_STRUCTURE.md           ◀── this file
│
├── tests/                         Test files (one concern per file)
│   ├── _flozic_common.py          Shared driver for flozic app tests
│   ├── _flozic_redirect_probe.py  URL redirect / slug probe helper
│   ├── flozic_prompts.json        Hardcoded prompt config per app
│   │
│   ├── test_flozic_acculynx.py          ┐
│   ├── test_flozic_chatgpt.py           │
│   ├── test_flozic_cliniko.py           │
│   ├── test_flozic_gmail.py             │
│   ├── test_flozic_gohighlevel.py       │  15 hardcoded flozic.ai
│   ├── test_flozic_google_sheets.py     │  integration app tests
│   ├── test_flozic_housecall_pro.py     │
│   ├── test_flozic_lightspeedxseries.py │
│   ├── test_flozic_microsoft_excel.py   │
│   ├── test_flozic_mindbody.py          │
│   ├── test_flozic_net_suite.py         │
│   ├── test_flozic_paypal.py            │
│   ├── test_flozic_shopify.py           │
│   ├── test_flozic_telegram.py          │
│   ├── test_flozic_whatsappbusiness.py  ┘
│   │
│   ├── test_flozic_agent_discord_bot.py         ┐
│   ├── test_flozic_agent_facebook_messenger_bot.py │  5 AI agent
│   ├── test_flozic_agent_telegram_bot.py         │  bot tests
│   ├── test_flozic_agent_twitter_bot.py          │
│   ├── test_flozic_agent_whatsapp_bot.py         ┘
│   │
│   ├── test_flozic_diversified.py       Parametrized: N AI-generated variants per app
│   ├── test_flozic_flow_by_automate.py  loop.flozic.ai flow-builder entry-point
│   │
│   ├── test_app_directory.py            App directory browse + search
│   ├── test_app_pairing.py              Cross-app pairing smoke
│   ├── test_appypie_navigation.py       Appypie top-nav / deep-link checks
│   ├── test_auth_entry.py               Auth entry flows (login / register)
│   ├── test_authenticated_sanity.py     Post-login sanity checks
│   ├── test_create_connect_workflow.py  End-to-end connect creation
│   ├── test_error_handling.py           4xx / 5xx error-page assertions
│   ├── test_explore_menu_integration.py Explore-menu nav integration
│   ├── test_gohighlevel_mindbody.py     Cross-app pairing: GoHighLevel ↔ Mindbody
│   ├── test_homepage_exhaustive.py      Homepage full-coverage checks
│   ├── test_homepage_links.py           Homepage link / redirect validation
│   ├── test_rebrand_completion.py       Rebrand / rename completion sweep
│   ├── test_responsive.py               Responsive / viewport assertions
│   ├── test_top_app_combinations.py     Top-N app-combination smoke
│   ├── test_trending_app_integrations.py Trending integrations smoke
│   │
│   └── marketing/                       Marketing-page tests
│       ├── test_homepage_functional.py
│       ├── test_homepage_seo.py
│       ├── test_homepage_smoke.py
│       ├── test_pricing_functional.py
│       ├── test_pricing_links.py
│       ├── test_pricing_seo.py
│       └── test_pricing_smoke.py
│
├── pages/                         Page Objects (UI abstractions)
│   ├── flozic_landing_page.py     /integrate/apps/<slug>/integrations entry
│   ├── auth_helper.py             accounts.appypie.com login (env-var creds)
│   ├── auth_state.py              Persisted auth-state storage / restoreclear
│   ├── authv2_helper.py           authv2.flozic.ai Cognito second-stage
│   ├── signup_to_login_switch.py  gohighlevel /register → /login workaround
│   ├── copilot_panel.py           Copilot "Connect created!" detection
│   ├── connect_canvas.py          Canvas populated check + screenshot
│   ├── connect_editor_page.py     Connect editor interactions
│   ├── dashboard_page.py          Dashboard landing
│   ├── app_directory_browse_page.py  App directory browse / search UI
│   ├── error_page.py              4xx / 5xx error-page assertions
│   ├── explore_menu_helper.py     Explore-menu open / navigate helper
│   ├── responsive_helper.py       Viewport resize + responsive checks
│   ├── wait_utils.py              Generic Playwright waits
│   └── marketing/                 Marketing pages
│       ├── app_directory_page.py
│       ├── header_component.py
│       ├── home_page.py
│       └── pricing_page.py
│
├── utils/                         Domain logic + AI helpers
│   ├── snapshot_writer.py         TestRecord → JSON snapshot
│   ├── health_tracker.py          Pass/fail aggregator + scoring
│   ├── layered_health_scores.py   Product / Infra / Framework split
│   ├── risk_interpreter.py        READY / WARNING / AT_RISK / BLOCKED
│   ├── error_clusterer.py         Within-run JS error clustering
│   ├── failure_clustering.py      Cross-run failure clustering
│   ├── js_console_monitor.py      Page console error subscription
│   ├── network_monitor.py         Network request / response monitoring
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
| `tests/` (flozic apps) | 15 | Hardcoded integration app tests |
| `tests/` (flozic agents) | 5 | AI agent bot tests |
| `tests/` (flozic other) | 2 | Diversified + loop.flozic.ai flow |
| `tests/` (general) | 15 | Auth, navigation, pairing, UI, rebrand |
| `tests/marketing/` | 7 | Homepage + pricing (smoke / functional / SEO) |
| `pages/` (main) | 14 | Page Objects |
| `pages/marketing/` | 4 | Marketing Page Objects |
| `utils/` | 20 | Snapshot, scoring, AI helpers, dashboard, PDF |
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

```bash
pip install -r requirements.txt
playwright install chromium

# Required env vars (no defaults — empty falls back to manual login)
export AUTOMATE_EMAIL="<your-test-account>@appypiellp.com"
export AUTOMATE_PASSWORD="<password>"

# Optional — unlocks the AI features
export OPENAI_API_KEY="sk-..."
export OPENAI_MODEL="gpt-4o"

# Run the 15-app smoke suite
python -m pytest tests/test_flozic_*.py --ignore=tests/test_flozic_diversified.py \
  --ignore=tests/test_flozic_agent_*.py --headed -v

# Run agent bot tests
python -m pytest tests/test_flozic_agent_*.py --headed -v

# Run loop.flozic.ai flow test
python -m pytest tests/test_flozic_flow_by_automate.py --headed -v

# After a known-good run, lock visual-regression baselines
python scripts/promote_baseline.py --all

# Full diversified run (~30 min, ~$0.50–$0.80 in OpenAI usage)
python -m pytest tests/test_flozic_diversified.py --headed -v
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
| Add a new agent bot test | Copy `tests/test_flozic_agent_discord_bot.py`, change the platform |
| Tune AI behaviour | `utils/ai_*.py` (system prompts at the top of each file) |
| Tune the dashboard | `utils/dashboard_builder.py` |
| Monitor network traffic | `utils/network_monitor.py` |
| Add a Page Object for a new screen | `python scripts/generate_page_object.py <url-or-html-file>` |
