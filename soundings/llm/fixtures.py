"""The fixture recording harness (WO-093): ``plan``, ``record`` (gated) and ``verify``.

``python -m soundings.llm.fixtures plan|record|verify`` prepares and checks the one planned
paid run (CLAUDE.md section 8 A4; WO-051 addendum item 1): ``probe_planner`` and
``hypothesis_writer`` over S04-S08 and the BT-01 rogue, ``sitrep_writer`` over every
handoff scenario (S02 LOST_VEHICLE, S05, S06, S08 HAZARD_NOTICE, BT-01).

How it reaches the model roles without touching the runner: runner v3 builds every role
client over the ``fixture_store`` it is given (``make_kit``) or over ``sitrep_client``.
The harness passes a ``FixtureStore`` subclass, so it sees every ``load(role, inputs)`` a
replay client makes, which is every request the system would send:

* ``plan`` uses a ``CapturingStore``: it records (role, inputs, fixture key, prompt size)
  and answers from the store as replay does. Nothing calls out; a missing fixture is the
  usual replay miss and the caller's deterministic fallback runs.
* ``record`` uses a ``RecordingStore``: a hit is served from disk; a miss is sent through
  a ``ModelClient(mode=record, transport=...)`` (the vendor transport only from the CLI,
  a stub in tests), which writes the fixture and returns it, so each distinct key is
  called once. The first transport error, or the call that would exceed the plan by more
  than ``OVERRUN_FRACTION``, stops the run; nothing is retried or looped.
* ``verify`` replays the same set with a ``CapturingStore`` and checks zero misses, zero
  guard violations beyond the ones the recording logged, and that no scenario fails a
  catalog ``expected`` key the deterministic run passes.

``record`` refuses (exit 3, before any call) under ``SOUNDINGS_CI``, unless
``MODEL_MODE=record``, unless ``SOUNDINGS_PAID_RUN_APPROVED`` equals the phrase the gate
printed, without ``MODEL_NAME`` or the price variables, and when the plan's worst case
(every allowed call at ``max_tokens`` out) exceeds what is left of ``PAID_RUN_CAP_USD``
after the rows in ``docs/log/PAID-RUNS.md``. The cap and the approval variable are
recorded in ``docs/log/DECISIONS.md`` (2026-10-17, WO-093).

Determinism: the plan is computed from seeded scenario runs and written as canonical
JSON with no clock; the only wall-clock read is the PAID-RUNS timestamp of a record run,
in the gate's format.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import logging
import math
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from soundings.canonical import canonical_json, round6
from soundings.llm.client import (
    _LIVE_MAX_TOKENS,
    REPO_ROOT,
    FixtureStore,
    ModelClient,
    ModelMode,
    ModelOutput,
    ReplayMiss,
    Transport,
    anthropic_record_transport,
    fixture_key,
    in_ci,
    model_name_from_env,
)
from soundings.llm.guard import DEFAULT_VIOLATIONS_PATH, VIOLATIONS_HEADER
from soundings.llm.roles import (
    HYPOTHESIS_WRITER_PROMPT,
    PROBE_PLANNER_PROMPT,
    ROLE_HYPOTHESIS_WRITER,
    ROLE_PROBE_PLANNER,
)
from soundings.llm.sitrep_writer import ROLE_SITREP_WRITER, SITREP_WRITER_PROMPT

__all__ = [
    "APPROVAL_ENV",
    "BT01_ID",
    "GATE_ROLE",
    "MAX_OUT_TOKENS",
    "OUT_TOKENS_ESTIMATE",
    "OVERRUN_FRACTION",
    "PAID_RUN_CAP_USD",
    "ROLES",
    "SYSTEM_PROMPTS",
    "CapturedCall",
    "CapturingStore",
    "FixturePlan",
    "PaidRunRow",
    "RecordRefused",
    "RecordSummary",
    "RecordingStopped",
    "RecordingStore",
    "RunConfig",
    "ScenarioSetRun",
    "VerifyReport",
    "approval_phrase",
    "build_plan",
    "gate_command",
    "main",
    "max_calls_for",
    "parse_paid_runs",
    "plan_table",
    "record",
    "remaining_budget",
    "run_configs",
    "run_scenario_set",
    "verify",
]

# --- constants ---------------------------------------------------------------------------

PAID_RUN_CAP_USD: Final = 15.0
"""CLAUDE.md section 8 A4: $15 total for paid runs (DECISIONS 2026-10-17, WO-093)."""

APPROVAL_ENV: Final = "SOUNDINGS_PAID_RUN_APPROVED"
"""Must equal ``approve paid run <desc>``, the phrase ``scripts/paid-run-gate.py`` printed."""

PRICE_IN_ENV: Final = "SOUNDINGS_USD_PER_MTOK_IN"
PRICE_OUT_ENV: Final = "SOUNDINGS_USD_PER_MTOK_OUT"

GATE_ROLE: Final = "fixture_run"
OVERRUN_FRACTION: Final = 0.10
"""``record`` stops before the call that would exceed the plan by more than 10 %."""

MAX_OUT_TOKENS: Final = _LIVE_MAX_TOKENS
"""``max_tokens`` of every vendor request (``soundings.llm.client``): the worst case."""

ROLES: Final = (ROLE_PROBE_PLANNER, ROLE_HYPOTHESIS_WRITER, ROLE_SITREP_WRITER)

SYSTEM_PROMPTS: Final[Mapping[str, str]] = {
    ROLE_PROBE_PLANNER: PROBE_PLANNER_PROMPT,
    ROLE_HYPOTHESIS_WRITER: HYPOTHESIS_WRITER_PROMPT,
    ROLE_SITREP_WRITER: SITREP_WRITER_PROMPT,
}
"""The ``system`` each role passes to ``ModelClient.call`` (tests pin the equality)."""

OUT_TOKENS_ESTIMATE: Final[Mapping[str, int]] = {
    ROLE_PROBE_PLANNER: 150,
    ROLE_HYPOTHESIS_WRITER: 400,
    ROLE_SITREP_WRITER: 800,
}
"""Stated output-token estimates per reply: a four-key probe plan with a 200-character
``why``; up to eight one-line hypotheses and a question; a SITREP's prose sections. The
budget check uses ``MAX_OUT_TOKENS`` instead."""

PLANNER_SCENARIOS: Final = ("S04", "S05", "S06", "S07", "S08")
SITREP_SCENARIOS: Final = ("S02", "S05", "S06", "S08")
BT01_ID: Final = "BT-01"
SCENARIO_ROOT: Final = REPO_ROOT / "scenarios"
PAID_RUNS_PATH: Final = REPO_ROOT / "docs" / "log" / "PAID-RUNS.md"
PLAN_PATH: Final = REPO_ROOT / "artifacts" / "fixtures" / "plan.json"
RUN_MANIFEST: Final = "fixture_run.json"
"""Written at the fixture root by ``record``: the keys recorded and those the guard rejected."""

GATE_APPROVER: Final = "approved by owner"
HARNESS_APPROVER_MARK: Final = "recorded by soundings.llm.fixtures"
_KEY_RE = re.compile(r"\b[0-9a-f]{64}\b")

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_REFUSED: Final = 3


# --- the scenario set --------------------------------------------------------------------


@dataclass(frozen=True)
class RunConfig:
    """One scenario of the set: which roles it exercises."""

    scenario: str
    source: str
    llm_roles: bool
    sitrep_auto: bool

    @property
    def roles(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.llm_roles:
            out += [ROLE_PROBE_PLANNER, ROLE_HYPOTHESIS_WRITER]
        if self.sitrep_auto:
            out.append(ROLE_SITREP_WRITER)
        return tuple(out)


def bt01_path(root: Path = SCENARIO_ROOT) -> Path | None:
    """``scenarios/BT-01.yaml`` when it is on this checkout (WO-080), else ``None``."""
    path = root / f"{BT01_ID}.yaml"
    return path if path.is_file() else None


def run_configs(
    scenarios: Iterable[str] | None = None, *, root: Path = SCENARIO_ROOT
) -> tuple[RunConfig, ...]:
    """The fixture-run set in a fixed order, optionally narrowed to ``scenarios``.

    S02 runs the SITREP writer only; S04 and S07 the planner and hypothesis writer only;
    S05, S06 and S08 all three; BT-01 all three when its file exists.
    """
    ids = sorted(set(PLANNER_SCENARIOS) | set(SITREP_SCENARIOS))
    out = [RunConfig(sid, sid, sid in PLANNER_SCENARIOS, sid in SITREP_SCENARIOS) for sid in ids]
    bt = bt01_path(root)
    if bt is not None:
        out.append(RunConfig(BT01_ID, str(bt), True, True))
    if scenarios is not None:
        wanted = list(scenarios)
        known = {c.scenario for c in out}
        unknown = [s for s in wanted if s not in known]
        if unknown:
            raise ValueError(f"not in the fixture-run set: {', '.join(unknown)}")
        out = [c for c in out if c.scenario in wanted]
    return tuple(out)


# --- stores ------------------------------------------------------------------------------


@dataclass(frozen=True)
class CapturedCall:
    """One request a replay client made through the store."""

    scenario: str
    role: str
    key: str
    prompt_chars: int
    hit: bool

    @property
    def in_tokens(self) -> int:
        return int(math.ceil(self.prompt_chars / 4))


class CapturingStore(FixtureStore):
    """A ``FixtureStore`` that records every ``load`` (each one is a model request)."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(root)
        self.scenario = ""
        self.captured: list[CapturedCall] = []

    def _capture(self, role: str, inputs: Any, hit: bool) -> None:
        system = SYSTEM_PROMPTS.get(role, "")
        self.captured.append(
            CapturedCall(
                scenario=self.scenario,
                role=role,
                key=fixture_key(role, inputs),
                prompt_chars=len(system) + len(canonical_json(inputs)),
                hit=hit,
            )
        )

    def load(self, role: str, inputs: Any) -> ModelOutput | None:
        output = super().load(role, inputs)
        self._capture(role, inputs, output is not None)
        return output


class RecordingStopped(RuntimeError):
    """``record`` stopped: a transport error, or the next call would overrun the plan."""


@dataclass
class _Spend:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    keys: dict[str, list[str]] = field(default_factory=dict)


class RecordingStore(CapturingStore):
    """Serves hits from disk and sends each miss once through a record-mode client.

    ``max_calls`` caps transport calls; the call that would pass it is not made and the
    run stops (``RecordingStopped``). A transport error stops the run too. After a stop
    every further miss returns ``None`` without a call, even if a caller swallowed the
    exception.
    """

    def __init__(self, client: ModelClient, *, max_calls: int) -> None:
        if client.mode is not ModelMode.RECORD:
            raise ValueError("RecordingStore needs a ModelClient in record mode")
        super().__init__(client.store.root)
        self.client = client
        self.max_calls = int(max_calls)
        self.spend = _Spend()
        self.stopped: str | None = None

    def load(self, role: str, inputs: Any) -> ModelOutput | None:
        hit = FixtureStore.load(self, role, inputs)
        self._capture(role, inputs, hit is not None)
        if hit is not None:
            return hit
        if self.stopped is not None:
            return None
        if self.spend.calls >= self.max_calls:
            self.stopped = f"overrun: the next call would exceed {self.max_calls} calls"
            raise RecordingStopped(self.stopped)
        self.spend.calls += 1  # a billed call counts even when it fails
        before = len(self.client.call_log)
        try:
            output = self.client.call(role, inputs, system=SYSTEM_PROMPTS[role])
        except Exception as exc:  # the first transport error stops the run
            # billed but unmeasured: count it at the estimate in and max_tokens out
            self.spend.tokens_in += self.captured[-1].in_tokens
            self.spend.tokens_out += MAX_OUT_TOKENS
            self.stopped = f"transport error: {type(exc).__name__}: {exc}"
            raise RecordingStopped(self.stopped) from exc
        for rec in self.client.call_log[before:]:
            self.spend.tokens_in += rec.tokens_in
            self.spend.tokens_out += rec.tokens_out
        self.spend.keys.setdefault(role, []).append(fixture_key(role, inputs))
        return output


# --- running the set ---------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioOutcome:
    scenario: str
    handling: dict[str, str]
    interruptions: int
    llm_choices: int
    expected_failures: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioSetRun:
    """What one pass over the set produced (stopped: why a record run ended early)."""

    configs: tuple[RunConfig, ...]
    outcomes: tuple[ScenarioOutcome, ...]
    stopped: str | None = None


def _run_one(
    cfg: RunConfig,
    store: FixtureStore | None,
    *,
    violations_path: Path,
    llm: bool = True,
) -> Any:
    from soundings.runner import ScenarioRunnerV3  # lazy: the runner reaches the sim

    sitrep = llm and cfg.sitrep_auto and store is not None
    return ScenarioRunnerV3(cfg.source).run(
        llm_roles=llm and cfg.llm_roles,
        fixture_store=store,
        writer_mode="auto" if sitrep else "template",
        sitrep_client=ModelClient(mode=ModelMode.REPLAY, store=store) if sitrep else None,
        violations_path=violations_path,
    )


def _expected_failures(result: Any, cfg: RunConfig) -> tuple[str, ...]:
    from soundings.eval.catalog_verify import check_expected
    from soundings.runner import ScenarioRunnerV3

    scenario = ScenarioRunnerV3(cfg.source).scenario
    return tuple(sorted({f.key for f in check_expected(result, scenario)}))


def _outcome(cfg: RunConfig, result: Any, *, grade: bool) -> ScenarioOutcome:
    handling = {cid: str(state.value) for cid, state in sorted(result.handling_outcome.items())}
    llm_choices = sum(1 for c in result.planner_choices if c.source == "llm")
    return ScenarioOutcome(
        scenario=cfg.scenario,
        handling=handling,
        interruptions=int(result.interruptions),
        llm_choices=llm_choices,
        expected_failures=_expected_failures(result, cfg) if grade else (),
    )


def run_scenario_set(
    store: CapturingStore,
    *,
    configs: Sequence[RunConfig] | None = None,
    violations_path: Path,
    grade: bool = False,
) -> ScenarioSetRun:
    """Run every config with ``llm_roles`` / SITREP auto over ``store`` (replay clients)."""
    cfgs = tuple(configs) if configs is not None else run_configs()
    outcomes: list[ScenarioOutcome] = []
    for cfg in cfgs:
        store.scenario = cfg.scenario
        try:
            result = _run_one(cfg, store, violations_path=violations_path)
        except RecordingStopped as exc:
            return ScenarioSetRun(cfgs, tuple(outcomes), str(exc))
        outcomes.append(_outcome(cfg, result, grade=grade))
        if isinstance(store, RecordingStore) and store.stopped is not None:
            return ScenarioSetRun(cfgs, tuple(outcomes), store.stopped)
    return ScenarioSetRun(cfgs, tuple(outcomes), None)


# --- the plan ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RolePlan:
    role: str
    calls: int
    distinct: int
    recorded: int
    to_record: int
    avg_in_tokens: int
    out_tokens: int
    keys_to_record: tuple[str, ...]


@dataclass(frozen=True)
class FixturePlan:
    """The plan ``plan`` prints and writes (canonical JSON, no clock)."""

    scenarios: tuple[str, ...]
    bt01: str
    desc: str
    roles: tuple[RolePlan, ...]
    per_scenario: dict[str, dict[str, int]]

    @property
    def to_record(self) -> int:
        return sum(r.to_record for r in self.roles)

    @property
    def calls(self) -> int:
        return sum(r.calls for r in self.roles)

    @property
    def distinct(self) -> int:
        return sum(r.distinct for r in self.roles)

    @property
    def recorded(self) -> int:
        return sum(r.recorded for r in self.roles)

    def avg_in_tokens(self) -> int:
        n = self.to_record
        if n == 0:
            return 0
        return int(math.ceil(sum(r.avg_in_tokens * r.to_record for r in self.roles) / n))

    def avg_out_tokens(self) -> int:
        n = self.to_record
        if n == 0:
            return 0
        return int(math.ceil(sum(r.out_tokens * r.to_record for r in self.roles) / n))

    def keys_to_record(self) -> dict[str, list[str]]:
        return {r.role: list(r.keys_to_record) for r in self.roles if r.keys_to_record}

    def to_json(self) -> dict[str, Any]:
        return {
            "scenarios": list(self.scenarios),
            "bt01": self.bt01,
            "desc": self.desc,
            "approval_phrase": approval_phrase(self),
            "gate_command": gate_command(self) if self.to_record else None,
            "max_calls": max_calls_for(self.to_record),
            "totals": {
                "calls": self.calls,
                "distinct": self.distinct,
                "recorded": self.recorded,
                "to_record": self.to_record,
                "avg_in_tokens": self.avg_in_tokens(),
                "avg_out_tokens": self.avg_out_tokens(),
            },
            "roles": [
                {
                    "role": r.role,
                    "calls": r.calls,
                    "distinct": r.distinct,
                    "recorded": r.recorded,
                    "to_record": r.to_record,
                    "avg_in_tokens": r.avg_in_tokens,
                    "out_tokens_estimate": r.out_tokens,
                    "keys_to_record": list(r.keys_to_record),
                }
                for r in self.roles
            ],
            "per_scenario": self.per_scenario,
        }


def describe(scenarios: Sequence[str]) -> str:
    """The gate ``--desc``: the WO-093 wording, or the BT-01-pending wording."""
    if BT01_ID in scenarios:
        return "record fixtures S04-S08 BT-01 handoffs"
    return "record fixtures S04-S08 handoffs"


def approval_phrase(plan: FixturePlan) -> str:
    """Exactly what ``scripts/paid-run-gate.py`` asks the owner to type."""
    return f"approve paid run {plan.desc}"


def gate_command(plan: FixturePlan) -> str:
    return (
        f"uv run python scripts/paid-run-gate.py --role {GATE_ROLE}"
        f" --calls {plan.to_record} --in-tokens {plan.avg_in_tokens()}"
        f' --out-tokens {plan.avg_out_tokens()} --desc "{plan.desc}"'
    )


def max_calls_for(planned: int) -> int:
    """The most transport calls ``record`` makes for a plan of ``planned`` calls."""
    return max(planned, int(math.floor(planned * (1.0 + OVERRUN_FRACTION) + 1e-9)))


def plan_from_captures(
    captured: Sequence[CapturedCall], configs: Sequence[RunConfig]
) -> FixturePlan:
    scenarios = tuple(c.scenario for c in configs)
    roles: list[RolePlan] = []
    for role in ROLES:
        calls = [c for c in captured if c.role == role]
        first: dict[str, CapturedCall] = {}
        for c in calls:
            first.setdefault(c.key, c)
        todo = sorted(k for k, c in first.items() if not c.hit)
        basis = [first[k] for k in todo] or list(first.values())
        avg_in = int(math.ceil(sum(c.in_tokens for c in basis) / len(basis))) if basis else 0
        roles.append(
            RolePlan(
                role=role,
                calls=len(calls),
                distinct=len(first),
                recorded=len(first) - len(todo),
                to_record=len(todo),
                avg_in_tokens=avg_in,
                out_tokens=OUT_TOKENS_ESTIMATE[role],
                keys_to_record=tuple(todo),
            )
        )
    per: dict[str, dict[str, int]] = {}
    for cfg in configs:
        row: dict[str, int] = {}
        for role in ROLES:
            n = len({c.key for c in captured if c.scenario == cfg.scenario and c.role == role})
            if n:
                row[role] = n
        per[cfg.scenario] = row
    bt01 = "included" if BT01_ID in scenarios else "BT-01 pending"
    return FixturePlan(scenarios, bt01, describe(scenarios), tuple(roles), per)


def build_plan(
    *,
    root: Path | None = None,
    scenarios: Iterable[str] | None = None,
    scenario_root: Path = SCENARIO_ROOT,
) -> FixturePlan:
    """Run the set in replay over a capturing store at ``root``; nothing calls out."""
    configs = run_configs(scenarios, root=scenario_root)
    store = CapturingStore(root)
    with tempfile.TemporaryDirectory() as tmp:
        run_scenario_set(store, configs=configs, violations_path=Path(tmp) / "VIOLATIONS.md")
    return plan_from_captures(store.captured, configs)


@dataclass(frozen=True)
class Prices:
    usd_per_mtok_in: float
    usd_per_mtok_out: float

    @classmethod
    def from_env(cls) -> Prices | None:
        try:
            return cls(float(os.environ[PRICE_IN_ENV]), float(os.environ[PRICE_OUT_ENV]))
        except (KeyError, ValueError):
            return None

    def usd(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.usd_per_mtok_in + tokens_out * self.usd_per_mtok_out) / 1e6


def plan_usd(plan: FixturePlan, prices: Prices) -> tuple[float, float]:
    """(estimate, worst case): the worst case is ``max_calls_for`` calls at ``max_tokens``."""
    n = plan.to_record
    est = prices.usd(n * plan.avg_in_tokens(), n * plan.avg_out_tokens())
    worst_n = max_calls_for(n)
    worst = prices.usd(worst_n * plan.avg_in_tokens(), worst_n * MAX_OUT_TOKENS)
    return round6(est), round6(worst)


def plan_table(plan: FixturePlan, prices: Prices | None = None) -> str:
    """The table ``plan`` prints."""
    head = (
        f"{'role':<18} {'calls':>5} {'distinct':>8} {'recorded':>8} {'to record':>9}"
        f" {'avg in tok':>10} {'out tok (est)':>13}"
    )
    lines = [
        f"Fixture-run plan (replay, zero network). Scenarios: {', '.join(plan.scenarios)}"
        + ("" if BT01_ID in plan.scenarios else " (BT-01 pending)"),
        head,
        "-" * len(head),
    ]
    for r in plan.roles:
        lines.append(
            f"{r.role:<18} {r.calls:>5} {r.distinct:>8} {r.recorded:>8} {r.to_record:>9}"
            f" {r.avg_in_tokens:>10} {r.out_tokens:>13}"
        )
    lines.append("-" * len(head))
    lines.append(
        f"{'total':<18} {plan.calls:>5} {plan.distinct:>8} {plan.recorded:>8}"
        f" {plan.to_record:>9} {plan.avg_in_tokens():>10} {plan.avg_out_tokens():>13}"
    )
    lines.append("")
    lines.append(
        "per scenario (distinct keys): "
        + "; ".join(
            f"{sid} " + ", ".join(f"{k}={v}" for k, v in row.items())
            for sid, row in plan.per_scenario.items()
        )
    )
    lines.append(
        "input tokens = ceil(chars/4) of system prompt + canonical inputs; output tokens are"
        f" stated estimates (max_tokens {MAX_OUT_TOKENS} is the worst case)"
    )
    lines.append(
        f"record makes at most {max_calls_for(plan.to_record)} calls (plan + 10 %), each"
        " distinct key once; the model's picks can change later keys"
    )
    if prices is not None and plan.to_record:
        est, worst = plan_usd(plan, prices)
        lines.append(
            f"estimate: ${est:.4f}  worst case: ${worst:.4f}  (cap ${PAID_RUN_CAP_USD:.2f})"
        )
    elif plan.to_record:
        lines.append(f"USD: set {PRICE_IN_ENV} and {PRICE_OUT_ENV} to print the estimate")
    lines.append("")
    if plan.to_record:
        lines.append("gate (prints model and USD, asks for the phrase):")
        lines.append(f"  {gate_command(plan)}")
        lines.append(f"approval phrase: {approval_phrase(plan)}")
    else:
        lines.append("every planned fixture is recorded; no paid run is needed")
    return "\n".join(lines) + "\n"


def write_plan(plan: FixturePlan, path: Path = PLAN_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_json(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


# --- the paid-runs log -------------------------------------------------------------------


@dataclass(frozen=True)
class PaidRunRow:
    when: str
    role: str
    calls: int
    cost_usd: float
    model: str
    desc: str
    approver: str

    @property
    def from_gate(self) -> bool:
        return self.approver == GATE_APPROVER

    @property
    def from_harness(self) -> bool:
        return HARNESS_APPROVER_MARK in self.approver


def parse_paid_runs(text: str) -> list[PaidRunRow]:
    """The table rows of ``docs/log/PAID-RUNS.md`` (header and separator skipped)."""
    rows: list[PaidRunRow] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 7 or cells[0] in ("when", "") or set(cells[0]) <= {"-"}:
            continue
        m = re.search(r"-?\d+(?:\.\d+)?", cells[3])
        try:
            calls = int(cells[2])
        except ValueError:
            calls = 0
        rows.append(
            PaidRunRow(
                when=cells[0],
                role=cells[1],
                calls=calls,
                cost_usd=float(m.group(0)) if m else 0.0,
                model=cells[4],
                desc=cells[5],
                approver=" | ".join(cells[6:]),
            )
        )
    return rows


def _row_desc(desc: str) -> str:
    return desc.split(";", 1)[0].strip()


def spent_usd(rows: Sequence[PaidRunRow], *, role: str = GATE_ROLE, desc: str) -> float:
    """Dollars spent so far, counting each paid run once.

    A harness row (actual cost) replaces the gate row (estimate) it follows for the same
    role and description. The newest gate row for ``(role, desc)`` with no harness row
    after it is this run's own approval and is not counted.
    """
    superseded: set[int] = set()
    for i, row in enumerate(rows):
        if not row.from_harness:
            continue
        for j in range(i - 1, -1, -1):
            prev = rows[j]
            if (
                j not in superseded
                and prev.from_gate
                and prev.role == row.role
                and _row_desc(prev.desc) == _row_desc(row.desc)
            ):
                superseded.add(j)
                break
    pending = [
        i
        for i, row in enumerate(rows)
        if i not in superseded and row.from_gate and row.role == role and row.desc == desc
    ]
    if pending:
        superseded.add(pending[-1])
    return round6(sum(r.cost_usd for i, r in enumerate(rows) if i not in superseded))


def remaining_budget(
    paid_runs_path: Path = PAID_RUNS_PATH, *, cap: float = PAID_RUN_CAP_USD, desc: str
) -> float:
    text = paid_runs_path.read_text(encoding="utf-8") if paid_runs_path.is_file() else ""
    return round6(cap - spent_usd(parse_paid_runs(text), desc=desc))


def default_now() -> str:
    """The gate's timestamp format (the one wall-clock read in this module)."""
    return dt.datetime.now().astimezone().isoformat(timespec="minutes")


def append_paid_run(path: Path, row: PaidRunRow) -> None:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    with path.open("a", encoding="utf-8") as fh:
        if text and not text.endswith("\n"):
            fh.write("\n")
        fh.write(
            f"| {row.when} | {row.role} | {row.calls} | ${row.cost_usd:.4f} | {row.model}"
            f" | {row.desc} | {row.approver} |\n"
        )


# --- record ------------------------------------------------------------------------------


class RecordRefused(RuntimeError):
    """``record`` refused before any call (exit 3)."""


@dataclass(frozen=True)
class RecordSummary:
    plan: FixturePlan
    calls: int
    keys: dict[str, list[str]]
    rejected: dict[str, list[str]]
    tokens_in: int
    tokens_out: int
    cost_usd: float
    stopped: str | None
    remaining_before: float
    denylisted: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stopped is None

    def text(self) -> str:
        lines = [
            f"record: {self.calls} transport call(s), "
            + ", ".join(f"{r}={len(k)}" for r, k in sorted(self.keys.items()))
            if self.calls
            else "record: nothing to record; zero transport calls",
            f"tokens in/out: {self.tokens_in}/{self.tokens_out}  cost: ${self.cost_usd:.4f}"
            f"  budget left before: ${self.remaining_before:.4f}",
            "guard rejections: "
            + (
                ", ".join(f"{r}={len(k)}" for r, k in sorted(self.rejected.items()))
                if self.rejected
                else "none"
            ),
        ]
        if self.denylisted:
            lines.append(
                f"denylisted words in {len(self.denylisted)} fixture(s) (the denylist check"
                f" scans *.json; decide before committing): {', '.join(self.denylisted)}"
            )
        if self.stopped:
            lines.append(f"STOPPED: {self.stopped}")
        lines.append("now: unset MODEL_MODE (back to replay) and run the verify step")
        return "\n".join(lines) + "\n"


def _violation_rows(path: Path) -> list[str]:
    if not path.is_file():
        return []
    body = path.read_text(encoding="utf-8")
    if body.startswith(VIOLATIONS_HEADER):
        body = body[len(VIOLATIONS_HEADER) :]
    return [ln for ln in body.splitlines() if ln.startswith("| ") and _KEY_RE.search(ln)]


def _rejected_keys(rows: Iterable[str]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for line in rows:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not _KEY_RE.fullmatch(cells[2]):
            continue
        out.setdefault(cells[1], set()).add(cells[2])
    return {role: sorted(keys) for role, keys in sorted(out.items())}


def _check_record_env(env: Mapping[str, str], plan_desc: str) -> None:
    if in_ci():
        raise RecordRefused("paid runs are forbidden in CI (SOUNDINGS_CI is set; CLAUDE.md 2.3)")
    mode = env.get("MODEL_MODE", "").strip().lower()
    if mode != ModelMode.RECORD.value:
        raise RecordRefused(f"record needs MODEL_MODE=record (got {mode or 'unset'!r})")
    phrase = f"approve paid run {plan_desc}"
    if env.get(APPROVAL_ENV, "").strip() != phrase:
        raise RecordRefused(f"{APPROVAL_ENV} must equal the gate's phrase exactly: {phrase!r}")


def record(
    *,
    transport: Transport = anthropic_record_transport,
    root: Path | None = None,
    paid_runs_path: Path = PAID_RUNS_PATH,
    violations_path: Path = DEFAULT_VIOLATIONS_PATH,
    scenarios: Iterable[str] | None = None,
    scenario_root: Path = SCENARIO_ROOT,
    now: Callable[[], str] = default_now,
    cap: float = PAID_RUN_CAP_USD,
) -> RecordSummary:
    """The gated recording run (see the module docstring). Raises ``RecordRefused``."""
    environ: Mapping[str, str] = os.environ
    configs = run_configs(scenarios, root=scenario_root)
    desc = describe([c.scenario for c in configs])
    _check_record_env(environ, desc)
    model = model_name_from_env()
    if not model:
        raise RecordRefused("MODEL_NAME is not set")
    try:
        prices = Prices(float(environ[PRICE_IN_ENV]), float(environ[PRICE_OUT_ENV]))
    except (KeyError, ValueError):
        raise RecordRefused(f"set {PRICE_IN_ENV} and {PRICE_OUT_ENV} from the price list") from None

    plan = build_plan(root=root, scenarios=scenarios, scenario_root=scenario_root)
    remaining = remaining_budget(paid_runs_path, cap=cap, desc=plan.desc)
    if plan.to_record == 0:
        return RecordSummary(plan, 0, {}, {}, 0, 0, 0.0, None, remaining)
    _est, worst = plan_usd(plan, prices)
    if worst > remaining:
        raise RecordRefused(
            f"worst case ${worst:.4f} exceeds the ${remaining:.4f} left of the"
            f" ${cap:.2f} cap (docs/log/PAID-RUNS.md)"
        )

    client = ModelClient(
        mode=ModelMode.RECORD,
        store=FixtureStore(root),
        model_name=model,
        transport=transport,
    )
    store = RecordingStore(client, max_calls=max_calls_for(plan.to_record))
    with tempfile.TemporaryDirectory() as tmp:
        tmp_violations = Path(tmp) / "VIOLATIONS.md"
        run = run_scenario_set(store, configs=configs, violations_path=tmp_violations)
        new_rows = _violation_rows(tmp_violations)
    if new_rows:
        violations_path.parent.mkdir(parents=True, exist_ok=True)
        if not violations_path.is_file() or violations_path.stat().st_size == 0:
            violations_path.write_text(VIOLATIONS_HEADER, encoding="utf-8")
        existing = violations_path.read_text(encoding="utf-8")
        with violations_path.open("a", encoding="utf-8") as fh:
            if not existing.endswith("\n"):
                fh.write("\n")
            fh.writelines(f"{ln}\n" for ln in new_rows)
    spend = store.spend
    cost = round6(prices.usd(spend.tokens_in, spend.tokens_out))
    recorded_keys = {r: sorted(set(k)) for r, k in sorted(spend.keys.items())}
    recorded_set = {k for keys in recorded_keys.values() for k in keys}
    rejected = {
        r: [k for k in keys if k in recorded_set] for r, keys in _rejected_keys(new_rows).items()
    }
    rejected = {r: k for r, k in rejected.items() if k}
    flagged = _denylisted_fixtures(client.store, recorded_keys)
    if spend.calls:
        n_fixtures = len(recorded_set)
        append_paid_run(
            paid_runs_path,
            PaidRunRow(
                when=now(),
                role=GATE_ROLE,
                calls=spend.calls,
                cost_usd=cost,
                model=model,
                desc=(
                    f"{plan.desc}; {n_fixtures} fixtures; tokens in/out"
                    f" {spend.tokens_in}/{spend.tokens_out}"
                    + (f"; stopped: {run.stopped}" if run.stopped else "")
                ),
                approver=f"owner ({APPROVAL_ENV}); {HARNESS_APPROVER_MARK}",
            ),
        )
        _write_manifest(client.store.root, plan, model, recorded_keys, rejected)
    return RecordSummary(
        plan,
        spend.calls,
        recorded_keys,
        rejected,
        spend.tokens_in,
        spend.tokens_out,
        cost,
        run.stopped,
        remaining,
        flagged,
    )


def _denylisted_fixtures(store: FixtureStore, keys: Mapping[str, list[str]]) -> list[str]:
    """``role/key`` of every written fixture whose reply holds a denylisted word.

    ``scripts/denylist-check.sh`` scans ``*.json``, so such a fixture cannot be committed
    as is; the guard already rejected it at run time (the template was used).
    """
    from soundings.schemas.denylist import find_denylisted

    out: list[str] = []
    for role, ks in sorted(keys.items()):
        for key in ks:
            path = store.path(role, key)
            if path.is_file() and find_denylisted(path.read_text(encoding="utf-8")):
                out.append(f"{role}/{key}")
    return out


def _write_manifest(
    root: Path,
    plan: FixturePlan,
    model: str,
    keys: Mapping[str, list[str]],
    rejected: Mapping[str, list[str]],
) -> Path:
    """Merge this run's keys into ``<root>/fixture_run.json`` (sorted, no clock)."""
    path = root / RUN_MANIFEST
    data: dict[str, Any] = {"runs": [], "keys": {}, "rejected": {}}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    data["runs"].append({"desc": plan.desc, "model": model, "scenarios": list(plan.scenarios)})
    for field_name, new in (("keys", keys), ("rejected", rejected)):
        merged: dict[str, list[str]] = data.get(field_name, {})
        for role, ks in new.items():
            merged[role] = sorted(set(merged.get(role, [])) | set(ks))
        data[field_name] = dict(sorted(merged.items()))
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def read_manifest(root: Path | None = None) -> dict[str, Any]:
    path = (root if root is not None else FixtureStore().root) / RUN_MANIFEST
    if not path.is_file():
        return {"runs": [], "keys": {}, "rejected": {}}
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


# --- verify ------------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyReport:
    scenarios: tuple[str, ...]
    lookups: int
    misses: tuple[CapturedCall, ...]
    violations: int
    unexpected_violations: tuple[str, ...]
    regressions: dict[str, list[str]]
    llm_choices: dict[str, int]
    expected_failures: dict[str, list[str]]
    baseline_failures: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.misses and not self.unexpected_violations and not self.regressions

    def text(self) -> str:
        lines = [
            f"verify: {len(self.scenarios)} scenario(s), {self.lookups} model lookups,"
            f" {len(self.misses)} replay miss(es)",
            f"guard violations: {self.violations} ({len(self.unexpected_violations)} not"
            " recorded as rejected)",
            "planner choices accepted from fixtures: "
            + ", ".join(f"{s}={n}" for s, n in self.llm_choices.items()),
        ]
        for sid, keys in self.baseline_failures.items():
            if keys:
                lines.append(f"{sid}: expected-block keys failing without the model too: {keys}")
        for sid, keys in self.regressions.items():
            lines.append(f"REGRESSION {sid}: fails {keys} only with the model roles on")
        for m in self.misses[:10]:
            lines.append(f"MISS {m.scenario} {m.role} {m.key}")
        lines.append("verify: OK" if self.ok else "verify: FAILED")
        return "\n".join(lines) + "\n"


def verify(
    *,
    root: Path | None = None,
    scenarios: Iterable[str] | None = None,
    scenario_root: Path = SCENARIO_ROOT,
) -> VerifyReport:
    """Replay the set over the recorded fixtures and compare with the deterministic run."""
    configs = run_configs(scenarios, root=scenario_root)
    store = CapturingStore(root)
    manifest = read_manifest(store.root)
    expected_rejections = {k for keys in manifest.get("rejected", {}).values() for k in keys}
    with tempfile.TemporaryDirectory() as tmp:
        violations = Path(tmp) / "VIOLATIONS.md"
        run = run_scenario_set(store, configs=configs, violations_path=violations, grade=True)
        rows = _violation_rows(violations)
        baseline: dict[str, tuple[str, ...]] = {}
        for cfg in configs:
            det = _run_one(cfg, None, violations_path=Path(tmp) / "det.md", llm=False)
            baseline[cfg.scenario] = _expected_failures(det, cfg)
    keys_hit = [k for keys in _rejected_keys(rows).values() for k in keys]
    unexpected = tuple(sorted(k for k in set(keys_hit) if k not in expected_rejections))
    regressions: dict[str, list[str]] = {}
    for out in run.outcomes:
        extra = sorted(set(out.expected_failures) - set(baseline.get(out.scenario, ())))
        if extra:
            regressions[out.scenario] = extra
    return VerifyReport(
        scenarios=tuple(c.scenario for c in configs),
        lookups=len(store.captured),
        misses=tuple(c for c in store.captured if not c.hit),
        violations=len(rows),
        unexpected_violations=unexpected,
        regressions=regressions,
        llm_choices={o.scenario: o.llm_choices for o in run.outcomes},
        expected_failures={o.scenario: list(o.expected_failures) for o in run.outcomes},
        baseline_failures={sid: list(keys) for sid, keys in baseline.items()},
    )


# --- CLI ---------------------------------------------------------------------------------


def _scenarios_arg(text: str | None) -> list[str] | None:
    if not text:
        return None
    return [s.strip() for s in text.split(",") if s.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m soundings.llm.fixtures",
        description="Plan, record (owner-approved, gated) and verify the model-call fixtures.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (
        ("plan", "count the calls the fixture run would make (replay; zero network)"),
        ("record", "the paid run: refused unless MODEL_MODE=record and the approval env match"),
        ("verify", "replay the recorded fixtures: zero misses, no outcome regressions"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--scenarios", default=None, help="comma list; default: the full set")
        p.add_argument("--fixture-root", type=Path, default=None, help="default: the store root")
        if name == "plan":
            p.add_argument("--out", type=Path, default=PLAN_PATH)
        if name == "record":
            p.add_argument("--paid-runs", type=Path, default=PAID_RUNS_PATH)
    args = parser.parse_args(argv)
    with quiet_replay_misses():
        return _main(args)


@contextlib.contextmanager
def quiet_replay_misses() -> Iterator[None]:
    """Replay misses are what ``plan`` counts: drop the per-miss warning while a CLI runs,
    then restore the logger (tests share the process)."""
    logger = logging.getLogger("soundings.llm.client")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(level)


def _main(args: argparse.Namespace) -> int:
    scenarios = _scenarios_arg(args.scenarios)
    try:
        if args.cmd == "plan":
            if ModelMode.from_env() is not ModelMode.REPLAY:
                print("plan runs in MODEL_MODE=replay only", file=sys.stderr)
                return EXIT_REFUSED
            if in_ci():
                print("plan expects replay misses, which raise under SOUNDINGS_CI", file=sys.stderr)
                return EXIT_REFUSED
            plan = build_plan(root=args.fixture_root, scenarios=scenarios)
            sys.stdout.write(plan_table(plan, Prices.from_env()))
            print(f"plan written to {write_plan(plan, args.out)}")
            return EXIT_OK
        if args.cmd == "record":
            try:
                summary = record(
                    root=args.fixture_root, paid_runs_path=args.paid_runs, scenarios=scenarios
                )
            except RecordRefused as exc:
                print(f"record refused: {exc}", file=sys.stderr)
                return EXIT_REFUSED
            sys.stdout.write(summary.text())
            return EXIT_OK if summary.ok else EXIT_FAILED
        if ModelMode.from_env() is not ModelMode.REPLAY:
            print("verify runs in MODEL_MODE=replay only", file=sys.stderr)
            return EXIT_REFUSED
        try:
            report = verify(root=args.fixture_root, scenarios=scenarios)
        except ReplayMiss as exc:  # under SOUNDINGS_CI a miss raises (WORKFLOW 8)
            print(f"verify: FAILED ({exc})", file=sys.stderr)
            return EXIT_FAILED
        sys.stdout.write(report.text())
        return EXIT_OK if report.ok else EXIT_FAILED
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
