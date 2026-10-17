"""WO-051 eval report tests (part A: the bang and human_queue baselines).

Part B adds the soundings policy over runner v3 and the catalog claims that need it
(>= 70% at or below ask, 0 missed threats for soundings).
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from soundings.canonical import canonical_json
from soundings.eval.harness import run_campaign
from soundings.eval.metrics import LlmDiff, PlannerDifference
from soundings.eval.perturb import TableShift
from soundings.eval.policies import (
    SOUNDINGS_POLICY_MODULE,
    PlannerStats,
    PolicyName,
    load_eval_config,
)
from soundings.eval.report import (
    ESTIMATE_LABEL,
    NO_LLM_EFFECT_SENTENCE,
    PENDING_RUNNER_V3,
    REPORT_COLUMNS,
    SHARED_TABLES_NOTE,
    EvalReport,
    MisspecifiedSection,
    TerminalChange,
    llm_section,
    render_markdown,
    write_report,
)
from soundings.schemas import LadderState, ProbeId, Rung, find_denylisted

REPO = Path(__file__).resolve().parent.parent
BASELINES = (PolicyName.BANG, PolicyName.HUMAN_QUEUE)
SEEDS = (1, 2)
SOUNDINGS_PENDING = importlib.util.find_spec(SOUNDINGS_POLICY_MODULE) is None

# Every metric column BUILD-PLAN names (rung histogram, missed threats, false shutdowns,
# median seen-to-handled, interruptions per hour, shutdown minutes x $/min, probes used).
BUILD_PLAN_COLUMNS = (
    "watch",
    "ask",
    "slow",
    "hold",
    "hand_to_person",
    "missed_threats",
    "false_shutdowns",
    "median_s_seen_to_handled",
    "interruptions_per_hour",
    "shutdown_minutes",
    "shutdown_usd",
    "probes_used",
)


@pytest.fixture(scope="module")
def report() -> EvalReport:
    """Catalog x seeds 1,2, every policy (soundings is pending until part B)."""
    cfg = load_eval_config()
    return run_campaign(
        "catalog",
        SEEDS,
        perturb_magnitude=cfg.eval.perturb_magnitude,
        perturb_seed=cfg.eval.perturb_seed,
    )


@pytest.fixture(scope="module")
def baseline_report() -> EvalReport:
    return run_campaign(
        "catalog",
        SEEDS,
        policies=BASELINES,
        perturb_magnitude=0.5,
        perturb_seed=17,
        include_misspecified=False,
    )


@pytest.fixture(scope="module")
def markdown(report: EvalReport) -> str:
    return render_markdown(report)


def _tables(md: str) -> list[tuple[str, tuple[str, ...]]]:
    """(section heading, header columns) for every Markdown table."""
    out: list[tuple[str, tuple[str, ...]]] = []
    section = ""
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("## "):
            section = line[3:]
        if line.startswith("| ") and i + 1 < len(lines) and lines[i + 1].startswith("|---"):
            out.append((section, tuple(c.strip() for c in line.strip("|").split("|"))))
    return out


def test_baselines_catalog_claims(baseline_report: EvalReport) -> None:
    """S01..S08 x seeds 1,2: bang misses nothing and shuts down the most; human_queue
    interrupts the most."""
    bang = baseline_report.outcomes[PolicyName.BANG]
    hq = baseline_report.outcomes[PolicyName.HUMAN_QUEUE]
    assert bang.runs == hq.runs == 16
    assert bang.gt_contacts == hq.gt_contacts == 18  # 9 per seed (S03 has two divers)
    assert bang.missed_threats == 0
    assert bang.shutdown_minutes > hq.shutdown_minutes
    assert bang.shutdown_minutes == max(
        m.shutdown_minutes for m in baseline_report.outcomes.values()
    )
    assert hq.interruptions_per_hour > bang.interruptions_per_hour
    assert hq.shutdown_minutes == 0.0
    assert bang.shutdown_usd == hq.shutdown_usd == 0.0
    assert baseline_report.scenarios == tuple(f"S0{i}" for i in range(1, 9))
    assert baseline_report.misspecified is None


def test_metric_columns_present(report: EvalReport, markdown: str) -> None:
    doc = json.loads(canonical_json(report))
    for policy in ("bang", "human_queue"):
        for col in BUILD_PLAN_COLUMNS:
            assert col in doc["outcomes"][policy], (policy, col)
    outcome_headers = [cols for sec, cols in _tables(markdown) if sec == "Outcomes by policy"]
    assert outcome_headers and all(c in outcome_headers[0] for c in BUILD_PLAN_COLUMNS)


def _full_report(report: EvalReport) -> EvalReport:
    """The report with an LLM difference and a mis-specified terminal change, so every
    table kind renders."""
    assert report.misspecified is not None
    diff = LlmDiff(
        stats=PlannerStats(calls=1, fixture_hits=1),
        runs_compared=1,
        contacts_compared=1,
        differences=(
            PlannerDifference(
                scenario_id="S04",
                seed=1,
                gt_id="gt-1",
                fields=("probe_sequence",),
                fallback_terminal=LadderState.RESOLVED_BENIGN,
                llm_terminal=LadderState.RESOLVED_BENIGN,
                fallback_max_rung=Rung.ASK,
                llm_max_rung=Rung.ASK,
                fallback_probes=(ProbeId.REGISTRY_LOOKUP,),
                llm_probes=(ProbeId.CAMERA_CUE,),
                fallback_s_seen_to_handled=40.0,
                llm_s_seen_to_handled=40.0,
                verdict="no effect: same probes count, rung and timing",
            ),
        ),
    )
    change = TerminalChange(
        scenario="S04",
        seed=1,
        policy=PolicyName.BANG,
        nominal_terminal=LadderState.HOLDING,
        misspecified_terminal=LadderState.WATCH_AFTER_HANDOFF,
    )
    mis = report.misspecified.model_copy(update={"terminal_changes": (change,)})
    return report.model_copy(update={"llm": llm_section(diff), "misspecified": mis})


def test_columns_stable(report: EvalReport) -> None:
    md = render_markdown(_full_report(report))
    by_section = _tables(md)
    expected = [
        ("Outcomes by policy", REPORT_COLUMNS["outcomes"]),
        ("Sim false alarms by policy", REPORT_COLUMNS["false_alarms"]),
        ("Per scenario", REPORT_COLUMNS["per_scenario"]),
        ("Mis-specified world", REPORT_COLUMNS["misspecified"]),
        ("Mis-specified world", REPORT_COLUMNS["misspecified_changes"]),
        ("Where the LLM layer added nothing", REPORT_COLUMNS["llm_diff"]),
    ]
    assert by_section == expected
    assert REPORT_COLUMNS["outcomes"] == (
        "policy",
        "gt_contacts",
        "at_or_below_ask_pct",
        "watch",
        "ask",
        "slow",
        "hold",
        "hand_to_person",
        "missed_threats",
        "false_shutdowns",
        "median_s_seen_to_handled",
        "unhandled",
        "interruptions_per_hour",
        "shutdown_minutes",
        "shutdown_usd",
        "probes_used",
    )
    assert REPORT_COLUMNS["per_scenario"] == (
        "scenario",
        "policy",
        "expected_terminal",
        "terminal_states",
        "matches_expected",
        "missed",
        "false_shutdown",
        "shutdown_minutes",
        "interruptions",
    )
    # every table row has as many cells as its header
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("|---"):
            width = len(lines[i - 1].strip("|").split("|"))
            j = i + 1
            while j < len(lines) and lines[j].startswith("| "):
                assert len(lines[j].strip("|").split("|")) == width, lines[j]
                j += 1


def test_section_order(markdown: str) -> None:
    headings = [line for line in markdown.split("\n") if line.startswith("## ")]
    assert headings == [
        "## Outcomes by policy",
        "## Sim false alarms by policy",
        "## Per scenario",
        "## Mis-specified world",
        "## Where the LLM layer added nothing",
        "## Assumptions",
    ]


def test_misspecified_section(markdown: str, report: EvalReport) -> None:
    mis = report.misspecified
    assert isinstance(mis, MisspecifiedSection)
    assert mis.magnitude == 0.5 and mis.seed == 17
    assert mis.table_shift.max_abs > 0.0 and mis.table_shift.mean_abs > 0.0
    assert "- perturbation magnitude: 0.500000" in markdown
    assert "- perturbation seed: 17" in markdown
    assert f"- table shift: max {mis.table_shift.max_abs:.6f}" in markdown
    assert {d.policy for d in mis.deltas} == set(report.policies_run)


def test_misspecified_magnitude_zero_changes_nothing() -> None:
    rep = run_campaign(
        "catalog",
        (1,),
        policies=BASELINES,
        perturb_magnitude=0.0,
        perturb_seed=17,
        include_llm_diff=False,
    )
    mis = rep.misspecified
    assert mis is not None
    assert mis.table_shift == TableShift(max_abs=0.0, mean_abs=0.0, cells=mis.table_shift.cells)
    assert mis.deltas and all(d.delta in (0.0, None) for d in mis.deltas)
    assert mis.terminal_changes == ()
    assert mis.nominal == mis.misspecified


def test_llm_section_honest(report: EvalReport, markdown: str) -> None:
    assert NO_LLM_EFFECT_SENTENCE in markdown
    body = markdown.split("## Where the LLM layer added nothing", 1)[1].split("## Assumptions")[0]
    assert "- fixture_hits: 0" in body
    assert "- differing contacts: 0" in body
    if SOUNDINGS_PENDING:
        assert report.llm.diff is None
        assert PENDING_RUNNER_V3 in report.llm.status


def test_soundings_listed_pending(report: EvalReport, markdown: str) -> None:
    if not SOUNDINGS_PENDING:
        pytest.skip("soundings_policy.py exists (part B); soundings runs for real")
    assert report.policies_pending == {PolicyName.SOUNDINGS: PENDING_RUNNER_V3}
    assert report.policies_run == BASELINES
    assert f"- soundings: {PENDING_RUNNER_V3}" in markdown
    assert f"| soundings | {PENDING_RUNNER_V3} |" in markdown


def test_honesty_labels(markdown: str) -> None:
    assert ESTIMATE_LABEL == "stress-test estimate"
    assert ESTIMATE_LABEL in markdown
    flat = " ".join(markdown.split())
    assert " ".join(SHARED_TABLES_NOTE.split()) in flat
    assert "$0: no mentor-validated figure yet" in markdown
    assert "costs.shutdown_usd_per_min_default = 0" in markdown
    cfg = load_eval_config()
    for name, text in cfg.baselines.assumptions.items():
        assert f"- {name}: " in markdown, name
        assert " ".join(text.split()) in flat, name
    assert " ".join(cfg.baselines.assumption_note.split()) in flat
    assert "human_review_s" in markdown and "berth_reopen_after_s" in markdown
    assert "max_human_interruptions" in markdown


def test_no_timestamps_hosts_or_absolute_paths(markdown: str, report: EvalReport) -> None:
    blob = markdown + canonical_json(report)
    assert str(REPO) not in blob
    assert "/Users/" not in blob and "/tmp/" not in blob
    assert re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", blob) is None
    assert re.fullmatch(r"[0-9a-f]{64}", report.inputs_sha256)


def test_report_vocabulary_clean(report: EvalReport, tmp_path: Path) -> None:
    md_path, json_path = write_report(report, tmp_path)
    assert find_denylisted(md_path.read_text(encoding="utf-8")) == []
    assert find_denylisted(json_path.read_text(encoding="utf-8")) == []
    for src in sorted((REPO / "soundings" / "eval").glob("*.py")):
        assert find_denylisted(src.read_text(encoding="utf-8")) == [], src.name


def test_write_report_format(report: EvalReport, tmp_path: Path) -> None:
    md_path, json_path = write_report(report, tmp_path / "nested")
    assert (md_path.name, json_path.name) == ("report.md", "report.json")
    raw_md = md_path.read_bytes()
    assert b"\r" not in raw_md and raw_md.endswith(b"\n")
    assert json_path.read_text(encoding="utf-8") == canonical_json(report) + "\n"
    assert EvalReport.model_validate_json(json_path.read_text(encoding="utf-8")) == report


def test_report_deterministic(tmp_path: Path) -> None:
    """Two CLI processes, both seed forms, give byte-identical report.md and report.json."""
    outs = []
    for i, seeds in enumerate(("1,2", "1..2")):
        out = tmp_path / f"e{i}"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "soundings.eval",
                "--suite",
                "catalog",
                "--seeds",
                seeds,
                "--mode",
                "replay",
                "--out",
                str(out),
            ],
            cwd=REPO,
            check=True,
            capture_output=True,
            env={"MODEL_MODE": "replay", "PATH": "/usr/bin:/bin"},
        )
        outs.append(out)
    for name in ("report.md", "report.json"):
        assert (outs[0] / name).read_bytes() == (outs[1] / name).read_bytes(), name
