"""
GPT-generated executive summary for the dashboard.

At session teardown, the dashboard builder calls summarize() with the run's
stats + records. Returns a 2-3 sentence high-level summary suitable for
non-QA stakeholders (PM, eng leads). The summary is embedded at the top
of the dashboard.

Skip behaviour:
  - No OPENAI_API_KEY  -> returns a deterministic plain-language fallback.
  - OpenAI HTTP error  -> returns the same fallback (no test break).
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")
API_BASE = "https://api.openai.com/v1/chat/completions"
REQUEST_TIMEOUT_S = 30


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
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return _fallback_summary(stats, decision_status)

    try:
        import requests  # type: ignore
    except ImportError:
        return _fallback_summary(stats, decision_status)

    # Build a compact, GPT-friendly description of the run.
    failed = [r for r in records if getattr(r, "status", "") == "FAIL"]
    failed_summary = [
        {"name": r.method, "feature": r.feature, "duration_ms": r.duration_ms}
        for r in failed[:10]
    ]
    # We DELIBERATELY do not pass numeric counts/percentages in a form the
    # model is tempted to restate. Numbers are computed and rendered by the
    # dashboard separately; here GPT only narrates the situation and gives
    # one recommendation. (Past versions that passed pass_rate=94.3% got
    # hallucinated back as "94.5%".)
    qualitative = (
        "all tests passed"   if stats["failed"] == 0 else
        "mostly passed"      if stats["pass_rate"] >= 80 else
        "mixed results"      if stats["pass_rate"] >= 50 else
        "mostly failed"
    )
    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content":
                "You write 2-3 sentence executive summaries of QA test runs "
                "for non-QA stakeholders (engineering leads, PMs). Tone: "
                "calm, factual, action-oriented. No jargon. No emojis.\n\n"
                "IMPORTANT: do NOT restate numeric metrics (counts, "
                "percentages, durations). Those are rendered separately. "
                "Describe what happened qualitatively and end with one "
                "clear next-step recommendation."},
            {"role": "user", "content":
                f"Qualitative outcome: {qualitative}\n"
                f"Release decision: {decision_status}\n"
                f"Failed test names (no counts): "
                f"{json.dumps([f['name'] for f in failed_summary])}\n\n"
                "Write 2-3 sentences. End with one clear recommendation "
                "(e.g. 'Recommended for release', 'Investigate flozic gohighlevel "
                "routing before release', etc.)."},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    try:
        resp = requests.post(API_BASE, json=payload, headers=headers,
                             timeout=REQUEST_TIMEOUT_S)
        if resp.status_code != 200:
            logger.warning("[AI-exec] HTTP %d: %s", resp.status_code, resp.text[:200])
            return _fallback_summary(stats, decision_status)
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return text or _fallback_summary(stats, decision_status)
    except Exception as e:
        logger.warning("[AI-exec] request failed: %s — using fallback", e)
        return _fallback_summary(stats, decision_status)
