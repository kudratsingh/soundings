"""WO-062C: the demo's scripted plan filing (``auto_file`` in docs/demo-script.yaml).

The schema is additive (a step without it validates as before) and rejects malformed
blocks; the served script resolves the yaml's local HH:MM times to POST /api/plans bodies
for today + day_offset; filing those bodies into an empty registry conflicts the plan and
the WO-082 solver returns an OPTIMAL proposal that moves only that plan; and the presenter
log keeps the two auto entries with their times. No browser, no network, no model.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from soundings.api import create_app
from soundings.demo import presenter
from soundings.demo.presenter import AutoNote, PresenterEventKind, PresenterLog, step_payload
from soundings.demo.script import AutoFile, DemoScript, DemoStep
from soundings.registry.models import PlanStatus
from soundings.registry.store import RegistryStore
from soundings.schemas._base import from_file_data

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "docs" / "demo-script.yaml"
BUSY_TUESDAY = 2


def _raw() -> dict[str, Any]:
    return yaml.safe_load(SCRIPT.read_text(encoding="utf-8"))


def _load(raw: dict[str, Any]) -> DemoScript:
    return DemoScript.model_validate(from_file_data(raw))


def test_busy_tuesday_declares_auto_file() -> None:
    script = DemoScript.load(SCRIPT)
    step = script.steps[BUSY_TUESDAY]
    assert step.scenario == "S01" and step.seed == 2
    auto = step.auto_file
    assert auto is not None
    assert (auto.at_s, auto.accept_after_s, auto.accept_at_s) == (8, 12, 20)
    # S01's own plan is one of the jobs in force (scenarios/S01-filed-survey-on-plan.yaml)
    assert auto.in_force[0].plan_id == "P-2026-1017-001"
    assert {p.area for p in auto.in_force} == {auto.plan.area}
    # every other step stays as it was (the schema is additive)
    assert [i for i, s in enumerate(script.steps) if s.auto_file is not None] == [BUSY_TUESDAY]
    # the filing and its accept land inside the shot (0:30-1:00)
    nxt = script.steps[BUSY_TUESDAY + 1].mark_s
    assert step.mark_s + auto.accept_at_s < nxt


def test_caption_is_unchanged() -> None:
    step = DemoScript.load(SCRIPT).steps[BUSY_TUESDAY]
    assert step.caption == (
        "Busy Tuesday. Filed jobs clear at ask; a plan is filed from a phone, "
        "conflict → proposed slot → accepted."
    )


def test_a_step_without_auto_file_still_validates() -> None:
    raw = _raw()
    del raw["steps"][BUSY_TUESDAY]["auto_file"]
    script = _load(raw)
    assert all(s.auto_file is None for s in script.steps)


def _mutated(path: list[Any], value: Any) -> dict[str, Any]:
    raw = copy.deepcopy(_raw())
    node: Any = raw["steps"][BUSY_TUESDAY]["auto_file"]
    for key in path[:-1]:
        node = node[key]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return raw


_DELETE = object()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["plan"], _DELETE),  # the plan is required
        (["at_s"], -1),
        (["at_s"], "8"),  # strict: a number, not a string
        (["accept_after_s"], 0),
        (["accept_after_s"], 40),  # accept at +48 s falls outside the 30 s shot
        (["utc_offset"], "PDT"),
        (["day_offset"], -1),
        (["surprise"], True),  # unknown keys are rejected
        (["plan", "start"], "2pm"),
        (["plan", "end"], "14:00"),  # end before start
        (["plan", "area"], "Inner Basin"),
        (["plan", "plan_id"], "P-2026-1017-001"),  # duplicates an in-force id
        (["plan", "depth_max_m"], _DELETE),  # one end of the depth band only
        (["plan", "operator"], 123),
        (["in_force", 0, "start"], "25:00"),
    ],
)
def test_malformed_auto_file_is_rejected(path: list[Any], value: Any) -> None:
    with pytest.raises(ValidationError):
        _load(_mutated(path, value))


def test_auto_file_only_on_a_catalog_step() -> None:
    raw = copy.deepcopy(_raw())
    raw["steps"][5]["auto_file"] = raw["steps"][BUSY_TUESDAY]["auto_file"]
    with pytest.raises(ValidationError, match="catalog scenario step"):
        _load(raw)


def _bodies(today: date) -> dict[str, Any]:
    step = DemoScript.load(SCRIPT).steps[BUSY_TUESDAY]
    return step_payload(BUSY_TUESDAY, step, today=today)["auto_file"]


def test_payload_resolves_bodies_for_today_plus_offset() -> None:
    auto = _bodies(date(2026, 10, 17))
    assert auto["accept_at_s"] == 20
    plan = auto["plan"]
    assert plan["plan_id"] == "P-PHONE-1"
    assert plan["start_at"] == "2026-10-18T14:30:00-07:00"
    assert plan["end_at"] == "2026-10-18T16:00:00-07:00"
    assert plan["source"] == "form"  # the same body the /registry form sends
    assert [b["start_at"][:10] for b in auto["in_force"]] == ["2026-10-18", "2026-10-18"]


def test_served_script_carries_the_bodies() -> None:
    body = TestClient(create_app()).get("/api/demo-script").json()
    auto = body["steps"][BUSY_TUESDAY]["auto_file"]
    assert auto["plan"]["plan_id"] == "P-PHONE-1" and len(auto["in_force"]) == 2
    assert all("auto_file" not in s for i, s in enumerate(body["steps"]) if i != BUSY_TUESDAY)


def test_filed_plan_conflicts_and_the_proposal_is_optimal(tmp_path: Path) -> None:
    today = datetime.now().astimezone().date()
    auto = _bodies(today)
    store = RegistryStore(tmp_path / "auto.db")
    try:
        in_force = [store.create(dict(b)) for b in auto["in_force"]]
        assert [p.status for p in in_force] == [PlanStatus.ACTIVE, PlanStatus.ACTIVE]
        filed = store.create(dict(auto["plan"]))
        assert filed.status is PlanStatus.CONFLICTED
        assert filed.conflicts[0].other_plan_id == "P-2026-1017-001"
        proposal = store.propose(filed.plan_id)
        assert proposal.status == "feasible" and proposal.optimal
        moved = [s.plan_id for s in proposal.shifts if s.shift_min != 0]
        assert moved == ["P-PHONE-1"]
        store.save_proposal(proposal)
        stored, _ = store.accept_proposal(proposal.proposal_id)
        assert stored.status.value == "accepted"
        assert {p.status for p in store.list(PlanStatus.ACTIVE)} == {PlanStatus.ACTIVE}
    finally:
        store.close()


def test_presenter_log_keeps_the_auto_entries_with_their_times() -> None:
    clock = iter([30.0, 38.0, 50.0])
    log = PresenterLog(monotonic=lambda: next(clock))
    step: DemoStep = DemoScript.load(SCRIPT).steps[BUSY_TUESDAY]
    log.record(PresenterEventKind.STEP, BUSY_TUESDAY, step)
    log.record(PresenterEventKind.AUTO, BUSY_TUESDAY, step, AutoNote.PLAN_FILED)
    log.record(PresenterEventKind.AUTO, BUSY_TUESDAY, step, AutoNote.PROPOSAL_ACCEPTED)
    autos = [e.as_json() for e in log.entries() if e.kind == "auto"]
    assert [(e["note"], e["t_s"]) for e in autos] == [
        ("auto: plan filed (stands in for the phone)", 8.0),
        ("auto: proposal accepted", 20.0),
    ]
    auto = step.auto_file
    assert isinstance(auto, AutoFile)
    assert [e["t_s"] for e in autos] == [auto.at_s, auto.accept_at_s]


def test_presenter_log_auto_entries_over_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(presenter, "_LOG", PresenterLog())
    client = TestClient(create_app())
    client.post("/api/presenter-log", json={"index": BUSY_TUESDAY})
    for note in (AutoNote.PLAN_FILED, AutoNote.PROPOSAL_ACCEPTED):
        r = client.post(
            "/api/presenter-log", json={"index": BUSY_TUESDAY, "kind": "auto", "note": note}
        )
        assert r.status_code == 200, r.text
    log = client.get("/api/presenter-log").json()
    assert [(e["kind"], e["note"]) for e in log] == [
        ("step", ""),
        ("auto", "auto: plan filed (stands in for the phone)"),
        ("auto", "auto: proposal accepted"),
    ]
    assert log[1]["t_s"] <= log[2]["t_s"]
    # a note is fixed text: free text, a note on a step entry, or auto without one are 422
    bad = [
        {"index": 2, "kind": "auto", "note": "anything"},
        {"index": 2, "kind": "step", "note": AutoNote.PLAN_FILED},
        {"index": 2, "kind": "auto"},
    ]
    for body in bad:
        assert client.post("/api/presenter-log", json=body).status_code == 422


def test_record_files_into_a_throwaway_registry() -> None:
    src = (REPO / "soundings" / "demo" / "record.py").read_text(encoding="utf-8")
    assert 'os.environ["SOUNDINGS_DB"]' in src and "TemporaryDirectory" in src


def test_day_offset_keeps_plans_in_the_future() -> None:
    raw = _raw()["steps"][BUSY_TUESDAY]["auto_file"]
    auto = AutoFile.model_validate(from_file_data(raw))
    today = date(2026, 10, 17)
    day = today + timedelta(days=auto.day_offset)
    assert day > today
