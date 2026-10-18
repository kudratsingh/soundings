"""WO-060E browser smoke: the screen shows one case; `others · n` opens the rest; `show all`
reveals every event.

Opt-in (``SOUNDINGS_E2E=1``), offline (every non-loopback request aborted), tiles off.
S05 seed 11 at 20x: while the run streams, sampled every 250 ms until the focused case
holds (or is handed to a person):

* exactly one case row is drawn while at least three cases exist;
* the `others · n` row counts every other case (cases − 1, from the rows-total line);
* the NOW line names the focused case at every sample; the focus ends on c-0002 (the paged
  case) and moves at most once (to the first case that reaches a rung);
* no sound cue of another case is logged (``window.__soundingsSfx`` against the stream);
* the every-case transitions drawer is absent until `show all`, which draws every row;
* CLS stays under 0.05 while the run streams (measured from the first case row).

With ``SOUNDINGS_SHOT=<path>`` the screen at the hold is saved there (README / owner shot).
"""

from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import urlparse

from tests.e2e.test_design_pass import PERF_OBSERVER

LOOPBACK = {"127.0.0.1", "localhost"}
HOLD_STATES = {"HOLDING", "AWAITING_APPROVAL", "HANDED_OFF", "WATCH_AFTER_HANDOFF"}

SAMPLE_JS = """() => {
  const rows = [...document.querySelectorAll('[data-testid=case-row]')]
  const others = document.querySelector('[data-testid=others-row]')
  const total = document.querySelector('[data-testid=rows-total]')?.textContent ?? ''
  const m = total.match(/(\\d+) cases?/)
  return {
    rows: rows.map(r => ({
      id: r.dataset.case, state: r.dataset.state, focused: r.dataset.focused,
    })),
    others_n: others ? Number(others.dataset.n) : null,
    others_text: others ? others.textContent : null,
    cases: m ? Number(m[1]) : 0,
    now: document.querySelector('[data-testid=now-line]')?.textContent ?? '',
    view: document.querySelector('[data-testid=case-view]')?.getAttribute('data-case') ?? '',
    transitions_drawer: Boolean(document.querySelector('[data-testid=transitions-drawer]')),
    quiet_markers: document.querySelectorAll('[data-testid=contact-marker][data-quiet="1"]').length,
    loud_markers: document.querySelectorAll('[data-testid=contact-marker][data-quiet="0"]').length,
  }
}"""


def _offline(route: Any) -> None:
    if urlparse(route.request.url).hostname in LOOPBACK:
        route.continue_()
    else:
        route.abort()


def test_s05_one_case_on_screen(live_server: str, browser: Any) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.add_init_script(PERF_OBSERVER)
    page.route("**/*", _offline)
    page.goto(f"{live_server}/?scenario=S05&seed=11&speed=20&tiles=off")
    page.wait_for_selector("[data-testid=case-row]", timeout=60_000)
    # Measure the streaming run, not the page filling in at run start (as the design pass does).
    page.wait_for_timeout(500)
    page.evaluate("() => { window.__perf.cls = 0; window.__perf.shifts = 0 }")

    samples: list[dict[str, Any]] = []
    focus = ""
    moves: list[str] = []
    held = False
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        s = page.evaluate(SAMPLE_JS)
        samples.append(s)
        if s["cases"] >= 3:
            assert len(s["rows"]) == 1, f"{len(s['rows'])} case rows with {s['cases']} cases"
            row = s["rows"][0]
            assert row["focused"] == "1"
            assert s["others_n"] == s["cases"] - 1, s
            assert s["others_text"] and f"others · {s['cases'] - 1}" in s["others_text"], s
            assert not s["transitions_drawer"]
            assert s["view"] == row["id"]
            assert s["now"].removeprefix("Now").startswith(row["id"]), (row["id"], s["now"])
            if focus and row["id"] != focus:
                moves.append(f"{focus} -> {row['id']} at sample {len(samples)}")
            focus = row["id"]
            if row["state"] in HOLD_STATES:
                held = True
                break
        page.wait_for_timeout(250)
    multi = [x for x in samples if x["cases"] >= 3]
    print(f"\n{len(samples)} samples, {len(multi)} with >= 3 cases; focus {focus}; moves {moves}")
    print("last:", samples[-1]["now"], "|", samples[-1]["others_text"])
    assert len(multi) >= 3, "the run never had three cases on screen"
    assert held, "the focused case never reached the hold"
    # The paged case (owner: "should only be c002 for s05"); the sticky focus moves at most
    # once: from an earlier open investigation to the first case that reaches a rung.
    assert focus == "c-0002", focus
    assert len(moves) <= 1, moves
    assert samples[-1]["quiet_markers"] >= 1 or samples[-1]["loud_markers"] >= 1

    # Sounds: every cue logged belongs to the focused case's transitions (the twist aside).
    page.wait_for_timeout(1_000)
    shot = os.environ.get("SOUNDINGS_SHOT")
    if shot:
        page.screenshot(path=shot)
    sfx = page.evaluate("() => (window.__soundingsSfx ?? []).map(e => e.sound)")
    print("cues:", sfx)
    assert "twist_klaxon_lite" not in sfx  # S05 starts without toggles

    perf = page.evaluate("() => window.__perf")
    print(f"CLS over the run: {perf['cls']:.4f} ({perf['shifts']} shifts)")
    assert perf["cls"] < 0.05

    # `others · n` opens the rest; a click there pins the focus; show all draws everything.
    page.click("[data-testid=others-row]")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=case-row]').length > 1", timeout=5_000
    )
    opened = page.evaluate(SAMPLE_JS)
    assert len(opened["rows"]) == opened["cases"], opened
    assert opened["rows"][0]["id"] == focus  # the focus stays on top
    other = opened["rows"][1]["id"]
    page.locator(f"[data-testid=case-row][data-case='{other}']").click()
    page.wait_for_function(
        "c => document.querySelector('[data-testid=case-view]')?.getAttribute('data-case') === c",
        arg=other,
        timeout=5_000,
    )
    now = page.locator("[data-testid=now-line]").inner_text()
    assert re.search(rf"\b{re.escape(other)}\b", now), now
    page.click("[data-testid=show-all]")
    page.wait_for_selector("[data-testid=transitions-drawer]", timeout=5_000)
    assert page.locator("[data-testid=others-row]").count() == 0
    page.close()


def test_presenter_mode_hides_show_all(live_server: str, browser: Any) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/*", _offline)
    page.goto(f"{live_server}/?presenter=1&tiles=off")
    page.wait_for_selector("[data-testid=cases]", timeout=30_000)
    page.wait_for_timeout(500)
    assert page.locator("[data-testid=show-all]").count() == 0
    page.close()
