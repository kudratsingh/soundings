"""WO-061 (BUILD-PLAN, verbatim): judge panel: place a contact by clicking the map, choose
behavior, press go; the investigation starts within 2 s and the UI follows it. Offline.
Opt-in locally with SOUNDINGS_E2E=1; CI has no browser."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from tests.e2e._judge import map_pixel, offline

REPO = Path(__file__).resolve().parents[2]
SHOT = REPO / "artifacts" / "judge-panel.png"


def test_place_go_follows_within_2s(
    live_server: str, browser: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOUNDINGS_LIVE_DIR", str(tmp_path))
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/*", offline)
    page.goto(f"{live_server}/?tiles=off&judge=1")
    page.wait_for_selector(".asset-mark", timeout=30_000)
    page.click("[data-testid=judge-open]")
    page.click("[data-testid=judge-place]")
    berth = next(
        f
        for f in page.request.get(f"{live_server}/api/harbor").json()["features"]
        if f["properties"].get("asset_id") == "fuel_berth_2"
    )
    lon, lat = berth["geometry"]["coordinates"]
    x, y = map_pixel(page, live_server, lat - 0.0036, lon)  # the fuel-berth approach
    page.mouse.click(x, y)
    page.click("[data-testid=judge-behavior-closing]")
    page.wait_for_selector("[data-testid=judge-go]")
    SHOT.parent.mkdir(exist_ok=True)
    page.screenshot(path=str(SHOT))
    # The judge reads the choice back before pressing Go (the panel prepares that run).
    page.wait_for_timeout(4000)
    t0 = time.monotonic()
    page.click("[data-testid=judge-go]")
    page.wait_for_selector("[data-testid=contact-marker]", timeout=10_000)
    page.wait_for_function(
        "() => !document.querySelector('[data-testid=state-timeline]')"
        "?.innerText.includes('no transitions yet')",
        timeout=10_000,
    )
    elapsed = time.monotonic() - t0
    assert elapsed <= 2.0, f"the screen followed the judge's run after {elapsed:.2f} s"
    assert "L-" in page.locator("[data-testid=scenario-label]").inner_text()
    assert list(tmp_path.glob("L-*.yaml"))
    page.wait_for_selector("[data-testid=rung-active]:not([data-rung=watch])", timeout=240_000)
    page.screenshot(path=str(SHOT.with_name("judge-run-following.png")))
    page.close()
