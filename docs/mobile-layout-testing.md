# Mobile Layout Testing — Design Report

**Branch:** `feature/mobile-layout-testing`
**Scope:** Marketing pages (homepage, pricing) + auth windows (login, signup)
**Status:** Code complete, syntax-verified. Not yet run against the live
site — this sandbox has no network access to flozic.ai / appypie.com, so
selectors in `tests/mobile/mobile_targets.py` are best-guess and should be
confirmed against the real DOM before this branch is merged.

## 1. Problem

The existing suite (`test_responsive.py`) checks responsiveness by
resizing a desktop browser window — it never emulates a real phone's user
agent, touch input, or device pixel ratio, and it doesn't test login or
signup at all. A page can pass that check while still being broken for an
actual mobile visitor (wrong breakpoint triggered, no touch emulation,
desktop font/DPI assumptions).

## 2. Approach

Two different bug classes need two different tests:

| Bug class | Question it answers | Test |
|---|---|---|
| Single-device breakage | Is this page broken *on this phone*? | `test_marketing_page_mobile_layout`, `test_auth_page_mobile_layout` |
| Cross-device inconsistency | Does the page behave the *same way* across phones, or silently diverge? | `test_marketing_page_cross_device_consistency` |

A page that passes single-device checks on every phone individually can
still fail the second test — e.g. the nav collapses to a hamburger on an
iPhone 13 but stays inline (and overflows) on a Galaxy S9+. That gap is
exactly the kind of "inconsistency in how a mobile user views the page"
the task asked for, and it's invisible to any test that only ever looks
at one device at a time.

## 3. What each check detects

**Single-device (`utils/mobile_layout_auditor.py`)**
- Missing/incorrect `<meta name="viewport">` (blocker)
- Horizontal scroll / overflow, and the specific offending element (major)
- Tap targets under 44×44px, the Apple/Google minimum (minor)
- Text rendering under 12px (minor)
- Critical CTA (login/signup submit) requiring a scroll to reach (minor)

**Cross-device (`utils/mobile_cross_device.py`)**
- A CTA visible on one phone but missing on another (major)
- Nav collapsed on one phone, inline on another (major — usually a
  breakpoint gap between the two devices' widths)
- Differing heading or form-field counts across devices (minor/blocker)

## 4. Devices covered

`pages/mobile/mobile_devices.py` uses Playwright's built-in device
profiles (real user-agents, viewport, DPR, touch emulation) rather than
hand-rolled ones:

- iPhone SE (small iOS)
- iPhone 13 (mainstream iOS)
- iPhone 14 Pro Max (large iOS)
- Pixel 5 (mainstream Android)
- Galaxy S9+ (different aspect ratio)
- iPad Mini (tablet control case)

## 5. How to run (once merged and pointed at real URLs)

```bash
git checkout -b feature/mobile-layout-testing
# copy in pages/mobile/, utils/mobile_*.py, tests/mobile/, updated pytest.ini
pytest -m mobile -v
```

Findings write to `reports/mobile/mobile-layout-report.html` (self-contained,
opens from `file://`) and a parallel `.json` for future integration into
`utils/dashboard_builder.py` or `utils/failure_clustering.py`.

## 6. Known gaps / next steps

- `tests/mobile/mobile_targets.py` selectors for the login/signup submit
  buttons are placeholders — replace with the real locators from
  `pages/auth_helper.py` / `pages/authv2_helper.py` once run against the
  live DOM.
- No visual-regression (screenshot diff) yet — could reuse
  `utils/ai_visual_diff.py`'s pattern per device.
- Not yet wired into `utils/health_tracker.py` scoring or the main
  dashboard; currently a standalone report by design, to keep this branch
  isolated until it's proven out.
