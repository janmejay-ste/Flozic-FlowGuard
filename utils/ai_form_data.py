"""
GPT-generated synthetic form data, with schema validation BEFORE caching.

Why this module exists:
  Form tests often need many varied valid inputs (50 emails, 50 phone
  numbers, 50 names) to exercise edge cases — Unicode names, country-code
  variations, plus-addressing in emails, etc. Hand-writing 50 rows is
  tedious; GPT does it in one call. But unvalidated GPT output is a
  liability: a single {"email": "abc"} row that gets cached will waste
  hours of debugging when a test fails 3 weeks later.

  So this module validates EVERY row against a typed schema BEFORE caching.
  Invalid rows are discarded with a log line. If too many rows are bad,
  the cache write is skipped entirely — better to regenerate than to
  poison future runs.

Validators are pure-Python regexes/callables; no pydantic dep required.

Usage:
    from utils.ai_form_data import generate_form_data, BUILTIN_VALIDATORS

    spec = {
        "email":       "email",                # built-in name → built-in validator
        "phone":       "phone_intl",
        "first_name":  "non_empty_str",
        "age":         {"type": "int", "min": 18, "max": 95},
        "vat_number":  custom_validator_fn,    # callable: (value) -> bool
    }
    rows = generate_form_data(spec, n=50)
    # rows is list[dict]; every row passed validation; bad rows already filtered.

If OPENAI_API_KEY is not set, returns the cached version if present, else [].
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")
API_BASE = "https://api.openai.com/v1/chat/completions"
REQUEST_TIMEOUT_S = 60

# Cache root — alongside the AI prompt cache, gitignored.
CACHE_DIR = Path(__file__).resolve().parents[1] / "cache" / "synthetic_data"

# Fraction of rows that must pass validation for the batch to be cached.
# Below this we discard and (optionally) retry — never poison the cache.
MIN_VALID_FRACTION = 0.70

# Max retries when validation rate is too low. One retry with a stricter
# prompt is usually enough; more is rarely worth the cost.
MAX_RETRIES = 1


# ─────────────────────────────────────────────────────────────────────────────
# Built-in validators
# Keep these conservative — better to reject a borderline row than to
# accept garbage that breaks a test 3 weeks from now.
# ─────────────────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)
# Loose international phone — allows +, digits, spaces, dashes, parentheses,
# 7-20 digits total. Strict enough to reject "abc" but tolerant of formats.
_PHONE_RE = re.compile(r"^[+\d\s\-\(\)]{7,32}$")
_URL_RE   = re.compile(r"^https?://[^\s]{3,}$")
_UUID_RE  = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_email(v: Any) -> bool:
    return isinstance(v, str) and bool(_EMAIL_RE.match(v)) and "@" in v


def _is_phone_intl(v: Any) -> bool:
    if not isinstance(v, str) or not _PHONE_RE.match(v):
        return False
    # Must contain at least 7 digits (otherwise "+()-" would pass)
    return sum(c.isdigit() for c in v) >= 7


def _is_url(v: Any) -> bool:
    return isinstance(v, str) and bool(_URL_RE.match(v))


def _is_uuid(v: Any) -> bool:
    return isinstance(v, str) and bool(_UUID_RE.match(v))


def _is_non_empty_str(v: Any) -> bool:
    return isinstance(v, str) and len(v.strip()) > 0


def _is_name(v: Any) -> bool:
    """At least 2 chars, must contain at least one letter, max 80 chars."""
    if not isinstance(v, str):
        return False
    s = v.strip()
    return 2 <= len(s) <= 80 and any(c.isalpha() for c in s)


def _is_int(v: Any) -> bool:
    # bool is subclass of int in Python; explicitly exclude.
    return isinstance(v, int) and not isinstance(v, bool)


def _is_positive_int(v: Any) -> bool:
    return _is_int(v) and v > 0


def _is_iso_date(v: Any) -> bool:
    return (isinstance(v, str)
            and bool(re.match(r"^\d{4}-\d{2}-\d{2}$", v)))


def _is_country_code(v: Any) -> bool:
    """ISO 3166-1 alpha-2 (2 letters) — coarse check, not full lookup."""
    return isinstance(v, str) and len(v) == 2 and v.isalpha() and v.isupper()


BUILTIN_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "email":          _is_email,
    "phone_intl":     _is_phone_intl,
    "url":            _is_url,
    "uuid":           _is_uuid,
    "non_empty_str":  _is_non_empty_str,
    "name":           _is_name,
    "int":            _is_int,
    "positive_int":   _is_positive_int,
    "iso_date":       _is_iso_date,
    "country_code":   _is_country_code,
}


# ─────────────────────────────────────────────────────────────────────────────
# Schema resolution
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ResolvedField:
    """A field spec after schema resolution. The validator is always callable."""
    name: str
    description: str               # human-readable, sent to GPT
    validator: Callable[[Any], bool]

    # Optional bounds for {"type": "int", "min": ..., "max": ...} specs.
    # Combined with validator into a single check at validation time.
    min: float | None = None
    max: float | None = None

    def validate(self, value: Any) -> bool:
        if not self.validator(value):
            return False
        if self.min is not None and isinstance(value, (int, float)):
            if value < self.min:
                return False
        if self.max is not None and isinstance(value, (int, float)):
            if value > self.max:
                return False
        return True


def _resolve_field(name: str, raw: Any) -> ResolvedField:
    """
    Resolve one entry in the user's spec dict into a ResolvedField.

    Accepts:
      - str            → name of a built-in validator (e.g. "email")
      - callable       → custom validator function
      - dict with keys:
            type        → built-in name (required)
            description → optional human-readable description for GPT
            min, max    → optional numeric bounds
    """
    if isinstance(raw, str):
        if raw not in BUILTIN_VALIDATORS:
            raise ValueError(
                f"Field '{name}': unknown built-in validator '{raw}'. "
                f"Known: {sorted(BUILTIN_VALIDATORS.keys())}"
            )
        return ResolvedField(
            name=name, description=raw, validator=BUILTIN_VALIDATORS[raw],
        )
    if callable(raw):
        return ResolvedField(
            name=name, description=getattr(raw, "__doc__", "") or "custom",
            validator=raw,
        )
    if isinstance(raw, dict):
        type_name = raw.get("type")
        if type_name not in BUILTIN_VALIDATORS:
            raise ValueError(
                f"Field '{name}': dict spec missing valid 'type'. Got {raw!r}."
            )
        desc = raw.get("description") or type_name
        return ResolvedField(
            name=name,
            description=desc,
            validator=BUILTIN_VALIDATORS[type_name],
            min=raw.get("min"),
            max=raw.get("max"),
        )
    raise ValueError(f"Field '{name}': unsupported spec type {type(raw).__name__}")


def _resolve_spec(spec: dict[str, Any]) -> list[ResolvedField]:
    return [_resolve_field(name, raw) for name, raw in spec.items()]


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────

def _cache_key(spec: dict[str, Any], n: int, model: str) -> str:
    """
    Hash the spec + n + model into a deterministic filename.

    Callables are hashed by qualified name so a renamed validator busts the
    cache (intentional — different validator may mean different behaviour).
    """
    def _norm(v: Any) -> Any:
        if callable(v):
            return f"<callable:{getattr(v, '__qualname__', repr(v))}>"
        return v

    canonical = json.dumps(
        {k: _norm(v) for k, v in spec.items()},
        sort_keys=True,
        default=str,
    )
    h = hashlib.sha256(
        f"{canonical}|n={n}|model={model}".encode("utf-8")
    ).hexdigest()[:16]
    return h


def _cache_path(spec: dict[str, Any], n: int, model: str) -> Path:
    return CACHE_DIR / f"{_cache_key(spec, n, model)}.json"


def _cache_read(spec: dict, n: int, model: str) -> list[dict] | None:
    if os.environ.get("REGEN_SYNTHETIC_DATA", "").lower() in ("1", "true", "yes"):
        return None
    p = _cache_path(spec, n, model)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[synthetic] cache read failed for %s: %s", p, e)
        return None


def _cache_write(spec: dict, n: int, model: str, rows: list[dict]) -> None:
    p = _cache_path(spec, n, model)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        logger.info("[synthetic] cached %d rows -> %s", len(rows), p)
    except Exception as e:
        logger.warning("[synthetic] cache write failed for %s: %s", p, e)


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationReport:
    accepted: list[dict] = field(default_factory=list)
    rejected: list[tuple[dict, str]] = field(default_factory=list)  # (row, reason)

    @property
    def accept_rate(self) -> float:
        total = len(self.accepted) + len(self.rejected)
        return (len(self.accepted) / total) if total else 0.0


def _validate_rows(
    rows: list[Any],
    fields: list[ResolvedField],
) -> ValidationReport:
    """Apply each field's validator to every row. Returns accepted/rejected."""
    report = ValidationReport()
    for row in rows:
        if not isinstance(row, dict):
            report.rejected.append(({"_row": row}, "not a dict"))
            continue
        bad_field: str | None = None
        for f in fields:
            if f.name not in row:
                bad_field = f"{f.name}: missing"
                break
            if not f.validate(row[f.name]):
                bad_field = f"{f.name}={row[f.name]!r}: failed {f.description}"
                break
        if bad_field:
            report.rejected.append((row, bad_field))
        else:
            report.accepted.append(row)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI call
# ─────────────────────────────────────────────────────────────────────────────

def _gpt_generate(
    fields: list[ResolvedField], n: int, model: str,
    strict_retry: bool = False,
) -> list[Any]:
    """Send the field spec to GPT, return parsed rows array. [] on failure."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        import requests  # type: ignore
    except ImportError:
        return []

    schema_text = "\n".join(
        f"  - {f.name}: {f.description}"
        + (f" (min={f.min})" if f.min is not None else "")
        + (f" (max={f.max})" if f.max is not None else "")
        for f in fields
    )

    sys_msg = (
        "You generate realistic, varied test data rows for QA automation. "
        "Output strictly conforms to the schema. Cover edge cases the "
        "developer might miss (Unicode names with diacritics, plus-addressed "
        "emails, international phone formats, mixed case, etc.) — but every "
        "value MUST still pass typical real-world validators."
    )
    if strict_retry:
        sys_msg += (
            " STRICTER MODE: a previous batch had too many invalid rows. "
            "Be conservative with formats — prefer clearly-valid values over "
            "edge cases. Validate each row mentally before emitting."
        )

    user_msg = (
        f"Generate exactly {n} synthetic test rows matching this schema:\n\n"
        f"{schema_text}\n\n"
        "Vary values across rows (no duplicates unless realistic). Reply "
        "with a SINGLE JSON object having key 'rows' whose value is an "
        "array of exactly {n} objects, each with the schema's field names "
        "as keys.".replace("{n}", str(n))
    )

    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user",   "content": user_msg},
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
        logger.error("[synthetic] OpenAI request failed: %s", e)
        return []
    if resp.status_code != 200:
        logger.error("[synthetic] OpenAI HTTP %d: %s",
                     resp.status_code, resp.text[:300])
        return []

    try:
        outer = json.loads(resp.json()["choices"][0]["message"]["content"])
        rows  = outer.get("rows") or outer.get("data") or outer.get("items") or []
        return rows if isinstance(rows, list) else []
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.error("[synthetic] parse error: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def generate_form_data(
    spec: dict[str, Any],
    n: int = 50,
    model: str | None = None,
    min_valid_fraction: float = MIN_VALID_FRACTION,
) -> list[dict]:
    """
    Generate `n` synthetic form-data rows matching `spec`.

    Returns a list of validated rows (may be smaller than `n` if some were
    rejected — but every returned row passed validation).

    Flow:
      1. Resolve spec → typed validators
      2. Check cache; return cached rows if present (bust with
         REGEN_SYNTHETIC_DATA=true)
      3. Call GPT
      4. Validate every returned row against the schema
      5. If accept-rate < min_valid_fraction, retry ONCE with stricter prompt
      6. If still bad, return what we have but DON'T cache (poisoning prevention)
      7. Otherwise cache and return
    """
    fields = _resolve_spec(spec)
    use_model = model or DEFAULT_MODEL

    cached = _cache_read(spec, n, use_model)
    if cached is not None:
        logger.info(
            "[synthetic] cache HIT for %d rows (%d fields). Set "
            "REGEN_SYNTHETIC_DATA=true to regenerate.",
            len(cached), len(fields),
        )
        return cached

    # First attempt
    raw_rows = _gpt_generate(fields, n, use_model, strict_retry=False)
    report   = _validate_rows(raw_rows, fields)
    logger.info(
        "[synthetic] attempt 1: %d accepted, %d rejected (rate=%.2f)",
        len(report.accepted), len(report.rejected), report.accept_rate,
    )

    # Retry once if we're below threshold and have an API key (i.e. attempt 1
    # actually ran). Don't retry if attempt 1 returned 0 rows from a missing
    # API key — that path returns the same empty list with no progress.
    attempts = 1
    while (raw_rows and report.accept_rate < min_valid_fraction
           and attempts <= MAX_RETRIES):
        attempts += 1
        logger.info(
            "[synthetic] accept rate %.2f below threshold %.2f — retrying "
            "with strict mode.",
            report.accept_rate, min_valid_fraction,
        )
        raw_rows = _gpt_generate(fields, n, use_model, strict_retry=True)
        report   = _validate_rows(raw_rows, fields)
        logger.info(
            "[synthetic] attempt %d: %d accepted, %d rejected (rate=%.2f)",
            attempts, len(report.accepted), len(report.rejected),
            report.accept_rate,
        )

    # Log a sample of rejections — useful for tuning validators / prompts.
    for row, reason in report.rejected[:5]:
        logger.warning("[synthetic] rejected: %s | row=%s", reason, row)

    if not report.accepted:
        logger.error("[synthetic] all rows rejected — returning [] (no cache)")
        return []

    # The user's key rule: validate FIRST, cache SECOND. Only write the
    # cache if the accept rate cleared the threshold AFTER (possibly) a
    # retry — otherwise the cache could lock in a bad batch.
    if report.accept_rate >= min_valid_fraction:
        _cache_write(spec, n, use_model, report.accepted)
    else:
        logger.warning(
            "[synthetic] accept rate %.2f still below threshold %.2f after "
            "%d attempt(s) — returning rows but NOT caching to avoid "
            "poisoning future runs.",
            report.accept_rate, min_valid_fraction, attempts,
        )
    return report.accepted
