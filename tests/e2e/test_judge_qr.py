"""WO-061 QR addendum (directive, verbatim): the judge panel also shows a QR code to the
registry form so a judge can file a plan from a phone; the filed plan must appear on the
map within 2 s and (if conflicted) show the CP-SAT proposal. Offline; the registry store is
a temp file. Opt-in locally with SOUNDINGS_E2E=1; CI has no browser."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e._judge import offline

REPO = Path(__file__).resolve().parents[2]
WEB_DIST = REPO / "web" / "dist"
SHOT = REPO / "artifacts" / "judge-qr.png"
# Plans are filed for tomorrow: the slot solver never moves a plan into the past, so a
# fixed afternoon today would turn infeasible once the clock passes it.
DAY = (date.today() + timedelta(days=1)).isoformat()


@pytest.fixture
def qr_server(tmp_path: Path, live_server: str) -> Iterator[str]:
    # ``live_server`` only enforces the opt-in and the built web/dist.
    import uvicorn

    from soundings.api import create_app
    from soundings.registry.router import get_store
    from soundings.registry.store import RegistryStore

    store = RegistryStore(tmp_path / "e2e-plans.db")
    app = create_app(WEB_DIST)
    app.dependency_overrides[get_store] = lambda: store
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        store.close()


def _screen(browser: Any, base: str) -> Any:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/*", offline)
    page.goto(f"{base}/?tiles=off&judge=1")
    page.click("[data-testid=judge-open]")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=judge-qr-url]')?.dataset.url"
    )
    return page


def _file_from_phone(browser: Any, url: str, plan_id: str, start: str, end: str) -> float:
    phone = browser.new_page(viewport={"width": 390, "height": 844})
    phone.route("**/*", offline)
    phone.goto(url)
    phone.wait_for_selector("#area option", state="attached")
    phone.fill("#plan_id", plan_id)
    phone.fill("#operator", "Judge survey team (demo)")
    phone.fill("#vehicle", "Survey glider")
    phone.fill("#modem_id", "MOD-4401")
    phone.fill("#contact_channel", "sim-phone-judge")
    phone.fill("#start_at", f"{DAY}T{start}")
    phone.fill("#end_at", f"{DAY}T{end}")
    phone.fill("#utc_offset", "-07:00")
    with phone.expect_response(lambda r: r.url.endswith("/api/plans") and r.status == 201):
        phone.click("#submit")
    t0 = time.monotonic()
    phone.close()
    return t0


def test_filed_plan_on_map_within_2s(qr_server: str, browser: Any) -> None:
    page = _screen(browser, qr_server)
    img = page.wait_for_selector("[data-testid=judge-qr]")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=judge-qr]').naturalWidth > 0"
    )
    assert img.evaluate("e => e.naturalWidth") > 0
    url = page.locator("[data-testid=judge-qr-url]").get_attribute("data-url")
    assert url and url.endswith("/registry") and url.startswith(qr_server)
    t0 = _file_from_phone(browser, url, "P-JUDGE-1", "14:00", "16:00")
    page.wait_for_selector("[data-testid=plan-marker][data-plan=P-JUDGE-1]", timeout=5_000)
    elapsed = time.monotonic() - t0
    assert elapsed <= 2.0, f"plan marker after {elapsed:.2f} s"
    SHOT.parent.mkdir(exist_ok=True)
    page.screenshot(path=str(SHOT))
    page.close()


def _api_plan(base: str, plan_id: str, modem: str) -> None:
    zone = httpx.get(f"{base}/api/plans/zones").json()[0]["zone_id"]
    body = {
        "plan_id": plan_id,
        "operator": "Harbor Survey Co (demo)",
        "vehicle": f"ROV-{plan_id}",
        "modem_id": modem,
        "area": zone,
        "start_at": f"{DAY}T14:00:00-07:00",
        "end_at": f"{DAY}T16:00:00-07:00",
        "contact_channel": "radio ch 16",
    }
    r = httpx.post(f"{base}/api/plans", json=body)
    assert r.status_code == 201, r.text


def test_conflicted_plan_shows_proposal(qr_server: str, browser: Any) -> None:
    for i, modem in enumerate(("MOD-1001", "MOD-1002", "MOD-1003"), start=1):
        _api_plan(qr_server, f"P-API-{i}", modem)
    page = _screen(browser, qr_server)
    url = page.locator("[data-testid=judge-qr-url]").get_attribute("data-url")
    assert url
    t0 = _file_from_phone(browser, url, "P-PHONE-4", "14:00", "16:00")
    page.wait_for_selector(
        "[data-testid=plan-marker][data-plan=P-PHONE-4][data-conflicted=true]", timeout=5_000
    )
    card = page.wait_for_selector("[data-testid=proposal-card]", timeout=5_000)
    assert time.monotonic() - t0 <= 2.0
    assert "P-PHONE-4" in card.inner_text()
    assert "operator accepts" in card.inner_text()
    buttons = card.query_selector_all("button")
    assert {b.get_attribute("data-testid") for b in buttons} <= {
        "proposal-accept",
        "proposal-decline",
    }
    page.wait_for_selector("[data-testid=proposal-decline]")
    before = httpx.get(f"{qr_server}/api/plans/P-PHONE-4").content
    page.click("[data-testid=proposal-decline]")
    page.wait_for_function(  # the card for P-PHONE-4 closes (another plan's may follow)
        "() => !(document.querySelector('[data-testid=proposal-card]')?.innerText ?? '')"
        ".includes('P-PHONE-4 conflicts')",
        timeout=5_000,
    )
    assert httpx.get(f"{qr_server}/api/plans/P-PHONE-4").content == before
    # Refile the proposal: a fresh screen asks the solver again for the same plan.
    page.reload()
    page.wait_for_function(
        "() => (document.querySelector('[data-testid=proposal-card]')?.innerText ?? '')"
        ".includes('P-PHONE-4 conflicts')",
        timeout=10_000,
    )
    page.wait_for_selector("[data-testid=proposal-accept]", timeout=5_000)
    page.screenshot(path=str(SHOT.with_name("judge-proposal.png")))
    window_before = httpx.get(f"{qr_server}/api/plans/P-PHONE-4").json()["start_at"]
    page.click("[data-testid=proposal-accept]")
    t1 = time.monotonic()
    page.wait_for_selector(
        "[data-testid=plan-marker][data-plan=P-PHONE-4][data-conflicted=false]", timeout=5_000
    )
    assert time.monotonic() - t1 <= 2.0
    assert httpx.get(f"{qr_server}/api/plans/P-PHONE-4").json()["start_at"] != window_before
    page.close()
