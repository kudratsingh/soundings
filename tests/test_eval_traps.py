"""WO-051 part B1: known-answer trap scenarios and the hand-off threshold sweep.

The traps' right answers and bands are stated in ``scenarios/traps/traps.yaml`` before
any run. These tests prove the plumbing (files validate, readings land at every
checkpoint, the sweep is consistent and deterministic, ``--suite traps`` archives); they
deliberately do not assert that every reading is in band, because the bands are the
calibration target and an out-of-band reading is a finding to report, not to tune away.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

import soundings.eval.__main__ as cli
from soundings.canonical import canonical_json
from soundings.eval.archive import list_archives, load_manifest
from soundings.eval.traps import (
    BAND_COLUMNS,
    SWEEP_COLUMNS,
    SWEEP_THRESHOLDS,
    TRAP_MANIFEST,
    TRAPS_ROOT,
    Band,
    CheckpointReading,
    TrapReport,
    TrapRun,
    load_trap_manifest,
    render_trap_markdown,
    run_traps,
    threshold_sweep,
    trap_paths,
)
from soundings.schemas import find_denylisted, load_ladder_config
from soundings.sim.scenario import resolve_scenario

runner = CliRunner()


@cache
def _report() -> TrapReport:
    return run_traps((1,))


def test_manifest_lists_four_traps_with_both_answers() -> None:
    manifest = load_trap_manifest()
    assert [t.id for t in manifest.traps] == ["TR-01", "TR-02", "TR-03", "TR-04"]
    answers = {t.id: t.should_hand_off for t in manifest.traps}
    assert answers == {"TR-01": False, "TR-02": True, "TR-03": True, "TR-04": False}
    for spec in manifest.traps:
        assert spec.why and len(spec.checkpoints) >= 2


def test_trap_files_validate_and_ids_match() -> None:
    manifest = load_trap_manifest()
    paths = trap_paths()
    assert [p.parent for p in paths] == [TRAPS_ROOT] * 4
    for spec in manifest.traps:
        sc = resolve_scenario(TRAPS_ROOT / spec.file)
        assert sc.id == spec.id
        assert spec.checkpoints[-1].tick <= sc.duration_ticks
        assert find_denylisted((TRAPS_ROOT / spec.file).read_text(encoding="utf-8")) == []


def test_bad_band_and_manifest_shapes_raise(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Band(low=0.6, high=0.4)
    with pytest.raises(ValidationError):
        Band(low=-0.1, high=0.4)
    doc = yaml.safe_load(TRAP_MANIFEST.read_text(encoding="utf-8"))
    doc["traps"][0]["checkpoints"] = [{"tick": 150, "band": [0, 1]}, {"tick": 60, "band": [0, 1]}]
    bad = tmp_path / "traps.yaml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_trap_manifest(bad)
    doc["traps"][1]["id"] = doc["traps"][0]["id"]
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_trap_manifest(bad)


def test_every_checkpoint_has_a_reading() -> None:
    report = _report()
    manifest = load_trap_manifest()
    assert [(r.trap_id, r.seed) for r in report.runs] == [(t.id, 1) for t in manifest.traps]
    for run in report.runs:
        spec = manifest.by_id(run.trap_id)
        assert [r.tick for r in run.readings] == [c.tick for c in spec.checkpoints]
        for reading in run.readings:
            if reading.threat_mass is None:
                assert reading.in_band is None and reading.observed_contacts == 0
            else:
                assert 0.0 <= reading.threat_mass <= 1.0
                assert reading.in_band == reading.band.holds(reading.threat_mass)
        masses = [r.threat_mass for r in run.readings if r.threat_mass is not None]
        assert run.peak_threat_mass == (max(masses) if masses else None)
    total = sum(len(r.readings) for r in report.runs)
    assert report.in_band + report.out_of_band + report.undetected == total


def test_sweep_covers_the_addendum_range_and_flags_the_configured_value() -> None:
    report = _report()
    assert SWEEP_THRESHOLDS == (0.25, 0.30, 0.35, 0.40, 0.45)
    assert tuple(row.threshold for row in report.sweep) == SWEEP_THRESHOLDS
    configured = float(load_ladder_config().stopping["threat_mass_handoff"])
    assert report.configured_threshold == configured
    assert [row.threshold for row in report.sweep if row.configured] == [configured]
    missed = [row.missed_threats for row in report.sweep]
    false = [row.false_shutdowns for row in report.sweep]
    assert missed == sorted(missed)  # a higher threshold never catches more
    assert false == sorted(false, reverse=True)  # and never shuts down more


def _run(trap: str, should: bool, peak: float | None) -> TrapRun:
    band = Band(low=0.0, high=1.0)
    reading = CheckpointReading(
        tick=60,
        band=band,
        threat_mass=peak,
        top_hypothesis=None,
        in_band=None if peak is None else True,
        observed_contacts=0 if peak is None else 1,
    )
    return TrapRun(
        trap_id=trap,
        seed=1,
        should_hand_off=should,
        readings=(reading,),
        peak_threat_mass=peak,
        peak_tick=None if peak is None else 60,
    )


def test_threshold_sweep_counts_on_hand_built_runs() -> None:
    runs = [
        _run("TR-01", False, 0.30),  # benign that reaches 0.25 and 0.30
        _run("TR-02", True, 0.40),  # threat caught up to 0.40
        _run("TR-03", True, None),  # threat never detected: missed everywhere
        _run("TR-04", False, 0.10),
    ]
    rows = threshold_sweep(runs, configured=0.35)
    got = {row.threshold: (row.false_shutdowns, row.missed_threats) for row in rows}
    assert got == {0.25: (1, 1), 0.30: (1, 1), 0.35: (0, 1), 0.40: (0, 1), 0.45: (0, 2)}
    assert [row.configured for row in rows] == [False, False, True, False, False]


def test_trap_report_deterministic_and_rendered() -> None:
    again = run_traps((1,))
    assert canonical_json(again) == canonical_json(_report())
    md = render_trap_markdown(_report())
    assert "stress-test estimate" in md and "Seeds: 1." in md
    assert "| " + " | ".join(BAND_COLUMNS) + " |" in md
    assert "| " + " | ".join(SWEEP_COLUMNS) + " |" in md
    assert find_denylisted(md) == [] and "/Users/" not in md


def test_cli_suite_traps_archives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_MODE", "replay")
    out = tmp_path / "traps"
    for stamp in ("20261017T150000Z", "20261017T150100Z"):
        result = runner.invoke(
            cli.app,
            [
                "--suite",
                "traps",
                "--seeds",
                "1",
                "--mode",
                "replay",
                "--out",
                str(out),
                "--stamp",
                stamp,
            ],
        )
        assert result.exit_code == 0, result.output
    first, second = list_archives(out)
    assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()
    report = TrapReport.model_validate_json((out / "report.json").read_text(encoding="utf-8"))
    assert report.suite == "traps" and report.seeds == (1,)
    manifest = load_manifest(second)
    assert manifest.mode == "replay" and manifest.suite == "traps"
    assert any(i.path == "scenarios/traps/traps.yaml" for i in manifest.inputs)
    assert sum(1 for i in manifest.inputs if i.path.startswith("scenarios/traps/TR-")) == 4
