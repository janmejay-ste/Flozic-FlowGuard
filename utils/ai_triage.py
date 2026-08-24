"""
AI-based failure triage.

On test failure, we already capture:
  - screenshot.png   (visual state at failure)
  - dom.html         (DOM snapshot)
  - url.txt          (current URL)
  - the test exception + traceback

This module bundles those artifacts into a single vision call and asks the
model to classify the failure and propose a fix. Output is written to
the same failure folder as triage.json and surfaced by the dashboard.

Categories the model is asked to choose from:
  PRODUCT_BUG    — the application under test is broken
  LOCATOR_DRIFT  — the test's selector no longer matches the DOM
  FLAKE          — transient (network, timing) — would pass on rerun
  INFRA          — Playwright / browser / Python env issue
  TEST_BUG       — assertion or logic mistake in the test itself

Provider-agnostic — Claude or OpenAI, selected by env vars. See
utils/ai_provider.py for the selection rules. With no provider configured,
triage() returns SKIPPED and the test still fails loudly on its own terms.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

MODULE = "ai_triage"

# Limit how much DOM we send — cost scales with input tokens, and the top of
# the body is usually enough to spot a missing element. 15KB is about 4k
# tokens, which keeps a single triage call well under $0.05.
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
    """Deprecated: the provider abstraction encodes images per-provider now.
    Retained for any external caller."""
    try:
        if not p.exists():
            return None
        return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii")
    except Exception as e:
        logger.debug("Could not encode image %s: %s", p, e)
        return None


def triage(
    test_name: str,
    failure_folder: Path,
    exception_message: str = "",
    traceback_text: str = "",
    model: str | None = None,
) -> TriageResult:
    """
    Read the failure artifacts in `failure_folder` and ask the model to
    classify the root cause. Writes the result to `failure_folder/triage.json`
    and returns the TriageResult.
    """
    from utils import ai_prompts
    from utils.ai_parser import send_json
    from utils.ai_provider import PROVIDER_NAME

    def _persist(result: TriageResult) -> TriageResult:
        try:
            failure_folder.mkdir(parents=True, exist_ok=True)
            (failure_folder / "triage.json").write_text(
                result.to_json(), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("Could not persist triage.json: %s", e)
        return result

    if PROVIDER_NAME == "noop":
        return _persist(TriageResult(
            status="SKIPPED",
            diagnosis="No AI provider configured; failure triage skipped.",
        ))

    # Gather artifacts.
    screenshot_path = failure_folder / "screenshot.png"
    dom_path        = failure_folder / "dom.html"
    url_path        = failure_folder / "url.txt"
    url             = _read_text_safe(url_path).strip()
    dom_excerpt     = _read_text_safe(dom_path, max_bytes=DOM_EXCERPT_BYTES)

    prompt = ai_prompts.render(
        "failure_triage",
        test_name=test_name,
        url=url or "(unknown)",
        exception_message=exception_message or "(none)",
        traceback_text=traceback_text or "(none)",
        dom_excerpt=dom_excerpt or "(empty)",
    )

    parsed, resp = send_json(
        prompt,
        module=MODULE,
        model=model,
        image_paths=[screenshot_path] if screenshot_path.exists() else None,
    )

    if resp is not None and resp.error:
        return _persist(TriageResult(
            status="ERROR", error=resp.error,
            diagnosis=f"{resp.provider} request failed: {resp.error}",
        ))
    if not isinstance(parsed, dict):
        return _persist(TriageResult(
            status="ERROR", error="ResponseParseError",
            diagnosis="Could not parse model output as JSON.",
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
            "provider":   resp.provider,
            "model":      resp.model,
            # Which prompt produced this verdict — lets a dashboard shift be
            # traced to a prompt edit instead of blamed on the product.
            "prompt_version": ai_prompts.version_for("failure_triage"),
        },
        raw=parsed,
    )
    logger.info(
        "[AI-triage] %s | category=%s confidence=%s severity=%s | %s",
        test_name, result.category, result.confidence, result.severity,
        result.diagnosis,
    )
    return _persist(result)
