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
"""

from __future__ import annotations

import base64
import json
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
) -> AIResponse:
    """
    Send a prompt to the selected provider. Returns AIResponse.

    Callers should not need to know which provider is in use. When adding
    a new ai_*.py module, use `send()` — never import provider modules
    directly. This keeps future model migrations to one place.
    """
    if PROVIDER_NAME == "claude":
        return _send_claude(
            prompt, model=model, image_paths=image_paths,
            response_json=response_json, temperature=temperature,
            max_tokens=max_tokens, system_prompt=system_prompt,
        )
    if PROVIDER_NAME == "openai":
        return _send_openai(
            prompt, model=model, image_paths=image_paths,
            response_json=response_json, temperature=temperature,
            max_tokens=max_tokens, system_prompt=system_prompt,
        )
    return AIResponse(provider="noop", error="No AI provider configured.")


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
    parsed = None
    if response_json:
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError) as e:
            logger.warning("[openai] non-JSON response: %s (first 120 chars: %r)",
                           e, text[:120])
    return AIResponse(
        text          = text,
        parsed_json   = parsed,
        provider      = "openai",
        model         = model,
        input_tokens  = int(usage.get("prompt_tokens", 0) or 0),
        output_tokens = int(usage.get("completion_tokens", 0) or 0),
    )
