# Flozic FlowGuard — System Design

> Python + Playwright QA framework for [flozic.ai](https://www.flozic.ai)
> (formerly Appy Pie Automate). Each test drives a real browser through the
> live product, and a chain of AI helpers grades **what the product actually
> built** against **what the prompt asked for** — surfacing real product
> bugs that a "did the API return 200?" test suite would miss.

---

## 1. What the framework does — in one paragraph

A test selects an app (e.g. "google-sheets"), types a natural-language
workflow prompt into the flozic.ai entry page, clicks the "Build my
workflow" button, completes the two-stage login (`accounts.appypie.com`
→ `authv2.flozic.ai` Cognito Hosted UI), waits for the flozic copilot
to auto-build the connect on `connectcloud.appypie.com/customeditor`,
screenshots the canvas, and asks GPT-4o whether the cards on the canvas
match the prompt's intent. On failure, a separate GPT call triages the
root cause into one of `PRODUCT_BUG / LOCATOR_DRIFT / FLAKE / INFRA /
TEST_BUG`. Every run archives a JSON snapshot; failures across runs are
clustered with a recency-weighted recurrence score. A single self-
contained `dashboard.html` renders the results — including filter /
search / sort / dark mode / collapsible sections / Excel-grid tables /
PDF export / copy-to-clipboard as **HTML table** / **TSV** / **Markdown**.

---

## 2. Architecture diagram

```
                           ┌────────────────────────────┐
                           │       pytest session       │
                           │  (conftest.py orchestrates)│
                           └──────────────┬─────────────┘
                                          │
       ┌──────────────────────────────────┼──────────────────────────────────┐
       │                                  │                                  │
       ▼                                  ▼                                  ▼
┌─────────────┐                ┌─────────────────────┐              ┌─────────────────┐
│  Browser    │                │  AI helpers          │              │  Storage        │
│  Playwright │                │  (utils/ai_*.py)     │              │                 │
│             │                │                      │              │                 │
│  Chrome     │                │  • prompt generator  │              │  reports/       │
│  headed     │                │  • canvas validator  │              │   ├─ recordings │
│  + video    │                │  • failure triage    │              │   ├─ failures   │
└──────┬──────┘                │  • visual diff       │              │   └─ trend/     │
       │                       │  • exec summary      │              │       ├─ snapshots/
       │ drives                │  • pr review         │              │       └─ dashboard.html
       │                       │  • page-object gen   │              │  baselines/<test>/canvas.png
       ▼                       │  • form-data gen     │              │  cache/ai_prompts/
┌──────────────────────┐       └───────────┬──────────┘              └─────────────────┘
│ pages/*.py           │                   │
│   page objects        │ ◀──────────────── │ JSON verdicts + classifications
│   (one per concern)   │                   │
│                       │                   ▼
│   • flozic_landing    │       ┌──────────────────────────┐
│   • auth_helper       │       │  utils/dashboard_builder │
│   • authv2_helper     │       │   reads in-memory records │
│   • copilot_panel     │       │   + JSON artifacts on disk│
│   • connect_canvas    │       │   → renders dashboard.html│
│   • ...               │       └──────────────────────────┘
└──────────────────────┘
```

---

## 3. Folder structure (current state)

```
automate-workflow-py/
│
├── README.md
├── conftest.py                       # Pytest lifecycle: browser, recording,
│                                     # artifact capture, snapshot archival,
│                                     # dashboard build at session end.
├── pyproject.toml
├── pytest.ini                        # Test markers, log format
├── requirements.txt                  # playwright, pytest, requests
├── .gitignore                        # reports/, cache/, .venv/ excluded
│                                     # baselines/ intentionally tracked
│
├── tests/                            # ── Test files (one per concern) ──
│   ├── __init__.py
│   ├── _flozic_common.py             # Shared driver for all 10 flozic apps
│   ├── _flozic_redirect_probe.py     # Ad-hoc probe (not a real test)
│   ├── flozic_prompts.json           # Hardcoded prompt config per app
│   │
│   │  ┌── flozic.ai entry-point tests (10 hardcoded apps) ──┐
│   ├── test_flozic_chatgpt.py
│   ├── test_flozic_cliniko.py
│   ├── test_flozic_gmail.py
│   ├── test_flozic_gohighlevel.py    # @pytest.mark.xfail (known product bug)
│   ├── test_flozic_google_sheets.py
│   ├── test_flozic_housecall_pro.py
│   ├── test_flozic_microsoft_excel.py
│   ├── test_flozic_mindbody.py
│   ├── test_flozic_paypal.py
│   ├── test_flozic_telegram.py
│   │  └─────────────────────────────────────────────────────┘
│   │
│   ├── test_flozic_diversified.py    # Parametrized: N GPT-generated prompts
│   │                                 #   per app (FLOZIC_VARIANTS_PER_APP=3)
│   │
│   │  ┌── Older / non-flozic tests ──┐
│   ├── test_create_connect_workflow.py
│   ├── test_gohighlevel_mindbody.py
│   ├── test_app_directory.py
│   ├── test_app_pairing.py
│   ├── test_appypie_navigation.py
│   ├── test_auth_entry.py
│   ├── test_authenticated_sanity.py
│   ├── test_error_handling.py
│   ├── test_explore_menu_integration.py
│   ├── test_homepage_exhaustive.py
│   ├── test_homepage_links.py
│   ├── test_rebrand_completion.py
│   ├── test_responsive.py
│   ├── test_top_app_combinations.py
│   ├── test_trending_app_integrations.py
│   │  └─────────────────────────────────────────────────────┘
│   │
│   └── marketing/                    # Homepage / pricing tests
│       ├── __init__.py
│       ├── test_homepage_functional.py
│       ├── test_homepage_seo.py
│       ├── test_homepage_smoke.py
│       ├── test_pricing_functional.py
│       ├── test_pricing_seo.py
│       ├── test_pricing_smoke.py
│       └── test_pricing_links.py
│
├── pages/                            # ── Page Objects (UI abstractions) ──
│   ├── __init__.py
│   │  ┌── flozic.ai connect-creation flow ──┐
│   ├── flozic_landing_page.py        # Hero + per-app entry pages
│   ├── auth_helper.py                # accounts.appypie.com login
│   │                                 #   reads AUTOMATE_EMAIL/PASSWORD env
│   ├── authv2_helper.py              # Cognito second-stage login
│   ├── signup_to_login_switch.py     # gohighlevel /register → /login
│   ├── copilot_panel.py              # left-side copilot detection
│   ├── connect_canvas.py             # canvas populated check + screenshot
│   ├── connect_editor_page.py        # connectcloud canvas (legacy)
│   │  └─────────────────────────────────────────────────────┘
│   │
│   ├── dashboard_page.py             # Dashboard landing
│   ├── app_directory_browse_page.py
│   ├── explore_menu_helper.py
│   ├── error_page.py
│   ├── responsive_helper.py
│   ├── wait_utils.py                 # Generic Playwright wait helpers
│   ├── auth_state.py
│   └── marketing/
│       ├── header_component.py
│       ├── home_page.py
│       ├── pricing_page.py
│       └── app_directory_page.py
│
├── utils/                            # ── Domain logic + AI helpers ──
│   ├── __init__.py
│   ├── config.py
│   ├── test_category.py              # @test_category decorator
│   │
│   │  ┌── Test result persistence ──┐
│   ├── snapshot_writer.py            # In-memory TestRecord -> JSON snapshot
│   │  └─────────────────────────────────────────────────────┘
│   │
│   │  ┌── Health scoring ──┐
│   ├── health_tracker.py             # Singleton aggregator across tests
│   ├── layered_health_scores.py      # Product/Infra/Framework split
│   ├── risk_interpreter.py           # READY/WARNING/AT_RISK/BLOCKED
│   ├── error_clusterer.py            # JS errors -> clusters within a run
│   ├── failure_clustering.py         # Test failures -> clusters across runs
│   ├── js_console_monitor.py         # Page console error subscription
│   │  └─────────────────────────────────────────────────────┘
│   │
│   │  ┌── AI helpers (each opt-in via OPENAI_API_KEY) ──┐
│   ├── ai_prompt_generator.py        # GPT-generated test prompts (cache-by-default opt-in)
│   ├── ai_validator.py               # Canvas screenshot vs. prompt -> VALID/INVALID
│   ├── ai_triage.py                  # Failure -> PRODUCT_BUG/LOCATOR_DRIFT/...
│   ├── ai_visual_diff.py             # Visual regression with semantic diff
│   ├── ai_exec_summary.py            # 2-3 sentence run summary at dashboard top
│   ├── ai_pr_review.py               # Static analysis on git diff
│   ├── ai_page_object.py             # DOM -> draft Page Object skeleton
│   ├── ai_form_data.py               # Synthetic form data, validate-before-cache
│   │  └─────────────────────────────────────────────────────┘
│   │
│   │  ┌── Reporting ──┐
│   ├── dashboard_builder.py          # Self-contained HTML dashboard
│   └── pdf_report_builder.py         # Printable HTML (PDF via window.print
│      └─────────────────────────────────────────────────────┘
│                                     #   or weasyprint if installed)
│
├── scripts/                          # ── Stand-alone CLIs ──
│   ├── promote_baseline.py           # Move canvas.png -> baselines/<test>/
│   ├── ai_pr_review.py               # Wrapper around utils/ai_pr_review.py
│   └── generate_page_object.py       # Wrapper around utils/ai_page_object.py
│
├── baselines/                        # ── Tracked in git (intentional) ──
│   └── <test_method>/canvas.png      # Visual-regression baselines
│                                     # Promoted manually via scripts/promote_baseline.py
│
├── cache/                            # ── Gitignored ──
│   └── ai_prompts/                   # Optional. Opt-in via FLOZIC_USE_PROMPT_CACHE=true
│
├── reports/                          # ── Gitignored, regenerated each run ──
│   ├── recordings/<test_id>/         # PASS path
│   │   ├── recording.webm            # Playwright video
│   │   ├── canvas.png                # Canvas screenshot at end of flow
│   │   ├── ai_verdict.json           # Canvas-vs-prompt verdict
│   │   ├── generated_prompt.json     # If FLOZIC_AI_PROMPT was on
│   │   └── visual_diff.json          # IDENTICAL / COSMETIC / REGRESSION
│   │
│   ├── failures/<test_id>_<ts>/      # FAIL path (overrides above for video)
│   │   ├── screenshot.png            # Page at moment of failure
│   │   ├── dom.html                  # DOM snapshot
│   │   ├── url.txt                   # URL at failure
│   │   ├── recording.webm            # Moved from recordings/
│   │   ├── triage.json               # GPT failure classification
│   │   └── console.log               # If console monitor caught errors
│   │
│   └── trend/
│       ├── dashboard.html            # Self-contained, single file
│       ├── report-printable.html     # PDF-friendly variant
│       ├── trend-history.json        # Rolling last-30-run summary
│       ├── python-health-snapshot.json   # LATEST run snapshot
│       └── snapshots/                # ── Cross-run history ──
│           └── <YYYYMMDD_HHMMSS>.json  # One per session, accrues forever
│
└── docs/                             # ── Operational documentation ──
    ├── system-design.md              # This file
    ├── migration-execution-plan.md
    ├── observation-log.md
    ├── observation-protocol.md
    ├── risk-register.md
    └── snapshot-contract.md
```

---

## 4. Subsystem responsibilities

### 4.1 Browser orchestration — `conftest.py`

The single source of truth for test lifecycle. One pytest fixture per
test creates a fresh Playwright `BrowserContext` + `Page`, attaches a
JS console monitor, runs the test, captures artifacts on failure
(screenshot/DOM/URL/console), invokes AI triage on the captured
artifacts, then moves video into the right destination folder.

At **session teardown** it writes the snapshot JSON, archives a
timestamped copy to `reports/trend/snapshots/`, builds the dashboard,
and generates the PDF-friendly printable report.

### 4.2 Page Objects — `pages/`

Pure UI abstraction. No assertions, no test logic — just methods that
move the browser through the product's flows. Selectors prefer
`get_by_role`, `get_by_text`, attribute locators (`name=`, `data-track`,
`data-testid`). CSS class selectors are explicitly avoided because the
flozic UI uses hash-suffixed classes that change on every deploy.

The flozic connect-creation flow is split across **six** page objects:

```
flozic_landing_page    → opens /integrate/apps/<slug>/integrations, types prompt, clicks build
signup_to_login_switch → handles the gohighlevel /register misroute
auth_helper            → accounts.appypie.com first-stage login
authv2_helper          → authv2.flozic.ai Cognito second-stage login
copilot_panel          → waits for "Connect created!" message on /customeditor
connect_canvas         → waits for canvas cards to populate, screenshots
```

This split exists because each step has independent failure modes — the
page objects survive UI changes in the steps they don't touch.

### 4.3 AI helpers — `utils/ai_*.py`

Eight independent modules, each one a pure function `(input) -> JSON
verdict`. All are graceful no-ops without `OPENAI_API_KEY`. None of
them blocks the test pipeline if OpenAI errors — they degrade to
SKIPPED with a logged reason.

| Module | Input | Output | Cost/run |
|---|---|---|---|
| `ai_prompt_generator` | App slug + N | List of varied prompts | ~$0.02 × 9 apps = $0.18 |
| `ai_validator` | Canvas PNG + user prompt | VALID/INVALID + reasoning | ~$0.015 per test |
| `ai_triage` | Screenshot + DOM + traceback | Category + severity + diagnosis + suggested fix | ~$0.02 per failure |
| `ai_visual_diff` | Current PNG vs baseline PNG | IDENTICAL/COSMETIC/REGRESSION/UNKNOWN | ~$0.025 (skipped if bytes identical) |
| `ai_exec_summary` | Stats + failed test names | 2-3 sentence executive summary | ~$0.01 once per run |
| `ai_pr_review` | Git diff | List of findings per file | ~$0.05 per file |
| `ai_page_object` | DOM HTML | Draft Page Object .py file | ~$0.10 per generation |
| `ai_form_data` | Schema spec + N | N validated rows | ~$0.05 per 50 rows |

Total cost per full run (37 tests × validation + triage on failures +
prompt generation + exec summary): **~$0.50–$0.80 at gpt-4o pricing**.

### 4.4 Health scoring — `utils/health_tracker.py` + `utils/layered_health_scores.py`

Independent of AI. Counts pass/fail/skip, applies decay penalties for
JS-error clusters, produces a 0-100 score split into:

- **Product Health** — pass rate × cluster severity penalty
- **Infrastructure Health** — Playwright/auth/CI subsystem health
- **Framework Health** — test suite's own defect rate

Then `risk_interpreter.py` turns these into a release decision:
`READY` / `WARNING` / `AT_RISK` / `BLOCKED`.

### 4.5 Failure clustering across runs — `utils/failure_clustering.py`

Reads every archived snapshot under `reports/trend/snapshots/`, groups
failures by a normalised fingerprint (`hash(test_method + normalized
exception)`), and computes a recency-weighted recurring score:

```
score = sum_{i ∈ runs_failed} (window - i) / window  ÷  (window+1)/2

  Chronic (every run failed)  → 1.0
  5 most-recent runs failed   → 0.30
  5 stale runs failed         → 0.03
```

This is the signal that lights up the "🔁 Recurring Failures" section
of the dashboard.

### 4.6 Dashboard — `utils/dashboard_builder.py`

The most opinionated file in the project (~1300 lines). Renders a
**single self-contained HTML file** with:

- 30-second triage box at top (visible when not READY)
- AI Executive Summary (GPT narrative; metrics computed in Python, not GPT)
- Health Score panel (Product / Infra / Framework)
- JS Error Clusters table
- 🔴 Issues to Triage table (FAIL + AI-INVALID rows only)
  - Excel-style red gridlines
  - Three copy buttons: Table (HTML), TSV, Markdown
  - Root-cause drill-down chips (PRODUCT_BUG / LOCATOR_DRIFT / …)
  - Collapsible via ▾/▸ caret in the header
- 🔁 Recurring Failures section (cross-run clustering)
- Feature Coverage table
- Test Results table (filter + search + sort + auto-refresh)
- Run History (last 10 runs)

Plus client-side niceties:
- Dark-mode toggle (persisted in `localStorage`)
- Collapsible `.card` sections (persisted)
- Screenshot hover preview (any 📷/🖼️ link)
- Auto-refresh toggle (10s `location.reload()`)
- 🖨️ Export PDF via `window.print()` with `@media print` CSS

All pure vanilla JS, no dependencies. Works offline from `file://`.

### 4.7 Reporting helpers

| File | Purpose |
|---|---|
| `pdf_report_builder.py` | Writes `reports/trend/report-printable.html`. If `weasyprint` is installed, generates a real `.pdf`. |
| `snapshot_writer.py` | Marshals in-memory `TestRecord`s to the snapshot JSON. Schema-versioned for future migrations. |
| `error_clusterer.py` | Groups JS console errors *within a single run* by fingerprint. Different from `failure_clustering.py`, which groups across runs. |

---

## 5. Data-flow diagram (one test, end to end)

```
1. pytest starts test_flozic_chatgpt_connect
   │
   ▼
2. conftest.page fixture
   • new BrowserContext (record_video=on)
   • new Page
   • attach JsConsoleMonitor
   │
   ▼
3. tests/_flozic_common.run_flozic_app_connect_test("chatgpt")
   │
   ├─→ utils/ai_prompt_generator.generate_prompt("chatgpt")   # if FLOZIC_AI_PROMPT=true
   │       └─→ POST openai.com/v1/chat/completions
   │           ← {prompt, trigger_app, action_apps, reasoning}
   │
   ▼
4. pages/flozic_landing_page.FlozicLandingPage
   • goto /integrate/apps/chatgpt/integrations
   • textarea.fill(prompt)
   • button.click()
   ↓
5. pages/auth_helper.perform_login(skip_initial_navigation=True)
   • email/password fill on accounts.appypie.com
   ↓
6. pages/authv2_helper.handle_authv2_login_if_present
   • Cognito Hosted UI second-stage login (Username → Next → Password → Continue)
   ↓
7. Browser lands on connectcloud.appypie.com/customeditor/<id>
   ↓
8. pages/copilot_panel.CopilotPanel
   • wait for .copilot-panel.open.side-left
   • wait for "Connect created!" assistant message
   • extract trigger app name from message
   ↓
9. pages/connect_canvas.wait_for_canvas_populated  (90s poll)
   • wait for "Select Trigger App" placeholder text to disappear
   ↓
10. pages/connect_canvas.screenshot_canvas
    → reports/recordings/test_flozic_chatgpt_connect/canvas.png
   ↓
11. utils/ai_visual_diff.diff_against_baseline
    • compares canvas.png to baselines/test_flozic_chatgpt_connect/canvas.png
    • bytes identical → IDENTICAL (no GPT call)
    • differ → POST openai.com (vision) → COSMETIC/REGRESSION/UNKNOWN
    → reports/recordings/<test>/visual_diff.json
    • REGRESSION → AssertionError (hard fail)
   ↓
12. utils/ai_validator.validate_canvas_with_ai
    • POST openai.com (vision) — sends canvas.png + prompt
    ← {status: VALID|INVALID, reasoning, raw: {trigger_app, action_apps, ...}}
    → reports/recordings/<test>/ai_verdict.json
    • INVALID → AssertionError (hard fail)
   ↓
13. Test body returns / raises
   ↓
14. conftest teardown:
    • on FAIL:
      • FailureArtifactManager.capture → reports/failures/<test>_<ts>/
        ├─ screenshot.png   (Playwright page screenshot)
        ├─ dom.html         (page.content())
        ├─ url.txt
        └─ console.log
      • utils/ai_triage.triage   ← screenshot + DOM + traceback
        → reports/failures/<test>_<ts>/triage.json
    • page.close()
    • move recording.webm to the right destination folder
    • _record_outcome → add_test_record(...)
   ↓
15. (other tests run, accumulating records in utils.snapshot_writer._STATE)
   ↓
16. Session teardown:
    • utils/snapshot_writer.write_snapshot → reports/trend/python-health-snapshot.json
    • copy to reports/trend/snapshots/<YYYYMMDD_HHMMSS>.json
    • utils/dashboard_builder.build:
      • read all session records + AI artifacts on disk
      • load history from snapshots/ for failure clustering
      • render single dashboard.html with all sections, JS, CSS inline
    • utils/pdf_report_builder.write_technical:
      • write report-printable.html
      • optional: weasyprint -> dashboard.pdf
```

---

## 6. Configuration

All runtime config is via environment variables. No config files in
source control beyond `pytest.ini` and `requirements.txt`.

| Env var | Purpose | Default |
|---|---|---|
| `AUTOMATE_EMAIL` | Login email for accounts.appypie.com | empty → manual login fallback |
| `AUTOMATE_PASSWORD` | Login password | empty → manual login fallback |
| `OPENAI_API_KEY` | Enables every `utils/ai_*` module | empty → AI features skip with logged reason |
| `OPENAI_MODEL` | Override the default model | `gpt-5.4` (set this to `gpt-4o` in practice) |
| `FLOZIC_AI_PROMPT` | If `true`, hardcoded tests use AI-generated prompts | `false` |
| `FLOZIC_USE_PROMPT_CACHE` | If `true`, reuse cached generated prompts | `false` (regenerate every run) |
| `FLOZIC_REGEN_PROMPTS` | Bust the prompt cache even if opt-in is on | `false` |
| `FLOZIC_VARIANTS_PER_APP` | Diversified suite — variants per app | `3` |
| `FLOZIC_DIVERSE_APPS` | Diversified suite — apps to cover (comma-list) | 9 working apps |
| `REGEN_SYNTHETIC_DATA` | Force `ai_form_data` to regenerate | `false` |
| `--record-video` (pytest flag) | Record per-test WebM | off |

---

## 7. CLI scripts

| Script | Use |
|---|---|
| `scripts/promote_baseline.py <test_id ...>` | Move `reports/recordings/<test_id>/canvas.png` → `baselines/<test_id>/canvas.png`. `--all` for first-run-state seed. `--list` for status. |
| `scripts/ai_pr_review.py [--base main]` | Run GPT review on `git diff`. Writes `reports/pr_review_<sha>.md`. Always exits 0 unless `--strict` + a blocker finding. |
| `scripts/generate_page_object.py <url-or-html> [--name FooPage]` | Draft a Page Object from a URL or HTML file. Output goes to `pages/<snake>_page.new.py` (never overwrites). |

---

## 8. Run modes (typical commands)

```powershell
# ── Smoke (10 hardcoded apps, ~5 min) ──
python -m pytest (Get-ChildItem tests/test_flozic_*.py -Exclude _*,test_flozic_diversified.py).FullName --headed -v

# ── Full diversified (37 tests = 10 hardcoded + 27 GPT-generated, ~30 min) ──
$env:OPENAI_API_KEY = "sk-..."
$env:OPENAI_MODEL   = "gpt-4o"
python -m pytest (Get-ChildItem tests/test_flozic_*.py -Exclude _*).FullName --headed -v

# ── Single test ──
python -m pytest tests/test_flozic_paypal.py --headed -v

# ── Promote visual baselines (after a known-good run) ──
python scripts/promote_baseline.py --all

# ── PR review locally ──
python scripts/ai_pr_review.py --base main
```

---

## 9. Cost model

Per **full diversified run** (37 tests) at gpt-4o pricing (USD):

| Phase | Calls | Per call | Subtotal |
|---|---:|---:|---:|
| Prompt generation (9 apps × 1 batched call) | 9 | $0.02 | $0.18 |
| Canvas validation (per test) | 37 | $0.015 | $0.56 |
| Failure triage (per FAIL, ~5/run) | 5 | $0.020 | $0.10 |
| Visual diff (IDENTICAL = free; only mismatches call GPT) | ~3 | $0.025 | $0.08 |
| Executive summary (once) | 1 | $0.010 | $0.01 |
| **Total** | | | **~$0.93** |

Smoke run (no diversified): **~$0.15** per run.

Set `FLOZIC_USE_PROMPT_CACHE=true` to cut prompt-generation cost to
~$0.02/run after the first.

---

## 10. Safety properties + non-goals

### Safety properties (enforced by design)

1. **AI helpers are no-op-safe.** Every `utils/ai_*` module degrades to
   SKIPPED when `OPENAI_API_KEY` is unset. No test breaks because AI is
   down.
2. **Validate-before-cache.** `utils/ai_form_data` validates every GPT
   row against a typed schema *before* writing the cache. Below 70%
   accept-rate triggers a retry; persistently bad batches return
   accepted rows but **don't write the cache** — prevents one bad GPT
   response from poisoning hours of future runs.
3. **Numeric metrics never come from GPT.** `ai_exec_summary.py`
   explicitly prompts the model not to restate counts/percentages; those
   are rendered separately in Python.
4. **Visual baselines are intentional.** `baselines/` is tracked in git
   (the framework's only data file in version control). Auto-promotion
   would defeat the purpose; the CLI is the only way to update them.
5. **Triage evidence is preserved.** Every `triage.json` includes the
   verbatim screenshot/DOM/URL/traceback that produced its verdict —
   so a future model can re-classify old failures without re-running.
6. **No credentials in source.** `pages/auth_helper.py` reads
   `AUTOMATE_EMAIL` / `AUTOMATE_PASSWORD` from env only. Empty → manual
   login fallback (3-min wait window). The git history of the active
   branch has been scrubbed of the previously-hardcoded values.
7. **Page-object generator is scaffold-only.** `scripts/generate_page_object.py`
   writes to `<name>.new.py`, never overwriting an existing reviewed
   page object. Post-generation regex guardrails refuse output that
   uses `:nth-child`, `time.sleep`, long `wait_for_timeout`, or CSS
   class/id selectors in `.locator()`.

### Non-goals (deliberate)

- **Not a load tester.** Tests run sequentially in a single Chromium.
- **Not a unit-test harness.** This is end-to-end browser testing.
- **Not coupled to one app.** The flozic-specific tests are 10 of ~30;
  the framework would carry over to any web app with the same shape.
- **PRs are advisory.** `ai_pr_review.py` defaults to exit 0; CI sees
  findings as comments, not gates. `--strict` opts in to blocking on
  blocker-severity findings.
- **AI verdicts are advisory at the row level, not session level.** A
  human can override any individual `ai_verdict.json` by looking at the
  saved canvas screenshot — the framework keeps both the verdict and
  the raw evidence.

---

## 11. Failure modes the framework explicitly distinguishes

| AI triage label | Meaning | Owner | Example from real runs |
|---|---|---|---|
| `PRODUCT_BUG` | Application built the wrong workflow | Engineering | "ChatGPT action set to 'Create image' instead of summarize" |
| `LOCATOR_DRIFT` | DOM element selector no longer matches | QA framework | (Rare; nightly product UI churn) |
| `FLAKE` | Transient — would likely pass on rerun | nobody | "Target page closed mid-test" |
| `INFRA` | Playwright / browser / Python env | QA framework | "Browser crash" |
| `TEST_BUG` | Assertion or logic mistake in test code | QA framework | "Test asserted on contact, prompt asked for deal" |

The triage system prompt is explicitly tuned to prefer `PRODUCT_BUG`
over `LOCATOR_DRIFT` when the missing element is downstream of a
copilot/AI processing step — because the locator is fine, the upstream
backend just didn't produce the element. Same for `TEST_BUG` vs
`PRODUCT_BUG` when the AI validator already determined the canvas was
wrong — that's a product issue, not a test code issue.

---

## 12. Roadmap snapshot (open items, not yet built)

- **CI integration.** Currently runs locally with `--headed`. GitHub
  Actions workflow + headless mode is a half-day of work.
- **Server-side PDF generation.** `weasyprint` install would let the
  conftest auto-generate `dashboard.pdf` per run (currently relies on
  the user clicking 🖨️ Export PDF in the browser).
- **Coverage catalog enforcement** (referenced in `docs/migration-execution-plan.md`).
- **Outcome verifier registry** with the first three verifiers
  (referenced in the older Java-era backlog; not ported to Python yet).
- **TypedAssert + assertion classification** for finer-grained
  test-vs-product attribution.

See `docs/risk-register.md` and `docs/observation-protocol.md` for the
backlog of items inherited from the Java-era framework.

---

## 13. Glossary

| Term | Meaning |
|---|---|
| **Flozic** | The product under test — formerly Appy Pie Automate, now flozic.ai |
| **Copilot** | Flozic's in-product assistant on `/customeditor` that auto-builds a connect from a natural-language prompt |
| **Connect** | A flozic workflow: one trigger app + one or more action apps |
| **Customeditor** | The connect-editor page where the canvas lives |
| **Canvas** | The right-hand workflow diagram showing trigger + action cards |
| **Snapshot** | One JSON file describing one test run's outcomes |
| **Baseline** | A reference canvas PNG that visual regression compares against |
| **Triage** | The AI classification of a failure's root cause |
| **Diversified suite** | The parametrized test that runs N GPT-generated prompts per app |

---

*Last updated: this file is regenerated by hand. When the structure
changes, update Section 3 (folder tree) and Section 5 (data-flow).*
