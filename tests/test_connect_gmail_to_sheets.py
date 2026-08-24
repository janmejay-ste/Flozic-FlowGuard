"""
Connect creation with dynamic field mapping: Gmail -> Google Sheets.

    Gmail "New Email"  ->  Google Sheets "Create Spreadsheet Row"

Driven by tests/connect_specs/gmail_to_sheets.json rather than hardcoded here,
so the next app pair is a spec file instead of another bespoke test.

WHAT THIS TEST ASSERTS
----------------------
  1. the connect can be built end to end through the editor UI
  2. the Sheets row fields actually LOAD after the dependent dropdowns settle
  3. each mapping RESOLVES — not merely that text landed in a box
  4. the connect activates

Point 3 is the one that matters. A mapping panel that looks filled in but whose
references don't bind produces a connect that runs and writes nothing. Asserting
on the field's text alone would call that a pass, so
ConnectMappingPanel.assert_mapping_resolved() checks for the editor's
"Could not resolve" state instead.

ORDERING IS LOAD-BEARING
------------------------
Two constraints, both read out of the product source rather than guessed:

  * The trigger run test must happen BEFORE mapping. The editor resolves
    mapping references against the trigger's sample data
    (custom-editor.component.ts, resolved.sample_data[0].data) and that only
    exists once a run test has fetched it. utils.connect_spec refuses a spec
    that maps without it.

  * Spreadsheet and Worksheet must settle BEFORE the row fields are touched.
    Those fields (data1, data2, ...) load asynchronously afterwards, and the
    product's own copilot handler retries for 50 seconds before giving up. Our
    wait is 75s so we don't fail while the product is still legitimately
    loading — that failure mode looks exactly like a product bug.

STATUS
------
The mapping selectors in pages/connect_mapping_panel.py are read from the
product template but NOT yet verified against the live DOM. Expect the first
live run to need a selector correction pass; they are gathered in one block in
that module for exactly that reason.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from playwright.sync_api import Page

from pages.auth_helper import perform_login
from pages.connect_editor_page import ConnectEditorPage
from pages.connect_mapping_panel import ConnectMappingPanel
from pages.dashboard_page import DashboardPage
from pages.wait_utils import complete_user_guide, dismiss_overlays
from utils.connect_spec import load
from utils.test_category import test_category

logger = logging.getLogger(__name__)

SPEC_NAME = "gmail_to_sheets"
ARTIFACT_DIR = Path("reports/recordings") / "test_connect_gmail_to_sheets"


def _step_labels(spec, source_step: str) -> list[str]:
    """Candidate labels for a step in the mapping picker.

    A spec names a step by its APP ("Gmail"); the picker labels rows by their
    ORDINAL AND EVENT ("1. New Email"). Return both so the page object can try
    each — matching on the app name alone finds nothing.
    """
    labels = [source_step]
    for step in (spec.trigger, spec.action):
        if step.app.lower() == source_step.lower() and step.event:
            labels.append(step.event)
    return labels


def _targeted(setup) -> dict[str, str]:
    """Spec setup fields -> the {field: search term} shape handle_setup_step
    wants. FIRST_AVAILABLE entries are omitted so the page object falls back to
    picking the first option."""
    return {f.field: f.value for f in setup if not f.wants_first_available}


@test_category(
    type="FULL",
    requires_login=True,
    feature="Connect Creation",
)
class TestConnectGmailToSheets:
    def test_gmail_to_sheets_with_dynamic_mapping(self, page: Page) -> None:
        spec = load(SPEC_NAME)
        logger.info("=== Connect: %s ===", spec.name)

        dashboard = DashboardPage(page)
        editor = ConnectEditorPage(page)
        mapping = ConnectMappingPanel(page)

        # ── Step 1: login + dashboard ──────────────────────────────────
        perform_login(page)
        dismiss_overlays(page)
        complete_user_guide(page)
        dismiss_overlays(page)
        assert dashboard.is_dashboard_loaded(), (
            f"Dashboard did not load. URL: {page.url}"
        )
        logger.info("Step 1 DONE: dashboard loaded.")

        # ── Step 2: open a fresh editor ────────────────────────────────
        if not editor.is_editor_visible(timeout_ms=3_000):
            dashboard.click_create_connect()
            page.wait_for_load_state("domcontentloaded")
            dismiss_overlays(page)
            complete_user_guide(page)
        assert editor.is_editor_visible(timeout_ms=30_000), (
            f"Editor did not open. URL: {page.url}"
        )
        logger.info("Step 2 DONE: editor open.")

        # ── Step 3: trigger — Gmail / New Email ────────────────────────
        # advance_to_setup_step() rather than a fixed number of click_continue()
        # calls: Gmail goes event -> account -> setup, other apps skip the
        # account panel. Counting them broke this test on the first live run.
        editor.select_trigger_app(spec.trigger.app)
        editor.select_trigger_event(spec.trigger.event)
        editor.advance_to_setup_step()
        editor.handle_setup_step_if_present(targeted=_targeted(spec.trigger.setup))
        logger.info(
            "Step 3 DONE: trigger %s / %s configured.",
            spec.trigger.app, spec.trigger.event,
        )

        # ── Step 4: run test on the trigger ────────────────────────────
        # MUST precede mapping — this is what produces the sample data that
        # mapping references resolve against.
        assert spec.trigger.run_test, (
            "spec.trigger.run_test must be true for a mapped connect; "
            "utils.connect_spec should have rejected this spec"
        )
        editor.click_continue_run_test()
        logger.info("Step 4 DONE: trigger run test — sample data available.")

        # ── Step 5: action — Google Sheets / Create Spreadsheet Row ────
        editor.click_add_new_step()
        editor.click_add_app()
        editor.select_action_app(spec.action.app)
        editor.select_action_event(spec.action.event)
        editor.advance_to_setup_step()   # event -> account -> setup
        logger.info(
            "Step 5 DONE: action %s / %s selected.",
            spec.action.app, spec.action.event,
        )

        # ── Step 6: dependent dropdowns — Spreadsheet, THEN Worksheet ──
        # One field at a time, in spec order, each verified before moving on.
        #
        # handle_setup_step() cannot do this: it walks every dropdown in one
        # pass with a 3s wait for options, so on 2026-08-11 it picked an
        # arbitrary spreadsheet (search='') and then found Worksheet empty.
        # Worksheet is an API round-trip that only starts once a valid
        # Spreadsheet is chosen, so the order and the waiting are the whole job.
        chosen: dict[str, str] = {}
        for f in spec.action.setup:
            term = "" if f.wants_first_available else f.value
            chosen[f.field] = editor.set_dropdown_by_field(f.field, term)
        logger.info("Step 6 DONE: dependent dropdowns set — %s", chosen)

        # Anything non-dependent left on the panel (optional extras) can go
        # through the bulk handler.
        if not spec.action.setup:
            editor.handle_setup_step_if_present()

        # ── Step 7: wait for the row fields to load ────────────────────
        available = mapping.wait_for_dynamic_fields(
            expected_min=len(spec.action.mapping) or 1,
        )
        logger.info("Step 7 DONE: %d mappable field(s): %s", len(available), available)

        # ── Step 8: apply and verify each mapping ──────────────────────
        applied: dict[str, str] = {}
        for m in spec.action.mapping:
            resolved_target = mapping.map_field(
                target=m.target,
                source_step=m.source_step,
                source_field=m.source_field,
                source_step_candidates=_step_labels(spec, m.source_step),
            )
            mapping.assert_mapping_resolved(resolved_target)
            applied[resolved_target] = f"{m.source_step}.{m.source_field}"
        logger.info("Step 8 DONE: %d mapping(s) applied and resolved.", len(applied))

        # Artifact first, so a later failure still leaves the mapping evidence.
        self._write_artifact(
            spec, available, applied, mapping.read_current_mapping(),
            dropdowns=chosen,
        )

        # ── Step 9: run test on the action, then activate ──────────────
        editor.click_continue_run_test()
        if spec.activate:
            editor.click_activate_connect()
            logger.info("Step 9 DONE: connect activated.")

        logger.info("=== SUCCESS: %s built, mapped and activated ===", spec.name)

    # ── Artifact ───────────────────────────────────────────────────────

    def _write_artifact(self, spec, available, applied, read_back,
                        dropdowns=None) -> None:
        """Record what was actually chosen.

        The spec uses __first__ in places where the choice doesn't matter to
        the assertion — but when this test fails six weeks from now, "any
        worksheet" is useless. This records the concrete values so the run is
        reproducible.
        """
        payload = {
            "schema_version": 1,
            "spec": spec.to_dict(),
            # What __first__ actually resolved to — the whole point of
            # recording this. "any worksheet" is useless when debugging later.
            "dropdowns_chosen": dropdowns or {},
            "mappable_fields_offered": available,
            "mappings_applied": applied,
            "mapping_read_back": read_back,
        }
        try:
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            path = ARTIFACT_DIR / "mapping.json"
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            logger.info("Mapping artifact: %s", path)
        except Exception as e:                 # never fail the test on artifacts
            logger.warning("Could not write mapping artifact: %s", e)
