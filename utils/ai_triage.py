"""
GPT-based failure triage.

On test failure, we already capture:
  - screenshot.png   (visual state at failure)
  - dom.html         (DOM snapshot)
  - url.txt          (current URL)
  - the test exception + traceback

This module bundles those artifacts into a single GPT call and asks the
model to classify the failure and propose a fix. Output is written to
the same failure folder as triage.json and surfaced by the dashboard.

Categories the model is asked to choose from:
  PRODUCT_BUG    — the application under test is broken
  LOCATOR_DRIFT  — the test's selector no longer matches the DOM
  FLAKE          — transient (network, timing) — would pass on rerun
  INFRA          — Playwright / browser / Python env issue
  TEST_BUG       — assertion or logic mistake in the test itself

Environment:
  OPENAI_API_KEY  — required. Without it, triage() returns SKIPPED.
  OPENAI_MODEL    — optional. Default 'gpt-5.4' (per CLAUDE.md), overridable.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")
API_BASE = "https://api.openai.com/v1/chat/completions"
REQUEST_TIMEOUT_S = 60

# Limit how much DOM we send — GPT cost scales with input tokens, and the
# top of the body is usually enough to spot a missing element. 15KB is
# about 4k tokens, which keeps a single triage call well under $0.05.
DOM_EXCERPT_BYTES = 15_000


@dataclass
class TriageResult:
    status: str                   # "TRIAGED" | "SKIPPED" | "ERROR"
    category: str = "UNKNOWN"     # PRODUCT_BUG | LOCATOR_DRIFT | FLAKE | INFRA | TEST_BUG | UNKNOWN
    confidence: float = 0.0       # 0.0 - 1.0
    diagnosis: str = ""           # 1-2 sentence summary
    suggested_fix: str = ""       # one-liner fix idea
    severity: str = "minor"       # minor | major | blocker
    # Evidence the classification was based on. Kept verbatim so old triages
    # can be re-classified by a future model without re-running the test.
    evidence: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _read_text_safe(p: Path, max_bytes: int | None = None) -> str:
    try:
        if not p.exists():
            return ""
        data = p.read_bytes()
        if max_bytes is not None and len(data) > max_bytes:
            data = data[:max_bytes] + b"\n<<truncated>>"
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug("Could not read %s: %s", p, e)
        return ""


def _encode_image(p: Path) -> str | None:
    try:
        if not p.exists():
            return None
        return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii")
    except Exception as e:
        logger.debug("Could not encode image %s: %s", p, e)
        return None


def _build_prompt(
    test_name: str, exception_message: str, traceback_text: str,
    url: str, dom_excerpt: str,
) -> str:
    return (
        "You are a senior QA engineer triaging an automated UI test failure.\n\n"
        f"Test:       {test_name}\n"
        f"URL:        {url or '(unknown)'}\n"
        f"Exception:  {exception_message or '(none)'}\n\n"
        "Traceback (last lines):\n"
        f"{traceback_text or '(none)'}\n\n"
        "DOM excerpt at failure (truncated):\n"
        "----BEGIN DOM----\n"
        f"{dom_excerpt or '(empty)'}\n"
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
        "TypeError on None, KeyError, or OpenAI HTTP error, the failure is "
        "in the TEST FRAMEWORK's call to the OpenAI API — NOT the product. "
        "Classify as INFRA with severity=minor. The application under test "
        "is not at fault; an OpenAI response was empty/malformed.\n"
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
        "Reply with a SINGLE JSON object, no other text, with keys:\n"
        "  category (one of the five above),\n"
        "  confidence (a float between 0.0 and 1.0 — your certainty),\n"
        "  diagnosis (1-2 sentences describing what went wrong),\n"
        "  suggested_fix (one-line action item),\n"
        "  severity (minor | major | blocker)."
    )


def triage(
    test_name: str,
    failure_folder: Path,
    exception_message: str = "",
    traceback_text: str = "",
    model: str | None = None,
) -> TriageResult:
    """
    Read the failure artifacts in `failure_folder` and ask GPT to classify
    the root cause. Writes the result to `failure_folder/triage.json` and
    returns the TriageResult.
    """
    def _persist(result: TriageResult) -> TriageResult:
        try:
            failure_folder.mkdir(parents=True, exist_ok=True)
            (failure_folder / "triage.json").write_text(
                result.to_json(), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("Could not persist triage.json: %s", e)
        return result

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return _persist(TriageResult(
            status="SKIPPED",
            diagnosis="OPENAI_API_KEY not set; failure triage skipped.",
        ))

    try:
        import requests  # type: ignore
    except ImportError:
        return _persist(TriageResult(
            status="ERROR",
            error="ModuleNotFoundError: requests",
            diagnosis="'requests' package not installed.",
        ))

    # Gather artifacts.
    screenshot_path = failure_folder / "screenshot.png"
    dom_path        = failure_folder / "dom.html"
    url_path        = failure_folder / "url.txt"
    url             = _read_text_safe(url_path).strip()
    dom_excerpt     = _read_text_safe(dom_path, max_bytes=DOM_EXCERPT_BYTES)
    image_data_url  = _encode_image(screenshot_path)

    content: list = [
        {"type": "text",
         "text": _build_prompt(test_name, exception_message, traceback_text,
                               url, dom_excerpt)}
    ]
    if image_data_url:
        content.append({"type": "image_url", "image_url": {"url": image_data_url}})

    payload = {
        "model": model or DEFAULT_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": content}],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }

    try:
        resp = requests.post(API_BASE, json=payload, headers=headers,
                             timeout=REQUEST_TIMEOUT_S)
    except Exception as e:
        return _persist(TriageResult(
            status="ERROR", error=str(e),
            diagnosis=f"OpenAI request failed: {e}",
        ))

    if resp.status_code != 200:
        return _persist(TriageResult(
            status="ERROR",
            error=f"HTTP {resp.status_code}: {resp.text[:300]}",
            diagnosis=f"OpenAI HTTP {resp.status_code}.",
        ))

    try:
        parsed = json.loads(resp.json()["choices"][0]["message"]["content"])
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        return _persist(TriageResult(
            status="ERROR", error=str(e),
            diagnosis=f"Could not parse model output: {e}",
        ))

    # Coerce confidence to float in [0,1]. Tolerate the model returning a
    # string ("0.87"), a stale enum ("medium"), or out-of-range numbers.
    raw_conf = parsed.get("confidence", 0.0)
    try:
        conf = float(raw_conf)
    except (TypeError, ValueError):
        conf = {"low": 0.3, "medium": 0.6, "high": 0.9}.get(
            str(raw_conf).lower(), 0.0
        )
    conf = max(0.0, min(1.0, conf))

    result = TriageResult(
        status="TRIAGED",
        category=str(parsed.get("category", "UNKNOWN")),
        confidence=conf,
        diagnosis=str(parsed.get("diagnosis", "")),
        suggested_fix=str(parsed.get("suggested_fix", "")),
        severity=str(parsed.get("severity", "minor")),
        # Keep verbatim copies of inputs that drove the verdict, so this
        # triage can be re-classified later by a different model/prompt
        # without re-running the test.
        evidence={
            "traceback":  traceback_text,
            "exception":  exception_message,
            "url":        url,
            "screenshot": str(screenshot_path) if screenshot_path.exists() else None,
            # DOM is large — link, don't embed.
            "dom_path":   str(dom_path) if dom_path.exists() else None,
            "model":      model or DEFAULT_MODEL,
        },
        raw=parsed,
    )
    logger.info(
        "[AI-triage] %s | category=%s confidence=%s severity=%s | %s",
        test_name, result.category, result.confidence, result.severity,
        result.diagnosis,
    )
    return _persist(result)
