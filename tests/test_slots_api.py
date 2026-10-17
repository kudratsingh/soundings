"""WO-082 API tests: compute, accept and decline a slot proposal over /api/plans.

A proposal changes nothing until an operator accepts it (CLAUDE.md 2.2: accept is a human
HTTP action; nothing is sent to any vehicle).
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from soundings.api import app
from soundings.canonical import canonical_json
from soundings.registry.router import REGISTRY_HTML, get_store
from soundings.registry.store import RegistryStore
from soundings.schemas import load_harbor
from tests.support.slot_cases import NOW, SIX_EXPECTED_SHIFT, at, six_plan_bodies

HARBOR = load_harbor()
NOW_Q = {"now": NOW.isoformat()}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "slots.db"


@pytest.fixture
def store(db: Path) -> Iterator[RegistryStore]:
    s = RegistryStore(db, harbor=HARBOR, now=lambda: NOW)
    yield s
    s.close()


@pytest.fixture
def client(store: RegistryStore) -> Iterator[TestClient]:
    app.dependency_overrides[get_store] = lambda: store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_store, None)


def _file_six(client: TestClient, **changes: dict[str, Any]) -> None:
    for body in six_plan_bodies():
        body.update(changes.get(body["plan_id"].replace("-", "_"), {}))
        r = client.post("/api/plans", json=body)
        assert r.status_code == 201, r.text


def _table_digests(db: Path) -> dict[str, tuple[int, str]]:
    """Row count and content hash per table (rows sorted, so order never matters)."""
    con = sqlite3.connect(db)
    try:
        names = [
            n
            for (n,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        out = {}
        for name in names:
            rows = sorted(repr(r) for r in con.execute(f'SELECT * FROM "{name}"').fetchall())
            out[name] = (len(rows), hashlib.sha256("\n".join(rows).encode()).hexdigest())
        return out
    finally:
        con.close()


def _plans_json(store: RegistryStore) -> str:
    return canonical_json([p.model_dump(mode="json") for p in store.list("all")])


def _propose(client: TestClient, plan_id: str = "P-B") -> dict[str, Any]:
    r = client.post(f"/api/plans/{plan_id}/proposal", params=NOW_Q)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


# --- exit tests (directive, verbatim) ------------------------------------------------------


def test_accept_updates_and_clears(client: TestClient, store: RegistryStore) -> None:
    """an accepted proposal updates the plan and clears the conflict"""
    _file_six(client)
    before_b = store.get("P-B")
    before_f = store.get("P-F")
    assert before_b is not None and before_f is not None
    assert before_b.status.value == "conflicted"
    proposal = _propose(client)
    assert proposal["status"] == "feasible" and proposal["total_delay_min"] == 210
    assert {s["plan_id"]: s["shift_min"] for s in proposal["shifts"]} == SIX_EXPECTED_SHIFT

    r = client.post("/api/plans/P-B/proposal/accept", json={"proposal_id": proposal["proposal_id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    # ordered by the new (start_at, plan_id): P-B and P-C now start at 11:00
    assert [p["plan_id"] for p in body["plans"]] == ["P-F", "P-A", "P-D", "P-B", "P-C", "P-E"]

    b, c, f = store.get("P-B"), store.get("P-C"), store.get("P-F")
    assert b is not None and c is not None and f is not None
    assert (b.start_at, b.end_at) == (at("11:00"), at("13:00"))
    assert (c.start_at, c.end_at) == (at("11:00"), at("13:00"))
    assert before_b.tick_s is not None and before_b.start_tick is not None
    ticks = round(120 * 60 / before_b.tick_s)
    assert (b.start_tick, b.end_tick) == (before_b.start_tick + ticks, before_b.end_tick + ticks)
    for plan in store.list("all"):
        assert plan.status.value == "active", plan.plan_id
        assert all(cv.resolved_by == proposal["proposal_id"] for cv in plan.conflicts)
    # P-F (in the water) keeps its row: only the conflict bookkeeping may differ
    keep = {"status", "conflicts"}
    assert f.model_dump(exclude=keep) == before_f.model_dump(exclude=keep)
    assert canonical_json(f.model_dump(mode="json", exclude=keep)) == canonical_json(
        before_f.model_dump(mode="json", exclude=keep)
    )
    stored = store.get_proposal(proposal["proposal_id"])
    assert stored is not None and stored.status.value == "accepted"


def test_decline_changes_nothing(client: TestClient, store: RegistryStore, db: Path) -> None:
    """a declined one leaves everything unchanged"""
    _file_six(client)
    snapshot = _plans_json(store)
    tables = _table_digests(db)
    proposal = _propose(client)
    assert _plans_json(store) == snapshot
    r = client.post(
        "/api/plans/P-B/proposal/decline", json={"proposal_id": proposal["proposal_id"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "declined"
    assert _plans_json(store) == snapshot
    after = _table_digests(db)
    assert {k: v for k, v in after.items() if k != "registry_proposal"} == {
        k: v for k, v in tables.items() if k != "registry_proposal"
    }
    assert after["registry_proposal"][0] == 1
    stored = store.get_proposal(proposal["proposal_id"])
    assert stored is not None and stored.status.value == "declined"
    assert client.get("/api/plans/P-B/proposal").status_code == 404
    again = client.post(
        "/api/plans/P-B/proposal/decline", json={"proposal_id": proposal["proposal_id"]}
    )
    assert again.status_code == 409 and again.json()["reason"] == "not_pending"


def test_accept_infeasible_returns_422(client: TestClient, store: RegistryStore) -> None:
    _file_six(client, P_C={"latest_start_at": at("10:30").isoformat()})
    proposal = _propose(client)
    assert proposal["status"] == "infeasible"
    assert proposal["binding_constraint"] == "latest_start"
    assert proposal["detail"] == "no feasible slot: latest_start (P-C latest_start 10:30)"
    snapshot = _plans_json(store)
    r = client.post("/api/plans/P-B/proposal/accept", json={"proposal_id": proposal["proposal_id"]})
    assert r.status_code == 422 and r.json()["reason"] == "infeasible"
    assert _plans_json(store) == snapshot


# --- the rest of the API --------------------------------------------------------------------


def test_nothing_changes_until_accept(client: TestClient, store: RegistryStore, db: Path) -> None:
    _file_six(client)
    before = _table_digests(db)
    snapshot = _plans_json(store)
    proposal = _propose(client)
    after = _table_digests(db)
    changed = sorted(k for k in after if after[k] != before.get(k))
    assert changed == ["registry_proposal"]
    assert after["registry_proposal"][0] == before["registry_proposal"][0] + 1
    assert _plans_json(store) == snapshot
    # computing the same proposal again is idempotent: same id, one row
    assert _propose(client)["proposal_id"] == proposal["proposal_id"]
    assert _table_digests(db)["registry_proposal"][0] == after["registry_proposal"][0]
    got = client.get("/api/plans/P-B/proposal")
    assert got.status_code == 200 and got.json() == proposal


def test_stale_proposal_409_changes_nothing(client: TestClient, store: RegistryStore) -> None:
    _file_six(client)
    proposal = _propose(client)
    late = six_plan_bodies()[1] | {
        "plan_id": "P-G",
        "modem_id": "MOD-4199",
        "start_at": at("12:00").isoformat(),
        "end_at": at("12:30").isoformat(),
        "earliest_start_at": None,
        "latest_start_at": None,
    }
    assert client.post("/api/plans", json=late).status_code == 201
    snapshot = _plans_json(store)
    r = client.post("/api/plans/P-B/proposal/accept", json={"proposal_id": proposal["proposal_id"]})
    assert r.status_code == 409 and r.json()["reason"] == "stale_proposal"
    assert _plans_json(store) == snapshot
    stored = store.get_proposal(proposal["proposal_id"])
    assert stored is not None and stored.status.value == "stale"


def test_second_accept_409_not_pending(client: TestClient) -> None:
    _file_six(client)
    proposal = _propose(client)
    choice = {"proposal_id": proposal["proposal_id"]}
    assert client.post("/api/plans/P-B/proposal/accept", json=choice).status_code == 200
    r = client.post("/api/plans/P-B/proposal/accept", json=choice)
    assert r.status_code == 409 and r.json()["reason"] == "not_pending"


def test_unknown_plan_404(client: TestClient) -> None:
    assert client.post("/api/plans/P-NOPE/proposal", params=NOW_Q).status_code == 404
    assert client.get("/api/plans/P-NOPE/proposal").status_code == 404
    _file_six(client)
    r = client.post("/api/plans/P-B/proposal/accept", json={"proposal_id": "SP-000000000000"})
    assert r.status_code == 404


def test_not_conflicted_plan_409(client: TestClient) -> None:
    _file_six(client)
    r = client.post("/api/plans/P-E/proposal", params=NOW_Q)
    assert r.status_code == 409 and r.json()["reason"] == "not_conflicted"


def test_proposal_for_other_plan_404(client: TestClient) -> None:
    _file_six(client)
    proposal = _propose(client)
    r = client.post("/api/plans/P-C/proposal/accept", json={"proposal_id": proposal["proposal_id"]})
    assert r.status_code == 404


def test_no_tick_scale_422_writes_nothing(
    client: TestClient, store: RegistryStore, db: Path
) -> None:
    _file_six(client, P_B={"tick_s": None})
    proposal = _propose(client)
    before = _table_digests(db)
    r = client.post("/api/plans/P-B/proposal/accept", json={"proposal_id": proposal["proposal_id"]})
    assert r.status_code == 422 and r.json()["reason"] == "no_tick_scale"
    assert _table_digests(db) == before


def test_default_now_is_store_clock(client: TestClient) -> None:
    _file_six(client)
    r = client.post("/api/plans/P-B/proposal")
    assert r.status_code == 200
    assert r.json()["now"] == NOW.isoformat()  # the store clock, not the wall clock


def test_form_has_proposal_panel() -> None:
    html = REGISTRY_HTML.read_text(encoding="utf-8")
    assert "<!-- WO-082: slot proposals -->" in html
    for needle in ("Propose slot", "proposed slot", "Accept", "Decline", "in the water, not moved"):
        assert needle in html, needle
    assert "/proposal/accept" in html and "/proposal/decline" in html
