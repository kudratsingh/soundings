"""WO-061 (BUILD-PLAN, verbatim): load page, see a contact appear, see the rung indicator
change, click approve on a Tier-2 recommendation, see HANDED_OFF. Offline (non-loopback
requests aborted). Opt-in locally with SOUNDINGS_E2E=1; CI has no browser."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.e2e._judge import offline

REPO = Path(__file__).resolve().parents[2]
SHOT = REPO / "artifacts" / "ui-approval.png"


def test_click_approve_sees_handed_off(live_server: str, browser: Any) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/*", offline)
    page.goto(f"{live_server}/?scenario=S05&seed=11&speed=50&tiles=off&source=stepped")
    page.wait_for_selector("[data-testid=contact-marker]", timeout=120_000)
    page.wait_for_selector("[data-testid=rung-active]:not([data-rung=watch])", timeout=180_000)
    page.wait_for_selector("[data-testid=approve]", timeout=240_000)
    card = page.locator("[data-testid=approval-card]")
    assert "scope" in page.locator("[data-testid=approval-scope]").inner_text()
    assert int(page.locator("[data-testid=approval-countdown]").get_attribute("data-left")) > 0
    SHOT.parent.mkdir(exist_ok=True)
    card.screenshot(path=str(SHOT.with_name("ui-approval-card.png")))
    page.click("[data-testid=approve]")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=state-timeline]')"
        "?.innerText.includes('HANDED_OFF')",
        timeout=60_000,
    )
    page.screenshot(path=str(SHOT))
    page.close()
