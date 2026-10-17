"""WO-021 unit tests: the simulated acoustic hail on hand-built fixtures (no sim run).

Exit tests: ``-k latency``, ``-k reply_table``, ``-k known_operators``, ``-k idempotent``,
``-k evidence_rows``, ``-k probe_keys``, ``-k config_agree``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

import soundings.challenge.hail as hail_mod
from soundings.canonical import canonical_json
from soundings.challenge.hail import (
    HONESTY_NOTE,
    NO_REPLY_CONFIDENCE,
    HailConfig,
    HailResult,
    HailSimulator,
    KnownOperatorEntry,
    KnownOperatorTable,
    hail_evidence_rows,
    hail_latency_ticks,
    load_hail_config,
    load_known_operators,
)
from soundings.probe_keys import TICK_BUCKET_S, idempotency_key, tick_bucket
from soundings.schemas import (
    UNKNOWN,
    EvidenceKind,
    EvidenceRow,
    FeatureName,
    HailReply,
    Hypothesis,
    ProbeId,
    Scenario,
)
from soundings.schemas._base import CONFIG_DIR, REPO_ROOT
from soundings.schemas.scenario import scenario_from_data
from soundings.sim.toggles import ToggleReport
from soundings.sim.types import ContactTruth, SimRun

TICK_S = 2.0
SILENT_AT = 300


# --- fixtures ----------------------------------------------------------------------------


def _contact(cid: str, registry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": cid,
        "start": {"lat": 33.74, "lon": -118.26, "depth_m": 5.0},
        "behavior": {"type": "stationary"},
        "registry": registry,
    }


def make_scenario(*, tick_s: float = TICK_S, known: tuple[dict[str, str], ...] = ()) -> Scenario:
    data = {
        "id": "S99",
        "name": "hail fixture",
        "seed": 5,
        "duration_ticks": 600,
        "tick_s": tick_s,
        "ground_truth": "FILED_ON_PLAN",
        "hour_of_day": 12,
        "registry_plans": [
            {
                "plan_id": "P-1",
                "operator": "Plan Op (demo)",
                "vehicle": "AUV (demo)",
                "modem_id": "MOD-4471",
                "area": "inner_basin_research",
                "window": {"start_tick": 0, "end_tick": 600},
                "contact": "sim-phone",
            }
        ],
        "known_operators": list(known),
        "contacts": [
            # modem comes from the plan; goes silent at SILENT_AT
            _contact(
                "gt-1",
                {
                    "filed": True,
                    "plan_id": "P-1",
                    "replies_to_hail": True,
                    "silent_after_tick": SILENT_AT,
                },
            ),
            # unfiled, well-formed modem id (S04 shape)
            _contact("gt-2", {"filed": False, "modem_id": "MOD-2290", "replies_to_hail": True}),
            # replies with no modem id
            _contact("gt-3", {"filed": False, "replies_to_hail": True}),
            # replies with a malformed modem id
            _contact("gt-4", {"filed": False, "modem_id": "BAD-77", "replies_to_hail": True}),
            # never replies
            _contact("gt-5", {"filed": False, "replies_to_hail": False}),
            # filed; its own modem id wins over the plan's
            _contact(
                "gt-6",
                {"filed": True, "plan_id": "P-1", "modem_id": "MOD-3310", "replies_to_hail": True},
            ),
        ],
        "expected": {"terminal_state": "CLEARED_AT_ASK"},
    }
    return scenario_from_data(data)


def _truth(gt: str, hyp: Hypothesis, replies: bool, silent: int | None = None) -> ContactTruth:
    return ContactTruth(gt_id=gt, hypothesis=hyp, replies_to_hail=replies, silent_after_tick=silent)


def make_run(scenario: Scenario) -> SimRun:
    truth = {
        "c-0001": _truth("gt-1", Hypothesis.FILED_OFF_PLAN, True, SILENT_AT),
        "c-0002": _truth("gt-2", Hypothesis.UNFILED_KNOWN_OPERATOR, True),
        "c-0003": _truth("gt-3", Hypothesis.UNAUTHORIZED_UUV, True),
        "c-0004": _truth("gt-4", Hypothesis.UNAUTHORIZED_UUV, True),
        "c-0005": _truth("gt-5", Hypothesis.UNAUTHORIZED_UUV, False),
        "c-0006": _truth("gt-6", Hypothesis.FILED_ON_PLAN, True),
        # a false alarm marked as replying: the fa- rule alone must silence it
        "c-0007": _truth("fa-0001", Hypothesis.BIOLOGIC, True),
        # a re-issued id for gt-1
        "c-0008": _truth("gt-1", Hypothesis.FILED_OFF_PLAN, True, SILENT_AT),
        # gt-2 after a contact_goes_quiet toggle at tick 50 (ContactTruth, not the file)
        "c-0009": _truth("gt-2", Hypothesis.UNFILED_KNOWN_OPERATOR, True, 50),
        # a surge contact with no scenario entry: replies but has no modem
        "c-0010": _truth("surge-1", Hypothesis.FILED_ON_PLAN, True),
    }
    return SimRun(
        scenario_id=scenario.id,
        seed=scenario.seed,
        tick_s=scenario.tick_s,
        n_ticks=scenario.duration_ticks,
        tracks={},
        detections=[],
        contact_truth=truth,
        truth_paths={},
        toggle_report=ToggleReport(),
    )


def known_table() -> KnownOperatorTable:
    return KnownOperatorTable.from_entries(
        [KnownOperatorEntry(modem_id="MOD-2290", operator="Port Survey Co (demo)")]
    )


def make_sim(
    *, cfg: HailConfig | None = None, known: KnownOperatorTable | None = None, seed: int = 5
) -> HailSimulator:
    scenario = make_scenario()
    return HailSimulator(
        scenario,
        make_run(scenario),
        seed=seed,
        known=known if known is not None else known_table(),
        cfg=cfg if cfg is not None else HailConfig(),
    )


# --- latency -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("latency_s", "tick_s", "expected"),
    [(20, 2, 10), (20, 3, 7), (20, 20, 1), (20, 2.0, 10), (20, 0.5, 40), (20, 30, 1)],
)
def test_latency_ticks_ceiling(latency_s: float, tick_s: float, expected: int) -> None:
    assert hail_latency_ticks(latency_s, tick_s) == expected


def test_latency_reply_tick_is_sent_plus_ten() -> None:
    sim = make_sim()
    assert sim.latency_ticks == 10
    for tick in (0, 20, 123, 299, 450):  # one tick per 30 s bucket
        result = sim.send("c-0002", tick)
        assert result.reply_tick == tick + 10


def test_latency_rejects_nonpositive() -> None:
    with pytest.raises(ValueError):
        hail_latency_ticks(0, 2)
    with pytest.raises(ValueError):
        hail_latency_ticks(20, 0)


# --- reply_table -------------------------------------------------------------------------

# (case, contact, sent_tick, previously_replied) -> (hail_reply, replied, modem_id, known)
REPLY_TABLE: list[tuple[str, str, int, bool, HailReply, bool, str | None, bool]] = [
    ("plan modem before silence", "c-0001", 10, False, HailReply.VALID, True, "MOD-4471", False),
    ("plan modem last tick", "c-0001", 299, False, HailReply.VALID, True, "MOD-4471", False),
    ("boundary sent == silent", "c-0001", SILENT_AT, False, HailReply.NONE, False, None, False),
    ("boundary, replied before", "c-0001", SILENT_AT, True, HailReply.TIMEOUT, False, None, False),
    ("after silence, replied before", "c-0001", 400, True, HailReply.TIMEOUT, False, None, False),
    ("after silence, first hail", "c-0001", 400, False, HailReply.NONE, False, None, False),
    ("prev flag ignored on reply", "c-0001", 10, True, HailReply.VALID, True, "MOD-4471", False),
    ("unfiled well-formed modem", "c-0002", 10, False, HailReply.VALID, True, "MOD-2290", True),
    ("reply without modem id", "c-0003", 10, False, HailReply.INVALID, True, None, False),
    ("reply with malformed modem", "c-0004", 10, False, HailReply.INVALID, True, "BAD-77", False),
    ("never replies", "c-0005", 10, False, HailReply.NONE, False, None, False),
    ("never replies, prev flag", "c-0005", 10, True, HailReply.TIMEOUT, False, None, False),
    ("contact modem beats plan", "c-0006", 10, False, HailReply.VALID, True, "MOD-3310", False),
    ("false alarm", "c-0007", 10, False, HailReply.NONE, False, None, False),
    ("false alarm, prev flag", "c-0007", 10, True, HailReply.TIMEOUT, False, None, False),
    ("re-issued id", "c-0008", 20, False, HailReply.VALID, True, "MOD-4471", False),
    ("quiet toggle before", "c-0009", 49, False, HailReply.VALID, True, "MOD-2290", True),
    ("quiet toggle at tick", "c-0009", 50, False, HailReply.NONE, False, None, False),
    ("surge contact, no modem", "c-0010", 10, False, HailReply.INVALID, True, None, False),
    ("unknown observed id", "c-0099", 10, False, HailReply.NONE, False, None, False),
]


@pytest.mark.parametrize(
    ("case", "contact", "tick", "prev", "bucket", "replied", "modem", "known"),
    REPLY_TABLE,
    ids=[row[0] for row in REPLY_TABLE],
)
def test_reply_table(
    case: str,
    contact: str,
    tick: int,
    prev: bool,
    bucket: HailReply,
    replied: bool,
    modem: str | None,
    known: bool,
) -> None:
    result = make_sim().send(contact, tick, previously_replied=prev)
    assert result.hail_reply is bucket, case
    assert result.replied is replied, case
    assert result.modem_id == modem, case
    assert result.operator_known is known, case
    assert result.sent_tick == tick
    assert result.reply_tick == tick + 10
    assert result.cost_usd == 0.01


def test_reply_table_has_enough_rows() -> None:
    assert len(REPLY_TABLE) >= 10
    assert any(row[2] == SILENT_AT and row[1] == "c-0001" for row in REPLY_TABLE)


def test_reply_table_validity_ignores_filing() -> None:
    """Addendum 23:05: an unfiled contact answering with a well-formed id is valid."""
    scenario = make_scenario()
    assert not scenario.contacts[1].registry.filed
    assert make_sim().send("c-0002", 0).hail_reply is HailReply.VALID


def test_reply_table_malformed_contact_id_raises() -> None:
    with pytest.raises(ValueError):
        make_sim().send("not-an-id", 0)


def test_reply_table_mismatched_run_raises() -> None:
    scenario = make_scenario()
    run = make_run(scenario)
    other = scenario.model_copy(update={"id": "S98"})
    with pytest.raises(ValueError):
        HailSimulator(other, run, seed=1, known=known_table(), cfg=HailConfig())


# --- known_operators ---------------------------------------------------------------------


def _write_known(tmp_path: Path, entries: list[dict[str, str]]) -> Path:
    path = tmp_path / "known_operators.yaml"
    path.write_text(yaml.safe_dump({"known_operators": entries}), encoding="utf-8")
    return path


def test_known_operators_union_of_file_and_scenario(tmp_path: Path) -> None:
    path = _write_known(
        tmp_path,
        [
            {"modem_id": "MOD-7735", "operator": "Lab (demo)", "note": "glider"},
            {"modem_id": "MOD-2290", "operator": "Port Survey Co (demo)", "note": "file"},
        ],
    )
    scenario = make_scenario(
        known=(
            {"operator": "Port Survey Co (demo)", "modem_id": "MOD-2290"},
            {"operator": "Scenario Only (demo)", "modem_id": "MOD-1111"},
        )
    )
    table = load_known_operators(path, scenario=scenario)
    assert [e.modem_id for e in table.entries] == ["MOD-1111", "MOD-2290", "MOD-7735"]
    entry_2290 = table.lookup("MOD-2290")
    assert entry_2290 is not None and entry_2290.note == "file"  # file entry kept on a repeat
    entry_1111 = table.lookup("MOD-1111")
    assert entry_1111 is not None and entry_1111.operator == "Scenario Only (demo)"
    assert table.lookup("MOD-9999") is None
    assert table.lookup(None) is None
    assert [e.modem_id for e in load_known_operators(path).entries] == ["MOD-2290", "MOD-7735"]


def test_known_operators_conflicting_duplicate_raises(tmp_path: Path) -> None:
    path = _write_known(tmp_path, [{"modem_id": "MOD-2290", "operator": "Port Survey Co (demo)"}])
    scenario = make_scenario(known=({"operator": "Someone Else (demo)", "modem_id": "MOD-2290"},))
    with pytest.raises(ValueError, match="two operators"):
        load_known_operators(path, scenario=scenario)
    bad = _write_known(
        tmp_path,
        [
            {"modem_id": "MOD-1", "operator": "A (demo)"},
            {"modem_id": "MOD-1", "operator": "B (demo)"},
        ],
    )
    with pytest.raises(ValueError, match="two operators"):
        load_known_operators(bad)


def test_known_operators_table_rejects_unsorted_or_duplicate() -> None:
    a = KnownOperatorEntry(modem_id="MOD-2", operator="A (demo)")
    b = KnownOperatorEntry(modem_id="MOD-1", operator="B (demo)")
    with pytest.raises(ValueError):
        KnownOperatorTable(entries=(a, b))
    with pytest.raises(ValueError):
        KnownOperatorTable(entries=(b, b))


def test_known_operators_drive_operator_known() -> None:
    with_known = make_sim().send("c-0002", 0)
    assert with_known.operator_known is True
    assert with_known.operator == "Port Survey Co (demo)"
    without = make_sim(known=KnownOperatorTable()).send("c-0002", 0)
    assert without.hail_reply is HailReply.VALID
    assert without.operator_known is False
    assert without.operator is None


def test_known_operators_config_file_seeded_with_s04_modem() -> None:
    s04_path = next((REPO_ROOT / "scenarios").glob("S04-*.yaml"))
    s04 = yaml.safe_load(s04_path.read_text(encoding="utf-8"))
    replying = [
        c["registry"]["modem_id"]
        for c in s04["contacts"]
        if c["registry"].get("replies_to_hail") and c["registry"].get("modem_id")
    ]
    assert replying, "S04 must have a replying contact with a modem id"
    table = load_known_operators()
    for modem in replying:
        assert table.lookup(modem) is not None
    assert 3 <= len(table.entries) <= 5
    assert all(e.operator.endswith("(demo)") for e in table.entries)


def test_known_operators_default_loader_adds_scenario_entries() -> None:
    scenario = make_scenario(known=({"operator": "Scenario Only (demo)", "modem_id": "MOD-1111"},))
    sim = HailSimulator(scenario, make_run(scenario), seed=1, cfg=HailConfig())
    assert sim.known.lookup("MOD-1111") is not None
    assert sim.known.lookup("MOD-2290") is not None


# --- idempotent --------------------------------------------------------------------------


class _RngSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, int]] = []
        self._real = hail_mod.loss_rng

    def __call__(self, seed: int, contact_id: str, sent_tick: int) -> np.random.Generator:
        self.calls.append((seed, contact_id, sent_tick))
        return self._real(seed, contact_id, sent_tick)


def test_idempotent_same_bucket_draws_once(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _RngSpy()
    monkeypatch.setattr(hail_mod, "loss_rng", spy)
    sim = make_sim(cfg=HailConfig(reply_loss_prob=0.5))
    first = sim.send("c-0002", 0)
    again = sim.send("c-0002", 14)  # 28 s: same 30 s bucket
    again_prev = sim.send("c-0002", 7, previously_replied=True)
    assert first == again == again_prev
    assert first.idempotency_key == again.idempotency_key
    assert len(spy.calls) == 1
    nxt = sim.send("c-0002", 15)  # 30 s: next bucket
    assert nxt.idempotency_key != first.idempotency_key
    assert nxt.sent_tick == 15
    assert len(spy.calls) == 2


def test_idempotent_key_is_shared_formula() -> None:
    sim = make_sim()
    result = sim.send("c-0001", 40)
    expected = idempotency_key("c-0001", "acoustic_hail", {}, tick_bucket(40, TICK_S))
    assert result.idempotency_key == expected
    assert sim.key("c-0001", 40) == expected


def test_idempotent_no_draw_when_loss_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _RngSpy()
    monkeypatch.setattr(hail_mod, "loss_rng", spy)
    make_sim().send("c-0002", 0)
    assert spy.calls == []


def test_idempotent_across_simulators_and_contacts() -> None:
    cfg = HailConfig(reply_loss_prob=0.5)
    a, b = make_sim(cfg=cfg), make_sim(cfg=cfg)
    for tick in range(0, 600, 5):
        for cid in ("c-0001", "c-0002", "c-0006"):
            assert a.send(cid, tick) == b.send(cid, tick)
    assert a.send("c-0002", 0).idempotency_key != a.send("c-0006", 0).idempotency_key


def test_idempotent_loss_draw_matches_documented_rng() -> None:
    from soundings.canonical import stable_hash

    gen = hail_mod.loss_rng(9, "c-0002", 33)
    salt = int(stable_hash(["c-0002", 33])[:8], 16)
    assert gen.random() == np.random.default_rng([9, salt]).random()


def test_idempotent_loss_one_drops_every_reply() -> None:
    sim = make_sim(cfg=HailConfig(reply_loss_prob=1.0))
    result = sim.send("c-0002", 0, previously_replied=True)
    assert result.replied is False
    assert result.hail_reply is HailReply.TIMEOUT


# --- evidence_rows -----------------------------------------------------------------------

ROW_ORDER = [
    FeatureName.HAIL_SENT_AT,
    FeatureName.HAIL_REPLY,
    FeatureName.MODEM_ID,
    FeatureName.OPERATOR_KNOWN,
]


def _all_results() -> list[HailResult]:
    out = []
    for _case, cid, tick, prev, *_ in REPLY_TABLE:
        out.append(make_sim().send(cid, tick, previously_replied=prev))
    return out


def test_evidence_rows_four_facts_in_order_never_null() -> None:
    for result in _all_results():
        rows = hail_evidence_rows(result, tick_s=TICK_S)
        assert [r.feature for r in rows] == ROW_ORDER
        assert len({r.row_id for r in rows}) == 4
        for row in rows:
            assert isinstance(row, EvidenceRow)
            assert row.kind is EvidenceKind.FACT
            assert row.probe_id is ProbeId.ACOUSTIC_HAIL
            assert row.source == "probe:acoustic_hail"
            assert row.contact_id == result.contact_id
            assert row.t == result.reply_tick
            assert row.derived_from == () and row.assumes == ""
            dumped = row.model_dump(mode="json")
            assert all(v is not None for v in dumped.values()), dumped
            # round trip through JSON re-validates, row_id included
            again = EvidenceRow.model_validate_json(row.model_dump_json())
            assert again == row and again.row_id == row.row_id
        by = {r.feature: r for r in rows}
        assert by[FeatureName.HAIL_SENT_AT].value == result.sent_tick * TICK_S
        assert by[FeatureName.HAIL_SENT_AT].unit.value == "s"
        assert by[FeatureName.HAIL_REPLY].value == result.hail_reply.value
        assert by[FeatureName.OPERATOR_KNOWN].value is result.operator_known
        expected_conf = 1.0 if result.replied else NO_REPLY_CONFIDENCE
        assert all(r.confidence == expected_conf for r in rows)


def test_evidence_rows_unknown_modem_on_non_reply() -> None:
    result = make_sim().send("c-0005", 10)
    rows = hail_evidence_rows(result, tick_s=TICK_S)
    modem_row = rows[2]
    assert modem_row.feature is FeatureName.MODEM_ID
    assert modem_row.value == UNKNOWN == "unknown"
    assert rows[1].value == "none"
    assert rows[3].value is False
    assert all(r.confidence == 0.9 for r in rows)


def test_evidence_rows_valid_reply_values() -> None:
    result = make_sim().send("c-0002", 12)
    rows = hail_evidence_rows(result, tick_s=TICK_S)
    assert [r.value for r in rows] == [24.0, "valid", "MOD-2290", True]
    assert all(r.t == 22 for r in rows)


def test_evidence_rows_name_no_hypothesis() -> None:
    """Ground truth is for grading only: no row or result names a hypothesis."""
    for result in _all_results():
        text = canonical_json([result, hail_evidence_rows(result, tick_s=TICK_S)])
        for hyp in Hypothesis:
            assert hyp.value not in text


def test_evidence_rows_honesty_docstring() -> None:
    doc = " ".join((hail_mod.__doc__ or "").split())
    assert doc.startswith(HONESTY_NOTE)
    assert "stress-test" in doc


# --- probe_keys --------------------------------------------------------------------------


def test_probe_keys_formula_and_shape() -> None:
    key = idempotency_key("c-0001", "acoustic_hail", {}, 3)
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert key == idempotency_key("c-0001", "acoustic_hail", {}, 3)
    assert key == idempotency_key("c-0001", "acoustic_hail", None, 3)
    assert key == idempotency_key("c-0001", ProbeId.ACOUSTIC_HAIL, {}, 3)
    expected = hashlib.sha256(
        canonical_json(["c-0001", "acoustic_hail", {}, 3]).encode("utf-8")
    ).hexdigest()
    assert key == expected


def test_probe_keys_change_with_args_bucket_contact_probe() -> None:
    base = idempotency_key("c-0001", "camera_cue", {"zoom": 1}, 3)
    assert base == idempotency_key("c-0001", "camera_cue", {"zoom": 1}, 3)
    assert base != idempotency_key("c-0001", "camera_cue", {"zoom": 2}, 3)
    assert base != idempotency_key("c-0001", "camera_cue", {"zoom": 1}, 4)
    assert base != idempotency_key("c-0002", "camera_cue", {"zoom": 1}, 3)
    assert base != idempotency_key("c-0001", "drift_check", {"zoom": 1}, 3)


def test_probe_keys_tick_bucket() -> None:
    assert TICK_BUCKET_S == 30
    assert tick_bucket(14, 2) == 0
    assert tick_bucket(15, 2) == 1
    assert tick_bucket(0, 2) == 0
    assert tick_bucket(29, 1) == 0 and tick_bucket(30, 1) == 1
    assert tick_bucket(10, 3.0) == 1
    assert isinstance(tick_bucket(15, 2.0), int)
    with pytest.raises(ValueError):
        tick_bucket(-1, 2)


# --- config_agree ------------------------------------------------------------------------


def test_config_agree_challenge_yaml_values() -> None:
    cfg = load_hail_config()
    assert cfg.cost_usd == 0.01
    assert cfg.latency_s == 20
    assert cfg.reply_loss_prob == 0.0
    assert cfg.modem_id_pattern == "^MOD-[0-9]{4,6}$"
    assert cfg == HailConfig()


def test_config_agree_with_registry_pattern() -> None:
    registry_path = CONFIG_DIR / "registry.yaml"
    if not registry_path.exists():
        pytest.skip("config/registry.yaml lands with WO-020; compared once it is merged")
    doc = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    assert load_hail_config().modem_id_pattern == doc["registry"]["modem_id_pattern"]


def test_config_agree_rejects_bad_config(tmp_path: Path) -> None:
    bad = tmp_path / "challenge.yaml"
    bad.write_text("hail:\n  latency_s: 20\n  modem_id_pattern: '('\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_hail_config(bad)
    extra = tmp_path / "extra.yaml"
    extra.write_text("hail:\n  latency_s: 20\n  surprise: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_hail_config(extra)
