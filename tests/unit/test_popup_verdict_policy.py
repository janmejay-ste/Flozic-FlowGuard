"""
Unit tests for the pricing-popup verdict policy.

`ai_popup_validator._decide()` exists because v1 of the prompt asked the model
for an `is_valid` boolean. On the 2026-08-11 run all six pricing variants
produced byte-identical observations:

    expected=BLOCK observed=BLOCK current=True target=False

...and the model answered true for three and false for three. Three tests
failed as "product bugs" on a coin flip.

The determinism test below is the regression guard for that: the same
observations must always produce the same verdict. The rest pin the policy
itself so a change to which plan names a category must mention shows up as a
deliberate diff rather than a silent shift in what the suite considers a bug.
"""

from __future__ import annotations

import pytest

from utils.ai_popup_validator import (
    _REQUIRES_CURRENT_MENTION,
    _REQUIRES_TARGET_MENTION,
    _decide,
)


def obs(expected="BLOCK", observed="BLOCK", current=True, target=False) -> dict:
    return {
        "expected_category": expected,
        "observed_category": observed,
        "popup_mentions_current": current,
        "popup_mentions_target": target,
        "reasoning": "synthetic",
    }


# ── The regression that motivated this module ──────────────────────────


def test_block_without_target_mention_is_valid():
    """The exact evidence from all six 2026-08-11 pricing variants. A BLOCK
    says "you cannot leave your current plan" — naming the target adds
    nothing, and the product doesn't do it. v1 failed three of these."""
    is_valid, failures = _decide(obs())
    assert is_valid is True, failures
    assert failures == []


def test_identical_observations_always_produce_identical_verdicts():
    """The core guarantee. v1 could not make this claim."""
    payload = obs()
    verdicts = {_decide(dict(payload))[0] for _ in range(50)}
    assert verdicts == {True}, (
        "the verdict must be a pure function of the observations — if this "
        "fails, sampling has crept back into the decision path"
    )


def test_all_six_pricing_variants_agree():
    """Yearly and monthly differ only in the period, which the policy does
    not consider — so all six must land the same way."""
    results = [
        _decide(obs())[0]
        for _period in ("Yearly", "Monthly")
        for _plan in ("Standard", "Professional", "Business")
    ]
    assert results == [True] * 6


# ── Category mismatch ──────────────────────────────────────────────────


def test_category_mismatch_is_invalid():
    is_valid, failures = _decide(obs(expected="BLOCK", observed="CHECKOUT"))
    assert is_valid is False
    assert any("mismatch" in f for f in failures)
    assert any("BLOCK" in f and "CHECKOUT" in f for f in failures)


def test_matching_categories_are_compared_case_insensitively():
    assert _decide(obs(expected="block", observed="BLOCK"))[0] is True


def test_categories_are_stripped_before_comparison():
    assert _decide(obs(expected=" BLOCK ", observed="BLOCK"))[0] is True


@pytest.mark.parametrize("expected, observed", [
    ("", "BLOCK"),
    ("BLOCK", ""),
    ("", ""),
])
def test_missing_category_is_invalid_not_a_crash(expected, observed):
    """A model that omits a category is an unusable verdict, not a pass."""
    is_valid, failures = _decide(obs(expected=expected, observed=observed))
    assert is_valid is False
    assert any("could not determine" in f for f in failures)


def test_empty_payload_is_invalid():
    is_valid, failures = _decide({})
    assert is_valid is False
    assert failures


# ── Plan-name requirements ─────────────────────────────────────────────


def test_block_requires_current_plan_mention():
    """A "downgrade not allowed" that never says which plan you're on is a
    genuinely unhelpful message — this one SHOULD fail."""
    is_valid, failures = _decide(obs(observed="BLOCK", current=False))
    assert is_valid is False
    assert any("current plan" in f for f in failures)


@pytest.mark.parametrize("category", sorted(_REQUIRES_TARGET_MENTION))
def test_confirm_and_checkout_require_target_mention(category):
    """The user is moving ONTO the target plan, so it must be named."""
    is_valid, failures = _decide(
        obs(expected=category, observed=category, current=True, target=False)
    )
    assert is_valid is False
    assert any("target plan" in f for f in failures)


@pytest.mark.parametrize("category", sorted(_REQUIRES_TARGET_MENTION))
def test_confirm_and_checkout_pass_with_both_names(category):
    assert _decide(
        obs(expected=category, observed=category, current=True, target=True)
    )[0] is True


def test_checkout_does_not_require_current_mention():
    """CHECKOUT is forward-looking; naming the old plan isn't required."""
    assert _decide(
        obs(expected="CHECKOUT", observed="CHECKOUT", current=False, target=True)
    )[0] is True


def test_unknown_category_has_no_mention_requirements():
    """NO_POPUP / CONTACT_US carry no plan-name obligation, so a category
    match alone is enough."""
    assert _decide(
        obs(expected="CONTACT_US", observed="CONTACT_US",
            current=False, target=False)
    )[0] is True


def test_multiple_failures_are_all_reported():
    """A downstream reader should see every reason, not just the first."""
    is_valid, failures = _decide(
        obs(expected="CONFIRM_PAID", observed="CONFIRM_PAID",
            current=False, target=False)
    )
    assert is_valid is False
    assert len(failures) == 2


# ── Policy shape ───────────────────────────────────────────────────────


def test_block_is_not_in_the_target_mention_set():
    """The specific rule the 2026-08-11 failures came down to. If someone adds
    BLOCK here, those three tests start failing again on correct popups."""
    assert "BLOCK" not in _REQUIRES_TARGET_MENTION
    assert "BLOCK_PERIOD" not in _REQUIRES_TARGET_MENTION
    assert "BLOCK" in _REQUIRES_CURRENT_MENTION
