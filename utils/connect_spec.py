"""
Declarative specs for connect-creation tests.

Before this, each app pair needed a bespoke page-object method —
`fill_gmail_draft_setup()`, `fill_mindbody_sale_setup()`. Gmail → Sheets would
have been the third, and the fourth pair the fourth method. The shape of a
connect is data (trigger app, event, setup fields, action app, event, mapping),
so it belongs in a spec file that the test reads.

A spec is pure data and this module is pure logic — no Playwright, no network —
so the validation below is unit-testable without a browser.

Sentinels
---------
Some choices genuinely don't matter to the assertion (which Gmail account, which
worksheet). Rather than hardcoding a value that will rot, a field can ask for
`FIRST_AVAILABLE` and the page object picks the first option the product offers.
The chosen value is always logged and written into the run artifact, so a
failure stays reproducible — "any worksheet" must not mean "we don't know which
worksheet was used when this broke".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# Bump on an intentional format change — tests/unit/test_connect_spec.py pins
# this, same contract as the network and cost schemas.
SCHEMA_VERSION = 1

# Ask the page object to take whatever the product lists first.
FIRST_AVAILABLE = "__first__"

SPEC_DIR = Path(__file__).resolve().parents[1] / "tests" / "connect_specs"


class SpecError(ValueError):
    """Raised when a spec is structurally invalid.

    Deliberately loud: a typo'd field name in a spec would otherwise surface
    as a mid-test Playwright timeout, which reads like a product bug.
    """


@dataclass
class SetupField:
    """One setup input on a trigger or action step."""
    field: str
    value: str
    # True when this dropdown gates the ones after it (Spreadsheet → Worksheet
    # → the row fields). The page object must let it settle before continuing;
    # not doing so is the single biggest flake source in this flow.
    dependent: bool = False

    @property
    def wants_first_available(self) -> bool:
        return self.value == FIRST_AVAILABLE


@dataclass
class FieldMapping:
    """One dynamic mapping: a target field on the action ← a source field on a
    previous step.

    `source_step` names the step to pull from as the product's own dropdown
    labels it ("Gmail"); `source_field` is the sample-data field within it
    ("Subject"). The editor's picker is two-level, so both are needed.
    """
    target: str
    source_step: str
    source_field: str


@dataclass
class StepSpec:
    app: str
    event: str
    account: str = FIRST_AVAILABLE
    setup: list[SetupField] = field(default_factory=list)
    mapping: list[FieldMapping] = field(default_factory=list)
    # Trigger steps need a run-test so the editor has sample data to resolve
    # mappings against — see the module docstring of pages/connect_mapping_panel.
    run_test: bool = False


@dataclass
class ConnectSpec:
    name: str
    trigger: StepSpec
    action: StepSpec
    activate: bool = True
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Parsing ────────────────────────────────────────────────────────────


def _parse_setup(raw: Any, where: str) -> list[SetupField]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecError(f"{where}.setup must be a list, got {type(raw).__name__}")
    out: list[SetupField] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SpecError(f"{where}.setup[{i}] must be an object")
        missing = {"field", "value"} - set(entry)
        if missing:
            raise SpecError(
                f"{where}.setup[{i}] missing {sorted(missing)}"
            )
        out.append(SetupField(
            field=str(entry["field"]),
            value=str(entry["value"]),
            dependent=bool(entry.get("dependent", False)),
        ))
    return out


def _parse_mapping(raw: Any, where: str) -> list[FieldMapping]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecError(f"{where}.mapping must be a list, got {type(raw).__name__}")
    out: list[FieldMapping] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SpecError(f"{where}.mapping[{i}] must be an object")
        missing = {"target", "source_step", "source_field"} - set(entry)
        if missing:
            raise SpecError(
                f"{where}.mapping[{i}] missing {sorted(missing)}. The editor's "
                "picker is two-level, so a mapping needs both the step to pull "
                "from and the field within it."
            )
        out.append(FieldMapping(
            target=str(entry["target"]),
            source_step=str(entry["source_step"]),
            source_field=str(entry["source_field"]),
        ))
    return out


def _parse_step(raw: Any, where: str) -> StepSpec:
    if not isinstance(raw, dict):
        raise SpecError(f"{where} must be an object")
    for key in ("app", "event"):
        if not raw.get(key):
            raise SpecError(f"{where}.{key} is required and must be non-empty")
    return StepSpec(
        app=str(raw["app"]),
        event=str(raw["event"]),
        account=str(raw.get("account", FIRST_AVAILABLE)),
        setup=_parse_setup(raw.get("setup"), where),
        mapping=_parse_mapping(raw.get("mapping"), where),
        run_test=bool(raw.get("run_test", False)),
    )


def parse(raw: dict[str, Any]) -> ConnectSpec:
    """Validate and parse a spec dict. Raises SpecError on any problem."""
    if not isinstance(raw, dict):
        raise SpecError(f"spec must be an object, got {type(raw).__name__}")

    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise SpecError(
            f"spec schema_version is {version!r}, expected {SCHEMA_VERSION}. "
            "Bumping the format requires updating utils/connect_spec.py and "
            "tests/unit/test_connect_spec.py together."
        )
    if not raw.get("name"):
        raise SpecError("spec.name is required and must be non-empty")
    for key in ("trigger", "action"):
        if key not in raw:
            raise SpecError(f"spec.{key} is required")

    spec = ConnectSpec(
        name=str(raw["name"]),
        trigger=_parse_step(raw["trigger"], "trigger"),
        action=_parse_step(raw["action"], "action"),
        activate=bool(raw.get("activate", True)),
    )

    # A mapping can only resolve against sample data, which only exists after a
    # run-test on the source step. Catch that here rather than as an unresolved
    # mapping 3 minutes into a browser run.
    if spec.action.mapping and not spec.trigger.run_test:
        raise SpecError(
            "action.mapping is set but trigger.run_test is false. The editor "
            "resolves mapping tokens against the trigger's sample data, which "
            "only exists after a run test — mappings would fail to resolve."
        )

    # Mappings must reference a step that exists in this spec.
    known_steps = {spec.trigger.app.lower(), spec.action.app.lower()}
    for m in spec.action.mapping:
        if m.source_step.lower() not in known_steps:
            raise SpecError(
                f"action.mapping target {m.target!r} pulls from step "
                f"{m.source_step!r}, which is not a step in this spec "
                f"({sorted(known_steps)})."
            )

    return spec


def load(name: str) -> ConnectSpec:
    """Load a spec by filename stem from tests/connect_specs/."""
    path = SPEC_DIR / f"{name}.json"
    if not path.exists():
        available = sorted(p.stem for p in SPEC_DIR.glob("*.json"))
        raise SpecError(f"No spec named {name!r}. Available: {available}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SpecError(f"{path} is not valid JSON: {e}") from None
    return parse(raw)
