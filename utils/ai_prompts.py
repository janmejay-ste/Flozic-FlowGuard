"""
Central registry for the prompt templates the `ai_*.py` modules send.

Before this, every prompt lived inline in the module that used it, which
made three ordinary things hard: seeing what we actually ask the model
across the framework, changing a shared convention (the "reply with a single
JSON object" clause appeared in six near-identical wordings), and knowing
whether a verdict regression came from a code change or a prompt edit.

Templates use `string.Template` (`$name`) rather than `str.format`, because
almost every prompt here embeds a literal JSON example and `{` / `}` would
need doubling throughout. The tradeoff: a literal dollar sign in a *template*
must be written `$$`. Substituted *values* are never rescanned, so a captured
popup containing "$49/month" is safe.

Each template carries a `version`. Bump it when you change wording in a way
that could move verdicts — it is written into the artifacts alongside the
result, so a shift in the dashboard can be traced to the prompt that caused
it rather than blamed on the product.

Usage:

    from utils import ai_prompts
    prompt = ai_prompts.render("canvas_validation",
                               user_prompt=..., expected_trigger=...)

Modules with prompts too specialized to share may register their own at
import time via `register()`; the point is that they all end up visible in
one place at runtime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from string import Template
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptTemplate:
    """One named, versioned prompt."""
    name:     str
    version:  int
    template: str
    system:   str | None = None
    notes:    str = ""

    def render(self, **kwargs: Any) -> str:
        """Substitute `$placeholders`. Raises KeyError on a missing one —
        a silently-empty placeholder produces a subtly wrong prompt, which
        is far harder to notice than a crash at render time."""
        return Template(self.template).substitute(**kwargs).strip()


_REGISTRY: dict[str, PromptTemplate] = {}


def register(tpl: PromptTemplate, *, replace: bool = False) -> PromptTemplate:
    if tpl.name in _REGISTRY and not replace:
        raise ValueError(
            f"Prompt {tpl.name!r} is already registered. Pass replace=True "
            "if you intend to override it."
        )
    _REGISTRY[tpl.name] = tpl
    return tpl


def get(name: str) -> PromptTemplate:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"No prompt registered under {name!r}. Known: "
            f"{', '.join(sorted(_REGISTRY))}"
        ) from None


def render(name: str, **kwargs: Any) -> str:
    return get(name).render(**kwargs)


def system_for(name: str) -> str | None:
    return get(name).system


def version_for(name: str) -> int:
    return get(name).version


def all_prompts() -> dict[str, PromptTemplate]:
    """Snapshot of the registry — used by the dashboard's provenance panel
    and by tests that assert versions were bumped deliberately."""
    return dict(_REGISTRY)


# ── Shared clauses ─────────────────────────────────────────────────────
#
# The JSON contract wording was duplicated (and had drifted) across modules.
# Compose it in rather than restating it.

JSON_ONLY = (
    "Reply with a SINGLE valid JSON object and nothing else — no markdown "
    "fences, no prose before or after."
)

LENIENT_EVENT_MATCHING = (
    "IMPORTANT — judge LENIENTLY on event-name details:\n"
    "  - Each integration only exposes a fixed list of trigger/action "
    "events; the user's prompt may reference an event that doesn't exist on "
    "that integration (e.g. 'New Lead' in HubSpot when only 'New Deal' is "
    "available; 'summarize' for ChatGPT when only 'Create image' / 'Create "
    "completion' are available).\n"
    "  - If the trigger APP and action APPS match the user's intent, treat "
    "the workflow as VALID even if the specific event picked is the closest "
    "available rather than the exact one the user named. Note the "
    "substitution in 'reasoning' but set is_valid=true.\n"
    "  - Only set is_valid=false if a wrong APP is on the canvas, a "
    "placeholder is still visible, or the substituted event is clearly wrong "
    "(e.g. 'Delete Contact' picked when the prompt said 'Create Contact')."
)


# ── Canvas validation (utils/ai_validator.py) ──────────────────────────

register(PromptTemplate(
    name="canvas_validation",
    version=1,
    notes="Vision call. Screenshot of the connect-creation canvas.",
    template=(
        "You are validating a no-code workflow editor screenshot.\n\n"
        "The user asked the AI builder to create this workflow:\n"
        '    "$user_prompt"\n\n'
        "Expected trigger app: $expected_trigger\n\n"
        "Look at the canvas in the screenshot and tell me:\n"
        "  1. Is a Trigger card visible with a real app name (not the "
        "placeholder text 'Select Trigger App')?\n"
        "  2. Are the Action card(s) visible with real app names (not "
        "'Select Action App')?\n"
        "  3. Does the trigger app shown match the expected one above?\n\n"
        + LENIENT_EVENT_MATCHING + "\n\n"
        + JSON_ONLY + " Keys:\n"
        "  is_valid (bool), trigger_app (string), action_apps (list of "
        "strings), placeholders_visible (bool), reasoning (string, one "
        "sentence)."
    ),
))


# ── Pricing popup validation (utils/ai_popup_validator.py) ─────────────

# The full PlanChangeService business-logic matrix, given to the model as
# context. The matrix IS the contract — if the model needs to know *why*
# BLOCK is correct for a Standard click from an Enterprise account, this
# paragraph is what tells it. Kept verbatim from the original prompt.
PLAN_CHANGE_MATRIX = """\
Flozic plan tier order (lower -> higher):
    Standard (1) < Professional (2) < Business (3) < Enterprise (4)

When a user clicks TRY NOW on the marketing pricing page, they're routed to
/portal-payment-handler/<planId>/<cpId>/<period> which invokes PlanChangeService.
The service decides which popup to show based on the user's current plan vs
the target plan + period. The full decision matrix:

  CURRENT = Free (new user):
    target = Standard / Professional / Business -> NO POPUP (proceeds to checkout)
    target = Enterprise -> CONTACT_US (opens Calendly)

  CURRENT = Trial of any tier:
    target = SAME tier -> SAME ("already your current plan")
    target = HIGHER tier -> CONFIRM_TRIAL
        ("You are currently on the X trial plan. Upgrading to / Purchasing the
          selected plan will immediately end your trial and activate the new
          plan. Do you want to continue?")
    target = LOWER tier (yearly section) -> BLOCK
        ("Downgrading to a lower plan is not allowed while your X trial is
          active. Please continue using your current plan or contact Support
          for assistance.")
    target = LOWER tier (monthly section) -> CONFIRM_PAID (special case - allowed)
    target = Enterprise -> CONTACT_US

  CURRENT = Paid (any tier, yearly or monthly):
    target = SAME tier + SAME period -> SAME
    target = SAME tier, monthly->yearly -> CONFIRM_SWITCH_YEARLY
    target = SAME tier, yearly->monthly -> BLOCK_PERIOD
        ("Switching from a yearly to a monthly plan is not allowed...")
    target = HIGHER tier -> CONFIRM_PAID
        ("You are currently subscribed to the X period plan. Purchasing the
          selected plan will cancel your current plan and activate the new
          plan immediately...")
    target = LOWER tier -> BLOCK
    target = Enterprise -> CONTACT_US
"""

register(PromptTemplate(
    name="pricing_popup_validation",
    version=2,
    notes=(
        "Matrix-aware. The current plan is injected from FLOZIC_CURRENT_PLAN "
        "rather than assumed, so the same prompt stays correct if the test "
        "account's tier changes.\n\n"
        "v2 (2026-08-11): removed the `is_valid` key. v1 asked the model for "
        "a boolean defined as a conjunction of four fields it already "
        "reported, and it returned opposite answers for byte-identical "
        "evidence across the six pricing variants at temperature=0. The "
        "conjunction now lives in ai_popup_validator._decide(). The model "
        "reports observations only."
    ),
    template=(
        "You are validating a PlanChangeService popup from the flozic.ai "
        "pricing flow.\n\n"
        + PLAN_CHANGE_MATRIX +
        "\nThe account under test is currently on the $current_plan plan. "
        "Judge every click relative to that tier — if the target is a lower "
        "tier, it is a DOWNGRADE and should produce BLOCK with a message "
        "naming the active plan.\n"
        "\nObservation from this test run:\n"
        "  Current plan:           $current_plan\n"
        "  Target plan (clicked):  $target_plan\n"
        "  Target period:          $target_period\n"
        "  Page-object classifier guess:  $page_classifier\n"
        "\nPopup as captured from the DOM:\n"
        "  .pcc-title:           $popup_title\n"
        "  .pcc-message:         $popup_message\n"
        "  primary action button:    $popup_primary\n"
        "  secondary action button:  $popup_secondary\n"
        "\nDecide:\n"
        "  1. Given the matrix and the current account state, what category "
        "SHOULD the popup be? (BLOCK / CONFIRM_TRIAL / CONFIRM_PAID / "
        "CONFIRM_SWITCH_YEARLY / BLOCK_PERIOD / SAME / NO_POPUP / CONTACT_US)\n"
        "  2. Given the captured text, what category did the product ACTUALLY "
        "show?\n"
        "  3. Do they match?\n"
        "  4. Does the message name the user's CURRENT plan?\n"
        "  5. Does the message name the TARGET plan?\n\n"
        "Report only what you observe. Do NOT judge whether the popup is "
        "correct overall and do NOT return an is_valid field — the framework "
        "applies the pass/fail rule to your observations in code, so that the "
        "same popup always produces the same verdict.\n\n"
        + JSON_ONLY + " Keys:\n"
        "  expected_category (string — what the matrix says it SHOULD be),\n"
        "  observed_category (string — what the captured text actually IS),\n"
        "  popup_mentions_current (bool),\n"
        "  popup_mentions_target (bool),\n"
        "  reasoning (string, one sentence describing what you saw)."
    ),
))


# ── Failure triage (utils/ai_triage.py) ────────────────────────────────

register(PromptTemplate(
    name="failure_triage",
    version=1,
    notes=(
        "Vision call (failure screenshot attached). The category set is "
        "closed on purpose — layered_health_scores buckets on it, so a "
        "free-text category silently breaks the score. The classification "
        "guidance below is hard-won: each clause exists because a real run "
        "was misclassified without it. Bump the version before editing it."
    ),
    template=(
        "You are a senior QA engineer triaging an automated UI test failure.\n\n"
        "Test:       $test_name\n"
        "URL:        $url\n"
        "Exception:  $exception_message\n\n"
        "Traceback (last lines):\n"
        "$traceback_text\n\n"
        "DOM excerpt at failure (truncated):\n"
        "----BEGIN DOM----\n"
        "$dom_excerpt\n"
        "----END DOM----\n\n"
        "A screenshot of the page at failure is attached.\n\n"
        "Classify the root cause. Choose category from:\n"
        "  PRODUCT_BUG    - the application is broken (real bug to file)\n"
        "  LOCATOR_DRIFT  - the test selector no longer matches the DOM\n"
        "  FLAKE          - transient (network/timing); would likely pass on rerun\n"
        "  INFRA          - Playwright/browser/Python env issue\n"
        "  TEST_BUG       - assertion or logic mistake in the test code itself\n\n"
        "IMPORTANT classification guidance:\n"
        "  - If the traceback references `ai_validator.py`, `ai_triage.py`, "
        "or `ai_visual_diff.py` AND the exception is a JSON decode error, "
        "TypeError on None, KeyError, or an LLM provider HTTP error, the "
        "failure is in the TEST FRAMEWORK's call to the AI provider — NOT "
        "the product. Classify as INFRA with severity=minor. The application "
        "under test is not at fault; a provider response was empty or "
        "malformed.\n"
        "  - If the exception message says 'AI rejected the canvas' or 'AI "
        "rejected canvas', the canvas-validation step already determined "
        "the application built the WRONG workflow. That is PRODUCT_BUG, "
        "not TEST_BUG. Pick TEST_BUG only when the failure is in test "
        "code itself (a typo'd selector, a wrong assertion, etc.).\n"
        "  - If the exception is a wait-for-locator timeout AND the locator "
        "targets a copilot/canvas element that should appear after backend "
        "AI processing, prefer PRODUCT_BUG (backend stalled/produced wrong "
        "output) over LOCATOR_DRIFT — unless the DOM clearly shows the "
        "page advanced past where the locator was looking.\n"
        "  - Reserve LOCATOR_DRIFT for cases where the DOM is fully loaded "
        "and contains a similar-but-different element the test should have "
        "matched instead.\n\n"
        + JSON_ONLY + " Keys:\n"
        "  category (one of the five above),\n"
        "  confidence (a float between 0.0 and 1.0 — your certainty),\n"
        "  diagnosis (1-2 sentences describing what went wrong),\n"
        "  suggested_fix (one-line action item),\n"
        "  severity (minor | major | blocker)."
    ),
))


# ── Visual regression (utils/ai_visual_diff.py) ────────────────────────

register(PromptTemplate(
    name="visual_diff",
    version=1,
    notes=(
        "Vision call with two images, baseline first then current — image "
        "order is load-bearing. UNKNOWN is offered deliberately so the model "
        "isn't forced into a binary guess; a false REGRESSION costs a human "
        "triage cycle."
    ),
    template=(
        "You are reviewing a visual regression for test '$test_id'.\n\n"
        "I'll show you TWO screenshots of the same UI:\n"
        "  Image 1 = BASELINE (the known-good reference)\n"
        "  Image 2 = CURRENT  (today's run)\n\n"
        "Classify the difference. Pick exactly one of:\n"
        "  IDENTICAL   - no meaningful difference\n"
        "  COSMETIC    - color, spacing, font-rendering, anti-aliasing — no\n"
        "                element changed identity, position, or content\n"
        "  REGRESSION  - an element changed identity / moved significantly /\n"
        "                disappeared / appeared / content changed\n"
        "  UNKNOWN     - you genuinely cannot tell (PREFER THIS over guessing)\n\n"
        + JSON_ONLY + " Keys:\n"
        "  classification (one of the four above),\n"
        "  confidence (float 0.0-1.0 — your certainty),\n"
        "  reasoning (one sentence)."
    ),
))


# NOTE: the "exec_summary" AI prompt was removed. The executive summary is now
# composed deterministically in utils/ai_exec_summary.py — a release verdict is
# Python's decision, not a model's, and a composed summary cannot hallucinate
# metrics or overclaim "recommended for release" off the pytest pass count.


# ── Prompt generation (utils/ai_prompt_generator.py) ───────────────────

register(PromptTemplate(
    name="workflow_prompt_generation",
    version=1,
    notes="Generates natural-language builder prompts for new app coverage.",
    template=(
        "Generate $count distinct natural-language prompts a user might type "
        "into a no-code workflow builder to create an automation.\n\n"
        "Trigger app: $trigger_app\n"
        "Action app(s): $action_apps\n\n"
        "Each prompt should read like something a real user would type — "
        "one sentence, concrete, no placeholder names like 'App A'. Vary the "
        "phrasing across the set.\n\n"
        + JSON_ONLY + " Keys:\n"
        '  prompts (list of objects, each with "prompt" (string), '
        '"expected_trigger" (string), "expected_actions" (list of strings)).'
    ),
))


# ── PR review (utils/ai_pr_review.py) ──────────────────────────────────

REVIEW_RULES = (
    "1. Hardcoded waits: flag any Thread.sleep, page.wait_for_timeout(N>500),\n"
    "   time.sleep — suggest event-based waits (wait_for_selector, expect()).\n"
    "2. Magic numbers: flag literal ints/floats (timeouts, retry counts,\n"
    "   array indices) that aren't named constants or commented.\n"
    "3. Brittle selectors: flag CSS classes that look hashed/generated\n"
    "   (e.g. 'awsui_*', 'css-7fhd23', '.MuiBox-root-42'), deep XPath\n"
    "   chains, and :nth-child / :nth-of-type. Prefer role/text/attribute.\n"
    "4. Duplicate locators: same selector string used in 2+ places in the\n"
    "   diff — should be a named constant or method.\n"
    "5. Missing waits: locator.click() or .fill() without a preceding\n"
    "   wait_for(state='visible') or expect().to_be_visible().\n"
    "6. Bare assertions: assert <expr> with no message — fails are hard to\n"
    "   triage. Require assert <expr>, '<context>'.\n"
    "7. New test class with no @test_category decorator.\n"
    "8. Duplicate helpers: two functions in the diff doing essentially the\n"
    "   same thing under different names.\n"
    "9. Missing retry / error handling around known-flaky calls\n"
    "   (network, navigation, focus changes).\n"
    "10. Missing screenshot/artifact capture in obvious failure paths\n"
    "    (e.g. try/except that swallows an exception without logging).\n"
    "11. Assertions inside a page object — they belong in the test.\n"
    "12. A direct HTTP call to an LLM API instead of utils/ai_provider.send().\n"
    "13. A new JSON artifact written without a schema_version field.\n"
    "14. A credential, token, or API key present in the added lines.\n"
)

register(PromptTemplate(
    name="pr_review",
    version=1,
    notes=(
        "One call per changed file. Rules 11-14 encode the repo conventions "
        "in PROJECT_STRUCTURE.md; rules 1-10 are the original checklist."
    ),
    system=(
        "You are a senior Python/Playwright test engineer doing a focused "
        "code review. Findings only. No general praise."
    ),
    template=(
        "Review the following git diff for file `$file_path`.\n\n"
        "Rules to check:\n"
        "$review_rules\n"
        "Diff (unified format):\n"
        "----BEGIN DIFF----\n"
        "$diff_excerpt\n"
        "----END DIFF----\n\n"
        "Return a SINGLE JSON object, no other text, with key 'findings' "
        "whose value is an array. Each item has:\n"
        "  file (string),\n"
        "  line (integer — line number IN THE NEW FILE if you can localize "
        "it, else 0),\n"
        "  severity (minor | major | blocker),\n"
        "  rule (short snake_case id like 'hardcoded_wait', 'brittle_selector'),\n"
        "  message (one sentence describing the problem),\n"
        "  suggested_fix (one-line action item).\n\n"
        "Only flag problems that are clearly present in the ADDED (+) lines "
        "of the diff. Ignore context lines. Be specific — vague nits get "
        "ignored by reviewers. Return an empty array if you find nothing."
    ),
))


# ── Page-object scaffolding (utils/ai_page_object.py) ──────────────────

register(PromptTemplate(
    name="page_object_scaffold",
    version=1,
    notes="Codegen. Output is written to a file for a human to review.",
    system=(
        "You generate Playwright Page Object classes for a Python test "
        "framework. Page objects interact with the browser and return data; "
        "they never contain assertions."
    ),
    template=(
        "Generate a Playwright Page Object class named $class_name for this "
        "page.\n\n"
        "URL: $url\n\n"
        "Relevant DOM (truncated):\n"
        "-----\n$dom\n-----\n\n"
        "Rules:\n"
        "  - Prefer role- and text-based locators over CSS/XPath.\n"
        "  - One method per user-meaningful action; return data, not "
        "assertions.\n"
        "  - Type-annotate every method.\n\n"
        + JSON_ONLY + " Keys:\n"
        '  class_name (string), code (string, the full Python source), '
        'locators (list of objects with "name" and "selector").'
    ),
))


# ── Form data synthesis (utils/ai_form_data.py) ────────────────────────

register(PromptTemplate(
    name="form_data",
    version=1,
    notes=(
        "Generates values for app-connection forms. Must never produce a "
        "real credential — see the explicit clause below."
    ),
    template=(
        "Generate plausible test values for the fields of a form that "
        "connects the app '$app_name' to a workflow automation product.\n\n"
        "Fields:\n$fields\n\n"
        "Rules:\n"
        "  - Values must be obviously synthetic test data.\n"
        "  - NEVER produce anything resembling a real credential, API key, "
        "OAuth token, or live account identifier. Use clearly fake values "
        "(e.g. 'test-api-key-0000').\n"
        "  - Respect each field's stated type and any format hint.\n\n"
        + JSON_ONLY + " Keys:\n"
        '  values (list of objects, each with "field" (string) and '
        '"value" (string)).'
    ),
))


# ── Mobile finding triage (utils/ai_mobile_triage.py) ──────────────────

register(PromptTemplate(
    name="mobile_finding_triage",
    version=1,
    notes=(
        "Classifies GROUPS of mobile-auditor findings, not individual ones — "
        "grouping happens deterministically in Python first. The enum is "
        "closed and Python re-validates every proposal (anything else becomes "
        "NEEDS_HUMAN), so this prompt can never invent a category. The "
        "auditor-fallibility clause is load-bearing: this repo shipped six "
        "auditor measurement bugs in one day (scroll ceiling, element "
        "identity x3, smooth-scroll reads, absolute thresholds), and a triage "
        "that assumes the instrument is right will blame the product for "
        "them. Bump the version before editing."
    ),
    template=(
        "You are a senior mobile QA engineer reviewing automated "
        "mobile-compatibility findings for a marketing website.\n\n"
        "Engine under test: $engine\n"
        "Finding groups ($group_count) — each is one distinct issue with its "
        "device/page spread and up to two verbatim sample messages:\n"
        "$groups_json\n\n"
        "For EACH group, classify it as exactly one of: $classifications\n\n"
        "Guidance — every clause here exists because a real run was "
        "misclassified without it:\n"
        "  - The measuring harness itself may be wrong or may have skipped a "
        "step. Wide-element reports for content inside a horizontal scroll "
        "container, drift measured during animation, and thresholds that a "
        "short page cannot physically meet have ALL been auditor artifacts "
        "in this suite. If the sample message describes a measurement that "
        "could plausibly mis-read the layout, prefer AUDITOR_ARTIFACT and "
        "say what to verify.\n"
        "  - Content collapsing, hiding, or moving into menus at narrow "
        "widths is usually RESPONSIVE_BY_DESIGN, not a defect — unless the "
        "affordance becomes unreachable at that width entirely.\n"
        "  - Inputs with font-size under 16px genuinely trigger iOS "
        "focus-zoom; tap targets under 44px are genuine ergonomics issues. "
        "Those are PRODUCT_DEFECT even when minor.\n"
        "  - When the evidence in the samples is insufficient to decide, "
        "answer NEEDS_HUMAN. A wrong confident label costs more than an "
        "honest shrug.\n\n"
        + JSON_ONLY + " Shape:\n"
        "{\n"
        '  "groups": [ { "id": "G1", "classification": "...",\n'
        '                "confidence": 0.0-1.0,\n'
        '                "rationale": "one sentence",\n'
        '                "suggested_fix": "one sentence",\n'
        '                "suggested_owner": "frontend|design|qa-framework" } ],\n'
        '  "summary": "at most two sentences over the whole set"\n'
        "}"
    ),
))
