"""WO-081 browser test: the registry form shows "conflicts with <plan> 14:00-16:00".

Owner directive (verbatim): form shows "conflicts with <plan> 14:00-16:00".

Runs with ``SOUNDINGS_E2E_REQUIRED=1`` (fails if Chromium cannot start) or
``SOUNDINGS_E2E=1`` (skips if it cannot); skipped otherwise so the default suite needs no
browser. The server fixture is local to this module (WO-060's ``tests/e2e/conftest.py``
is not on main yet); it serves the real app on 127.0.0.1 with a temp-file store.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REQUIRED = os.environ.get("SOUNDINGS_E2E_REQUIRED") == "1"
ENABLED = REQUIRED or os.environ.get("SOUNDINGS_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not ENABLED, reason="browser test: set SOUNDINGS_E2E=1 or SOUNDINGS_E2E_REQUIRED=1"
)

NOW = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def registry_server(tmp_path: Path) -> Iterator[str]:
    import uvicorn

    from soundings.api import create_app
    from soundings.registry.router import get_store
    from soundings.registry.store import RegistryStore

    store = RegistryStore(tmp_path / "e2e.db", now=lambda: NOW)
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


def test_form_shows_conflict_lines(registry_server: str) -> None:
    import httpx

    sync_api = pytest.importorskip("playwright.sync_api")
    resp = httpx.post(
        f"{registry_server}/api/plans",
        json={
            "plan_id": "P-X",
            "operator": "Harbor Survey Co (demo)",
            "vehicle": "ROV-Blue-1",
            "modem_id": "MOD-1234",
            "area": "inner_basin_research",
            "start_at": "2026-10-17T13:00:00-07:00",
            "end_at": "2026-10-17T16:00:00-07:00",
            "contact_channel": "radio ch 16",
        },
    )
    assert resp.status_code == 201, resp.text
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure means no browser here
            if REQUIRED:
                raise
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.goto(f"{registry_server}/registry")
            page.wait_for_selector("#area option[value='inner_basin_research']", state="attached")
            page.select_option("#area", "inner_basin_research")
            page.fill("#plan_id", "P-Y")
            page.fill("#operator", "Pier Works (demo)")
            page.fill("#vehicle", "ROV-Red-2")
            page.fill("#modem_id", "MOD-5678")
            page.fill("#contact_channel", "radio ch 12")
            page.fill("#start_at", "2026-10-17T14:00")
            page.fill("#end_at", "2026-10-17T18:00")
            page.fill("#utc_offset", "-07:00")
            page.click("#submit")
            page.wait_for_function(
                "document.getElementById('result').textContent.indexOf('P-Y') >= 0"
            )
            assert "conflicts with P-X 14:00-16:00" in page.inner_text("#result")

            def row(plan_id: str) -> Any:
                code = page.locator("code", has_text=re.compile(f"^{plan_id}$"))
                return page.locator("#plans-body tr", has=code).first

            y_row, x_row = row("P-Y"), row("P-X")
            y_row.wait_for()
            assert "conflicts with P-X 14:00-16:00" in y_row.inner_text()
            assert "conflicts with P-Y 14:00-16:00" in x_row.inner_text()
        finally:
            browser.close()
