"""
tests/mobile/flozic_marketing_flows.py

Real inventory of the Flozic marketing site's mobile-relevant surface.

EVERY SELECTOR AND FLOW HERE WAS READ OUT OF THE SOURCE, not guessed.
Source of truth: the Pixazo-AI/flozic-platform-marketing repo, specifically
`public/index.html` (the 5,552-line static homepage) and
`scripts/smoke-test.mjs` (the 31 critical-path assertions the team maintains).

WHY THIS FILE EXISTS
--------------------
The first mobile suite pointed at `accounts.appypie.com` — a host two
migrations stale — and its interaction checks matched on generic patterns like
`[class*='hamburger']`. The site does not use that word anywhere. A
pattern-matched check that finds nothing reports "not applicable" forever,
which reads like coverage and is not. Grounding the selectors in the actual
markup is the difference between testing the site and testing a guess.

BREAKPOINTS
-----------
Taken from the `@media` queries in public/index.html, not from a list of
popular phones. The mobile-relevant ones are 768 / 640 / 600 / 560. Testing
AT and either side of a boundary is what catches a layout that only settles
on one side of it.

KNOWN-FRAGILE SPOTS
-------------------
scripts/smoke-test.mjs carries a `3b. mobile-overflow` group, which means the
team has already shipped and fixed mobile overflow bugs twice:
  * the Legal-Entity table needs an overflow-x container below 472px
  * a long-label CTA overflowed the homepage at 320px
Both are regression-prone by definition. They get explicit checks.
"""

from __future__ import annotations

MARKETING_BASE = "https://www.flozic.ai"

# ── Pages ──────────────────────────────────────────────────────────────
# Mirrors the critical-path groups in scripts/smoke-test.mjs so the mobile
# suite covers the same surface the team already treats as load-bearing.
PAGES: dict[str, str] = {
    "homepage":      "/",
    "app_directory": "/integrate/app-directory",
    "pricing":       "/integrate/pricing-plan",
    "features":      "/integrate/features",
    "contact":       "/contact-us",
    "app_hub":       "/integrate/apps/gmail/integrations",
}

# ── Chrome ─────────────────────────────────────────────────────────────
# header#site-header.main-header > nav#connectTopNavigation.navbar.fixed-top
# `fixed-top` is why the sticky checks matter: it is a genuinely fixed
# element on every page, and it must neither drift while scrolling nor cover
# a control the user is trying to tap.
HEADER = "header#site-header"
TOP_NAV = "nav#connectTopNavigation"
FOOTER_NAV = "#connectBottomNavigation"

# Bootstrap 4 collapse — NOT Bootstrap 5 (`data-toggle`, not `data-bs-toggle`),
# so it depends on Bootstrap's JS being loaded and bound. That is exactly the
# kind of thing that renders fine and does nothing when tapped.
MENU_TOGGLE = "button.navbar-toggler"
MENU_PANEL = "#navbarNav"
# Bootstrap adds .show to the collapse target when open, and flips
# aria-expanded on the toggler. Two independent signals; assert both.
MENU_OPEN_CLASS = "show"

# ── Hero prompt flow — the site's primary conversion path ──────────────
# From the inline "HERO PROMPT FLOW JS" block in public/index.html:
#   * #generateButton ships disabled + aria-disabled="true"
#   * typing into #hero-prompt fires 'input' -> refreshState() -> enables it
#   * #charCount renders "<len>/1000", gaining .is-near-limit at >=900
#   * tapping a .pill / .pill--combo fills the textarea from data-q
#   * .pill--app are real <a> links and are DELIBERATELY skipped by the
#     handler (`if (p.tagName === 'A') return;`) so they navigate instead
#   * Generate base64-encodes the prompt and redirects to
#     https://loop.flozic.ai/connects
HERO_TEXTAREA = "#hero-prompt"
HERO_CHARCOUNT = "#charCount"
HERO_GENERATE = "#generateButton"
HERO_PILL_FILL = ".pill--combo, .pill:not(.pill--app):not(a)"
HERO_PILL_LINK = ".pill--app"
HERO_MAXLENGTH = 1000
HERO_DESTINATION_HOST = "loop.flozic.ai"

# ── Other interactive elements found in the markup ─────────────────────
MODALS = ("#RequestAppModal", "#myModal")
DISMISSIBLE_ALERT = ("#automateAlert", "#automateClose")
LANG_CHANGER_DATA = "#flozic-i18n-data"
# Section anchors that the in-page nav targets. Used to verify that an
# anchor tap actually scrolls, and lands clear of the fixed header.
SECTION_ANCHORS = (
    "#Features", "#how", "#pricing-plan", "#app-directory", "#blog", "#mcpserver",
)

# ── Contact form (Contact Form 7, id from the markup) ──────────────────
CONTACT_FORM = "form#wpcf7-f13088-o1, form"
CONTACT_FIELDS = {
    "name": "input[name*='name' i]:not([type='hidden'])",
    "email": "input[type='email'], input[name*='email' i]",
    "message": "textarea",
}

# ── Breakpoints from the site's own CSS ────────────────────────────────
# Values are the @media max-widths in public/index.html. `-1 / +1` pairs
# straddle each boundary, which is where a layout that only settles on one
# side of the query shows up.
CSS_BREAKPOINTS = (560, 600, 640, 768)
BREAKPOINT_PROBES = tuple(
    sorted({320, 360, 390, 430}
           | {b - 1 for b in CSS_BREAKPOINTS}
           | set(CSS_BREAKPOINTS))
)

# ── Known-fragile, from the team's own smoke test ──────────────────────
# scripts/smoke-test.mjs group "3b. mobile-overflow".
LEGAL_ENTITY_MAX_WIDTH = 472      # table needs an overflow-x container below this
HOME_CTA_MIN_WIDTH = 320          # long-label CTA overflowed here


def url(page_key: str) -> str:
    """Absolute URL for a PAGES key."""
    return MARKETING_BASE + PAGES[page_key]


def is_hero_destination(current_url: str | None) -> bool:
    """True when the hero Generate CTA landed where its handler sends it."""
    return HERO_DESTINATION_HOST in (current_url or "")
