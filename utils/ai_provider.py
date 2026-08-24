"""
Provider abstraction for the `utils/ai_*.py` modules.

Selects the LLM backend once at module load, keeping each ai_*.py caller
provider-agnostic. Callers get a `send()` function that takes a prompt +
optional images and returns a normalized `AIResponse`.

Selection rules (checked in order):
  1. FLOZIC_AI_PROVIDER env var — explicit "claude" or "openai"
  2. ANTHROPIC_API_KEY set + anthropic package installed → claude
  3. OPENAI_API_KEY set → openai
  4. Neither → NoopProvider that returns `SKIPPED`

A missing key never crashes — the AI helpers already handle SKIPPED /
ERROR gracefully. The framework degrades to keyword-only validation and
tests keep running.

Both providers return the same `AIResponse` shape so callers write one
JSON-parsing path. Vision support (image_paths) works for both.

Every call routed through `send()` is metered by `utils/ai_cost_tracker.py`
— token counts and USD are attributed to the calling ai_*.py module, and a
session cap (FLOZIC_AI_COST_CAP_USD) short-circuits further calls once the
budget is gone. Metering lives here rather than in each caller precisely
because this is the one function they all already go through.
"""

from __future__ import annotations

import base64
import inspect
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class AIResponse:
    """Uniform envelope across providers."""
    text:            str = ""
    parsed_json:     dict[str, Any] | None = None
    provider:        str = "noop"     # "openai" | "claude" | "noop"
    model:           str = ""
    input_tokens:    int = 0
    output_tokens:   int = 0
    error:           str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.text != ""


# ── Provider selection ─────────────────────────────────────────────────


def _has_anthropic_pkg() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _pick_provider_name() -> str:
    """Determine which provider to use for this session."""
    explicit = os.environ.get("FLOZIC_AI_PROVIDER", "").strip().lower()
    if explicit in ("claude", "anthropic"):
        return "claude"
    if explicit == "openai":
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY", "").strip() and _has_anthropic_pkg():
        return "claude"
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "openai"
    return "noop"


PROVIDER_NAME = _pick_provider_name()


# ── Caller attribution ─────────────────────────────────────────────────

# Frames belonging to the plumbing itself — never the answer to "who spent
# this?". Walking past them lands on the ai_*.py module that actually asked.
_PLUMBING = {"ai_provider", "ai_parser", "claude_client", "ai_cost_tracker"}


def _calling_module() -> str:
    """
    Best-effort name of the ai_*.py module that initiated this call.

    Inferred from the stack rather than required as an argument so that cost
    attribution works for every existing call site without touching it, and
    can't silently go stale when a module is renamed. Callers that want to
    be explicit can pass `module=`.
    """
    try:
        for frame_info in inspect.stack()[1:]:
            name = Path(frame_info.filename).stem
            if name not in _PLUMBING and name.startswith("ai_"):
                return name
        # Not called from an ai_* module — a script or test calling directly.
        for frame_info in inspect.stack()[1:]:
            name = Path(frame_info.filename).stem
            if name not in _PLUMBING:
                return name
    except Exception:            # pragma: no cover — introspection is best-effort
        pass
    return "unknown"


# ── Uniform send() ─────────────────────────────────────────────────────


def send(
    prompt: str,
    *,
    model:          str | None = None,
    image_paths:    list[Path] | None = None,
    response_json:  bool = True,
    temperature:    float = 0.0,
    max_tokens:     int   = 2048,
    system_prompt:  str   | None = None,
    module:         str   | None = None,
) -> AIResponse:
    """
    Send a prompt to the selected provider. Returns AIResponse.

    Callers should not need to know which provider is in use. When adding
    a new ai_*.py module, use `send()` — never import provider modules
    directly. This keeps future model migrations to one place.

    `module` names the spender for cost attribution; when omitted it is
    inferred from the call stack.
    """
    from utils import ai_cost_tracker

    attribution = module or _calling_module()

    # Budget gate. Refusing here — rather than inside each caller — means a
    # blown budget degrades every AI feature the same way: to the SKIPPED /
    # keyword-only path the modules already handle. Tests keep running.
    if ai_cost_tracker.over_budget():
        return AIResponse(
            provider=PROVIDER_NAME,
            error=(
                f"AI session budget of ${ai_cost_tracker.TRACKER.cap_usd:.2f} "
                f"exhausted; call from {attribution} skipped."
            ),
        )

    if PROVIDER_NAME == "claude":
        resp = _send_claude(
            prompt, model=model, image_paths=image_paths,
            response_json=response_json, temperature=temperature,
            max_tokens=max_tokens, system_prompt=system_prompt,
        )
    elif PROVIDER_NAME == "openai":
        resp = _send_openai(
            prompt, model=model, image_paths=image_paths,
            response_json=response_json, temperature=temperature,
            max_tokens=max_tokens, system_prompt=system_prompt,
        )
    else:
        return AIResponse(provider="noop", error="No AI provider configured.")

    # Record even on error: a failed call that burned input tokens still
    # costs money, and an error rate per module is worth seeing.
    ai_cost_tracker.record(
        module=attribution,
        provider=resp.provider,
        model=resp.model,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        error=resp.error,
    )
    return resp


# ── Claude adapter ─────────────────────────────────────────────────────


def _send_claude(
    prompt: str, *, model, image_paths, response_json,
    temperature, max_tokens, system_prompt,
) -> AIResponse:
    from utils.claude_client import send as claude_send, DEFAULT_CLAUDE_MODEL
    resp = claude_send(
        prompt, model=model, image_paths=image_paths,
        response_json=response_json, temperature=temperature,
        max_tokens=max_tokens, system_prompt=system_prompt,
    )
    return AIResponse(
        text          = resp.text,
        parsed_json   = resp.parsed_json,
        provider      = "claude",
        model         = resp.model or (model or DEFAULT_CLAUDE_MODEL),
        input_tokens  = resp.input_tokens,
        output_tokens = resp.output_tokens,
        error         = resp.error,
    )


# ── OpenAI adapter ─────────────────────────────────────────────────────


def _send_openai(
    prompt: str, *, model, image_paths, response_json,
    temperature, max_tokens, system_prompt,
) -> AIResponse:
    """OpenAI chat-completions transport. Mirrors the shape of every existing
    utils/ai_*.py caller before the provider abstraction landed."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return AIResponse(provider="openai", error="OPENAI_API_KEY not set")

    try:
        import requests
    except ImportError:
        return AIResponse(
            provider="openai",
            error="requests package not installed",
        )

    model = model or os.environ.get("OPENAI_MODEL", "gpt-4o")

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for p in (image_paths or []):
        p = Path(p)
        if not p.exists():
            logger.warning("[openai] image not found: %s", p)
            continue
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": content})

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_json:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload, headers=headers, timeout=60,
        )
    except Exception as e:
        return AIResponse(provider="openai", model=model, error=str(e))

    if r.status_code != 200:
        return AIResponse(
            provider="openai", model=model,
            error=f"HTTP {r.status_code}: {r.text[:400]}",
        )

    try:
        data = r.json()
        text = data["choices"][0]["message"]["content"] or ""
    except Exception as e:
        return AIResponse(
            provider="openai", model=model,
            error=f"Response parse failed: {e}",
        )

    usage = data.get("usage") or {}
    finish_reason = str(data["choices"][0].get("finish_reason") or "")
    parsed = None
    truncated: str | None = None
    if response_json:
        from utils.ai_parser import parse_json
        result = parse_json(text)
        parsed = result if isinstance(result, dict) else None
        if parsed is None and text:
            if finish_reason == "length":
                # The completion hit max_tokens mid-JSON. Reported as an
                # error (not just a warning) because send_json's corrective
                # retry re-sends with the SAME cap and a LONGER prompt — it
                # is guaranteed to truncate again, so it must not run.
                truncated = (
                    f"response truncated at max_tokens={max_tokens} "
                    "(finish_reason=length) — the output budget is too "
                    "small for this prompt; raise max_tokens at the call site"
                )
                logger.warning("[openai] %s", truncated)
            else:
                logger.warning("[openai] non-JSON response (first 120 chars: %r)",
                               text[:120])
    return AIResponse(
        text          = text,
        parsed_json   = parsed,
        provider      = "openai",
        model         = model,
        input_tokens  = int(usage.get("prompt_tokens", 0) or 0),
        output_tokens = int(usage.get("completion_tokens", 0) or 0),
        error         = truncated,
    )
