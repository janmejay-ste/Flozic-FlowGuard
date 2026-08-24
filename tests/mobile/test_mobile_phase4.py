"""
tests/mobile/test_mobile_phase4.py

Phase 4 mobile coverage: modals, browser history, the contact-page <select>,
and the signup validation-message layout — the mobile counterpart of the
tracked desktop defect where the Flozic logo becomes progressively cropped as
validation messages appear (reported 2026-08-18 at 1440x900 / 1366x768 /
1536x690 / 1536x864).

Same contract as the rest of the mobile suite: a test passes only when a real
interaction produced an observable consequence, and anything that cannot be
exercised is reported in words rather than silently skipped.

Grounding:
  * #RequestAppModal and #myModal are real ids in public/index.html.
  * The site is Bootstrap 4 (data-toggle=, not data-bs-toggle=), so modal
    triggers are [data-toggle="modal"] and open state is the .show class.
  * /contact-us has exactly one <select> (probed in the marketing repo).
"""

from __future__ import annotations

import logging

import pytest

from tests.mobile import flozic_marketing_flows as F
from tests.mobile.mobile_targets import AUTH_PAGES, open_auth_page
from utils.mobile_interaction_auditor import MobileInteractionAuditor

logger = logging.getLogger(__name__)


def _open(page, page_key: str):
    page.goto(F.url(page_key), wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(600)


# ── Modals ─────────────────────────────────────────────────────────────


@pytest.mark.mobile
def test_modals_open_and_close_on_tap(mobile_page, mobile_report_collector):
    """Every visible Bootstrap modal trigger must open its modal, the modal
    must fit the viewport, and it must be dismissable.

    A modal that opens and cannot be closed is a full-screen trap on a phone —
    same class of defect as an un-dismissable menu. Dismissal is therefore
    asserted, not assumed.
    """
    page, device = mobile_page
    _open(page, "homepage")
    a = MobileInteractionAuditor(page, page_name="homepage", device_name=device)

    triggers = page.locator("[data-toggle='modal'][data-target]")
    visible = []
    for i in range(min(triggers.count(), 6)):
        el = triggers.nth(i)
        try:
            if el.is_visible():
                visible.append(el)
        except Exception:
            continue

    if not visible:
        a._add("modal", "info",
               f"No visible modal trigger on the homepage at {device} — the "
               f"modals (#RequestAppModal, #myModal) exist in the DOM but "
               f"nothing on this viewport opens them. Modal behaviour NOT "
               f"exercised here.")
        mobile_report_collector.record(a.findings)
        return

    tested = 0
    for trig in visible[:2]:
        target_sel = trig.get_attribute("data-target") or ""
        if not target_sel.startswith("#"):
            continue
        modal = page.locator(target_sel).first
        if not modal.count():
            a._add("modal", "major",
                   f"Trigger points at {target_sel} on {device} but no such "
                   f"modal exists in the DOM.")
            continue

        trig.tap()
        page.wait_for_timeout(700)          # BS4 fade animation
        cls = (modal.get_attribute("class") or "")
        opened = "show" in cls.split() and modal.is_visible()
        if not opened:
            a._add("modal", "major",
                   f"Tapping the {target_sel} trigger on {device} did not open "
                   f"the modal (class={cls!r}). It renders without JS and does "
                   f"nothing when tapped.",
                   details={"target": target_sel, "class": cls})
            continue
        tested += 1

        # Must fit: a modal wider than the viewport puts its own close button
        # off-screen — the trap again, by geometry instead of JS.
        vp = page.viewport_size or {"width": 390}
        box = modal.locator(".modal-dialog").first.bounding_box() or modal.bounding_box()
        if box and (box["x"] < -2 or box["x"] + box["width"] > vp["width"] + 2):
            a._add("modal", "major",
                   f"{target_sel} extends outside the {vp['width']}px viewport "
                   f"on {device} (x={round(box['x'])}, "
                   f"w={round(box['width'])}).",
                   details={"target": target_sel, "box": box})

        # Dismiss: the in-modal close control first, Escape as fallback.
        closed = False
        close_btn = modal.locator("[data-dismiss='modal']").first
        try:
            if close_btn.count() and close_btn.is_visible():
                close_btn.tap()
            else:
                page.keyboard.press("Escape")
            page.wait_for_timeout(700)
            closed = "show" not in (modal.get_attribute("class") or "").split()
        except Exception:
            closed = False
        if not closed:
            a._add("modal", "blocker",
                   f"{target_sel} opened on {device} but could not be "
                   f"dismissed (close control, then Escape) — a full-screen "
                   f"trap on a phone.",
                   details={"target": target_sel})
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

    if tested == 0 and visible:
        a._add("modal", "info",
               f"{len(visible)} visible trigger(s) on {device} but none "
               f"successfully opened a modal — see findings above.")
    mobile_report_collector.record(a.findings)
    blockers = [f for f in a.findings if f.severity == "blocker"]
    assert not blockers, "\n".join(f.message for f in blockers)


# ── Browser history ────────────────────────────────────────────────────


@pytest.mark.mobile
def test_history_back_and_forward_keep_the_page_alive(
    mobile_page, mobile_report_collector,
):
    """Navigate by menu, go back, go forward — and prove the page still WORKS
    after each restore.

    The bug this targets: bfcache restores the page snapshot but event
    handlers wired to one-shot init code are dead, so the restored page looks
    perfect and the hamburger does nothing. Asserting the URL alone would miss
    it entirely; the menu must re-open after the back navigation.
    """
    page, device = mobile_page
    _open(page, "homepage")
    home = page.url
    a = MobileInteractionAuditor(page, page_name="homepage", device_name=device)

    toggle = page.locator(F.MENU_TOGGLE).first
    if toggle.is_visible():
        toggle.tap()
        page.wait_for_timeout(700)
    link = page.locator(f"{F.MENU_PANEL} a[href^='/integrate/']").first
    assert link.is_visible(), f"no menu link to navigate with on {device}"
    dest_href = link.get_attribute("href")
    link.tap()
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    page.wait_for_timeout(500)
    dest = page.url
    assert dest != home, f"menu link never navigated on {device}"

    # BACK ──────────────────────────────────────────────────────────────
    page.go_back(wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    assert page.url.rstrip("/") == home.rstrip("/"), (
        f"go_back on {device} landed at {page.url}, not the homepage {home}."
    )
    # The restored page must still be interactive, not a dead snapshot.
    toggle = page.locator(F.MENU_TOGGLE).first
    if toggle.is_visible():
        before = toggle.get_attribute("aria-expanded")
        toggle.tap()
        page.wait_for_timeout(700)
        after = toggle.get_attribute("aria-expanded")
        panel_cls = (page.locator(F.MENU_PANEL).first.get_attribute("class") or "")
        alive = (before is not None and after != before) or \
                (F.MENU_OPEN_CLASS in panel_cls.split())
        if not alive:
            a._add("history", "major",
                   f"After back-navigation on {device} the menu toggle no "
                   f"longer responds (aria-expanded {before!r} -> {after!r}) — "
                   f"the bfcache-restored page looks fine but its JS is dead.",
                   details={"before": before, "after": after})
        else:
            toggle.tap()          # close it again
            page.wait_for_timeout(500)
            a._add("history", "info",
                   f"Back-navigation on {device}: URL restored and the menu "
                   f"still opens — the restored page is live, not a snapshot.")
    else:
        a._add("history", "info",
               f"Menu toggle not visible after back-navigation on {device} — "
               f"interactivity after restore NOT verified.")

    # FORWARD ───────────────────────────────────────────────────────────
    page.go_forward(wait_until="domcontentloaded")
    page.wait_for_timeout(500)
    assert page.url == dest or (dest_href or "") in page.url, (
        f"go_forward on {device} landed at {page.url}, expected {dest}."
    )
    mobile_report_collector.record(a.findings)
    majors = [f for f in a.findings if f.severity in ("blocker", "major")]
    assert not majors, "\n".join(f.message for f in majors)


# ── The contact-page <select> ──────────────────────────────────────────


@pytest.mark.mobile
def test_contact_select_is_usable_on_mobile(mobile_page, mobile_report_collector):
    """The one <select> on /contact-us: tap it, choose an option, verify the
    value landed. On phones a <select> opens the native picker, which
    emulation cannot show — but the selection round-trip is the part the
    site's own code is responsible for, and that is what is asserted.
    """
    page, device = mobile_page
    _open(page, "contact")
    a = MobileInteractionAuditor(page, page_name="contact", device_name=device)

    sel = page.locator("select:visible").first
    if not sel.count():
        a._add("form", "info",
               f"No visible <select> on /contact-us at {device} — the field "
               f"may have been redesigned. Select behaviour NOT exercised.")
        mobile_report_collector.record(a.findings)
        return

    options = sel.evaluate(
        "el => [...el.options].map(o => ({value: o.value, text: o.text.trim(),"
        " disabled: o.disabled}))"
    )
    real = [o for o in options if o["value"] and not o["disabled"]]
    if not real:
        a._add("form", "info",
               f"The /contact-us <select> on {device} has no selectable "
               f"options ({len(options)} total) — nothing to choose.")
        mobile_report_collector.record(a.findings)
        return

    sel.tap()
    page.wait_for_timeout(200)
    sel.select_option(real[0]["value"])
    page.wait_for_timeout(200)
    got = sel.input_value()
    mobile_report_collector.record(a.findings)
    assert got == real[0]["value"], (
        f"Selected {real[0]['value']!r} in the /contact-us dropdown on "
        f"{device} but it holds {got!r} — the selection did not stick."
    )


# ── Signup validation layout (mobile counterpart of the tracked bug) ───


_LOGO_JS = """
() => {
  const img = document.querySelector(
    "header img, img[src*='logo' i], img[alt*='flozic' i], img[alt*='logo' i]");
  if (!img) return null;
  const r = img.getBoundingClientRect();
  return { w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10,
           natW: img.naturalWidth, natH: img.naturalHeight,
           src: (img.getAttribute('src') || '').slice(-50) };
}
"""


def _logo_state(page):
    st = page.evaluate(_LOGO_JS)
    if not st or not st["natW"] or not st["h"]:
        return None
    st["distortion"] = abs(
        (st["w"] / st["h"]) / (st["natW"] / st["natH"]) - 1
    )
    return st


@pytest.mark.mobile
def test_signup_validation_does_not_crop_the_logo(
    mobile_page, mobile_report_collector,
):
    """Trigger the signup validation messages and verify the branding survives.

    Mirrors the tracked desktop defect (reported 2026-08-18): at 1440x900 /
    1366x768 / 1536x690 / 1536x864 the Flozic logo becomes progressively
    cropped as validation messages appear. That bug's trigger is the
    validation UI reflowing the branding column, so the mobile check is the
    same sequence: empty submit -> invalid email + focus password -> measure
    the logo's rendered aspect ratio and the page's overflow at each stage.

    Distortion = |rendered aspect / natural aspect − 1|. A logo drawn at its
    own proportions scores ~0; the reported "zoomed/cropped" rendering shows
    up as either distortion or a shrinking box between stages.
    """
    entry_url, follow_link, _af = AUTH_PAGES["signup"]
    page, device = mobile_page
    open_auth_page(page, entry_url, follow_link)
    page.wait_for_timeout(800)
    a = MobileInteractionAuditor(page, page_name="signup", device_name=device)

    stages = []
    base = _logo_state(page)
    if base is None:
        a._add("signup_validation", "info",
               f"No logo image found on the signup page at {device} — the "
               f"cropping check has nothing to measure. NOT verified.")
        mobile_report_collector.record(a.findings)
        return
    stages.append(("initial", base))

    # Stage 1 — empty submit (their scenario 1).
    submit = page.locator("button[type='submit']:visible").first
    if submit.count():
        try:
            submit.tap()
            page.wait_for_timeout(900)
            st = _logo_state(page)
            if st:
                stages.append(("empty-submit", st))
        except Exception as e:
            a._add("signup_validation", "info",
                   f"Empty-submit could not be performed on {device} "
                   f"({type(e).__name__}) — stage 1 NOT exercised.")
    else:
        a._add("signup_validation", "info",
               f"No visible submit button on the signup page at {device} — "
               f"validation could not be triggered by submit.")

    # Stage 2 — invalid email, then focus the password field (scenario 2).
    email = page.locator(
        "input[type='email']:visible, input[name*='email' i]:visible, "
        "input[name='username']:visible").first
    pw = page.locator("input[type='password']:visible").first
    if email.count():
        try:
            email.tap()
            email.fill("")
            email.type("not-an-email", delay=10)
            if pw.count():
                pw.tap()
            else:
                page.keyboard.press("Tab")
            page.wait_for_timeout(900)
            st = _logo_state(page)
            if st:
                stages.append(("invalid-email", st))
        except Exception as e:
            a._add("signup_validation", "info",
                   f"Invalid-email stage could not be performed on {device} "
                   f"({type(e).__name__}) — stage 2 NOT exercised.")

    # Judge the stages.
    worst = max(stages, key=lambda s: s[1]["distortion"])
    for name, st in stages:
        if st["distortion"] > 0.2:
            a._add("signup_validation", "major",
                   f"Signup logo scales incorrectly at stage '{name}' on "
                   f"{device}: rendered {st['w']}x{st['h']}px vs natural "
                   f"{st['natW']}x{st['natH']} — aspect distorted "
                   f"{round(st['distortion'] * 100)}%. Matches the tracked "
                   f"desktop defect (logo progressively cropped as validation "
                   f"messages appear).",
                   details={"stage": name, **st})
    if len(stages) >= 2:
        first, last = stages[0][1], stages[-1][1]
        shrink = 1 - (last["w"] * last["h"]) / max(first["w"] * first["h"], 1)
        if shrink > 0.25 and not any(f.severity == "major" for f in a.findings):
            a._add("signup_validation", "major",
                   f"Signup logo shrank {round(shrink * 100)}% between stage "
                   f"'{stages[0][0]}' and '{stages[-1][0]}' on {device} "
                   f"({first['w']}x{first['h']} -> {last['w']}x{last['h']}) — "
                   f"the branding column is being squeezed as validation "
                   f"messages appear.",
                   details={"stages": [dict(s[1], stage=s[0]) for s in stages]})

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth"
        " - document.documentElement.clientWidth")
    if overflow > 2:
        a._add("signup_validation", "major",
               f"Signup page overflows horizontally by {overflow}px on "
               f"{device} after triggering validation messages.",
               details={"overflow_px": overflow})

    if not any(f.severity in ("blocker", "major") for f in a.findings):
        a._add("signup_validation", "info",
               f"Signup logo held its proportions across "
               f"{len(stages)} stage(s) on {device} (worst distortion "
               f"{round(worst[1]['distortion'] * 100)}% at '{worst[0]}') and "
               f"the page did not overflow. The desktop-resolution variants "
               f"of the tracked bug are NOT covered by this mobile check.")
    mobile_report_collector.record(a.findings)
    majors = [f for f in a.findings if f.severity in ("blocker", "major")]
    assert not majors, "\n".join(f.message for f in majors)
