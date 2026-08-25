# Mobile Layout Testing — Flozic FlowGuard

Device-emulated Playwright tests that audit the Flozic marketing pages
(homepage, pricing) and auth windows (login, signup) for mobile-specific
layout problems, and flag when the **same page renders inconsistently**
across different phones — not just whether it's broken on one device.

See [`docs/mobile-layout-testing.md`](docs/mobile-layout-testing.md) for
the full design rationale, check catalog, and known gaps.

## What this catches

**Single-device breakage** (is this page broken on *this* phone?)
- Missing/incorrect `<meta name="viewport">`
- Horizontal scroll / overflow, with the specific offending element
- Tap targets under 44×44px (Apple/Google minimum)
- Text rendering under 12px
- Login/signup submit button requiring a scroll to reach

**Cross-device inconsistency** (does the page behave the *same way* on
every phone, or silently diverge?)
- A CTA visible on one device but missing on another
- Nav collapsed (hamburger) on one device, inline/overflowing on another
- Differing heading or form-field counts across devices

## Devices covered

iPhone SE, iPhone 13, iPhone 14 Pro Max, Pixel 5, Galaxy S9+, iPad Mini
(profiles in `pages/mobile/mobile_devices.py`).

## Running it

```bash
# Everything mobile-related
pytest -m mobile -v --headless

# Just the marketing pages (homepage + pricing)
pytest tests/mobile/test_mobile_marketing_layout.py -v --headless

# Just login/signup
pytest tests/mobile/test_mobile_auth_layout.py -v --headless

# One device only, for a quick check
pytest tests/mobile/test_mobile_marketing_layout.py -v --headless -k iPhone_SE
```

## Where results go

This suite writes its own report, separate from the main
`reports/trend/dashboard.html` (by design, while this branch is being
proven out — see the design doc):

- `reports/mobile/mobile-layout-report.html` — open directly in a browser
- `reports/mobile/mobile-layout-report.json` — same data, machine-readable

A `PASSED` in the terminal only means no *blocker*-severity issue was
found — major/minor findings (small tap targets, font sizes, etc.) still
show up in the HTML report even on passing tests, so check it after every
run.

## File map

```
pages/mobile/mobile_devices.py            Device profiles (viewport, UA, touch, DPR)
utils/mobile_layout_auditor.py            Single-device checks
utils/mobile_cross_device.py              Cross-device consistency checks
utils/mobile_report_builder.py            HTML/JSON report writer
tests/mobile/conftest.py                  Device-parametrized fixtures (built on browser_instance)
tests/mobile/mobile_targets.py            URLs + selectors, kept separate from test logic
tests/mobile/test_mobile_marketing_layout.py
tests/mobile/test_mobile_auth_layout.py
```

## Known gaps

- Login/signup submit-button selectors in `mobile_targets.py` are
  best-guess placeholders — confirm against the real DOM and swap in the
  actual locators from `pages/auth_helper.py` / `pages/authv2_helper.py`.
- No visual-regression (screenshot diff) yet.
- Not yet wired into `utils/health_tracker.py` scoring or the main
  dashboard — intentional for now, see design doc.