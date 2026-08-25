"""
utils/mobile_interaction_auditor.py

Mobile FUNCTIONAL auditor — the companion to mobile_layout_auditor.py.

THE DISTINCTION, AND WHY IT MATTERS
-----------------------------------
The layout auditor answers "does this page RENDER acceptably at this size?"
It reads bounding boxes and computed styles. Every check it runs would pass
on a page whose buttons do nothing, whose menu never opens, and which cannot
be scrolled at all — because none of those are geometry.

This module answers "does it WORK when touched?" Two rules follow from that:

1. Every check asserts an OBSERVABLE EFFECT. Tapping is not evidence; a URL
   change, an aria-expanded flip, a scroll position that moved, a value that
   landed in an input — those are evidence. This project has repeatedly
   shipped checks that performed an action, logged success, and verified
   nothing: a login that skipped its form, five tests XPASSing on a transient
   URL, a connect "activated" by a JS click on a disabled button. An
   interaction suite is the easiest place in a codebase to repeat that
   mistake, so it is designed against here.

2. Real touch, not mouse. `page.mouse` emits mouse events and
   `window.scrollTo` bypasses input entirely; a page whose touch handling is
   broken passes both. Gestures here go through CDP
   `Input.dispatchTouchEvent` / `Input.synthesizeScrollGesture`, which
   produce genuine touch streams — the same path Chrome DevTools device mode
   uses. `has_touch: True` on every profile in pages/mobile/mobile_devices.py
   is what makes that meaningful.

ABSENT FEATURES ARE REPORTED, NOT SKIPPED
-----------------------------------------
Not every page has a carousel or a chat widget. When a check finds nothing to
act on it emits an `info` finding saying so. A check that silently does
nothing is indistinguishable from a check that passed, which is the failure
mode this whole module exists to avoid.

CHROMIUM ONLY (gracefully)
--------------------------
CDP is Chromium-specific. On firefox/webkit the gesture checks record an
`info` finding and return rather than falling back to mouse events, because a
mouse-based "swipe" would be a check that tests something the user never does.
"""

from __future__ import annotations

import functools
import logging
from typing import Optional

from utils.mobile_layout_auditor import Inconsistency


def _tallied(fn):
    """Executed-coverage tally around ONE check invocation.

    Lives on the check methods themselves, not in run_all, because flow tests
    legitimately call individual checks directly (e.g. check_vertical_scroll
    on app_directory) — the 2026-08-20 validation run proved a run_all-only
    tally under-counts: WebKit showed 48 not-executed runs against 54 gap
    instances in the findings, because 6 direct invocations were invisible
    to it. A coverage denominator that misses invocations is its own
    reporting illusion.

    executed=False when the check crashed OR self-reported the run_all crash
    sentinel ("...interaction was NOT verified.", ~line 1176) OR could only
    report a capability gap (UNTESTED/UNSUPPORTED). Content-driven
    "nothing applicable on this page" counts as executed: we looked — that
    includes check_widget's "presence verified, contents NOT verified."
    (~line 1013), which is a CONCLUSION about a cross-origin iframe, not a
    coverage gap, so it must NOT match the crash sentinel below.
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        from utils.mobile_report_builder import note_check_run
        before = len(self.findings)
        ok = True
        try:
            return fn(self, *args, **kwargs)
        except Exception:
            ok = False
            raise
        finally:
            new = self.findings[before:]
            untested = any("UNTESTED" in f.message or "UNSUPPORTED" in f.message
                           for f in new)
            crashed = any("interaction was NOT verified" in f.message for f in new)
            if crashed or not ok:             # hard crash or run_all's crash sentinel
                note_check_run(self.engine, executed=False, structural=False)
            elif untested:                    # engine capability gap -> N/A
                note_check_run(self.engine, executed=False, structural=True)
            else:                             # check completed successfully
                note_check_run(self.engine, executed=True)
    return wrapper

logger = logging.getLogger(__name__)


# ── Selector-agnostic candidate patterns ───────────────────────────────
# Same philosophy as the layout auditor: match on roles, ARIA and broad
# class-name families rather than one product's hashed classes, so these
# survive a redesign.

MENU_TOGGLE = (
    "button[aria-expanded], [aria-label*='menu' i], [aria-label*='navigation' i], "
    "button.navbar-toggler, [class*='hamburger'], [class*='menu-toggle'], "
    "[class*='burger'], [class*='nav-toggle']"
)
NAV_LINK = "header a[href], nav a[href]"
# Substring class matching alone is NOT sufficient here. Flozic's pricing page
# carries `carousel_item`, `slider_Items` and `slidertask` on static Bootstrap
# grid cells -- vestigial names from an earlier design. Matching on those
# produced 6 "carousel did not respond to a left swipe" findings against
# markup that is not a carousel and has no swipe behaviour to exercise.
# Candidates must additionally pass _CAROUSEL_EVIDENCE_JS.
CAROUSEL = (
    "[class*='carousel'], [class*='slider'], [class*='swiper'], "
    "[aria-roledescription='carousel'], [class*='slick']"
)

# Behavioural evidence that a matched element really is a swipeable carousel:
# it either scrolls horizontally, or ships carousel controls (prev/next,
# indicator dots), or declares itself one via ARIA.
_CAROUSEL_EVIDENCE_JS = """
(el) => {
  if (!el) return null;
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const scrollable = el.scrollWidth > el.clientWidth + 4 &&
                     ['auto', 'scroll'].includes(cs.overflowX);
  const controls = el.querySelectorAll(
    "[class*='prev'], [class*='next'], [class*='indicator'], [class*='dots'], " +
    "[data-slide], [aria-label*='next' i], [aria-label*='previous' i]").length;
  const aria = el.getAttribute('aria-roledescription') === 'carousel' ||
               el.getAttribute('role') === 'region' && /carousel/i.test(el.className || '');
  return { usable: r.width > 40 && r.height > 20,
           scrollable, controls, aria,
           is_carousel: Boolean(r.width > 40 && r.height > 20 &&
                                (scrollable || controls > 0 || aria)),
           w: Math.round(r.width), h: Math.round(r.height),
           scrollW: el.scrollWidth, clientW: el.clientWidth,
           overflowX: cs.overflowX,
           cls: (el.className || '').toString().slice(0, 50) };
}
"""
# Flozic ships its own widget as a cross-origin iframe:
#   <iframe id="flozic-widget-iframe" src="https://widget.flozic.ai/?cid=...">
# confirmed by probing the live homepage and pricing page. The generic
# third-party patterns are kept after it for other deployments.
WIDGET_IFRAME = "iframe#flozic-widget-iframe"
WIDGET_LAUNCHER = (
    f"{WIDGET_IFRAME}, "
    "iframe[title*='chat' i], iframe[id*='chat' i], [id*='chat-widget' i], "
    "[class*='chat-widget'], [class*='intercom'], [class*='crisp'], "
    "[class*='tawk'], [class*='drift'], [class*='livechat'], [class*='freshchat']"
)
# The widget is injected well after domcontentloaded. Racing it produced
# device-dependent findings (iPad Mini only) that looked like a device
# difference and were purely a timing artifact.
WIDGET_LOAD_TIMEOUT_MS = 8_000
TEXT_INPUT = (
    "input[type='text'], input[type='email'], input[type='search'], "
    "input:not([type]), textarea"
)


class MobileInteractionAuditor:
    """Functional mobile checks against an already-loaded page."""

    # Apple HIG / Google Material minimum touch target. Duplicated from
    # MobileLayoutAuditor rather than inherited: these two auditors are
    # deliberately independent (geometry vs behaviour). Referencing
    # self.MIN_TAP_TARGET_PX without defining it here made check_widget raise
    # AttributeError on all 6 devices x 2 engines -- caught only because a
    # crashed check self-reports as "NOT verified" instead of passing silently.
    MIN_TAP_TARGET_PX = 44

    # iOS Safari auto-zooms the page when a focused input's font-size is under
    # 16px -- the single most common "keyboard opened and the layout jumped"
    # defect on real iPhones. The zoom itself cannot be simulated in emulation
    # (no OS keyboard), but its TRIGGER CONDITION is plain computed CSS, so it
    # is checked statically and honestly labelled as such.
    MIN_INPUT_FONT_PX = 16

    # A fixed header taller than this fraction of the viewport leaves too
    # little room to read on a phone. 25% is a judgement call, deliberately
    # reported as `minor` rather than treated as a defect.
    MAX_FIXED_VIEWPORT_FRACTION = 0.25
    # Fixed elements are allowed to move by this much between scroll positions
    # before it counts as drift — sub-pixel rounding and 1px borders are normal.
    FIXED_DRIFT_TOLERANCE_PX = 2
    # Upper bound on how far a page must scroll to count as scrollable. The
    # REQUIRED delta is derived per page from the range actually available --
    # see _required_scroll_delta. A flat 40px produced two false blockers on
    # the signup page, which has only 20-29px of scroll range at phone widths:
    # scrolling perfectly to its bottom still read as "does not scroll".
    MAX_REQUIRED_SCROLL_PX = 40
    MIN_REQUIRED_SCROLL_PX = 8
    REQUIRED_SCROLL_FRACTION = 0.5
    SETTLE_MS = 400

    def __init__(self, page, page_name: str, device_name: str):
        self.page = page
        self.page_name = page_name
        self.device_name = device_name
        self.findings: list[Inconsistency] = []
        self._cdp = None
        self._cdp_tried = False
        self.engine = self._detect_engine()

    def _detect_engine(self) -> str:
        """'chromium' | 'webkit' | 'firefox' | 'unknown'."""
        try:
            return self.page.context.browser.browser_type.name
        except Exception:
            return "unknown"

    # ── CDP plumbing ───────────────────────────────────────────────────

    def _cdp_session(self):
        """Lazily open a CDP session. None on non-Chromium engines."""
        if self._cdp_tried:
            return self._cdp
        self._cdp_tried = True
        try:
            self._cdp = self.page.context.new_cdp_session(self.page)
        except Exception as e:
            logger.info(
                "[mobile-interaction] CDP unavailable (%s) — touch gestures "
                "will be reported as not-applicable rather than faked with "
                "mouse events.", type(e).__name__,
            )
            self._cdp = None
        return self._cdp

    # Flozic's homepage sets `scroll-behavior: smooth`, which makes
    # window.scrollTo/scrollBy ANIMATE. Any measurement taken straight after
    # then reads the pre-scroll box. That produced 11 unverifiable "covered by
    # a fixed element" findings whose centred_y stayed at ~0-59px when it
    # should have been ~400 -- the centring step had silently done nothing.
    # Every programmatic scroll for measurement purposes must go through here.
    _SCROLL_INSTANT_JS = """
    (y) => {
      const root = document.documentElement;
      const prev = root.style.scrollBehavior;
      root.style.scrollBehavior = 'auto';       // beat the CSS rule
      window.scrollTo({ top: y, left: 0, behavior: 'instant' });
      root.style.scrollBehavior = prev;
      return Math.round(window.scrollY);
    }
    """

    def _scroll_to_instant(self, y: int) -> int:
        """Scroll to `y` synchronously, defeating CSS smooth scrolling."""
        try:
            return self.page.evaluate(self._SCROLL_INSTANT_JS, y)
        except Exception:
            self.page.evaluate("(y) => window.scrollTo(0, y)", y)
            self.page.wait_for_timeout(self.SETTLE_MS)
            return self.page.evaluate("() => Math.round(window.scrollY)")

    def _touch_scroll(self, dy: int, dx: int = 0) -> bool:
        """Scroll by a real touch gesture. Returns False if CDP is absent."""
        cdp = self._cdp_session()
        if cdp is None:
            return False
        vp = self.page.viewport_size or {"width": 390, "height": 844}
        cdp.send("Input.synthesizeScrollGesture", {
            "x": vp["width"] // 2,
            "y": vp["height"] // 2,
            "xDistance": -dx,     # CDP distance is inverted vs scroll direction
            "yDistance": -dy,
            "gestureSourceType": "touch",
            "speed": 3000,
        })
        self.page.wait_for_timeout(self.SETTLE_MS)
        return True

    def _traverse_scroll(self, dy: int) -> str:
        """Move DOWN the page to reach content. Returns the mechanism used.

        Distinct from _touch_scroll on purpose. Some checks need to test
        whether TOUCH scrolling works (a touch-only concern); others just need
        to get far enough down the page to see whether a section rendered. On
        WebKit the first is impossible and the second is trivial, so conflating
        them makes every below-the-fold section look unrendered on WebKit —
        a page of false findings from an engine difference.

        Returns 'touch' when a real gesture was used, 'programmatic' otherwise.
        Callers that report findings should say which.
        """
        if self._touch_scroll(dy=dy):
            return "touch"
        self._scroll_to_instant(self.page.evaluate("() => window.scrollY") + dy)
        self.page.wait_for_timeout(self.SETTLE_MS)
        return "programmatic"

    def _touch_swipe(self, box: dict, dx: int, dy: int = 0) -> bool:
        """Swipe across `box` with a genuine touchstart/move/end stream."""
        x0 = box["x"] + box["width"] / 2
        y0 = box["y"] + box["height"] / 2
        steps = 8
        cdp = self._cdp_session()
        if cdp is None:
            # No CDP (WebKit/Firefox) => no swipe. An earlier version dispatched
            # synthetic TouchEvents from page JS and I asserted that "genuinely
            # exercises swipe handlers" WITHOUT testing it. WebKit does not
            # support the Touch/TouchEvent constructors, so it silently failed.
            # Rather than invent an implementation to make a test green, the
            # capability is reported UNSUPPORTED on this engine.
            return False
        cdp.send("Input.dispatchTouchEvent", {
            "type": "touchStart",
            "touchPoints": [{"x": x0, "y": y0}],
        })
        for i in range(1, steps + 1):
            cdp.send("Input.dispatchTouchEvent", {
                "type": "touchMove",
                "touchPoints": [{
                    "x": x0 + dx * i / steps,
                    "y": y0 + dy * i / steps,
                }],
            })
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        self.page.wait_for_timeout(self.SETTLE_MS)
        return True

    # ── Scrolling ──────────────────────────────────────────────────────

    def _required_scroll_delta(self, available_px: int) -> int:
        """How far this page must move to count as touch-scrollable.

        Proportional, not absolute: a page with 29px of range cannot move 40px,
        so demanding it is a guaranteed false failure. Half the available range,
        clamped to [8, 40].
        """
        return max(
            self.MIN_REQUIRED_SCROLL_PX,
            min(self.MAX_REQUIRED_SCROLL_PX,
                round(available_px * self.REQUIRED_SCROLL_FRACTION)),
        )

    @_tallied
    def check_vertical_scroll(self):
        """A page taller than the viewport must actually scroll, by touch.

        Catches the classic mobile scroll trap: `overflow:hidden` on body (or
        a full-screen overlay swallowing touchmove) leaves a page that renders
        perfectly and cannot be read past the first screen.
        """
        m = self.page.evaluate(
            "() => ({ scrollH: document.documentElement.scrollHeight,"
            " clientH: document.documentElement.clientHeight,"
            " y: window.scrollY })"
        )
        if m["scrollH"] <= m["clientH"] + 4:
            self._add("scroll", "info",
                      f"Page fits within the viewport on {self.device_name} "
                      f"({m['scrollH']}px content vs {m['clientH']}px) — "
                      f"vertical scrolling not applicable.")
            return

        if not self._touch_scroll(dy=m["clientH"]):
            self._add("scroll", "info",
                      f"Touch gesture could not be exercised on "
                      f"{self.engine} ({self.device_name}): the Chromium CDP "
                      f"gesture API (Input.synthesizeScrollGesture) is "
                      f"unavailable, and a synthetic TouchEvent does not pan "
                      f"the document. NOT substituted with a wheel or mouse "
                      f"event — touch scrolling is simply UNTESTED on this "
                      f"engine.")
            return

        after = self.page.evaluate("() => window.scrollY")
        moved = after - m["y"]
        available = m["scrollH"] - m["clientH"]
        required = self._required_scroll_delta(available)
        if moved < required:
            self._add("scroll", "blocker",
                      f"Page does not scroll by touch on {self.device_name}: "
                      f"{m['scrollH']}px of content in a {m['clientH']}px "
                      f"viewport ({available}px of range), but a full-screen "
                      f"swipe moved scrollY by only {moved}px — under the "
                      f"{required}px required for this page. Content below the "
                      f"fold is unreachable.",
                      details={"scroll_height": m["scrollH"],
                               "client_height": m["clientH"],
                               "available": available,
                               "required": required, "delta": moved})
            return

        # Can it reach the bottom? An overlay or a scroll-jacking script often
        # allows some movement then stops.
        #
        # Iteration count scales to the page. A fixed cap of 12 reported a
        # false "gesture intercepted" on all 6 devices against the Flozic
        # homepage: 844px viewport x 12 = ~10,100px of a 21,463px page, so the
        # loop ran out of steps and the shortfall was arithmetic, not a bug.
        # +8 gives slack for lazy-loaded content that grows the page mid-scroll.
        max_steps = int(m["scrollH"] / max(m["clientH"], 1)) + 8
        for _ in range(max_steps):
            before = self.page.evaluate("() => window.scrollY")
            self._touch_scroll(dy=m["clientH"])
            if self.page.evaluate("() => window.scrollY") - before < 8:
                break
        end = self.page.evaluate(
            "() => ({ y: window.scrollY, max: document.documentElement.scrollHeight"
            " - document.documentElement.clientHeight })"
        )
        shortfall = end["max"] - end["y"]
        if shortfall > max(80, end["max"] * 0.1):
            self._add("scroll", "major",
                      f"Repeated touch scrolling stalls {shortfall}px short of "
                      f"the bottom on {self.device_name} (reached "
                      f"{end['y']}/{end['max']}px) — something is intercepting "
                      f"the gesture.",
                      details={"reached": end["y"], "max": end["max"]})
        self._scroll_to_instant(0)
        self.page.wait_for_timeout(self.SETTLE_MS)

    @_tallied
    def check_horizontal_pan(self):
        """The page must not be pannable sideways.

        The layout auditor already reports that something is WIDER than the
        viewport. This is the interaction consequence — whether the user can
        actually drag the page off-centre, which is what makes overflow feel
        broken rather than merely clipped.
        """
        if not self._touch_scroll(dy=0, dx=200):
            self._add("h_pan", "info",
                      f"Horizontal pan could not be exercised on {self.engine} "
                      f"({self.device_name}): needs the Chromium CDP gesture "
                      f"API. UNTESTED, not substituted.")
            return
        x = self.page.evaluate("() => window.scrollX")
        if x > 4:
            self._add("h_pan", "major",
                      f"Page pans horizontally by {x}px on {self.device_name} — "
                      f"a sideways swipe moves the whole layout off-centre.",
                      details={"scroll_x": x})
            self.page.evaluate("() => window.scrollTo(0, window.scrollY)")

    # ── Sticky / fixed / floating ──────────────────────────────────────

    @_tallied
    def check_sticky_and_fixed(self):
        """Fixed elements must stay put while scrolling, and not eat the screen."""
        js = """
        () => [...document.querySelectorAll('body *')]
          .filter(el => {
            const p = getComputedStyle(el).position;
            if (p !== 'fixed' && p !== 'sticky') return false;
            const r = el.getBoundingClientRect();
            return r.width > 8 && r.height > 8;
          })
          .slice(0, 12)
          .map((el, idx) => {
            const r = el.getBoundingClientRect();
            // IDENTITY ACROSS TWO SAMPLES -- third attempt, and the reason the
            // first two failed is worth recording:
            //   1. tag+class matched two different class="" DIVs against each
            //      other -> phantom 1088px drift.
            //   2. index-among-fixed-elements broke as soon as the SET changed
            //      size: a back-to-top button appearing after scrolling shifts
            //      every later index by one, so element n was compared with
            //      element n+1 -> phantom 132px diagonal "drift" (66px in x AND
            //      y, which no fixed element does).
            // Stamping the node itself is positional- and count-independent.
            // An element that appears later has no stamp and is correctly
            // treated as new rather than silently matched to a neighbour.
            if (!el.dataset.fgFixedId) el.dataset.fgFixedId = 'fg' + idx + '-' +
              Math.round(r.width) + 'x' + Math.round(r.height);
            return { key: el.dataset.fgFixedId,
                     tag: el.tagName, cls: el.className?.toString().slice(0, 40) || '',
                     pos: getComputedStyle(el).position,
                     x: Math.round(r.x), y: Math.round(r.y),
                     w: Math.round(r.width), h: Math.round(r.height) };
          })
        """
        self._scroll_to_instant(0)
        self.page.wait_for_timeout(self.SETTLE_MS)
        before = self.page.evaluate(js)
        if not before:
            self._add("sticky", "info",
                      f"No fixed or sticky elements on {self.page_name} at "
                      f"{self.device_name} — nothing to verify while scrolling.")
            return

        vp_h = (self.page.viewport_size or {"height": 844})["height"]
        for el in before:
            if el["pos"] == "fixed" and el["h"] > vp_h * self.MAX_FIXED_VIEWPORT_FRACTION:
                pct = round(el["h"] / vp_h * 100)
                self._add("sticky", "minor",
                          f"Fixed {el['tag']} ('{el['cls']}') occupies {pct}% of "
                          f"the {vp_h}px viewport on {self.device_name}, leaving "
                          f"little room for content.",
                          selector=f"{el['tag']}.{el['cls']}",
                          details={"height": el["h"], "viewport_height": vp_h})

        if not self._touch_scroll(dy=vp_h):
            self._add("sticky", "info",
                      f"Fixed-element drift could not be exercised on "
                      f"{self.engine} ({self.device_name}): needs the Chromium "
                      f"CDP gesture API. UNTESTED.")
            return
        after = self.page.evaluate(js)

        for b in before:
            match = next((a for a in after if a["key"] == b["key"]), None)
            if match is not None and (
                match["pos"] != b["pos"]
                # A fixed element that changed size between samples is being
                # animated (the header's squeezelogo state does exactly this).
                # Comparing its origin then is meaningless.
                or abs(match["w"] - b["w"]) > 2 or abs(match["h"] - b["h"]) > 2
            ):
                match = None
            if match is None:
                # Disappearing on scroll is a common, intentional pattern
                # (hide-on-scroll headers), so this is informational.
                self._add("sticky", "info",
                          f"{b['pos'].title()} {b['tag']} ('{b['cls']}') is no "
                          f"longer present after scrolling on {self.device_name} "
                          f"— hide-on-scroll, or it was removed.")
                continue
            if b["pos"] != "fixed":
                continue          # sticky elements are SUPPOSED to move
            dy = abs(match["y"] - b["y"])
            dx = abs(match["x"] - b["x"])
            if max(dx, dy) > self.FIXED_DRIFT_TOLERANCE_PX:
                # dx and dy separately, never summed: "132px" for a 66+66
                # diagonal read as a big vertical drift and hid the fact that
                # no real fixed element moves diagonally.
                self._add("sticky", "major",
                          f"Fixed {b['tag']} ('{b['cls']}') moved while "
                          f"scrolling on {self.device_name}: dy={dy}px "
                          f"(y {b['y']}->{match['y']}), dx={dx}px "
                          f"(x {b['x']}->{match['x']}) — it is not holding "
                          f"position.",
                          selector=f"{b['tag']}.{b['cls']}",
                          details={"before": b, "after": match, "dx": dx, "dy": dy})

        # A floating element pinned over the bottom of the screen can cover
        # the last interactive row, which only shows up once scrolled down.
        #
        # CRITICAL REFINEMENT: a control sitting behind a .fixed-top header is
        # NOT a defect -- with a fixed header, content passes behind it on the
        # way past, and the user scrolls it clear. Flagging every such element
        # produced 11 phantom findings against the Flozic homepage (nav links,
        # combo buttons) that a user can tap without difficulty.
        #
        # The real question is "can the user get at this control at all?", so
        # each candidate is scrolled to the VERTICAL CENTRE of the viewport --
        # as far from a top- or bottom-anchored fixed element as it can get --
        # and only reported if it is STILL covered there.
        covered = self.page.evaluate("""
        () => {
          const fixed = [...document.querySelectorAll('body *')].filter(el => {
            const s = getComputedStyle(el);
            if (s.position !== 'fixed') return false;
            const r = el.getBoundingClientRect();
            return r.width > 8 && r.height > 8 && s.visibility !== 'hidden'
                   && s.pointerEvents !== 'none';
          });
          // A fixed element is only allowed to be "in the way" if it is not
          // simply the header the content scrolls behind. Identify candidates
          // first, then re-test each one centred in the viewport.
          const candidates = [];
          for (const el of document.querySelectorAll('a[href], button, input, select')) {
            const r = el.getBoundingClientRect();
            if (r.width < 4 || r.height < 4) continue;
            if (r.bottom < 0 || r.top > innerHeight) continue;
            const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            if (!top || el === top || el.contains(top) || top.contains(el)) continue;
            if (fixed.some(f => f === top || f.contains(top))) candidates.push(el);
          }

          const hits = [];
          const unverified = [];
          const startY = window.scrollY;
          const root = document.documentElement;
          const prevBehavior = root.style.scrollBehavior;
          root.style.scrollBehavior = 'auto';   // this page sets `smooth`
          for (const el of candidates.slice(0, 12)) {
            // Centre it: maximally clear of top- and bottom-pinned overlays.
            const r0 = el.getBoundingClientRect();
            const want = window.scrollY + r0.y + r0.height / 2 - innerHeight / 2;
            window.scrollTo({ top: want, left: 0, behavior: 'instant' });
            const r = el.getBoundingClientRect();
            // Did centring actually happen? If the element could not be moved
            // near the middle (page too short, scroll clamped, container
            // scrolls independently) then reachability was NOT tested, and
            // saying "unreachable" would be a fabricated verdict.
            const centreErr = Math.abs((r.y + r.height / 2) - innerHeight / 2);
            if (centreErr > innerHeight * 0.25) {
              unverified.push({ tag: el.tagName,
                label: (el.innerText || el.value || '').trim().slice(0, 30),
                centred_y: Math.round(r.y), centre_error: Math.round(centreErr) });
              continue;
            }
            if (r.width < 4 || r.height < 4) continue;
            const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            if (!top || el === top || el.contains(top) || top.contains(el)) continue;
            const blocker = fixed.find(f => f === top || f.contains(top));
            if (blocker) {
              // Still unreachable with the control dead-centre -- a real trap.
              hits.push({ tag: el.tagName,
                          label: (el.innerText || el.value || '').trim().slice(0, 30),
                          by: blocker.tagName + '.' +
                              (blocker.className?.toString().slice(0, 30) || ''),
                          centred_y: Math.round(r.y) });
            }
          }
          root.style.scrollBehavior = prevBehavior;
          window.scrollTo({ top: startY, left: 0, behavior: 'instant' });
          return { hits: hits.slice(0, 5), unverified: unverified.slice(0, 5) };
        }
        """)
        for u in (covered or {}).get("unverified", []):
            # Reported, never silent: an untested control must not read as a
            # passing one. But it is `info`, not `major` -- we do not know.
            self._add("sticky", "info",
                      f"Could not verify whether {u['tag']} '{u['label']}' is "
                      f"reachable on {self.device_name}: centring it left it "
                      f"{u['centre_error']}px off centre, so the overlap test "
                      f"never ran. NOT a finding either way.",
                      details=u)
        for h in (covered or {}).get("hits", []):
            self._add("sticky", "major",
                      f"{h['tag']} '{h['label']}' is unreachable on "
                      f"{self.device_name}: still covered by {h['by']} even "
                      f"when scrolled to the middle of the viewport, so a tap "
                      f"hits the overlay rather than the control.",
                      details=h)
        self._scroll_to_instant(0)

    # ── Menu ───────────────────────────────────────────────────────────

    @_tallied
    def check_menu_toggle(self):
        """The hamburger must open AND close, with items inside the viewport."""
        toggle = self._first_visible(MENU_TOGGLE)
        if toggle is None:
            self._add("menu", "info",
                      f"No mobile menu toggle found on {self.page_name} at "
                      f"{self.device_name} — nav may be inline at this width.")
            return

        def nav_state():
            return self.page.evaluate("""
            () => {
              const links = [...document.querySelectorAll('header a[href], nav a[href]')];
              const vis = links.filter(a => {
                const r = a.getBoundingClientRect();
                const s = getComputedStyle(a);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
                       && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05;
              });
              return { visible: vis.length,
                       offscreen: vis.filter(a => {
                         const r = a.getBoundingClientRect();
                         return r.right > innerWidth + 2 || r.left < -2;
                       }).length };
            }
            """)

        closed = nav_state()
        expanded_before = toggle.get_attribute("aria-expanded")
        try:
            toggle.tap()
        except Exception as e:
            self._add("menu", "major",
                      f"Menu toggle could not be tapped on {self.device_name}: "
                      f"{type(e).__name__}. A control that rejects a touch event "
                      f"is unusable on a phone.")
            return
        self.page.wait_for_timeout(self.SETTLE_MS)
        opened = nav_state()
        expanded_after = toggle.get_attribute("aria-expanded")

        aria_changed = (
            expanded_before is not None and expanded_after != expanded_before
        )
        revealed = opened["visible"] - closed["visible"]
        if not aria_changed and revealed <= 0:
            self._add("menu", "blocker",
                      f"Tapping the menu toggle on {self.device_name} changed "
                      f"nothing: {closed['visible']} nav links visible before, "
                      f"{opened['visible']} after, aria-expanded "
                      f"{expanded_before!r} -> {expanded_after!r}. The mobile "
                      f"navigation cannot be opened.",
                      details={"before": closed, "after": opened})
            return

        if opened["offscreen"]:
            self._add("menu", "major",
                      f"{opened['offscreen']} nav link(s) sit outside the "
                      f"viewport once the menu is open on {self.device_name} — "
                      f"unreachable without horizontal panning.",
                      details=opened)

        # Closing again matters as much as opening: a menu that covers the page
        # and cannot be dismissed is a trap.
        try:
            toggle.tap()
            self.page.wait_for_timeout(self.SETTLE_MS)
            reclosed = nav_state()
            expanded_end = toggle.get_attribute("aria-expanded")
            if reclosed["visible"] >= opened["visible"] and (
                expanded_before is None or expanded_end == expanded_after
            ):
                self._add("menu", "major",
                          f"Menu does not close on a second tap on "
                          f"{self.device_name} ({opened['visible']} links open, "
                          f"{reclosed['visible']} after re-tap) — no way back to "
                          f"the page.",
                          details={"open": opened, "after_retap": reclosed})
        except Exception:
            self._add("menu", "minor",
                      f"Menu opened but the toggle could not be tapped again on "
                      f"{self.device_name} — it may be covered by the open menu.")

    # ── Taps produce effects ───────────────────────────────────────────

    @_tallied
    def check_taps_have_effect(self, max_links: int = 3):
        """Tap real nav links and require an observable consequence."""
        # STAMP the elements JS selected, and address them by that stamp.
        # The previous version returned hrefs and re-located each one with
        # `locator("header a[href='/'], nav a[href='/']").first`, which for
        # href="/" matched several nodes and resolved to a HIDDEN one -- 11
        # "Link '' (/) rejected a tap: TimeoutError" majors on both engines,
        # from tapping an element the JS never chose. Sixth appearance of the
        # .first-over-an-OR-list trap in this project.
        #
        # Self-navigation is also excluded here: a link whose resolved href is
        # the current URL cannot change it, so "did not navigate" would be
        # meaningless. That is what href="/" on the / page was.
        handles = self.page.evaluate("""
        () => [...document.querySelectorAll('header a[href], nav a[href]')]
          .filter(a => {
            const r = a.getBoundingClientRect();
            const s = getComputedStyle(a);
            if (!(r.width > 8 && r.height > 8 && s.visibility !== 'hidden'
                  && r.top >= 0 && r.top < innerHeight)) return false;
            const raw = a.getAttribute('href');
            if (!raw || raw.startsWith('#')) return false;
            let resolved;
            try { resolved = new URL(a.href, location.href); } catch (e) { return false; }
            if (resolved.origin !== location.origin) return false;   // offsite
            // Same page (ignoring hash) => not a navigation target.
            if (resolved.pathname === location.pathname
                && resolved.search === location.search) return false;
            return true;
          })
          .slice(0, 8)
          .map((a, i) => {
            a.dataset.fgTapId = 'fgtap' + i;
            return { stamp: 'fgtap' + i,
                     href: a.getAttribute('href'),
                     text: (a.innerText || '').trim().slice(0, 30) };
          })
        """)
        if not handles:
            self._add("tap", "info",
                      f"No in-viewport navigation link on {self.page_name} at "
                      f"{self.device_name} points anywhere other than the "
                      f"current page — nothing to validate a tap against.")
            return

        start_url = self.page.url
        tested = 0
        for h in handles:
            if tested >= max_links:
                break
            # Unique by construction -- this is the exact node JS chose.
            loc = self.page.locator(f"[data-fg-tap-id='{h['stamp']}']").first
            try:
                if loc.count() != 1 or not loc.is_visible():
                    continue
                loc.tap()
            except Exception as e:
                self._add("tap", "major",
                          f"Link '{h['text']}' ({h['href']}) rejected a tap on "
                          f"{self.device_name}: {type(e).__name__}.")
                tested += 1
                continue
            self.page.wait_for_timeout(self.SETTLE_MS * 2)
            if self.page.url == start_url:
                self._add("tap", "major",
                          f"Tapping '{h['text']}' ({h['href']}) on "
                          f"{self.device_name} did not navigate — still at "
                          f"{start_url}. The link renders but does nothing to "
                          f"a touch.",
                          selector=f"a[href='{h['href']}']",
                          details={"href": h["href"]})
            else:
                self.page.go_back(wait_until="domcontentloaded")
                self.page.wait_for_timeout(self.SETTLE_MS)
            tested += 1

    # ── Forms ──────────────────────────────────────────────────────────

    @_tallied
    def check_form_interaction(self):
        """Tapping an input must focus it, and typing must land."""
        field = self._first_visible(TEXT_INPUT)
        if field is None:
            self._add("form", "info",
                      f"No visible text input on {self.page_name} at "
                      f"{self.device_name}.")
            return
        try:
            field.tap()
        except Exception as e:
            self._add("form", "major",
                      f"Text input rejected a tap on {self.device_name}: "
                      f"{type(e).__name__}.")
            return
        self.page.wait_for_timeout(200)

        focused = self.page.evaluate(
            "() => document.activeElement && document.activeElement.tagName"
        )
        if focused not in ("INPUT", "TEXTAREA"):
            self._add("form", "major",
                      f"Tapping a text input on {self.device_name} did not focus "
                      f"it (activeElement is {focused}) — the keyboard would not "
                      f"open on a real device.",
                      details={"active_element": focused})
            return

        probe = "flowguard"
        try:
            field.fill("")
            field.type(probe, delay=20)
        except Exception as e:
            self._add("form", "major",
                      f"Could not type into the focused input on "
                      f"{self.device_name}: {type(e).__name__}.")
            return
        got = field.input_value()
        if got != probe:
            self._add("form", "major",
                      f"Typed {len(probe)} characters into an input on "
                      f"{self.device_name} but it holds {len(got)} — input is "
                      f"being dropped or rewritten.",
                      details={"expected_len": len(probe), "actual_len": len(got)})
        field.fill("")

    # ── Carousel / swipe ───────────────────────────────────────────────

    @_tallied
    def check_swipe(self):
        """A carousel must respond to a horizontal swipe."""
        # Find a candidate that shows real carousel BEHAVIOUR, not just a
        # matching class name.
        car, evidence = None, None
        try:
            loc = self.page.locator(CAROUSEL)
            for i in range(min(loc.count(), 14)):
                el = loc.nth(i)
                try:
                    ev = el.evaluate(_CAROUSEL_EVIDENCE_JS)
                except Exception:
                    continue
                if ev and ev.get("is_carousel"):
                    car, evidence = el, ev
                    break
                if ev and ev.get("usable") and evidence is None:
                    evidence = ev          # remember the best near-miss
        except Exception:
            pass

        if car is None:
            near = ""
            if evidence:
                near = (f" Closest match <{evidence['cls']}> is "
                        f"{evidence['w']}x{evidence['h']}px, "
                        f"scrollWidth {evidence['scrollW']} vs clientWidth "
                        f"{evidence['clientW']}, overflow-x "
                        f"{evidence['overflowX']}, {evidence['controls']} "
                        f"control(s) — static markup, not a carousel.")
            self._add("swipe", "info",
                      f"No swipeable carousel on {self.page_name} at "
                      f"{self.device_name} — swipe NOT APPLICABLE." + near)
            return
        box = car.bounding_box()
        if not box or box["width"] < 40:
            self._add("swipe", "info", "Carousel found but has no usable box.")
            return

        def fingerprint():
            return car.evaluate("""
            el => ({ sl: Math.round(el.scrollLeft),
                     active: el.querySelectorAll(
                       '.active, [aria-selected="true"], [class*="active"]').length,
                     text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120) })
            """)

        before = fingerprint()
        if not self._touch_swipe(box, dx=-int(box["width"] * 0.6)):
            self._add("swipe", "info",
                      f"Swipe is UNSUPPORTED on {self.engine} "
                      f"({self.device_name}): it requires the Chromium CDP "
                      f"gesture API. Not substituted with a mouse drag or a "
                      f"synthetic TouchEvent (WebKit lacks the TouchEvent "
                      f"constructor), so swipe is UNTESTED on this engine.")
            return
        after = fingerprint()
        if before == after:
            self._add("swipe", "minor",
                      f"Carousel on {self.page_name} did not respond to a "
                      f"left swipe on {self.device_name} (scrollLeft "
                      f"{before['sl']}, content unchanged) — it may be "
                      f"arrow-only, which is awkward on touch.",
                      details={"before": before, "after": after})

    # ── Chat widget ────────────────────────────────────────────────────

    @_tallied
    def check_widget(self):
        """The chat/support widget: present, tappable, and reachable.

        REWRITTEN after the first live run. Two bugs, both producing false
        `minor` findings:

        1. It raced the widget. The iframe is injected long after
           domcontentloaded, so a 600ms wait found it only on whichever device
           happened to be slow enough -- reported as an iPad-Mini-only issue
           when the widget is on every device.
        2. It measured the WRONG DOCUMENT. Flozic's widget is a cross-origin
           iframe (widget.flozic.ai); its UI lives inside that frame. Tapping
           the iframe from the parent and then diffing the PARENT's
           innerHTML length can never detect anything, so "produced no
           detectable change" was guaranteed regardless of whether the widget
           works.

        What is checked now: the launcher exists within a bounded wait, is a
        usable tap target, sits inside the viewport, and its own frame has
        real content. Frame interaction goes through frame_locator, which
        crosses the origin boundary properly.
        """
        w = None
        try:
            loc = self.page.locator(WIDGET_LAUNCHER).first
            loc.wait_for(state="visible", timeout=WIDGET_LOAD_TIMEOUT_MS)
            w = loc
        except Exception:
            self._add("widget", "info",
                      f"No chat/support widget appeared within "
                      f"{WIDGET_LOAD_TIMEOUT_MS // 1000}s on {self.page_name} "
                      f"at {self.device_name} — nothing to test.")
            return

        vp = self.page.viewport_size or {"width": 390, "height": 844}
        box = w.bounding_box()
        if not box:
            self._add("widget", "info",
                      f"Widget found but has no box on {self.device_name}.")
            return

        # Screen-swallowing check.
        frac = (box["width"] * box["height"]) / (vp["width"] * vp["height"])
        if frac > 0.5:
            self._add("widget", "major",
                      f"Chat widget covers {round(frac * 100)}% of the "
                      f"{vp['width']}x{vp['height']} viewport on "
                      f"{self.device_name} before being opened.",
                      details={"box": box})

        # Tap-target size -- the launcher is a control, so the 44px rule applies.
        if min(box["width"], box["height"]) < self.MIN_TAP_TARGET_PX:
            self._add("widget", "minor",
                      f"Widget launcher is {round(box['width'])}x"
                      f"{round(box['height'])}px on {self.device_name}, under "
                      f"the {self.MIN_TAP_TARGET_PX}px minimum touch target.",
                      details={"box": box})

        # Reachable without horizontal panning.
        if box["x"] < -2 or box["x"] + box["width"] > vp["width"] + 2:
            self._add("widget", "major",
                      f"Widget launcher extends outside the {vp['width']}px "
                      f"viewport on {self.device_name} — unreachable without "
                      f"panning.",
                      details={"box": box})

        # Does the frame actually contain anything? A launcher rendering an
        # empty cross-origin frame is a real defect and is invisible from the
        # parent's DOM.
        try:
            body = self.page.frame_locator(WIDGET_IFRAME).locator("body").first
            body.wait_for(state="attached", timeout=4_000)
            inner = (body.inner_text() or "").strip()
            html_len = len(body.inner_html() or "")
            if html_len < 32:
                self._add("widget", "major",
                          f"Widget iframe is present but its document is "
                          f"essentially empty on {self.device_name} "
                          f"({html_len} chars of HTML) — the launcher renders "
                          f"nothing to interact with.",
                          details={"html_len": html_len})
            else:
                self._add("widget", "info",
                          f"Widget loaded on {self.device_name}: "
                          f"{round(box['width'])}x{round(box['height'])}px "
                          f"launcher, frame has {html_len} chars"
                          + (f", text {inner[:30]!r}" if inner else "")
                          + ". Cross-frame tap behaviour NOT exercised beyond "
                            "load — the widget UI is third-party.")
        except Exception as e:
            self._add("widget", "info",
                      f"Widget iframe content could not be inspected on "
                      f"{self.device_name} ({type(e).__name__}) — presence "
                      f"verified, contents NOT verified.")

    # ── Orientation ────────────────────────────────────────────────────

    @_tallied
    def check_orientation(self):
        """Rotating to landscape must not introduce horizontal overflow."""
        vp = self.page.viewport_size
        if not vp:
            self._add("orientation", "info", "No viewport size available.")
            return
        portrait = dict(vp)
        try:
            self.page.set_viewport_size(
                {"width": portrait["height"], "height": portrait["width"]}
            )
            self.page.wait_for_timeout(self.SETTLE_MS * 2)
            overflow = self.page.evaluate(
                "() => document.documentElement.scrollWidth"
                " - document.documentElement.clientWidth"
            )
            if overflow > 2:
                self._add("orientation", "major",
                          f"Rotating {self.device_name} to landscape "
                          f"({portrait['height']}x{portrait['width']}) leaves "
                          f"{overflow}px of horizontal overflow.",
                          details={"overflow_px": overflow,
                                   "landscape": [portrait["height"], portrait["width"]]})
            # Is the nav still reachable after rotation?
            reachable = self.page.evaluate("""
            () => [...document.querySelectorAll('header a[href], nav a[href], button')]
              .filter(el => {
                const r = el.getBoundingClientRect();
                return r.width > 4 && r.height > 4 && r.left >= -2
                       && r.right <= innerWidth + 2;
              }).length
            """)
            if reachable == 0:
                self._add("orientation", "major",
                          f"No interactive control is fully inside the viewport "
                          f"after rotating {self.device_name} to landscape.")
        finally:
            self.page.set_viewport_size(portrait)
            self.page.wait_for_timeout(self.SETTLE_MS)

    # ── Keyboard / focus zoom ──────────────────────────────────────────

    @_tallied
    def check_input_zoom(self):
        """Inputs must not trigger iOS focus-zoom, and focus must not be lost.

        WHAT THIS DOES AND DOES NOT TEST: Playwright emulation has no OS
        keyboard, so keyboard-induced viewport resize cannot be exercised
        here — that is a real-device concern and is NOT claimed. What is
        testable is the trigger condition Apple documents: focusing a control
        whose font-size is under 16px makes iOS Safari zoom the page. That is
        computed CSS, checked directly.

        Also flags `user-scalable=no` / `maximum-scale=1` used as a "fix":
        it does suppress the zoom, but by disabling pinch-zoom for everyone,
        which is a WCAG 1.4.4 failure worth knowing about.
        """
        offenders = self.page.evaluate("""
        () => {
          const out = [];
          for (const el of document.querySelectorAll(
              'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea, select')) {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            if (r.width < 8 || r.height < 8 || cs.visibility === 'hidden') continue;
            const fs = parseFloat(cs.fontSize || '16');
            if (fs < 16) out.push({
              tag: el.tagName, type: el.getAttribute('type') || '',
              name: el.getAttribute('name') || el.id || '',
              font_px: Math.round(fs * 10) / 10 });
          }
          return out.slice(0, 8);
        }
        """) or []
        meta = self.page.evaluate(
            "() => document.querySelector('meta[name=\"viewport\"]')?.content || ''"
        ) or ""
        zoom_suppressed = (
            "user-scalable=no" in meta.replace(" ", "")
            or "maximum-scale=1" in meta.replace(" ", "")
        )

        if not offenders:
            self._add("input_zoom", "info",
                      f"All visible form controls on {self.page_name} at "
                      f"{self.device_name} have font-size >= "
                      f"{self.MIN_INPUT_FONT_PX}px — no iOS focus-zoom trigger. "
                      f"(Keyboard-induced viewport resize itself needs a real "
                      f"device and is NOT tested here.)")
            return
        for o in offenders:
            sev = "minor"
            note = ""
            if zoom_suppressed:
                # The zoom won't fire, but only because pinch-zoom is disabled
                # for every user -- worth reporting, not worth `minor` twice.
                sev = "info"
                note = (" The viewport meta suppresses zoom "
                        "(user-scalable/maximum-scale), which avoids the jump "
                        "but disables pinch-zoom — a WCAG 1.4.4 concern.")
            # Risk-framed, not asserted as observed: this audit runs in
            # Chromium/WebKit emulation, never in real iOS Safari. Claiming
            # "makes iOS Safari zoom" as fact overstates the evidence.
            self._add("input_zoom", sev,
                      f"{o['tag']}[name='{o['name']}'] on {self.page_name} has "
                      f"font-size {o['font_px']}px on {self.device_name} — "
                      f"below the 16px threshold associated with iOS Safari's "
                      f"automatic focus-zoom (Apple-documented rule; "
                      f"physical-device behaviour was not verified in this run)."
                      + note,
                      details={**o, "zoom_suppressed": zoom_suppressed})

    # ── Runner ─────────────────────────────────────────────────────────

    # Insertion order IS execution order. `tap` is last because it is the only
    # check that navigates away; anything after it would audit a different
    # document.
    CHECK_NAMES = (
        "scroll", "h_pan", "sticky", "menu", "form", "swipe", "widget",
        "orientation", "input_zoom", "tap",
    )

    def _checks(self) -> dict:
        """name -> bound method, in execution order."""
        return {
            "scroll":      self.check_vertical_scroll,
            "h_pan":       self.check_horizontal_pan,
            "sticky":      self.check_sticky_and_fixed,
            "menu":        self.check_menu_toggle,
            "form":        self.check_form_interaction,
            "swipe":       self.check_swipe,
            "widget":      self.check_widget,
            "orientation": self.check_orientation,
            "input_zoom":  self.check_input_zoom,
            "tap":         self.check_taps_have_effect,
        }

    def run_all(self, include: Optional[set[str]] = None) -> list[Inconsistency]:
        """Run every check. A check that raises is recorded, never swallowed.

        `include` optionally narrows to a subset of check names, for pages
        where a whole class of interaction is meaningless.
        """
        # Executed-coverage tallying lives in @_tallied on the check methods
        # themselves (NOT here), so direct invocations by flow tests count
        # in the denominator too — see _tallied's docstring.
        for name, fn in self._checks().items():
            if include is not None and name not in include:
                continue
            try:
                fn()
            except Exception as e:
                # A crashed check is a gap in coverage. Recording it as a
                # finding is the difference between "we looked and it was fine"
                # and "we never looked".
                self._add(name, "minor",
                          f"Check '{name}' raised {type(e).__name__}: "
                          f"{str(e).splitlines()[0][:200]} on {self.device_name}. "
                          f"This interaction was NOT verified.",
                          details={"error": type(e).__name__})
        return self.findings

    # ── Helpers ────────────────────────────────────────────────────────

    def _first_visible(self, selector: str, timeout_ms: int = 2_000):
        """First visible match, or None. Never raises."""
        try:
            loc = self.page.locator(selector)
            n = min(loc.count(), 12)
            for i in range(n):
                el = loc.nth(i)
                try:
                    if el.is_visible(timeout=timeout_ms // max(n, 1)):
                        return el
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _add(self, category, severity, message, selector=None, details=None):
        self.findings.append(Inconsistency(
            page=self.page_name,
            device=self.device_name,
            category=category,
            severity=severity,
            message=message,
            selector=selector,
            details=details or {},
        ))
