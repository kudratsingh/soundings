"""WO-086 browser smoke (opt-in: ``SOUNDINGS_E2E=1``): the rung ledger, the red blunt
counter and the projection ellipse on the radar map.

Opens ``/?scenario=S05&seed=11&speed=0&tiles=off`` with every non-loopback request aborted
and waits for ``run finished``. Each ``ledger-count`` must equal the in-process final
``ledger`` event (which ``tests/test_ledger_integration.py`` proves equal to the eval
harness numbers), ``blunt-count`` must equal the bang baseline's closures and be painted in
the ``--blunt`` token, and the map must be the radar scope with at least one trail, one
velocity leader and one projection ellipse. The hail ping is WO-062P's ``sfx.ts`` (no
second sound module), so the WO's ping assertions are not part of this smoke.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from soundings.stream.engine import stream_events
from soundings.stream.events import LedgerEvent

LOOPBACK = {"127.0.0.1", "localhost"}


def _offline(route: Any) -> None:
    host = urlparse(route.request.url).hostname
    if host in LOOPBACK:
        route.continue_()
    else:
        route.abort()


def test_ledger_panel_matches_stream(live_server: str, browser: Any) -> None:
    final = [e for e in stream_events("S05", 11) if isinstance(e, LedgerEvent)][-1].payload
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/*", _offline)
    page.goto(f"{live_server}/?scenario=S05&seed=11&speed=0&tiles=off")
    page.wait_for_selector("[data-testid=ledger-panel]", timeout=60_000)
    page.wait_for_selector("text=run finished", timeout=180_000)
    for bucket, n in final.counts.items():
        cell = page.locator(f"[data-testid=ledger-count][data-bucket={bucket.value}]")
        assert cell.inner_text().strip() == str(n), bucket
    blunt = page.locator("[data-testid=blunt-count]")
    assert blunt.inner_text().strip() == str(final.blunt.berth_closures)
    colours = page.evaluate(
        """() => {
          const el = document.querySelector('[data-testid=blunt-count]')
          const probe = document.createElement('span')
          probe.style.color = 'var(--blunt)'
          document.body.appendChild(probe)
          const want = getComputedStyle(probe).color
          probe.remove()
          return [getComputedStyle(el).color, want]
        }"""
    )
    assert colours[0] == colours[1], colours
    assert page.locator(".map-wrap.radar").count() == 1
    assert page.locator("path.contact-trail").count() >= 1
    assert page.locator("path.velocity-leader").count() >= 1
    # The ellipse belongs to the selected case; pick each visible contact until one with a
    # projected track (two or more readings) is selected.
    ellipses = page.locator("path.projection-ellipse")
    markers = page.locator("[data-testid=contact-marker]")
    for i in range(markers.count()):
        if ellipses.count() >= 1:
            break
        markers.nth(i).click(force=True)
        page.wait_for_timeout(500)
    assert ellipses.count() >= 1
    tip = page.locator(".blunt").get_attribute("title") or ""
    assert "counterfactual" in tip
    page.close()
