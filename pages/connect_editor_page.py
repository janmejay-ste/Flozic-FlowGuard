"""
Connect editor page object — pragmatic port of pages.ConnectEditorPage
covering the surface used by CreateConnectWorkflowTest and
GoHighLevelMindbodyConnectTest.

The Java original is ~2000 lines with many fallback strategies. This
port keeps the same selectors and same method names so future
refinement is straightforward, but doesn't replicate every Java
defensive helper. Methods are written with Playwright auto-waiting in
mind, which eliminates large chunks of the Java retry logic.

Coverage:
  - select_trigger_app / select_action_app
  - select_trigger_event / select_action_event
  - click_continue (multiple variants)
  - click_continue_run_test
  - handle_setup_step (custom dropdowns)
  - click_add_new_step / click_add_app
  - fill_gmail_draft_setup (best-effort)
  - fill_mindbody_sale_setup (best-effort, three field-input strategies)
  - click_activate_connect
"""

from __future__ import annotations

import logging
import re
import time
from typing import Mapping

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)


class ConnectEditorPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    # ── Visibility ────────────────────────────────────────────────────

    def is_editor_visible(self, timeout_ms: int = 5_000) -> bool:
        for sel in [
            "app-custom-editor",
            ".connect-editor",
            "[class*='customeditor']",
        ]:
            try:
                self.page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
                return True
            except PlaywrightTimeoutError:
                continue
        return "customeditor" in (self.page.url or "")

    # ── App selection (trigger + action) ──────────────────────────────

    def _search_and_select_app(self, app_name: str, timeout_ms: int = 20_000) -> None:
        """
        Select an app from the app-picker panel.

        Priority strategy:
        1. Click `a[data-track='select application']` directly if the app is
           already visible in the default panel list (no search needed). This
           is the element Angular's (click) handler is bound to — clicking a
           child <p> or <div> does NOT always register selection properly.
        2. If not found, search using fill() + dispatch_event("input") and
           wait for the filtered list to update.
        3. After search, target `a[data-track='select application']` again.
        4. Fall back to CSS :has-text() candidates if the data-track approach
           doesn't find the app (e.g. older editor pages without that attribute).
        """
        # Step 1: Check if the app is already visible without searching
        direct_sel = f"a[data-track='select application']:has-text('{app_name}')"
        direct_loc = self.page.locator(direct_sel).first
        try:
            direct_loc.wait_for(state="visible", timeout=4_000)
            direct_loc.click(timeout=10_000)
            logger.info("App '%s' selected (direct, no search)", app_name)
            return
        except (PlaywrightTimeoutError, Exception):
            pass

        # Step 2: Search for the app
        search = self.page.locator(
            "input[placeholder*='Search' i], "
            ".search-app input, "
            "input.app-search, "
            ".inputgroup-search input"
        ).first
        search.wait_for(state="visible", timeout=timeout_ms)
        # Use fill() + dispatch_event("input") so Angular's reactive binding fires.
        # press_sequentially is more natural but on some panels fill+dispatch is
        # more reliable because the input has debounce rather than per-keystroke logic.
        # Also try press_sequentially as secondary to cover both patterns.
        search.click()
        search.fill(app_name)
        try:
            search.dispatch_event("input")
        except Exception:
            pass
        self.page.wait_for_timeout(1_500)
        # If the list hasn't updated, also type char-by-char as a second trigger
        after_fill = self.page.locator(direct_sel).first
        try:
            after_fill.wait_for(state="visible", timeout=2_000)
            after_fill.click(timeout=10_000)
            logger.info("App '%s' selected (fill+dispatch, data-track)", app_name)
            return
        except (PlaywrightTimeoutError, Exception):
            pass
        # press_sequentially fallback for Angular ngModel bindings.
        #
        # CLEAR FIRST. This block runs after the fill() above already put
        # `app_name` in the box, so typing without clearing appends to it and
        # the box becomes "GmailGmail" — which matches nothing, so the search
        # falls through to the broad XPath fallback below and takes ~28s to
        # land on a stale card. Observed live on 2026-08-11.
        search.click()
        search.fill("")
        search.press_sequentially(app_name, delay=40)
        self.page.wait_for_timeout(2_000)
        logger.debug(
            "App search box now contains: %r", search.input_value()
        )

        # Step 3: After search, try data-track selector first then CSS fallbacks
        candidates = [
            (direct_sel, 5_000),                                         # Angular target
            (f"[class*='app-name']:has-text('{app_name}')", 3_000),
            (f"[class*='appName']:has-text('{app_name}')",  3_000),
            (f"[class*='app-title']:has-text('{app_name}')", 3_000),
            (f".app-item:has-text('{app_name}')",            3_000),
            (f"li:has-text('{app_name}')",                   3_000),
            (                                                             # XPath contains fallback
                f"xpath=//div[contains(@class,'app') and contains(normalize-space(.),'{app_name}')] | "
                f"//span[contains(normalize-space(.),'{app_name}')] | "
                f"//p[contains(normalize-space(.),'{app_name}')]",
                3_000,
            ),
        ]
        result = None
        for sel, wait in candidates:
            loc = self.page.locator(sel).first
            try:
                loc.wait_for(state="visible", timeout=wait)
                result = loc
                break
            except PlaywrightTimeoutError:
                continue

        if result is None:
            # Last-resort: broadest XPath with the full timeout
            result = self.page.locator(
                f"xpath=//*[contains(normalize-space(.),'{app_name}')]"
                f"[not(self::script or self::style or self::head or self::html or self::body)]"
            ).first
            result.wait_for(state="visible", timeout=timeout_ms)

        result.click(timeout=10_000)
        logger.info("App '%s' selected", app_name)

    def select_trigger_app(self, app_name: str) -> None:
        logger.info("Selecting trigger app: %s", app_name)
        self._search_and_select_app(app_name)

    def select_action_app(self, app_name: str) -> None:
        logger.info("Selecting action app: %s", app_name)
        self._search_and_select_app(app_name)

    # ── Event selection ──────────────────────────────────────────────

    def _select_event(self, event_name: str, timeout_ms: int = 20_000) -> None:
        # Priority-ordered probe. normalize-space(.)='X' (equality) is tried
        # first because it's most precise; contains(normalize-space(.),X) is
        # tried second for event cards that wrap text in child spans so that
        # the parent element's normalize-space output includes icon text too.
        # CSS :has-text() is the broadest fallback — it uses substring matching
        # on any descendant text, which handles Mindbody-style event lists.
        candidates = [
            (
                f"xpath=//span[normalize-space(.)='{event_name}']"
                f" | //label[normalize-space(.)='{event_name}']",
                min(timeout_ms, 8_000),
            ),
            (
                f"xpath=//span[contains(normalize-space(.),'{event_name}')]"
                f"[not(ancestor::script)]"
                f" | //label[contains(normalize-space(.),'{event_name}')]",
                min(timeout_ms, 8_000),
            ),
            (f"li:has-text('{event_name}')",          5_000),
            (f".event-item:has-text('{event_name}')", 5_000),
            (f"[class*='event']:has-text('{event_name}')", 5_000),
        ]
        label = None
        for sel, wait in candidates:
            loc = self.page.locator(sel).first
            try:
                loc.wait_for(state="visible", timeout=wait)
                label = loc
                break
            except PlaywrightTimeoutError:
                continue

        if label is None:
            raise PlaywrightTimeoutError(
                f"Event '{event_name}' not found after trying all selectors"
            )

        # The span is wrapped by a clickable parent (radio/li). The span itself
        # may be reported as not-enabled by Playwright's actionability check.
        # Try a normal click with force fallback, then JS-click as last resort.
        try:
            label.click(timeout=5_000)
        except Exception:
            try:
                label.click(timeout=5_000, force=True)
            except Exception:
                label.evaluate("el => el.click()")
        self.page.wait_for_timeout(2_000)  # Angular settle
        logger.info("Event '%s' selected", event_name)

    def select_trigger_event(self, event_name: str) -> None:
        self._select_event(event_name)

    def select_action_event(self, event_name: str) -> None:
        self._select_event(event_name)

    # ── Continue buttons ──────────────────────────────────────────────

    def click_continue(self, timeout_ms: int = 20_000) -> None:
        """
        Click the first visible Continue button. The Angular UI uses
        aria-disabled="true" on the <a> Continue, so we JS-click as a
        fallback.

        NOTE: Do NOT use btn.count() == 0 as an early exit. Angular may not
        have rendered the next panel into the DOM yet immediately after a prior
        Continue click. count() is synchronous and instant — it misses elements
        that appear within the next few seconds. Let wait_for() handle the
        waiting for each candidate instead.
        """
        # Per-selector wait times: give the account panel extra time to render
        # because Angular re-hydrates it via HTTP after the event panel is dismissed.
        wait_times = {
            "[data-track='continue with event']":   3_000,
            "[data-track='continue with account']": 8_000,
            "button:has-text('Continue'):not([data-track='continue'])": 3_000,
            "a:has-text('Continue'):not([data-track='continue'])":      3_000,
        }
        candidates = list(wait_times.keys())
        for sel in candidates:
            btn = self.page.locator(sel).first
            try:
                btn.wait_for(state="visible", timeout=wait_times[sel])
            except PlaywrightTimeoutError:
                continue
            try:
                btn.click(timeout=5_000)
                logger.info("Continue clicked via: %s", sel)
                self.page.wait_for_timeout(1_500)
                return
            except (PlaywrightTimeoutError, Exception):
                # aria-disabled may block normal click; try JS-click
                try:
                    btn.evaluate("el => el.click()")
                    logger.info("Continue JS-clicked via: %s", sel)
                    self.page.wait_for_timeout(1_500)
                    return
                except Exception:
                    continue
        raise RuntimeError("No visible Continue button found")

    def advance_to_setup_step(self, max_continues: int = 4) -> int:
        """
        Click through whatever intermediate Continue panels this app has until
        the setup step (the one carrying "Continue & Run Test") is reached.
        Returns how many Continues were clicked.

        Why this exists: the number of panels between choosing an event and
        reaching setup VARIES BY APP. Gmail goes event -> account -> setup;
        others skip the account panel, and some interpose an extra confirm.
        Hardcoding the count is what broke the Gmail run on 2026-08-11 — the
        test called click_continue() once, landed on the account panel, and
        then timed out looking for [data-track='continue'] which only exists
        one panel later.

        Idempotent: if the run-test button is already visible this returns 0
        without clicking anything.
        """
        clicked = 0
        for _ in range(max_continues):
            run_test = self.page.locator("[data-track='continue']").first
            try:
                run_test.wait_for(state="visible", timeout=2_500)
                logger.info(
                    "Reached setup step after %d Continue click(s).", clicked
                )
                return clicked
            except PlaywrightTimeoutError:
                pass
            try:
                self.click_continue()
                clicked += 1
            except RuntimeError:
                # No Continue available and no run-test button either — the
                # caller's next wait will report the real state.
                logger.warning(
                    "No Continue button and no run-test button after %d "
                    "click(s). Current URL: %s", clicked, self.page.url,
                )
                return clicked
        logger.warning(
            "Clicked %d Continue(s) without reaching the setup step. URL: %s",
            clicked, self.page.url,
        )
        return clicked

    def handle_setup_step_if_present(
        self,
        targeted: Mapping[str, str] | None = None,
    ) -> bool:
        """
        Run handle_setup_step() when the panel actually has dropdowns.

        Some steps (Gmail's "New Email") have no configurable dropdowns at all,
        and handle_setup_step() raises RuntimeError in that case. Returns True
        if dropdowns were handled, False if there were none.
        """
        if self.page.locator("div[id^='menu-drop']").count() == 0:
            logger.info("No setup dropdowns on this step — nothing to configure.")
            return False
        self.handle_setup_step(targeted=targeted)
        return True

    def click_continue_run_test(self, timeout_ms: int = 20_000) -> None:
        btn = self.page.locator("[data-track='continue']").first
        btn.wait_for(state="visible", timeout=timeout_ms)
        try:
            btn.click(timeout=10_000)
        except Exception:
            btn.evaluate("el => el.click()")
        logger.info("'Continue & Run Test' clicked")
        # Wait for the canvas to return to the add-step state. The editor
        # runs the test asynchronously; the canvas (with the + button) appears
        # once the run completes. We do NOT try to wait for a spinner — the
        # spinner is inside the slide-out panel which closes on its own.
        # click_add_new_step already has a 60s timeout to handle slow runs.
        self.page.wait_for_timeout(2_000)

    # ── Setup-step dropdowns ──────────────────────────────────────────

    def handle_setup_step(
        self,
        targeted: Mapping[str, str] | None = None,
    ) -> None:
        """
        Handle menu_icon-box dropdown fields. If `targeted` is supplied,
        each field gets the matching search term; otherwise the first
        option is selected (with "Test Sheet" as the default for index 0,
        mirroring Java's spreadsheet-flow default).
        """
        drops = self.page.locator("div[id^='menu-drop']")
        count = drops.count()
        if count == 0:
            raise RuntimeError("No menu-drop elements found in setup step")
        logger.info("Found %d custom dropdown(s)", count)

        for i in range(count):
            drop = drops.nth(i)
            try:
                drop_id = drop.get_attribute("id") or ""
                name = ""
                inp = drop.locator("input[name]").first
                if inp.count() > 0:
                    name = inp.get_attribute("name") or ""

                search_term = ""
                if targeted is not None:
                    search_term = targeted.get(name, "") or targeted.get(drop_id, "")
                if not search_term and i == 0 and targeted is None:
                    search_term = "Test Sheet"

                trigger = drop.locator(".menu_icon-box, .dropdown-trigger").first
                if trigger.count() == 0 or not trigger.is_visible():
                    continue
                trigger.click(timeout=5_000)
                # Dropdown #0 (Spreadsheet) is immediate; dropdown #1 (Worksheet)
                # is populated via API call after a spreadsheet is chosen — give
                # it extra time to load before trying to click the first option.
                wait_ms = 2_000 if i > 0 else 800
                self.page.wait_for_timeout(wait_ms)

                if search_term:
                    sinput = self.page.locator(".search-list input, input[placeholder*='Search' i]").last
                    if sinput.count() > 0 and sinput.is_visible():
                        sinput.fill(search_term)
                        self.page.wait_for_timeout(700)

                # Wait up to 3 s for at least one option to appear (API response).
                # The Angular component renders options inside `.menu_dropdown`
                # (confirmed from app-edit-options source: positionDropdown queries
                # `.menu_dropdown`). Also covers legacy `.list-data .single-data`
                # and ng-select patterns.
                first_opt = self.page.locator(
                    "li.menu_dropdown-option, "
                    ".menu_dropdown li, "
                    ".menu_dropdown .single-data, "
                    ".list-data .single-data, "
                    ".option-item, "
                    ".ng-option:not(.ng-option-disabled)"
                ).first
                try:
                    first_opt.wait_for(state="visible", timeout=3_000)
                    first_opt.click(timeout=5_000)
                    logger.info("Dropdown #%d (%s): selected (search='%s')",
                                i, name or drop_id, search_term)
                except PlaywrightTimeoutError:
                    logger.warning(
                        "Dropdown #%d (%s): no options visible after wait — closing dropdown",
                        i, name or drop_id
                    )
                    # Close the open dropdown so it doesn't block the canvas /
                    # copilot panel.
                    #
                    # Keyboard Escape does NOT close these dropdowns — the
                    # Angular component closes them via jQuery's
                    # $(document).click() handler which removes 'menu_active'.
                    # Directly manipulating the class is the reliable method.
                    try:
                        self.page.evaluate(
                            """() => {
                                document.querySelectorAll('.menu.menu_active')
                                    .forEach(el => el.classList.remove('menu_active'));
                                document.querySelectorAll('.editoption-dropmenu.active_menu')
                                    .forEach(el => el.classList.remove('active_menu'));
                            }"""
                        )
                        self.page.wait_for_timeout(300)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("Dropdown #%d error: %s", i, str(e).splitlines()[0])

    # ── Dependent dropdowns ───────────────────────────────────────────
    #
    # handle_setup_step() above walks every dropdown in ONE pass with a 3s wait
    # for options. That is fine for independent fields and wrong for a
    # dependent chain: Google Sheets' Worksheet list is fetched from the API
    # only after a Spreadsheet is chosen, and it does not arrive in 3s. The
    # 2026-08-11 run showed exactly that —
    #     Dropdown #0 (spreadsheetId): selected (search='')
    #     Dropdown #1 (sheetId): no options visible after wait
    # — so Worksheet stayed empty and the row fields that depend on it never
    # loaded at all.
    #
    # set_dropdown_by_field() sets ONE field, waits properly for its options,
    # and verifies the value took, so a caller can walk a chain in order.

    # Human field labels -> the DOM input[name] the product uses. Needed
    # because handle_setup_step matches on the input's `name`, and "Worksheet"
    # has no textual relationship to "sheetId".
    FIELD_ALIASES: Mapping[str, tuple[str, ...]] = {
        "spreadsheet": ("spreadsheetid",),
        "worksheet": ("sheetid", "worksheetid"),
        "label": ("labelids",),
    }

    # `li.menu_dropdown-option` first: the 2026-08-20 editor redesign ("Set
    # Up:" panel with Advanced/AI mapping buttons) renders options as
    # <li id="single-dropdown" class="menu_dropdown-option"> inside a BARE
    # <ul> — no .menu_dropdown ancestor — so every older selector missed them
    # while the options API returned 200 with data. Proven from the
    # 2026-08-20_103235 failure artifact, where 'Test Sheet' was in the DOM
    # while the page object reported "never populated".
    OPTION_SELECTOR = (
        "li.menu_dropdown-option, "
        ".menu_dropdown li, "
        ".menu_dropdown .single-data, "
        ".list-data .single-data, "
        ".option-item, "
        ".ng-option:not(.ng-option-disabled)"
    )

    @staticmethod
    def _norm(s: str | None) -> str:
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    @classmethod
    def _field_matches(cls, wanted: str, name: str, drop_id: str) -> bool:
        w, n, d = cls._norm(wanted), cls._norm(name), cls._norm(drop_id)
        if not w:
            return False
        if w == n or w == d:
            return True
        for alias in cls.FIELD_ALIASES.get(w, ()):
            if alias == n or alias in d:
                return True
        return w in n or (n and n in w)

    def _find_dropdown(self, field: str):
        """The menu-drop container whose input name / id matches `field`."""
        drops = self.page.locator("div[id^='menu-drop']")
        for i in range(drops.count()):
            drop = drops.nth(i)
            drop_id = drop.get_attribute("id") or ""
            name = ""
            inp = drop.locator("input[name]").first
            if inp.count() > 0:
                name = inp.get_attribute("name") or ""
            if self._field_matches(field, name, drop_id):
                return drop, (name or drop_id)
        return None, None

    def set_dropdown_by_field(
        self,
        field: str,
        search_term: str = "",
        timeout_ms: int = 45_000,
    ) -> str:
        """
        Set one setup dropdown and confirm it took.

        `field` may be a human label ("Worksheet") or the DOM name ("sheetId").
        `search_term` empty means "take the first option" — used where the
        choice genuinely doesn't matter.

        Returns the option text that was selected, so the caller can record
        which value a FIRST_AVAILABLE choice resolved to.

        Raises AssertionError if the field isn't present or never populates —
        a silent skip here is what let the previous run continue with an empty
        Worksheet and fail 75s later with a misleading message.
        """
        drop, resolved_name = self._find_dropdown(field)
        if drop is None:
            present = []
            drops = self.page.locator("div[id^='menu-drop']")
            for i in range(drops.count()):
                inp = drops.nth(i).locator("input[name]").first
                present.append(
                    (inp.get_attribute("name") if inp.count() else None)
                    or drops.nth(i).get_attribute("id")
                )
            raise AssertionError(
                f"No setup dropdown matching {field!r}. Present: {present}. "
                "Add an entry to ConnectEditorPage.FIELD_ALIASES if the "
                "product renamed the input."
            )

        trigger = drop.locator(".menu_icon-box, .dropdown-trigger").first
        trigger.wait_for(state="visible", timeout=15_000)
        trigger.click(timeout=10_000)

        # Wait for options. This is the long wait the old code lacked: a
        # dependent list is an API round-trip after the parent changed.
        #
        # SCOPE MATTERS. A page-wide locator's .first is the first match in DOM
        # ORDER, which is an option belonging to an EARLIER, now-closed dropdown
        # (Spreadsheet). Its .is_visible() is False forever, so the wait timed
        # out on 2026-08-11 even though Worksheet's options were on screen.
        # Prefer options inside this field's own container, and fall back to a
        # :visible-filtered page query for builds that portal the menu out.
        options = self._options_for(drop)
        end = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < end:
            options = self._options_for(drop)
            if options.count() > 0:
                break
            self.page.wait_for_timeout(1_000)
        else:
            self._close_open_dropdowns()
            raise AssertionError(
                f"Dropdown {resolved_name!r} never populated within "
                f"{timeout_ms}ms. If this is a dependent field, its parent was "
                "probably not set to a valid value."
            )

        if search_term:
            sinput = self.page.locator(
                ".search-list input, input[placeholder*='Search' i]"
            ).last
            if sinput.count() > 0 and sinput.is_visible():
                sinput.fill("")
                sinput.fill(search_term)
                self.page.wait_for_timeout(1_200)

        options = self._options_for(drop)
        target = options.filter(
            has_text=re.compile(re.escape(search_term), re.I)
        ).first if search_term else options.first

        try:
            target.wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeoutError:
            visible = [
                (options.nth(i).text_content() or "").strip()
                for i in range(min(options.count(), 10))
            ]
            self._close_open_dropdowns()
            raise AssertionError(
                f"Dropdown {resolved_name!r} has no option matching "
                f"{search_term!r}. First options offered: {visible}"
            ) from None

        chosen = (target.text_content() or "").strip()
        target.click(timeout=10_000)
        self.page.wait_for_timeout(1_500)
        logger.info(
            "Dropdown %r: selected %r (searched %r)",
            resolved_name, chosen, search_term or "<first>",
        )
        return chosen

    def _options_for(self, drop):
        """Visible options belonging to `drop`.

        Container-scoped first. The page-wide fallback is :visible-filtered on
        purpose — an unfiltered page query returns options from other, closed
        dropdowns and .first then never becomes visible.
        """
        scoped = drop.locator(self.OPTION_SELECTOR).filter(visible=True)
        if scoped.count() > 0:
            return scoped
        visible_sel = ", ".join(
            f"{part.strip()}:visible" for part in self.OPTION_SELECTOR.split(",")
        )
        return self.page.locator(visible_sel)

    def _close_open_dropdowns(self) -> None:
        """Escape doesn't close these — the Angular component relies on a
        jQuery document-click handler. Strip the active classes directly."""
        try:
            self.page.evaluate(
                """() => {
                    document.querySelectorAll('.menu.menu_active')
                        .forEach(el => el.classList.remove('menu_active'));
                    document.querySelectorAll('.editoption-dropmenu.active_menu')
                        .forEach(el => el.classList.remove('active_menu'));
                }"""
            )
            self.page.wait_for_timeout(300)
        except Exception:
            pass

    # ── Add-step + add-app ────────────────────────────────────────────

    def click_add_new_step(self, timeout_ms: int = 60_000) -> None:
        # 60s ceiling: this runs after Continue & Run Test which can take
        # 15-30s for the webhook round-trip; the + button only appears after
        # the page has advanced from /options/ (setup) to the canvas view.
        #
        # Root cause of previous failures: the broad selectors
        # `button[aria-label*='add' i]` and `[data-track*='add']` matched
        # `quick-action-chip` autocomplete suggestions inside the still-open
        # setup panel when Worksheet was not selected (required field empty),
        # causing `Continue & Run Test` to not advance the page.
        #
        # Fix: (a) wait for the page to leave /options/ URL, (b) use only
        # explicit "Add Action App" text + a well-scoped + button CSS class.
        # Do NOT use aria-label wildcards or data-track wildcards here.

        # Wait for the setup-options page to close and the canvas to appear.
        # The URL changes from /options/... to /customeditor/... (no /options/).
        try:
            self.page.wait_for_url(
                lambda url: "/options/" not in (url or ""),
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            # Page may already be on the canvas — proceed anyway
            pass

        # Try selectors in priority order; stop at the first visible hit.
        # `#add-new-step-button` is the real circular "+" button in the canvas
        # (confirmed from DOM: id="add-new-step-button" fconnectioncenter).
        # The copilot chips are tried second as they also open the app picker.
        selectors = [
            "#add-new-step-button",                   # real canvas + button (DOM confirmed)
            "button:has-text('Add Action App')",       # copilot chip
            "a:has-text('Add Action App')",
            "[class*='addActionApp']:visible",
            ".add-step-btn:visible",
            # Blue + circle: must be in the canvas area, NOT in a dropdown
            "app-custom-editor .plus-icon",
            "app-custom-editor [class*='plus']",
            ".canvas-container [class*='plus']",
        ]
        for sel in selectors:
            loc = self.page.locator(sel).first
            try:
                loc.wait_for(state="visible", timeout=5_000)
                # Try normal click first, fall back to JS-click if the element
                # is technically outside the viewport (e.g. copilot chip inside
                # a CSS-transformed or overflow-hidden container).
                try:
                    loc.scroll_into_view_if_needed(timeout=3_000)
                    loc.click(timeout=5_000)
                except (PlaywrightTimeoutError, Exception):
                    loc.evaluate("el => el.click()")
                self.page.wait_for_timeout(800)
                logger.info("Add-new-step clicked via: %s", sel)
                return
            except (PlaywrightTimeoutError, Exception):
                continue

        # Last resort: wait the full remaining budget on the first explicit text,
        # then JS-click to bypass viewport constraints.
        btn = self.page.locator("button:has-text('Add Action App')").first
        btn.wait_for(state="visible", timeout=timeout_ms)
        try:
            btn.scroll_into_view_if_needed(timeout=5_000)
            btn.click(timeout=5_000)
        except (PlaywrightTimeoutError, Exception):
            btn.evaluate("el => el.click()")
        self.page.wait_for_timeout(800)

    def click_add_app(self, timeout_ms: int = 15_000) -> None:
        # After click_add_new_step selects "Add Action App", the app-search
        # panel opens. This method is now a no-op passthrough since the step
        # above already opened the app selector. Keep it for GoHighLevel test
        # compatibility but make it safe to call on an already-open panel.
        btn = self.page.locator(
            "button:has-text('Add Action App'), "
            "a:has-text('Add Action App')"
        ).first
        try:
            if btn.is_visible(timeout=2_000):
                btn.click(timeout=10_000)
                self.page.wait_for_timeout(1_500)
            else:
                # Panel already open from click_add_new_step — skip
                logger.info("Add Action App panel already open — skipping click_add_app")
        except Exception:
            logger.info("click_add_app: panel already open or not needed")

    # ── Field-fill strategies (Mindbody-style and Gmail) ──────────────

    def fill_gmail_draft_setup(self) -> None:
        """Best-effort: map To / Subject / Body via choices-container."""
        for field in ("to", "subject", "body"):
            try:
                self._map_choices_field(field)
            except Exception as e:
                logger.warning("[Gmail] could not map '%s': %s",
                               field, str(e).splitlines()[0])

    def _map_choices_field(self, field_name: str) -> None:
        trigger = self.page.locator(
            f"[id*='{field_name}'] .choices__inner, "
            f"label:has-text('{field_name}') ~ div .choices__inner"
        ).first
        trigger.wait_for(state="visible", timeout=5_000)
        trigger.click(timeout=5_000)
        self.page.wait_for_timeout(500)
        first = self.page.locator(".choices__item--choice:not(.choices__item--disabled)").first
        if first.is_visible():
            first.click(timeout=5_000)

    def fill_mindbody_sale_setup(
        self,
        quantity: str = "1",
        amount: str = "1000",
        notes: str = "Automation Test",
    ) -> None:
        """Best-effort port of fillMindbodySaleSetup."""
        self.page.wait_for_timeout(1_500)
        for fn, val, method in [
            ("quantity", quantity, self._fill_mindbody_custom_value),
            ("amount", amount, self._fill_mindbody_content_editable),
            ("notes", notes, self._fill_mindbody_direct_input),
        ]:
            try:
                method(fn, val)
            except Exception as e:
                logger.warning("[Mindbody] field '%s' skipped: %s",
                               fn, str(e).splitlines()[0])

    def _fill_mindbody_custom_value(self, field_name: str, value: str) -> None:
        trigger = self.page.evaluate_handle(
            """
            (fn) => {
                const drops = document.querySelectorAll('[id^="menu-drop"]');
                for (const d of drops) {
                    const btn = d.querySelector('.readmorebutton2');
                    const label = d.previousElementSibling || d.parentElement;
                    if (btn && (d.id.toLowerCase().includes(fn.toLowerCase()) ||
                        (label && label.innerText.toLowerCase().includes(fn.toLowerCase()))))
                        return btn;
                }
                return null;
            }
            """,
            field_name,
        )
        if not trigger or trigger.json_value() is None:
            raise RuntimeError(f"readmorebutton2 not found for '{field_name}'")
        trigger_el = trigger.as_element()
        if not trigger_el:
            raise RuntimeError(f"readmorebutton2 element not found for '{field_name}'")
        trigger_el.click()
        self.page.wait_for_timeout(500)
        custom_input = self.page.locator(
            ".checkIfNotBlurAfteraWhile input, div[contenteditable='true']"
        ).last
        custom_input.fill(value)
        custom_input.press("Enter")
        self.page.wait_for_timeout(300)

    def _fill_mindbody_content_editable(self, field_name: str, value: str) -> None:
        ok = self.page.evaluate(
            """
            ([fn, v]) => {
                const drops = document.querySelectorAll('[id^="menu-drop"]');
                for (const d of drops) {
                    if (!d.id.toLowerCase().includes(fn.toLowerCase())) continue;
                    const ce = d.querySelector('div[contenteditable="true"]');
                    if (ce && ce.offsetParent !== null) {
                        ce.focus();
                        document.execCommand('selectAll', false, null);
                        document.execCommand('insertText', false, v);
                        ce.dispatchEvent(new Event('input', {bubbles:true}));
                        ce.dispatchEvent(new Event('change', {bubbles:true}));
                        return true;
                    }
                }
                return false;
            }
            """,
            [field_name, value],
        )
        if not ok:
            raise RuntimeError(f"contenteditable not found for '{field_name}'")
        self.page.wait_for_timeout(300)

    def _fill_mindbody_direct_input(self, field_name: str, value: str) -> None:
        ok = self.page.evaluate(
            """
            ([fn, v]) => {
                const drops = document.querySelectorAll('[id^="menu-drop"]');
                for (const d of drops) {
                    const inp = d.querySelector(
                        `input[name="${fn}"]:not([type="hidden"]),textarea[name="${fn}"]`);
                    if (inp && inp.offsetParent !== null) {
                        inp.value = v;
                        inp.dispatchEvent(new Event('input', {bubbles:true}));
                        inp.dispatchEvent(new Event('change', {bubbles:true}));
                        return true;
                    }
                }
                return false;
            }
            """,
            [field_name, value],
        )
        if not ok:
            raise RuntimeError(f"direct input not found for '{field_name}'")
        self.page.wait_for_timeout(300)

    # ── Activate Connect ──────────────────────────────────────────────

    def click_activate_connect(self, timeout_ms: int = 30_000) -> None:
        """
        Activate the connect and VERIFY it actually activated.

        This used to be a false pass. The old version was:

            btn = self.page.locator(
                "button:has-text('Activate'), ... button.active_agent_Button"
            ).first
            try:    btn.click()
            except: btn.evaluate("el => el.click()")
            logger.info("Connect ACTIVATED")

        Three compounding faults, observed on 2026-08-11 when the suite
        reported PASSED and 100/100 health for a connect that was never
        activated:

          1. `.first` over a comma-separated OR takes the first match in DOM
             order, which can be a different button whose text merely contains
             "Activate".
          2. The product's button is `[disabled]="!activateAgent"` — disabled
             until the connect is complete. Playwright's click() correctly
             refuses a disabled button, but the except branch then JS-clicked
             it, and a JS click on a disabled button fires NOTHING. The
             fallback existed to defeat aria-disabled elsewhere; here it
             silently defeated a real guard.
          3. Nothing verified the outcome — "ACTIVATED" was logged
             unconditionally.

        Now: exact selector, refuse to fake a disabled click, and assert the
        post-state the product actually renders (custom-editor.component.html:
        `*ngIf="!activeAgent"` wraps the button, and a status pill with
        `.dotactive` + "Active" replaces it).
        """
        btn = self.page.locator("button.active_agent_Button").first
        try:
            btn.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            raise AssertionError(
                "No 'Activate Connect' button (button.active_agent_Button) is "
                f"visible. Current URL: {self.page.url}"
            ) from None

        # A disabled button means the product considers the connect incomplete.
        # That is a real finding — surface the product's own explanation rather
        # than forcing a click that cannot work.
        if btn.is_disabled():
            reason = ""
            tip = self.page.locator(".hoverTooltip").filter(visible=True).first
            if tip.count():
                reason = (tip.text_content() or "").strip()
            raise AssertionError(
                "'Activate Connect' is DISABLED — the connect is not in an "
                f"activatable state. Product says: {reason or '(no tooltip)'}. "
                "Check every required setup field is filled and the action's "
                "run test succeeded."
            )

        btn.click(timeout=10_000)

        # Verify. The button's container is *ngIf="!activeAgent", so on success
        # the button goes away and a status pill appears in its place.
        try:
            btn.wait_for(state="hidden", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            raise AssertionError(
                "Clicked 'Activate Connect' but the button is still present — "
                "activeAgent never flipped, so the connect did not activate."
            ) from None

        status = self.page.locator("#dropdownMenuButton .dotpill, .dotactive").first
        try:
            status.wait_for(state="visible", timeout=10_000)
            label = (
                self.page.locator("#dropdownMenuButton").first.text_content() or ""
            ).strip()
        except PlaywrightTimeoutError:
            label = ""

        if label and "active" not in label.lower():
            raise AssertionError(
                f"Connect status reads {label!r} after activation, expected "
                "'Active'."
            )
        logger.info("Connect ACTIVATED and verified (status: %s)", label or "Active")
