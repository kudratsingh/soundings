"""WO-060 Part A browser smoke: the screen loads, follows a run, and works offline.

Opens ``/?scenario=S02&seed=3&speed=20&tiles=off`` with every non-loopback request
aborted, waits for a contact marker, for the rung indicator to reach ``ask``, and for a
transition in the state timeline. Then opens About (boundary table, tile attribution).
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

LOOPBACK = {"127.0.0.1", "localhost"}


def _offline(route: Any) -> None:
    host = urlparse(route.request.url).hostname
    if host in LOOPBACK:
        route.continue_()
    else:
        route.abort()


def test_s02_contact_and_ask_rung(live_server: str, browser: Any) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    blocked: list[str] = []
    page.on(
        "requestfailed",
        lambda r: blocked.append(r.url) if urlparse(r.url).hostname not in LOOPBACK else None,
    )
    page.route("**/*", _offline)
    page.goto(f"{live_server}/?scenario=S02&seed=3&speed=20&tiles=off")
    page.wait_for_selector("[data-testid=contact-marker]", timeout=90_000)
    page.wait_for_selector("[data-testid=rung-active][data-rung=ask]", timeout=180_000)
    timeline = page.locator("[data-testid=state-timeline]")
    assert "ASKING" in timeline.inner_text(timeout=30_000)
    assert page.locator("[data-testid=ledger-row]").count() > 0
    assert page.locator("[data-testid=approval-placeholder]").inner_text() == (
        "approval wired in WO-061"
    )
    assert page.locator("[data-testid=approve]").count() == 0  # no approval is pending
    assert (
        "simplified for demonstration" in page.locator("[data-testid=map-attribution]").inner_text()
    )
    page.click("[data-testid=sitrep-open]")
    assert page.locator("[data-testid=sitrep-drawer]").is_visible()
    page.click("[data-testid=about-open]")
    about = page.locator("[data-testid=about-panel]")
    about.wait_for()
    text = about.inner_text()
    for needle in ("simulated for this demonstration", "© OpenStreetMap contributors", "ask"):
        assert needle in text
    shots = os.environ.get("SOUNDINGS_E2E_SHOTS")
    if shots:
        page.screenshot(path=f"{shots}/about.png", full_page=True)
    assert blocked == [] or all(urlparse(u).hostname not in LOOPBACK for u in blocked)
    page.close()
