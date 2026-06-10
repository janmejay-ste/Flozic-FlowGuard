# Migration Risk Register

Tracked risks that affect the **operational viability** of the migration,
not just its technical execution. A migration can succeed technically
while failing operationally — these are the items that determine
whether the migrated suite can sustain itself in CI/CD or remains
human-attended forever.

## Priority summary

The three risks below are NOT peers. R-1 dominates; R-2 and R-3 are
consequences or facts that mostly need acknowledgement, not
investigation.

| Risk | Severity | Uncertainty | Could it materially change the final system? |
|---|---|---|---|
| **R-1 — Turnstile / auth automation** | **HIGH** | **HIGH** | **Yes** — determines whether the migrated suite is CI-runnable or permanently human-attended |
| R-2 — No CI pipeline today | Medium | Low | No — project-management fact, not architectural |
| R-3 — Suite splits into two cohorts | Low | Low | No — characteristic that follows from R-1's status |

Operationally: if R-1 disappeared (a sanctioned bypass became
available), R-2 becomes a 2-hour setup task and R-3 becomes
irrelevant. R-1 is the upstream constraint that makes R-2 and R-3
worth mentioning at all.

The risk register is sorted by impact-weighted severity, not by
discovery order.

## R-1 — Authenticated tests cannot run unattended (Cloudflare Turnstile gate)

**Status:** Open, severity HIGH.
**Surfaced:** Phase 0, Run 1 (2026-06-05).
**Evidence:** `reports/failures/test_sanity_journey_20260605_133326_294/dom.html` shows the multi-step login UI with a `#turnstile-captcha` mount point between email entry and password screen. Headless Chromium cannot satisfy the gate; the test waited the full 30-second automated budget plus the 3-minute manual fallback, then timed out.

### What is and isn't blocked by this risk

| Question | Status |
|---|---|
| Does this block the migration? | **No.** Java has the same constraint and has worked around it via human attendance. |
| Does this make the migration worse than the status quo? | **No.** Python's behaviour mirrors Java's at this surface. |
| Does this block CI/CD adoption of the migrated suite? | **Yes** — for authenticated tests specifically. Unattended-capable tests (smoke, marketing) are unaffected. |
| Is this a pre-existing limitation that the Java suite tolerates? | **Yes.** No CI config exists in the Java repo; the Java auth tests have always been run manually. |

### Concrete consequences

The migrated Python suite, like the Java suite, naturally divides into
two operational categories:

| Category | Members (currently migrated) | CI/CD readiness |
|---|---|---|
| **Unattended-capable** | `PricingSmokeTest`, `HomepageSmokeTest`, `LoginTest` (page-only), `SignupTest` (page-only) = 18 methods | Ready today |
| **Human-attended-required** | `TestAuthenticatedSanityJourney` = 1 method, AND eventually all Connect-workflow / Mindbody / Explore-menu tests that need login | Requires R-1 mitigation OR permanent human-attended workflow |

Once Phase 3/4 migrations land (Connect workflows, Explore menus —
all of which require login), the unattended-capable test count stays
~18 while the human-attended-required count grows to ~60–80. The
unattended portion becomes a shrinking minority of the suite.

### Mitigation paths (decision needed from product/security team)

| Path | Effort | Effect | Trade-offs |
|---|---|---|---|
| **A — Request a Turnstile bypass for test accounts** | Product team work; out-of-scope for the migration | Auth tests run unattended in CI | Requires product/security cooperation; introduces a "test-mode" auth path |
| **B — Pre-seed authenticated session state (cookies/localStorage)** | Engineering — ~4 hr Python work, then per-session refresh | Tests start logged-in; skip the auth UI entirely | Session expires; needs refresh; doesn't test login UI itself |
| **C — Maintain human-attended workflow for auth tests** | No engineering effort | Status quo continues | Excludes auth coverage from CI; relies on developer discipline |
| **D — Use Playwright's `storage_state` mechanism** | Engineering — ~2 hr | Variant of B; uses Playwright's native session-export | Same trade-offs as B; cleaner API |

Path D is the cheapest engineering option and what I'd recommend if
the team wants unattended auth coverage. It involves:
1. One-time: log in headed, export storage_state to JSON
2. Per-test: load storage_state into the browser context, skip the login UI
3. Periodic: refresh storage_state when session expires (~1× per few days)

But **this is a future-state decision, not a migration prerequisite.**
The migration can proceed under path C (status quo). R-1 only becomes
blocking if/when the team decides the authenticated suite must be
CI-runnable.

### When R-1 is decided

Trigger conditions that move R-1 from "open" to "decided":
- Product team confirms whether path A (test-account bypass) is available
- OR engineering allocates time for path B / D (storage_state)
- OR a decision is made that path C (status quo, manual auth runs) is acceptable forever

Until then, the migration treats R-1 as a known constraint and
designs around it.

## R-2 — No CI pipeline exists today

**Status:** Open, severity MEDIUM.
**Surfaced:** Phase 0 grep of `.github/workflows`, `Jenkinsfile`, `.gitlab-ci.yml` — none exist.

### Implication

The "5 days of unattended observation" the migration plan calls for
requires either:
- A developer running the suite daily by hand (Phase 0's current
  approach)
- A new CI pipeline (Java OR Python) to run it automatically

The migration plan assumes evidence of stability over time. Without
CI, that evidence is gated on developer discipline. The 5-day
observation window may take longer than 5 calendar days in practice
because runs depend on someone remembering to execute them.

### Mitigation

| Path | Effort | Effect |
|---|---|---|
| Accept slower-than-calendar observation | None | Observation window may take 1–2 weeks instead of 5 days |
| Set up a minimal GitHub Actions workflow for Phase 0 observation | ~2 hr | Daily runs become automatic |

Recommendation: accept the slower pace for Phase 0; build CI as part
of Phase 6 (cut-over). Building CI now risks contaminating Phase 0's
observation infrastructure with CI-specific issues.

## R-3 — Suite naturally splits into two architecturally-distinct populations

**Status:** Open, severity MEDIUM (consequence of R-1).
**Implication:** The migrated Python suite is not one CI job. It's
two:

1. **Daily unattended job** — runs smoke + marketing + non-auth tests
   in headless mode, headless-safe
2. **On-demand attended job** — runs auth + Connect-workflow +
   Explore-menu tests headed, with operator presence (until R-1
   path A/B/D is chosen)

Job 2 doesn't fit a typical CI scheduler. It needs either:
- A self-service trigger (developer kicks it off manually)
- A scheduled window when someone is on-call to satisfy captcha
- The R-1 mitigation that removes captcha-gating

This architectural split should be made explicit in any cut-over
plan (Phase 6).

## Risk-register policy

This document is updated whenever a new operational constraint
surfaces during the migration. Risks are NOT closed without an
explicit decision recorded.

The format mirrors the C4 / B4 work's discipline: a risk is open
until evidence resolves it, not until enough sessions have passed.

## Standing references

- `docs/migration-execution-plan.md` — the plan these risks affect
- `docs/observation-protocol.md` — the protocol R-2 affects
- `docs/observation-log.md` — Run 1 was the trigger for R-1
- `../automate-workflow-test/docs/evidence-first-analysis.md` —
  framework discipline ("inputs to investigation, not facts")
