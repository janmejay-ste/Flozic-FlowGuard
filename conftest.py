"""
Top-level pytest fixtures and hooks. Replaces what BaseTest.java
provided in the TestNG project:

  - Session-scope browser lifecycle (replaces @BeforeSuite / @AfterSuite)
  - Function-scope context+page (replaces @BeforeMethod / @AfterMethod)
  - Failure-artifact capture (replaces FailureArtifactManager)
  - Test-record accumulation into the v3 snapshot (replaces HealthTracker.addTestRecord)
  - Optional video recording via Playwright's built-in (replaces custom VideoRecorder)
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from utils.js_console_monitor import JsConsoleMonitor
from utils.network_monitor import NetworkMonitor, export_run_overview
from utils.snapshot_writer import add_test_record, write_snapshot, _STATE
from utils.dashboard_builder import build as build_dashboard
import utils.health_tracker as _ht

logger = logging.getLogger(__name__)

# Feature flag — disable network capture without code changes when debugging
# the monitor itself or chasing a perf regression in the monitor.
ENABLE_NETWORK_CAPTURE = os.environ.get(
    "FLOWGUARD_NETWORK_CAPTURE", "1",
).strip().lower() not in ("0", "false", "no", "off")


# ──────────────────────────────────────────────────────────────────────
# CLI options
# ──────────────────────────────────────────────────────────────────────


def pytest_addoption(parser: pytest.Parser) -> None:
    """Custom CLI flags for the migrated test infrastructure."""
    parser.addoption(
        "--no-video",
        action="store_true",
        default=False,
        help="Disable video recording (video is ON by default for all tests).",
    )
    parser.addoption(
        "--headed",
        action="store_true",
        default=True,
        help="Run with a visible browser (default: headed).",
    )
    parser.addoption(
        "--headless",
        action="store_true",
        default=False,
        help="Run with a headless browser (overrides --headed default).",
    )
    parser.addoption(
        "--slow-mo",
        type=int,
        default=0,
        help="Playwright slow_mo delay in ms for debugging.",
    )
    parser.addoption(
        "--browser",
        default="chromium",
        choices=("chromium", "firefox", "webkit"),
        help=(
            "Which Playwright browser engine to launch. Default: chromium. "
            "Use 'firefox' or 'webkit' for cross-browser runs. Install the "
            "engine first with: playwright install <engine>."
        ),
    )
    parser.addoption(
        "--cohort",
        default=None,
        help=(
            "Cohort tag for this run's test records. If omitted, each test's "
            "cohort is auto-derived from its @test_category metadata: "
            "FULL/SANITY tests requiring login → 'baseline-auth', everything "
            "else → 'baseline-smoke'. Override explicitly when running an "
            "experimental migration: --cohort experiment-<name>. The two "
            "default cohorts are NOT aggregated — they're operationally "
            "different populations (unattended-headless vs human-attended-"
            "headed) per docs/risk-register.md R-1 and R-3."
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Session-scope browser lifecycle
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def browser_instance(request: pytest.FixtureRequest) -> Iterator[Browser]:
    """One browser instance per test session — engine chosen by --browser."""
    headless = request.config.getoption("--headless")
    slow_mo = request.config.getoption("--slow-mo")
    engine = request.config.getoption("--browser")

    logger.info(
        "[session] Launching %s (headless=%s, slow_mo=%d ms)",
        engine, headless, slow_mo,
    )
    with sync_playwright() as pw:
        # Chromium accepts --start-maximized; Firefox/WebKit ignore CLI args
        # and use viewport sizing instead.
        if engine == "chromium":
            launcher = pw.chromium
            launch_args = ["--start-maximized", "--disable-notifications"]
        elif engine == "firefox":
            launcher = pw.firefox
            launch_args = []
        else:  # webkit
            launcher = pw.webkit
            launch_args = []

        browser = launcher.launch(
            headless=headless,
            slow_mo=slow_mo,
            args=launch_args,
        )
        try:
            yield browser
        finally:
            browser.close()
            logger.info("[session] %s closed", engine)


def pytest_configure(config: pytest.Config) -> None:
    """Repoint snapshot/dashboard/pdf module-level paths to a per-browser
    folder so parallel runs (e.g. chromium + firefox in two terminals) write
    to separate directories and never overwrite each other's results."""
    _retarget_report_paths(config)


def _report_root(config: pytest.Config) -> Path:
    """
    Return the report-output root for THIS session, namespaced by browser
    engine so parallel runs (Chrome + Firefox at the same time) write to
    different folders and never collide.

      --browser chromium → reports/trend/          (unchanged default)
      --browser firefox  → reports/trend/firefox/
      --browser webkit   → reports/trend/webkit/
    """
    engine = config.getoption("--browser")
    base = Path("reports/trend")
    return base if engine == "chromium" else (base / engine)


def _retarget_report_paths(config: pytest.Config) -> Path:
    """Repoint the module-level snapshot/dashboard paths to this run's
    per-browser folder. Returns the resolved root for reuse by callers."""
    root = _report_root(config)
    root.mkdir(parents=True, exist_ok=True)

    # snapshot_writer.SNAPSHOT_PATH
    import utils.snapshot_writer as _sw
    _sw.SNAPSHOT_PATH = root / "python-health-snapshot.json"

    # dashboard_builder.DASHBOARD_PATH + TREND_JSON
    import utils.dashboard_builder as _db
    _db.DASHBOARD_PATH = root / "dashboard.html"
    _db.TREND_JSON     = root / "trend-history.json"

    # pdf_report_builder writes report-printable.html / .pdf into the
    # same trend folder — repoint if it exposes a path constant too.
    try:
        import utils.pdf_report_builder as _pb
        for attr in ("REPORT_PDF_PATH", "REPORT_PRINTABLE_PATH",
                     "PDF_PATH", "PRINTABLE_PATH"):
            if hasattr(_pb, attr):
                old = getattr(_pb, attr)
                setattr(_pb, attr, root / Path(old).name)
    except Exception:
        pass

    return root


@pytest.fixture(scope="session", autouse=True)
def session_teardown_snapshot() -> Iterator[None]:
    """
    At session end:
      1. Write v3-schema JSON snapshot (bridge for any Java tooling).
      2. Build the Python-side HTML dashboard — no Java dependency.
      3. Compute layered health scores + error clusters.
      4. Build PDF report (weasyprint if installed, else printable HTML).
    Always runs, even if tests failed. Report paths are namespaced by
    --browser via the pytest_configure hook so parallel runs (Chrome +
    Firefox) don't collide.
    """
    yield

    # ── Collect session data ──────────────────────────────────────────────
    with _STATE.lock:
        records    = list(_STATE.test_records)
        started_at = _STATE.started_at_ms

    # ── 1. JSON snapshot ──────────────────────────────────────────────────
    try:
        path = write_snapshot()
        logger.info("[session] Snapshot written: %s", path)
        # Also archive the snapshot to a timestamped file so cross-run
        # analyses (failure clustering, flake detection, trend analysis)
        # have historical data to work with. The live python-health-snapshot.json
        # is still overwritten each run; this archive accrues forever.
        try:
            import shutil
            from datetime import datetime as _dt
            # Archive into the same per-browser folder the live snapshot lives in.
            from utils.snapshot_writer import SNAPSHOT_PATH as _LIVE_SNAPSHOT
            archive_dir = _LIVE_SNAPSHOT.parent / "snapshots"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / (
                _dt.fromtimestamp(started_at / 1000).strftime("%Y%m%d_%H%M%S")
                + ".json"
            )
            shutil.copy2(path, archive_path)
            logger.info("[session] Snapshot archived: %s", archive_path)
        except Exception as e:
            logger.warning("[session] Snapshot archive failed (non-fatal): %s", e)
    except Exception as e:
        logger.error("[session] Snapshot write failed: %s", e)

    # ── 2. HTML dashboard ─────────────────────────────────────────────────
    try:
        dash_path = build_dashboard(records, started_at)
        logger.info("[session] Dashboard written: %s", dash_path)
    except Exception as e:
        logger.error("[session] Dashboard build failed: %s", e)

    # ── 3 + 4. Layered scores + PDF report ───────────────────────────────
    try:
        from utils.error_clusterer import cluster as build_clusters, load_previous_titles
        from utils.layered_health_scores import compute as compute_scores
        from utils.pdf_report_builder import build as build_pdf
        from utils.risk_interpreter import interpret
        from utils.dashboard_builder import _compute_stats

        stats    = _compute_stats(records)
        decision = interpret(
            total        = stats["total"],
            passed       = stats["passed"],
            failed       = stats["failed"],
            smoke_total  = stats["smoke_total"],
            smoke_passed = stats["smoke_passed"],
        )

        clusters = _ht.get_clusters()
        scores   = compute_scores(records, clusters)

        logger.info(
            "[session] Health Score: %d/100 | Product: %d | Infra: %d | Framework: %d | "
            "JS clusters: %d",
            scores.overall, scores.product_health, scores.infra_health,
            scores.framework_health, len(clusters),
        )

        pdf_path = build_pdf(records, stats, decision, scores, clusters, started_at)
        logger.info("[session] Report written: %s", pdf_path)

    except Exception as e:
        logger.error("[session] PDF/scores build failed: %s", e, exc_info=True)

    # Session-wide network overview — aggregates the per-test summaries
    # NetworkExporter has been accumulating throughout the run. Cheap to call
    # (uses in-memory counters) and gives us the input for future failure-
    # fingerprinting work without re-reading individual artifact folders.
    try:
        from utils.snapshot_writer import SNAPSHOT_PATH as _SNAPSHOT
        run_dir = _SNAPSHOT.parent / "run_summary"
        overview_path = export_run_overview(run_dir)
        logger.info("[session] Network overview: %s", overview_path)
    except Exception as e:
        logger.warning("[session] Network overview write failed: %s", e)


# ──────────────────────────────────────────────────────────────────────
# Function-scope page lifecycle
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def page(
    browser_instance: Browser,
    request: pytest.FixtureRequest,
) -> Iterator[Page]:
    """
    Fresh context + page per test. The context is the unit of isolation
    Playwright recommends — it gives the test its own cookie jar,
    storage, and (optionally) a video recording.
    """
    # Video is ON by default for every test. Disable with --no-video.
    record_video = not request.config.getoption("--no-video")

    test_name = request.node.name
    # no_viewport=True lets Chrome use the actual OS window size — required
    # for --start-maximized to take effect. Firefox/WebKit don't support
    # --start-maximized, so we give them an explicit viewport instead.
    engine = request.config.getoption("--browser")
    ctx_kwargs: dict = (
        {"no_viewport": True}
        if engine == "chromium"
        else {"viewport": {"width": 1440, "height": 900}}
    )
    if record_video:
        # Playwright writes a <random>.webm file into this directory.
        # We move it to the right place (failure folder / recordings/) in teardown.
        video_dir = Path("reports/recordings") / test_name
        video_dir.mkdir(parents=True, exist_ok=True)
        ctx_kwargs["record_video_dir"] = str(video_dir)
        ctx_kwargs["record_video_size"] = {"width": 1280, "height": 800}

    context = browser_instance.new_context(**ctx_kwargs)
    pg = context.new_page()

    # Always-on JS monitor — attaches listeners on the blank page before any
    # navigation so every console/pageerror event is captured from the start.
    # Tests that need to make assertions on JS errors should use the
    # `console_monitor` fixture (which returns this same instance).
    _page_monitor = JsConsoleMonitor(pg)
    pg._js_monitor = _page_monitor  # type: ignore[attr-defined]

    # Always-on network monitor — same lifecycle as the JS monitor. Capture
    # is resilient: if attach raises, observability is disabled for this
    # test but the test itself still runs.
    _net_monitor: NetworkMonitor | None = None
    if ENABLE_NETWORK_CAPTURE:
        try:
            _net_monitor = NetworkMonitor(pg)
        except Exception:
            logger.exception("[net] NetworkMonitor attach failed; capture disabled "
                             "for this test.")
            _net_monitor = None
    # Namespaced attribute on Page — avoids collisions with future Playwright
    # internals. Tests should use the `network_monitor` fixture instead of
    # reaching into the attribute directly.
    setattr(pg, "_flowguard_network_monitor", _net_monitor)

    try:
        yield pg
    finally:
        # Capture failure artifacts BEFORE closing the page — once closed
        # we can no longer take screenshots / dump DOM.
        rep = getattr(request.node, "rep_call", None)
        failed = rep is not None and rep.failed

        artifact_folder: str | None = None
        if failed:
            artifact_folder = _capture_failure_artifacts(pg, test_name)
            # GPT failure triage — reads the artifacts just captured, asks
            # GPT to classify the root cause, writes triage.json next to the
            # screenshot. Cheap no-op if OPENAI_API_KEY is unset.
            try:
                from utils.ai_triage import triage as _ai_triage
                _ai_triage(
                    test_name=test_name,
                    failure_folder=Path("reports/failures") / artifact_folder,
                    exception_message=str(getattr(rep, "longrepr", "")).splitlines()[0]
                        if rep is not None else "",
                    traceback_text="\n".join(
                        str(getattr(rep, "longrepr", "")).splitlines()[-20:]
                    ) if rep is not None else "",
                )
            except Exception as e:
                logger.warning("AI triage failed (non-fatal): %s", e)

        # Grab the video path BEFORE closing the page — pg.video is only
        # accessible while the page object is alive. After pg.close() the
        # video file is finalised by Playwright on disk.
        video_path: str | None = None
        if record_video and pg.video:
            try:
                # .path() returns the file path even before the file is fully
                # written; the file is complete after pg.close() returns.
                video_path = pg.video.path()
            except Exception as e:
                logger.warning("Could not get video path: %s", e)

        # Close page — this finalises the video file on disk.
        pg.close()

        # Move video into the failure folder so it sits alongside the
        # screenshot / DOM artifacts. For passing tests keep it in recordings/.
        if video_path:
            src = Path(video_path)
            if src.exists():
                if failed and artifact_folder:
                    dest = Path("reports/failures") / artifact_folder / "recording.webm"
                    try:
                        shutil.move(str(src), str(dest))
                        video_path = str(dest)
                    except Exception:
                        pass  # keep original path on move failure
                else:
                    # passed test — move to recordings/ folder
                    rec_dir = Path("reports/recordings") / test_name
                    rec_dir.mkdir(parents=True, exist_ok=True)
                    dest = rec_dir / "recording.webm"
                    try:
                        shutil.move(str(src), str(dest))
                        video_path = str(dest)
                    except Exception:
                        pass

        context.close()

        # Network monitor teardown is a two-step sequence:
        #   1. finish_test() — record this test's traffic into the session
        #      aggregator. Runs regardless of outcome so passing tests are
        #      included in network_overview.json. Decoupled from export().
        #   2. stop()       — detach Playwright listeners.
        # Both are idempotent and isolated in try/except so observability
        # failures never mask the test outcome.
        if _net_monitor is not None:
            try:
                _net_monitor.finish_test()
            except Exception as e:
                logger.warning("[net] NetworkMonitor.finish_test() failed: %s", e)
            try:
                _net_monitor.stop()
            except Exception as e:
                logger.warning("[net] NetworkMonitor.stop() failed: %s", e)

        # Forward JS console events to the session-level health tracker so
        # ErrorClusterer and score computation can use them at session teardown.
        try:
            _ht.record_js_events(_page_monitor.events, test_name)
        except Exception as e:
            logger.warning("health_tracker.record_js_events failed: %s", e)

        # Record the test outcome for the snapshot writer.
        _record_outcome(request, failed, artifact_folder, video_path)


# ──────────────────────────────────────────────────────────────────────
# Console-monitor fixture — attaches before any navigation
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def console_monitor(page: Page) -> JsConsoleMonitor:
    """
    Return the session-level JsConsoleMonitor already attached to this page.

    The `page` fixture always attaches a JsConsoleMonitor on the blank page
    before any navigation — listeners capture every console/pageerror event
    from that point on.  This fixture simply hands tests a reference to that
    same monitor so they can make assertions (e.g. assert monitor.fatal_count() == 0).

    Tests that don't need JS-error assertions can ignore this fixture entirely.
    """
    # The monitor was attached in the `page` fixture and stored as _js_monitor.
    # Fall back to creating a fresh one (no events missed because the page is
    # still blank when this fixture runs) in case the attribute is absent.
    monitor = getattr(page, "_js_monitor", None)
    if monitor is None:
        monitor = JsConsoleMonitor(page)
    return monitor


@pytest.fixture
def network_monitor(page: Page) -> NetworkMonitor | None:
    """
    Return the session-level NetworkMonitor already attached to this page,
    or None if network capture was disabled via FLOWGUARD_NETWORK_CAPTURE=0
    (or if attach failed at startup).

    Tests that want to assert on network behaviour should use this fixture
    rather than poking at the page's private attribute directly:

        def test_x(page, network_monitor):
            page.goto(...)
            failures = network_monitor.failures() if network_monitor else ()
            assert all(e.status != 500 for e in failures)
    """
    return getattr(page, "_flowguard_network_monitor", None)


# ──────────────────────────────────────────────────────────────────────
# Pytest hooks — make per-phase outcome available to fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> object:
    """
    Attach the test's per-phase report to the item so fixtures can
    inspect `request.node.rep_call.failed` during teardown.
    """
    outcome = yield
    rep = outcome.get_result()  # type: ignore[attr-defined]
    setattr(item, f"rep_{rep.when}", rep)
    return rep


# ──────────────────────────────────────────────────────────────────────
# Failure-artifact capture
# ──────────────────────────────────────────────────────────────────────


def _capture_failure_artifacts(page: Page, test_name: str) -> str:
    """
    Capture screenshot + DOM + URL into reports/failures/<test>_<ts>/.
    Returns the folder name (not full path). Mirrors the Java
    FailureArtifactManager output layout so the Java DashboardBuilder's
    artifact links still work.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    folder_name = f"{test_name}_{stamp}"
    folder = Path("reports/failures") / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    try:
        page.screenshot(path=str(folder / "screenshot.png"), full_page=False)
    except Exception as e:
        logger.warning("Screenshot capture failed: %s", e)

    try:
        (folder / "dom.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:
        logger.warning("DOM dump failed: %s", e)

    try:
        (folder / "url.txt").write_text(page.url, encoding="utf-8")
    except Exception:
        pass

    # Network artifacts — pulled from the per-page NetworkMonitor attached
    # in the page fixture. Writes network-summary.json + network-events.json
    # alongside the DOM/screenshot. No-op when capture is disabled.
    try:
        net_monitor = getattr(page, "_flowguard_network_monitor", None)
        if net_monitor is not None:
            net_monitor.export(folder)
    except Exception as e:
        logger.warning("[net] Network artifact export failed: %s", e)

    logger.info("[failure] Artifacts saved to: %s", folder)
    return folder_name


# ──────────────────────────────────────────────────────────────────────
# Outcome recording
# ──────────────────────────────────────────────────────────────────────


def _record_outcome(
    request: pytest.FixtureRequest,
    failed: bool,
    artifact_folder: str | None,
    video_path: str | None,
) -> None:
    """Push the test result to the snapshot writer."""
    rep = getattr(request.node, "rep_call", None)
    duration_ms = int((rep.duration if rep else 0) * 1000)

    # Pull TestCategory metadata if the class supplied it via @test_category.
    cls = request.node.cls
    meta = getattr(cls, "_test_category", None) if cls else None

    category = meta.type if meta else "UNKNOWN"
    feature = meta.feature if meta else "Unknown"
    login = "Auth" if (meta and meta.requires_login) else "Guest"
    clazz = cls.__name__ if cls else request.node.module.__name__

    # Cohort resolution:
    #   - Explicit --cohort wins (used for experimental migrations)
    #   - Otherwise, auto-derive based on requires_login from @test_category:
    #     tests needing login → 'baseline-auth' (human-attended-required)
    #     tests not needing login → 'baseline-smoke' (unattended-capable)
    # This split is operationally meaningful per docs/risk-register.md R-3:
    # baseline-smoke runs unattended-headless; baseline-auth requires
    # human attendance for the Turnstile gate.
    explicit_cohort = request.config.getoption("--cohort")
    if explicit_cohort:
        cohort = explicit_cohort
    elif meta and meta.requires_login:
        cohort = "baseline-auth"
    else:
        cohort = "baseline-smoke"

    # Tag cohort with the browser engine so a hypothetical cross-browser
    # merged dashboard can still tell chromium runs from firefox runs.
    # No-op for the default chromium engine to preserve historical cohort
    # values used in trend comparisons.
    _engine = request.config.getoption("--browser")
    if _engine != "chromium":
        cohort = f"{cohort}-{_engine}"

    add_test_record(
        category=category,
        login=login,
        feature=feature,
        clazz=clazz,
        method=request.node.name,
        status="FAIL" if failed else "PASS",
        duration_ms=duration_ms,
        artifact_folder=artifact_folder,
        video_path=video_path,
        cohort=cohort,
    )
