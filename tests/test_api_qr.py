"""WO-061 QR addendum: GET /api/qr (display only; encodes the registry form URL)."""

from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import soundings.api_routes.qr as qr_mod
from soundings.api import create_app
from soundings.api_routes.qr import QR_TARGETS, qr_svg

REPO = Path(__file__).resolve().parents[1]
SVG_ROOT = "{http://www.w3.org/2000/svg}svg"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("SOUNDINGS_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("SOUNDINGS_LAN", raising=False)
    return TestClient(create_app())


def test_svg_encodes_the_registry_url(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = qr_mod.segno.make

    def spy(content: str, **kw: Any) -> Any:
        seen.append(content)
        return real(content, **kw)

    monkeypatch.setattr(qr_mod.segno, "make", spy)
    r = client.get("/api/qr", params={"target": "/registry"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert ET.fromstring(r.content).tag == SVG_ROOT
    assert seen == ["http://testserver/registry"]
    assert r.headers["x-qr-payload"] == "http://testserver/registry"
    assert r.content == qr_svg("http://testserver/registry")


@pytest.mark.parametrize("target", ["/api/plans", "https://example.com", ""])
def test_only_declared_targets(client: TestClient, target: str) -> None:
    assert client.get("/api/qr", params={"target": target}).status_code == 422
    assert client.get("/api/qr/info", params={"target": target}).status_code == 422
    assert QR_TARGETS == frozenset({"/registry"})


def test_base_url(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    info = client.get("/api/qr/info", params={"target": "/registry"}).json()
    assert info == {"url": "http://testserver/registry", "loopback": False, "lan": False}
    monkeypatch.setenv("SOUNDINGS_PUBLIC_BASE_URL", "http://192.168.1.20:8000/")
    r = client.get("/api/qr", params={"target": "/registry"})
    assert r.headers["x-qr-payload"] == "http://192.168.1.20:8000/registry"
    monkeypatch.delenv("SOUNDINGS_PUBLIC_BASE_URL")
    for host in ("127.0.0.1", "localhost"):
        local = TestClient(create_app(), base_url=f"http://{host}:8000")
        got = local.get("/api/qr/info", params={"target": "/registry"}).json()
        assert got == {"url": f"http://{host}:8000/registry", "loopback": True, "lan": False}
    monkeypatch.setenv("SOUNDINGS_LAN", "1")
    assert client.get("/api/qr/info", params={"target": "/registry"}).json()["lan"] is True


def test_svg_byte_identical() -> None:
    url = "http://192.168.1.20:8000/registry"
    a, b = qr_svg(url), qr_svg(url)
    code = (
        "import hashlib, sys; from soundings.api_routes.qr import qr_svg; "
        f"sys.stdout.write(hashlib.sha256(qr_svg({url!r})).hexdigest())"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env={**os.environ, "PYTHONHASHSEED": "1"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest() == out


def test_qr_display_only() -> None:
    tree = ast.parse((REPO / "soundings" / "api_routes" / "qr.py").read_text(encoding="utf-8"))
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
    banned = ("soundings.ladder", "soundings.llm", "anthropic")
    assert not [m for m in mods if m.startswith(banned)], mods


def test_filed_plan_is_listed_at_once(tmp_path: Path) -> None:
    """The server half of 'a filed plan appears on the map within 2 s': the plan the
    phone files is in GET /api/plans?status=active on the very next poll (1 s)."""
    from soundings.registry.router import get_store
    from soundings.registry.store import RegistryStore

    app = create_app()
    store = RegistryStore(tmp_path / "plans.db")
    app.dependency_overrides[get_store] = lambda: store
    c = TestClient(app)
    zone = c.get("/api/plans/zones").json()[0]["zone_id"]
    body = {
        "operator": "Judge survey team (demo)",
        "vehicle": "Survey glider",
        "modem_id": "MOD-4401",
        "area": zone,
        "start_at": "2026-10-17T14:00:00-07:00",
        "end_at": "2026-10-17T16:00:00-07:00",
        "contact_channel": "sim-phone-judge",
    }
    made = c.post("/api/plans", json=body)
    assert made.status_code == 201, made.text
    listed = c.get("/api/plans", params={"status": "active"}).json()
    assert made.json()["plan_id"] in [p["plan_id"] for p in listed]
