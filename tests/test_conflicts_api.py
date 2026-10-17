"""WO-081 API tests: conflicts over /api/plans, the form render contract, retention.

Hard rule exercised: CLAUDE.md section 6 (conflict rows go with their plan in the 30-day
purge) by ``test_purge_removes_conflicts``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from soundings.api import app
from soundings.registry.models import PlanConflict
from soundings.registry.router import REGISTRY_HTML, get_store
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
        "start_at": "2026-10-17T13:00:00-07:00",
        "end_at": "2026-10-17T16:00:00-07:00",
        "contact_channel": "radio ch 16",
    }
    data.update(overrides)
    return data


@pytest.fixture
def store(tmp_path: Path) -> RegistryStore:
    return RegistryStore(tmp_path / "conf.db", harbor=HARBOR, now=lambda: NOW)


@pytest.fixture
def client(store: RegistryStore) -> Iterator[TestClient]:
    app.dependency_overrides[get_store] = lambda: store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_store, None)
        store.close()


def _file_pair(client: TestClient) -> dict[str, Any]:
    first = client.post("/api/plans", json=plan_json(plan_id="P-X"))
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "active"
    assert first.json()["conflicts"] == []
    second = client.post(
        "/api/plans",
        json=plan_json(
            plan_id="P-Y",
            modem_id="MOD-5678",
            start_at="2026-10-17T14:00:00-07:00",
            end_at="2026-10-17T18:00:00-07:00",
        ),
    )
    assert second.status_code == 201, second.text
    body: dict[str, Any] = second.json()
    return body


def test_conflicting_post_returns_201_conflicted(client: TestClient) -> None:
    body = _file_pair(client)
    assert body["status"] == "conflicted"
    assert body["conflicts"] == [
        {
            "other_plan_id": "P-X",
            "overlap_start": "2026-10-17T14:00:00-07:00",
            "overlap_end": "2026-10-17T16:00:00-07:00",
            "overlap_minutes": 120,
            "reasons": ["area", "depth", "window"],
            "resolved_by": None,
        }
    ]


def test_get_shows_conflict_on_both_plans(client: TestClient) -> None:
    _file_pair(client)
    x = client.get("/api/plans/P-X").json()
    y = client.get("/api/plans/P-Y").json()
    assert x["status"] == y["status"] == "conflicted"
    assert [c["other_plan_id"] for c in x["conflicts"]] == ["P-Y"]
    assert [c["other_plan_id"] for c in y["conflicts"]] == ["P-X"]
    listed = {p["plan_id"]: p for p in client.get("/api/plans").json()}
    assert sorted(listed) == ["P-X", "P-Y"]  # active means in force
    assert listed["P-X"]["conflicts"] == x["conflicts"]
    assert listed["P-Y"]["conflicts"] == y["conflicts"]
    alone = client.get("/api/plans/P-X/conflicts")
    assert alone.status_code == 200
    assert alone.json() == x["conflicts"]
    assert client.get("/api/plans/P-NOPE/conflicts").status_code == 404


def test_status_conflicted_filters(client: TestClient) -> None:
    _file_pair(client)
    lone = client.post(
        "/api/plans", json=plan_json(plan_id="P-Z", area="pier_400_approach", modem_id="MOD-9999")
    )
    assert lone.status_code == 201 and lone.json()["status"] == "active"
    conflicted = client.get("/api/plans", params={"status": "conflicted"}).json()
    assert [p["plan_id"] for p in conflicted] == ["P-X", "P-Y"]
    active = client.get("/api/plans", params={"status": "active"}).json()
    assert [p["plan_id"] for p in active] == ["P-X", "P-Z", "P-Y"]  # (start_at, plan_id)
    ranged = client.get(
        "/api/plans",
        params={
            "status": "conflicted",
            "start_at": "2026-10-17T13:30:00-07:00",
            "end_at": "2026-10-17T13:45:00-07:00",
        },
    ).json()
    assert [p["plan_id"] for p in ranged] == ["P-X"]


def test_expire_returns_partner_to_active(client: TestClient, store: RegistryStore) -> None:
    _file_pair(client)
    resp = client.post("/api/plans/P-X/expire")
    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"
    assert resp.json()["conflicts"] == []
    y = client.get("/api/plans/P-Y").json()
    assert y["status"] == "active"
    assert y["conflicts"] == []
    with Session(store.engine) as s:
        assert s.exec(select(PlanConflict)).all() == []


def test_partner_with_other_conflicts_stays_conflicted(client: TestClient) -> None:
    _file_pair(client)
    third = client.post(
        "/api/plans",
        json=plan_json(
            plan_id="P-W",
            modem_id="MOD-7777",
            start_at="2026-10-17T17:00:00-07:00",
            end_at="2026-10-17T19:00:00-07:00",
        ),
    )
    assert third.json()["status"] == "conflicted"
    assert [c["other_plan_id"] for c in third.json()["conflicts"]] == ["P-Y"]
    client.post("/api/plans/P-X/expire")
    y = client.get("/api/plans/P-Y").json()
    assert y["status"] == "conflicted"
    assert [c["other_plan_id"] for c in y["conflicts"]] == ["P-W"]


def test_rejected_plan_creates_no_conflict_rows(client: TestClient, store: RegistryStore) -> None:
    assert client.post("/api/plans", json=plan_json(plan_id="P-X")).status_code == 201
    bad = client.post(
        "/api/plans",
        json=plan_json(plan_id="P-BAD", depth_min_m=20, depth_max_m=5, modem_id="MOD-5678"),
    )
    assert bad.status_code == 422
    assert bad.json()["reason"] == "depth_band_inverted"
    bad_modem = client.post("/api/plans", json=plan_json(plan_id="P-BAD2", modem_id="nope"))
    assert bad_modem.status_code == 422
    with Session(store.engine) as s:
        assert s.exec(select(PlanConflict)).all() == []
    assert client.get("/api/plans/P-X").json()["status"] == "active"


def test_job_type_and_depth_round_trip(client: TestClient) -> None:
    resp = client.post(
        "/api/plans",
        json=plan_json(plan_id="P-J", job_type="dredge", depth_min_m=5, depth_max_m=9),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["job_type"], body["depth_min_m"], body["depth_max_m"]) == ("dredge", 5.0, 9.0)
    blank = client.post(
        "/api/plans",
        json=plan_json(
            plan_id="P-K", job_type="", depth_min_m="", depth_max_m="", area="main_channel"
        ),
    )
    assert blank.status_code == 201, blank.text
    assert blank.json()["job_type"] == "other"
    unknown = client.post("/api/plans", json=plan_json(plan_id="P-U", job_type="welding"))
    assert unknown.status_code == 422


def test_depth_bands_apart_do_not_conflict(client: TestClient) -> None:
    assert (
        client.post(
            "/api/plans", json=plan_json(plan_id="P-A", depth_min_m=0, depth_max_m=10)
        ).json()["status"]
        == "active"
    )
    b = client.post(
        "/api/plans",
        json=plan_json(plan_id="P-B", modem_id="MOD-5678", depth_min_m=10, depth_max_m=20),
    )
    assert b.json()["status"] == "active" and b.json()["conflicts"] == []


def test_purge_removes_conflicts(client: TestClient, store: RegistryStore) -> None:
    _file_pair(client)
    client.post(
        "/api/plans",
        json=plan_json(
            plan_id="P-LATER",
            modem_id="MOD-4444",
            start_at="2026-11-30T13:00:00-07:00",
            end_at="2026-11-30T16:00:00-07:00",
        ),
    )
    purged = store.purge_older_than(30, now=NOW + timedelta(days=31))
    assert purged == 2
    with Session(store.engine) as s:
        assert s.exec(select(PlanConflict)).all() == []
    assert store.get("P-LATER") is not None


def test_form_render_contract(client: TestClient) -> None:
    page = client.get("/registry")
    assert page.status_code == 200
    html = page.text
    assert html == REGISTRY_HTML.read_text(encoding="utf-8")
    assert '"conflicts with "' in html
    assert "<th>Conflicts</th>" in html
    for field in ("job_type", "depth_min_m", "depth_max_m"):
        assert f'id="{field}"' in html
    assert "status=active" in html  # the table lists plans in force, conflicted included
