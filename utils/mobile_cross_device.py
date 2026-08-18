"""
utils/mobile_cross_device.py

Single-device checks (mobile_layout_auditor.py) catch a page that's broken
on its own. This module catches the other class of bug this project exists
to find: the SAME page rendering *inconsistently* across different mobile
devices — e.g. the nav collapses to a hamburger on an iPhone 13 but stays
as a cramped overflowing top-bar on a Galaxy S9+, or a CTA button is
reachable on an iPhone SE but pushed off-screen on a Pixel 5.

A snapshot is a small, cheap-to-compute fingerprint of "what the page looks
like" on one device. Comparing snapshots across devices for the same page
surfaces the inconsistencies a single-device run can never see.
"""
from dataclasses import dataclass
from utils.mobile_layout_auditor import Inconsistency


@dataclass
class DeviceSnapshot:
    device: str
    nav_link_count: int
    nav_is_collapsed: bool          # hamburger/off-canvas vs inline nav
    visible_cta_texts: list[str]
    heading_count: int
    form_field_count: int


def capture_snapshot(page, device_name: str) -> DeviceSnapshot:
    data = page.evaluate(
        """
        () => {
          const navLinks = Array.from(document.querySelectorAll('nav a, header a'))
            .filter(a => a.getBoundingClientRect().width > 0);
          const hamburger = document.querySelector(
            '[aria-label*="menu" i], [class*="hamburger" i], [class*="mobile-menu" i], [class*="nav-toggle" i]'
          );
          const ctas = Array.from(document.querySelectorAll('a, button'))
            .filter(el => {
              const r = el.getBoundingClientRect();
              return r.width > 0 && r.height > 0 && /try|sign\\s?up|get started|buy|login|log in/i.test(el.innerText || '');
            })
            .map(el => el.innerText.trim().slice(0, 30));
          const headings = document.querySelectorAll('h1, h2, h3').length;
          const fields = document.querySelectorAll('input, textarea, select').length;
          return {
            navLinkCount: navLinks.length,
            navCollapsed: !!hamburger,
            ctas,
            headingCount: headings,
            fieldCount: fields,
          };
        }
        """
    )
    return DeviceSnapshot(
        device=device_name,
        nav_link_count=data["navLinkCount"],
        nav_is_collapsed=data["navCollapsed"],
        visible_cta_texts=sorted(set(data["ctas"])),
        heading_count=data["headingCount"],
        form_field_count=data["fieldCount"],
    )


def compare_snapshots(page_name: str, snapshots: list[DeviceSnapshot]) -> list[Inconsistency]:
    """
    All entries here are mobile devices — differences are expected to be
    minor reflow, not missing content. Flags real divergence.
    """
    findings: list[Inconsistency] = []
    if len(snapshots) < 2:
        return findings

    baseline = snapshots[0]
    for snap in snapshots[1:]:
        # A CTA visible on one mobile device and silently absent on another
        # is almost always a bug (cut off, mis-positioned, or z-index issue),
        # not intentional responsive design.
        missing_ctas = set(baseline.visible_cta_texts) - set(snap.visible_cta_texts)
        extra_ctas = set(snap.visible_cta_texts) - set(baseline.visible_cta_texts)
        if missing_ctas:
            findings.append(Inconsistency(
                page=page_name, device=snap.device, category="cross_device", severity="major",
                message=(
                    f"CTA(s) {sorted(missing_ctas)} are visible on {baseline.device} but "
                    f"missing on {snap.device} — check for cut-off, overlap, or hidden overflow."
                ),
            ))
        if extra_ctas:
            findings.append(Inconsistency(
                page=page_name, device=snap.device, category="cross_device", severity="minor",
                message=(
                    f"CTA(s) {sorted(extra_ctas)} appear on {snap.device} but not on "
                    f"{baseline.device} — confirm this divergence is intentional."
                ),
            ))

        # Nav should behave consistently across phone-class devices; a nav
        # that's collapsed on one phone and inline (likely overflowing) on
        # another usually means a breakpoint was set against one device's
        # width and never verified on the rest.
        if baseline.nav_is_collapsed != snap.nav_is_collapsed:
            findings.append(Inconsistency(
                page=page_name, device=snap.device, category="cross_device", severity="major",
                message=(
                    f"Nav is {'collapsed' if baseline.nav_is_collapsed else 'inline'} on "
                    f"{baseline.device} but {'collapsed' if snap.nav_is_collapsed else 'inline'} "
                    f"on {snap.device} — likely a breakpoint gap between these viewport widths."
                ),
            ))

        if baseline.heading_count != snap.heading_count:
            findings.append(Inconsistency(
                page=page_name, device=snap.device, category="cross_device", severity="minor",
                message=(
                    f"Heading count differs: {baseline.heading_count} on {baseline.device} vs "
                    f"{snap.heading_count} on {snap.device} — content may be dropped on one device."
                ),
            ))

        if baseline.form_field_count != snap.form_field_count:
            findings.append(Inconsistency(
                page=page_name, device=snap.device, category="cross_device", severity="blocker",
                message=(
                    f"Form field count differs: {baseline.form_field_count} on {baseline.device} vs "
                    f"{snap.form_field_count} on {snap.device} — a form field may be missing/inaccessible."
                ),
            ))

    return findings
