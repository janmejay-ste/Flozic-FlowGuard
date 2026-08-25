"""
GPT-powered Page Object scaffold generator.

Given an HTML page (or live URL), extracts only the interactive elements
with their stable attributes (role, name, label, placeholder, id, data-*),
then asks GPT to draft a Python Page Object using Playwright. Strict
guardrails are baked into the system prompt to keep selectors stable:

  - Prefer page.get_by_role(...) and page.get_by_text(...)
  - Then attribute-based locators (input[name='email'])
  - NEVER use CSS class selectors (they break on every deploy)
  - NEVER use :nth-child / :nth-of-type
  - NEVER use deep '>' chains beyond depth 1
  - Every interaction method waits for visibility first

Output is written to `pages/<snake_case>_page.new.py` so it cannot
overwrite an existing reviewed page object. Human review is required
before promoting `.new.py` → `.py`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MODULE = "ai_page_object"


# Hard cap on the DOM snippet sent to GPT. Keeps token cost bounded on
# very large pages.
MAX_DOM_BYTES = 30_000


# ─────────────────────────────────────────────────────────────────────────────
# DOM excerpt extraction — keep ONLY interactive elements and their stable
# attributes. We deliberately strip class= and inline styles so GPT can't
# anchor on them. This is the single biggest defence against brittle output.
# ─────────────────────────────────────────────────────────────────────────────

INTERACTIVE_TAGS = (
    "button", "a", "input", "select", "textarea",
    "label", "form", "h1", "h2", "h3",
)

# Attributes we KEEP (stable). Everything else is stripped.
STABLE_ATTRS = (
    "id", "name", "type", "role", "aria-label", "aria-labelledby",
    "placeholder", "title", "alt", "value", "href",
    "for", "data-track", "data-testid", "data-test", "data-qa",
)


def extract_dom_excerpt(html: str) -> str:
    """
    Return a compact DOM excerpt containing only interactive elements with
    only their stable attributes. Class names and inline styles are removed
    so the generator can't anchor on them.

    This is a regex-based passthrough — good enough for scaffolding even on
    malformed HTML. We don't build a full DOM tree.
    """
    if not html:
        return ""

    keep_lines: list[str] = []
    # Match each opening tag (optionally self-closing) of an interactive type.
    tag_pattern = re.compile(
        r"<\s*(" + "|".join(INTERACTIVE_TAGS) + r")\b([^>]*?)/?>",
        re.IGNORECASE,
    )
    # Also keep nearest text content so the model knows the button label.
    # We just emit the tag and let the text-between-tags appear later in
    # sequence — simpler than full parsing.
    text_pattern = re.compile(r">([^<]{2,200})<", re.IGNORECASE)

    # Emit interactive tags
    for m in tag_pattern.finditer(html):
        tag = m.group(1).lower()
        raw_attrs = m.group(2) or ""
        kept = []
        for attr in STABLE_ATTRS:
            # Match attr="value" or attr='value' (case-insensitive)
            am = re.search(
                rf'\b{re.escape(attr)}\s*=\s*"([^"]*)"',
                raw_attrs, re.IGNORECASE,
            ) or re.search(
                rf"\b{re.escape(attr)}\s*=\s*'([^']*)'",
                raw_attrs, re.IGNORECASE,
            )
            if am:
                kept.append(f'{attr}="{am.group(1)}"')
        kept_str = (" " + " ".join(kept)) if kept else ""
        keep_lines.append(f"<{tag}{kept_str}>")

    # Append a sample of visible text snippets so labels survive
    texts = []
    seen = set()
    for m in text_pattern.finditer(html):
        t = m.group(1).strip()
        if not t or len(t) < 3:
            continue
        # Skip JS/CSS leftovers
        if any(c in t for c in (";", "{", "}", "function", "var ")):
            continue
        if t in seen:
            continue
        seen.add(t)
        texts.append(t)
        if len(texts) >= 80:
            break

    out = "\n".join(keep_lines)
    if texts:
        out += "\n\n<!-- Visible text snippets (preserve as locator targets):\n"
        out += "\n".join(f"  - {t!r}" for t in texts[:60])
        out += "\n-->"

    # Cap size
    if len(out) > MAX_DOM_BYTES:
        out = out[:MAX_DOM_BYTES] + "\n<<TRUNCATED>>"
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Result + GPT call
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GeneratedPageObject:
    status: str                              # "GENERATED" | "SKIPPED" | "ERROR"
    code: str = ""
    review_checklist: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    error: str | None = None


def _system_prompt() -> str:
    return (
        "You are a senior Playwright Python test engineer drafting a Page "
        "Object scaffold. Your output will be saved with a .new.py suffix "
        "and reviewed by a human BEFORE promotion to production. Be "
        "conservative — better to leave a stub than to invent a brittle "
        "locator.\n\n"
        "HARD RULES (never violate):\n"
        "  1. NEVER use CSS class selectors. Class names are unstable on "
        "     every framework deploy.\n"
        "  2. NEVER use :nth-child, :nth-of-type, or positional selectors.\n"
        "  3. NEVER use deep CSS chains. Max one descendant combinator.\n"
        "  4. PREFER, in this order: page.get_by_role(role, name='...'), "
        "     page.get_by_label('...'), page.get_by_placeholder('...'), "
        "     page.get_by_text('...', exact=True), "
        "     attribute locators like \"input[name='email']\".\n"
        "  5. Every interaction method MUST wait for visibility first "
        "     (locator.wait_for(state='visible', timeout=...)).\n"
        "  6. Use module-level constants for locator strings when reused.\n"
        "  7. Each method docstring states the precondition and "
        "     postcondition in one line.\n"
        "  8. NO Thread.sleep, NO page.wait_for_timeout > 500ms.\n"
        "  9. Constructor takes only (self, page: Page).\n"
        " 10. If you can't find a stable locator for an element, OMIT the "
        "     corresponding method and add a TODO comment with the element "
        "     description — DO NOT guess.\n"
    )


def _user_prompt(class_name: str, dom_excerpt: str, source: str) -> str:
    return (
        f"Draft a Python Page Object named `{class_name}` for the page "
        f"sourced from: {source}\n\n"
        "Only the interactive elements + visible text snippets are shown "
        "below (class names and inline styles were stripped before sending "
        "— do not invent any).\n\n"
        "DOM excerpt:\n"
        "----BEGIN DOM----\n"
        f"{dom_excerpt}\n"
        "----END DOM----\n\n"
        "Reply with a SINGLE JSON object, no other text, with keys:\n"
        "  code (the full .py file content as a string), \n"
        "  review_checklist (array of 3-7 strings — specific items a human "
        "should verify before promoting this page object).\n\n"
        "Constraints:\n"
        "  - File must start with a triple-quoted module docstring naming "
        "    the source and saying 'AI-generated scaffold — review before "
        "    promoting'.\n"
        "  - `from playwright.sync_api import Page` must be imported.\n"
        f"  - Define `class {class_name}:` with `def __init__(self, page: Page)`.\n"
        "  - Methods are short — one logical action each.\n"
        "  - Include a comment block at the BOTTOM listing every element "
        "    you saw in the DOM but DECLINED to wrap (and why)."
    )


def generate_page_object(
    class_name: str,
    dom_html: str,
    source_label: str = "(provided HTML)",
    model: str | None = None,
) -> GeneratedPageObject:
    """
    Send a DOM excerpt to the model, return a draft Page Object as a Python
    string plus a manual-review checklist. Returns SKIPPED with empty code
    when no AI provider is configured (the caller prints the reason, exit 0).
    """
    from utils.ai_parser import send_json
    from utils.ai_provider import PROVIDER_NAME

    if PROVIDER_NAME == "noop":
        return GeneratedPageObject(
            status="SKIPPED",
            error="No AI provider configured; page-object generator skipped.",
        )

    excerpt = extract_dom_excerpt(dom_html)
    if not excerpt.strip():
        return GeneratedPageObject(
            status="ERROR",
            error="No interactive elements found in the provided HTML.",
        )

    parsed, resp = send_json(
        _user_prompt(class_name, excerpt, source_label),
        module=MODULE,
        model=model,
        system_prompt=_system_prompt(),
        # Codegen output is far larger than a verdict — a class with a dozen
        # methods overruns the 2048 default and comes back truncated.
        max_tokens=8192,
    )
    if resp is not None and resp.error:
        return GeneratedPageObject(status="ERROR", error=resp.error)
    if not isinstance(parsed, dict):
        return GeneratedPageObject(
            status="ERROR", error="Model returned unparseable JSON.",
        )

    code = str(parsed.get("code", "")).strip()
    if not code:
        return GeneratedPageObject(
            status="ERROR",
            error="Model returned no code field.",
            raw=parsed,
        )

    # Post-generation guardrails — refuse to emit code that violated the
    # hard rules. We can't fully sandbox GPT, but we can refuse the worst
    # offenders so the user doesn't accidentally accept brittle output.
    violations = []
    bad_patterns = [
        (r":nth-child\(",            "uses :nth-child"),
        (r":nth-of-type\(",          "uses :nth-of-type"),
        (r"\btime\.sleep\s*\(",      "uses time.sleep"),
        # > 500ms wait
        (r"wait_for_timeout\(\s*([5-9]\d\d|\d{4,})\s*\)",
         "uses long wait_for_timeout"),
        # CSS class/id-only selector passed to .locator(...) — the actually
        # brittle case. Previous version flagged 'x@y.com' as a class
        # selector; this one requires the selector context.
        (r"\.locator\(\s*['\"]\s*[.#][A-Za-z_]",
         "passes a class/id CSS selector to .locator()"),
        # The other common CSS-classy pattern: tag.class chains in a locator
        (r"\.locator\(\s*['\"]\s*[A-Za-z][A-Za-z0-9]*\.[A-Za-z_]",
         "passes a tag.class CSS selector to .locator()"),
    ]
    for pat, label in bad_patterns:
        if re.search(pat, code):
            violations.append(label)

    raw_checklist = parsed.get("review_checklist") or []
    checklist = [str(x).strip() for x in raw_checklist if str(x).strip()]
    if violations:
        checklist.insert(
            0,
            "🚨 Generator emitted output that violates guardrails: "
            + "; ".join(violations) + ". Review carefully before promoting.",
        )

    return GeneratedPageObject(
        status="GENERATED",
        code=code,
        review_checklist=checklist,
        raw=parsed,
    )
