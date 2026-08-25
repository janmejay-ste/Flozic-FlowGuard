"""
Unit tests for utils/ai_cost_tracker.py.

Pins three contracts:

  1. `SCHEMA_VERSION` and the shape of `ai-cost.json`, same convention as the
     network schema — downstream consumers should get a version bump, not a
     silently changed format.
  2. Pricing arithmetic, including the Sonnet 5 introductory window. A run in
     September must not keep quoting the promo rate.
  3. Unknown models are reported as unpriced rather than counted as free —
     a $0 line that looks real is worse than an explicit gap.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from utils.ai_cost_tracker import (
    PRICING,
    SCHEMA_VERSION,
    CostTracker,
    price_call,
)


def test_schema_version_pinned():
    """Any change to the ai-cost.json format must be an explicit bump."""
    assert SCHEMA_VERSION == 1, (
        f"Schema version changed to {SCHEMA_VERSION}. If intentional, update "
        "the dashboard's cost panel and this test together."
    )


# ── Pricing ────────────────────────────────────────────────────────────


def test_price_call_uses_per_million_rates():
    """Opus 5 is $5/$25 per 1M tokens."""
    usd, known = price_call("claude-opus-5", 1_000_000, 1_000_000,
                            on=date(2026, 8, 7))
    assert known is True
    assert usd == pytest.approx(30.00)


def test_price_call_scales_below_a_million():
    usd, _ = price_call("claude-opus-5", 10_000, 2_000, on=date(2026, 8, 7))
    # 10k in @ $5/M = $0.05 ; 2k out @ $25/M = $0.05
    assert usd == pytest.approx(0.10)


def test_sonnet5_intro_pricing_applies_inside_the_window():
    """Through 2026-08-31 Sonnet 5 bills at the $2/$10 introductory rate."""
    usd, known = price_call("claude-sonnet-5", 1_000_000, 1_000_000,
                            on=date(2026, 8, 7))
    assert known is True
    assert usd == pytest.approx(12.00)


def test_sonnet5_reverts_to_list_price_after_the_window():
    """The day the promo ends, the list rate applies — the tracker must not
    keep under-reporting spend because the table was written in August."""
    usd, _ = price_call("claude-sonnet-5", 1_000_000, 1_000_000,
                        on=date(2026, 9, 1))
    assert usd == pytest.approx(18.00)


def test_intro_window_boundary_is_inclusive():
    on_last_day, _ = price_call("claude-sonnet-5", 1_000_000, 0,
                                on=date(2026, 8, 31))
    assert on_last_day == pytest.approx(2.00)


def test_dated_model_snapshot_prices_like_its_base():
    """`claude-haiku-4-5-20251001` must not fall through as unpriced."""
    dated, known_dated = price_call("claude-haiku-4-5-20251001", 1_000_000, 0)
    base, known_base = price_call("claude-haiku-4-5", 1_000_000, 0)
    assert known_dated and known_base
    assert dated == base


def test_unknown_model_is_flagged_not_silently_free():
    usd, known = price_call("some-future-model", 1_000_000, 1_000_000)
    assert usd == 0.0
    assert known is False


def test_every_pricing_entry_is_positive():
    for name, entry in PRICING.items():
        assert entry.input_per_mtok > 0, f"{name} has a non-positive input rate"
        assert entry.output_per_mtok > 0, f"{name} has a non-positive output rate"


# ── Accumulation ───────────────────────────────────────────────────────


def _tracker_with_calls(cap=None) -> CostTracker:
    """Two modules, three calls. ai_triage is deliberately the bigger
    spender in a single call so the by-module ordering is a real assertion
    rather than a restatement of insertion order."""
    t = CostTracker(cap_usd=cap)
    t.record(module="ai_validator", provider="claude", model="claude-opus-5",
             input_tokens=10_000, output_tokens=1_000, on=date(2026, 8, 7))
    t.record(module="ai_triage", provider="claude", model="claude-opus-5",
             input_tokens=40_000, output_tokens=4_000, on=date(2026, 8, 7))
    t.record(module="ai_validator", provider="claude", model="claude-opus-5",
             input_tokens=10_000, output_tokens=1_000, on=date(2026, 8, 7))
    return t


def test_totals_accumulate_across_calls():
    summary = _tracker_with_calls().summary()["session"]
    assert summary["calls"] == 3
    assert summary["input_tokens"] == 60_000
    assert summary["output_tokens"] == 6_000


def test_attribution_is_per_module_and_sorted_by_spend():
    by_module = _tracker_with_calls().summary()["by_module"]
    assert set(by_module) == {"ai_validator", "ai_triage"}
    assert by_module["ai_validator"]["calls"] == 2
    assert by_module["ai_triage"]["calls"] == 1
    # ai_triage spent more in one call than ai_validator did in two, so it
    # must lead — the panel is only useful if the biggest spender is first.
    assert list(by_module) == ["ai_triage", "ai_validator"]
    assert by_module["ai_triage"]["usd"] > by_module["ai_validator"]["usd"]


def test_module_totals_sum_to_the_session_total():
    summary = _tracker_with_calls().summary()
    assert sum(m["usd"] for m in summary["by_module"].values()) == \
        pytest.approx(summary["session"]["usd"])


def test_errors_are_counted_but_still_billed():
    """A failed call that burned input tokens still cost money."""
    t = CostTracker()
    t.record(module="ai_triage", provider="claude", model="claude-opus-5",
             input_tokens=5_000, output_tokens=0, error="HTTP 500",
             on=date(2026, 8, 7))
    session = t.summary()["session"]
    assert session["errors"] == 1
    assert session["usd"] > 0


def test_unpriced_models_are_surfaced_in_the_summary():
    t = CostTracker()
    t.record(module="ai_validator", provider="claude", model="mystery-model",
             input_tokens=1_000, output_tokens=1_000)
    assert t.summary()["unpriced_models"] == ["mystery-model"]


# ── Budget cap ─────────────────────────────────────────────────────────


def test_no_cap_means_never_over_budget():
    assert _tracker_with_calls(cap=None).over_budget() is False


def test_under_cap_is_not_over_budget():
    assert _tracker_with_calls(cap=100.00).over_budget() is False


def test_cap_trips_once_spend_reaches_it():
    t = _tracker_with_calls(cap=0.0001)   # any real spend exceeds this
    assert t.over_budget() is True
    assert t.summary()["cap_exceeded"] is True


def test_cap_state_is_reported_in_the_artifact():
    t = _tracker_with_calls(cap=5.00)
    summary = t.summary()
    assert summary["cap_usd"] == 5.00
    assert summary["cap_exceeded"] is False


# ── Export ─────────────────────────────────────────────────────────────


def test_export_writes_versioned_artifact(tmp_path: Path):
    path = _tracker_with_calls().export_to(tmp_path)
    assert path.name == "ai-cost.json"
    data = json.loads(path.read_text())
    assert data["schema_version"] == SCHEMA_VERSION
    assert "generated_at" in data


def test_export_contains_the_expected_top_level_keys(tmp_path: Path):
    data = json.loads(_tracker_with_calls().export_to(tmp_path).read_text())
    assert set(data) == {
        "schema_version", "generated_at", "cap_usd", "cap_exceeded",
        "session", "by_module", "unpriced_models", "calls",
    }


def test_export_creates_missing_directories(tmp_path: Path):
    nested = tmp_path / "reports" / "trend" / "run_summary"
    assert _tracker_with_calls().export_to(nested).exists()


def test_export_of_an_empty_session_is_still_valid(tmp_path: Path):
    """A run with AI disabled must produce a readable zero report, not a
    missing file — otherwise 'no spend' is indistinguishable from 'export
    broke'."""
    data = json.loads(CostTracker().export_to(tmp_path).read_text())
    assert data["session"]["calls"] == 0
    assert data["session"]["usd"] == 0.0
    assert data["by_module"] == {}


def test_reset_clears_state():
    t = _tracker_with_calls()
    t.reset()
    assert t.summary()["session"]["calls"] == 0
