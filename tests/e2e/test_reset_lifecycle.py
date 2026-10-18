"""WO-060C browser smoke: Reset stops the previous run and starts exactly one new one.

Opens ``/?scenario=S02&seed=1&speed=20&tiles=off`` (the page-load auto-start runs once),
waits for the first contact, then clicks Reset twice fast. ``GET /api/runs`` must then
hold exactly one unfinished run and the screen must show that run's first case.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.request import Request, urlopen


def _runs(base: str) -> list[dict[str, Any]]:
    with urlopen(f"{base}/api/runs", timeout=30) as r:
        return list(json.loads(r.read()))


def _unfinished(base: str) -> list[dict[str, Any]]:
    return [r for r in _runs(base) if not r["finished"]]


def test_reset_twice_leaves_one_run(live_server: str, browser: Any) -> None:
    for r in _unfinished(live_server):  # other smokes share the server's engine
        urlopen(Request(f"{live_server}/api/runs/{r['run_id']}", method="DELETE"), timeout=30)
    before = {r["run_id"] for r in _runs(live_server)}
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"{live_server}/?scenario=S02&seed=1&speed=20&tiles=off")
    page.wait_for_selector("[data-testid=contact-marker]", timeout=120_000)
    assert len(_unfinished(live_server)) == 1  # the auto-start ran once
    button = page.locator("[data-testid=start-run]")
    assert button.inner_text() == "Reset"
    button.click()
    assert "starting S02 seed 1" in page.locator("[data-testid=now-line]").inner_text()
    assert button.inner_text() == "starting…" and button.get_attribute("aria-busy") == "true"
    # A second click while the first start is in flight is ignored (aria-busy, no clicks).
    button.dispatch_event("click")
    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=start-run]');"
        " return b && b.dataset.starting === '0' && b.textContent === 'Reset' }",
        timeout=120_000,
    )
    button.click()  # and a second Reset once the first has landed replaces it too
    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=start-run]');"
        " return b && b.dataset.starting === '0' && b.textContent === 'Reset' }",
        timeout=120_000,
    )
    page.wait_for_selector("[data-testid=contact-marker]", timeout=120_000)
    deadline = time.monotonic() + 30
    while len(_unfinished(live_server)) != 1 and time.monotonic() < deadline:
        time.sleep(0.2)
    live = _unfinished(live_server)
    assert len(live) == 1, live
    assert live[0]["scenario"] == "S02" and live[0]["seed"] == 1
    new = [r for r in _runs(live_server) if r["run_id"] not in before]
    # auto-start once, Reset, (the ignored click), Reset: three runs, two of them stopped
    assert len(new) == 3, new
    assert sorted(bool(r["cancelled"]) for r in new) == [False, True, True]
    assert "starting" not in page.locator("[data-testid=now-line]").inner_text()
    assert "S02" in page.locator("[data-testid=scenario-label]").inner_text()
    page.close()
