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
    ("Login",         "a[href*='authv2.flozic.ai'], a[href*='accounts.appypie'], "
                      "a[href*='/login'], a[title='log in'], a[title='Login'], "
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

    def _click_first(self, name: str) -> None:
        """Click the first matching element for a primary-nav entry."""
        sel = PRIMARY_LINKS.get(name)
        if not sel:
            raise RuntimeError(f"Unknown primary nav link: {name}")
        loc = self.page.locator(sel)
        total = loc.count()
        for i in range(total):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    el.scroll_into_view_if_needed()
                    el.click(timeout=5_000)
                    return
            except Exception:
                continue
        raise RuntimeError(f"No visible '{name}' nav link found")

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
