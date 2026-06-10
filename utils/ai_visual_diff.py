"""
Visual regression with semantic diffing.

Workflow per test:
  1. Capture current canvas → reports/recordings/<test_id>/canvas.png
  2. Look for the matching baseline → baselines/<test_id>/canvas.png
     (NOTE: baselines/ lives at the REPO root, not under reports/, so it
      survives `reports/` cleanups and lives in git.)
  3. If baseline missing                       -> FIRST_RUN  (caller promotes later)
  4. If bytes identical                        -> IDENTICAL  (no GPT call needed)
  5. If bytes differ, send both to GPT for     -> COSMETIC | REGRESSION | UNKNOWN
     semantic classification.

States (per user spec):
  FIRST_RUN   - no baseline yet (first time we see this test)
  IDENTICAL   - byte-equal screenshots
  COSMETIC    - GPT thinks the change is color/spacing/font-rendering noise
  REGRESSION  - GPT thinks an element changed/moved/disappeared
  UNKNOWN     - GPT is uncertain (we asked for this explicitly to avoid
                forcing binary classification)
  SKIPPED     - no OPENAI_API_KEY (only IDENTICAL/FIRST_RUN can be reported)
  ERROR       - request or parse failure

Baselines are updated INTENTIONALLY by running:
    python scripts/promote_baseline.py <test_id>

Never auto-promote: that defeats the purpose of historical truth.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")
API_BASE = "https://api.openai.com/v1/chat/completions"
REQUEST_TIMEOUT_S = 60

# baselines/ is at the REPO ROOT so it lives in version control alongside
# the tests, not under reports/ (which is regenerated/cleaned). The user
# called this out explicitly: "reports are ephemeral, baselines are truth".
BASELINE_ROOT = Path(__file__).resolve().parents[1] / "baselines"


@dataclass
class VisualDiffResult:
    status: str                       # FIRST_RUN | IDENTICAL | COSMETIC | REGRESSION | UNKNOWN | SKIPPED | ERROR
    classification: str = ""          # same as status when GPT was consulted; empty otherwise
    confidence: float = 0.0           # 0.0 - 1.0
    reasoning: str = ""               # GPT's explanation, or system message
    baseline_path: str | None = None
    current_path: str | None = None
    bytes_equal: bool = False
    raw: dict = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def baseline_path_for(test_id: str) -> Path:
    """Return where the baseline canvas.png lives for a given test_id."""
    return BASELINE_ROOT / test_id / "canvas.png"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _encode_image(p: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def _gpt_classify(baseline: Path, current: Path, test_id: str) -> VisualDiffResult:
    """Send both images to GPT, ask for COSMETIC | REGRESSION | UNKNOWN."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return VisualDiffResult(
            status="SKIPPED",
            baseline_path=str(baseline), current_path=str(current),
            reasoning="OPENAI_API_KEY not set; cannot run semantic diff.",
        )
    try:
        import requests  # type: ignore
    except ImportError:
        return VisualDiffResult(
            status="ERROR", error="ModuleNotFoundError: requests",
            baseline_path=str(baseline), current_path=str(current),
        )

    prompt = (
        f"You are reviewing a visual regression for test '{test_id}'.\n\n"
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
        "Reply with a SINGLE JSON object, no other text, with keys:\n"
        "  classification (one of the four above),\n"
        "  confidence (float 0.0-1.0 — your certainty),\n"
        "  reasoning (one sentence)."
    )
    payload = {
        "model": DEFAULT_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _encode_image(baseline)}},
                {"type": "image_url", "image_url": {"url": _encode_image(current)}},
            ],
        }],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(API_BASE, json=payload, headers=headers,
                             timeout=REQUEST_TIMEOUT_S)
    except Exception as e:
        return VisualDiffResult(
            status="ERROR", error=str(e),
            baseline_path=str(baseline), current_path=str(current),
        )
    if resp.status_code != 200:
        return VisualDiffResult(
            status="ERROR",
            error=f"HTTP {resp.status_code}: {resp.text[:300]}",
            baseline_path=str(baseline), current_path=str(current),
        )
    try:
        parsed = json.loads(resp.json()["choices"][0]["message"]["content"])
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        return VisualDiffResult(
            status="ERROR", error=str(e),
            baseline_path=str(baseline), current_path=str(current),
        )

    cls = str(parsed.get("classification", "UNKNOWN")).upper()
    if cls not in {"IDENTICAL", "COSMETIC", "REGRESSION", "UNKNOWN"}:
        cls = "UNKNOWN"
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    return VisualDiffResult(
        status=cls,
        classification=cls,
        confidence=conf,
        reasoning=str(parsed.get("reasoning", "")),
        baseline_path=str(baseline),
        current_path=str(current),
        raw=parsed,
    )


def diff_against_baseline(
    test_id: str,
    current_png: Path,
    output_verdict_path: Path | None = None,
) -> VisualDiffResult:
    """
    Compare `current_png` against the baseline for `test_id`.

    - If no baseline exists, returns FIRST_RUN. Caller can promote later
      with scripts/promote_baseline.py.
    - If bytes identical, returns IDENTICAL with no GPT call (cheap).
    - Otherwise calls GPT for semantic classification.
    """
    if not current_png.exists():
        return VisualDiffResult(
            status="ERROR", error=f"Current screenshot missing: {current_png}",
            current_path=str(current_png),
        )

    baseline = baseline_path_for(test_id)
    if not baseline.exists():
        result = VisualDiffResult(
            status="FIRST_RUN",
            current_path=str(current_png),
            baseline_path=str(baseline),
            reasoning=(
                "No baseline found. After QA review, promote with:  "
                f"python scripts/promote_baseline.py {test_id}"
            ),
        )
    else:
        bytes_equal = _sha256(baseline) == _sha256(current_png)
        if bytes_equal:
            result = VisualDiffResult(
                status="IDENTICAL", classification="IDENTICAL",
                confidence=1.0, bytes_equal=True,
                baseline_path=str(baseline), current_path=str(current_png),
                reasoning="Byte-identical to baseline.",
            )
        else:
            result = _gpt_classify(baseline, current_png, test_id)
            result.bytes_equal = False

    if output_verdict_path is not None:
        try:
            output_verdict_path.parent.mkdir(parents=True, exist_ok=True)
            output_verdict_path.write_text(result.to_json(), encoding="utf-8")
        except Exception as e:
            logger.warning("Could not write visual diff verdict: %s", e)
    logger.info(
        "[VisualDiff] test=%s status=%s confidence=%.2f reasoning=%s",
        test_id, result.status, result.confidence, result.reasoning,
    )
    return result


def promote_baseline(test_id: str, current_png: Path) -> Path:
    """
    Copy a current canvas screenshot to become the new baseline.
    Called by scripts/promote_baseline.py — never auto-invoked.
    """
    if not current_png.exists():
        raise FileNotFoundError(current_png)
    dest = baseline_path_for(test_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current_png, dest)
    logger.info("[VisualDiff] baseline promoted: %s -> %s", current_png, dest)
    return dest
