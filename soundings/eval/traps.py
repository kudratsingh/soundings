"""Known-answer trap scenarios and the hand-off threshold sweep (WO-051 part B1).

A trap is a scenario under ``scenarios/traps/`` whose right answer (hand to a person or
not) and expected ``threat_mass`` band per checkpoint are written in
``scenarios/traps/traps.yaml`` before anything runs. ``run_traps`` scores each trap's
observed prefix at every checkpoint (sim -> evidence table -> the ask-rung hail ->
posterior) and reports where ``threat_mass`` landed against the stated band.

The sweep (WO-051 addendum 2c) replays the same readings against every
``stopping.threat_mass_handoff`` value from 0.25 to 0.45 in 0.05 steps. It is a
scorer-level reading: a trap "would hand off" at a threshold when any checkpoint's
``threat_mass`` reaches it. The machine's other stop rules (benign mass, imminence,
budget) are not applied here; the sweep never edits ``config/ladder.yaml``.

Ground truth is read only to find which observed contacts belong to the trap's scenario
contacts; the scorer itself sees the evidence table alone. This module lives in
``soundings/eval`` because that is the only package allowed to read the answer key.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import Field, model_validator

from soundings.canonical import canonical_json, round6
from soundings.challenge.hail import HailSimulator, hail_evidence_rows
from soundings.investigation.features import build_evidence
from soundings.investigation.pol import load_pol_table
from soundings.investigation.posterior import (
    LikelihoodTables,
    PriorConfig,
    load_likelihood_tables,
    load_prior_config,
    score_contact,
)
from soundings.schemas import Hypothesis, Scenario, load_ladder_config
from soundings.schemas._base import CONFIG_DIR, REPO_ROOT, NonNegInt, StrictModel
from soundings.schemas.denylist import FreeText
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.sim.current import load_current_field, tide_phase_for_hour
from soundings.sim.run import run_scenario, truncate_run
from soundings.sim.scenario import resolve_scenario
from soundings.sim.types import SimRun

TRAPS_ROOT: Path = REPO_ROOT / "scenarios" / "traps"
TRAP_MANIFEST: Path = TRAPS_ROOT / "traps.yaml"
FALSE_ALARM_PREFIX = "fa-"

SWEEP_THRESHOLDS: tuple[float, ...] = (0.25, 0.30, 0.35, 0.40, 0.45)
"""``stopping.threat_mass_handoff`` values the addendum (2c) sweeps."""

TRAP_LABEL = "stress-test estimate"
TRAP_TITLE = "# Soundings trap calibration"
SWEEP_METHOD = (
    "Method: each trap's observed prefix is scored at every checkpoint after the ask-rung "
    "hail; a trap would hand off at a threshold when any checkpoint's threat_mass reaches "
    "it. Scorer-level reading: the machine's benign, imminence and budget stop rules are "
    "not applied, and config/ladder.yaml is never edited."
)

BAND_COLUMNS: tuple[str, ...] = (
    "trap",
    "seed",
    "should_hand_off",
    "tick",
    "band",
    "threat_mass",
    "top_hypothesis",
    "in_band",
)
SWEEP_COLUMNS: tuple[str, ...] = (
    "threshold",
    "runs",
    "false_shutdowns",
    "missed_threats",
    "configured",
)
TRAP_COLUMNS: tuple[str, ...] = ("trap", "should_hand_off", "why")

UnitFloat = Annotated[float, Field(ge=0.0, le=1.0)]


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class Band(StrictModel):
    low: UnitFloat
    high: UnitFloat

    @model_validator(mode="after")
    def _ordered(self) -> Band:
        if self.low > self.high:
            raise ValueError(f"band low {self.low} is above high {self.high}")
        return self

    def holds(self, value: float) -> bool:
        return self.low <= value <= self.high

    def text(self) -> str:
        return f"{self.low:.2f}..{self.high:.2f}"


class Checkpoint(StrictModel):
    tick: Annotated[int, Field(gt=0)]
    band: Band


class TrapSpec(StrictModel):
    id: str
    file: str
    should_hand_off: bool
    why: FreeText
    checkpoints: Annotated[tuple[Checkpoint, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _increasing(self) -> TrapSpec:
        ticks = [c.tick for c in self.checkpoints]
        if ticks != sorted(set(ticks)):
            raise ValueError(f"trap {self.id}: checkpoint ticks must be strictly increasing")
        return self


class TrapManifest(StrictModel):
    traps: Annotated[tuple[TrapSpec, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> TrapManifest:
        ids = [t.id for t in self.traps]
        if len(set(ids)) != len(ids):
            raise ValueError("trap ids must be unique")
        return self

    def by_id(self, trap_id: str) -> TrapSpec:
        for t in self.traps:
            if t.id == trap_id:
                return t
        raise KeyError(trap_id)


def _band_from_pair(raw: object) -> object:
    if isinstance(raw, list) and len(raw) == 2:
        return {"low": float(raw[0]), "high": float(raw[1])}
    return raw


def load_trap_manifest(path: str | Path = TRAP_MANIFEST) -> TrapManifest:
    """Parse ``traps.yaml``; a band written as ``[low, high]`` becomes a ``Band``."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    traps = []
    for entry in doc["traps"]:
        item = dict(entry)
        item["checkpoints"] = tuple(
            {"tick": int(cp["tick"]), "band": _band_from_pair(cp["band"])}
            for cp in item.get("checkpoints", ())
        )
        traps.append(item)
    return TrapManifest.model_validate({"traps": tuple(traps)})


def trap_paths(manifest_path: str | Path = TRAP_MANIFEST) -> list[Path]:
    """Scenario files the manifest names, sorted; a missing file raises."""
    root = Path(manifest_path).parent
    paths = []
    for spec in load_trap_manifest(manifest_path).traps:
        path = root / spec.file
        if not path.is_file():
            raise FileNotFoundError(f"trap {spec.id}: {spec.file} not found")
        paths.append(path)
    return sorted(paths)


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


class CheckpointReading(StrictModel):
    tick: NonNegInt
    band: Band
    threat_mass: float | None
    """Highest threat_mass over the trap's observed contacts at this tick; None when
    none of them had been detected yet."""
    top_hypothesis: Hypothesis | None
    in_band: bool | None
    observed_contacts: NonNegInt


class TrapRun(StrictModel):
    trap_id: str
    seed: int
    should_hand_off: bool
    readings: tuple[CheckpointReading, ...]
    peak_threat_mass: float | None
    peak_tick: NonNegInt | None

    def would_hand_off(self, threshold: float) -> bool:
        return self.peak_threat_mass is not None and self.peak_threat_mass >= threshold


class SweepRow(StrictModel):
    threshold: float
    runs: NonNegInt
    false_shutdowns: NonNegInt
    missed_threats: NonNegInt
    configured: bool


class TrapSummary(StrictModel):
    trap_id: str
    should_hand_off: bool
    why: str


class TrapReport(StrictModel):
    suite: str
    seeds: tuple[int, ...]
    mode: str
    label: str
    inputs_sha256: str
    configured_threshold: float
    traps: tuple[TrapSummary, ...]
    runs: tuple[TrapRun, ...]
    sweep: tuple[SweepRow, ...]
    in_band: NonNegInt
    out_of_band: NonNegInt
    undetected: NonNegInt


class _Scorer:
    """Shared, read-only inputs to every checkpoint score (loaded once per campaign)."""

    def __init__(self) -> None:
        self.harbor: Harbor = load_harbor()
        self.field = load_current_field()
        self.pol = load_pol_table()
        self.tables: LikelihoodTables = load_likelihood_tables()
        self.priors: PriorConfig = load_prior_config()

    def tide(self, hour: int) -> object:
        return tide_phase_for_hour(hour, self.field)


@cache
def _scorer() -> _Scorer:
    return _Scorer()


def _trap_contact_ids(run: SimRun, scenario: Scenario) -> list[str]:
    """Observed ids that belong to the trap's own scenario contacts (eval-side only)."""
    gt_ids = {c.id for c in scenario.contacts}
    return sorted(
        cid
        for cid, truth in run.contact_truth.items()
        if truth.gt_id in gt_ids
        and not truth.gt_id.startswith(FALSE_ALARM_PREFIX)
        and cid in run.tracks
        and run.tracks[cid].points
    )


def _read_checkpoint(
    scenario: Scenario, full: SimRun, cp: Checkpoint, seed: int, scorer: _Scorer
) -> CheckpointReading:
    prefix = truncate_run(full, min(cp.tick, full.n_ticks))
    ids = _trap_contact_ids(prefix, scenario)
    if not ids:
        return CheckpointReading(
            tick=cp.tick,
            band=cp.band,
            threat_mass=None,
            top_hypothesis=None,
            in_band=None,
            observed_contacts=0,
        )
    table = build_evidence(
        prefix,
        scorer.harbor,
        scorer.field,
        scorer.pol,
        hour_of_day=scenario.hour_of_day,
        tide_phase_for_hour=scorer.tide,
    )
    hail = HailSimulator(scenario, full, seed=seed)
    for cid in ids:
        sent = hail.send(cid, prefix.tracks[cid].points[-1].tick)
        table.extend(hail_evidence_rows(sent, tick_s=prefix.tick_s))
    # ids are sorted, so a tie keeps the lowest observed id.
    mass, top = -1.0, Hypothesis.DEBRIS_OR_DRIFT
    for cid in ids:
        posterior = score_contact(table, cid, scorer.tables, scorer.priors).posterior
        here = round6(posterior.threat_mass())
        if here > mass:
            mass, top = here, posterior.top()
    return CheckpointReading(
        tick=cp.tick,
        band=cp.band,
        threat_mass=mass,
        top_hypothesis=top,
        in_band=cp.band.holds(mass),
        observed_contacts=len(ids),
    )


def score_trap(spec: TrapSpec, scenario: Scenario, seed: int) -> TrapRun:
    """Score one trap at one seed at every checkpoint of its manifest entry."""
    scorer = _scorer()
    last = max(cp.tick for cp in spec.checkpoints)
    full = run_scenario(
        scenario, seed=seed, ticks=min(last, scenario.duration_ticks), harbor=scorer.harbor
    )
    readings = tuple(_read_checkpoint(scenario, full, cp, seed, scorer) for cp in spec.checkpoints)
    peak: CheckpointReading | None = None
    for r in readings:
        if r.threat_mass is not None and (peak is None or r.threat_mass > (peak.threat_mass or 0)):
            peak = r
    return TrapRun(
        trap_id=spec.id,
        seed=seed,
        should_hand_off=spec.should_hand_off,
        readings=readings,
        peak_threat_mass=None if peak is None else peak.threat_mass,
        peak_tick=None if peak is None else peak.tick,
    )


def threshold_sweep(
    runs: Sequence[TrapRun],
    *,
    thresholds: Sequence[float] = SWEEP_THRESHOLDS,
    configured: float,
) -> tuple[SweepRow, ...]:
    """False shutdowns (benign trap reaches the threshold) and missed threats (threat
    trap never reaches it) per threshold, over every (trap, seed) run."""
    rows = []
    for t in sorted(thresholds):
        rows.append(
            SweepRow(
                threshold=round6(t),
                runs=len(runs),
                false_shutdowns=sum(
                    1 for r in runs if not r.should_hand_off and r.would_hand_off(t)
                ),
                missed_threats=sum(
                    1 for r in runs if r.should_hand_off and not r.would_hand_off(t)
                ),
                configured=abs(t - configured) < 1e-9,
            )
        )
    return tuple(rows)


def traps_inputs_sha256(manifest_path: Path, seeds: Sequence[int], mode: str) -> str:
    """sha256 over ``config/*.yaml``, the trap manifest, every trap file and the args."""
    h = hashlib.sha256()
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        h.update(f"config/{path.name}\n".encode())
        h.update(path.read_bytes())
    h.update(f"traps/{manifest_path.name}\n".encode())
    h.update(manifest_path.read_bytes())
    for path in trap_paths(manifest_path):
        h.update(f"traps/{path.name}\n".encode())
        h.update(path.read_bytes())
    h.update(canonical_json({"suite": "traps", "seeds": list(seeds), "mode": mode}).encode())
    return h.hexdigest()


def run_traps(
    seeds: Sequence[int],
    *,
    manifest_path: str | Path = TRAP_MANIFEST,
    mode: str = "replay",
    configured_threshold: float | None = None,
) -> TrapReport:
    """Every trap x seed, the band readings and the threshold sweep."""
    mpath = Path(manifest_path)
    manifest = load_trap_manifest(mpath)
    seed_list = tuple(sorted(set(seeds)))
    configured = (
        configured_threshold
        if configured_threshold is not None
        else float(load_ladder_config().stopping["threat_mass_handoff"])
    )
    harbor = _scorer().harbor
    runs: list[TrapRun] = []
    for spec in sorted(manifest.traps, key=lambda s: s.id):
        scenario = resolve_scenario(mpath.parent / spec.file, harbor)
        if scenario.id != spec.id:
            raise ValueError(f"trap {spec.id}: {spec.file} declares id {scenario.id}")
        runs.extend(score_trap(spec, scenario, seed) for seed in seed_list)
    readings = [r for run in runs for r in run.readings]
    return TrapReport(
        suite="traps",
        seeds=seed_list,
        mode=mode,
        label=TRAP_LABEL,
        inputs_sha256=traps_inputs_sha256(mpath, seed_list, mode),
        configured_threshold=round6(configured),
        traps=tuple(
            TrapSummary(trap_id=s.id, should_hand_off=s.should_hand_off, why=s.why)
            for s in sorted(manifest.traps, key=lambda s: s.id)
        ),
        runs=tuple(runs),
        sweep=threshold_sweep(runs, configured=configured),
        in_band=sum(1 for r in readings if r.in_band is True),
        out_of_band=sum(1 for r in readings if r.in_band is False),
        undetected=sum(1 for r in readings if r.in_band is None),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _cell(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, Hypothesis):
        return value.value
    return str(value)


def _table(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    return out


def render_trap_markdown(report: TrapReport) -> str:
    seeds = ", ".join(str(s) for s in report.seeds)
    out = [TRAP_TITLE, ""]
    out.append(f"Label: {report.label}. Simulated harbor, hand-set answers; not operational.")
    out.append("")
    out.append(f"- suite: {report.suite}")
    out.append(f"- seeds: {seeds}")
    out.append(f"- mode: {report.mode}")
    out.append(f"- inputs_sha256: {report.inputs_sha256}")
    out.append(f"- configured stopping.threat_mass_handoff: {report.configured_threshold:.2f}")
    out.append(
        f"- checkpoints in band: {report.in_band}, out of band: {report.out_of_band}, "
        f"trap contact not yet detected: {report.undetected}"
    )
    out.append("")
    out.append("## Traps")
    out.append("")
    out.append(
        "Each right answer and band was written before any run (scenarios/traps/traps.yaml)."
    )
    out.append("")
    out.extend(
        _table(
            TRAP_COLUMNS,
            [[t.trap_id, _cell(t.should_hand_off), " ".join(t.why.split())] for t in report.traps],
        )
    )
    out.append("")
    out.append("## Threat mass against the band")
    out.append("")
    out.append(
        f"Seeds: {seeds}. Method: threat_mass is the highest over the trap's observed "
        "contacts at that tick, scored after the ask-rung hail; in_band n/a means the "
        "contact had not been detected yet."
    )
    out.append("")
    rows = [
        [
            run.trap_id,
            str(run.seed),
            _cell(run.should_hand_off),
            str(r.tick),
            r.band.text(),
            _cell(r.threat_mass),
            _cell(r.top_hypothesis),
            _cell(r.in_band),
        ]
        for run in report.runs
        for r in run.readings
    ]
    out.extend(_table(BAND_COLUMNS, rows))
    out.append("")
    out.append("## Hand-off threshold sweep")
    out.append("")
    out.append(f"Seeds: {seeds}. {SWEEP_METHOD}")
    out.append("")
    out.extend(
        _table(
            SWEEP_COLUMNS,
            [
                [
                    f"{row.threshold:.2f}",
                    str(row.runs),
                    str(row.false_shutdowns),
                    str(row.missed_threats),
                    _cell(row.configured),
                ]
                for row in report.sweep
            ],
        )
    )
    out.append("")
    return "\n".join(out)
