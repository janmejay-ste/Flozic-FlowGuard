"""
Tolerant JSON parsing for LLM responses, plus retry-on-malformed.

Every `ai_*.py` module used to carry its own `json.loads(content)` inside a
try/except that degraded the whole verdict to ERROR on a stray markdown
fence. That is a lot of lost signal for a formatting slip, and the recovery
logic had drifted into three slightly different shapes.

This module centralizes two things:

  `parse_json(text)`   — best-effort structured parse. Strips fences, pulls
                         the first balanced JSON value out of surrounding
                         prose, and repairs the handful of malformations
                         models actually emit (trailing commas, smart quotes,
                         Python literals). Returns None only when there is
                         genuinely no JSON in there.

  `send_json(prompt)`  — `ai_provider.send()` + `parse_json()`, and on a
                         parse miss, one corrective retry that shows the model
                         its own malformed output and asks for raw JSON.

Repair is deliberately conservative. Every transform is one a human reading
the output would call obviously-intended; none of them guess at *values*.
A response we can't parse stays unparsed rather than becoming a plausible
wrong verdict — a wrong verdict is worse than a skipped one, because it
lands on the dashboard as a product finding.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# How many corrective retries send_json() will attempt after a parse miss.
DEFAULT_JSON_RETRIES = 1


# ── Fence + prose stripping ────────────────────────────────────────────


def strip_json_fences(text: str) -> str:
    """
    Remove ```json ... ``` fences that models emit even when asked for raw
    JSON. Idempotent on already-clean strings.
    """
    s = (text or "").strip()
    if s.startswith("```"):
        newline = s.find("\n")
        s = s[newline + 1:] if newline != -1 else s[3:]
    if s.endswith("```"):
        s = s[:-3].rstrip()
    return s.strip()


def extract_json_span(text: str) -> str | None:
    """
    Pull the first balanced JSON object or array out of `text`, ignoring any
    prose wrapped around it ("Sure! Here's the verdict: {...} Let me know...").

    Brace-counting is quote-aware and escape-aware, so a `}` inside a string
    value doesn't end the span early.
    """
    s = text or ""
    start = None
    for i, ch in enumerate(s):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None

    opener = s[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None   # unbalanced — truncated response, usually a max_tokens cut


# ── Repair ─────────────────────────────────────────────────────────────

_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_SMART_QUOTES = str.maketrans({
    "“": '"', "”": '"',    # curly double quotes
    "‘": "'", "’": "'",    # curly single quotes
})


def repair_json(text: str) -> str:
    """
    Apply the conservative repairs that recover real-world model output.
    Each is safe on already-valid JSON, so this can run unconditionally.
    """
    s = (text or "").translate(_SMART_QUOTES)
    # Python literals — models slip into them when the prompt reads Pythonic.
    s = re.sub(r"\bTrue\b",  "true",  s)
    s = re.sub(r"\bFalse\b", "false", s)
    s = re.sub(r"\bNone\b",  "null",  s)
    s = re.sub(r"\bNaN\b",   "null",  s)
    # Trailing commas before a closing brace/bracket.
    s = _TRAILING_COMMA.sub(r"\1", s)
    return s.strip()


def parse_json(text: str) -> dict[str, Any] | list[Any] | None:
    """
    Best-effort parse of a model response into JSON.

    Tries, in order: the raw text, the de-fenced text, the first balanced
    JSON span, and finally that span with repairs applied. Returns None when
    none of those yield valid JSON.
    """
    if not text or not text.strip():
        return None

    candidates: list[str] = []
    raw = text.strip()
    candidates.append(raw)

    defenced = strip_json_fences(raw)
    if defenced != raw:
        candidates.append(defenced)

    span = extract_json_span(defenced)
    if span and span not in candidates:
        candidates.append(span)

    # Repairs go last so a response that parses cleanly is never rewritten.
    for base in list(candidates):
        repaired = repair_json(base)
        if repaired not in candidates:
            candidates.append(repaired)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


# ── send + parse + retry ───────────────────────────────────────────────


_RETRY_INSTRUCTION = (
    "\n\nYour previous reply could not be parsed as JSON. Reply again with "
    "ONE valid JSON object and nothing else — no markdown fences, no prose "
    "before or after, no trailing commas. Your previous reply was:\n"
    "-----\n{previous}\n-----"
)


def send_json(
    prompt: str,
    *,
    module: str,
    model: str | None = None,
    image_paths: list[Path] | None = None,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    system_prompt: str | None = None,
    retries: int = DEFAULT_JSON_RETRIES,
) -> tuple[dict[str, Any] | list[Any] | None, Any]:
    """
    Send `prompt` and return `(parsed_json_or_None, AIResponse)`.

    On a parse miss the model is shown its own malformed output and asked
    once (by default) for clean JSON. Transport errors are NOT retried here
    — the SDK already retries those, and a second full call on a 401 just
    doubles the latency of a failure we already understand.

    The returned AIResponse is always the last one received, so callers can
    surface `.error` / `.provider` in their verdict regardless of outcome.
    """
    from utils.ai_provider import send as ai_send

    attempt_prompt = prompt
    resp = None

    for attempt in range(retries + 1):
        resp = ai_send(
            attempt_prompt,
            model=model,
            image_paths=image_paths,
            response_json=True,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            module=module,
        )
        if resp.error:
            return None, resp

        # The provider already attempts a parse; trust it when it succeeded.
        parsed = resp.parsed_json
        if parsed is None:
            parsed = parse_json(resp.text)
            if parsed is not None:
                logger.info(
                    "[%s] Recovered JSON from a malformed %s response via repair.",
                    module, resp.provider,
                )
        if parsed is not None:
            return parsed, resp

        if attempt < retries:
            logger.warning(
                "[%s] %s returned unparseable JSON — retrying (%d/%d). "
                "First 200 chars: %r",
                module, resp.provider, attempt + 1, retries, (resp.text or "")[:200],
            )
            attempt_prompt = prompt + _RETRY_INSTRUCTION.format(
                previous=(resp.text or "")[:1000]
            )

    logger.error(
        "[%s] %s returned unparseable JSON after %d attempt(s).",
        module, resp.provider if resp else "unknown", retries + 1,
    )
    return None, resp


def send_text(
    prompt: str,
    *,
    module: str,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    system_prompt: str | None = None,
) -> Any:
    """
    Plain-prose counterpart to `send_json()` for callers that want narrative
    output (executive summaries, bug write-ups). Returns the AIResponse.
    """
    from utils.ai_provider import send as ai_send

    return ai_send(
        prompt,
        model=model,
        response_json=False,
        temperature=temperature,
        max_tokens=max_tokens,
        system_prompt=system_prompt,
        module=module,
    )
