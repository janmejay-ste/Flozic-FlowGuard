"""
Marketing-site top navigation component. Python port of the Java
`MarketingHeaderComponent` (only the parts the smoke tests need).

The Java component checks for nav links using href-based selectors
rather than visible-text matching. This is intentional: title attributes
and visible text both change during rebrand cycles, but routing paths
are more stable. We mirror the same approach here.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from playwright.sync_api import Page

logger = logging.getLogger(__name__)


# Mirror of Java PRIMARY_LINKS map. OrderedDict preserves declaration
# order so missingLinks() returns names in a stable order regardless
# of Python dict iteration semantics across versions.
PRIMARY_LINKS: OrderedDict[str, str] = OrderedDict([
    ("Features",      "a[href*='/features'], a[title='features']"),
    ("App Directory", "a[href*='/integrate/app-directory'], a[href*='/app-directory'], "
                      "a[href*='/integrations'], a[title='App Directory']"),
    ("AI Automation", "a[href*='/ai-workflow'], a[href*='/ai-automation'], "
                      "a[title='AI Automation']"),
    ("AI Agents",     "a[href*='/ai-agents'], a[href*='/agents'], a[title='AI Agents']"),
    ("MCP Server",    "a[href*='/mcp'], a[title='MCP Server']"),
    ("Pricing",       "a[href*='/pricing'], a[href*='/pricing-plan'], a[title='Pricing']"),
    ("Blog",          "a[href='/blog/'], a[href$='/blog/']"),
    ("Sign Up",       "a[href*='/register'], a[href*='/signup'], a[title='Sign Up']"),
    # The live header's Login link points at loop.flozic.ai/auth/cognito/login
    # (an app route that then redirects to the Cognito host), so the /login and
    # text/title clauses are what actually match. The host clauses are kept for
    # older marketing builds that linked straight to the IdP.
    # /agent/login is EXCLUDED by name: on the live pricing page a BUY NOW
    # CTA links to loop.flozic.ai/agent/login and the bare [href*='/login']
    # clause clicked it (2026-08-20 run, "[header] Clicking 'Login' ->
    # text='BUY NOW'"). The agent flow is out of scope for these tests.
    ("Login",         "a[href*='auth/cognito/login'], "
                      "a[href*='accounts.flozic.ai'], a[href*='authv2.flozic.ai'], "
                      "a[href*='accounts.appypie'], "
                      "a[href*='/login']:not([href*='/agent/']), "
                      "a[title='log in'], a[title='Login'], "
                      "a:has-text('Login'), a:has-text('Log in'), a:has-text('Sign in')"),
])


class MarketingHeaderComponent:
    def __init__(self, page: Page) -> None:
        self.page = page

    def missing_links(self) -> list[str]:
        """
        Return names of primary nav links NOT present in the DOM.
        Empty list = all primary links present. Mirrors Java
        MarketingHeaderComponent.missingLinks().
        """
        missing: list[str] = []
        for name, selector in PRIMARY_LINKS.items():
            if self.page.locator(selector).count() == 0:
                missing.append(name)
        return missing

    def all_primary_links_visible(self) -> bool:
        return not self.missing_links()

    # ── Click actions (used by Functional tests) ──────────────────────

    @staticmethod
    def _safe_visible(el) -> bool:
        try:
            return el.is_visible()
        except Exception:
            return False

    def _click_first(self, name: str) -> None:
        """Click the first matching element for a primary-nav entry."""
        sel = PRIMARY_LINKS.get(name)
        if not sel:
            raise RuntimeError(f"Unknown primary nav link: {name}")
        loc = self.page.locator(sel)
        # POLL, don't snapshot: the auth buttons (Log In / Sign Up) are a
        # JS-injected island (fz-unified-auth-btns builder). A single pass at
        # t=0 ran before injection finished — the 2026-08-20 failure artifacts
        # contain the visible-by-then anchor that is_visible() had reported
        # hidden moments earlier. Up to ~7s covers the injection window.
        total = 0
        for _attempt in range(14):
            total = loc.count()
            if any(self._safe_visible(loc.nth(i)) for i in range(total)):
                break
            self.page.wait_for_timeout(500)
        for i in range(total):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    el.scroll_into_view_if_needed()
                    # Log the actual target before clicking. These selectors are
                    # 9-clause OR lists, so "clicked Login" says very little —
                    # a:has-text('Sign in') can match a promo or a cookie
                    # banner. When the URL then fails to change, this line is
                    # the difference between a diagnosable failure and a guess.
                    try:
                        logger.info(
                            "[header] Clicking '%s' -> href=%r text=%r (match %d of %d)",
                            name, el.get_attribute("href"),
                            (el.inner_text() or "").strip()[:40], i + 1, total,
                        )
                    except Exception:
                        pass
                    el.click(timeout=5_000)
                    return
            except Exception:
                continue
        # SECOND PASS: the 2026-08-20 header restructure moved Log In into a
        # mega-menu flyout (<ul class="sub-menu-link-list">), so no match is
        # visible until a parent nav item opens. Hover each top-level item
        # that owns a submenu and retry — this is exactly what a user does.
        parents = self.page.locator(
            "li.nav-item:has(ul[class*='sub-menu']) > a, "
            "li.nav-item:has(ul[class*='sub-menu']) > span, "
            "li.nav-item:has(ul[class*='sub-menu']) > button"
        )
        for pi in range(min(parents.count(), 8)):
            par = parents.nth(pi)
            try:
                if not par.is_visible():
                    continue
                par.hover()
                self.page.wait_for_timeout(400)
            except Exception:
                continue
            for i in range(total):
                el = loc.nth(i)
                try:
                    if el.is_visible():
                        logger.info(
                            "[header] Clicking '%s' inside the submenu opened by "
                            "parent %d -> href=%r", name, pi + 1,
                            el.get_attribute("href"),
                        )
                        el.click(timeout=5_000)
                        return
                except Exception:
                    continue
        raise RuntimeError(
            f"No visible '{name}' nav link found, even after opening "
            f"{min(parents.count(), 8)} submenu(s)."
        )

    def click_pricing(self) -> None:        self._click_first("Pricing")
    def click_app_directory(self) -> None:  self._click_first("App Directory")
    def click_features(self) -> None:       self._click_first("Features")
    def click_signup(self) -> None:         self._click_first("Sign Up")
    def click_login(self) -> None:          self._click_first("Login")

    def wait_for_url_contains(self, fragment: str, timeout_sec: int = 15) -> None:
        """Block until the URL contains fragment (case-insensitive)."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        try:
            self.page.wait_for_url(
                lambda url: fragment.lower() in (url or "").lower(),
                timeout=timeout_sec * 1000,
            )
            logger.info("URL transitioned to contain '%s': %s", fragment, self.page.url)
        except PlaywrightTimeoutError:
            logger.warning("URL did not transition to contain '%s' within %ds — current: %s",
                          fragment, timeout_sec, self.page.url)
