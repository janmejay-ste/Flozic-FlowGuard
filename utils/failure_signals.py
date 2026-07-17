"""
Failure signal consolidator.

Reads per-test artifacts produced by other subsystems (NetworkMonitor,
ai_validator, ai_popup_validator, ai_triage, Playwright) and emits a single
normalized failure-signals.json. Sprint-2 FingerprintEngine consumes this
file directly — it does NOT re-read raw artifacts.

Single-responsibility design (per the architectural review):
  NetworkMonitor      → network-summary.json    (raw evidence)
  ai_validator        → ai_verdict.json         (raw evidence)
  ai_popup_validator  → popup_verdict.json      (raw evidence)
  ai_triage           → triage.json             (raw evidence)
  Playwright          → screenshot + dom.html   (raw evidence)
  FailureSignalBuilder → failure-signals.json   (NORMALIZED, consolidated)
  FingerprintEngine    → fingerprints.json      (groups by signal hash)

Each subsystem owns its raw artifact. This module only consolidates and
normalizes — it never overwrites another subsystem's file.

Normalization rules (apply to every string-typed signal before storage):
  URLs       — strip query strings, replace UUID / Mongo ObjectId / numeric
               path segments / random-string segments with placeholder
               tokens so /api/users/abc123-def4-… and /api/users/xyz789-…
               produce the same normalized form.
  Tracebacks — strip line numbers, memory addresses, timestamps. Stack
               trace frames keep file path + symbol, lose `:42` suffix.
  Messages   — lowercase, collapse whitespace, strip trailing punctuation,
               strip references to specific run timestamps.

Why normalize at storage time rather than at fingerprint time? Because the
normalized form is also useful for the dashboard's diagnosis text and the
deduplication of "top failed endpoints" — keeping it in failure-signals.json
means every consumer sees the same stable representation.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Schema version — bump when the on-disk shape of failure-signals.json
# changes. tests/unit/test_failure_signals.py pins schema_version=1.
SCHEMA_VERSION = 1


# ── Normalization rules ────────────────────────────────────────────────

# Regex patterns for path/URL token replacement. Order matters: more
# specific patterns first, otherwise a generic /<id> would eat UUIDs.
_RE_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_RE_OID  = re.compile(r"\b[0-9a-f]{24}\b")        # MongoDB ObjectId
_RE_LONG_HEX = re.compile(r"\b[0-9a-f]{16,}\b")   # session ids, hashes
_RE_NUM_PATH = re.compile(r"/\d+(?=/|$)")          # /12345/ → /<id>/
_RE_RAND_PATH = re.compile(                        # /aB3-…_xY_… → /<rand>
    r"/[A-Za-z0-9_-]{16,}(?=/|$)"
)

# Generic in-message scrubbing.
_RE_LINE_NUM = re.compile(r":\d+(:\d+)?(?=[\s\)\]])")     # file.py:42:5
_RE_MEM_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_RE_ISO_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_RE_HHMMSS   = re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b")
_RE_WHITESPACE = re.compile(r"\s+")
_RE_TIMEOUT_MS = re.compile(r"\b\d{4,}\s*ms\b", re.IGNORECASE)   # "20000ms" → "<ms>"


def _normalize_url(url: str) -> str:
    """Strip query, replace UUID/OID/numeric/random path segments with
    placeholders. Preserves scheme + host + path shape."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except Exception:
        return _normalize_message(url)
    path = parts.path or ""
    path = _RE_UUID.sub("<uuid>", path)
    path = _RE_OID.sub("<oid>", path)
    path = _RE_LONG_HEX.sub("<hex>", path)
    path = _RE_NUM_PATH.sub("/<id>", path)
    path = _RE_RAND_PATH.sub("/<rand>", path)
    # Drop query + fragment — they're full of session/state noise.
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _normalize_message(s: str) -> str:
    """Apply scrubbing + lowercase + whitespace collapse + punctuation strip.

    Idempotent — repeated application produces the same result. Safe to
    call on already-normalized strings (the FingerprintEngine will).
    """
    if not s:
        return ""
    out = s
    out = _RE_ISO_TIME.sub("<ts>", out)
    out = _RE_HHMMSS.sub("<ts>", out)
    out = _RE_MEM_ADDR.sub("<addr>", out)
    out = _RE_LINE_NUM.sub("", out)
    out = _RE_UUID.sub("<uuid>", out)
    out = _RE_OID.sub("<oid>", out)
    out = _RE_TIMEOUT_MS.sub("<ms>", out)
    out = out.lower()
    out = _RE_WHITESPACE.sub(" ", out).strip()
    out = out.rstrip(".,;:!?")
    return out


def _normalize_traceback(tb: str) -> str:
    """Like _normalize_message but preserves frame structure. Strips line
    numbers and memory addresses; keeps file paths and symbol names."""
    if not tb:
        return ""
    # Strip line numbers (file.py:42 → file.py)
    out = _RE_LINE_NUM.sub("", tb)
    out = _RE_MEM_ADDR.sub("<addr>", out)
    out = _RE_ISO_TIME.sub("<ts>", out)
    out = _RE_UUID.sub("<uuid>", out)
    # Collapse internal blank lines but keep newlines.
    out = re.sub(r"\n[ \t]*\n+", "\n", out)
    return out.strip()


# ── Signal schema ──────────────────────────────────────────────────────


@dataclass
class NetworkSignals:
    """Failed-endpoint summary distilled from network-summary.json.
    Normalized URLs ready for fingerprint hashing."""
    failed_endpoints: list[dict[str, Any]] = field(default_factory=list)
    timeout_count:    int = 0
    total_failures:   int = 0


@dataclass
class AISignals:
    """Verdict-level facts from ai_validator / ai_popup_validator / ai_triage.
    Reasoning strings are pre-normalized."""
    validator_status:           str = ""   # VALID / INVALID / ERROR / SKIPPED
    validator_reasoning_norm:   str = ""
    popup_status:               str = ""
    popup_reasoning_norm:       str = ""
    triage_category:            str = ""
    triage_diagnosis_norm:      str = ""


@dataclass
class ExceptionSignals:
    """Last-line exception class + normalized message."""
    type:            str = ""
    message_norm:    str = ""
    traceback_norm:  str = ""


@dataclass
class FailureSignals:
    """One failure → one consolidated signals record. Written to
    failure-signals.json in the per-failure folder."""
    schema_version: int = SCHEMA_VERSION
    test_name:      str = ""
    network:        NetworkSignals    = field(default_factory=NetworkSignals)
    ai:             AISignals         = field(default_factory=AISignals)
    exception:      ExceptionSignals  = field(default_factory=ExceptionSignals)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Builder ────────────────────────────────────────────────────────────


class FailureSignalBuilder:
    """Reads the raw evidence files in a failure folder and produces a
    normalized FailureSignals record.

    Stateless — pass folder paths and exception strings; the builder
    consults the disk for already-written artifacts. Missing artifacts are
    silently treated as empty (network-summary may not exist if capture is
    off; ai_verdict may not exist if no AI key was set).
    """

    @staticmethod
    def build_for(
        failure_folder: Path,
        *,
        test_name:      str = "",
        exception_type: str = "",
        exception_msg:  str = "",
        traceback:      str = "",
        verdict_paths:  list[Path] | None = None,
    ) -> FailureSignals:
        signals = FailureSignals(test_name=test_name)

        # ── Network signals ────────────────────────────────────────────
        net_summary_path = failure_folder / "network-summary.json"
        if net_summary_path.exists():
            try:
                net = json.loads(net_summary_path.read_text())
                signals.network = _extract_network_signals(net)
            except Exception as e:
                logger.debug("[failure-signals] network parse failed: %s", e)

        # ── AI signals ─────────────────────────────────────────────────
        # ai_validator's ai_verdict.json + ai_popup_validator's popup_verdict.json
        # live in reports/recordings/<test_id>/, not in the failure folder.
        # The caller passes paths explicitly so this builder doesn't have
        # to know the per-test folder convention.
        for vp in (verdict_paths or []):
            if not vp.exists():
                continue
            try:
                v = json.loads(vp.read_text())
            except Exception as e:
                logger.debug("[failure-signals] verdict parse failed (%s): %s", vp, e)
                continue
            name = vp.name.lower()
            if "popup" in name:
                signals.ai.popup_status = v.get("status", "")
                signals.ai.popup_reasoning_norm = _normalize_message(
                    v.get("reasoning", "")
                )
            else:
                signals.ai.validator_status = v.get("status", "")
                signals.ai.validator_reasoning_norm = _normalize_message(
                    v.get("reasoning", "")
                )

        # ai_triage.json lives in the failure folder itself.
        triage_path = failure_folder / "triage.json"
        if triage_path.exists():
            try:
                t = json.loads(triage_path.read_text())
                signals.ai.triage_category = t.get("category", "")
                signals.ai.triage_diagnosis_norm = _normalize_message(
                    t.get("diagnosis", "")
                )
            except Exception as e:
                logger.debug("[failure-signals] triage parse failed: %s", e)

        # ── Exception signals ─────────────────────────────────────────
        signals.exception.type = exception_type or ""
        signals.exception.message_norm = _normalize_message(exception_msg)
        signals.exception.traceback_norm = _normalize_traceback(traceback)

        return signals

    @staticmethod
    def export_to(
        failure_folder: Path,
        signals:        FailureSignals,
    ) -> Path:
        """Write failure-signals.json into `failure_folder`. Returns path."""
        target = Path(failure_folder) / "failure-signals.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(signals.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
        return target


# ── Network-signal extraction ──────────────────────────────────────────


def _extract_network_signals(summary: dict[str, Any]) -> NetworkSignals:
    """Distill the failed-request list from a network-summary.json payload.

    Groups by (method, normalized_url, status/failure_reason) and counts —
    so multiple retries against the same endpoint collapse to one signal.
    """
    failures = summary.get("failures") or []
    by_key: dict[tuple[str, str, str], int] = {}
    timeout_count = 0
    for ev in failures:
        method = ev.get("method", "?") or "?"
        url_norm = _normalize_url(ev.get("url", ""))
        failure_reason = ev.get("failure_reason")
        status = ev.get("status")
        if failure_reason:
            status_token = f"FAILED:{failure_reason}"
            # Browsers spell this several ways: "timeout" (Playwright),
            # "ERR_TIMED_OUT" (Chrome), "timed out" (Firefox). Normalize
            # before checking so we count all three as timeouts.
            fr_lower = str(failure_reason).lower().replace("_", " ").replace("-", " ")
            if "timeout" in fr_lower or "timed out" in fr_lower:
                timeout_count += 1
        else:
            status_token = str(status) if status is not None else "FAILED:unknown"
        key = (method, url_norm, status_token)
        by_key[key] = by_key.get(key, 0) + 1

    failed_endpoints = [
        {
            "method":         m,
            "url_normalized": u,
            "status_token":   s,
            "count":          c,
        }
        for (m, u, s), c in sorted(
            by_key.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
    ]
    return NetworkSignals(
        failed_endpoints=failed_endpoints,
        timeout_count=timeout_count,
        total_failures=int(summary.get("failed_count") or 0),
    )
