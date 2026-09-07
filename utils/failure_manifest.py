"""
Observability v2 / Phase 1 — per-failure manifest (failure.json + console.json).

When a test fails, we already capture screenshot/DOM/url/video and the FULL
network traffic (request/response bodies in network-events.json). What was
missing is the EXTRACTION: the console events died with the process, and the
backend's state at failure time sat unread inside the captured bodies.

write_failure_manifest() runs at teardown for each failure and writes into the
failure folder:
  * console.json  — the test's JS console events (previously in-memory only)
  * failure.json  — a small manifest a developer (or the report) reads first:
      test, error signature, page URL, connect/account id parsed from the
      editor URL, console error count, and BACKEND STATE: the last relevant
      API responses (endpoint, status, body excerpt) pulled from the already-
      captured network events — turning "the UI state never appeared" into
      "getByConnectId returned 200 with status 'pending'".

Deterministic extraction only (no AI), and never raises into a run.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _event_to_dict(e: Any) -> dict:
    """Normalize a console event to a plain dict. Events arrive as
    dataclasses (utils.js_console_monitor.ConsoleEvent) at runtime but as
    dicts in tests/other callers — accept both (and any object with the
    attrs) so JSON serialization never throws into a run."""
    if isinstance(e, dict):
        return e
    if dataclasses.is_dataclass(e) and not isinstance(e, type):
        return dataclasses.asdict(e)
    return {k: getattr(e, k, None) for k in ("type", "text", "location", "source_origin")}

SCHEMA_VERSION = 1

# API path markers whose responses describe backend/workflow state for the
# flows this framework exercises. Order = display priority.
RELEVANT_API_MARKERS = (
    "getByConnectId", "customerconnect/", "copilot", "loginAssist",
    "getAppsCustom", "createConnect", "saveConnect",
)

_CONNECT_URL_RE = re.compile(r"/customeditor/([a-z0-9]+)(?:/account/([a-z0-9]+))?", re.I)


def parse_connect_ids(url: str) -> dict[str, str | None]:
    m = _CONNECT_URL_RE.search(url or "")
    return {"connect_id": m.group(1) if m else None,
            "account_id": m.group(2) if m else None}


def _short_url(url: str) -> str:
    """Path + first marker context, without host/query noise."""
    u = re.sub(r"^https?://[^/]+", "", url or "")
    return u.split("?")[0][-120:]


def extract_backend_state(events: list[dict], limit: int = 5) -> list[dict[str, Any]]:
    """The LAST `limit` relevant API responses from captured network events —
    newest last, so the final entry is the backend's state closest to the
    failure. Purely reads what the run already recorded."""
    relevant = [e for e in events or []
                if any(m in str(e.get("url", "")) for m in RELEVANT_API_MARKERS)]
    out = []
    for e in relevant[-limit:]:
        body = e.get("response_body")
        excerpt = None
        if isinstance(body, str) and body.strip():
            excerpt = body.strip()[:300]
        elif isinstance(body, (dict, list)):
            excerpt = json.dumps(body)[:300]
        out.append({
            "endpoint": _short_url(str(e.get("url", ""))),
            "method": e.get("method"),
            "status": e.get("status"),
            "failure_reason": e.get("failure_reason"),
            "duration_ms": e.get("duration_ms"),
            "body_excerpt": excerpt,
        })
    return out


def write_failure_manifest(folder: Path | str, test_name: str,
                           error_signature: str,
                           console_events: list[dict] | None) -> Path | None:
    """Write console.json + failure.json into an existing failure folder.
    Reads url.txt / network-events.json that _capture_failure_artifacts already
    wrote there. Defensive end to end — a manifest failure never fails a test."""
    try:
        from utils.atomic_io import atomic_write_json
        folder = Path(folder)
        if not folder.is_dir():
            return None

        console_events = [_event_to_dict(e) for e in list(console_events or [])[:500]]
        atomic_write_json(folder / "console.json", console_events)
        console_errors = sum(1 for e in console_events
                             if str(e.get("type", "")).lower() == "error")

        page_url = ""
        u = folder / "url.txt"
        if u.is_file():
            try:
                page_url = u.read_text(encoding="utf-8").strip()
            except OSError:
                pass

        net_events: list[dict] = []
        ne = folder / "network-events.json"
        if ne.is_file():
            try:
                data = json.loads(ne.read_text(encoding="utf-8"))
                net_events = data if isinstance(data, list) else data.get("events", [])
            except (OSError, ValueError):
                pass

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "test": test_name,
            "captured_at_ms": int(time.time() * 1000),
            "error_signature": error_signature or None,
            "page_url": page_url or None,
            **parse_connect_ids(page_url),
            "console": {"total_events": len(console_events),
                        "errors": console_errors,
                        "file": "console.json"},
            "backend_state": extract_backend_state(net_events),
        }
        path = folder / "failure.json"
        atomic_write_json(path, manifest)
        return path
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[failure] manifest write failed (non-fatal): %s", e)
        return None
