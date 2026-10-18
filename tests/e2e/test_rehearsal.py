"""Demo rehearsal regressions (2026-10-17, docs/REHEARSAL.md). Opt-in: ``SOUNDINGS_E2E=1``.

Each test pins one defect the run-through found on the watch-officer screen:

* an open approval request leads the case list and the NOW line (a handed-off case used to
  outrank it), and the SITREP drawer never covers the approval card;
* Escape closes the SITREP drawer and the About panel, and About puts focus on Close;
* the responder's dispatch to look is a key moment, and the NOW line does not flip between
  handed-off cases on every watch update;
* the scenario picker follows the run on screen, so Reset restarts what is shown.

Offline: every non-loopback request is aborted.
"""

from __future__ import annotations

from typing import Any

from tests.e2e._judge import offline
from tests.e2e._quiet import reveal


def _page(browser: Any, live_server: str, query: str) -> Any:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/*", offline)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{live_server}/?{query}")
    page.errors = errors  # type: ignore[attr-defined]
    return page


def _overlaps(a: dict[str, float], b: dict[str, float]) -> bool:
    return not (
        a["x"] + a["width"] <= b["x"]
        or b["x"] + b["width"] <= a["x"]
        or a["y"] + a["height"] <= b["y"]
        or b["y"] + b["height"] <= a["y"]
    )


def test_approval_leads_and_drawer_spares_it(live_server: str, browser: Any) -> None:
    page = _page(browser, live_server, "scenario=S05&seed=11&speed=50&tiles=off&source=stepped")
    page.wait_for_selector("[data-testid=approve]", timeout=240_000)
    # The case waiting on a person is the selected case and the NOW line names the request.
    now = page.locator("[data-testid=now-line]").inner_text()
    assert "awaiting approval" in now, now
    first = page.locator("[data-testid=case-row]").first.inner_text()
    assert "AWAITING APPROVAL" in first, first
    # The SITREP drawer opens over the map, never over the approval card.
    page.click("[data-testid=sitrep-open]")
    drawer = page.wait_for_selector("[data-testid=sitrep-drawer]")
    card = page.locator("[data-testid=approval-card]").bounding_box()
    assert card is not None
    assert not _overlaps(card, drawer.bounding_box())
    page.locator("[data-testid=approve]").click(timeout=5_000)  # still clickable
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-testid=sitrep-drawer]", state="detached", timeout=2_000)
    # About: focus lands on Close, Escape closes it.
    page.click("[data-testid=about-open]")
    page.wait_for_selector("[data-testid=about-panel]")
    focused = page.evaluate("() => document.activeElement?.dataset?.testid ?? ''")
    assert focused == "about-close"
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-testid=about-panel]", state="detached", timeout=2_000)
    assert not page.errors  # type: ignore[attr-defined]
    page.close()


def test_responder_moment_and_steady_now_line(live_server: str, browser: Any) -> None:
    page = _page(browser, live_server, "scenario=S02&seed=3&speed=50&tiles=off")
    page.wait_for_selector("[data-testid=contact-marker]", timeout=60_000)
    # WO-060D: key moments show the focused case; pin the glider's case (c-0001).
    # WO-060E: a case that is not the focus sits behind `others · n`; reveal opens it.
    reveal(page, "[data-testid=case-row][data-case='c-0001']", 60_000).first.click()
    seen: list[str] = []
    for _ in range(240):
        line = page.locator("[data-testid=now-line]").inner_text().split("—")[0].strip()
        if not seen or seen[-1] != line:
            seen.append(line)
        chips = page.locator("[data-testid=moment-chip]").all_inner_texts()
        if any("responder dispatched to look" in c for c in chips):
            break
        page.wait_for_timeout(250)
    else:
        raise AssertionError(f"no responder key moment; NOW history {seen}")
    # Each case enters the NOW line once; watch updates do not flip it back.
    ids = [s.split("·")[0].strip() for s in seen if "·" in s]
    collapsed = [i for k, i in enumerate(ids) if k == 0 or ids[k - 1] != i]
    assert len(collapsed) == len(set(collapsed)), seen
    assert not page.errors  # type: ignore[attr-defined]
    page.close()


def test_picker_follows_the_run_on_screen(live_server: str, browser: Any) -> None:
    page = _page(browser, live_server, "presenter=1&tiles=off")
    page.wait_for_selector("[data-testid=presenter-bar][data-applied='0']", timeout=30_000)
    page.wait_for_function(
        "() => document.querySelector('[data-testid=scenario-label]').innerText.startsWith('S')",
        timeout=20_000,
    )
    label = page.locator("[data-testid=scenario-label]").inner_text()
    scenario = label.split("·")[0].strip()
    seed = label.rsplit("seed", 1)[1].strip()
    page.wait_for_function(
        "([sc, sd]) => document.querySelector('select[aria-label=scenario]').value === sc"
        " && document.querySelector('input[aria-label=seed]').value === sd",
        arg=[scenario, seed],
        timeout=5_000,
    )
    page.close()
