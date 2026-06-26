"""
Per-page HTTP capture for failure diagnostics.

Three concerns, three classes (capture / export / analyze) so each can
evolve independently — analyzers can grow without touching capture, and
new exporters (HAR, sqlite, etc.) won't disturb the monitor.

Lifecycle:
  - One NetworkMonitor per Playwright Page, attached in the page fixture
    BEFORE the first navigation so no events are missed.
  - Events accumulate in memory during the test.
  - At test teardown, NetworkExporter writes:
      network-summary.json   — counts, slowest, failures (ages well)
      network-events.json    — every retained event (rawer, denser)
    into the failure folder.
  - NetworkAnalyzer is stateless — call it on a list of events to derive
    summary statistics. Used by the exporter and by the dashboard.

Capture policy (chosen to balance diagnostic value vs artifact size +
secret leakage):

  Resource types — FULL capture (headers + body up to MAX_BODY_BYTES):
      fetch, xhr, document
  Resource types — METADATA-ONLY (no headers, no body), failures only:
      script, stylesheet, font, image, media, other

  Sensitive URLs (login / oauth / payment / etc.) — NEVER capture body
  in either direction; headers always redacted. The URL itself is kept
  so the artifact still says "POST /login → 401" but not what was sent.

  Headers — sensitive keys (Authorization, Cookie, etc.) are always
  redacted regardless of URL.

  JSON bodies — even on non-sensitive URLs, any value under a sensitive
  JSON key (password, access_token, ssn, etc.) is replaced with '***'.

  Body size — hard 2KB cap per request body, no folder cap. Compression
  is the right answer if folders grow large; truncation would silently
  drop evidence.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Schema version — bump whenever the on-disk shape of network-events.json
# or network-summary.json changes. Old dashboards reading newer files
# should refuse to render rather than guess.
SCHEMA_VERSION = 1

# ── Capture policy constants ──────────────────────────────────────────────

MAX_BODY_BYTES = 2048

# Resource types that get full capture (headers + body when status>=400).
FULL_CAPTURE_RESOURCE_TYPES = frozenset({"fetch", "xhr", "document"})

# All other resource types get metadata-only capture, AND only when they fail.
# (Successful CSS/font loads are pure noise.)
METADATA_ONLY_RESOURCE_TYPES = frozenset({
    "script", "stylesheet", "font", "image", "media", "other",
    "websocket", "manifest", "eventsource", "texttrack",
})

# Layer 1 — URL substrings that mean "never capture bodies, redact all".
# Lowercased; matched as substring against lowercased URL.
SENSITIVE_URL_FRAGMENTS = (
    "/login", "/signup", "/register", "/logout",
    "/oauth", "/token", "/auth/", "/cognito",
    "/password", "/reset", "/verify", "/verifypassword",
    "/payment", "/billing", "/credit", "/card",
    "/secret", "/key",
)

# Layer 2 — header keys to redact on every request. Match is case-insensitive.
SENSITIVE_HEADER_KEYS = frozenset({
    "authorization", "cookie", "set-cookie",
    "x-api-key", "x-auth-token", "x-csrf-token", "x-session-token",
    "proxy-authorization", "bearer",
})

# Layer 3 — JSON keys to redact in request/response bodies, even on
# non-sensitive URLs. Case-insensitive substring match against key name.
SENSITIVE_JSON_KEY_FRAGMENTS = (
    "password", "passwd", "secret", "token",
    "access_token", "refresh_token", "id_token",
    "api_key", "apikey", "private_key",
    "ssn", "creditcard", "credit_card", "card_number",
    "cvv", "cvc",
    "session", "cookie",
)

REDACTED = "***REDACTED***"
REDACTED_BODY_SENSITIVE_URL = "<redacted: sensitive URL>"


# ── Schema ────────────────────────────────────────────────────────────────


@dataclass
class NetworkEvent:
    """One HTTP exchange. Fields are nullable because failure paths drop
    some (no response_status if the request never connected, etc.).

    `sequence_id` is a monotonically-increasing per-monitor counter assigned
    at request time. Stable across exports — used as a unique cross-link
    key for timeline, dashboard, fingerprinting, and AI diagnosis.
    """
    sequence_id: int
    timestamp: float                    # epoch seconds (wall clock)
    method: str
    url: str
    resource_type: str
    status: int | None = None
    duration_ms: float | None = None
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    request_body: str | None = None
    response_body: str | None = None
    failure_reason: str | None = None   # set when the request failed at the
                                        # transport layer (DNS, TLS, abort)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Capture ───────────────────────────────────────────────────────────────


class NetworkMonitor:
    """Attach to a Playwright Page, accumulate NetworkEvents until detach.

    Thread-safety: Playwright sync_api delivers events on the dispatcher
    thread but tests read events on the main thread at teardown. A Lock
    guards the event list against the rare overlapping read.

    Usage:
      monitor = NetworkMonitor(page)
      try:
          ... run test ...
      finally:
          monitor.stop()

    Or as a context manager:
      with NetworkMonitor(page) as monitor:
          ... run test ...
    """

    def __init__(self, page: Any) -> None:
        self._page = page
        self._events: list[NetworkEvent] = []
        # request guid → (event, monotonic start time) for matching
        # response events to their originating request.
        self._inflight: dict[str, tuple[NetworkEvent, float]] = {}
        self._lock = threading.Lock()
        self._stopped = False
        self._finished = False
        self._seq = 0   # monotonic event-ID source

        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)

    def __enter__(self) -> "NetworkMonitor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ── Playwright event handlers ────────────────────────────────────────

    def _on_request(self, request: Any) -> None:
        try:
            with self._lock:
                self._seq += 1
                seq = self._seq
            ev = NetworkEvent(
                sequence_id=seq,
                timestamp=time.time(),
                method=request.method,
                url=request.url,
                resource_type=getattr(request, "resource_type", "other"),
                request_headers=_redact_headers(dict(request.headers)),
            )
            # Request body — only fill in for full-capture resource types
            # on non-sensitive URLs. Read at response time would be safer
            # but Playwright doesn't keep it; capture now.
            if (ev.resource_type in FULL_CAPTURE_RESOURCE_TYPES
                    and not _is_sensitive_url(ev.url)):
                try:
                    body = request.post_data
                    if body is not None:
                        ev.request_body = _truncate_and_redact_body(body)
                except Exception:
                    pass
            elif _is_sensitive_url(ev.url):
                ev.request_body = REDACTED_BODY_SENSITIVE_URL
            self._inflight[id(request)] = (ev, time.monotonic())
        except Exception as e:
            logger.debug("[net] _on_request capture failed: %s", e)

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            ev_tuple = self._inflight.pop(id(request), None)
            if ev_tuple is None:
                # Response without a matched request — synthesise a minimal event.
                with self._lock:
                    self._seq += 1
                    seq = self._seq
                ev = NetworkEvent(
                    sequence_id=seq,
                    timestamp=time.time(),
                    method=getattr(request, "method", "?"),
                    url=response.url,
                    resource_type=getattr(request, "resource_type", "other"),
                )
                started = time.monotonic()
            else:
                ev, started = ev_tuple

            ev.status = response.status
            ev.duration_ms = (time.monotonic() - started) * 1000.0
            ev.response_headers = _redact_headers(dict(response.headers))

            # Body capture rules:
            #   - sensitive URL → never
            #   - full-capture resource + status>=400 → up to MAX_BODY_BYTES
            #   - everything else → skip (would be a successful payload
            #     for a non-debug resource — noise)
            if _is_sensitive_url(ev.url):
                ev.response_body = REDACTED_BODY_SENSITIVE_URL
            elif (ev.resource_type in FULL_CAPTURE_RESOURCE_TYPES
                  and ev.status is not None and ev.status >= 400):
                try:
                    raw = response.body()  # bytes
                    if raw is not None:
                        ev.response_body = _truncate_and_redact_body(
                            raw.decode("utf-8", errors="replace")
                        )
                except Exception:
                    pass

            self._append(ev)
        except Exception as e:
            logger.debug("[net] _on_response capture failed: %s", e)

    def _on_request_failed(self, request: Any) -> None:
        try:
            ev_tuple = self._inflight.pop(id(request), None)
            if ev_tuple is None:
                with self._lock:
                    self._seq += 1
                    seq = self._seq
                ev = NetworkEvent(
                    sequence_id=seq,
                    timestamp=time.time(),
                    method=getattr(request, "method", "?"),
                    url=getattr(request, "url", ""),
                    resource_type=getattr(request, "resource_type", "other"),
                )
                started = time.monotonic()
            else:
                ev, started = ev_tuple
            ev.duration_ms = (time.monotonic() - started) * 1000.0
            ev.failure_reason = getattr(request, "failure", None) or "unknown"
            self._append(ev)
        except Exception as e:
            logger.debug("[net] _on_request_failed capture failed: %s", e)

    # ── Retention filter ─────────────────────────────────────────────────

    def _append(self, ev: NetworkEvent) -> None:
        """Drop events that don't pass the resource-type / failure filter.

        Keep:
          - All full-capture resource types (fetch/xhr/document) regardless
            of status.
          - Failed requests of any other resource type (metadata-only).
        Drop:
          - Successful 2xx CSS/font/image/etc. — pure noise.
        """
        is_full = ev.resource_type in FULL_CAPTURE_RESOURCE_TYPES
        is_failure = (
            ev.failure_reason is not None
            or (ev.status is not None and ev.status >= 400)
        )
        if not is_full and not is_failure:
            return
        # Metadata-only resource types drop headers/body even on failure.
        if not is_full:
            ev.request_headers = {}
            ev.response_headers = {}
            ev.request_body = None
            ev.response_body = None
        with self._lock:
            self._events.append(ev)

    # ── Read API ─────────────────────────────────────────────────────────

    def events(self) -> tuple[NetworkEvent, ...]:
        """Return an immutable snapshot of captured events. Callers can't
        mutate the monitor's internal state through the return value."""
        with self._lock:
            return tuple(self._events)

    def failures(self) -> tuple[NetworkEvent, ...]:
        return tuple(e for e in self.events() if _is_failure(e))

    def summary(self) -> dict[str, Any]:
        """Convenience — defers to NetworkAnalyzer."""
        return NetworkAnalyzer.summary(self.events())

    # ── Session aggregation ──────────────────────────────────────────────

    def finish_test(self) -> None:
        """Record this test's traffic in the session-level aggregator.

        Must be called exactly once per test, at teardown, BEFORE stop().
        Runs regardless of test outcome (Option C of the lifecycle design):
        aggregation is driven by test completion, not by artifact export.
        Idempotent — safe to call multiple times but only the first counts.
        """
        if getattr(self, "_finished", False):
            return
        self._finished = True
        try:
            _record_run_total(self.summary(), self.events())
        except Exception as e:
            logger.debug("[net] finish_test aggregation failed: %s", e)

    # ── Persistence ──────────────────────────────────────────────────────

    def export(self, folder: Path) -> tuple[Path, Path]:
        """Write network-summary.json + network-events.json into `folder`.
        Stable public API — the on-disk layout can evolve without callers
        having to know about NetworkExporter.

        Does NOT update session aggregates — call finish_test() for that.
        Aggregation and export are deliberately decoupled so passing tests
        (which never export) still contribute to the session overview.
        """
        return NetworkExporter.export_to(folder, self.events())

    # ── Lifecycle ────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Remove Playwright listeners. Idempotent — safe to call from
        teardown even if attach failed mid-construction."""
        if self._stopped:
            return
        self._stopped = True
        for evt, fn in (
            ("request", self._on_request),
            ("response", self._on_response),
            ("requestfailed", self._on_request_failed),
        ):
            try:
                self._page.remove_listener(evt, fn)
            except Exception:
                pass

    # Back-compat alias.
    detach = stop


# ── Analyze ───────────────────────────────────────────────────────────────


class NetworkAnalyzer:
    """Stateless helpers that summarise a list of NetworkEvents.

    Kept separate from NetworkMonitor so the dashboard can analyse
    archived event lists without re-running tests.
    """

    SLOW_THRESHOLD_MS = 2000.0

    @classmethod
    def summary(cls, events: Iterable[NetworkEvent]) -> dict[str, Any]:
        evs = list(events)
        failed = [e for e in evs if _is_failure(e)]
        slowest = sorted(
            (e for e in evs if e.duration_ms is not None),
            key=lambda e: e.duration_ms or 0,
            reverse=True,
        )[:10]
        by_status: dict[str, int] = {}
        for e in evs:
            key = str(e.status) if e.status is not None else (
                f"FAILED:{e.failure_reason or 'unknown'}"
            )
            by_status[key] = by_status.get(key, 0) + 1

        durations = [e.duration_ms for e in evs if e.duration_ms is not None]
        avg_latency = (sum(durations) / len(durations)) if durations else 0.0
        p95_latency = cls._percentile(durations, 95) if durations else 0.0
        failed_pct  = (len(failed) / len(evs) * 100.0) if evs else 0.0

        return {
            "total_requests":     len(evs),
            "failed_count":       len(failed),
            "failed_percentage":  round(failed_pct, 2),
            "slow_count":         sum(
                1 for e in evs
                if e.duration_ms is not None and e.duration_ms >= cls.SLOW_THRESHOLD_MS
            ),
            "avg_latency_ms":     round(avg_latency, 1),
            "p95_latency_ms":     round(p95_latency, 1),
            "by_status":          by_status,
            "slowest":            [e.to_dict() for e in slowest],
        }

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        """Linear-interpolation percentile. No numpy dependency."""
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        k = (len(ordered) - 1) * (pct / 100.0)
        lo = int(k)
        hi = min(lo + 1, len(ordered) - 1)
        frac = k - lo
        return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


# ── Export ────────────────────────────────────────────────────────────────


_RUN_TOTAL = {
    "requests":             0,
    "failed":               0,
    "slow":                 0,
    "all_durations":        [],   # all durations across the session — bounded
                                  # by request count, used to compute P95
    "top_failed_endpoints": {},   # "METHOD url" → count
    "top_slowest":          [],   # list of (duration_ms, url, status)
}
_RUN_TOTAL_LOCK = threading.Lock()


def _record_run_total(summary: dict[str, Any], events: Iterable[NetworkEvent]) -> None:
    """Accumulate one test's summary into the session-level totals.
    Called by NetworkExporter.export_to so every export contributes."""
    with _RUN_TOTAL_LOCK:
        _RUN_TOTAL["requests"] += summary.get("total_requests", 0)
        _RUN_TOTAL["failed"]   += summary.get("failed_count", 0)
        _RUN_TOTAL["slow"]     += summary.get("slow_count", 0)
        for e in events:
            if e.duration_ms is not None:
                _RUN_TOTAL["all_durations"].append(e.duration_ms)
            if _is_failure(e):
                key = f"{e.method} {e.url}"
                _RUN_TOTAL["top_failed_endpoints"][key] = (
                    _RUN_TOTAL["top_failed_endpoints"].get(key, 0) + 1
                )
            if e.duration_ms is not None and e.duration_ms >= NetworkAnalyzer.SLOW_THRESHOLD_MS:
                _RUN_TOTAL["top_slowest"].append((
                    e.duration_ms, e.url, e.status or 0,
                ))


def export_run_overview(folder: Path) -> Path:
    """Write reports/run_summary/network_overview.json with session-wide
    aggregates. Cheap to call — uses in-memory counters built up by every
    per-test export. Returns the written path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "network_overview.json"
    with _RUN_TOTAL_LOCK:
        top_failed = sorted(
            _RUN_TOTAL["top_failed_endpoints"].items(),
            key=lambda kv: kv[1], reverse=True,
        )[:20]
        top_slowest = sorted(
            _RUN_TOTAL["top_slowest"], key=lambda t: t[0], reverse=True,
        )[:20]
        durations = _RUN_TOTAL["all_durations"]
        total = _RUN_TOTAL["requests"]
        failed = _RUN_TOTAL["failed"]
        avg_latency = (sum(durations) / len(durations)) if durations else 0.0
        p95_latency = NetworkAnalyzer._percentile(durations, 95) if durations else 0.0
        failed_pct  = (failed / total * 100.0) if total else 0.0
        payload = {
            "schema_version":    SCHEMA_VERSION,
            "generated_at":      time.time(),
            "requests":          total,
            "failed":            failed,
            "failed_percentage": round(failed_pct, 2),
            "slow":              _RUN_TOTAL["slow"],
            "avg_latency_ms":    round(avg_latency, 1),
            "p95_latency_ms":    round(p95_latency, 1),
            "top_failed_endpoints": [
                {"endpoint": ep, "count": n} for ep, n in top_failed
            ],
            "top_slowest": [
                {"duration_ms": d, "url": u, "status": s}
                for d, u, s in top_slowest
            ],
        }
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out


class NetworkExporter:
    """Write captured events to disk in a stable, versioned format."""

    @staticmethod
    def export_to(folder: Path, events: Iterable[NetworkEvent]) -> tuple[Path, Path]:
        """Write network-summary.json + network-events.json into `folder`.

        Returns (summary_path, events_path). Caller must ensure the folder
        exists; this method writes only the two JSON files.
        """
        # Deterministic ordering: sort by sequence_id so identical traffic
        # produces byte-identical JSON across runs (critical for fingerprint
        # stability and diff-based regression detection).
        evs = sorted(events, key=lambda e: e.sequence_id)
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)

        summary_path = folder / "network-summary.json"
        events_path  = folder / "network-events.json"

        summary_payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at":   time.time(),
            **NetworkAnalyzer.summary(evs),
            "failures": [e.to_dict() for e in evs if _is_failure(e)],
        }
        events_payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at":   time.time(),
            "events":         [e.to_dict() for e in evs],
        }

        summary_path.write_text(json.dumps(summary_payload, indent=2, default=str),
                                encoding="utf-8")
        events_path.write_text(json.dumps(events_payload, indent=2, default=str),
                               encoding="utf-8")

        # Aggregation is intentionally NOT done here — see
        # NetworkMonitor.finish_test(). Aggregating in the exporter would
        # mean passing tests (which never export) are missing from the
        # session-level network_overview.json.

        return summary_path, events_path


# ── Redaction helpers (module-private) ────────────────────────────────────


def _is_sensitive_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    return any(frag in lower for frag in SENSITIVE_URL_FRAGMENTS)


def _is_failure(e: NetworkEvent) -> bool:
    return (
        e.failure_reason is not None
        or (e.status is not None and e.status >= 400)
    )


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in SENSITIVE_HEADER_KEYS:
            out[k] = REDACTED
        else:
            out[k] = v
    return out


def _truncate_and_redact_body(body: str) -> str:
    """Apply Layer-3 JSON-key redaction, then cap at MAX_BODY_BYTES."""
    redacted = _redact_json_keys(body)
    if len(redacted) > MAX_BODY_BYTES:
        return redacted[:MAX_BODY_BYTES] + f"…<truncated, total {len(redacted)} bytes>"
    return redacted


def _redact_json_keys(body: str) -> str:
    """Parse `body` as JSON; if it parses, deep-walk and redact sensitive
    keys; serialize back. If it doesn't parse, return body unchanged
    (we don't try to redact arbitrary string formats — that's a footgun)."""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return body

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: (REDACTED if _is_sensitive_key(k) else walk(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    try:
        return json.dumps(walk(parsed), separators=(",", ":"))
    except (TypeError, ValueError):
        return body


def _is_sensitive_key(key: str) -> bool:
    if not isinstance(key, str):
        return False
    lower = key.lower()
    return any(frag in lower for frag in SENSITIVE_JSON_KEY_FRAGMENTS)
