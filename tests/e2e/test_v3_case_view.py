"""WO-060 part B browser smokes: the case view filled from the runner v3 stream.

Opt-in (``SOUNDINGS_E2E=1``), offline (every non-loopback request aborted), tiles off:

* S05 seed 11 at 20x reaches AWAITING_APPROVAL: the case needing approval shows its LADDER
  step card with the hold under standing orders, the posterior panel shows threat mass
  >= 0.6 (stress-test estimate), and the approval card is up;
* S02 shows LOST_VEHICLE with the search box in the HANDOFF ("Notice to operator") card and
  the SITREP drawer;
* S04 ends with a RESOLVED_BENIGN case and its RECOMMENDATION card names the stop clause.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

LOOPBACK = {"127.0.0.1", "localhost"}


def _offline(route: Any) -> None:
    if urlparse(route.request.url).hostname in LOOPBACK:
        route.continue_()
    else:
        route.abort()


def _page(browser: Any, live_server: str, query: str) -> Any:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/*", _offline)
    page.goto(f"{live_server}/?{query}&tiles=off")
    return page


def test_s05_awaiting_approval_hold_and_posterior(live_server: str, browser: Any) -> None:
    page = _page(browser, live_server, "scenario=S05&seed=11&speed=20")
    row = page.locator("[data-testid=case-row][data-state=AWAITING_APPROVAL]")
    row.first.wait_for(timeout=180_000)
    row.first.click()
    page.wait_for_selector("[data-testid=approval-card]", timeout=30_000)
    ladder = page.locator("[data-testid=step][data-step=ladder][data-status=current]")
    ladder.first.wait_for(timeout=30_000)
    lines = ladder.first.locator("[data-testid=ladder-line]").all_inner_texts()
    print("LADDER:", lines)
    assert any("Hold" in t and "standing orders" in t for t in lines), lines
    assert any("Approval requested" in t for t in lines), lines
    masses = [
        float(x)
        for x in page.locator("[data-testid=case-view] [data-testid=posterior]").evaluate_all(
            "els => els.map(e => e.getAttribute('data-threat-mass'))"
        )
    ]
    print("threat masses in the case view:", masses)
    assert masses and max(masses) >= 0.6
    assert "stress-test estimate" in page.locator("[data-testid=case-view]").inner_text()
    now = page.locator("[data-testid=now-line]").inner_text()
    assert "awaiting approval" in now, now
    page.close()


def test_s02_lost_vehicle_search_box(live_server: str, browser: Any) -> None:
    page = _page(browser, live_server, "scenario=S02&seed=3&speed=50")
    row = page.locator("[data-testid=case-row][data-state=LOST_VEHICLE]")
    row.first.wait_for(timeout=180_000)
    row.first.click()
    card = page.locator("[data-testid=step][data-step=handoff]")
    card.first.wait_for(timeout=30_000)
    assert "Notice to operator" in card.first.inner_text()
    card.first.get_by_role("button").first.click()
    drawer = page.locator("[data-testid=sitrep-drawer]")
    drawer.wait_for(timeout=10_000)
    assert "Search box" in drawer.inner_text()
    page.close()


def test_s04_resolved_benign(live_server: str, browser: Any) -> None:
    page = _page(browser, live_server, "scenario=S04&speed=50")
    row = page.locator("[data-testid=case-row][data-state=RESOLVED_BENIGN]")
    row.first.wait_for(timeout=180_000)
    row.first.click()
    rec = page.locator("[data-testid=step][data-step=recommendation][data-status=done]")
    rec.first.wait_for(timeout=30_000)
    text = rec.first.inner_text()
    print("RECOMMENDATION:", text)
    assert "Stop clause" in text and "stress-test estimate" in text
    page.close()
