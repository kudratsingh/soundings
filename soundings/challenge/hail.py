"""Simulated acoustic challenge-response for this demonstration. Replies come from
scenario ground truth. No real modem protocol, authentication, or signal processing is
modelled.

The ``acoustic_hail`` probe of docs/SPEC-INVESTIGATION.md section 5 (rung ask, $0.01,
20 s). ``HailSimulator.send`` is a simulated physical exchange with the contact. It asks
the world (``soundings.ports.WorldRun.hail_responder``, answered inside the simulator)
how the thing behind an observed id would respond, and reads that contact's scenario
registry spec (``modem_id``, ``plan_id``). It reads no ground-truth field itself, and
nothing it returns names a hypothesis: the evidence rows carry only what a harbor would
hear (did a reply come, which modem id, is that modem on the known-operator table).

Reply rule, evaluated at ``sent_tick``; the reply lands at ``sent_tick + latency_ticks``:

1. An observed id the world has no responder for (no source, or a sensor false alarm)
   does not reply.
2. A contact replies if ``HailResponder.answers_at(sent_tick)``: it is set up to reply
   and has not gone quiet (the ``contact_goes_quiet`` toggle is honoured, because the
   simulator answers from the run, not the scenario file).
3. With ``reply_loss_prob > 0`` a reply is lost with that probability, drawn from
   ``numpy.random.default_rng([seed, int(stable_hash([contact_id, sent_tick])[:8], 16)])``.
   There is no shared generator: the draw depends on seed, contact and tick only.
4. The modem id is the contact's ``registry.modem_id``, else the ``modem_id`` of the plan
   named by ``registry.plan_id``, else None.
5. Bucket: a reply with a modem id matching ``modem_id_pattern`` is ``valid``; a reply with
   no modem id or a malformed one is ``invalid``; no reply is ``timeout`` when the caller
   says this lineage answered before (``previously_replied``), else ``none``. Filing status
   never enters (DECISIONS 2026-10-16 23:05): the matcher (WO-022) judges filing.
6. ``operator_known`` is whether the modem id is on the ``KnownOperatorTable``.

The hail reads the world and never changes it: ``send`` takes the run read-only and
caches by the shared SPEC section 5 idempotency key (``soundings.probe_keys``), so a second
send in the same 30 s bucket returns the cached result and draws nothing.

Evidence rows (``hail_evidence_rows``) are four ``kind=fact`` rows tagged
``probe_id=acoustic_hail`` and ``source="probe:acoustic_hail"`` at the reply tick:
``hail_sent_at`` (seconds), ``hail_reply``, ``modem_id`` (``"unknown"`` without a reply)
and ``operator_known``. Confidence is 1.0 for a reply. For a non-reply it is the constant
``NO_REPLY_CONFIDENCE`` = 0.9: a stress-test setting for this demonstration (silence may
be a lost reply rather than a silent contact), not a measured detection figure.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import yaml
from pydantic import StrictBool, StrictStr, StringConstraints, model_validator

from soundings.canonical import round6, stable_hash
from soundings.ports import HailResponder, WorldRun
from soundings.probe_keys import idempotency_key, tick_bucket
from soundings.schemas._base import (
    CONFIG_DIR,
    NonNegFloat,
    NonNegInt,
    PositiveFloat,
    Probability,
    RefId,
    StrictModel,
    from_file_data,
)
from soundings.schemas.denylist import FreeText
from soundings.schemas.enums import EvidenceKind, FeatureName, HailReply, ProbeId, Unit
from soundings.schemas.evidence import UNKNOWN, ContactId, EvidenceRow, EvidenceValue
from soundings.schemas.scenario import Scenario, ScenarioContact

HONESTY_NOTE = (
    "Simulated acoustic challenge-response for this demonstration. Replies come from "
    "scenario ground truth. No real modem protocol, authentication, or signal processing "
    "is modelled."
)
"""The SPEC-INVESTIGATION section 8 honesty line, verbatim from this module's docstring."""

CHALLENGE_CONFIG_PATH: Path = CONFIG_DIR / "challenge.yaml"
KNOWN_OPERATORS_PATH: Path = CONFIG_DIR / "known_operators.yaml"

HAIL_PROBE = ProbeId.ACOUSTIC_HAIL
HAIL_SOURCE = "probe:acoustic_hail"
REPLY_CONFIDENCE = 1.0
NO_REPLY_CONFIDENCE = 0.9
"""Stress-test confidence on the rows of a hail that got no reply (see module docstring)."""

IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def _read_yaml(path: str | Path) -> Any:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


# --- config ------------------------------------------------------------------------------


class HailConfig(StrictModel):
    """The ``hail:`` block of ``config/challenge.yaml``."""

    latency_s: PositiveFloat = 20.0
    cost_usd: NonNegFloat = 0.01
    reply_loss_prob: Probability = 0.0
    modem_id_pattern: StrictStr = "^MOD-[0-9]{4,6}$"

    @model_validator(mode="after")
    def _pattern_compiles(self) -> HailConfig:
        try:
            re.compile(self.modem_id_pattern)
        except re.error as exc:
            raise ValueError(f"modem_id_pattern does not compile: {exc}") from exc
        return self


def load_hail_config(path: str | Path = CHALLENGE_CONFIG_PATH) -> HailConfig:
    """Load the ``hail:`` block of ``config/challenge.yaml``."""
    doc = _read_yaml(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("hail"), dict):
        raise ValueError(f"{path}: expected a top-level 'hail:' mapping")
    return HailConfig.model_validate(from_file_data(doc["hail"]))


def hail_latency_ticks(latency_s: float, tick_s: float) -> int:
    """``ceil(latency_s / tick_s)``: 20 s is 10 ticks at ``tick_s=2`` and 7 at ``tick_s=3``."""
    if latency_s <= 0 or tick_s <= 0:
        raise ValueError("latency_s and tick_s must be > 0")
    return int(math.ceil(round(latency_s / tick_s, 9)))


# --- known operators ---------------------------------------------------------------------


class KnownOperatorEntry(StrictModel):
    modem_id: RefId
    operator: FreeText
    note: FreeText = ""


class KnownOperatorTable(StrictModel):
    """Modem ids the harbor recognises without a filed plan, sorted by ``modem_id``."""

    entries: tuple[KnownOperatorEntry, ...] = ()

    @model_validator(mode="after")
    def _unique_sorted(self) -> KnownOperatorTable:
        ids = [e.modem_id for e in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("known operator modem ids must be unique")
        if ids != sorted(ids):
            raise ValueError("known operator entries must be sorted by modem_id")
        return self

    @cached_property
    def _by_modem(self) -> dict[str, KnownOperatorEntry]:
        return {e.modem_id: e for e in self.entries}

    def lookup(self, modem_id: str | None) -> KnownOperatorEntry | None:
        """The entry for ``modem_id``, or None (also for None)."""
        if modem_id is None:
            return None
        return self._by_modem.get(modem_id)

    @classmethod
    def from_entries(cls, entries: Iterable[KnownOperatorEntry]) -> KnownOperatorTable:
        """Merge entries; a repeated modem id must name the same operator (first note kept)."""
        merged: dict[str, KnownOperatorEntry] = {}
        for entry in entries:
            prior = merged.get(entry.modem_id)
            if prior is None:
                merged[entry.modem_id] = entry
            elif prior.operator != entry.operator:
                raise ValueError(
                    f"modem {entry.modem_id} is listed for two operators: "
                    f"{prior.operator!r} and {entry.operator!r}"
                )
        return cls(entries=tuple(merged[k] for k in sorted(merged)))


def load_known_operators(
    path: str | Path = KNOWN_OPERATORS_PATH, scenario: Scenario | None = None
) -> KnownOperatorTable:
    """The union of ``config/known_operators.yaml`` and ``scenario.known_operators``.

    A modem id listed twice with different operators raises ``ValueError``.
    """
    doc = _read_yaml(path)
    if not isinstance(doc, dict) or "known_operators" not in doc:
        raise ValueError(f"{path}: expected a top-level 'known_operators:' list")
    raw = doc["known_operators"] or []
    entries = [KnownOperatorEntry.model_validate(from_file_data(item)) for item in raw]
    if scenario is not None:
        entries.extend(
            KnownOperatorEntry(
                modem_id=k.modem_id, operator=k.operator, note=f"scenario {scenario.id}"
            )
            for k in scenario.known_operators
        )
    return KnownOperatorTable.from_entries(entries)


# --- the hail ----------------------------------------------------------------------------


class HailResult(StrictModel):
    """What one hail heard. Holds no ground-truth label."""

    contact_id: ContactId
    sent_tick: NonNegInt
    reply_tick: NonNegInt
    replied: StrictBool
    hail_reply: HailReply
    modem_id: RefId | None
    operator_known: StrictBool
    operator: FreeText | None
    idempotency_key: IdempotencyKey
    cost_usd: NonNegFloat

    @model_validator(mode="after")
    def _consistent(self) -> HailResult:
        if self.reply_tick < self.sent_tick:
            raise ValueError("reply_tick must be >= sent_tick")
        reply_buckets = (HailReply.VALID, HailReply.INVALID)
        if self.replied != (self.hail_reply in reply_buckets):
            raise ValueError("replied must be True exactly for valid or invalid")
        if not self.replied and self.modem_id is not None:
            raise ValueError("a hail without a reply has no modem id")
        if self.operator_known != (self.operator is not None):
            raise ValueError("operator is set exactly when operator_known")
        return self


def loss_rng(seed: int, contact_id: str, sent_tick: int) -> np.random.Generator:
    """The reply-loss generator for one (seed, contact, tick); nothing else draws from it."""
    salt = int(stable_hash([contact_id, sent_tick])[:8], 16)
    return np.random.default_rng([int(seed), salt])


class HailSimulator:
    """Hails the observed contacts of one run (any ``soundings.ports.WorldRun``).

    Deterministic in (scenario, seed, contact, tick); the run is only read.
    """

    def __init__(
        self,
        scenario: Scenario,
        run: WorldRun,
        *,
        seed: int,
        known: KnownOperatorTable | None = None,
        cfg: HailConfig | None = None,
    ) -> None:
        if run.scenario_id != scenario.id:
            raise ValueError(f"run is for {run.scenario_id}, scenario is {scenario.id}")
        self._scenario = scenario
        self._run = run
        self._seed = int(seed)
        self._known = known if known is not None else load_known_operators(scenario=scenario)
        self._cfg = cfg if cfg is not None else load_hail_config()
        self._pattern = re.compile(self._cfg.modem_id_pattern)
        self._contacts: dict[str, ScenarioContact] = {c.id: c for c in scenario.contacts}
        self._cache: dict[str, HailResult] = {}
        self.tick_s: float = float(run.tick_s)
        self.latency_ticks: int = hail_latency_ticks(self._cfg.latency_s, self.tick_s)

    @property
    def cfg(self) -> HailConfig:
        return self._cfg

    @property
    def known(self) -> KnownOperatorTable:
        return self._known

    def key(self, contact_id: str, tick: int) -> str:
        """The SPEC section 5 idempotency key of a hail to ``contact_id`` at ``tick``."""
        return idempotency_key(contact_id, HAIL_PROBE.value, {}, tick_bucket(tick, self.tick_s))

    def _modem_for(self, responder: HailResponder) -> str | None:
        contact = self._contacts.get(responder.registry_contact_id)
        if contact is None:
            return None
        reg = contact.registry
        if reg.modem_id is not None:
            return reg.modem_id
        if reg.plan_id is not None:
            return self._scenario.plan(reg.plan_id).modem_id
        return None

    def send(self, contact_id: str, tick: int, *, previously_replied: bool = False) -> HailResult:
        """Hail ``contact_id`` at ``tick``. A repeat in the same tick bucket returns the cache."""
        if tick < 0:
            raise ValueError("tick must be >= 0")
        key = self.key(contact_id, tick)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        responder = self._run.hail_responder(contact_id)
        replied = responder is not None and responder.answers_at(tick)
        if replied and self._cfg.reply_loss_prob > 0.0:
            draw = float(loss_rng(self._seed, contact_id, tick).random())
            replied = draw >= self._cfg.reply_loss_prob
        modem_id = self._modem_for(responder) if replied and responder is not None else None
        if replied:
            well_formed = modem_id is not None and self._pattern.fullmatch(modem_id) is not None
            bucket = HailReply.VALID if well_formed else HailReply.INVALID
        else:
            bucket = HailReply.TIMEOUT if previously_replied else HailReply.NONE
        entry = self._known.lookup(modem_id)
        result = HailResult(
            contact_id=contact_id,
            sent_tick=tick,
            reply_tick=tick + self.latency_ticks,
            replied=replied,
            hail_reply=bucket,
            modem_id=modem_id,
            operator_known=entry is not None,
            operator=entry.operator if entry is not None else None,
            idempotency_key=key,
            cost_usd=self._cfg.cost_usd,
        )
        self._cache[key] = result
        return result


def hail_evidence_rows(result: HailResult, *, tick_s: float) -> list[EvidenceRow]:
    """The four fact rows a hail writes, in this order.

    ``hail_sent_at``, ``hail_reply``, ``modem_id``, ``operator_known``; the caller appends
    them through ``EvidenceTable.append``.
    """
    confidence = REPLY_CONFIDENCE if result.replied else NO_REPLY_CONFIDENCE
    modem_value: EvidenceValue = result.modem_id if result.modem_id is not None else UNKNOWN
    cells: tuple[tuple[FeatureName, EvidenceValue, Unit], ...] = (
        (FeatureName.HAIL_SENT_AT, round6(result.sent_tick * float(tick_s)), Unit.S),
        (FeatureName.HAIL_REPLY, result.hail_reply.value, Unit.CATEGORY),
        (FeatureName.MODEM_ID, modem_value, Unit.ID),
        (FeatureName.OPERATOR_KNOWN, result.operator_known, Unit.BOOL),
    )
    return [
        EvidenceRow(
            contact_id=result.contact_id,
            t=result.reply_tick,
            feature=feature,
            value=value,
            unit=unit,
            source=HAIL_SOURCE,
            confidence=confidence,
            kind=EvidenceKind.FACT,
            probe_id=HAIL_PROBE,
        )
        for feature, value, unit in cells
    ]


__all__ = [
    "CHALLENGE_CONFIG_PATH",
    "HAIL_SOURCE",
    "HONESTY_NOTE",
    "KNOWN_OPERATORS_PATH",
    "NO_REPLY_CONFIDENCE",
    "HailConfig",
    "HailResult",
    "HailSimulator",
    "KnownOperatorEntry",
    "KnownOperatorTable",
    "hail_evidence_rows",
    "hail_latency_ticks",
    "load_hail_config",
    "load_known_operators",
    "loss_rng",
]
