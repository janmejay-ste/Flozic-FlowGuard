"""
GPT-based workflow-prompt generator for flozic.ai entry-point tests.

Given an app slug (e.g. 'google-sheets'), asks GPT to write:
  - a natural-language prompt that a real user might type into the flozic
    'Build my workflow' textarea, featuring that app
  - the expected trigger app name as it should appear on the canvas
  - the expected action app names

The output is consumed by tests/_flozic_common.py, optionally replacing the
hardcoded prompts in tests/flozic_prompts.json. Enable per run by setting:

    FLOZIC_AI_PROMPT=true   ./.venv/Scripts/python.exe -m pytest ...

Environment:
  OPENAI_API_KEY   — required when FLOZIC_AI_PROMPT=true. Without it,
                     generate_prompt() falls back to the hardcoded config.
  OPENAI_MODEL     — optional. Default 'gpt-5.4' (overridable).

JSON contract returned by the model:
  {
    "prompt":         "<one sentence the user might type>",
    "trigger_app":    "<exact app name as flozic shows it>",
    "action_apps":    ["<action 1>", "<action 2>"],
    "reasoning":      "<one-sentence justification of the trigger pick>"
  }
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")
API_BASE = "https://api.openai.com/v1/chat/completions"
REQUEST_TIMEOUT_S = 30

# Cache directory for generated prompts. Each entry is keyed by
# (slug, n, model). Re-runs reuse the cached variants unless
# FLOZIC_REGEN_PROMPTS=true is set. At 27 prompts/run × ~$0.01/each,
# this saves ~$0.27 per re-run.
CACHE_DIR = os.environ.get(
    "FLOZIC_PROMPT_CACHE",
    str(Path(__file__).resolve().parents[1] / "cache" / "ai_prompts"),
)


def _cache_path(slug: str, n: int, model: str) -> Path:
    safe_model = model.replace("/", "_").replace(":", "_")
    return Path(CACHE_DIR) / f"{slug}_{n}_{safe_model}.json"


def _cache_enabled() -> bool:
    """
    Cache policy: fresh prompts every run by DEFAULT — the test framework
    wants new GPT prompts on each run to maximise coverage variety.

    Set FLOZIC_USE_PROMPT_CACHE=true to opt in to caching (saves ~$0.30/run
    by reusing the previously generated variants).

    The legacy FLOZIC_REGEN_PROMPTS=true flag is still respected for
    backward compatibility, but it's now a no-op since regenerate is the
    default. It also forces a fresh batch when caching IS enabled, which
    is useful when you want to bust an opt-in cache.
    """
    use_cache = os.environ.get("FLOZIC_USE_PROMPT_CACHE", "").lower() in ("1", "true", "yes")
    regen     = os.environ.get("FLOZIC_REGEN_PROMPTS", "").lower() in ("1", "true", "yes")
    if regen:
        return False  # explicit bust — never read cache
    return use_cache   # default False -> regenerate each run


def _cache_read(slug: str, n: int, model: str) -> list["GeneratedPrompt"] | None:
    if not _cache_enabled():
        return None
    p = _cache_path(slug, n, model)
    if not p.exists():
        return None
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
        return [GeneratedPrompt.from_dict(d) for d in items]
    except Exception as e:
        logger.warning("[AI-prompt] cache read failed for %s: %s", p, e)
        return None


def _cache_write(slug: str, n: int, model: str, items: list["GeneratedPrompt"]) -> None:
    # Only write to cache when caching is opted-in. Otherwise we'd accumulate
    # stale files that the user has to clean up manually.
    if not _cache_enabled():
        return
    p = _cache_path(slug, n, model)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps([asdict(g) for g in items], indent=2),
            encoding="utf-8",
        )
        logger.info("[AI-prompt] cached %d variants -> %s", len(items), p)
    except Exception as e:
        logger.warning("[AI-prompt] cache write failed for %s: %s", p, e)

# Apps that have NO trigger events — they can only be used as actions.
# Used to coach the model so it picks a realistic trigger app rather than
# one of these (matches the bug we saw with the hardcoded chatgpt prompt).
ACTION_ONLY_APPS = {
    "ChatGPT", "Slack", "Trello", "Google Calendar",
    "HubSpot", "Gmail (send-only)",
}


@dataclass
class GeneratedPrompt:
    prompt: str
    trigger_app: str
    action_apps: list[str]
    reasoning: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GeneratedPrompt":
        return cls(
            prompt=str(d.get("prompt", "")).strip(),
            trigger_app=str(d.get("trigger_app", "")).strip(),
            action_apps=[str(a).strip() for a in d.get("action_apps", [])],
            reasoning=str(d.get("reasoning", "")).strip(),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def is_enabled() -> bool:
    """True if the caller asked for AI-generated prompts via env var."""
    return os.environ.get("FLOZIC_AI_PROMPT", "").lower() in ("1", "true", "yes")


def _build_system_prompt() -> str:
    return (
        "You write realistic workflow descriptions for a no-code automation "
        "platform. A trigger is the event that STARTS the workflow ('when X "
        "happens'). Actions are what happen NEXT. Some apps have no trigger "
        "events (action-only); these include: "
        + ", ".join(sorted(ACTION_ONLY_APPS))
        + ". If the user-named app is action-only, pick a realistic trigger "
        "app and use the user-named app as an action."
    )


def _build_user_prompt(app_name: str, slug: str) -> str:
    return (
        f"Generate a workflow prompt that a real user might type to automate "
        f"something involving the app '{app_name}' (slug: '{slug}').\n\n"
        "Rules:\n"
        "  1. The prompt must read like one natural sentence a person would type.\n"
        "  2. Start with 'When ...' to make the trigger obvious.\n"
        "  3. Mention at least one ACTION after the trigger.\n"
        "  4. Keep it under 200 characters.\n"
        f"  5. {app_name} should appear in the prompt (as trigger or action).\n\n"
        "Reply with a SINGLE JSON object, no other text, with keys:\n"
        "  prompt (string), trigger_app (exact app name as it appears on "
        "automation platforms), action_apps (list of action app names in "
        "execution order), reasoning (one sentence)."
    )


def _slug_to_app_name(slug: str) -> str:
    """Map a URL slug to a human-readable app name (best-effort)."""
    overrides = {
        "gohighlevel":     "GoHighLevel",
        "housecall-pro":   "Housecall Pro",
        "microsoft-excel": "Microsoft Excel",
        "chatgpt":         "ChatGPT",
        "paypal":          "PayPal",
    }
    if slug in overrides:
        return overrides[slug]
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-"))


def generate_prompts(
    slug: str,
    n: int = 5,
    model: str | None = None,
) -> list[GeneratedPrompt]:
    """
    Generate N varied prompts for the given app slug.

    Different from generate_prompt() in two ways:
      1. Asks for N variants in one call (cheaper + more coherent diversity).
      2. The model is explicitly told to vary phrasing, action chains, and
         optional details (timing, filters, error handling).

    Returns an empty list on failure (no API key, network error, parse error).
    Callers can then fall back to the hardcoded single prompt.

    Cached by (slug, n, model) — set FLOZIC_REGEN_PROMPTS=true to bust it.
    """
    use_model = model or DEFAULT_MODEL

    cached = _cache_read(slug, n, use_model)
    if cached:
        logger.info(
            "[AI-prompt] cache HIT for slug=%s n=%d (%d variants); set "
            "FLOZIC_REGEN_PROMPTS=true to regenerate.", slug, n, len(cached),
        )
        return cached

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        import requests  # type: ignore
    except ImportError:
        return []

    app_name = _slug_to_app_name(slug)
    user_msg = (
        f"Generate {n} DIFFERENT workflow prompts that a user might type to "
        f"automate something involving the app '{app_name}'.\n\n"
        "Vary across these dimensions:\n"
        "  - Phrasing style (terse vs verbose, formal vs casual)\n"
        "  - Number of actions (1 action vs 2 vs 3)\n"
        "  - Action apps chosen (don't repeat the same pair every time)\n"
        "  - Optional details (filters like 'only starred', time windows,\n"
        "    error handling like 'if no reply within 24h')\n\n"
        "Rules per prompt:\n"
        "  1. Reads like one natural sentence a person would type.\n"
        "  2. Start with 'When ...' to make the trigger obvious.\n"
        f"  3. {app_name} should appear (as trigger or action).\n"
        "  4. Under 200 characters each.\n\n"
        "Reply with a SINGLE JSON object with key 'prompts' whose value is "
        "an array of N objects, each with: prompt, trigger_app, action_apps, reasoning."
    )

    payload = {
        "model": model or DEFAULT_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user",   "content": user_msg},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    try:
        resp = requests.post(API_BASE, json=payload, headers=headers,
                             timeout=REQUEST_TIMEOUT_S * 2)  # batch -> 60s
    except Exception as e:
        logger.error("[AI-prompt] batch request failed: %s", e)
        return []
    if resp.status_code != 200:
        logger.error("[AI-prompt] batch HTTP %d: %s",
                     resp.status_code, resp.text[:300])
        return []
    try:
        content = resp.json()["choices"][0]["message"]["content"]
        outer   = json.loads(content)
        items   = outer.get("prompts") or outer.get("items") or []
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.error("[AI-prompt] batch parse error: %s", e)
        return []

    results: list[GeneratedPrompt] = []
    dropped: list[tuple[GeneratedPrompt, str]] = []
    app_name = _slug_to_app_name(slug)
    for item in items:
        if not isinstance(item, dict):
            continue
        g = GeneratedPrompt.from_dict(item)
        if not (g.prompt and g.trigger_app):
            continue
        # Coherence check: the slug app must appear SOMEWHERE in the workflow
        # (as trigger OR action). Without this, the model sometimes generates
        # prompts like "Typeform → Gmail → Slack" for the gmail integrations
        # page — incoherent because the user landed on the gmail entry but
        # asked for a Typeform trigger, and flozic's auto-build picks neither
        # cleanly. Reject these so they don't generate noisy test failures.
        in_trigger = app_name.lower() in g.trigger_app.lower()
        in_actions = any(app_name.lower() in (a or "").lower() for a in g.action_apps)
        in_prompt  = app_name.lower() in g.prompt.lower()
        if not (in_trigger or in_actions or in_prompt):
            dropped.append((g, f"slug app '{app_name}' not present in trigger/actions/prompt"))
            continue
        results.append(g)
    for g, reason in dropped[:3]:
        logger.warning("[AI-prompt] dropped incoherent variant for slug=%s: %s | %r",
                       slug, reason, g.prompt)
    logger.info("[AI-prompt] generated %d variants for slug=%s (%d dropped)",
                len(results), slug, len(dropped))
    if results:
        _cache_write(slug, n, use_model, results)
    return results


def generate_prompt(slug: str, model: str | None = None) -> GeneratedPrompt | None:
    """
    Ask GPT to generate a workflow prompt featuring the given app slug.

    Returns a GeneratedPrompt on success; None on any failure (no API key,
    network error, malformed response, etc.). The caller should fall back
    to the hardcoded config in flozic_prompts.json on None.

    Cached by (slug, 1, model) — bust via FLOZIC_REGEN_PROMPTS=true.
    """
    use_model = model or DEFAULT_MODEL
    cached = _cache_read(slug, 1, use_model)
    if cached:
        return cached[0]

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.info(
            "[AI-prompt] OPENAI_API_KEY not set — generator unavailable. "
            "Falling back to hardcoded flozic_prompts.json."
        )
        return None

    try:
        import requests  # type: ignore
    except ImportError:
        logger.error("[AI-prompt] 'requests' not installed.")
        return None

    app_name = _slug_to_app_name(slug)
    payload = {
        "model": model or DEFAULT_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user",   "content": _build_user_prompt(app_name, slug)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }

    try:
        resp = requests.post(API_BASE, json=payload, headers=headers,
                             timeout=REQUEST_TIMEOUT_S)
    except Exception as e:
        logger.error("[AI-prompt] OpenAI request failed: %s", e)
        return None

    if resp.status_code != 200:
        logger.error("[AI-prompt] OpenAI HTTP %d: %s",
                     resp.status_code, resp.text[:300])
        return None

    try:
        content = resp.json()["choices"][0]["message"]["content"]
        parsed  = json.loads(content)
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.error("[AI-prompt] Could not parse OpenAI response: %s", e)
        return None

    gen = GeneratedPrompt.from_dict(parsed)
    if not gen.prompt or not gen.trigger_app:
        logger.error("[AI-prompt] Model returned incomplete object: %s", parsed)
        return None
    _cache_write(slug, 1, use_model, [gen])

    logger.info(
        "[AI-prompt] Generated for slug=%s: trigger=%s, prompt=%r",
        slug, gen.trigger_app, gen.prompt,
    )
    return gen
