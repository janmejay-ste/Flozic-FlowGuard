"""
AI triage for MOBILE AUDITOR FINDINGS — classification, not judgement.

WHAT THIS IS FOR
----------------
A full mobile run emits findings at volume (the 2026-08-20 run: 26 major,
247 minor). The expensive part is not detecting them — it is classifying
them: product defect vs auditor artifact vs normal responsive behaviour.
This session did that by hand for days, and every misclassification cost a
debugging detour. This module asks the AI to do that classification pass,
under the same constitution as every other AI use in this repo:

  THE AI OBSERVES, PYTHON DECIDES.

The model proposes a classification per finding GROUP from a closed enum.
Python validates every proposal; anything outside the enum becomes
NEEDS_HUMAN. The AI never touches severity, the Mobile score, or pass/fail —
scoring stays deterministic (scoring v3). A wrong AI classification can waste
a reader's attention; it can never move a number.

WHY GROUPS, NOT RAW FINDINGS
----------------------------
247 minors are ~10 actual issues repeated across 6 device profiles and 2-3
pages. Grouping is done deterministically in Python BEFORE any tokens are
spent: same category + normalized message pattern => one group carrying its
device/page spread. This keeps the payload small (hard cap below), makes the
output stable run-to-run, and means the AI is asked ~10 questions instead
of 273.

INERT BY DEFAULT
----------------
No provider configured => `triage_mobile_findings` returns None and logs
exactly why, and the dashboard block says "AI analysis unavailable". The
same no-silent-default rule as the email trigger: analysis appearing or not
appearing must always be explainable from the log.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Closed classification set. layered scoring does NOT consume these — they
# are reader-facing labels only — but the set is still closed so a dashboard
# filter or a future gate can rely on it.
CLASSIFICATIONS = (
    "PRODUCT_DEFECT",        # a real defect a user would hit on a phone
    "AUDITOR_ARTIFACT",      # the check itself mis-measured (this repo has
                             #   shipped six of these; assume it can happen)
    "RESPONSIVE_BY_DESIGN",  # intended responsive behaviour, not a bug
    "NEEDS_HUMAN",           # genuinely ambiguous — say so, don't guess
)

# Payload budget. ~4 chars/token; groups beyond the cap are dropped LOUDLY
# (a "not analysed" marker), never silently.
MAX_PAYLOAD_CHARS = 16_000
MAX_GROUPS = 24
MAX_SAMPLES_PER_GROUP = 2

# Module-level result store for the session, mirroring how mobile findings
# themselves flow to the dashboard: process-local so a stale file can never
# be rendered as this run's analysis.
_SESSION_TRIAGE: dict[str, Any] | None = None


def session_triage() -> dict[str, Any] | None:
    """This session's triage result, or None if it never ran/failed."""
    return _SESSION_TRIAGE


def reset_session_triage() -> None:
    """Test-only hook."""
    global _SESSION_TRIAGE
    _SESSION_TRIAGE = None


# ── Deterministic grouping ─────────────────────────────────────────────


def _pattern(message: str) -> str:
    """Collapse the volatile parts of a finding message so identical issues
    on different devices/sizes fold into one group."""
    m = message or ""
    # Device names FIRST: numeric collapse would otherwise turn "Pixel 5"
    # into "Pixel _" before the device pattern can match it, splitting one
    # issue into per-device groups — the exact thing grouping exists to avoid.
    m = re.sub(
        r"iPhone(?: \d+)?(?: Pro(?: Max)?| SE)?|iPad Mini|Pixel \d+|"
        r"Galaxy S\d+\+?", "_device_", m)
    m = re.sub(r"\d+(\.\d+)?(px|ms|s|%)", "_", m)
    m = re.sub(r"\d+x\d+", "_x_", m)
    m = re.sub(r"\b\d+\b", "_", m)
    m = re.sub(r"\[name='[^']*'\]", "[name=_]", m)
    m = re.sub(r"\s+", " ", m).strip()
    return m[:180]


def group_findings(findings: Sequence[dict]) -> list[dict]:
    """Fold raw findings into stable groups.

    Key = (severity, category, message pattern). info-severity findings are
    excluded: they are the auditor's own "not applicable / untested" notes,
    already self-explanatory, and sending them would spend most of the budget
    on things that need no classification.
    """
    groups: dict[tuple, dict] = {}
    for f in findings:
        sev = (f.get("severity") or "").lower()
        if sev not in ("blocker", "major", "minor"):
            continue
        key = (sev, f.get("category") or "", _pattern(f.get("message") or ""))
        g = groups.setdefault(key, {
            "severity": sev,
            "category": key[1],
            "pattern": key[2],
            "count": 0,
            "pages": set(), "devices": set(), "engines": set(),
            "samples": [],
        })
        g["count"] += 1
        g["pages"].add(f.get("page") or "?")
        g["devices"].add(f.get("device") or "?")
        g["engines"].add(f.get("engine") or "?")
        if len(g["samples"]) < MAX_SAMPLES_PER_GROUP:
            g["samples"].append((f.get("message") or "")[:220])

    out = []
    for i, g in enumerate(sorted(
        groups.values(),
        key=lambda x: ({"blocker": 0, "major": 1, "minor": 2}[x["severity"]],
                       -x["count"]),
    )):
        out.append({
            "id": f"G{i + 1}",
            "severity": g["severity"],
            "category": g["category"],
            "count": g["count"],
            "pages": sorted(g["pages"]),
            "devices": sorted(g["devices"]),
            "engines": sorted(g["engines"]),
            "samples": g["samples"],
        })
    return out


def build_payload(groups: Sequence[dict]) -> tuple[str, list[dict]]:
    """Serialize groups under the hard budget.

    Returns (payload_json, included_groups). Groups that don't fit are
    excluded from the payload but returned to the caller flagged as
    not-analysed — a silent cap would read as "everything was analysed".
    """
    included: list[dict] = []
    for g in groups[:MAX_GROUPS]:
        candidate = included + [g]
        if len(json.dumps(candidate)) > MAX_PAYLOAD_CHARS:
            break
        included.append(g)
    if len(included) < len(groups):
        logger.warning(
            "[ai-mobile] payload budget: analysing %d of %d groups — the "
            "remainder will be marked NOT ANALYSED, not silently dropped.",
            len(included), len(groups),
        )
    return json.dumps(included, indent=1), included


# ── Output validation: Python decides ──────────────────────────────────


def validate_output(
    raw: Any, included: Sequence[dict],
) -> dict[str, Any] | None:
    """Normalize the model's proposal into a trustworthy structure.

    Every group the model was shown gets EXACTLY one entry: missing or
    unrecognized proposals become NEEDS_HUMAN with a rationale saying why.
    An enum violation is never passed through — inventing a category here is
    how a free-text label would quietly become load-bearing later.
    """
    if not isinstance(raw, dict):
        return None
    proposals = raw.get("groups")
    if not isinstance(proposals, list):
        return None

    by_id: dict[str, dict] = {}
    for p in proposals:
        if isinstance(p, dict) and isinstance(p.get("id"), str):
            by_id[p["id"]] = p

    out_groups = []
    for g in included:
        p = by_id.get(g["id"], {})
        cls = str(p.get("classification", "")).strip().upper()
        if cls not in CLASSIFICATIONS:
            cls, rationale = "NEEDS_HUMAN", (
                f"model proposed {p.get('classification')!r}, outside the "
                f"closed set" if p else "model returned no proposal for this group"
            )
        else:
            rationale = str(p.get("rationale", ""))[:300]
        conf = p.get("confidence")
        conf = round(float(conf), 2) if isinstance(conf, (int, float)) \
            and 0 <= float(conf) <= 1 else None
        out_groups.append({
            "id": g["id"],
            "classification": cls,
            "confidence": conf,
            "rationale": rationale,
            "suggested_fix": str(p.get("suggested_fix", ""))[:300],
            "suggested_owner": str(p.get("suggested_owner", ""))[:60],
        })

    summary = str(raw.get("summary", "")).strip()[:400]
    return {"groups": out_groups, "summary": summary}


# ── Entry point ────────────────────────────────────────────────────────


def triage_mobile_findings(
    findings: Sequence[dict],
    engine: str = "",
) -> dict[str, Any] | None:
    """Classify this session's mobile findings. Never raises; never scores.

    Returns the stored triage dict, or None when there was nothing to do or
    no provider is configured (logged either way).
    """
    global _SESSION_TRIAGE

    groups = group_findings(findings)
    if not groups:
        logger.info("[ai-mobile] no blocker/major/minor mobile findings — "
                    "nothing to triage.")
        return None

    if not (os.environ.get("OPENAI_API_KEY", "").strip()
            or os.environ.get("ANTHROPIC_API_KEY", "").strip()):
        logger.info(
            "[ai-mobile] %d finding group(s) present but no AI provider is "
            "configured (OPENAI_API_KEY unset) — triage skipped, dashboard "
            "will say 'AI analysis unavailable'.", len(groups),
        )
        return None

    payload, included = build_payload(groups)
    from utils.ai_parser import send_json
    from utils.ai_prompts import render

    prompt = render(
        "mobile_finding_triage",
        engine=engine or "unknown",
        group_count=str(len(included)),
        groups_json=payload,
        classifications=" | ".join(CLASSIFICATIONS),
    )
    # The output budget must scale with the batch: each classified group can
    # legitimately use ~200 tokens (validate_output allows 300-char rationale
    # and suggested_fix fields), so a flat cap silently truncates large
    # batches. The 2026-08-20 WebKit run proved it: 24 groups against a flat
    # 1600 cap returned exactly 1600 output tokens twice — JSON cut mid-word,
    # whole analysis discarded. 600 covers the envelope and summary.
    parsed, resp = send_json(
        prompt, module="ai_mobile_triage",
        max_tokens=min(8192, 600 + 200 * len(included)),
    )
    if parsed is None:
        logger.warning(
            "[ai-mobile] model output was not usable JSON (provider=%s, "
            "error=%r) — NO analysis will be shown. A fabricated verdict is "
            "worse than an absent one.",
            getattr(resp, "provider", "?"), getattr(resp, "error", None),
        )
        return None

    result = validate_output(parsed, included)
    if result is None:
        logger.warning("[ai-mobile] model JSON had the wrong shape — discarded.")
        return None

    # Groups the budget excluded are surfaced, not forgotten.
    analysed_ids = {g["id"] for g in included}
    for g in groups:
        if g["id"] not in analysed_ids:
            result["groups"].append({
                "id": g["id"], "classification": "NEEDS_HUMAN",
                "confidence": None,
                "rationale": "not analysed — payload budget reached",
                "suggested_fix": "", "suggested_owner": "",
            })

    # Coverage is part of the result: renderers must be able to say
    # "analysed X of Y" instead of implying the analysis was complete.
    result["analysed_count"] = len(included)
    result["total_count"] = len(groups)

    # Attach the input groups so renderers can show what each id means.
    result["input_groups"] = {g["id"]: g for g in groups}
    result["engine"] = engine
    _SESSION_TRIAGE = result
    _update_sidecar(result, engine)
    logger.info(
        "[ai-mobile] triaged %d group(s): %s",
        len(result["groups"]),
        ", ".join(f"{g['id']}={g['classification']}" for g in result["groups"][:8]),
    )
    return result


def _update_sidecar(result: dict, engine: str) -> None:
    """Attach the triage to this engine's mobile-summary.json so cross-engine
    exports carry the analysis, not just the counts. Read-modify-write; a
    missing sidecar is fine (mobile fixture may not have torn down yet)."""
    base = "reports/trend" if (engine or "chromium") == "chromium" \
        else os.path.join("reports/trend", engine)
    path = os.path.join(base, "mobile-summary.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
        data["ai_triage"] = {k: v for k, v in result.items() if k != "input_groups"}
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
    except (OSError, ValueError):
        pass
