"""WO-082 unit tests: the slot model, its optimum, determinism, diagnosis and rule proofs.

Hard rules exercised: CLAUDE.md 2.1/2.2 (``test_proposal_is_not_control``: a proposal is
not an action and nothing accepts on its own) and 2.4 (``-k deterministic``,
``test_no_clock``).
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import os
import random
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from soundings.canonical import canonical_json
from soundings.registry.models import REGISTRY_CONFIG_PATH, PlanRejected, RejectReason
from soundings.registry.slots import (
    WITHIN_CAPACITY_DETAIL,
    BindingConstraint,
    PinReason,
    SlotConfig,
    SlotProposal,
    build_model,
    build_specs,
    load_slot_config,
    make_solver,
    model_set,
    propose_slots,
)
from soundings.registry.store import RegistryStore
from soundings.schemas.harbor import load_harbor
from tests.support.busy_plans import bt01_or_standin
from tests.support.slot_cases import (
    NOW,
    SIX,
    SIX_EXPECTED_SHIFT,
    SIX_OPTIMUM_MIN,
    ZONE,
    at,
    six_plans,
    view,
)

ROOT = Path(__file__).resolve().parents[1]
SLOTS_PY = ROOT / "soundings" / "registry" / "slots.py"
HARBOR = load_harbor()
CFG = load_slot_config()


def _cfg(**capacity: int) -> SlotConfig:
    return CFG.model_copy(update={"zone_capacity": dict(capacity)})


def _digest(p: SlotProposal) -> str:
    return hashlib.sha256(canonical_json(p).encode()).hexdigest()


# --- brute-force reference (pure Python, no ortools) ---------------------------------------


def brute_force_reference(cfg: SlotConfig, capacity: int) -> list[tuple[int, dict[str, int]]]:
    """Every capacity-respecting shift vector of the six-plan table, sorted by delay.

    Movable plans (conflicted, not yet started at ``NOW``) range over the step grid
    between their earliest and latest starts; the rest stay put. Windows are half-open.
    """
    step = cfg.slot_step_min
    choices: dict[str, list[int]] = {}
    for pid, (start, _end, earliest, latest, conflicted) in SIX.items():
        if not conflicted or at(start) <= NOW:
            choices[pid] = [0]
            continue
        lo = at(earliest or start)
        hi = at(latest) if latest else at(start) + timedelta(minutes=cfg.latest_start_slack_min)
        first = -int((at(start) - lo).total_seconds() // 60 // step)
        last = int((hi - at(start)).total_seconds() // 60 // step)
        choices[pid] = [k * step for k in range(first, last + 1)]
    ids = sorted(SIX)
    out: list[tuple[int, dict[str, int]]] = []
    for combo in itertools.product(*(choices[p] for p in ids)):
        shift = dict(zip(ids, combo, strict=True))
        spans = []
        for pid in ids:
            s, e = at(SIX[pid][0]), at(SIX[pid][1])
            d = timedelta(minutes=shift[pid])
            spans.append((s + d, e + d))
        ok = all(sum(1 for s, e in spans if s <= t < e) <= capacity for t, _ in spans)
        if ok:
            out.append((sum(abs(v) for v in combo), shift))
    return sorted(out, key=lambda x: x[0])


# --- exit tests (directive, verbatim) ------------------------------------------------------


def test_six_plan_optimum() -> None:
    """hand-built 6-plan case has a known optimum and the solver returns it"""
    capacity = CFG.capacity_for(ZONE)
    ref = brute_force_reference(CFG, capacity)
    assert ref[0][0] == SIX_OPTIMUM_MIN == 210
    assert ref[0][1] == SIX_EXPECTED_SHIFT
    assert sum(1 for d, _ in ref if d == ref[0][0]) == 1, "the optimum must be unique"
    assert min(d for d, _ in ref if d > ref[0][0]) == 225

    p = propose_slots("P-B", six_plans(), harbor=HARBOR, now=NOW)
    assert p.status == "feasible"
    assert p.optimal is True
    assert p.solver_status == "OPTIMAL"
    assert p.total_delay_min == 210
    assert {s.plan_id: s.shift_min for s in p.shifts} == SIX_EXPECTED_SHIFT
    pins = {s.plan_id: s.pinned for s in p.shifts}
    assert pins["P-F"] is PinReason.IN_WATER
    assert pins["P-E"] is PinReason.NOT_CONFLICTED
    by_id = {s.plan_id: s for s in p.shifts}
    assert (by_id["P-B"].new_start_at, by_id["P-B"].new_end_at) == (at("11:00"), at("13:00"))
    assert (by_id["P-C"].new_start_at, by_id["P-C"].new_end_at) == (at("11:00"), at("13:00"))
    assert p.capacity == capacity
    assert p.proposal_id == "SP-" + p.inputs_hash[:12]
    assert p.binding_constraint is None


def test_infeasible_names_binding() -> None:
    """infeasible -> explicit "no feasible slot" with the binding constraint named"""
    # (i) P-C latest 10:30
    p1 = propose_slots("P-B", six_plans(P_C="10:30"), harbor=HARBOR, now=NOW)
    # (ii) now 09:15, capacity 1: P-F and P-A (and P-B) are already in the water
    p2 = propose_slots("P-B", six_plans(), harbor=HARBOR, now=at("09:15"), cfg=_cfg(**{ZONE: 1}))
    # (iii) three 600-minute plans in fuel_berths filed 09:00, capacity 1
    three = [view(f"P-Q{i}", "09:00", "19:00", area="fuel_berths", modem=i) for i in range(3)]
    p3 = propose_slots("P-Q0", three, harbor=HARBOR, now=NOW, cfg=_cfg(fuel_berths=1))
    for p, binding in (
        (p1, BindingConstraint.LATEST_START),
        (p2, BindingConstraint.IN_WATER),
        (p3, BindingConstraint.CAPACITY),
    ):
        assert p.status == "infeasible"
        assert p.binding_constraint is binding
        assert p.detail.startswith("no feasible slot:")
        assert p.detail.startswith(f"no feasible slot: {binding.value} (")
        assert p.optimal is False and p.total_delay_min == 0
        assert p.shifts and all(s.shift_min == 0 for s in p.shifts)
    assert p1.detail == "no feasible slot: latest_start (P-C latest_start 10:30)"
    assert "P-F" in p2.detail and "P-A" in p2.detail and "09:00" in p2.detail
    assert "fuel_berths" in p3.detail


# --- determinism ---------------------------------------------------------------------------


def test_deterministic_six_plan_in_process_and_shuffled() -> None:
    first = propose_slots("P-B", six_plans(), harbor=HARBOR, now=NOW)
    second = propose_slots("P-B", six_plans(), harbor=HARBOR, now=NOW)
    assert canonical_json(first) == canonical_json(second)
    for seed in range(5):
        plans = six_plans()
        random.Random(seed).shuffle(plans)
        again = propose_slots("P-B", plans, harbor=HARBOR, now=NOW)
        assert _digest(again) == _digest(first)


_SUBPROCESS = """
import hashlib
from soundings.canonical import canonical_json
from soundings.registry.slots import propose_slots
from soundings.schemas.harbor import load_harbor
from tests.support.slot_cases import NOW, six_plans
p = propose_slots("P-B", six_plans(), harbor=load_harbor(), now=NOW)
print(p.proposal_id, hashlib.sha256(canonical_json(p).encode()).hexdigest())
"""


def test_deterministic_across_hash_seeds() -> None:
    expected = propose_slots("P-B", six_plans(), harbor=HARBOR, now=NOW)
    outputs = []
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "MODEL_MODE": "replay"}
        env["PYTHONPATH"] = str(ROOT)
        done = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        outputs.append(done.stdout.strip())
    assert outputs[0] == outputs[1] == f"{expected.proposal_id} {_digest(expected)}"


def _bt01_views() -> list:  # type: ignore[type-arg]
    from soundings.registry.store import plan_views_from_scenario

    return plan_views_from_scenario(bt01_or_standin())


def test_deterministic_bt01_components() -> None:
    from soundings.registry.models import PlanStatus, scenario_epoch

    scenario = bt01_or_standin()
    views = _bt01_views()
    now = scenario_epoch(scenario)
    seen: set[tuple[str, ...]] = set()
    solved = 0
    for target in views:
        if target.status is not PlanStatus.CONFLICTED:
            continue
        p1 = propose_slots(target.plan_id, views, harbor=HARBOR, now=now)
        if p1.plan_ids in seen:
            continue
        seen.add(p1.plan_ids)
        shuffled = list(views)
        random.Random(len(seen)).shuffle(shuffled)
        p2 = propose_slots(target.plan_id, shuffled, harbor=HARBOR, now=now)
        assert canonical_json(p1) == canonical_json(p2)
        solved += 1
    assert solved >= 1


def test_deterministic_model_construction() -> None:
    plans = six_plans()
    specs = build_specs(plans, now=NOW, cfg=CFG, day_start=at("00:00"))
    random.Random(3).shuffle(plans)
    again = build_specs(plans, now=NOW, cfg=CFG, day_start=at("00:00"))
    assert specs == again
    m1, _ = build_model(specs, 2, CFG)
    m2, _ = build_model(again, 2, CFG)
    assert str(m1.proto) == str(m2.proto)
    assert str(m1.proto).count("cumulative") == 1


# --- model builder -------------------------------------------------------------------------


def test_build_specs_bounds_and_pins() -> None:
    specs = {
        s.plan_id: s for s in build_specs(six_plans(), now=NOW, cfg=CFG, day_start=at("00:00"))
    }
    assert [s.plan_id for s in sorted(specs.values(), key=lambda s: s.rank)] == [
        "P-F",
        "P-A",
        "P-B",
        "P-C",
        "P-D",
        "P-E",
    ]
    assert specs["P-F"].pinned is PinReason.IN_WATER and specs["P-F"].k_hi == 0
    assert specs["P-E"].pinned is PinReason.NOT_CONFLICTED and specs["P-E"].k_lo == 0
    assert (specs["P-A"].k_lo, specs["P-A"].k_hi) == (0, 0)
    assert (specs["P-B"].k_lo, specs["P-B"].k_hi) == (0, 8)
    assert (specs["P-C"].k_lo, specs["P-C"].k_hi) == (0, 8)
    assert specs["P-B"].start_s == 9 * 3600 and specs["P-B"].duration_s == 7200


def test_bounds_snap_to_step_grid() -> None:
    plan = view("P-S", "09:00", "10:00", earliest="08:40", latest="09:40")
    (spec,) = build_specs([plan], now=NOW, cfg=CFG, day_start=at("00:00"))
    assert (spec.k_lo, spec.k_hi) == (-1, 2)  # 08:45 and 09:30: snapped toward 09:00
    plan = view("P-S", "09:00", "10:00", earliest="08:50", latest="09:10")
    (spec,) = build_specs([plan], now=NOW, cfg=CFG, day_start=at("00:00"))
    assert (spec.k_lo, spec.k_hi) == (0, 0)
    # no earlier than now, no later than the horizon
    plan = view("P-S", "08:30", "23:00", earliest="06:00")
    (spec,) = build_specs([plan], now=NOW, cfg=CFG, day_start=at("00:00"))
    assert spec.k_lo == -2 and spec.k_hi == 4  # 08:00 earliest (now); 23:00 + 60 = 24:00


def test_earlier_slot_allowed_by_earliest_bound() -> None:
    plans = [
        view("P-1", "10:00", "11:00", modem=1),
        view("P-2", "10:00", "11:00", modem=2),
        view("P-3", "10:00", "11:00", earliest="09:00", modem=3),
    ]
    p = propose_slots("P-1", plans, harbor=HARBOR, now=NOW)
    assert p.optimal and p.total_delay_min == 60
    assert {s.plan_id: s.shift_min for s in p.shifts} == {"P-1": 0, "P-2": 0, "P-3": -60}


def test_model_set_scope() -> None:
    plans = [
        *six_plans(),
        view("P-OTHER-ZONE", "09:00", "11:00", area="pier_400_approach", modem=8),
        view("P-OTHER-DAY", "09:00", "11:00", modem=9).model_copy(
            update={"start_at": at("09:00") + timedelta(days=1)}
            | {"end_at": at("11:00") + timedelta(days=1)}
        ),
    ]
    from soundings.registry.models import load_conflict_config

    target = next(p for p in plans if p.plan_id == "P-B")
    members = model_set(target, plans, HARBOR, load_conflict_config())
    ids = [m.plan_id for m in members]
    assert ids == ["P-F", "P-A", "P-B", "P-C", "P-D", "P-E"]


def test_solver_parameters_single_worker_and_seed() -> None:
    solver = make_solver(CFG)
    params = solver.parameters
    assert params.num_search_workers == 1 or params.num_workers == 1
    assert params.random_seed == CFG.random_seed == 7
    assert params.max_time_in_seconds == CFG.max_time_in_seconds == 2.0
    assert params.max_deterministic_time == 2.0
    assert params.log_search_progress is False


def test_capacity_from_config_never_literal() -> None:
    raw = yaml.safe_load(REGISTRY_CONFIG_PATH.read_text(encoding="utf-8"))["slots"]
    assert CFG.zone_capacity_default == raw["zone_capacity_default"]
    assert CFG.capacity_for(ZONE) == raw.get("zone_capacity", {}).get(
        ZONE, raw["zone_capacity_default"]
    )
    p = propose_slots("P-B", six_plans(), harbor=HARBOR, now=NOW)
    assert p.capacity == CFG.zone_capacity_default
    bumped = CFG.model_copy(update={"zone_capacity_default": 5})
    assert propose_slots("P-B", six_plans(), harbor=HARBOR, now=NOW, cfg=bumped).capacity == 5
    per_zone = _cfg(**{ZONE: 4})  # 10:00-11:00 holds four plans; four fit as filed
    p4 = propose_slots("P-B", six_plans(), harbor=HARBOR, now=NOW, cfg=per_zone)
    assert p4.capacity == 4 and p4.total_delay_min == 0


def test_within_capacity_pair_zero_delay() -> None:
    plans = [view("P-1", "10:00", "12:00", modem=1), view("P-2", "11:00", "13:00", modem=2)]
    p = propose_slots("P-1", plans, harbor=HARBOR, now=NOW)
    assert p.status == "feasible" and p.optimal
    assert p.total_delay_min == 0
    assert p.detail == WITHIN_CAPACITY_DETAIL
    assert all(s.shift_min == 0 for s in p.shifts)


def test_not_conflicted_plans_never_move() -> None:
    plans = [
        view("P-1", "10:00", "12:00", modem=1),
        view("P-2", "10:00", "12:00", modem=2),
        view("P-3", "10:00", "12:00", modem=3, conflicted=False),
    ]
    p = propose_slots("P-1", plans, harbor=HARBOR, now=NOW)
    shifts = {s.plan_id: s for s in p.shifts}
    assert shifts["P-3"].shift_min == 0 and shifts["P-3"].pinned is PinReason.NOT_CONFLICTED
    assert p.total_delay_min == 120  # P-2 (later in sorted order) waits for P-1


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    derandomize=True,
)
@given(minutes=st.integers(min_value=0, max_value=6 * 60))
def test_in_water_plan_never_shifted(minutes: int) -> None:
    now = at("07:00") + timedelta(minutes=minutes)
    p = propose_slots("P-B", six_plans(), harbor=HARBOR, now=now, cfg=_cfg(**{ZONE: 2}))
    started = {pid for pid, (start, *_rest) in SIX.items() if at(start) <= now}
    for shift in p.shifts:
        if shift.plan_id in started:
            assert shift.shift_min == 0
            assert shift.pinned is PinReason.IN_WATER
        else:
            assert shift.new_start_at >= now


def test_start_bounds_invalid_rejects() -> None:
    store = RegistryStore(":memory:", harbor=HARBOR, now=lambda: NOW)
    base = {
        "operator": "Harbor Survey Co (demo)",
        "vehicle": "ROV-1",
        "modem_id": "MOD-4100",
        "area": ZONE,
        "start_at": at("10:00"),
        "end_at": at("11:00"),
        "contact_channel": "radio ch 16",
    }
    try:
        for bad in (
            {"earliest_start_at": at("10:30"), "latest_start_at": at("10:15")},
            {"earliest_start_at": at("10:15")},
            {"latest_start_at": at("09:45")},
        ):
            with pytest.raises(PlanRejected) as info:
                store.create({**base, **bad})
            assert info.value.reason is RejectReason.START_BOUNDS_INVALID
        assert store.count() == 0
        ok = store.create(
            {**base, "earliest_start_at": at("09:00"), "latest_start_at": at("12:00")}
        )
        assert ok.earliest_start_at == at("09:00") and ok.latest_start_at == at("12:00")
    finally:
        store.close()


def test_slot_config_validation() -> None:
    assert CFG.horizon_minutes == 24 * 60
    with pytest.raises(ValueError):
        SlotConfig(horizon_end_local="25:00")
    with pytest.raises(ValueError):
        SlotConfig(zone_capacity_default=0)


# --- rule proofs -----------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_proposal_is_not_control() -> None:
    banned = (
        "soundings.registry.store",
        "soundings.llm",
        "anthropic",
        "soundings.challenge",
        "soundings.ladder",
    )
    for name in _imports(SLOTS_PY):
        assert not any(name == b or name.startswith(b + ".") for b in banned), name
    callers: list[tuple[str, str]] = []
    for path in sorted((ROOT / "soundings").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "accept_proposal"
                ):
                    callers.append((path.relative_to(ROOT).as_posix(), fn.name))
    assert callers == [("soundings/registry/router.py", "accept_slot")]


def test_no_clock() -> None:
    tree = ast.parse(SLOTS_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("now", "utcnow", "today") or not isinstance(
                node.value, ast.Name
            ), ast.unparse(node)
            if isinstance(node.value, ast.Name) and node.value.id == "time":
                pytest.fail(f"time.{node.attr} in slots.py")
    assert not any(n == "random" or n.startswith("random.") for n in _imports(SLOTS_PY))
    assert "time" not in _imports(SLOTS_PY)
    source = SLOTS_PY.read_text(encoding="utf-8")
    assert "datetime.now" not in source and "time.time" not in source


def test_ortools_imported_lazily() -> None:
    code = "import sys, soundings.api, soundings.registry; print('ortools' in sys.modules)"
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert done.stdout.strip() == "False"
