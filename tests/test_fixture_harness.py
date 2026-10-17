"""WO-093: the fixture recording harness (plan / record / verify). No network anywhere.

Every ``record`` here runs through an injected stub transport; the vendor transport is
never passed. Fixtures, PAID-RUNS and VIOLATIONS files live in ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from soundings.llm import fixtures as fx
from soundings.llm.client import FixtureStore, ModelClient, ModelMode, fixture_key
from soundings.llm.guard import VIOLATIONS_HEADER

REPO = Path(__file__).resolve().parents[1]
PAIR = ["S04", "S05"]
PAID_HEADER = (
    "# Paid runs\n\n| when | role | calls | est. cost | model | description | approver |\n"
    "|---|---|---|---|---|---|---|\n"
)
RUNG_ORDER = ["watch", "ask", "slow", "hold", "hand_to_person"]


class StubTransport:
    """Counts calls; replies with ``reply(role, inputs)`` (default: a reply every guard
    rejects, so the run follows exactly the planned, deterministic path)."""

    def __init__(self, reply: Any = None, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.reply = reply
        self.fail = fail

    def __call__(self, *, model: str, role: str, system: str | None, inputs: Any) -> Any:
        self.calls.append((role, system))
        if self.fail:
            raise RuntimeError("stub transport down")
        if self.reply is None:
            return {"stub": True}
        return self.reply(role, inputs)


def _accepting(role: str, inputs: Any, *, eyes_on: bool = False) -> dict[str, Any]:
    """Valid-looking replies: the probe planner names the last permitted probe (only
    zero-interruption probes unless ``eyes_on``)."""
    if role == "probe_planner":
        rung = (
            RUNG_ORDER.index(inputs["current_rung"]) if inputs["current_rung"] in RUNG_ORDER else 1
        )
        left = inputs["remaining_budget"]
        ran = set(inputs["already_run"])
        options = [
            a["probe_id"]
            for a in inputs["allowlist"]
            if a["probe_id"] not in ran
            and a["rung_required"] in RUNG_ORDER
            and RUNG_ORDER.index(a["rung_required"]) <= rung
            and (a["cost_usd"] == 0 or left["paid_probes"] > 0)
            and (a["interruption"] == 0 or (eyes_on and left["interruptions"] > 0))
        ]
        if not options or left["probes"] <= 0:
            return {"stub": True}
        probs = inputs["posterior"]["posterior"]
        top = sorted(probs, key=lambda h: -probs[h])[:2]
        return {
            "next_probe": options[-1],
            "why": "Separates the two leading hypotheses.",
            "hypotheses_to_discriminate": top,
            "question_for_human": None,
        }
    if role == "hypothesis_writer":
        return {
            "hypotheses": [
                {"id": h, "why": "Fits the evidence rows."} for h in inputs["hypotheses"][:3]
            ],
            "question_for_human": "Is any work filed near this position?",
        }
    return {"stub": True}


@pytest.fixture
def paid_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Everything ``record`` needs, with the stub's prices; SOUNDINGS_CI unset."""
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    monkeypatch.setenv("MODEL_MODE", "record")
    monkeypatch.setenv("MODEL_NAME", "stub-model")
    monkeypatch.setenv(fx.PRICE_IN_ENV, "3")
    monkeypatch.setenv(fx.PRICE_OUT_ENV, "15")
    monkeypatch.setenv(fx.APPROVAL_ENV, "approve paid run " + fx.describe(PAIR))
    paid = tmp_path / "PAID-RUNS.md"
    paid.write_text(PAID_HEADER, encoding="utf-8")
    return {
        "root": tmp_path / "fixtures",
        "paid": paid,
        "violations": tmp_path / "VIOLATIONS.md",
    }


def _record(env: dict[str, Path], transport: StubTransport, **kw: Any) -> fx.RecordSummary:
    return fx.record(
        transport=transport,
        root=env["root"],
        paid_runs_path=env["paid"],
        violations_path=env["violations"],
        scenarios=kw.pop("scenarios", PAIR),
        now=lambda: "2026-10-17T12:30-07:00",
        **kw,
    )


def _plan(root: Path, monkeypatch: pytest.MonkeyPatch) -> fx.FixturePlan:
    monkeypatch.setenv("MODEL_MODE", "replay")
    plan = fx.build_plan(root=root, scenarios=PAIR)
    monkeypatch.setenv("MODEL_MODE", "record")
    return plan


def _written(root: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for role_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        out[role_dir.name] = sorted(p.stem for p in role_dir.glob("*.json"))
    return out


# --- plan --------------------------------------------------------------------------------


def test_plan_deterministic_s04_s05(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    root = tmp_path / "empty"
    outs = []
    for i in range(2):
        out = tmp_path / f"plan{i}.json"
        assert (
            fx.main(
                ["plan", "--scenarios", "S04,S05", "--fixture-root", str(root), "--out", str(out)]
            )
            == 0
        )
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]
    data = json.loads(outs[0])
    by_role = {r["role"]: r for r in data["roles"]}
    for role in fx.ROLES:
        assert by_role[role]["calls"] > 0, role
        assert by_role[role]["to_record"] == by_role[role]["distinct"] > 0
        assert by_role[role]["avg_in_tokens"] > 0
    assert data["totals"]["to_record"] == sum(r["to_record"] for r in data["roles"])
    assert data["scenarios"] == PAIR
    assert data["bt01"] == "BT-01 pending" or "BT-01" in data["scenarios"]


def test_plan_gate_command_well_formed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    plan = fx.build_plan(root=tmp_path / "empty", scenarios=PAIR)
    argv = shlex.split(fx.gate_command(plan))
    assert argv[:4] == ["uv", "run", "python", "scripts/paid-run-gate.py"]
    opts = dict(zip(argv[4::2], argv[5::2], strict=True))
    assert opts == {
        "--role": "fixture_run",
        "--calls": str(plan.to_record),
        "--in-tokens": str(plan.avg_in_tokens()),
        "--out-tokens": str(plan.avg_out_tokens()),
        "--desc": plan.desc,
    }
    # The gate itself parses the line and asks for exactly our phrase; no approval typed.
    env = {k: v for k, v in os.environ.items() if k != "SOUNDINGS_CI"}
    env.update({fx.PRICE_IN_ENV: "3", fx.PRICE_OUT_ENV: "15", "MODEL_NAME": "stub-model"})
    proc = subprocess.run(
        [sys.executable, str(REPO / argv[3]), *argv[4:]],
        input="",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        check=False,
    )
    assert proc.returncode == 1, proc.stderr
    assert f"Type exactly:  {fx.approval_phrase(plan)}" in proc.stdout
    assert not (tmp_path / "docs").exists()  # not approved: nothing logged


def test_plan_system_prompts_match_roles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``system`` the harness sends per role is the one each role passes itself."""
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    seen: dict[str, set[str | None]] = {}
    original = ModelClient.call

    def spy(self: ModelClient, role: str, inputs: Any, *, system: str | None = None) -> Any:
        seen.setdefault(role, set()).add(system)
        return original(self, role, inputs, system=system)

    monkeypatch.setattr(ModelClient, "call", spy)
    fx.build_plan(root=tmp_path / "empty", scenarios=["S05"])
    assert set(seen) == set(fx.ROLES)
    for role, systems in seen.items():
        assert systems == {fx.SYSTEM_PROMPTS[role]}


def test_plan_counts_recorded_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    root = tmp_path / "fx"
    plan = fx.build_plan(root=root, scenarios=["S04"])
    keys = plan.keys_to_record()
    role = "hypothesis_writer"
    store = FixtureStore(root)
    path = store.path(role, keys[role][0])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"output": {"stub": True}}), encoding="utf-8")
    again = fx.build_plan(root=root, scenarios=["S04"])
    row = {r.role: r for r in again.roles}[role]
    assert row.recorded == 1 and row.to_record == len(keys[role]) - 1


def test_run_set_bt01_pending_and_present(tmp_path: Path) -> None:
    assert [c.scenario for c in fx.run_configs(root=tmp_path)] == [
        "S02",
        "S04",
        "S05",
        "S06",
        "S07",
        "S08",
    ]
    assert fx.describe(["S04", "S05"]) == "record fixtures S04-S08 handoffs"
    (tmp_path / "BT-01.yaml").write_text("id: BT-01\n", encoding="utf-8")
    cfgs = fx.run_configs(root=tmp_path)
    bt = cfgs[-1]
    assert bt.scenario == "BT-01" and bt.llm_roles and bt.sitrep_auto
    assert fx.describe([c.scenario for c in cfgs]) == "record fixtures S04-S08 BT-01 handoffs"
    roles = {c.scenario: c.roles for c in cfgs}
    assert roles["S02"] == ("sitrep_writer",)
    assert roles["S04"] == ("probe_planner", "hypothesis_writer")
    with pytest.raises(ValueError, match="S01"):
        fx.run_configs(["S01"], root=tmp_path)


# --- record refusals ---------------------------------------------------------------------


def _assert_nothing_happened(env: dict[str, Path], stub: StubTransport) -> None:
    assert stub.calls == []
    assert not env["root"].exists() or not any(env["root"].rglob("*.json"))
    assert env["paid"].read_text(encoding="utf-8") == PAID_HEADER


def test_record_refuses_without_record_mode(
    paid_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_MODE", "replay")
    stub = StubTransport()
    with pytest.raises(fx.RecordRefused, match="MODEL_MODE=record"):
        _record(paid_env, stub)
    _assert_nothing_happened(paid_env, stub)


def test_record_refuses_without_approval_env(
    paid_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = StubTransport()
    monkeypatch.delenv(fx.APPROVAL_ENV)
    with pytest.raises(fx.RecordRefused, match=fx.APPROVAL_ENV):
        _record(paid_env, stub)
    monkeypatch.setenv(fx.APPROVAL_ENV, "approve paid run")  # not the exact phrase
    with pytest.raises(fx.RecordRefused, match=fx.APPROVAL_ENV):
        _record(paid_env, stub)
    _assert_nothing_happened(paid_env, stub)


def test_record_refuses_under_ci(
    paid_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOUNDINGS_CI", "1")
    stub = StubTransport()
    with pytest.raises(fx.RecordRefused, match="CI"):
        _record(paid_env, stub)
    _assert_nothing_happened(paid_env, stub)
    assert (
        fx.main(
            [
                "record",
                "--scenarios",
                "S04,S05",
                "--fixture-root",
                str(paid_env["root"]),
                "--paid-runs",
                str(paid_env["paid"]),
            ]
        )
        == fx.EXIT_REFUSED
    )


def test_record_refuses_over_remaining_budget(
    paid_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with paid_env["paid"].open("a", encoding="utf-8") as fh:
        fh.write(
            "| 2026-10-17T11:00-07:00 | probe_planner | 40 | $14.9990 | m | earlier run"
            " | approved by owner |\n"
        )
    before = paid_env["paid"].read_text(encoding="utf-8")
    stub = StubTransport()
    with pytest.raises(fx.RecordRefused, match="exceeds"):
        _record(paid_env, stub)
    assert stub.calls == []
    assert paid_env["paid"].read_text(encoding="utf-8") == before


def test_record_refuses_without_model_name_or_prices(
    paid_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = StubTransport()
    monkeypatch.delenv(fx.PRICE_OUT_ENV)
    with pytest.raises(fx.RecordRefused, match=fx.PRICE_OUT_ENV):
        _record(paid_env, stub)
    monkeypatch.setenv(fx.PRICE_OUT_ENV, "15")
    monkeypatch.delenv("MODEL_NAME")
    with pytest.raises(fx.RecordRefused, match="MODEL_NAME"):
        _record(paid_env, stub)
    _assert_nothing_happened(paid_env, stub)


# --- record with a stub transport --------------------------------------------------------


def test_stub_record_writes_planned_keys_and_is_idempotent(
    paid_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(paid_env["root"], monkeypatch)
    stub = StubTransport()
    summary = _record(paid_env, stub)
    assert summary.ok, summary.stopped
    assert len(stub.calls) == summary.calls == plan.to_record
    assert {role for role, _ in stub.calls} == set(fx.ROLES)
    assert all(system == fx.SYSTEM_PROMPTS[role] for role, system in stub.calls)
    written = _written(paid_env["root"])
    assert written == {r: sorted(k) for r, k in plan.keys_to_record().items()}
    rows = fx.parse_paid_runs(paid_env["paid"].read_text(encoding="utf-8"))
    assert len(rows) == 1
    row = rows[0]
    assert (row.when, row.role, row.calls, row.model) == (
        "2026-10-17T12:30-07:00",
        "fixture_run",
        plan.to_record,
        "stub-model",
    )
    assert row.desc.startswith(plan.desc + ";") and row.from_harness
    assert row.cost_usd > 0
    # every stub reply was rejected by a guard and logged; the manifest lists them
    manifest = fx.read_manifest(paid_env["root"])
    assert manifest["keys"] == written
    assert manifest["rejected"] == written
    assert paid_env["violations"].read_text(encoding="utf-8").startswith(VIOLATIONS_HEADER)

    again = _record(paid_env, stub)
    assert again.calls == 0 and again.ok
    assert len(stub.calls) == plan.to_record  # zero new transport calls
    assert len(fx.parse_paid_runs(paid_env["paid"].read_text(encoding="utf-8"))) == 1


def test_stub_record_stops_at_first_transport_error(paid_env: dict[str, Path]) -> None:
    stub = StubTransport(fail=True)
    summary = _record(paid_env, stub)
    assert len(stub.calls) == 1
    assert summary.stopped is not None and "transport error" in summary.stopped
    assert not summary.ok
    rows = fx.parse_paid_runs(paid_env["paid"].read_text(encoding="utf-8"))
    assert [r.calls for r in rows] == [1]  # the billed call is logged
    assert summary.tokens_out == fx.MAX_OUT_TOKENS and rows[0].cost_usd > 0
    assert "stopped" in rows[0].desc


def test_stub_record_overrun_stops_before_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    stub = StubTransport()
    client = ModelClient(
        mode=ModelMode.RECORD, store=FixtureStore(tmp_path), model_name="stub-model", transport=stub
    )
    store = fx.RecordingStore(client, max_calls=1)
    assert store.load("probe_planner", {"a": 1}) == {"stub": True}
    assert store.load("probe_planner", {"a": 1}) == {"stub": True}  # a hit: no call
    with pytest.raises(fx.RecordingStopped, match="overrun"):
        store.load("probe_planner", {"a": 2})
    assert store.load("probe_planner", {"a": 3}) is None  # stopped: no further calls
    assert len(stub.calls) == 1
    assert fx.max_calls_for(74) == 81 and fx.max_calls_for(3) == 3 and fx.max_calls_for(0) == 0


# --- verify ------------------------------------------------------------------------------


def test_verify_passes_against_stub_recorded_fixtures(
    paid_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(paid_env, StubTransport())
    monkeypatch.setenv("MODEL_MODE", "replay")
    report = fx.verify(root=paid_env["root"], scenarios=PAIR)
    assert report.ok, report.text()
    assert report.misses == () and report.lookups > 0
    assert report.violations > 0 and report.unexpected_violations == ()
    assert (
        fx.main(["verify", "--scenarios", "S04,S05", "--fixture-root", str(paid_env["root"])]) == 0
    )


def test_verify_with_accepted_replies(
    paid_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replies the guards accept change the path; replay follows it with zero misses and
    no catalog key fails that the deterministic run passes."""
    summary = _record(paid_env, StubTransport(_accepting), scenarios=["S05"])
    assert summary.ok, summary.stopped
    monkeypatch.setenv("MODEL_MODE", "replay")
    report = fx.verify(root=paid_env["root"], scenarios=["S05"])
    assert report.misses == ()
    assert report.unexpected_violations == ()
    assert report.llm_choices["S05"] > 0
    assert report.ok, report.text()


def test_verify_flags_an_outcome_regression(
    paid_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded planner that spends eyes-on probes the deterministic planner does not
    pushes S05 over its catalog interruption limit: verify reports it and fails."""

    def eyes_on(role: str, inputs: Any) -> dict[str, Any]:
        return _accepting(role, inputs, eyes_on=True)

    assert _record(paid_env, StubTransport(eyes_on), scenarios=["S05"]).ok
    monkeypatch.setenv("MODEL_MODE", "replay")
    report = fx.verify(root=paid_env["root"], scenarios=["S05"])
    assert report.misses == ()
    assert report.regressions == {"S05": ["max_human_interruptions"]}
    assert "max_human_interruptions" not in report.baseline_failures["S05"]
    assert not report.ok and "REGRESSION S05" in report.text()


def test_verify_fails_with_no_fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    report = fx.verify(root=tmp_path / "empty", scenarios=["S04"])
    assert not report.ok and report.misses
    assert "verify: FAILED" in report.text()


# --- the paid-runs ledger ----------------------------------------------------------------


def test_spent_counts_each_run_once() -> None:
    desc = "record fixtures S04-S08 handoffs"
    text = PAID_HEADER + "".join(
        [
            "| t1 | probe_planner | 40 | $0.5000 | m | old run | approved by owner |\n",
            f"| t2 | fixture_run | 74 | $0.7000 | m | {desc} | approved by owner |\n",
            f"| t3 | fixture_run | 70 | $0.6500 | m | {desc}; 70 fixtures"
            f" | owner; {fx.HARNESS_APPROVER_MARK} |\n",
            f"| t4 | fixture_run | 5 | $0.0500 | m | {desc} | approved by owner |\n",
        ]
    )
    rows = fx.parse_paid_runs(text)
    assert [r.calls for r in rows] == [40, 74, 70, 5]
    # t2 is replaced by t3's actual cost; t4 is this run's own pending approval
    assert fx.spent_usd(rows, desc=desc) == pytest.approx(1.15)
    assert fx.spent_usd(rows, desc="something else") == pytest.approx(1.2)


def test_remaining_budget_reads_the_log(tmp_path: Path) -> None:
    path = tmp_path / "PAID-RUNS.md"
    assert fx.remaining_budget(path, desc="x") == fx.PAID_RUN_CAP_USD == 15.0
    path.write_text(PAID_HEADER + "| t | r | 1 | $2.5000 | m | y | approved by owner |\n")
    assert fx.remaining_budget(path, desc="x") == pytest.approx(12.5)


def test_fixture_key_of_captured_call_matches_client(tmp_path: Path) -> None:
    store = fx.CapturingStore(tmp_path)
    store.scenario = "S00"
    assert store.load("probe_planner", {"b": 1.0000001, "a": [1]}) is None
    (call,) = store.captured
    assert call.key == fixture_key("probe_planner", {"a": [1], "b": 1.0})
    assert not call.hit and call.in_tokens > 0


def test_stub_record_flags_denylisted_fixtures(
    paid_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded reply holding a denylisted word is named in the summary (the denylist
    check scans *.json). The matcher is stubbed so this file names no such word."""
    import soundings.schemas.denylist as dl

    monkeypatch.setattr(dl, "find_denylisted", lambda text: ["x"] if "FLAGME" in text else [])
    summary = _record(
        paid_env, StubTransport(lambda role, inputs: {"stub": "FLAGME"}), scenarios=["S04"]
    )
    assert summary.calls > 0 and len(summary.denylisted) == summary.calls
    assert "denylisted words in" in summary.text()


def test_record_cli_uses_the_record_transport() -> None:
    import inspect

    from soundings.llm.client import RECORD_TIMEOUT_S, anthropic_record_transport

    default = inspect.signature(fx.record).parameters["transport"].default
    assert default is anthropic_record_transport and RECORD_TIMEOUT_S >= 30
