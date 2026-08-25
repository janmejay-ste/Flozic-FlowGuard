"""
Dynamic field mapping in the connect editor's setup panel.

A component object for the mapping UI, kept out of connect_editor_page.py
(already ~750 lines) the same way copilot_panel and connect_canvas are.

THE DOM, AS CAPTURED FROM A LIVE RUN
------------------------------------
Verified against reports/failures/test_gmail_to_sheets_..._120443/dom.html on
2026-08-11, not inferred from the template. Every mappable row field sits in a
`menu-drop{k}` container that holds BOTH its trigger and its own picker:

    div#menu-drop3
      span#editOptions[column1]0     <- the "Add or Select" trigger
        <svg/> Add or Select         <- the "+" is an SVG; the TEXT is just
                                        "Add or Select" (matching "+ Add or
                                        Select" finds nothing)
      div#scrollheight3              <- this field's picker
        "Search or select a dynamic value from previous step(s):"
        input.formcontrol[placeholder="Search..."]
        a.choice                     <- level 1: which previous step
        (level 2 rows appear after a step is clicked)

LEVEL 1 IS LABELLED BY EVENT, NOT APP
-------------------------------------
The step rows read "1. New Email" — the step's ORDINAL plus its EVENT name.
The app name ("Gmail") does not appear, so matching a step by app silently
finds nothing. map_field() therefore takes a list of candidate labels and
tries each; the caller passes both the app and the event, since a spec is
written in terms of apps and the UI is written in terms of events.

Two things that burned earlier attempts:

  * The `scrollheight` index is NOT the column index. It starts at 3 here
    because menu-drop0..2 are Spreadsheet, Worksheet and Value Input Option.
    Never compute it — find the picker inside the field's own `menu-drop`
    ancestor instead. That survives the product adding or removing a
    preceding setup field.

  * Field labels are the SPREADSHEET'S COLUMN HEADERS, so they are data:
    "vexeluserpaymenttest18@yopmail.com", "From pricing yearly",
    "Professional", "IND". The stable key is `columnN` from the element id;
    the label is for humans and for spec matching.

WHY SAMPLE DATA MUST EXIST FIRST
--------------------------------
Level 2 lists the chosen step's *sample data*. custom-editor.component.ts
resolves references against `resolved.sample_data[0].data` entries shaped
{key, value, text|label}. No run test on the trigger => no sample data => the
level-2 list is empty. utils.connect_spec refuses a spec that maps without
trigger.run_test for exactly this reason.

WHY THE WAIT IS SO LONG
-----------------------
The row fields load only after Spreadsheet AND Worksheet are both set, and the
product's own copilot handler retries for 50 seconds before giving up. Our wait
exceeds that budget so we don't fail while the product is still legitimately
loading — that failure mode looks exactly like a product bug.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Sequence

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from utils.connect_spec import FIRST_AVAILABLE

logger = logging.getLogger(__name__)

# ── Selectors (verified against a captured live DOM, 2026-08-11) ────────

# One per mappable row field. id looks like `editOptions[column1]0`.
FIELD_TRIGGER = "[id^='editOptions[']"
# The enclosing container that also holds this field's picker.
FIELD_GROUP_XPATH = "xpath=ancestor::*[starts-with(@id,'menu-drop')][1]"
# The picker, found *within* the field's own group — never by index.
PICKER = "[id^='scrollheight']"
# Level 1: the previous steps you can pull data from.
STEP_CHOICE = "a.choice"
# Filter input inside an open picker.
PICKER_SEARCH = "input.formcontrol[placeholder='Search...']"
# Level 2: sample-data rows of the chosen step. Matched by text — the template
# builds these from sample data with no stable per-row class.
SAMPLE_FIELD_ROW = "a, li"
MAPPING_HEADING = "Search or select a dynamic value from previous step"

# Must exceed the product's own 50s dynamic-field retry budget.
DYNAMIC_FIELDS_TIMEOUT_MS = 75_000


class ConnectMappingPanel:
    def __init__(self, page: Page) -> None:
        self.page = page

    # ── Discovery ──────────────────────────────────────────────────────

    def _field_triggers(self) -> Locator:
        return self.page.locator(FIELD_TRIGGER).filter(visible=True)

    def mappable_fields(self) -> list[tuple[str, str]]:
        """`[(column_key, label)]` in DOM order.

        `column_key` ("column1") comes from the element id and is stable.
        `label` is the spreadsheet's column header and is data.
        """
        out: list[tuple[str, str]] = []
        triggers = self._field_triggers()
        for i in range(triggers.count()):
            el = triggers.nth(i)
            raw_id = el.get_attribute("id") or ""
            m = re.search(r"editOptions\[([^\]]+)\]", raw_id)
            key = m.group(1) if m else raw_id
            out.append((key, self._label_for(el) or key))
        return out

    def mappable_field_labels(self) -> list[str]:
        """Just the labels, for the __first__ resolution and for logging."""
        return [label for _key, label in self.mappable_fields()]

    def _label_for(self, trigger: Locator) -> str | None:
        """The `<label>` belonging to a field trigger.

        Walks up to the enclosing group and takes the LAST label found there —
        the panel renders `<label>Header (Optional)</label>` before the trigger
        with no attribute tying them together. "(Optional)" is stripped as UI
        decoration, not part of the column name.
        """
        try:
            raw = trigger.evaluate(
                """
                el => {
                    let n = el;
                    for (let hops = 0; n && hops < 8; hops++, n = n.parentElement) {
                        const labs = n.querySelectorAll('label');
                        if (labs.length) {
                            const t = (labs[labs.length - 1].textContent || '').trim();
                            if (t) return t;
                        }
                    }
                    return null;
                }
                """
            )
        except Exception:                      # pragma: no cover — DOM churn
            return None
        if not raw:
            return None
        return re.sub(r"\s*\(optional\)\s*$", "", raw, flags=re.I).strip()

    def wait_for_dynamic_fields(
        self,
        expected_min: int = 1,
        timeout_ms: int = DYNAMIC_FIELDS_TIMEOUT_MS,
    ) -> list[str]:
        """
        Wait until at least `expected_min` mappable fields exist.

        The Spreadsheet -> Worksheet -> row-fields gate. Returns the labels
        found, so a caller can log exactly what the product offered.
        """
        logger.info(
            "[mapping] Waiting up to %.0fs for >= %d row field(s) to load "
            "(the product's own budget for this is 50s)...",
            timeout_ms / 1000, expected_min,
        )
        end = time.monotonic() + timeout_ms / 1000.0
        fields: list[tuple[str, str]] = []
        while time.monotonic() < end:
            fields = self.mappable_fields()
            if len(fields) >= expected_min:
                logger.info(
                    "[mapping] %d row field(s) available: %s",
                    len(fields), [f"{k}={l!r}" for k, l in fields],
                )
                return [label for _k, label in fields]
            self.page.wait_for_timeout(1_000)

        raise PlaywrightTimeoutError(
            f"Only {len(fields)} mappable field(s) appeared within "
            f"{timeout_ms}ms (needed {expected_min}). Found: {fields}. "
            f"Looked for {FIELD_TRIGGER}. The row fields load only after BOTH "
            "Spreadsheet and Worksheet are set — confirm those took."
        )

    # ── Mapping ────────────────────────────────────────────────────────

    def map_field(
        self,
        target: str,
        source_step: str,
        source_field: str,
        timeout_ms: int = 20_000,
        source_step_candidates: Sequence[str] | None = None,
    ) -> str:
        """
        Map `source_step.source_field` into the row field `target`.

        `target` is matched against the column label, or may be FIRST_AVAILABLE
        to take the first field. Returns the label actually used.

        `source_step_candidates` are alternative labels for the same step,
        tried in order. Needed because level 1 shows "1. New Email" — the
        EVENT name — while a spec names the step by its APP ("Gmail"). The
        caller passes both.
        """
        candidates = list(source_step_candidates or [source_step])
        fields = self.mappable_fields()
        if not fields:
            raise AssertionError(
                "No mappable row fields present. For a Sheets action, call "
                "wait_for_dynamic_fields() after setting Spreadsheet and "
                "Worksheet."
            )

        if target == FIRST_AVAILABLE:
            index = 0
            key, label = fields[0]
            logger.info(
                "[mapping] target=__first__ -> %s (%r); all offered: %s",
                key, label, [l for _k, l in fields],
            )
        else:
            hits = [
                i for i, (k, l) in enumerate(fields)
                if target.lower() in l.lower() or target.lower() == k.lower()
            ]
            if not hits:
                raise AssertionError(
                    f"No row field matching {target!r}. Offered: "
                    f"{[(k, l) for k, l in fields]}"
                )
            index = hits[0]
            key, label = fields[index]

        trigger = self._field_triggers().nth(index)
        trigger.scroll_into_view_if_needed()
        trigger.click(timeout=timeout_ms)
        self.page.wait_for_timeout(800)

        # Where the picker lives moved in the 2026-08-20 editor redesign. It
        # used to render INSIDE the field's menu-drop group; it is now a
        # SIBLING rendered after the group closes, carrying an `open-down`
        # class while open (failure artifact 20260820_113906: scrollheight3
        # sits outside menu-drop, choices-container scrollheight ... open-down).
        # Try the old structural home first for back-compat, then the
        # page-wide OPEN picker — the open-down/:visible filter is what keeps
        # a page-wide query from grabbing a closed picker (the .first trap).
        picker = trigger.locator(FIELD_GROUP_XPATH).locator(PICKER).first
        try:
            picker.wait_for(state="visible", timeout=3_000)
        except PlaywrightTimeoutError:
            picker = self.page.locator(
                "[id^='scrollheight'].open-down"
            ).filter(visible=True).first
        try:
            picker.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            raise AssertionError(
                f"Clicking {key} ({label!r}) did not open a picker. Expected a "
                f"{PICKER} inside its menu-drop group."
            ) from None

        # Level 1 — choose the step to pull from. Try each candidate label:
        # the rows are titled "N. <Event>", so the app name won't match.
        step_row = None
        matched_on = None
        for cand in candidates:
            self._filter(picker, cand)
            row = picker.locator(STEP_CHOICE).filter(
                has_text=re.compile(re.escape(cand), re.I)
            ).first
            try:
                row.wait_for(state="visible", timeout=5_000)
                step_row, matched_on = row, cand
                break
            except PlaywrightTimeoutError:
                continue

        if step_row is None:
            offered = self._texts(picker.locator(STEP_CHOICE))
            raise AssertionError(
                f"No previous step matching any of {candidates} in the picker "
                f"for {label!r}. Steps offered: {offered}. Note these are "
                'labelled "N. <Event name>", not by app name.'
            )
        step_row.click()
        self.page.wait_for_timeout(800)
        logger.info("[mapping] %s: step matched on %r", key, matched_on)

        # Level 2 — choose the sample-data field within that step.
        field_row = picker.locator(SAMPLE_FIELD_ROW).filter(
            has_text=re.compile(re.escape(source_field), re.I)
        ).first
        try:
            field_row.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            offered = self._texts(picker.locator(SAMPLE_FIELD_ROW))
            raise AssertionError(
                f"Step {source_step!r} exposed no field matching "
                f"{source_field!r}. Offered: {offered}. An empty list means "
                "the trigger run test produced no sample data."
            ) from None
        field_row.click()
        self.page.wait_for_timeout(800)
        logger.info(
            "[mapping] %s (%r) <- %s.%s", key, label, source_step, source_field,
        )
        return label

    @staticmethod
    def _texts(loc: Locator, limit: int = 15) -> list[str]:
        """First few option texts — for actionable failure messages."""
        out = []
        for i in range(min(loc.count(), limit)):
            t = (loc.nth(i).text_content() or "").strip()
            if t:
                out.append(re.sub(r"\s+", " ", t)[:60])
        return out

    def _filter(self, picker: Locator, term: str) -> None:
        """Type into the picker's Search... box. Best-effort — the list is
        short enough to click directly when the box isn't rendered."""
        box = picker.locator(PICKER_SEARCH).first
        try:
            box.wait_for(state="visible", timeout=3_000)
            box.fill(term)
            self.page.wait_for_timeout(600)
        except PlaywrightTimeoutError:
            logger.debug("[mapping] No Search... box in this picker; not filtering.")

    # ── Verification ───────────────────────────────────────────────────

    def read_current_mapping(self) -> dict[str, str]:
        """
        Column label -> what that field currently shows.

        Written to `mapping.json`. This is what makes a mapping failure
        diagnosable; a screenshot of the panel rarely shows which token
        landed where.
        """
        out: dict[str, str] = {}
        triggers = self._field_triggers()
        for i in range(triggers.count()):
            el = triggers.nth(i)
            label = self._label_for(el) or (el.get_attribute("id") or f"field{i}")
            try:
                out[label] = re.sub(
                    r"\s+", " ", (el.text_content() or "")
                ).replace("Add or Select", "").strip()
            except Exception:                  # pragma: no cover
                out[label] = ""
        return out

    def assert_mapping_resolved(self, target: str) -> None:
        """
        Assert the mapping RESOLVED — not merely that something landed in the
        box.

        custom-editor.component.ts emits `Could not resolve "<nodeRef>"` for a
        reference it cannot bind. A field that reads back plausibly but shows
        that error is broken, and asserting on its text alone would call it a
        pass. That distinction is the point of this test.
        """
        error = self.page.get_by_text(re.compile(r"could not resolve", re.I)).first
        if error.count() and error.is_visible():
            raise AssertionError(
                f"Mapping for {target!r} did not resolve: "
                f"{(error.text_content() or '').strip()!r}. The referenced step "
                "has no matching sample-data key — check the trigger run test."
            )
