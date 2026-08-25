"""
Unit tests for utils/connect_spec.py.

These matter more than they look. A connect-creation test is a 2-4 minute
browser run, so a typo in a spec that only surfaces as a mid-run Playwright
timeout costs minutes AND reads like a product bug on the dashboard. Every
check below moves a class of spec mistake from "mystery timeout at minute 3"
to "SpecError before the browser launches".

The two ordering rules encoded here are not style preferences — they come from
the product source:
  * mapping requires trigger.run_test, because the editor resolves mapping
    references against the trigger's sample data and that only exists after a
    run test.
  * dependent dropdowns gate the fields after them (Spreadsheet -> Worksheet
    -> row fields).
"""

from __future__ import annotations

import json

import pytest

from utils.connect_spec import (
    FIRST_AVAILABLE,
    SCHEMA_VERSION,
    SPEC_DIR,
    SpecError,
    load,
    parse,
)


def minimal(**over) -> dict:
    """A valid spec with no mapping — the simplest thing that parses."""
    spec = {
        "schema_version": SCHEMA_VERSION,
        "name": "t",
        "trigger": {"app": "Gmail", "event": "New Email"},
        "action": {"app": "Google Sheets", "event": "Create Spreadsheet Row"},
    }
    spec.update(over)
    return spec


def mapped(**over) -> dict:
    """A valid spec that maps, so it must also run a trigger test."""
    spec = minimal()
    spec["trigger"]["run_test"] = True
    spec["action"]["mapping"] = [
        {"target": "Column A", "source_step": "Gmail", "source_field": "Subject"}
    ]
    spec.update(over)
    return spec


# ── Schema version ─────────────────────────────────────────────────────


def test_schema_version_pinned():
    assert SCHEMA_VERSION == 1, (
        f"Version changed to {SCHEMA_VERSION}. Update every spec in "
        "tests/connect_specs/ and this test together."
    )


@pytest.mark.parametrize("version", [None, 0, 2, "1"])
def test_wrong_schema_version_is_rejected(version):
    """An old spec silently parsed under a new format is worse than a crash."""
    with pytest.raises(SpecError, match="schema_version"):
        parse(minimal(schema_version=version))


# ── Required fields ────────────────────────────────────────────────────


@pytest.mark.parametrize("missing", ["name", "trigger", "action"])
def test_missing_top_level_key_is_rejected(missing):
    spec = minimal()
    del spec[missing]
    with pytest.raises(SpecError, match=missing):
        parse(spec)


@pytest.mark.parametrize("blank", ["", None])
def test_blank_name_is_rejected(blank):
    with pytest.raises(SpecError, match="name"):
        parse(minimal(name=blank))


@pytest.mark.parametrize("step", ["trigger", "action"])
@pytest.mark.parametrize("key", ["app", "event"])
def test_step_requires_app_and_event(step, key):
    spec = minimal()
    spec[step][key] = ""
    with pytest.raises(SpecError, match=f"{step}.{key}"):
        parse(spec)


def test_non_dict_spec_is_rejected():
    with pytest.raises(SpecError, match="must be an object"):
        parse(["not", "a", "spec"])          # type: ignore[arg-type]


# ── Defaults ───────────────────────────────────────────────────────────


def test_account_defaults_to_first_available():
    """Most flows don't care which connected account is used."""
    assert parse(minimal()).trigger.account == FIRST_AVAILABLE


def test_run_test_defaults_false_and_activate_defaults_true():
    spec = parse(minimal())
    assert spec.trigger.run_test is False
    assert spec.activate is True


def test_setup_and_mapping_default_to_empty():
    spec = parse(minimal())
    assert spec.trigger.setup == []
    assert spec.action.mapping == []


# ── Setup fields ───────────────────────────────────────────────────────


def test_dependent_flag_is_parsed():
    spec = parse(minimal(action={
        "app": "Google Sheets", "event": "Create Spreadsheet Row",
        "setup": [
            {"field": "Spreadsheet", "value": "Test Sheet", "dependent": True},
            {"field": "Worksheet", "value": FIRST_AVAILABLE},
        ],
    }))
    assert [f.dependent for f in spec.action.setup] == [True, False]


def test_wants_first_available_helper():
    spec = parse(minimal(action={
        "app": "Google Sheets", "event": "Create Spreadsheet Row",
        "setup": [
            {"field": "Worksheet", "value": FIRST_AVAILABLE},
            {"field": "Spreadsheet", "value": "Test Sheet"},
        ],
    }))
    assert [f.wants_first_available for f in spec.action.setup] == [True, False]


@pytest.mark.parametrize("bad", [{"field": "x"}, {"value": "y"}, {}])
def test_setup_entry_missing_keys_is_rejected(bad):
    with pytest.raises(SpecError, match="setup"):
        parse(minimal(action={
            "app": "A", "event": "E", "setup": [bad],
        }))


def test_setup_must_be_a_list():
    with pytest.raises(SpecError, match="must be a list"):
        parse(minimal(action={"app": "A", "event": "E", "setup": {"field": "x"}}))


# ── Mapping ────────────────────────────────────────────────────────────


def test_valid_mapping_parses():
    m = parse(mapped()).action.mapping[0]
    assert (m.target, m.source_step, m.source_field) == \
        ("Column A", "Gmail", "Subject")


@pytest.mark.parametrize("missing", ["target", "source_step", "source_field"])
def test_mapping_entry_missing_keys_is_rejected(missing):
    """The editor's picker is two-level, so a mapping needs both halves."""
    entry = {"target": "A", "source_step": "Gmail", "source_field": "Subject"}
    del entry[missing]
    with pytest.raises(SpecError, match=missing):
        parse(mapped(action={
            "app": "Google Sheets", "event": "E", "mapping": [entry],
        }))


def test_mapping_without_trigger_run_test_is_rejected():
    """The rule that prevents a 3-minute run ending in unresolved mappings."""
    spec = mapped()
    spec["trigger"]["run_test"] = False
    with pytest.raises(SpecError, match="run_test"):
        parse(spec)


def test_mapping_from_unknown_step_is_rejected():
    """Catches a typo'd step name before the browser opens."""
    spec = mapped()
    spec["action"]["mapping"][0]["source_step"] = "Slack"
    with pytest.raises(SpecError, match="Slack"):
        parse(spec)


def test_mapping_source_step_match_is_case_insensitive():
    spec = mapped()
    spec["action"]["mapping"][0]["source_step"] = "gmail"
    assert parse(spec).action.mapping[0].source_step == "gmail"


def test_first_available_is_allowed_as_a_mapping_target():
    """'map any field' — the target column genuinely doesn't matter yet."""
    spec = mapped()
    spec["action"]["mapping"][0]["target"] = FIRST_AVAILABLE
    assert parse(spec).action.mapping[0].target == FIRST_AVAILABLE


# ── Loading from disk ──────────────────────────────────────────────────


def test_unknown_spec_name_lists_what_is_available():
    with pytest.raises(SpecError, match="Available"):
        load("no_such_spec")


def test_the_shipped_gmail_to_sheets_spec_is_valid():
    """The spec we actually run must stay parseable — this is the guard that
    a hand-edit to the JSON can't silently break the test."""
    spec = load("gmail_to_sheets")
    assert spec.trigger.app == "Gmail"
    assert spec.trigger.event == "New Email"
    assert spec.action.app == "Google Sheets"
    assert spec.action.event == "Create Spreadsheet Row"
    assert spec.trigger.run_test is True, "mapping requires a trigger run test"
    assert spec.activate is True


def test_shipped_spec_marks_the_dependent_dropdowns():
    """Spreadsheet and Worksheet gate the row fields. If someone drops these
    flags, the mapping step races the product's async field load."""
    setup = {f.field: f for f in load("gmail_to_sheets").action.setup}
    assert setup["Spreadsheet"].dependent is True
    assert setup["Worksheet"].dependent is True


def test_every_shipped_spec_parses():
    """Any JSON added to tests/connect_specs/ must be valid."""
    files = sorted(SPEC_DIR.glob("*.json"))
    assert files, "no connect specs found"
    for path in files:
        try:
            parse(json.loads(path.read_text(encoding="utf-8")))
        except SpecError as e:
            pytest.fail(f"{path.name} is invalid: {e}")


def test_to_dict_roundtrips_for_artifacts():
    """The spec is written into the run artifact; it must serialise."""
    payload = parse(mapped()).to_dict()
    assert json.loads(json.dumps(payload))["schema_version"] == SCHEMA_VERSION
