"""
utils/mobile_layout_auditor.py

Selector-agnostic mobile-friendliness auditor for Flozic FlowGuard.

Why selector-agnostic: marketing/login/signup markup changes often, and a
mobile audit whose checks are hard-coded to `.btn-primary-xyz` breaks the
moment a class name changes. Every check here reads generic DOM/CSSOM
properties (bounding boxes, computed styles, viewport meta) instead, so the
same auditor runs unmodified against the homepage, pricing page, login
modal, or signup form.

Usage:
    auditor = MobileLayoutAuditor(page, page_name="homepage", device_name="iPhone 13")
    findings = auditor.run_all()
    # findings: list[Inconsistency]
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Inconsistency:
    page: str
    device: str
    category: str            # overflow | tap_target | font_size | viewport_meta | above_fold | cross_device
    severity: str             # blocker | major | minor
    message: str
    selector: Optional[str] = None
    details: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "page": self.page,
            "device": self.device,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "selector": self.selector,
            "details": self.details,
        }


class MobileLayoutAuditor:
    """Runs a battery of mobile-friendliness checks against an already-loaded page."""

    MIN_TAP_TARGET_PX = 44        # Apple HIG / Google Material minimum touch target
    MIN_READABLE_FONT_PX = 12
    MAX_ELEMENTS_SAMPLED = 80     # cap DOM walk for perf on large pages

    def __init__(self, page, page_name: str, device_name: str):
        self.page = page
        self.page_name = page_name
        self.device_name = device_name
        self.findings: list[Inconsistency] = []

    # ---------------------------------------------------------------- checks

    def check_viewport_meta(self):
        content = self.page.evaluate(
            "document.querySelector('meta[name=\"viewport\"]')?.content || null"
        )
        if not content:
            self._add(
                "viewport_meta", "blocker",
                "No <meta name=\"viewport\"> tag found — mobile browsers will "
                "render the desktop layout at full width and force users to "
                "pinch-zoom instead of getting a reflowed mobile layout.",
            )
        elif "width=device-width" not in content.replace(" ", ""):
            self._add(
                "viewport_meta", "major",
                f"Viewport meta tag is present but missing width=device-width: '{content}'",
            )

    def check_horizontal_overflow(self):
        overflow_px = self.page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        if overflow_px and overflow_px > 2:  # 2px slack for sub-pixel rounding
            self._add(
                "overflow", "major",
                f"Page scrolls horizontally by {overflow_px}px on {self.device_name} — "
                f"something is wider than the viewport.",
            )

    def check_fixed_width_offenders(self):
        """Finds specific elements wider than the viewport (root cause of overflow)."""
        vw = self.page.evaluate("window.innerWidth")
        # An element wider than the viewport is only a defect if the USER pays
        # for it: either the document scrolls sideways, or the element has no
        # horizontally-scrollable ancestor to live in. A wide table inside an
        # overflow-x container is the RECOMMENDED pattern — the marketing
        # repo's own smoke test (group 3b) asserts exactly that for the
        # Legal-Entity table. Reporting it as major produced 26 false majors
        # (cmp-table + its thead/tr/tbody x 6 devices) in the 2026-08-20 full
        # run and dragged the Mobile score to 40 on a page whose document
        # never scrolled horizontally.
        offenders = self.page.evaluate(
            """
            (vw) => {
              const docOverflow = document.documentElement.scrollWidth
                                  - document.documentElement.clientWidth;
              const els = Array.from(document.querySelectorAll('body *'));
              const out = [];
              for (const el of els) {
                const r = el.getBoundingClientRect();
                if (!(r.width > vw + 5 && r.height > 0)) continue;
                let contained = false;
                for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
                  const cs = getComputedStyle(a);
                  if (['auto', 'scroll', 'hidden'].includes(cs.overflowX)
                      && a.clientWidth <= vw + 5) { contained = true; break; }
                }
                out.push({tag: el.tagName,
                          cls: (el.className || '').toString().slice(0, 60),
                          w: Math.round(r.width), contained,
                          docOverflow: Math.round(docOverflow)});
                if (out.length >= 8) break;
              }
              return out;
            }
            """,
            vw,
        )
        for o in offenders:
            if o["contained"] and o["docOverflow"] <= 2:
                self._add(
                    "overflow", "info",
                    f"<{o['tag'].lower()}> (class=\"{o['cls']}\") is {o['w']}px wide "
                    f"but scrolls inside its own overflow-x container on "
                    f"{self.device_name} — the recommended pattern, not a defect.",
                    selector=o["cls"] or o["tag"],
                )
            else:
                self._add(
                    "overflow", "major",
                    f"<{o['tag'].lower()}> (class=\"{o['cls']}\") renders {o['w']}px wide "
                    f"against a {vw}px viewport on {self.device_name}"
                    + (f" and the document scrolls {o['docOverflow']}px sideways"
                       if o["docOverflow"] > 2 else " with no scrollable container")
                    + ".",
                    selector=o["cls"] or o["tag"],
                )

    def check_tap_targets(self):
        elements = self.page.evaluate(
            """
            (maxEls) => {
              const sel = 'a, button, input, select, [role="button"]';
              const els = Array.from(document.querySelectorAll(sel)).slice(0, maxEls);
              // Build a dev-greppable CSS selector: prefer #id, then a data-testid,
              // else tag + up to two classes (+ href hint for anchors). This is for
              // LOCATING the element in code, not a guaranteed-unique locator.
              const cssFor = (el) => {
                if (el.id) return '#' + el.id;
                const dt = el.getAttribute('data-testid') || el.getAttribute('data-test');
                if (dt) return `[data-testid="${dt}"]`;
                let s = el.tagName.toLowerCase();
                const cls = (el.className || '').toString().trim().split(/\\s+/).filter(Boolean).slice(0, 2);
                if (cls.length) s += '.' + cls.join('.');
                if (el.tagName === 'A' && el.getAttribute('href'))
                  s += `[href="${el.getAttribute('href').slice(0, 60)}"]`;
                return s;
              };
              return els.map(el => {
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                const visible = style.display !== 'none' && style.visibility !== 'hidden' && r.width > 0 && r.height > 0;
                return {tag: el.tagName, text: (el.innerText || el.value || '').trim().slice(0, 40),
                        w: r.width, h: r.height, visible, sel: cssFor(el).slice(0, 120)};
              });
            }
            """,
            self.MAX_ELEMENTS_SAMPLED,
        )
        for el in elements:
            if not el["visible"]:
                continue
            if el["w"] < self.MIN_TAP_TARGET_PX or el["h"] < self.MIN_TAP_TARGET_PX:
                self._add(
                    "tap_target", "minor",
                    f"{el['tag']} '{el['text']}' is {el['w']:.0f}x{el['h']:.0f}px on "
                    f"{self.device_name} — below the {self.MIN_TAP_TARGET_PX}px recommended "
                    f"minimum touch-target size.",
                    selector=el.get("sel"),
                )

    def check_font_sizes(self):
        small_count = self.page.evaluate(
            """
            (minPx) => {
              const els = Array.from(document.querySelectorAll('p, span, a, li, label, button, h1, h2, h3'));
              return els.filter(el => {
                const size = parseFloat(getComputedStyle(el).fontSize);
                return size > 0 && size < minPx && el.innerText && el.innerText.trim().length > 0;
              }).length;
            }
            """,
            self.MIN_READABLE_FONT_PX,
        )
        if small_count > 0:
            self._add(
                "font_size", "minor",
                f"{small_count} text elements render below {self.MIN_READABLE_FONT_PX}px on "
                f"{self.device_name} — likely unreadable without pinch-zoom.",
            )

    def check_above_the_fold(self, selector: str, label: str):
        """
        Flags a critical element (e.g. the login submit button, or signup CTA)
        that requires scrolling to reach on this device — a common mobile-only
        regression that never shows up on desktop QA.
        """
        try:
            box = self.page.locator(selector).first.bounding_box(timeout=3000)
        except Exception:
            box = None
        if box is None:
            self._add(
                "above_fold", "major",
                f"Could not locate '{label}' ({selector}) on {self.device_name} — "
                f"it may be hidden, unrendered, or the selector no longer matches.",
                selector=selector,
            )
            return
        viewport_h = self.page.evaluate("window.innerHeight")
        if box["y"] > viewport_h:
            self._add(
                "above_fold", "minor",
                f"'{label}' sits {box['y']:.0f}px down on a {viewport_h}px-tall viewport "
                f"on {self.device_name} — user must scroll before they can act.",
                selector=selector,
            )

    def run_all(self, above_fold_targets: Optional[list[tuple[str, str]]] = None):
        """
        above_fold_targets: optional list of (selector, label) for page-specific
        critical actions (submit button, primary CTA) to above-the-fold check.
        """
        self.check_viewport_meta()
        self.check_horizontal_overflow()
        self.check_fixed_width_offenders()
        self.check_tap_targets()
        self.check_font_sizes()
        for selector, label in (above_fold_targets or []):
            self.check_above_the_fold(selector, label)
        return self.findings

    # --------------------------------------------------------------- helpers

    def _add(self, category, severity, message, selector=None, details=None):
        self.findings.append(
            Inconsistency(
                page=self.page_name,
                device=self.device_name,
                category=category,
                severity=severity,
                message=message,
                selector=selector,
                details=details or {},
            )
        )
