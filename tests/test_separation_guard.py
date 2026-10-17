"""WO-083: separation advisories through the standing-orders guard, audited with inputs."""

from __future__ import annotations

import ast
import datetime as dt
import json
import re
import subprocess
import sys
import typing
from pathlib import Path

import pytest

from soundings.advisories.separation import (
    ADVISORY_ACTION,
    LABEL_OUT_OF_SCOPE,
    AdvisoryDecision,
    SeparationAdvisor,
    SeparationAdvisory,
    load_advisory_config,
    separation_check,
)
from soundings.canonical import stable_hash
from soundings.ladder.audit import AuditKind, AuditLog
from soundings.ladder.boundary import load_boundary
from soundings.ladder.guards import (
    UNSIGNED,
    ApproverStatus,
    BoundaryGuards,
    GuardInputs,
    SeparationFacts,
    separation_slow_permitted,
)
from soundings.ladder.standing_orders import StandingScope
from soundings.ladder.types import ActionScope, GuardName
from soundings.schemas import Rung, ToggleName
from tests.support.separation_fixtures import (
    HOUR,
    guards_for,
    job,
    signed_status,
    unsigned_status,
    vessel,
)

REPO = Path(__file__).resolve().parent.parent
CFG = load_advisory_config()
BASIN = "inner_harbor_west"
TICK, TICK_S = 12, 5.0


def head_on(basin: str = BASIN, tick: int = TICK) -> SeparationAdvisory:
    (adv,) = separation_check(
        [vessel("ais-01", tick, (0.0, 0.0), (5.0, 0.0))],
        [job("P-1", tick, (1000.0, 50.0), (-5.0, 0.0), basin=basin)],
        CFG,
    )
    return adv


def decide(
    guards: BoundaryGuards, basin: str = BASIN, audit: AuditLog | None = None
) -> AdvisoryDecision:
    (d,) = SeparationAdvisor(guards, audit, "run-test", CFG).step(
        TICK,
        TICK_S,
        [vessel("ais-01", TICK, (0.0, 0.0), (5.0, 0.0))],
        [job("P-1", TICK, (1000.0, 50.0), (-5.0, 0.0), basin=basin)],
    )
    return d


def test_blocked_unsigned_or_expired(tmp_path: Path) -> None:
    signed = signed_status(tmp_path)
    ok = decide(guards_for(signed))
    assert ok.status == "issued" and ok.decision.guard is GuardName.SLOW_PERMITTED
    assert ok.granted_scope == ActionScope(
        action="advisory_one_basin", basin="inner_harbor_west", max_minutes=30
    )
    assert ok.label == "slow advisory proposed for basin inner_harbor_west"

    unsigned = decide(guards_for(unsigned_status(tmp_path)))
    assert unsigned.status == "blocked" and unsigned.decision.reason == "standing_orders_unsigned"
    assert unsigned.granted_scope is None

    assert signed.orders is not None
    past = signed.orders.valid_until + dt.timedelta(hours=1)
    expired = decide(guards_for(signed, epoch=past - dt.timedelta(seconds=TICK * TICK_S)))
    assert expired.status == "blocked" and expired.decision.reason == UNSIGNED
    # the same epoch with a valid instant still issues: the expiry, not the epoch, blocks
    fresh = decide(guards_for(signed, epoch=signed.orders.signed_at + dt.timedelta(hours=1)))
    assert fresh.status == "issued"

    out = decide(guards_for(signed), basin="main_channel")
    assert out.status == "blocked" and out.decision.reason == "basin_out_of_standing_scope"
    assert out.label == LABEL_OUT_OF_SCOPE == "advisory blocked: out of standing-orders scope"


def test_audited_with_inputs(tmp_path: Path) -> None:
    audit = AuditLog(":memory:")
    issued = decide(guards_for(signed_status(tmp_path)), audit=audit)
    blocked = decide(guards_for(signed_status(tmp_path)), basin="main_channel", audit=audit)
    guard_rows = audit.rows(kind=AuditKind.GUARD_DECISION)
    action_rows = audit.rows(kind=AuditKind.ACTION)
    assert len(guard_rows) == 2 and len(action_rows) == 1
    assert [r.seq for r in guard_rows] == [issued.audit_seq, blocked.audit_seq]
    for row, d in zip(guard_rows, (issued, blocked), strict=True):
        p = row.payload
        sep = p["inputs"]["separation"]
        for key in (
            "cpa_m",
            "tcpa_s",
            "own_x_m",
            "own_y_m",
            "own_vx_mps",
            "own_vy_mps",
            "other_x_m",
            "other_y_m",
            "other_vx_mps",
            "other_vy_mps",
            "separation_m",
            "advisory_window_s",
            "velocity_source",
        ):
            assert key in sep, key
        assert sep["cpa_m"] == pytest.approx(50.0, abs=1e-3)
        assert sep["tcpa_s"] == pytest.approx(100.0, abs=1e-3)
        assert sep["separation_m"] == CFG.separation_m
        assert sep["advisory_window_s"] == CFG.advisory_window_s
        assert p["inputs"]["guard_inputs"]["basin"] == d.advisory.basin
        assert p["guard"] == "slow_permitted" and p["reason"] == d.decision.reason
        assert stable_hash(p["inputs"]) == p["inputs_hash"] == d.decision.inputs_hash
        assert row.t_s == TICK * TICK_S and row.contact_id == "c-0001"
    act = action_rows[0].payload
    assert act["action"] == "advisory_one_basin" and act["rung"] == "slow"
    assert act["idempotency_key"] == f"sep:ais-01:P-1:{TICK}"
    assert issued.audit_seq is not None and action_rows[0].seq == issued.audit_seq + 1
    assert audit.verify().ok


def _inputs(scope: StandingScope | None, reasons: tuple[ToggleName, ...] = ()) -> GuardInputs:
    return GuardInputs(
        contact_id="c-0001",
        now_s=60.0,
        standing_orders_signed=scope is not None,
        standing_orders_scope=scope,
        basin=BASIN,
        degraded_reasons=reasons,
    )


def test_slow_permitted_unchanged(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_guards.py",
            "tests/test_guards_integration.py",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert re.search(r"\d+ passed", proc.stdout)

    signed = signed_status(tmp_path)
    assert signed.orders is not None
    crit = load_boundary().criteria
    facts = head_on().inputs
    base_scope = StandingScope.from_orders(signed.orders)
    narrow = base_scope.model_copy(update={"allowed_rungs": (Rung.WATCH, Rung.ASK)})
    for scope in (base_scope, narrow):
        base = separation_slow_permitted(_inputs(scope), crit, facts)
        assert base.permits
        for reason in ToggleName:
            d = separation_slow_permitted(_inputs(scope, (reason,)), crit, facts)
            assert base.permits or not d.permits  # a degraded reason only removes
            if scope is narrow and reason in scope.degraded_when:
                assert not d.permits and d.reason == "slow_not_in_standing_scope"
    # through BoundaryGuards: the real orders keep slow in degraded mode, so the permit holds
    for status in (
        ApproverStatus.from_toggles([t]) for t in ("network_down", "approver_unreachable")
    ):
        d = decide(guards_for(signed, approver=status))
        assert d.status == "issued" and d.decision.permits


def test_criteria_edges(tmp_path: Path) -> None:
    signed = signed_status(tmp_path)
    assert signed.orders is not None
    crit = load_boundary().criteria
    inputs = _inputs(StandingScope.from_orders(signed.orders))
    facts = head_on().inputs
    assert separation_slow_permitted(inputs, crit, facts).reason == "separation_criteria_met"
    far = facts.model_copy(update={"cpa_m": CFG.separation_m})
    assert separation_slow_permitted(inputs, crit, far).reason == "cpa_not_below_separation_m"
    for t in (0.0, -5.0, CFG.advisory_window_s):
        late = facts.model_copy(update={"tcpa_s": t})
        assert (
            separation_slow_permitted(inputs, crit, late).reason == "tcpa_outside_advisory_window"
        )


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_no_model_imports() -> None:
    files = sorted((REPO / "soundings" / "advisories").glob("*.py"))
    files.append(REPO / "soundings" / "ladder" / "guards.py")
    assert len(files) >= 4
    for f in files:
        for mod in _imports(f):
            assert not mod.startswith(("soundings.llm", "anthropic")), (f, mod)
            assert not (mod.startswith(".") and "llm" in mod), (f, mod)
        text = f.read_text(encoding="utf-8")
        assert 'importlib.import_module("soundings.llm' not in text
    # SeparationFacts: numbers, patterned ids and one literal; no free-text field
    hints = typing.get_type_hints(SeparationFacts, include_extras=True)
    for name, field in SeparationFacts.model_fields.items():
        hint = hints[name]
        if field.annotation is str:
            meta = [m for m in field.metadata if getattr(m, "pattern", None)]
            assert meta, f"{name} is free text"
        else:
            assert hint is float or typing.get_origin(hint) in (typing.Literal, typing.Annotated), (
                name
            )


def test_vocabulary(tmp_path: Path) -> None:
    signed = signed_status(tmp_path)
    labels = {
        decide(guards_for(signed)).label,
        decide(guards_for(signed), basin="main_channel").label,
        decide(guards_for(unsigned_status(tmp_path))).label,
    }
    allowed = {
        "advisory",
        "slow",
        "proposes",
        "proposed",
        "for",
        "basin",
        "blocked",
        "out",
        "of",
        "standing-orders",
        "scope",
        "standing",
        "orders",
        "unsigned",
        "or",
        "expired",
        "outside",
        "the",
        "criteria",
        "inner_harbor_west",
    }
    for text in labels:
        assert "advisory" in text
        words = set(re.findall(r"[a-z_-]+", text.lower()))
        assert words <= allowed, words - allowed
    # the only action ever produced is advisory_one_basin, at rung slow
    audit = AuditLog(":memory:")
    decide(guards_for(signed), audit=audit)
    actions = [r.payload for r in audit.rows(kind=AuditKind.ACTION)]
    assert actions and {a["action"] for a in actions} == {ADVISORY_ACTION} == {"advisory_one_basin"}
    assert {a["rung"] for a in actions} == {"slow"}
    # no route, heading or speed instruction leaves this module: nothing names one
    src = (REPO / "soundings" / "advisories" / "separation.py").read_text(encoding="utf-8")
    for word in ("reroute", "divert", "steer", "command", "waypoint", "route_to", "set_speed"):
        assert word not in src.lower(), word
    denylist = [
        ln.strip()
        for ln in (REPO / "config" / "denylist.txt").read_text().splitlines()
        if ln.strip()
    ]
    for path in [
        *sorted((REPO / "soundings" / "advisories").glob("*.py")),
        REPO / "config" / "advisories.yaml",
    ]:
        body = path.read_text(encoding="utf-8")
        for pat in denylist:
            assert not re.search(pat, body, flags=re.IGNORECASE), (path, pat)
    assert json.dumps(sorted(labels))  # labels are plain strings
    assert HOUR == 7
