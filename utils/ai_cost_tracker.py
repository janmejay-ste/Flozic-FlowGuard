"""
Per-call cost accounting for every LLM request the framework makes.

Every call that flows through `utils/ai_provider.send()` is recorded here:
which module made it, which provider/model served it, how many tokens it
burned, and what that cost in USD. At session end the totals are written to
`reports/trend/run_summary/ai-cost.json` so the dashboard can show spend
alongside pass rate.

Three things this buys us:

  1. **Module attribution** — "the popup validator is 60% of our AI spend"
     is a fact we can act on, not a guess.
  2. **A session cap** — a runaway retry loop in a 46-test suite used to be
     an unbounded bill. `FLOZIC_AI_COST_CAP_USD` stops it.
  3. **Migration safety** — when a model's price changes or we switch
     providers, the delta shows up in one artifact instead of a statement.

Environment:
  FLOZIC_AI_COST_CAP_USD   — session ceiling in USD. Once exceeded, further
                             `send()` calls short-circuit to a SKIPPED-style
                             error instead of hitting the API. Unset = no cap.
  FLOZIC_AI_COST_DISABLED  — set to "1" to turn accounting off entirely.

Unknown models are recorded with `pricing_known: false` and a cost of 0.0.
They still count toward token totals but never toward the cap — better to
under-report than to invent a number and block a run on a fiction.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Bump only with an intentional format change — tests/unit/test_ai_cost.py
# pins this, same contract as the network schema.
SCHEMA_VERSION = 1


# ── Pricing ────────────────────────────────────────────────────────────
#
# USD per 1,000,000 tokens. Source: Anthropic and OpenAI published rates,
# checked 2026-08-07. These are list prices — batch/cache discounts are not
# modelled, so a figure here is an upper bound on actual spend.


@dataclass(frozen=True)
class ModelPrice:
    """Input/output rate per 1M tokens, with an optional promo window."""
    input_per_mtok:  float
    output_per_mtok: float
    # Introductory pricing that reverts on `intro_until` (inclusive).
    intro_input_per_mtok:  float | None = None
    intro_output_per_mtok: float | None = None
    intro_until:           date | None = None

    def rates_on(self, on: date) -> tuple[float, float]:
        """Effective (input, output) rate for a given date."""
        if (
            self.intro_until is not None
            and self.intro_input_per_mtok is not None
            and self.intro_output_per_mtok is not None
            and on <= self.intro_until
        ):
            return self.intro_input_per_mtok, self.intro_output_per_mtok
        return self.input_per_mtok, self.output_per_mtok


PRICING: dict[str, ModelPrice] = {
    # ── Anthropic ──
    "claude-fable-5":    ModelPrice(10.00, 50.00),
    "claude-opus-5":     ModelPrice(5.00,  25.00),
    "claude-opus-4-8":   ModelPrice(5.00,  25.00),
    "claude-opus-4-7":   ModelPrice(5.00,  25.00),
    "claude-opus-4-6":   ModelPrice(5.00,  25.00),
    # Sonnet 5 launched on introductory pricing that reverts after
    # 2026-08-31. Both rates are encoded so a run in September doesn't
    # silently keep quoting the promo number.
    "claude-sonnet-5":   ModelPrice(
        3.00, 15.00,
        intro_input_per_mtok=2.00, intro_output_per_mtok=10.00,
        intro_until=date(2026, 8, 31),
    ),
    "claude-sonnet-4-6": ModelPrice(3.00,  15.00),
    "claude-haiku-4-5":  ModelPrice(1.00,   5.00),
    # ── OpenAI (legacy provider — kept so cross-provider runs are comparable) ──
    "gpt-4o":            ModelPrice(2.50,  10.00),
    "gpt-4o-mini":       ModelPrice(0.15,   0.60),
}


def _normalize_model(model: str) -> str:
    """Strip a dated snapshot suffix so `claude-haiku-4-5-20251001` prices
    the same as `claude-haiku-4-5`."""
    m = (model or "").strip()
    if m in PRICING:
        return m
    # Longest-prefix match handles dated variants without a second table.
    for known in sorted(PRICING, key=len, reverse=True):
        if m.startswith(known):
            return known
    return m


def price_call(model: str, input_tokens: int, output_tokens: int,
               on: date | None = None) -> tuple[float, bool]:
    """
    Return `(usd, pricing_known)` for a single call.

    `pricing_known` is False for models absent from PRICING — the caller
    should surface that rather than treat 0.0 as a real number.
    """
    key = _normalize_model(model)
    entry = PRICING.get(key)
    if entry is None:
        return 0.0, False
    in_rate, out_rate = entry.rates_on(on or date.today())
    usd = (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
    return round(usd, 6), True


# ── Records ────────────────────────────────────────────────────────────


@dataclass
class CallCost:
    """One LLM call's cost line."""
    module:        str        # "ai_popup_validator" — who spent it
    provider:      str        # "claude" | "openai" | "noop"
    model:         str
    input_tokens:  int
    output_tokens: int
    usd:           float
    pricing_known: bool
    error:         str | None = None   # set when the call itself failed


@dataclass
class _Totals:
    calls:         int = 0
    input_tokens:  int = 0
    output_tokens: int = 0
    usd:           float = 0.0
    errors:        int = 0


class CostTracker:
    """
    Session-scoped accumulator. One instance per pytest run, reachable via
    the module-level `TRACKER`. Thread-safe because pytest-xdist and any
    future parallel validator calls can record concurrently.
    """

    def __init__(self, cap_usd: float | None = None) -> None:
        self._lock = threading.Lock()
        self._calls: list[CallCost] = []
        self._by_module: dict[str, _Totals] = {}
        self._session = _Totals()
        self._cap_usd = cap_usd
        self._cap_tripped = False

    # ── Recording ──

    def record(
        self,
        *,
        module: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        error: str | None = None,
        on: date | None = None,
    ) -> CallCost:
        usd, known = price_call(model, input_tokens, output_tokens, on=on)
        call = CallCost(
            module=module, provider=provider, model=model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            usd=usd, pricing_known=known, error=error,
        )
        with self._lock:
            self._calls.append(call)
            for bucket in (self._session, self._by_module.setdefault(module, _Totals())):
                bucket.calls         += 1
                bucket.input_tokens  += call.input_tokens
                bucket.output_tokens += call.output_tokens
                bucket.usd            = round(bucket.usd + usd, 6)
                if error:
                    bucket.errors += 1
            if not known and model:
                logger.warning(
                    "[cost] No pricing entry for model %r — tokens counted, "
                    "cost recorded as $0. Add it to utils/ai_cost_tracker.PRICING.",
                    model,
                )
        return call

    # ── Budget ──

    @property
    def cap_usd(self) -> float | None:
        return self._cap_usd

    def spent_usd(self) -> float:
        with self._lock:
            return round(self._session.usd, 6)

    def over_budget(self) -> bool:
        """True once the session cap is exhausted. Logs the trip exactly once
        so a capped run produces one clear line, not one per remaining call."""
        if self._cap_usd is None:
            return False
        with self._lock:
            if self._session.usd < self._cap_usd:
                return False
            if not self._cap_tripped:
                self._cap_tripped = True
                logger.error(
                    "[cost] Session AI budget of $%.2f exhausted (spent $%.4f "
                    "across %d calls). Further AI calls are skipped; tests "
                    "continue with keyword-only fallbacks.",
                    self._cap_usd, self._session.usd, self._session.calls,
                )
            return True

    # ── Reporting ──

    def summary(self) -> dict[str, Any]:
        with self._lock:
            by_module = {
                name: {
                    "calls":         t.calls,
                    "input_tokens":  t.input_tokens,
                    "output_tokens": t.output_tokens,
                    "usd":           round(t.usd, 6),
                    "errors":        t.errors,
                }
                for name, t in sorted(
                    self._by_module.items(), key=lambda kv: -kv[1].usd
                )
            }
            unpriced = sorted({c.model for c in self._calls if not c.pricing_known and c.model})
            return {
                "schema_version": SCHEMA_VERSION,
                "generated_at":   datetime.now(timezone.utc).isoformat(),
                "cap_usd":        self._cap_usd,
                "cap_exceeded":   self._cap_tripped,
                "session": {
                    "calls":         self._session.calls,
                    "input_tokens":  self._session.input_tokens,
                    "output_tokens": self._session.output_tokens,
                    "usd":           round(self._session.usd, 6),
                    "errors":        self._session.errors,
                },
                "by_module":        by_module,
                "unpriced_models":  unpriced,
                "calls":            [asdict(c) for c in self._calls],
            }

    def export_to(self, directory: Path) -> Path:
        """Write `ai-cost.json` into `directory`. Returns the path."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "ai-cost.json"
        path.write_text(
            json.dumps(self.summary(), indent=2) + "\n", encoding="utf-8"
        )
        return path

    def reset(self) -> None:
        """Test-support hook — clears all accumulated state."""
        with self._lock:
            self._calls.clear()
            self._by_module.clear()
            self._session = _Totals()
            self._cap_tripped = False


# ── Module-level session tracker ───────────────────────────────────────


def _cap_from_env() -> float | None:
    raw = os.environ.get("FLOZIC_AI_COST_CAP_USD", "").strip()
    if not raw:
        return None
    try:
        cap = float(raw)
    except ValueError:
        logger.warning("[cost] FLOZIC_AI_COST_CAP_USD=%r is not a number — ignored.", raw)
        return None
    if cap <= 0:
        logger.warning("[cost] FLOZIC_AI_COST_CAP_USD=%s must be > 0 — ignored.", cap)
        return None
    return cap


ENABLED = os.environ.get("FLOZIC_AI_COST_DISABLED", "").strip() != "1"
TRACKER = CostTracker(cap_usd=_cap_from_env())


def record(**kwargs: Any) -> CallCost | None:
    """Convenience wrapper — no-op when accounting is disabled."""
    if not ENABLED:
        return None
    return TRACKER.record(**kwargs)


def over_budget() -> bool:
    return ENABLED and TRACKER.over_budget()


def export_to(directory: Path) -> Path:
    return TRACKER.export_to(directory)
