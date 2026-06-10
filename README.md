# automate-workflow-py

Python / Playwright / pytest counterpart to the Java / Selenium / TestNG
project at `../automate-workflow-test/`. Implements Phase 2 + Phase 3
of `docs/python-playwright-migration-plan.md` from the Java repo —
project skeleton + first migrated test class.

## What is and is not in this repo

**Migrated (here, Python):**
- Browser-driven test infrastructure (pytest fixtures replacing
  `BaseTest`)
- The `AuthenticatedTest` sanity journey
- Page objects required by that one test
- A minimal snapshot writer that emits v3-schema-compatible JSON

**NOT migrated (still in Java repo):**
- Scoring engine (`HealthPolicy`, `HealthTracker`, `RiskInterpreter`)
- Dashboard HTML builder
- PDF report builders
- C4 cluster scoring, B4 schema migration
- All other browser-driven tests (queued for Phase 4)

The Python stack writes to the **same** `reports/trend/` directory the
Java stack reads. The Java DashboardBuilder remains the canonical
report generator. This is the hybrid model from the migration plan.

## Quick start

```powershell
# 1. Install Python 3.11+ first (check with: python --version)

# 2. Create a virtual environment (recommended)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install Playwright browsers (Chromium only, one-time)
playwright install chromium

# 5. Run the migrated test
pytest tests/test_authenticated_sanity.py -v

# Run all tests (currently just one)
pytest

# Run with video recording
pytest --record-video

# Run in headed mode (visible browser)
pytest --headed
```

## Repository layout

```
automate-workflow-py/
├── pyproject.toml             # project metadata + tool config
├── requirements.txt           # pip-installable deps
├── pytest.ini                 # markers, log format, fixtures
├── conftest.py                # session + function fixtures
├── pages/                     # page objects (Playwright Locator-based)
│   ├── __init__.py
│   ├── auth_helper.py         # login flow (replaces ManualLoginHelper)
│   ├── dashboard_page.py      # dashboard + create-connect + logout
│   └── wait_utils.py          # overlay/loader helpers
├── utils/
│   ├── __init__.py
│   ├── test_category.py       # @test_category decorator (replaces @TestCategory)
│   └── snapshot_writer.py     # writes v3-schema test records
├── tests/
│   ├── __init__.py
│   └── test_authenticated_sanity.py
├── reports/                   # created at runtime (gitignored)
│   ├── failures/              # screenshot + dom + console per failed test
│   ├── recordings/            # Playwright .webm videos
│   └── trend/                 # snapshot files; Java reads from here
└── docs/
    └── snapshot-contract.md   # v3 schema contract (cross-language)
```

## Current status — Phase 3 proof of concept

This is **one** test class migrated to validate that Playwright auto-wait
addresses the StaleElementReferenceException pattern that dominates the
Java stack's failures. Migration of further test classes is gated on
the comparison data from running this stack side-by-side with the Java
equivalent for 5 days (per migration plan Phase 3 unlock condition).

What this proves so far:
- ✅ pytest fixtures can replace `BaseTest` lifecycle
- ✅ Playwright page objects can replace Selenium page objects
- ✅ v3-schema-compatible snapshot files can be written from Python

What this does NOT yet prove:
- ❓ Whether Playwright is materially better than Selenium for THIS app's
  flakiness profile (needs 5-day side-by-side run)
- ❓ Whether the snapshot bridge actually integrates cleanly with the
  Java DashboardBuilder (needs end-to-end verification)
- ❓ Whether the hybrid CI pipeline works (needs CI configuration)

These are the Phase 3 → Phase 4 gates documented in the migration plan.

## References

- `../automate-workflow-test/docs/python-playwright-migration-plan.md`
  — full plan this repo implements a slice of
- `../automate-workflow-test/docs/evidence-first-analysis.md` — the
  analytical discipline applied to every claim in this repo's docs
- `../automate-workflow-test/docs/scoring-system.md` — the scoring
  engine this repo intentionally does NOT migrate
