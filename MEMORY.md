# MEMORY — Flozic FlowGuard

**Purpose:** portable onboarding brief for another Claude session, agent,
or engineer picking up this project on a new device. Read this first
before making changes.

Last updated: 2026-06-27 (after Phase 1 provider-abstraction pilot).

---

## Project in one paragraph

Flozic FlowGuard is a **Python/Playwright/pytest QA observability platform**
for [flozic.ai](https://www.flozic.ai) — an AI workflow-automation product
(rebranded from Appy Pie Automate). It goes beyond "test runner" into
platform territory: browser automation, AI-assisted semantic validation
(canvas + popup), matrix-aware verdicts, failure clustering, historical
trend analysis, health scoring, visual regression, and structured network
observability. `docs/system-design.md` is the canonical architecture doc;
`PROJECT_STRUCTURE.md` is a thin directory map.

---

## User + account context

- **User:** Janmejay (`janmejay@appypiellp.com`), QA engineer.
- **Test account state:** the login account is on the **Enterprise plan** —
  the highest tier. This matters for pricing tests: every downgrade attempt
  (Standard / Professional / Business, Yearly or Monthly) should produce a
  BLOCK popup. Override via `FLOZIC_CURRENT_PLAN=<name>` if that changes.
- **Working hours:** IST evening / late night.
- **Preferred cadence:** short focused updates, concrete deliverables per
  commit, one thing at a time. Not a fan of over-explaining.

---

## Repository + branches

- **Remote:** `https://github.com/janmejay-ste/Flozic-FlowGuard.git`
- **Default branch on GitHub:** `feature/ai-dashboard-flozic-tests`
  (there is NO `main` branch — verify before ever proposing a "merge to main")
- **Current working branch:** `feature/framework-architecture-improvements`
- **Never merge to the default branch without explicit user approval.**
  Recommended workflow: PR via `gh pr create --base
  feature/ai-dashboard-flozic-tests --head <current>`.

### Auth caveat
GitHub credentials on this machine may be stale. If `git push` returns 404,
the PAT was revoked and needs regeneration at
https://github.com/settings/tokens. **Never paste credentials into chat**
— multiple PATs have already been exposed in this project's history and
had to be revoked.

---

## Environment contract

Read these before running anything:

```bash
# Required for login-required tests. Missing → 3-min manual-login fallback
# per test. Fine locally, HANGS CI.
export AUTOMATE_EMAIL="janmejay@appypiellp.com"
export AUTOMATE_PASSWORD="<password>"

# One of these must be set for AI features (validators, triage, popup check).
# Missing → tests fall back to keyword-only classification (still pass).
export ANTHROPIC_API_KEY="sk-ant-..."      # preferred if set
export OPENAI_API_KEY="sk-proj-..."         # fallback / legacy

# Optional: force provider selection (default: auto per keys above)
export FLOZIC_AI_PROVIDER="claude"          # or "openai"
export CLAUDE_MODEL="claude-sonnet-5"
export OPENAI_MODEL="gpt-4o"

# Pricing test override — matrix-aware validator computes expected popup
# category from this. Default "Enterprise" is correct for this account.
export FLOZIC_CURRENT_PLAN="Enterprise"

# Optional toggle: disable network capture entirely
export FLOWGUARD_NETWORK_CAPTURE=1
```

Setup:
```bash
cd ~/Flozic-FlowGuard
source .venv/bin/activate           # not from parent dir
python -m pytest tests/ --headed -v
```

Headed is safer than headless — Cloudflare Turnstile on `authv2.flozic.ai`
can block headless. Only use `--headless` if you've confirmed it works for
your account.

---

## Architecture layers (from `docs/system-design.md`)

```
tests/                → pytest test files (assertions live here)
  ├── test_flozic_<app>.py       → per-app integration tests
  ├── test_flozic_agent_<bot>.py → conversational-agent tests (xfail for now)
  ├── test_pricing_plan_try_now.py → pricing matrix × plan × period
  ├── test_app_pairing.py        → app directory + pairing + full sign-in
  └── unit/                       → pure-Python regression (schema, normalization)

pages/                → Playwright Page Objects (browser interactions)
  └── marketing/       → marketing-side pages (pricing, homepage)

utils/                → domain logic + AI helpers + observability
  ├── network_monitor.py         → HTTP capture (schema v1 frozen)
  ├── failure_signals.py         → normalized failure evidence (sprint-2 wip)
  ├── ai_provider.py             → LLM provider abstraction (Claude/OpenAI)
  ├── claude_client.py           → Anthropic Messages API wrapper
  ├── ai_validator.py            → canvas verdict
  ├── ai_popup_validator.py      → pricing popup matrix-aware verdict
  ├── ai_triage.py               → failure root-cause classification
  ├── dashboard_builder.py       → self-contained HTML dashboard
  ├── health_tracker.py          → pass/fail scoring
  └── ... (see docs/system-design.md for the full list)

scripts/              → stand-alone CLIs (baseline promotion, PR review,
                         page-object scaffolding, artifact validation)
reports/              → gitignored; generated per run
  ├── failures/<test>_<ts>/     → per-failure artifacts (DOM, screenshot,
                                    network-events.json, network-summary.json,
                                    triage.json, video, AI verdicts)
  └── trend/                     → dashboard.html + snapshots + run summary
```

---

## Conventions to follow

1. **Assertions live in tests, not page objects.** Page objects only
   interact with the browser and return data.
2. **Every ai_*.py module goes through `utils/ai_provider.send()`.**
   Never call `requests.post()` to an LLM directly.
3. **`schema_version` in every JSON artifact.** Bumps require an intentional
   change; tests in `tests/unit/` pin the current version.
4. **Test files own their test category** via `@test_category(...)`
   decorator (used by health scoring + cohort tagging).
5. **When you find a product bug, don't hide it** — surface it on the
   dashboard via `[ISSUE]` warning logs, and either xfail the test with a
   clear reason OR let it fail loud with structured diagnosis.
6. **Never commit credentials or PATs.** Even in test data. Use env vars.
7. **Commits should be atomic** — one concern per commit. Docs changes,
   framework fixes, and new features go in separate commits.

---

## Product bugs currently tracked

| Bug | Status | Test evidence |
|---|---|---|
| Agent flow was misrouting `/agents/conversational/*` to `/connects` instead of `/agent/builder` | **FIXED on product side (~2026-06-26)** | 5 agent tests now reach `/agent/builder?agent=chat` |
| Agent OAuth lands on `authv2.flozic.ai/signup` instead of `/login` | **ACTIVE** — auto-recovered in `authv2_helper.py` via signup→login switch | `[ISSUE]` warning fires for every agent test |
| ChatGPT workflow action defaults to "Create image" regardless of prompt | **ACTIVE product bug** — AI/Workflow team | Recurring in `test_flozic_chatgpt_connect` and `test_flozic_diversified[chatgpt-*]` |
| HubSpot trigger picks "Deal" when prompt says "Lead" | **ACTIVE** — matrix-aware AI validator flags this correctly | `test_flozic_diversified[paypal-v1]` (variant with HubSpot Lead trigger) |
| Cliniko trigger picks wrong event ("archived" instead of "scheduled") | **ACTIVE** | `test_flozic_diversified[cliniko-v1/v2]` |
| App pairing flows misroute to `/register` (signup) before login | **RECOVERED by framework** via `switch_signup_to_login_if_needed()` | Tests pass but `[ISSUE]` warning surfaces the misroute |
| Marketing header login-link selector drift | **INTERMITTENT** — appears in some runs | `test_header_login_routes_to_auth_domain` |

---

## What's been built recently (Sprint 1 + partial Sprint 2)

### Sprint 1 — Network observability layer (complete)
- `NetworkMonitor` / `NetworkAnalyzer` / `NetworkExporter` in
  `utils/network_monitor.py`
- Three-layer redaction (sensitive URLs, sensitive headers, sensitive JSON keys)
- Per-test `network-summary.json` + `network-events.json` in failure folders
- Session-level `network_overview.json` with avg/P95 latency, failed_percentage
- Golden-file regression test at `tests/unit/test_network_schema.py`
- Schema v1 **frozen** — bumps require test update

### Sprint 2 (in progress) — Failure fingerprinting
- **2a landed:** `utils/failure_signals.py` with normalization primitives
  (URL/message/traceback), `FailureSignalBuilder`, `FailureSignals` dataclass,
  16 unit tests in `tests/unit/test_failure_signals.py`
- **2b next:** `utils/fingerprint_engine.py` — weighted hash (50% endpoint+status,
  25% AI diagnosis, 15% exception type, 10% network context). Writes
  `fingerprints.json` at session end.
- **2c next:** Dashboard root-cause card — "3 root causes affecting 12 tests"
- Add `docs/fingerprinting.md` when 2b lands.

### Phase 1 — Provider abstraction (pilot landed, rollout pending)
- `utils/claude_client.py` + `utils/ai_provider.py` shipped
- `utils/ai_validator.py` refactored as pilot (uses provider abstraction)
- **Pending:** roll out to 8 remaining ai_*.py modules
  (`ai_triage`, `ai_popup_validator`, `ai_visual_diff`, `ai_exec_summary`,
   `ai_prompt_generator`, `ai_pr_review`, `ai_page_object`, `ai_form_data`)
- Before the rollout, extract shared utilities:
  - `utils/ai_prompts.py` — prompt template registry
  - `utils/ai_parser.py` — JSON repair + retry-on-malformed
  - `utils/ai_cost_tracker.py` — per-call cost, session cap, module attribution

### New tests added this branch
- 11 new prompt-based UI tests (6 new integration apps + 5 conversational agents)
- 6-variant pricing plan tests (Yearly/Monthly × Standard/Professional/Business)
- App pairing extended with full sign-in flow + workflow verification + logout

---

## Active roadmap (revised after architectural review)

Order was revised to prioritize **analysis over execution** — AI-powered
understanding gives more ROI than AI-powered clicking for this framework's
maturity level.

```
Phase 1 — Provider Foundation                    ← IN PROGRESS
  ├── Provider abstraction (LANDED)
  ├── Extract ai_prompts / ai_parser / ai_cost_tracker (NEXT)
  └── Roll out to remaining 8 ai_*.py modules

Phase 3 — Failure Analyzer                       ← HIGHEST ROI NEXT
  ├── 3a. Failure Investigator (structured root-cause analysis)
  └── 3b. Bug Writer (Jira-ready markdown, human-in-the-loop)

Phase 4 — Trend Intelligence + Release Summary
  ├── Trend Intelligence — natural-language regression detection
  └── AI Release Summary — nightly report

Phase 2 — Semantic Self-Healing                  ← MOVED LATER
  ├── ProposedAction schema (Claude proposes, Engine executes)
  ├── Semantic vs Product recovery split
  ├── knowledge/selectors.json (rich KB, not simple cache)
  └── Recovery caps (per-test / per-session / per-page)

Phase 5 — Test Planner Agent
  └── Requirement → scenarios → coverage gaps → pytest skeleton

Phase 6+ — Goal Runner (deferred)
  └── Only for exploratory / discovery tests, never regression
```

## Load-bearing architectural decisions

Do not violate these without explicit user approval:

1. **LLM never controls the browser directly.** It proposes structured
   actions; a deterministic RecoveryEngine validates and executes. This is
   Phase 2's central safety invariant.
2. **Semantic Recovery ≠ Product Recovery.** If the button was renamed,
   recover semantically. If it was removed, FAIL the test — do not hide
   product regressions as "recovered selectors."
3. **Every artifact is versioned.** `schema_version` field in every JSON;
   golden-file tests pin the current schema.
4. **Providers are pluggable.** All AI calls flow through `ai_provider.send()`.
   The framework must work with Claude, OpenAI, or neither (noop fallback).
5. **Passing tests contribute to session aggregates.** Do not couple
   aggregation to failure-only paths — repeat mistake from earlier design.

---

## How the current session ended

- Phase 1 pilot committed at `ad6fb0f` (local, push blocked by 404).
- `authv2_helper.py` now auto-recovers when OAuth lands on `/signup`.
- Provider abstraction lets you switch to Claude with 2 env vars.
- Next commit should extract the shared AI utilities before rolling out
  the provider abstraction to the remaining 8 ai_*.py modules.

## How to resume on a new device

1. Clone the repo (or pull if already cloned):
   ```bash
   git clone https://github.com/janmejay-ste/Flozic-FlowGuard.git
   cd Flozic-FlowGuard
   git checkout feature/framework-architecture-improvements
   git pull
   ```
2. Set up the venv:
   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   pip install anthropic       # only needed if using Claude provider
   ```
3. Export env vars per the "Environment contract" section above.
4. Read `docs/system-design.md` for architecture.
5. Read this MEMORY.md for context of what's been happening.
6. Check `git log --oneline -20` for recent commits.
7. Run `tests/unit/` to verify the framework is healthy locally:
   ```bash
   python -m pytest tests/unit/ -v
   ```
   Expected: 21 passing, ~0.3s.

## Ask, don't assume

If you're a Claude session picking this up cold:
- Ask what phase they want to work on next (many possible next moves).
- Never commit or push without confirmation.
- Never merge to the default branch without explicit approval.
- Never paste credentials in chat.
