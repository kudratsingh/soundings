"""WO-020 router tests: the REST surface under /api/plans, the /registry form, and the
mount order (Phase 0's /health and the web/dist mount still behave).

``get_store`` is overridden with a tmp-path store with a fixed clock, so no test touches
the developer's database.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soundings.api import app, create_app
from soundings.registry.router import get_store
from soundings.registry.store import RegistryStore
from soundings.schemas import load_harbor

NOW = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)
HARBOR = load_harbor()


def plan_json(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "operator": "Harbor Survey Co (demo)",
        "vehicle": "ROV-Blue-1",
        "modem_id": "MOD-1234",
        "area": "inner_basin_research",
        "start_tick": 100,
        "end_tick": 200,
        "start_at": "2026-10-17T14:00:00-07:00",
        "end_at": "2026-10-17T16:00:00-07:00",
        "contact_channel": "radio ch 16",
    }
    data.update(overrides)
    return data


@pytest.fixture
def store(tmp_path: Path) -> RegistryStore:
    return RegistryStore(tmp_path / "api.db", harbor=HARBOR, now=lambda: NOW)


def _client(application: FastAPI, store: RegistryStore) -> Iterator[TestClient]:
    application.dependency_overrides[get_store] = lambda: store
    try:
        yield TestClient(application)
    finally:
        application.dependency_overrides.pop(get_store, None)
        store.close()


@pytest.fixture
def client(store: RegistryStore) -> Iterator[TestClient]:
    yield from _client(app, store)


def test_post_201_then_get_200(client: TestClient) -> None:
    resp = client.post("/api/plans", json=plan_json(plan_id="P-API-001"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["plan_id"] == "P-API-001"
    assert body["status"] == "active"
    assert body["start_at"] == "2026-10-17T14:00:00-07:00"
    got = client.get("/api/plans/P-API-001")
    assert got.status_code == 200
    assert got.json() == body
    assert client.get("/api/plans/P-NOPE").status_code == 404


def test_post_assigns_plan_id(client: TestClient) -> None:
    resp = client.post("/api/plans", json=plan_json(plan_id="", source="form"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["plan_id"] == "P-20261017-001"
    assert resp.json()["source"] == "form"


def test_post_bad_zone_422_reason(client: TestClient, store: RegistryStore) -> None:
    resp = client.post("/api/plans", json=plan_json(area="not_a_zone"))
    assert resp.status_code == 422
    assert resp.json()["reason"] == "area_not_security_zone"
    assert "inner_basin_research" in resp.json()["detail"]
    assert store.count() == 0


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"start_tick": None, "end_tick": None, "start_at": None, "end_at": None},
            "window_missing",
        ),
        ({"start_tick": 9, "end_tick": 1}, "window_inverted"),
        ({"modem_id": "XYZ"}, "bad_modem_id"),
    ],
)
def test_post_reject_reasons(
    client: TestClient, store: RegistryStore, overrides: dict[str, Any], reason: str
) -> None:
    resp = client.post("/api/plans", json=plan_json(**overrides))
    assert resp.status_code == 422
    assert resp.json()["reason"] == reason
    assert store.count() == 0


def test_post_duplicate_422(client: TestClient) -> None:
    assert client.post("/api/plans", json=plan_json(plan_id="P-DUP")).status_code == 201
    resp = client.post("/api/plans", json=plan_json(plan_id="P-DUP"))
    assert resp.status_code == 422
    assert resp.json()["reason"] == "duplicate_plan_id"


def test_post_malformed_field_422(client: TestClient, store: RegistryStore) -> None:
    resp = client.post("/api/plans", json=plan_json(start_tick="soon"))
    assert resp.status_code == 422
    assert "detail" in resp.json()
    naive = client.post("/api/plans", json=plan_json(start_at="2026-10-17T14:00:00"))
    assert naive.status_code == 422
    assert store.count() == 0


def test_post_tick_only_with_epoch_query(client: TestClient) -> None:
    resp = client.post(
        "/api/plans",
        params={"epoch": "2026-10-17T14:00:00-07:00", "tick_s": 2},
        json=plan_json(plan_id="P-TICK", start_at=None, end_at=None),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["start_at"] == "2026-10-17T14:03:20-07:00"


def test_get_tick_overlap_returns_only_overlapping(client: TestClient) -> None:
    windows = {
        "P-EARLY": (0, 399),
        "P-HIT": (380, 405),
        "P-TOUCH": (410, 500),
        "P-LATE": (411, 600),
    }
    for pid, (s, e) in windows.items():
        assert (
            client.post(
                "/api/plans", json=plan_json(plan_id=pid, start_tick=s, end_tick=e)
            ).status_code
            == 201
        )
    resp = client.get("/api/plans", params={"start_tick": 400, "end_tick": 410})
    assert resp.status_code == 200
    assert [p["plan_id"] for p in resp.json()] == ["P-HIT", "P-TOUCH"]
    assert client.get("/api/plans", params={"start_tick": 400}).status_code == 422


def test_get_time_overlap(client: TestClient) -> None:
    client.post("/api/plans", json=plan_json(plan_id="P-T1"))
    client.post(
        "/api/plans",
        json=plan_json(
            plan_id="P-T2", start_at="2026-10-17T18:00:00-07:00", end_at="2026-10-17T19:00:00-07:00"
        ),
    )
    resp = client.get(
        "/api/plans",
        params={"start_at": "2026-10-17T22:30:00+00:00", "end_at": "2026-10-17T23:00:00+00:00"},
    )
    assert [p["plan_id"] for p in resp.json()] == ["P-T1"]


def test_expire_then_list_excludes(client: TestClient) -> None:
    client.post("/api/plans", json=plan_json(plan_id="P-KEEP"))
    client.post("/api/plans", json=plan_json(plan_id="P-GONE"))
    resp = client.post("/api/plans/P-GONE/expire")
    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"
    assert resp.json()["expired_at"] == "2026-10-17T20:00:00Z"
    assert [p["plan_id"] for p in client.get("/api/plans").json()] == ["P-KEEP"]
    assert [p["plan_id"] for p in client.get("/api/plans", params={"status": "active"}).json()] == [
        "P-KEEP"
    ]
    ranged = client.get("/api/plans", params={"start_tick": 150, "end_tick": 150}).json()
    assert [p["plan_id"] for p in ranged] == ["P-KEEP"]
    assert [
        p["plan_id"] for p in client.get("/api/plans", params={"status": "expired"}).json()
    ] == ["P-GONE"]
    assert len(client.get("/api/plans", params={"status": "all"}).json()) == 2
    assert client.post("/api/plans/P-NOPE/expire").status_code == 404


def test_no_delete_route(client: TestClient) -> None:
    client.post("/api/plans", json=plan_json(plan_id="P-STAY"))
    assert client.delete("/api/plans/P-STAY").status_code == 405
    assert client.get("/api/plans/P-STAY").status_code == 200


def test_zones_exact_sorted(client: TestClient) -> None:
    resp = client.get("/api/plans/zones")
    assert resp.status_code == 200
    ids = [z["zone_id"] for z in resp.json()]
    assert ids == sorted(z.zone_id for z in HARBOR.zones)
    assert set(resp.json()[0]) == {"zone_id", "name", "criticality"}


def test_registry_form_page(client: TestClient) -> None:
    resp = client.get("/registry")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<form" in resp.text
    assert "/api/plans/zones" in resp.text
    assert (
        "Registry entries are retained for at most 30 days. Simplified for demonstration."
        in resp.text
    )


def test_health_unchanged(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "model_mode": "replay"}


def test_routes_win_over_web_dist_mount(tmp_path: Path, store: RegistryStore) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<title>Soundings</title>", encoding="utf-8")
    for c in _client(create_app(web_dist=dist), store):
        assert "Soundings" in c.get("/").text
        assert "<form" in c.get("/registry").text
        assert c.get("/api/plans/zones").status_code == 200
        assert c.get("/health").json() == {"ok": True, "model_mode": "replay"}


def test_get_store_reads_env_at_request_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("soundings.registry.router._STORES", {})
    first, second = tmp_path / "one.db", tmp_path / "two.db"
    monkeypatch.setenv("SOUNDINGS_DB", str(first))
    a = get_store()
    monkeypatch.setenv("SOUNDINGS_DB", str(second))
    b = get_store()
    assert (a.db_path, b.db_path) == (str(first), str(second))
    assert first.is_file() and second.is_file()
    assert get_store() is b
