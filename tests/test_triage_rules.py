"""WO-085 rule proofs (CLAUDE.md section 2) for the surge triage."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from soundings import investigation_wiring
from soundings.investigation.machine import StopOutcome
from soundings.runner import ScenarioRunnerV2
from soundings.schemas.enums import LadderState
from soundings.triage import TriageEvent, load_triage_config
from tests.support.bt01 import bt01_with_surge, run
from tests.test_triage_runner import surge_run

REPO = Path(__file__).resolve().parents[1]
TRIAGE_SRC = (REPO / "soundings" / "triage.py").read_text(encoding="utf-8")
RUNNER_SRC = (REPO / "soundings" / "runner.py").read_text(encoding="utf-8")


def _wo085_block() -> str:
    m = re.search(r"# --- WO-085 begin: surge triage ---(.*?)# --- WO-085 end", RUNNER_SRC, re.S)
    assert m is not None
    return m.group(1)


def _imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out |= {f"{node.module}.{a.name}" for a in node.names}
    return out


# --- 2.1: the work stops at the handoff; triage never withholds one -----------------------


def test_rule_2_1_handoff_never_withheld_one_slot_no_optional_budget() -> None:
    cfg = load_triage_config().model_copy(
        update={"investigation_slots": 1, "optional_interruptions_per_hour": 0}
    )
    result = run(bt01_with_surge(25), triage=cfg)
    handoffs = [k for k, r in result.recommendations.items() if r.outcome is StopOutcome.HANDOFF]
    assert handoffs
    for case_id in handoffs:
        assert result.case(case_id).state is LadderState.HANDED_OFF, case_id
    granted = sum(1 for d in result.triage_log if d.event is TriageEvent.GRANTED)
    assert granted == result.interruptions
    assert not any(d.kind == "eyes_on" for d in result.triage_log if d.event is TriageEvent.GRANTED)


def test_rule_2_1_triage_writes_no_ladder_state() -> None:
    tree = ast.parse(TRIAGE_SRC)
    names = _imports(tree)
    assert not any("LadderState" in n for n in names)
    assert not any(n.startswith("soundings.ladder") or n == "soundings.runner" for n in names)
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not calls & {"advance", "check_transition", "check_transition_v2", "step"}


# --- 2.2: no model output becomes a control ------------------------------------------------


def test_rule_2_2_no_llm_import() -> None:
    assert not any(n.startswith("soundings.llm") for n in _imports(ast.parse(TRIAGE_SRC)))
    assert "soundings.llm" not in _wo085_block()


def test_rule_2_2_question_text_does_not_move_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_triage_config()
    base = ScenarioRunnerV2("S05", triage=cfg).run()
    monkeypatch.setattr(
        investigation_wiring,
        "template_question",
        lambda report: "A different question for the watch officer, same evidence.",
    )
    other = ScenarioRunnerV2("S05", triage=cfg).run()
    questions = {r.question_for_human for r in other.recommendations.values()}
    assert "A different question for the watch officer, same evidence." in questions
    assert other.triage_log_hash == base.triage_log_hash != ""


# --- 2.4: determinism ----------------------------------------------------------------------


def test_rule_2_4_no_clock_random_or_set_iteration() -> None:
    tree = ast.parse(TRIAGE_SRC)
    names = _imports(tree)
    for banned in ("time", "datetime", "random", "numpy.random", "uuid"):
        assert banned not in names, banned
    assert "datetime.now" not in TRIAGE_SRC and "time.time" not in TRIAGE_SRC
    for node in ast.walk(tree):
        if isinstance(node, ast.For | ast.comprehension):
            it = node.iter
            assert not isinstance(it, ast.Set | ast.SetComp), ast.unparse(it)
            if isinstance(it, ast.Call) and isinstance(it.func, ast.Name):
                assert it.func.id not in {"set", "frozenset"}, ast.unparse(it)


# --- 2.6: every queued decision cites rows ------------------------------------------------


def test_rule_2_6_decisions_cite_rows() -> None:
    result = surge_run()
    seen = 0
    for d in result.triage_log:
        if d.event in (TriageEvent.ENQUEUED, TriageEvent.REKEYED):
            seen += 1
            assert d.evidence_row_ids, d
            for rid in d.evidence_row_ids:
                assert rid in result.evidence, (d, rid)
    assert seen


# --- 2.5: no hype words --------------------------------------------------------------------


def test_rule_2_5_denylist() -> None:
    files = [
        "soundings/triage.py",
        "config/triage.yaml",
        "tests/test_triage.py",
        "tests/test_triage_runner.py",
        "tests/test_triage_rules.py",
        "tests/support/bt01.py",
        "tests/fixtures/triage_bt_synthetic.yaml",
    ]
    proc = subprocess.run(
        ["grep", "-iEf", "config/denylist.txt", *files],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout == "", proc.stdout
