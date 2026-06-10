# Observation Window Log

Per-run records from the Phase 0 observation period. Updated each run.
The end-of-window report at `docs/observation-window-report.md` will
aggregate these into the formal conclusion.

## Run 1 — 2026-06-05, ~13:29 (headless)

**Command:** `pytest --cohort baseline`
**Browser mode:** Headless (no `--headed`)
**Wall-clock:** 243.85 s (~4:04)
**Snapshot:** `reports/trend/observation/baseline-2026-06-05-run1-headless.json`

| Result | Count |
|---|---:|
| Passed | 18 |
| Failed | 1 |
| **Total** | **19** |

### Failure details

| Test | Category | Evidence |
|---|---|---|
| `TestAuthenticatedSanityJourney.test_sanity_journey` | `framework_timing` (or `infrastructure` — headless captcha constraint) | Captured DOM at `reports/failures/test_sanity_journey_20260605_133326_294/` shows the email-then-Cloudflare-Turnstile login UI; password field never appeared because the captcha gate stayed unsatisfied. 30s automated wait timed out; 3-min manual fallback expired without intervention (headless = no human to satisfy captcha). |

**One-sentence reason:** Headless Chromium cannot satisfy the Cloudflare Turnstile gate that appears between email entry and the password screen on `accounts.appypie.com`. This is a known constraint already documented in the README; not a new finding.

### Categorization rationale

The failure is NOT `product` (the auth flow works fine for real users
and in headed mode), NOT `selector_drift` (the selectors are correct
for the captcha-deferred shape), NOT `unknown` (cause is well
understood). It's headless-mode-vs-captcha — `framework_timing` is
the closest fit since it's about timing budget exhaustion in a mode
where the gate cannot clear.

### Decision impact

Per the protocol's impact-based rule: a single understood failure in
a known-headless-constrained test does NOT trigger the "pause" rule.
The 18 smoke tests (the ones that should be headless-safe) all
passed.

**Implication for the observation window design:**
- The smoke tests can run unattended headless across the window
- The auth test requires `--headed` for any meaningful observation
- Future observation runs should be split into:
  - `pytest -m smoke --cohort baseline` (unattended, headless, daily)
  - `pytest tests/test_authenticated_sanity.py --headed --cohort baseline` (with-presence, less frequent)

This is a Phase 0 finding that adjusts the observation procedure, not
a finding that affects the migration conclusion.

## Run summary so far

| Date | Mode | Total | Pass | Fail | Unresolved? |
|---|---|---:|---:|---:|---|
| 2026-06-05 (1) | headless | 19 | 18 | 1 | No (failure understood) |

## Notes for the operator running the remaining window

For days 2–5 of the observation window:

```powershell
# Smoke tests — headless, unattended, daily
cd "C:\Users\Janmejay\OneDrive\Desktop\automate-workflow-py"
.\.venv\Scripts\Activate.ps1

# Run only the headless-safe portion
pytest tests/marketing tests/test_auth_entry.py --cohort baseline
Copy-Item reports\trend\python-health-snapshot.json `
  reports\trend\observation\baseline-$(Get-Date -Format yyyy-MM-dd)-smoke.json

# Then separately, when you can attend the browser:
pytest tests/test_authenticated_sanity.py --headed --cohort baseline
Copy-Item reports\trend\python-health-snapshot.json `
  reports\trend\observation\baseline-$(Get-Date -Format yyyy-MM-dd)-auth-headed.json
```

After each run, append a new section to this log following the same
template as Run 1.

After 5 runs across at least 3 calendar days, write the end-of-window
report at `docs/observation-window-report.md` applying the protocol's
decision rules.
