"""
AI-generated executive summary for the dashboard.

At session teardown, the dashboard builder calls summarize() with the run's
stats + records. Returns a 2-3 sentence high-level summary suitable for
non-QA stakeholders (PM, eng leads). The summary is embedded at the top
of the dashboard.

Provider-agnostic — Claude or OpenAI, selected by env vars. See
utils/ai_provider.py for the selection rules.

Skip behaviour:
  - No provider configured -> deterministic plain-language fallback.
  - Provider error          -> the same fallback (no test break).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

MODULE = "ai_exec_summary"


def _fallback_summary(stats: dict, decision_status: str) -> str:
    """Deterministic plain-language summary used when GPT is unavailable."""
    p, f, t = stats["passed"], stats["failed"], stats["total"]
    rate    = stats["pass_rate"]
    if t == 0:
        return "No tests ran in this session."
    if f == 0:
        return (
            f"All {t} tests passed ({rate}%). Release decision: {decision_status}."
        )
    return (
        f"{p}/{t} tests passed ({rate}%). {f} failed. "
        f"Release decision: {decision_status}. "
        "See failing rows below for triage details."
    )


def summarize(stats: dict, records: list, decision_status: str = "READY") -> str:
    """
    Build a short executive summary. Always returns a non-empty string —
    falls back to a deterministic summary when GPT is unavailable.
    """
    from utils import ai_prompts
    from utils.ai_parser import send_text
    from utils.ai_provider import PROVIDER_NAME

    if PROVIDER_NAME == "noop":
        return _fallback_summary(stats, decision_status)

    failed = [r for r in records if getattr(r, "status", "") == "FAIL"]
    failed_names = [r.method for r in failed[:10]]

    # We DELIBERATELY do not pass numeric counts/percentages in a form the
    # model is tempted to restate. Numbers are computed and rendered by the
    # dashboard separately; here the model only narrates the situation and
    # gives one recommendation. (Past versions that passed pass_rate=94.3%
    # got it hallucinated back as "94.5%".)
    qualitative = (
        "all tests passed"   if stats["failed"] == 0 else
        "mostly passed"      if stats["pass_rate"] >= 80 else
        "mixed results"      if stats["pass_rate"] >= 50 else
        "mostly failed"
    )

    try:
        resp = send_text(
            ai_prompts.render(
                "exec_summary",
                qualitative=qualitative,
                decision_status=decision_status,
                failed_names=json.dumps(failed_names),
            ),
            module=MODULE,
            system_prompt=ai_prompts.system_for("exec_summary"),
            max_tokens=400,
        )
    except Exception as e:
        logger.warning("[AI-exec] request failed: %s — using fallback", e)
        return _fallback_summary(stats, decision_status)

    if resp.error:
        logger.warning("[AI-exec] %s error: %s — using fallback",
                       resp.provider, resp.error)
        return _fallback_summary(stats, decision_status)

    return (resp.text or "").strip() or _fallback_summary(stats, decision_status)
