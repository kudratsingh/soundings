"""The investigation state machine, the stopping rule and the frozen recommendation.

SPEC-INVESTIGATION sections 6 and 7, SPEC-LADDER sections 1 to 3 (INVESTIGATING runs at
rung ``ask``), CLAUDE.md section 4 (hand-rolled machine: typed states, a checkpoint per
transition, an idempotency key per action, deterministic guards).

What lives here (WO-033 part A):

* ``evaluate_stop(inputs, cfg)``: SPEC-INVESTIGATION section 7 in the safety-first
  precedence of WO-033 (``C_CLEARED``, ``C_LOST``, ``C_THREAT``, ``C_KNOWN_OPERATOR``,
  ``C_BENIGN``, ``C_BUDGET``); every threshold comes from the ``stopping`` block of
  ``config/ladder.yaml`` through ``load_stopping_config``. No threshold literal appears
  in this file (a grep test enforces it).
* ``InvestigationMachine``: score, decide (the stop rule first, then the planner),
  probe, rescore. The loop is bounded by the ``budgets`` block of ``config/ladder.yaml``
  and by the scenario's last tick; budgets count planner-chosen probes only, and the
  ask-rung hail that runner v1 already sent arrives as one paid probe in the starting
  ledger (DECISIONS 2026-10-16 23:05).
* ``Recommendation``: frozen. ``recommended_rung`` is a computed field of ``outcome``
  (``RECOMMENDED_RUNG``), so no caller, writer or model can set it; the investigation
  only ever recommends ``ask`` or ``hand_to_person`` (CLAUDE.md 2.1).

The planner, the probe executor and the question writer are injected through the
``Planner``, ``ProbeRunner`` and ``QuestionWriter`` Protocols below; WO-033 part B wires
WO-031's deterministic planner and probe executor and WO-032's roles into them. The
question for the human is text only and never feeds any other field (CLAUDE.md 2.2).

Determinism (CLAUDE.md 2.4): time is the sim tick. A probe lands
``ceil(latency_s / tick_s)`` ticks after it is chosen; there is no wall clock and no
randomness here. Floats are rounded to 6 dp at the output boundary.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol

from pydantic import (
    Field,
    ModelWrapValidatorHandler,
    ValidationInfo,
    computed_field,
    model_validator,
)

from soundings.canonical import canonical_json, round6, stable_hash
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import ACOUSTIC_SOURCE_PREFIX, NOT_CLOSING_S
from soundings.investigation.posterior import (
    STRESS_TEST_LABEL,
    LikelihoodTables,
    PosteriorReport,
    PriorConfig,
    score_contact,
)
from soundings.probe_keys import idempotency_key, tick_bucket
from soundings.schemas._base import (
    NonNegFloat,
    NonNegInt,
    Probability,
    StrictModel,
    from_file_data,
)
from soundings.schemas.config import LADDER_PATH, load_ladder_config
from soundings.schemas.denylist import FreeText, find_denylisted
from soundings.schemas.enums import (
    EvidenceKind,
    FeatureName,
    HailReply,
    Hypothesis,
    ProbeId,
    RegistryMatch,
    Rung,
)
from soundings.schemas.evidence import UNKNOWN, EvidenceRow, EvidenceValue

GENESIS_HASH: Final = "0" * 64
SECONDS_PER_MINUTE: Final = 60

# --- stopping rule ---------------------------------------------------------------------


class StopOutcome(StrEnum):
    """The five outcomes of SPEC-INVESTIGATION section 7 (``stop_rule_fired(outcome)``)."""

    RESOLVED_BENIGN = "RESOLVED_BENIGN"
    HAZARD_NOTICE = "HAZARD_NOTICE"
    CLEARED_AT_ASK = "CLEARED_AT_ASK"
    LOST_VEHICLE = "LOST_VEHICLE"
    HANDOFF = "HANDOFF"


class StopRuleId(StrEnum):
    """The stop clauses in precedence order (first match wins)."""

    C_CLEARED = "C_CLEARED"
    C_LOST = "C_LOST"
    C_THREAT = "C_THREAT"
    C_KNOWN_OPERATOR = "C_KNOWN_OPERATOR"
    C_BENIGN = "C_BENIGN"
    C_BUDGET = "C_BUDGET"


STOP_PRECEDENCE: Final[tuple[StopRuleId, ...]] = tuple(StopRuleId)


class StoppingConfig(StrictModel):
    """``config/ladder.yaml`` ``stopping``. Spec section 7's ``lost_vehicle_minutes`` is
    the config key ``lost_vehicle_silence_min``; the config key is the source."""

    benign_mass_resolve: Probability
    benign_time_guard_s: NonNegFloat
    threat_mass_handoff: Probability
    imminent_time_s: NonNegFloat
    imminent_benign_mass_max: Probability
    lost_vehicle_silence_min: NonNegFloat

    @property
    def lost_vehicle_silence_s(self) -> float:
        return self.lost_vehicle_silence_min * SECONDS_PER_MINUTE


def load_stopping_config(path: str | Path = LADDER_PATH) -> StoppingConfig:
    """The ``stopping`` block of ``config/ladder.yaml``; a missing or extra key raises."""
    block = load_ladder_config(path).stopping
    return StoppingConfig.model_validate({k: float(v) for k, v in block.items()})


class StopInputs(StrictModel):
    """What the stop rule reads: the posterior plus the latest evidence rows.

    ``None`` means the evidence is absent or ``unknown``; an absent value never satisfies
    a clause. ``ttc_s`` is ``time_to_nearest_critical_asset``; ``None`` (unknown, or
    ``NOT_CLOSING_S``) means no critical asset is being approached. ``silence_s`` is the
    time since the latest acoustic-detector fact row or ``hail_reply=valid`` row.
    ``row_ids`` are the evidence rows these inputs were read from (cited, not scored).
    """

    benign_mass: Probability
    threat_mass: Probability
    top: Hypothesis
    p_known_operator: Probability = 0.0
    registry_match: RegistryMatch | None = None
    hail_reply: HailReply | None = None
    operator_known: bool | None = None
    ttc_s: NonNegFloat | None = None
    silence_s: NonNegFloat | None = None
    budget_exhausted: bool = False
    row_ids: tuple[str, ...] = ()


class StopDecision(StrictModel):
    outcome: StopOutcome
    rule_id: StopRuleId
    uncertain: bool = False


def _below(value: float | None, limit: float) -> bool:
    return value is not None and value < limit


def evaluate_stop(inputs: StopInputs, cfg: StoppingConfig) -> StopDecision | None:
    """SPEC-INVESTIGATION section 7 plus the approved ``C_KNOWN_OPERATOR`` clause.

    Clauses in precedence order; the first that holds wins, and ``None`` means keep
    investigating:

    1. ``C_CLEARED``: registry exact and hail valid -> ``CLEARED_AT_ASK``.
    2. ``C_LOST``: top ``FILED_OFF_PLAN``, hail none/timeout, silence above the
       lost-vehicle limit -> ``LOST_VEHICLE``.
    3. ``C_THREAT``: threat mass at or above the handoff threshold, or an imminent
       critical asset with benign mass below the imminent maximum -> ``HANDOFF``.
    4. ``C_KNOWN_OPERATOR``: top ``UNFILED_KNOWN_OPERATOR`` with P at or above the benign
       resolve threshold, hail valid, operator known, outside the benign time guard ->
       ``RESOLVED_BENIGN`` (DECISIONS 2026-10-16 23:05).
    5. ``C_BENIGN``: benign mass at or above the resolve threshold outside the benign
       time guard -> ``HAZARD_NOTICE`` when top is ``DEBRIS_OR_DRIFT``, else
       ``RESOLVED_BENIGN``.
    6. ``C_BUDGET``: budget exhausted -> ``HANDOFF`` with ``uncertain=True``.
    """
    if inputs.registry_match is RegistryMatch.EXACT and inputs.hail_reply is HailReply.VALID:
        return StopDecision(outcome=StopOutcome.CLEARED_AT_ASK, rule_id=StopRuleId.C_CLEARED)
    if (
        inputs.top is Hypothesis.FILED_OFF_PLAN
        and inputs.hail_reply in (HailReply.NONE, HailReply.TIMEOUT)
        and inputs.silence_s is not None
        and inputs.silence_s > cfg.lost_vehicle_silence_s
    ):
        return StopDecision(outcome=StopOutcome.LOST_VEHICLE, rule_id=StopRuleId.C_LOST)
    imminent = _below(inputs.ttc_s, cfg.imminent_time_s)
    if inputs.threat_mass >= cfg.threat_mass_handoff or (
        imminent and inputs.benign_mass < cfg.imminent_benign_mass_max
    ):
        return StopDecision(outcome=StopOutcome.HANDOFF, rule_id=StopRuleId.C_THREAT)
    guarded = _below(inputs.ttc_s, cfg.benign_time_guard_s)
    if (
        inputs.top is Hypothesis.UNFILED_KNOWN_OPERATOR
        and inputs.p_known_operator >= cfg.benign_mass_resolve
        and inputs.hail_reply is HailReply.VALID
        and inputs.operator_known is True
        and not guarded
    ):
        return StopDecision(
            outcome=StopOutcome.RESOLVED_BENIGN, rule_id=StopRuleId.C_KNOWN_OPERATOR
        )
    if inputs.benign_mass >= cfg.benign_mass_resolve and not guarded:
        outcome = (
            StopOutcome.HAZARD_NOTICE
            if inputs.top is Hypothesis.DEBRIS_OR_DRIFT
            else StopOutcome.RESOLVED_BENIGN
        )
        return StopDecision(outcome=outcome, rule_id=StopRuleId.C_BENIGN)
    if inputs.budget_exhausted:
        return StopDecision(
            outcome=StopOutcome.HANDOFF, rule_id=StopRuleId.C_BUDGET, uncertain=True
        )
    return None


def _latest(table: EvidenceTable, contact_id: str, feature: FeatureName) -> EvidenceRow | None:
    return table.latest(contact_id, feature)


def _known(row: EvidenceRow | None) -> EvidenceValue | None:
    if row is None or (isinstance(row.value, str) and row.value == UNKNOWN):
        return None
    return row.value


def _enum_or_none[E: StrEnum](cls: type[E], value: EvidenceValue | None) -> E | None:
    if not isinstance(value, str):
        return None
    try:
        return cls(value)
    except ValueError:
        return None


def last_heard_tick(table: EvidenceTable, contact_id: str) -> tuple[int, str] | None:
    """(tick, row id) of the latest acoustic-detector fact row or ``hail_reply=valid`` row."""
    best: tuple[int, str] | None = None
    for row in table.for_contact(contact_id):
        heard = (
            row.kind is EvidenceKind.FACT and row.source.startswith(ACOUSTIC_SOURCE_PREFIX)
        ) or (row.feature is FeatureName.HAIL_REPLY and row.value == HailReply.VALID.value)
        if heard and (best is None or row.t >= best[0]):
            best = (row.t, row.row_id)
    return best


def stop_inputs_from(
    report: PosteriorReport,
    table: EvidenceTable,
    *,
    tick: int,
    tick_s: float,
    budget_exhausted: bool = False,
) -> StopInputs:
    """Build ``StopInputs`` for ``report.contact_id`` at ``tick`` from the posterior and
    the latest evidence rows (``EvidenceTable.latest``)."""
    cid = report.contact_id
    post = report.posterior
    cited: list[str] = []

    def read(feature: FeatureName) -> EvidenceValue | None:
        row = _latest(table, cid, feature)
        if row is not None:
            cited.append(row.row_id)
        return _known(row)

    registry = _enum_or_none(RegistryMatch, read(FeatureName.REGISTRY_MATCH))
    hail = _enum_or_none(HailReply, read(FeatureName.HAIL_REPLY))
    op_value = read(FeatureName.OPERATOR_KNOWN)
    operator_known = op_value if isinstance(op_value, bool) else None
    ttc_value = read(FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET)
    ttc: float | None = None
    if isinstance(ttc_value, int | float) and not isinstance(ttc_value, bool):
        ttc = float(ttc_value) if 0.0 <= float(ttc_value) < NOT_CLOSING_S else None
    heard = last_heard_tick(table, cid)
    silence: float | None = None
    if heard is not None:
        cited.append(heard[1])
        silence = round6(max(0, tick - heard[0]) * float(tick_s))
    return StopInputs(
        benign_mass=round6(post.benign_mass()),
        threat_mass=round6(post.threat_mass()),
        top=post.top(),
        p_known_operator=round6(post.p(Hypothesis.UNFILED_KNOWN_OPERATOR)),
        registry_match=registry,
        hail_reply=hail,
        operator_known=operator_known,
        ttc_s=None if ttc is None else round6(ttc),
        silence_s=silence,
        budget_exhausted=budget_exhausted,
        row_ids=tuple(dict.fromkeys(cited)),
    )


def evaluate_stop_at(
    report: PosteriorReport,
    table: EvidenceTable,
    cfg: StoppingConfig,
    *,
    tick: int,
    tick_s: float,
    budget_exhausted: bool = False,
) -> tuple[StopInputs, StopDecision | None]:
    """``stop_inputs_from`` then ``evaluate_stop``: the rule over a posterior and a table."""
    inputs = stop_inputs_from(
        report, table, tick=tick, tick_s=tick_s, budget_exhausted=budget_exhausted
    )
    return inputs, evaluate_stop(inputs, cfg)


# --- budgets ---------------------------------------------------------------------------


class BudgetLimits(StrictModel):
    """``config/ladder.yaml`` ``budgets`` (per contact)."""

    probes_per_contact: NonNegInt
    paid_probes_per_contact: NonNegInt
    human_interruptions_per_contact: NonNegInt
    llm_calls_per_contact: NonNegInt


def load_budgets(path: str | Path = LADDER_PATH) -> BudgetLimits:
    """The ``budgets`` block of ``config/ladder.yaml``; a missing or extra key raises."""
    return BudgetLimits.model_validate(dict(load_ladder_config(path).budgets))


class InvestigationLedger(StrictModel):
    """Per-contact spend. ``probes_used`` counts planner-chosen probes only; ``paid_used``
    counts probes with cost > 0, including the ask-rung hail (start at ``paid_used=1``)."""

    probes_used: NonNegInt = 0
    paid_used: NonNegInt = 0
    interruptions: NonNegInt = 0
    llm_calls: NonNegInt = 0

    def allows(self, choice: ProbeChoice, limits: BudgetLimits) -> bool:
        return (
            self.probes_used + 1 <= limits.probes_per_contact
            and (not choice.paid or self.paid_used + 1 <= limits.paid_probes_per_contact)
            and self.interruptions + choice.interruption <= limits.human_interruptions_per_contact
        )

    def after(self, choice: ProbeChoice) -> InvestigationLedger:
        return InvestigationLedger(
            probes_used=self.probes_used + 1,
            paid_used=self.paid_used + (1 if choice.paid else 0),
            interruptions=self.interruptions + choice.interruption,
            llm_calls=self.llm_calls,
        )


class BudgetView(StrictModel):
    """What the planner sees of the budget: the limits, the spend, and the clock."""

    limits: BudgetLimits
    ledger: InvestigationLedger
    tick: NonNegInt
    ticks_left: NonNegInt


# --- injected collaborators ------------------------------------------------------------


class ProbeChoice(StrictModel):
    """One probe the planner proposes. Cost, latency, interruption and rung are the
    allowlist values (WO-031 ``ProbeSpec``); ``why`` is display text only."""

    probe_id: ProbeId
    args: dict[str, str | int | float | bool] = Field(default_factory=dict)
    cost_usd: NonNegFloat = 0.0
    latency_s: NonNegFloat = 0.0
    interruption: NonNegInt = 0
    rung_required: Rung = Rung.WATCH
    source: str = "deterministic"
    why: FreeText = ""

    @property
    def paid(self) -> bool:
        return self.cost_usd > 0.0


class Planner(Protocol):
    """Picks the next probe, or ``None`` when no informative probe fits the budget."""

    def next_probe(
        self, table: EvidenceTable, posterior: PosteriorReport, budget: BudgetView, rung: Rung
    ) -> ProbeChoice | None: ...


class ProbeRunner(Protocol):
    """Executes one probe and returns the evidence rows it produced (not yet appended).

    Raise ``ProbeError`` for a probe that failed in a way the investigation must record
    (``error`` -> ``ERRORED``); any other exception is a crash, and the run resumes from
    its checkpoints.
    """

    def run(
        self, probe_id: ProbeId, contact_id: str, tick: int, args: Mapping[str, Any]
    ) -> Sequence[EvidenceRow]: ...


class QuestionWriter(Protocol):
    """Writes the question for the human (text only; it never feeds another field)."""

    def question_for_human(
        self, report: PosteriorReport, decision: StopDecision, table: EvidenceTable
    ) -> str | None: ...


class InvestigationError(RuntimeError):
    """A collaborator failed in a way the investigation records (``error`` ->
    ``ERRORED``); the ladder then treats the case as failed and a person takes it."""


class ProbeError(InvestigationError):
    """A probe failed; the investigation records ``error`` and ends in ``ERRORED``."""


class TemplateQuestionWriter:
    """Deterministic default question (WO-032's template takes over in part B)."""

    def question_for_human(
        self, report: PosteriorReport, decision: StopDecision, table: EvidenceTable
    ) -> str | None:
        if decision.outcome is not StopOutcome.HANDOFF:
            return None
        ranked = report.posterior.ranked()
        first, second = ranked[0][0].value, ranked[1][0].value
        lead = "Evidence is inconclusive. " if decision.uncertain else ""
        return f"{lead}Can a person confirm whether this contact is {first} or {second}?"


class NoProbePlanner:
    """A planner that never proposes a probe (the budget is exhausted at once)."""

    def next_probe(
        self, table: EvidenceTable, posterior: PosteriorReport, budget: BudgetView, rung: Rung
    ) -> ProbeChoice | None:
        return None


class NoRowsProbeRunner:
    """A probe runner that returns no rows (for wiring tests and dry runs)."""

    def run(
        self, probe_id: ProbeId, contact_id: str, tick: int, args: Mapping[str, Any]
    ) -> Sequence[EvidenceRow]:
        return ()


# --- states, events, transitions -------------------------------------------------------


class InvestigationState(StrEnum):
    """Investigation states. Names avoid ``LadderState`` members (``ERRORED`` is the
    WO's FAILED, renamed so it never reads as ``LadderState.FAILED``)."""

    SCORING = "SCORING"
    DECIDING = "DECIDING"
    PROBING = "PROBING"
    STOPPED = "STOPPED"
    ERRORED = "ERRORED"


class InvestigationEvent(StrEnum):
    """SPEC-LADDER section 2 event names, plus ``probe_selected`` inside the loop."""

    POSTERIOR_UPDATED = "posterior_updated"
    STOP_RULE_FIRED = "stop_rule_fired"
    PROBE_SELECTED = "probe_selected"
    PROBE_RESULT = "probe_result"
    ERROR = "error"


INITIAL_STATE: Final = InvestigationState.SCORING
TERMINAL_INVESTIGATION_STATES: Final[frozenset[InvestigationState]] = frozenset(
    {InvestigationState.STOPPED, InvestigationState.ERRORED}
)

_S = InvestigationState
_E = InvestigationEvent
TRANSITIONS: Final[Mapping[tuple[InvestigationState, InvestigationEvent], InvestigationState]] = (
    MappingProxyType(
        {
            (_S.SCORING, _E.POSTERIOR_UPDATED): _S.DECIDING,
            (_S.DECIDING, _E.STOP_RULE_FIRED): _S.STOPPED,
            (_S.DECIDING, _E.PROBE_SELECTED): _S.PROBING,
            (_S.PROBING, _E.PROBE_RESULT): _S.SCORING,
            (_S.SCORING, _E.ERROR): _S.ERRORED,
            (_S.DECIDING, _E.ERROR): _S.ERRORED,
            (_S.PROBING, _E.ERROR): _S.ERRORED,
        }
    )
)
"""Exhaustive: any other (state, event) pair raises ``IllegalTransition``."""


class IllegalTransition(RuntimeError):
    """A (state, event) pair outside ``TRANSITIONS``."""


def next_state(state: InvestigationState, event: InvestigationEvent) -> InvestigationState:
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise IllegalTransition(f"{state.value} x {event.value} is not a transition") from None


def action_key(contact_id: str, seq: int, event: InvestigationEvent | str, payload: Any) -> str:
    """``sha256(canonical_json([contact_id, seq, event, payload]))``; for a probe action
    the payload is the probe idempotency key (``soundings.probe_keys``)."""
    body = [str(contact_id), int(seq), str(event), payload]
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def event_label(event: InvestigationEvent, arg: str | None) -> str:
    """``stop_rule_fired(HANDOFF)``, ``probe_selected(drift_check)``, ``posterior_updated``."""
    return event.value if arg is None else f"{event.value}({arg})"


# --- recommendation --------------------------------------------------------------------

RECOMMENDED_RUNG: Final[Mapping[StopOutcome, Rung]] = MappingProxyType(
    {
        StopOutcome.CLEARED_AT_ASK: Rung.ASK,
        StopOutcome.RESOLVED_BENIGN: Rung.ASK,
        StopOutcome.HAZARD_NOTICE: Rung.ASK,
        StopOutcome.LOST_VEHICLE: Rung.ASK,
        StopOutcome.HANDOFF: Rung.HAND_TO_PERSON,
    }
)
"""The investigation never recommends ``slow`` or ``hold``; the ladder decides whether
standing orders apply first (Phase 4)."""


_COMPUTED: Final = ("recommended_rung", "recommendation_hash")


class Recommendation(StrictModel):
    """The frozen end of an investigation. Nothing downstream may edit it.

    ``recommended_rung`` is computed from ``outcome`` through ``RECOMMENDED_RUNG``; it is
    not a stored field, so it cannot be passed, assigned or copied in.
    ``recommendation_hash`` is ``stable_hash`` of every other field; WO-043 puts it in
    the handoff payload so each SITREP ties to the recommendation it rests on.
    """

    contact_id: str
    outcome: StopOutcome
    rule_id: StopRuleId
    posterior: dict[str, float]
    entropy_bits: NonNegFloat
    threat_mass: Probability
    benign_mass: Probability
    top: Hypothesis
    uncertain: bool
    question_for_human: FreeText | None = None
    evidence_row_ids: tuple[str, ...]
    probes_used: NonNegInt
    interruptions: NonNegInt
    tick: NonNegInt
    label: str = STRESS_TEST_LABEL

    @model_validator(mode="wrap")
    @classmethod
    def _check_computed(
        cls,
        data: Any,
        handler: ModelWrapValidatorHandler[Recommendation],
        info: ValidationInfo,
    ) -> Recommendation:
        """A serialized recommendation carries its computed fields; accept them only if
        they equal what this model computes (a rung can never be supplied)."""
        given: dict[str, Any] = {}
        if isinstance(data, dict):
            given = {k: data[k] for k in _COMPUTED if k in data}
            data = {k: v for k, v in data.items() if k not in _COMPUTED}
            if info.mode == "json":  # JSON arrays arrive as lists once re-entered as a dict
                data = from_file_data(data)
        rec = handler(data)
        for name, value in given.items():
            if str(value) != str(getattr(rec, name)):
                raise ValueError(f"{name} does not match the recommendation's content")
        return rec

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recommended_rung(self) -> Rung:
        return RECOMMENDED_RUNG[self.outcome]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recommendation_hash(self) -> str:
        fields = self.model_dump(mode="json", exclude={"recommendation_hash"})
        return stable_hash(fields)


# --- checkpoints -----------------------------------------------------------------------


class CheckpointChainError(ValueError):
    """Checkpoints that do not link, hash or replay."""


class InvestigationCheckpoint(StrictModel):
    """One per transition, hash-chained per contact (the WO-040 pattern).

    ``evidence_rows`` are the rows this transition appended (only ``probe_result`` adds
    any), so ``resume`` rebuilds the table without re-running a probe. ``probe_key`` is
    the WO-031 probe idempotency key of the probe action, when there is one. The
    ``stop_rule_fired`` checkpoint carries the frozen ``Recommendation``.
    """

    seq: NonNegInt
    contact_id: str
    tick: NonNegInt
    from_state: InvestigationState
    event: InvestigationEvent
    event_arg: str | None = None
    to_state: InvestigationState
    action_key: str
    probe_key: str | None = None
    posterior: dict[str, float]
    evidence_rows: tuple[EvidenceRow, ...] = ()
    ledger: InvestigationLedger
    recommendation: Recommendation | None = None
    prev_hash: str
    hash: str

    @staticmethod
    def compute_hash(fields: Mapping[str, Any]) -> str:
        return stable_hash({k: v for k, v in fields.items() if k != "hash"})

    @classmethod
    def build(cls, **fields: Any) -> InvestigationCheckpoint:
        draft = cls.model_validate({**fields, "hash": GENESIS_HASH})
        return draft.model_copy(update={"hash": cls.compute_hash(draft.model_dump(mode="json"))})

    def verify_hash(self) -> bool:
        return self.hash == self.compute_hash(self.model_dump(mode="json"))

    @property
    def label(self) -> str:
        return event_label(self.event, self.event_arg)


def verify_checkpoint_chain(checkpoints: Sequence[InvestigationCheckpoint]) -> None:
    """Raise ``CheckpointChainError`` unless seq runs 0..n-1, hashes recompute, links hold
    and every step is a ``TRANSITIONS`` row."""
    prev = GENESIS_HASH
    state = INITIAL_STATE
    for i, cp in enumerate(checkpoints):
        if cp.seq != i:
            raise CheckpointChainError(f"checkpoint seq {cp.seq} at position {i}")
        if cp.prev_hash != prev:
            raise CheckpointChainError(f"checkpoint {i}: prev_hash does not link")
        if not cp.verify_hash():
            raise CheckpointChainError(f"checkpoint {i}: hash does not match its content")
        if cp.contact_id != checkpoints[0].contact_id:
            raise CheckpointChainError(f"checkpoint {i}: another contact")
        if cp.from_state is not state or TRANSITIONS.get((cp.from_state, cp.event)) is not (
            cp.to_state
        ):
            raise CheckpointChainError(f"checkpoint {i}: not a transition from {state.value}")
        prev, state = cp.hash, cp.to_state


def checkpoint_line(cp: InvestigationCheckpoint) -> str:
    """One canonical-JSON line (6-dp floats)."""
    return canonical_json(cp)


def load_checkpoints(path: str | Path) -> tuple[InvestigationCheckpoint, ...]:
    """Read a JSONL checkpoint file written by ``InvestigationMachine``."""
    p = Path(path)
    if not p.exists():
        return ()
    lines = p.read_text(encoding="utf-8").splitlines()
    return tuple(InvestigationCheckpoint.model_validate_json(ln) for ln in lines if ln.strip())


# --- the machine -----------------------------------------------------------------------


class ProbeCall(StrictModel):
    """One probe the machine executed (``cached`` when the key had already run)."""

    contact_id: str
    probe_id: ProbeId
    tick: NonNegInt
    idempotency_key: str
    cost_usd: NonNegFloat
    cached: bool
    source: str


class InvestigationResult(StrictModel):
    contact_id: str
    state: InvestigationState
    recommendation: Recommendation | None
    checkpoints: tuple[InvestigationCheckpoint, ...]
    probe_calls: tuple[ProbeCall, ...]
    ledger: InvestigationLedger
    tick: NonNegInt
    error: str | None = None

    @property
    def events(self) -> tuple[str, ...]:
        """The transition log as event labels, in order."""
        return tuple(cp.label for cp in self.checkpoints)

    def transition_log_lines(self) -> list[str]:
        """Canonical JSONL of the transitions (no evidence payloads)."""
        return [
            canonical_json(
                {
                    "seq": cp.seq,
                    "contact_id": cp.contact_id,
                    "tick": cp.tick,
                    "from": cp.from_state,
                    "event": cp.label,
                    "to": cp.to_state,
                    "action_key": cp.action_key,
                    "hash": cp.hash,
                }
            )
            for cp in self.checkpoints
        ]


def latency_ticks(latency_s: float, tick_s: float) -> int:
    """``ceil(latency_s / tick_s)`` (9-dp guard against float noise)."""
    if tick_s <= 0:
        raise ValueError("tick_s must be > 0")
    return int(math.ceil(round(latency_s / tick_s, 9)))


class InvestigationMachine:
    """Score, decide, probe, rescore for one contact at rung ``ask``.

    ``table`` is the contact's evidence table at the start of the investigation; probe
    rows are appended to it. ``tick`` is the tick the investigation starts at and
    ``max_tick`` the last tick the scenario has; a probe that would land after it means
    the budget is exhausted. ``ledger`` is the starting spend (one paid probe when the
    ask-rung hail ran). Every transition writes an ``InvestigationCheckpoint`` to
    ``checkpoints`` and, when ``checkpoint_path`` is given, appends it to that JSONL.
    """

    def __init__(
        self,
        contact_id: str,
        table: EvidenceTable,
        *,
        planner: Planner,
        probe_runner: ProbeRunner,
        tables: LikelihoodTables,
        prior_cfg: PriorConfig,
        stopping: StoppingConfig,
        budgets: BudgetLimits,
        tick: int,
        tick_s: float,
        max_tick: int,
        writer: QuestionWriter | None = None,
        ledger: InvestigationLedger | None = None,
        rung: Rung = Rung.ASK,
        rungs: Sequence[Rung] | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        if tick < 0 or max_tick < tick:
            raise ValueError("need 0 <= tick <= max_tick")
        self.contact_id = contact_id
        self.table = table
        self.planner = planner
        self.probe_runner = probe_runner
        self.tables = tables
        self.prior_cfg = prior_cfg
        self.stopping = stopping
        self.budgets = budgets
        self.tick_s = float(tick_s)
        self.max_tick = max_tick
        self.writer: QuestionWriter = writer if writer is not None else TemplateQuestionWriter()
        self.rung = rung
        self.rung_order = tuple(rungs) if rungs is not None else load_ladder_config().rungs
        self.checkpoint_path = None if checkpoint_path is None else Path(checkpoint_path)
        self._start_tick = tick
        self._start_ledger = ledger if ledger is not None else InvestigationLedger()
        self._reset()

    def _reset(self) -> None:
        self.state = INITIAL_STATE
        self.tick = self._start_tick
        self.ledger = self._start_ledger
        self.checkpoints: list[InvestigationCheckpoint] = []
        self.probe_calls: list[ProbeCall] = []
        self.executed: dict[str, tuple[EvidenceRow, ...]] = {}
        self._report: PosteriorReport | None = None
        self._pending: ProbeChoice | None = None
        self._recommendation: Recommendation | None = None
        self._error: str | None = None

    # --- bookkeeping -------------------------------------------------------------------

    def _posterior_cells(self) -> dict[str, float]:
        return {} if self._report is None else self._report.posterior.rounded()

    def _transition(
        self,
        event: InvestigationEvent,
        *,
        arg: str | None = None,
        payload: Any = None,
        probe_key: str | None = None,
        rows: Sequence[EvidenceRow] = (),
        recommendation: Recommendation | None = None,
    ) -> InvestigationCheckpoint:
        to_state = next_state(self.state, event)
        seq = len(self.checkpoints)
        cp = InvestigationCheckpoint.build(
            seq=seq,
            contact_id=self.contact_id,
            tick=self.tick,
            from_state=self.state,
            event=event,
            event_arg=arg,
            to_state=to_state,
            action_key=action_key(self.contact_id, seq, event, payload),
            probe_key=probe_key,
            posterior=self._posterior_cells(),
            evidence_rows=tuple(rows),
            ledger=self.ledger,
            recommendation=recommendation,
            prev_hash=self.checkpoints[-1].hash if self.checkpoints else GENESIS_HASH,
        )
        self.checkpoints.append(cp)
        if self.checkpoint_path is not None:
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            with self.checkpoint_path.open("a", encoding="utf-8") as fh:
                fh.write(checkpoint_line(cp) + "\n")
        self.state = to_state
        return cp

    def _append_rows(self, rows: Sequence[EvidenceRow]) -> tuple[EvidenceRow, ...]:
        added: list[EvidenceRow] = []
        for row in rows:
            if row.contact_id != self.contact_id:
                raise ProbeError(f"probe row for {row.contact_id}, not {self.contact_id}")
            if row.row_id not in self.table:
                self.table.append(row)
                added.append(row)
        return tuple(added)

    def _probe_key(self, choice: ProbeChoice, tick: int) -> str:
        bucket = tick_bucket(tick, self.tick_s)
        return idempotency_key(self.contact_id, choice.probe_id.value, choice.args, bucket)

    # --- steps -------------------------------------------------------------------------

    def _fail(self, where: str, exc: Exception, payload: Any) -> None:
        self._error = f"{where}: {type(exc).__name__}"
        self._transition(InvestigationEvent.ERROR, arg=where, payload=payload)

    def _score(self) -> None:
        try:
            self._report = score_contact(self.table, self.contact_id, self.tables, self.prior_cfg)
        except ValueError as exc:  # a table or evidence error: recorded, never guessed past
            self._fail("score", exc, None)
            return
        self._transition(
            InvestigationEvent.POSTERIOR_UPDATED,
            payload=self._report.posterior.rounded(),
        )

    def _view(self) -> BudgetView:
        return BudgetView(
            limits=self.budgets,
            ledger=self.ledger,
            tick=self.tick,
            ticks_left=self.max_tick - self.tick,
        )

    def _allowed(self, choice: ProbeChoice) -> bool:
        order = {r: i for i, r in enumerate(self.rung_order)}
        if order[choice.rung_required] > order[self.rung]:
            return False
        if not self.ledger.allows(choice, self.budgets):
            return False
        return self.tick + latency_ticks(choice.latency_s, self.tick_s) <= self.max_tick

    def _stop(self, inputs: StopInputs, decision: StopDecision) -> None:
        assert self._report is not None
        report = self._report
        post = report.posterior
        question = self.writer.question_for_human(report, decision, self.table)
        if question is not None and find_denylisted(question):
            # Writer text is display only; denylisted text is replaced, never shown.
            question = TemplateQuestionWriter().question_for_human(report, decision, self.table)
        self._recommendation = Recommendation(
            contact_id=self.contact_id,
            outcome=decision.outcome,
            rule_id=decision.rule_id,
            posterior=post.rounded(),
            entropy_bits=round6(post.entropy()),
            threat_mass=round6(post.threat_mass()),
            benign_mass=round6(post.benign_mass()),
            top=post.top(),
            uncertain=decision.uncertain,
            question_for_human=question,
            evidence_row_ids=tuple(dict.fromkeys((*report.used_row_ids, *inputs.row_ids))),
            probes_used=self.ledger.probes_used,
            interruptions=self.ledger.interruptions,
            tick=self.tick,
        )
        self._transition(
            InvestigationEvent.STOP_RULE_FIRED,
            arg=decision.outcome.value,
            payload=[decision.outcome.value, decision.rule_id.value, decision.uncertain],
            recommendation=self._recommendation,
        )

    def _decide(self) -> None:
        assert self._report is not None
        inputs, decision = evaluate_stop_at(
            self._report, self.table, self.stopping, tick=self.tick, tick_s=self.tick_s
        )
        try:
            if decision is not None:
                self._stop(inputs, decision)
                return
            choice = self.planner.next_probe(self.table, self._report, self._view(), self.rung)
            if choice is None or not self._allowed(choice):
                inputs = inputs.model_copy(update={"budget_exhausted": True})
                final = evaluate_stop(inputs, self.stopping)
                assert final is not None  # C_BUDGET always fires on an exhausted budget
                self._stop(inputs, final)
                return
        except InvestigationError as exc:
            self._recommendation = None
            self._fail("decide", exc, None)
            return
        key = self._probe_key(choice, self.tick)
        self._pending = choice
        self._transition(
            InvestigationEvent.PROBE_SELECTED,
            arg=choice.probe_id.value,
            payload=key,
            probe_key=key,
        )

    def _probe(self) -> None:
        choice = self._pending
        assert choice is not None
        key = self.checkpoints[-1].probe_key
        assert key is not None
        landed = self.tick + latency_ticks(choice.latency_s, self.tick_s)
        cached = key in self.executed
        if cached:
            rows: tuple[EvidenceRow, ...] = ()
        else:
            try:
                produced = tuple(
                    self.probe_runner.run(choice.probe_id, self.contact_id, landed, choice.args)
                )
                rows = self._append_rows(produced)
            except InvestigationError as exc:
                self._fail(choice.probe_id.value, exc, key)
                return
            self.executed[key] = rows
        self.probe_calls.append(
            ProbeCall(
                contact_id=self.contact_id,
                probe_id=choice.probe_id,
                tick=landed,
                idempotency_key=key,
                cost_usd=round6(choice.cost_usd),
                cached=cached,
                source=choice.source,
            )
        )
        self.tick = landed
        self.ledger = self.ledger.after(choice)
        self._pending = None
        self._transition(
            InvestigationEvent.PROBE_RESULT,
            arg=choice.probe_id.value,
            payload=[key, [r.row_id for r in rows]],
            probe_key=key,
            rows=rows,
        )

    # --- driving -----------------------------------------------------------------------

    def _bound(self) -> int:
        # Each probe round is three transitions; one more round scores and stops.
        return 3 * (self.budgets.probes_per_contact + 1) + 2

    def run(self) -> InvestigationResult:
        """Run (or continue) until ``STOPPED`` or ``ERRORED``; bounded by the budgets."""
        while self.state not in TERMINAL_INVESTIGATION_STATES:
            if len(self.checkpoints) > self._bound():
                raise IllegalTransition("investigation exceeded its transition bound")
            if self.state is InvestigationState.SCORING:
                self._score()
            elif self.state is InvestigationState.DECIDING:
                self._decide()
            else:
                self._probe()
        return self.result()

    def result(self) -> InvestigationResult:
        return InvestigationResult(
            contact_id=self.contact_id,
            state=self.state,
            recommendation=self._recommendation,
            checkpoints=tuple(self.checkpoints),
            probe_calls=tuple(self.probe_calls),
            ledger=self.ledger,
            tick=self.tick,
            error=self._error,
        )

    def resume(self, checkpoints: Sequence[InvestigationCheckpoint]) -> InvestigationResult:
        """Replay ``checkpoints`` (rebuilding the table from their rows, never re-running
        a probe), then continue the run from the last state."""
        verify_checkpoint_chain(checkpoints)
        if checkpoints and checkpoints[0].contact_id != self.contact_id:
            raise CheckpointChainError("checkpoints belong to another contact")
        self._reset()
        if self.checkpoint_path is not None:
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            self.checkpoint_path.write_text(
                "".join(checkpoint_line(cp) + "\n" for cp in checkpoints), encoding="utf-8"
            )
        for cp in checkpoints:
            for row in cp.evidence_rows:
                if row.row_id not in self.table:
                    self.table.append(row)
            if cp.event is InvestigationEvent.PROBE_RESULT and cp.probe_key is not None:
                self.executed[cp.probe_key] = cp.evidence_rows
            if cp.event is InvestigationEvent.ERROR:
                self._error = f"{cp.event_arg}: resumed after error"
            self.checkpoints.append(cp)
            self.state = cp.to_state
            self.tick = cp.tick
            self.ledger = cp.ledger
            if cp.recommendation is not None:
                self._recommendation = cp.recommendation
        if self.state not in TERMINAL_INVESTIGATION_STATES and self.checkpoints:
            self._report = score_contact(self.table, self.contact_id, self.tables, self.prior_cfg)
            if self.state is InvestigationState.PROBING:
                self._pending = self._replay_choice()
        return self.run()

    def _replay_choice(self) -> ProbeChoice:
        """Ask the planner again for the pending probe and check it is the same action."""
        assert self._report is not None
        selected = self.checkpoints[-1]
        choice = self.planner.next_probe(self.table, self._report, self._view(), self.rung)
        if choice is None or self._probe_key(choice, self.tick) != selected.probe_key:
            raise CheckpointChainError("planner no longer selects the checkpointed probe")
        return choice


__all__ = [
    "GENESIS_HASH",
    "INITIAL_STATE",
    "RECOMMENDED_RUNG",
    "STOP_PRECEDENCE",
    "TERMINAL_INVESTIGATION_STATES",
    "TRANSITIONS",
    "BudgetLimits",
    "BudgetView",
    "CheckpointChainError",
    "IllegalTransition",
    "InvestigationError",
    "InvestigationCheckpoint",
    "InvestigationEvent",
    "InvestigationLedger",
    "InvestigationMachine",
    "InvestigationResult",
    "InvestigationState",
    "NoProbePlanner",
    "NoRowsProbeRunner",
    "Planner",
    "ProbeCall",
    "ProbeChoice",
    "ProbeError",
    "ProbeRunner",
    "QuestionWriter",
    "Recommendation",
    "StopDecision",
    "StopInputs",
    "StopOutcome",
    "StopRuleId",
    "StoppingConfig",
    "TemplateQuestionWriter",
    "action_key",
    "checkpoint_line",
    "evaluate_stop",
    "evaluate_stop_at",
    "event_label",
    "last_heard_tick",
    "latency_ticks",
    "load_budgets",
    "load_checkpoints",
    "load_stopping_config",
    "next_state",
    "stop_inputs_from",
    "verify_checkpoint_chain",
]
