"""
FlowGuard MCP server — the PROTOCOL layer (read-only v1).

WHAT THIS FILE IS (the MCP concepts, in one page):

  * MCP (Model Context Protocol) is a standard for exposing capabilities to AI
    clients (Claude Code, Claude Desktop, Cursor, ...). The client and this
    server speak JSON-RPC. v1 uses the STDIO transport: the client launches
    this script as a subprocess and exchanges messages over stdin/stdout —
    which is why this file must never print() to stdout (it would corrupt the
    protocol stream; logs go to stderr).

  * A TOOL is a function the AI may CALL (with arguments) when it decides your
    question needs it. FastMCP turns each decorated function into a tool: the
    type hints become the tool's input JSON Schema, and the DOCSTRING becomes
    the description the model reads to decide when to use it — so docstrings
    here are prompts, not comments.

  * A RESOURCE is data the client can READ by URI (no arguments, like a GET).
    We expose the combined report's location as one, for contrast with tools.

  * The handshake: on startup the client sends `initialize`, then
    `tools/list` — the server replies with every tool's name/description/
    schema. From then on the model calls `tools/call` with JSON arguments and
    gets JSON results back.

All actual logic lives in utils/mcp_tools.py (plain, unit-tested Python) —
this file only adapts it to the protocol. Read-only by design: no tool
triggers runs, mutates state, or exposes credentials.

Run manually:      .venv/bin/python flowguard_mcp.py
Register (repo .mcp.json already does this for Claude Code).
"""
from __future__ import annotations

# mcp SDK 2.x: the class FastMCP (1.x) is now MCPServer — same decorator API.
from mcp.server.mcpserver import MCPServer

from utils import mcp_tools as t

mcp = MCPServer(
    "flowguard",
    instructions=(
        "Read-only access to Flozic FlowGuard's QA results: release status, "
        "failures with evidence, systemic clusters, mobile findings, failure "
        "history and cross-engine comparison. Data reflects the LAST completed "
        "runs — check get_run_provenance for freshness. This server cannot "
        "start runs or change anything."
    ),
)


@mcp.tool()
def get_release_status() -> dict:
    """Release decision (READY/AT_RISK/BLOCKED...), why, and each engine's
    health scores (overall/product/infra/framework/mobile) with run provenance.
    Start here for 'can we release?' or 'how healthy is the build?'."""
    return t.get_release_status()


@mcp.tool()
def get_failures(engine: str | None = None) -> dict:
    """All failing tests from the latest completed runs (optionally one engine:
    'chromium' or 'webkit'): test id, feature, normalized error signature,
    failure type, and the evidence folder path."""
    return t.get_failures(engine=engine)


@mcp.tool()
def get_systemic_clusters() -> dict:
    """Signature-matched systemic failures: groups of >=3 independent tests
    sharing one normalized error signature (evidence of a single common cause,
    NOT mere same-feature concentration). Use to answer 'is this one bug or
    forty?'."""
    return t.get_systemic_clusters()


@mcp.tool()
def get_failure_evidence(test_name: str) -> dict:
    """Debug evidence for one failing test (substring match, e.g. 'acculynx'):
    error, AI diagnosis + suggested fix, page URL, network digest (failed
    request counts/statuses), and paths to screenshot/DOM/video/network bodies."""
    return t.get_failure_evidence(test_name)


@mcp.tool()
def get_mobile_findings(engine: str = "chromium", severity: str | None = None,
                        check: str | None = None, limit: int = 50) -> dict:
    """Mobile UX findings for an engine, filterable by severity (blocker/major/
    minor/info) and check (tap_target, font_size, input_zoom, horizontal_overflow,
    ...). Each finding has the element, device, page, CSS selector and the
    recommended fix."""
    return t.get_mobile_findings(engine=engine, severity=severity,
                                 check=check, limit=limit)


@mcp.tool()
def get_failure_history() -> dict:
    """Cross-run classification of failures over the last 30 runs: NEW (seen
    once), OBSERVED (twice), RECURRING (3+), FLAKY (mixed pass/fail),
    CONSISTENT_FAILURE (fails >=90% of runs it appears in)."""
    return t.get_failure_history()


@mcp.tool()
def compare_engines() -> dict:
    """Cross-engine 4-state comparison over co-executed tests: shared failures,
    chromium-only defects, webkit-only defects, and not-comparable (a test the
    other engine never ran is neither a pass nor a defect)."""
    return t.compare_engines()


@mcp.tool()
def get_run_provenance() -> dict:
    """Freshness check: when each engine last completed a run, which suite
    (full/mobile), the git commit it tested, and the combined report path."""
    return t.get_run_provenance()


@mcp.resource("flowguard://report")
def combined_report_location() -> str:
    """Where the human-readable combined cross-engine report lives."""
    import os
    p = "reports/trend/combined-report.html"
    return (f"Combined cross-engine report: {os.path.abspath(p)}"
            if os.path.isfile(p) else "No combined report generated yet — run the suite.")


if __name__ == "__main__":
    # STDIO transport: the MCP client (e.g. Claude Code) spawns this process
    # and speaks JSON-RPC over stdin/stdout.
    mcp.run()
