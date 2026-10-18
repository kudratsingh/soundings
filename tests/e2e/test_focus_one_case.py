"""WO-060D browser smoke: the screen follows one case from start to finish.

Opt-in (``SOUNDINGS_E2E=1``), offline (every non-loopback request aborted), tiles off.
S05 seed 11 at 20x, 1440x900:

* the focus mode badge starts at ``sticky``;
* the first case that passes ASK becomes the focus and stays the focus (sampled every 50 ms
  in the page) through HOLDING and HANDED_OFF while other cases appear in the cases list;
* other contacts on the map are drawn at reduced opacity (``data-dim=1``), the focused one
  is not;
* the trace line at the top of the case view ends with HANDED_OFF when the handoff lands
  (or HANDED_OFF → WATCH_AFTER_HANDOFF when the watch after it starts in the same tick),
  and every reached step card shows the transition that entered it and its row count;
* at the hold, a click on another row pins it (``focus: pinned``) and the badge returns the
  focus to the hero (``sticky``);
* cumulative layout shift stays under 0.05 while rows stream (input-driven shifts excluded,
  as the design pass measured).

Screenshots: ``artifacts/focus-sticky.png`` (the hold, focus on the hero) and
``artifacts/focus-pinned.png`` (the same moment, another case pinned).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tests.e2e._judge import offline
from tests.e2e._quiet import open_others

REPO = Path(__file__).resolve().parents[2]
ART = REPO / "artifacts"

PAST_ASK = [
    "INVESTIGATING",
    "ADVISING_SLOW",
    "HOLDING",
    "AWAITING_APPROVAL",
    "HANDED_OFF",
    "WATCH_AFTER_HANDOFF",
    "RESOLVED_BENIGN",
    "HAZARD_NOTICE",
    "FAILED",
]

ENDS_HANDED_OFF = re.compile(r"HANDED_OFF( t \d+ s)?( → WATCH_AFTER_HANDOFF( t \d+ s)?)?$")
CLIMBED = ["ADVISING_SLOW", "HOLDING", "AWAITING_APPROVAL", "HANDED_OFF", "WATCH_AFTER_HANDOFF"]

PROBE = """
(() => {
  window.__perf = { cls: 0, shifts: 0, top: [] };
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (!e.hadRecentInput) {
          window.__perf.cls += e.value; window.__perf.shifts += 1;
          const name = (x) => !x.node ? '?' : x.node.className
            ? String(x.node.className.baseVal ?? x.node.className).slice(0, 40) : x.node.nodeName;
          window.__perf.top.push(
            [Math.round(e.value * 10000) / 10000, (e.sources || []).map(name).join('|')]);
        }
      }
    }).observe({ type: 'layout-shift', buffered: true });
  } catch (err) { window.__perf.noLayoutShift = String(err); }
  // Sample the focused case and the trace's last state every 50 ms.
  window.__focusLog = [];
  window.__sampling = true;
  setInterval(() => {
    if (!window.__sampling) return;
    const v = document.querySelector('[data-testid=case-view]');
    const tr = document.querySelector('[data-testid=case-trace]');
    const c = v ? v.getAttribute('data-case') : '';
    const s = tr ? tr.getAttribute('data-last-state') : '';
    const last = window.__focusLog[window.__focusLog.length - 1];
    if (!last || last[0] !== c || last[1] !== s) window.__focusLog.push([c, s]);
  }, 50);
})();
"""


def _focused(page: Any) -> str:
    return page.locator("[data-testid=case-view]").get_attribute("data-case") or ""


def test_s05_focus_stays_on_the_first_case_past_ask(live_server: str, browser: Any) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.add_init_script(PROBE)
    page.route("**/*", offline)
    page.goto(f"{live_server}/?scenario=S05&seed=11&speed=20&tiles=off")
    page.wait_for_selector("[data-testid=focus-mode][data-mode=sticky]", timeout=30_000)

    # The first case past ask that is still the story (S05: a benign investigation opens and
    # clears before the hero is asked; it does not hold the focus) becomes the focus once it
    # climbs or is handed off, and nothing moves it from then on.
    climbed = ",".join(f"[data-testid=case-row][data-state={s}]" for s in CLIMBED)
    page.wait_for_selector(climbed, timeout=180_000)
    hero = _focused(page)
    hero_row = page.locator(f"[data-testid=case-row][data-case='{hero}']")
    assert hero_row.get_attribute("data-focused") == "1"
    assert hero_row.get_attribute("data-state") in PAST_ASK, hero_row.get_attribute("data-state")
    start = len(page.evaluate("() => window.__focusLog")) - 1
    # CLS while rows stream, from here on (as the design pass measured: after the first paint).
    load_cls = page.evaluate("() => window.__perf.cls")
    load_top = page.evaluate(
        "() => window.__perf.top.slice().sort((a, b) => b[0] - a[0]).slice(0, 6)"
    )
    page.evaluate(
        "() => { window.__perf.cls = 0; window.__perf.shifts = 0; window.__perf.top = [] }"
    )

    # The hold: the sticky screenshot, then pin another case, then back to sticky.
    page.wait_for_selector(
        f"[data-testid=case-row][data-case='{hero}'][data-state=HOLDING],"
        f" [data-testid=case-row][data-case='{hero}'][data-state=AWAITING_APPROVAL]",
        timeout=180_000,
    )
    # WO-060E: the other cases are folded behind `others · n` (opened before the pin below)
    page.wait_for_selector("[data-testid=others-row]", timeout=60_000)
    assert _focused(page) == hero
    dims = page.locator("[data-testid=contact-marker]").evaluate_all(
        "els => els.map(e => [e.getAttribute('data-contact'), e.getAttribute('data-dim')])"
    )
    members = (
        page.locator(f"[data-testid=case-row][data-case='{hero}']").get_attribute("data-members")
        or hero
    ).split(",")
    print("markers (contact, dim):", dims, "hero members:", members)
    assert all(d == "0" for cid, d in dims if cid in members)
    assert all(d == "1" for cid, d in dims if cid not in members)
    entered = page.locator(
        "[data-testid=case-view] [data-testid=step][data-status=done] [data-testid=step-entered]"
    ).all_inner_texts()
    print("entered lines:", entered)
    assert entered and all("evidence row" in t for t in entered)
    assert any("→ ASKING" in t for t in entered)
    ART.mkdir(exist_ok=True)
    page.screenshot(path=str(ART / "focus-sticky.png"))

    page.evaluate("() => { window.__sampling = false }")
    open_others(page)  # WO-060E
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=case-row]').length >= 2", timeout=10_000
    )
    rows = page.locator("[data-testid=case-row]")
    other = ""
    for i in range(rows.count()):
        cid = rows.nth(i).get_attribute("data-case") or ""
        if cid != hero:
            other = cid
            rows.nth(i).click()
            break
    assert other
    page.wait_for_selector("[data-testid=focus-mode][data-mode=pinned]", timeout=5_000)
    page.wait_for_function(
        "c => document.querySelector('[data-testid=case-view]')?.getAttribute('data-case') === c",
        arg=other,
        timeout=5_000,
    )
    page.wait_for_timeout(300)
    page.screenshot(path=str(ART / "focus-pinned.png"))
    page.click("[data-testid=focus-mode]")
    page.wait_for_selector("[data-testid=focus-mode][data-mode=sticky]", timeout=5_000)
    assert _focused(page) == hero
    page.evaluate("() => { window.__focusLog.push(['resume', '']); window.__sampling = true }")

    # The handoff after the hold: the trace ends with HANDED_OFF; the focus never moved.
    page.wait_for_function(
        """() => { const t = document.querySelector('[data-testid=case-trace]')?.textContent ?? '';
           const h = Math.max(t.lastIndexOf('HOLDING'), t.lastIndexOf('AWAITING_APPROVAL'));
           return h >= 0 && t.indexOf('HANDED_OFF', h) > h }""",
        timeout=180_000,
    )
    page.wait_for_timeout(200)
    log = page.evaluate("() => window.__focusLog")
    trace = page.locator("[data-testid=case-trace]").inner_text()
    perf = page.evaluate("() => window.__perf")
    print("hero:", hero)
    print("trace:", trace)
    print("focus log:", log)
    # Not asserted: the design pass measures after the first paint, as this test does below.
    print(f"CLS at load and first focus: {load_cls:.4f} {load_top}")
    print(f"CLS while rows stream: {perf['cls']:.4f} ({perf['shifts']} shifts) {perf['top'][:12]}")
    # The handoff lands and the watch after it may start in the same tick, so the trace ends
    # with HANDED_OFF or with HANDED_OFF → WATCH_AFTER_HANDOFF.
    tail = trace[max(trace.rfind("HOLDING"), trace.rfind("AWAITING_APPROVAL")) :]
    assert "HANDED_OFF" in tail, tail
    last = page.locator("[data-testid=case-trace]").get_attribute("data-last-state")
    assert last in ("HANDED_OFF", "WATCH_AFTER_HANDOFF"), last
    assert ENDS_HANDED_OFF.search(trace.strip()), trace
    moved = [c for c, _s in log[start:] if c not in (hero, "resume")]
    assert not moved, f"focus moved on its own: {log[start:]}"
    assert page.locator("[data-testid=case-row]").count() >= 2
    assert "noLayoutShift" not in perf
    assert perf["cls"] < 0.05
    page.close()
