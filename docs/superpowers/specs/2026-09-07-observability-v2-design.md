# Observability v2 — design spec

**Goal:** when a test fails, the framework has already captured enough evidence that a
developer can identify the failing layer (backend / frontend / selector) **without asking
QA to reproduce**. Move from *"the UI state never appeared"* to *"the backend job stayed
`pending`"* / *"the API returned 500"* / *"the API succeeded but the UI never rendered it"*.

**Origin:** external review of the 2026-09 reports — "the next maturity step is diagnostic
evidence, not more dashboards." The report layer is frozen; this is capture-side work.

## What exists today (per failed test, in `reports/failures/<test>_<ts>/`)
screenshot.png · dom.html · url.txt · recording.webm · triage.json (AI diagnosis) ·
network-events.json (**full request/response bodies**) · network-summary.json (status
digest — already surfaced on evidence cards). Sidecar carries git commit + environment.

**Gaps:** console errors are in-memory only (never written per failure); no Playwright
trace; backend state at failure time is captured *in* network bodies but never extracted;
no correlation ID linking a failure to a specific connect/workflow.

## Phase 1 — extract what we already have (small, no new capture)
1. **console.json per failure** — dump the session `JsConsoleMonitor` events for the
   failing test's window into the failure folder (data already in memory at teardown,
   `conftest.py` ~663).
2. **Connect/workflow ID extraction** — the editor URL in `url.txt` embeds it
   (`/customeditor/<connect_id>/account/<account_id>`). Parse into `failure.json`
   (new small manifest: test, signature, connect_id, page_url, timestamps).
3. **Backend-state extraction from captured traffic** — scan the failure's
   network-events.json for the *last* relevant API responses
   (`getByConnectId`, `customerconnect/*`, copilot endpoints) and record
   `{endpoint, status, body_excerpt}` in failure.json. This alone answers
   "did the backend respond, and with what?" for the timeout cluster.
4. **Surface on evidence cards** — one line per card: `Backend at failure:
   getByConnectId → 200, status:"pending"` (or "no relevant API traffic captured").

Acceptance: every FAIL folder gains console.json + failure.json; evidence cards show the
backend-state line; zero new runtime cost (post-processing existing captures at teardown).

## Phase 2 — Playwright tracing (medium; runtime + disk cost)
- `context.tracing.start(screenshots=True, snapshots=True)` per test;
  `stop(path=trace.zip)` **only on failure**, `stop()` discard on pass.
- Costs: ~5–15% runtime overhead, ~2–10 MB per failing test. Mitigations: cap retained
  traces per run (e.g. 20, newest first), never embed in the report (link only), add
  trace path to the evidence card ("open with `playwright show-trace`").
- Rollout flag: `FLOWGUARD_TRACE=1` env toggle, default ON for full runs, OFF for subsets.

Acceptance: a failing connect test yields a trace.zip a dev can open to scrub the exact
timeline (DOM snapshots, network, console) of the failure.

## Phase 3 — active backend correlation (optional; needs a product decision)
Poll the product API for the connect/job status at the moment of failure (session cookies
are live in the browser context). Adds the authoritative "backend job state = X" even when
the UI captured no relevant traffic. **Deferred** until Phases 1–2 prove insufficient:
it couples tests to product API shape and needs an owner's sign-off on API usage.

## Non-goals
- No report/layout changes beyond the new evidence-card lines (report structure frozen).
- No AI in the capture path — extraction is deterministic; the AI triage stays advisory.
- No change to scoring, gating, or the systemic detector (they consume, not produce).

## Order & effort
Phase 1: ~half a day, all wins, no risk → do first. Phase 2: ~a day incl. disk management
and a full-run validation. Phase 3: spec separately if still needed after 1–2.
