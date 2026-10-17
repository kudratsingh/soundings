"""WO-082 browser test: propose a slot in the registry form, accept it, conflict gone.

Part of ``make e2e`` (opt-in locally with ``SOUNDINGS_E2E=1``; CI has no browser). The
browser is WO-060's ``browser`` fixture; the server is the real app on 127.0.0.1 with a
temp-file store (zone capacity 1 via a test slot config) injected through ``get_store``,
so the developer's ``soundings.db`` is never touched. Saves
``artifacts/registry-proposal.png`` for the PR.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

NOW = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)  # 08:00 at -07:00
REPO = Path(__file__).resolve().parents[2]
SHOT = REPO / "artifacts" / "registry-proposal.png"
ZONE = "inner_basin_research"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def proposal_server(tmp_path: Path) -> Iterator[str]:
    import uvicorn

    from soundings.api import create_app
    from soundings.registry.router import get_store
    from soundings.registry.slots import load_slot_config
    from soundings.registry.store import RegistryStore

    cfg = load_slot_config().model_copy(update={"zone_capacity": {ZONE: 1}})
    store = RegistryStore(tmp_path / "e2e-slots.db", now=lambda: NOW, slot_config=cfg)
    application = create_app()
    application.dependency_overrides[get_store] = lambda: store
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("registry server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        store.close()


def _plan(plan_id: str, modem: str, start: str, end: str) -> dict[str, Any]:
    return {
        "plan_id": plan_id,
        "operator": "Harbor Survey Co (demo)",
        "vehicle": f"ROV-{plan_id}",
        "modem_id": modem,
        "area": ZONE,
        "start_at": f"2026-10-17T{start}:00-07:00",
        "end_at": f"2026-10-17T{end}:00-07:00",
        "contact_channel": "radio ch 16",
    }


def test_form_proposes_and_accepts_slot(proposal_server: str, browser: Any) -> None:
    import httpx

    for body in (
        _plan("P-X", "MOD-1234", "13:00", "15:00"),
        _plan("P-Y", "MOD-5678", "14:00", "16:00"),
    ):
        resp = httpx.post(f"{proposal_server}/api/plans", json=body)
        assert resp.status_code == 201, resp.text
    page = browser.new_page()
    try:
        page.goto(f"{proposal_server}/registry")

        def row(plan_id: str) -> Any:
            code = page.locator("code", has_text=re.compile(f"^{plan_id}$"))
            return page.locator("#plans-body tr", has=code).first

        y_row = row("P-Y")
        y_row.wait_for()
        assert "conflicts with P-X 14:00-15:00" in y_row.inner_text()
        y_row.locator("button.propose").click()
        page.wait_for_selector("#proposal-panel:not([hidden])")
        lines = page.inner_text("#proposal-lines")
        assert "P-Y 14:00-16:00 → 15:00-17:00 (+60 min)" in lines
        assert "Total delay: 60 min" in page.inner_text("#proposal-total")
        SHOT.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOT), full_page=True)
        page.click("#proposal-accept")
        page.wait_for_function(
            "document.getElementById('result').textContent.indexOf('Accepted') >= 0"
        )
        page.wait_for_function(
            "document.getElementById('plans-body').textContent.indexOf('conflicts with') < 0"
        )
        assert "conflicts with" not in page.inner_text("#plans-body")
        assert "15:00" in row("P-Y").inner_text()
    finally:
        page.close()
