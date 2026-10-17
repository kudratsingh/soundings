from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soundings.api import app, create_app


def test_health_exact_json() -> None:
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "model_mode": "replay"}


def test_health_reports_env_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_MODE", "record")
    assert TestClient(app).get("/health").json() == {"ok": True, "model_mode": "record"}


def test_health_defaults_to_replay_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_MODE", raising=False)
    assert TestClient(app).get("/health").json()["model_mode"] == "replay"


def test_app_builds_without_web_dist(tmp_path: Path) -> None:
    client = TestClient(create_app(web_dist=tmp_path / "missing"))
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 404


def test_static_mount_serves_index_and_health_still_wins(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<title>Soundings</title>", encoding="utf-8")
    client = TestClient(create_app(web_dist=tmp_path))
    assert "Soundings" in client.get("/").text
    assert client.get("/health").json()["ok"] is True
