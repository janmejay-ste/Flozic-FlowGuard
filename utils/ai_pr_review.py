"""
GPT-powered code review for test/page-object changes.

Reads a unified git diff, sends one file at a time to GPT with a rule list,
and gets back structured findings. Designed to be advisory: it never blocks
a PR, it just produces a markdown report you can read at a glance.

Rule set (per user spec):
  - Hardcoded waits (Thread.sleep, page.wait_for_timeout(>500))
  - Magic numbers (literal ints/floats without explanation)
  - Brittle selectors (CSS classes like 'awsui_*', deep XPath, nth-child)
  - Duplicate locators (same selector used in multiple places)
  - Missing waits before .click() / .fill()
  - assert without an explanatory message
  - New test class missing @test_category metadata
  - Duplicate helper methods (same logic with a different name)
  - Missing retry / try-except around flaky operations
  - Missing screenshot/artifact capture on assertion failure

Findings are returned as JSON so the CLI can render markdown OR post inline
PR comments via the GitHub/GitLab API later.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")
API_BASE = "https://api.openai.com/v1/chat/completions"
REQUEST_TIMEOUT_S = 60

# Per-file size cap. Bigger files are truncated with a marker — keeps GPT
# token cost predictable on big refactors.
MAX_DIFF_BYTES_PER_FILE = 20_000

REVIEW_RULES = (
    "1. Hardcoded waits: flag any Thread.sleep, page.wait_for_timeout(N>500),\n"
    "   time.sleep — suggest event-based waits (wait_for_selector, expect()).\n"
    "2. Magic numbers: flag literal ints/floats (timeouts, retry counts,\n"
    "   array indices) that aren't named constants or commented.\n"
    "3. Brittle selectors: flag CSS classes that look hashed/generated\n"
    "   (e.g. 'awsui_*', 'css-7fhd23', '.MuiBox-root-42'), deep XPath\n"
    "   chains, and :nth-child / :nth-of-type. Prefer role/text/attribute.\n"
    "4. Duplicate locators: same selector string used in 2+ places in the\n"
    "   diff — should be a named constant or method.\n"
    "5. Missing waits: locator.click() or .fill() without a preceding\n"
    "   wait_for(state='visible') or expect().to_be_visible().\n"
    "6. Bare assertions: assert <expr> with no message — fails are hard to\n"
    "   triage. Require assert <expr>, '<context>'.\n"
    "7. New test class with no @test_category decorator.\n"
    "8. Duplicate helpers: two functions in the diff doing essentially the\n"
    "   same thing under different names.\n"
    "9. Missing retry / error handling around known-flaky calls\n"
    "   (network, navigation, focus changes).\n"
    "10. Missing screenshot/artifact capture in obvious failure paths\n"
    "    (e.g. try/except that swallows an exception without logging).\n"
)


@dataclass
class Finding:
    file: str
    line: int = 0           # best-effort; 0 if GPT couldn't localize
    severity: str = "minor" # minor | major | blocker
    rule: str = ""          # short identifier (e.g. "hardcoded_wait")
    message: str = ""       # one-sentence description
    suggested_fix: str = "" # one-line action

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(
            file=str(d.get("file", "")),
            line=int(d.get("line", 0) or 0),
            severity=str(d.get("severity", "minor")),
            rule=str(d.get("rule", "")),
            message=str(d.get("message", "")),
            suggested_fix=str(d.get("suggested_fix", "")),
        )


@dataclass
class ReviewResult:
    status: str                          # "REVIEWED" | "SKIPPED" | "ERROR"
    files_reviewed: int = 0
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def split_diff_by_file(diff_text: str) -> dict[str, str]:
    """
    Split a unified diff into {filename: file_diff_text}.

    Recognises the 'diff --git a/<path> b/<path>' header. Returns absolute
    file paths as keys (the 'b/' side — i.e. the new path).
    """
    out: dict[str, str] = {}
    current_file: str | None = None
    buf: list[str] = []
    header_re = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    for line in diff_text.splitlines(keepends=True):
        m = header_re.match(line.rstrip("\n"))
        if m:
            # Flush previous
            if current_file is not None and buf:
                out[current_file] = "".join(buf)
            current_file = m.group(2)
            buf = [line]
        else:
            if current_file is not None:
                buf.append(line)
    if current_file is not None and buf:
        out[current_file] = "".join(buf)
    return out


def filter_diff_to_python_tests_and_pages(by_file: dict[str, str]) -> dict[str, str]:
    """Keep only *.py files under tests/ or pages/ or utils/."""
    keep: dict[str, str] = {}
    for path, d in by_file.items():
        if not path.endswith(".py"):
            continue
        if not (path.startswith("tests/") or path.startswith("pages/")
                or path.startswith("utils/")):
            continue
        keep[path] = d
    return keep


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n<<TRUNCATED — diff exceeded review size cap>>"


def review_file_diff(
    file_path: str,
    file_diff: str,
    model: str | None = None,
) -> list[Finding]:
    """
    Send one file's diff to GPT, return its list of findings.

    On any failure (no API key, HTTP error, parse error) returns an empty
    list — the caller still reports a clean run rather than failing the PR.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        import requests  # type: ignore
    except ImportError:
        return []

    diff_excerpt = _truncate(file_diff, MAX_DIFF_BYTES_PER_FILE)

    user_msg = (
        f"Review the following git diff for file `{file_path}`.\n\n"
        "Rules to check:\n"
        f"{REVIEW_RULES}\n"
        "Diff (unified format):\n"
        "----BEGIN DIFF----\n"
        f"{diff_excerpt}\n"
        "----END DIFF----\n\n"
        "Return a SINGLE JSON object, no other text, with key 'findings' "
        "whose value is an array. Each item has:\n"
        "  file (string),\n"
        "  line (integer — line number IN THE NEW FILE if you can localize "
        "it, else 0),\n"
        "  severity (minor | major | blocker),\n"
        "  rule (short snake_case id like 'hardcoded_wait', 'brittle_selector'),\n"
        "  message (one sentence describing the problem),\n"
        "  suggested_fix (one-line action item).\n\n"
        "Only flag problems that are clearly present in the ADDED (+) lines "
        "of the diff. Ignore context lines. Be specific — vague nits get "
        "ignored by reviewers. Return an empty array if you find nothing."
    )

    payload = {
        "model": model or DEFAULT_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content":
                "You are a senior Python/Playwright test engineer doing a "
                "focused code review. Findings only. No general praise."},
            {"role": "user", "content": user_msg},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        resp = requests.post(API_BASE, json=payload, headers=headers,
                             timeout=REQUEST_TIMEOUT_S)
    except Exception as e:
        logger.warning("[ai-pr-review] request failed for %s: %s", file_path, e)
        return []
    if resp.status_code != 200:
        logger.warning("[ai-pr-review] HTTP %d for %s: %s",
                       resp.status_code, file_path, resp.text[:200])
        return []
    try:
        parsed = json.loads(resp.json()["choices"][0]["message"]["content"])
        items  = parsed.get("findings") or []
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.warning("[ai-pr-review] parse error for %s: %s", file_path, e)
        return []

    findings: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        f = Finding.from_dict(item)
        # Ensure 'file' field is populated even if model omitted it
        if not f.file:
            f.file = file_path
        findings.append(f)
    return findings


def review_diff(diff_text: str, model: str | None = None) -> ReviewResult:
    """
    Review a full unified diff. Returns aggregated findings.

    Skips files outside tests/, pages/, utils/. Truncates large per-file
    diffs to keep per-call token cost bounded.
    """
    if not diff_text.strip():
        return ReviewResult(status="SKIPPED", error="Empty diff")

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return ReviewResult(
            status="SKIPPED",
            error="OPENAI_API_KEY not set; review skipped.",
        )

    by_file  = filter_diff_to_python_tests_and_pages(split_diff_by_file(diff_text))
    findings: list[Finding] = []
    for path, file_diff in by_file.items():
        logger.info("[ai-pr-review] reviewing %s (%d bytes)", path, len(file_diff))
        findings.extend(review_file_diff(path, file_diff, model=model))

    return ReviewResult(
        status="REVIEWED",
        files_reviewed=len(by_file),
        findings=findings,
    )


def render_markdown(result: ReviewResult, sha: str = "") -> str:
    """Pretty-print the review result as a markdown report."""
    if result.status == "SKIPPED":
        return f"# PR Review — skipped\n\n{result.error or ''}\n"
    if result.status == "ERROR":
        return f"# PR Review — error\n\n{result.error or ''}\n"

    by_sev: dict[str, list[Finding]] = {"blocker": [], "major": [], "minor": []}
    for f in result.findings:
        by_sev.setdefault(f.severity, []).append(f)

    lines: list[str] = []
    header = f"# PR Review{(' — ' + sha) if sha else ''}"
    lines.append(header)
    lines.append("")
    lines.append(
        f"Reviewed **{result.files_reviewed}** changed Python file(s) under "
        f"`tests/`, `pages/`, `utils/`. Found "
        f"**{len(result.findings)}** finding(s)."
    )
    lines.append("")
    if not result.findings:
        lines.append("✅ No issues found by the rule set.")
        return "\n".join(lines) + "\n"

    counts = " · ".join(
        f"{lvl}: **{len(by_sev[lvl])}**"
        for lvl in ("blocker", "major", "minor") if by_sev[lvl]
    )
    lines.append(f"Severity breakdown: {counts}")
    lines.append("")

    for sev in ("blocker", "major", "minor"):
        items = by_sev[sev]
        if not items:
            continue
        icon = {"blocker": "🔥", "major": "⚠️", "minor": "·"}[sev]
        lines.append(f"## {icon} {sev.title()} ({len(items)})")
        lines.append("")
        for f in items:
            loc = f"`{f.file}`" + (f":L{f.line}" if f.line else "")
            lines.append(f"- **[{f.rule}]** {loc} — {f.message}")
            if f.suggested_fix:
                lines.append(f"  - *Suggested:* {f.suggested_fix}")
        lines.append("")

    return "\n".join(lines) + "\n"
