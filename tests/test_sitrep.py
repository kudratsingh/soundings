"""WO-042 part A: the SITREP template writer and validator (SPEC-LADDER section 6).

Real catalog evidence: a LOST_VEHICLE SITREP from S02 seed 3 through runner v1 (WO-022),
and a draft SITREP from S05 seed 11's evidence table at the 180-tick prefix (WO-012's
decision tick). Ground truth is read only to grade the search box and to pick a contact.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from soundings.canonical import canonical_json
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.posterior import (
    STRESS_TEST_LABEL,
    Posterior,
    load_likelihood_tables,
    load_prior_config,
    score_contact,
)
from soundings.ladder.handoff import HandoffService
from soundings.ladder.sitrep import (
    HYPOTHESIS_ROWS_PER_FEATURE,
    MISSING_PREFIX,
    SEARCH_BOX_HEADING,
    SITREP_HEADINGS,
    SPEC_FEATURES,
    ActionLine,
    HypothesisLine,
    SearchBoxSection,
    Sitrep,
    SitrepError,
    SitrepInputs,
    SitrepIssueCode,
    SitrepLine,
    SitrepStatus,
    SitrepWriter,
    build_sitrep,
    headings_for,
    render_markdown,
    sitrep_inputs,
    sitrep_json,
    validate_sitrep,
)
from soundings.ladder.synthetic import S05_CONTACT
from soundings.llm.client import FixtureStore, ModelClient, ModelMode, fixture_key
from soundings.llm.sitrep_writer import (
    ROLE_SITREP_WRITER,
    SitrepPath,
    SitrepViolation,
    SitrepWriterRole,
    guard_sitrep,
    sitrep_writer_prompt,
    template_sitrep,
)
from soundings.runner import LostVehicleNotice, RunResult
from soundings.schemas import ToggleName
from soundings.schemas.denylist import DENYLIST, find_denylisted
from soundings.schemas.enums import EvidenceKind, FeatureName, Hypothesis, Rung
from soundings.schemas.evidence import EvidenceRow
from soundings.sim.run import truncate_run
from tests.runner_runs import gt_of, run, truth_at
from tests.support import sitrep_bodies as bodies
from tests.support.handoff_runs import REJECTION_CONTACT, S05Handoff, run_s05
from tests.test_posterior_catalog import _evidence, _run, _truth_ids

REPO = Path(__file__).resolve().parent.parent
TABLES = load_likelihood_tables()
PRIORS = load_prior_config()
S05_PREFIX_TICKS = 180
SPEC_SECTION_NAMES = (
    "Contact",
    "What we know (facts, each with evidence ids)",
    "What we assume",
    "What we don't know",
    "Hypotheses (posterior, labeled stress-test",
    "Actions already taken (rung, time, standing-order clause)",
    "Recommended rung (computed; not editable by the writer)",
    "What a person must decide",
    "Search box (LOST_VEHICLE only)",
)


# --- real inputs ----------------------------------------------------------------------------


@cache
def _s05() -> tuple[EvidenceTable, str]:
    prefix = truncate_run(_run("S05", 11), S05_PREFIX_TICKS)
    table = _evidence("S05", prefix)
    return table, _truth_ids(prefix, Hypothesis.UNAUTHORIZED_UUV)[0]


@cache
def s05_draft_inputs() -> SitrepInputs:
    table, cid = _s05()
    report = score_contact(table, cid, TABLES, PRIORS)
    return SitrepInputs.from_report(
        table, report, recommended_rung=Rung.HAND_TO_PERSON, status=SitrepStatus.DRAFT
    )


@cache
def _s02() -> tuple[RunResult, LostVehicleNotice]:
    result = run("S02", 3)
    assert result.notices, "S02 seed 3 must end with a LOST_VEHICLE notice"
    return result, result.notices[0]


@cache
def s02_lost_vehicle_inputs() -> SitrepInputs:
    result, notice = _s02()
    report = score_contact(result.evidence, notice.contact_id, TABLES, PRIORS)
    return SitrepInputs.from_report(
        result.evidence,
        report,
        recommended_rung=Rung.ASK,  # runner v1 ends LOST_VEHICLE at the ask rung
        search_box=notice.search_box,
        decision_row_ids=notice.evidence_row_ids,
    )


CASES = {"s05_draft": s05_draft_inputs, "s02_lost_vehicle": s02_lost_vehicle_inputs}


@pytest.fixture(params=sorted(CASES))
def inputs(request: pytest.FixtureRequest) -> SitrepInputs:
    return CASES[request.param]()


def _all_lines(s: Sitrep) -> list[tuple[str, tuple[str, ...]]]:
    out = [(f"contact:{ln.text}", ln.evidence_ids) for ln in s.contact]
    out += [(f"fact:{ln.text}", ln.evidence_ids) for ln in s.facts]
    out += [(f"assume:{ln.text}", ln.evidence_ids) for ln in s.assumptions]
    out += [(f"unknown:{ln.text}", ln.evidence_ids) for ln in s.unknowns]
    out += [(f"hyp:{h.hypothesis.value}", h.evidence_ids) for h in s.hypotheses]
    out += [(f"action:{a.clause}", a.evidence_ids) for a in s.actions_taken]
    out += [(f"decide:{ln.text}", ln.evidence_ids) for ln in s.person_must_decide]
    if s.search_box is not None:
        out.append(("search_box", s.search_box.evidence_ids))
    return out


# --- headings and the honesty rules ---------------------------------------------------------


def test_headings_match_spec_section_6() -> None:
    assert len(SITREP_HEADINGS) == 9
    for heading, spec in zip(SITREP_HEADINGS, SPEC_SECTION_NAMES, strict=True):
        assert heading.startswith(spec), (heading, spec)
    spec_text = (REPO / "docs" / "SPEC-LADDER.md").read_text(encoding="utf-8")
    section6 = spec_text.split("## 6.")[1].split("## 7.")[0]
    positions = [section6.index(name.split(" (")[0]) for name in SPEC_SECTION_NAMES]
    assert positions == sorted(positions)
    assert STRESS_TEST_LABEL in SITREP_HEADINGS[4]


def test_sections_and_honesty(inputs: SitrepInputs) -> None:
    sitrep = build_sitrep(inputs)
    md = render_markdown(sitrep)

    # The headings appear in section 6 order; the search box only for LOST_VEHICLE.
    expected = SITREP_HEADINGS if inputs.lost_vehicle else SITREP_HEADINGS[:8]
    found = [ln[3:] for ln in md.splitlines() if ln.startswith("## ")]
    assert found == list(expected) == list(headings_for(sitrep))
    assert (f"## {SEARCH_BOX_HEADING}" in md) is inputs.lost_vehicle

    # Every HypothesisLine and the rendered Hypotheses section carry the label.
    assert len(sitrep.hypotheses) == len(Hypothesis)
    assert all(h.label == STRESS_TEST_LABEL == "stress-test estimate" for h in sitrep.hypotheses)
    hyp_section = md.split(f"## {SITREP_HEADINGS[4]}")[1].split("\n## ")[0]
    hyp_lines = [ln for ln in hyp_section.splitlines() if ln.startswith("- ")]
    assert len(hyp_lines) == len(Hypothesis)
    assert all(STRESS_TEST_LABEL in ln for ln in hyp_lines)

    # Every assumption row of the contact appears in What we assume or What we don't know.
    rows = inputs.contact_rows
    assumption_ids = {r.row_id for r in rows if r.kind is EvidenceKind.ASSUMPTION}
    assert assumption_ids
    cited = {i for ln in (*sitrep.assumptions, *sitrep.unknowns) for i in ln.evidence_ids}
    assert assumption_ids <= cited
    for ln in sitrep.assumptions:  # each assumption line quotes its rows' assumes
        for row_id in ln.evidence_ids:
            assert inputs.index[row_id].assumes in ln.text

    # Each section 2 feature with no row shows up as exactly one `missing:` line.
    present = {r.feature for r in rows}
    missing = [ln.text for ln in sitrep.unknowns if ln.text.startswith(MISSING_PREFIX)]
    assert missing == [f"missing: {f.value}" for f in SPEC_FEATURES if f not in present]

    # No fact line cites an assumption row; assumption lines cite only assumption rows.
    for ln in sitrep.facts:
        assert all(inputs.index[i].kind is not EvidenceKind.ASSUMPTION for i in ln.evidence_ids)
    for ln in sitrep.assumptions:
        assert all(inputs.index[i].kind is EvidenceKind.ASSUMPTION for i in ln.evidence_ids)


def test_every_line_cites_existing_rows(inputs: SitrepInputs) -> None:
    sitrep = build_sitrep(inputs)
    assert validate_sitrep(sitrep, inputs) == []
    for where, ids in _all_lines(sitrep):
        if where.startswith(f"unknown:{MISSING_PREFIX}"):
            assert ids == (), where  # the only exemption, and it carries no ids
            continue
        assert len(ids) >= 1, where
        assert all(i in inputs.index for i in ids), where
    assert sitrep.facts and sitrep.assumptions
    assert {ln.text for ln in sitrep.facts}.isdisjoint({ln.text for ln in sitrep.assumptions})


def test_missing_lines_on_a_sparse_table() -> None:
    """A three-row table: every other section 2 feature is a `missing:` line, uncited."""
    table = EvidenceTable()
    base = {"contact_id": "c-0042", "probe_id": None, "source": "sim.detector:hyd_1"}
    depth = EvidenceRow(
        **base, t=4, feature=FeatureName.DEPTH, value=5.5, unit="m", confidence=0.9, kind="fact"
    )
    current = EvidenceRow(
        **base,
        t=4,
        feature=FeatureName.CURRENT_FIELD,
        value=0.2,
        unit="m/s",
        confidence=0.5,
        kind="assumption",
        assumes="current field is the configured default; no live gauge",
    )
    hail = EvidenceRow(
        **base,
        t=4,
        feature=FeatureName.HAIL_REPLY,
        value="unknown",
        unit="category",
        confidence="unknown",
        kind="fact",
    )
    table.extend([depth, current, hail])
    uniform = Posterior.uniform().probs
    inputs = SitrepInputs(
        contact_id="c-0042", evidence=table.rows(), posterior=uniform, recommended_rung="watch"
    )
    sitrep = build_sitrep(inputs)
    assert validate_sitrep(sitrep, inputs) == []
    missing = [ln for ln in sitrep.unknowns if ln.is_missing]
    expected = [f for f in SPEC_FEATURES if f not in {FeatureName.DEPTH, FeatureName.HAIL_REPLY}]
    assert [ln.text for ln in missing] == [f"missing: {f.value}" for f in expected]
    assert all(ln.evidence_ids == () for ln in missing)
    assert [ln.evidence_ids for ln in sitrep.facts] == [(depth.row_id,)]
    unknown_ids = [ln.evidence_ids for ln in sitrep.unknowns if not ln.is_missing]
    assert unknown_ids == [(current.row_id,), (hail.row_id,)]  # assumption, then unknown value
    assert all(h.evidence_ids for h in sitrep.hypotheses)  # prior only: still cited


# --- the recommended rung -------------------------------------------------------------------


@pytest.mark.parametrize("rung", list(Rung))
def test_recommended_rung_equals_input(rung: Rung) -> None:
    inputs = dataclasses.replace(s05_draft_inputs(), recommended_rung=rung)
    sitrep = build_sitrep(inputs)
    assert sitrep.recommended_rung is rung
    assert f"## {SITREP_HEADINGS[6]}\n- {rung.value} " in render_markdown(sitrep)
    assert validate_sitrep(sitrep, inputs) == []
    other = next(r for r in Rung if r is not rung)
    edited = sitrep.model_copy(update={"recommended_rung": other})
    codes = {i.code for i in validate_sitrep(edited, inputs)}
    assert codes == {SitrepIssueCode.RECOMMENDED_RUNG_MISMATCH}


def test_template_needs_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The template never touches soundings.llm or anthropic."""
    for name in list(sys.modules):
        if name.startswith("anthropic"):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "anthropic", None)  # importing it would now raise
    sitrep = build_sitrep(s05_draft_inputs())
    assert sitrep.writer is SitrepWriter.TEMPLATE
    text = (REPO / "soundings" / "ladder" / "sitrep.py").read_text(encoding="utf-8")
    assert "soundings.llm" not in text and "anthropic" not in text


# --- the validator --------------------------------------------------------------------------


def _codes(sitrep: Sitrep, inputs: SitrepInputs) -> set[SitrepIssueCode]:
    return {i.code for i in validate_sitrep(sitrep, inputs)}


def _denylisted_word() -> str:
    """Build a denylisted word from the pattern at test time (no literal in the repo)."""
    pattern = DENYLIST[0].pattern
    word = re.sub(r"\(.*?\)\?|\\b", "", pattern)
    assert find_denylisted(word) == [word]
    return word


def test_validator_rejects_bad_citations() -> None:
    inputs = s05_draft_inputs()
    good = build_sitrep(inputs)
    assumption = good.assumptions[0]
    fact = good.facts[0]

    cites_assumption = good.model_copy(
        update={"facts": (SitrepLine(text=fact.text, evidence_ids=assumption.evidence_ids),)}
    )
    assert _codes(cites_assumption, inputs) == {SitrepIssueCode.CITATION}

    uncited = good.model_copy(update={"facts": (SitrepLine(text=fact.text),)})
    assert _codes(uncited, inputs) == {SitrepIssueCode.CITATION}

    ghost = good.model_copy(
        update={"facts": (SitrepLine(text=fact.text, evidence_ids=("0" * 16,)),)}
    )
    assert _codes(ghost, inputs) == {SitrepIssueCode.CITATION}

    fact_as_assumption = good.model_copy(update={"assumptions": (fact,)})
    assert SitrepIssueCode.CITATION in _codes(fact_as_assumption, inputs)

    present = inputs.contact_rows[0].feature.value
    bad_missing = good.model_copy(
        update={"unknowns": (*good.unknowns, SitrepLine(text=f"missing: {present}"))}
    )
    assert _codes(bad_missing, inputs) == {SitrepIssueCode.CITATION}

    no_decision_ids = good.model_copy(update={"person_must_decide": (SitrepLine(text="decide"),)})
    assert _codes(no_decision_ids, inputs) == {SitrepIssueCode.CITATION}

    uncited_hyp = good.model_copy(
        update={"hypotheses": (HypothesisLine(hypothesis=Hypothesis.BIOLOGIC, p=0.5),)}
    )
    assert _codes(uncited_hyp, inputs) == {SitrepIssueCode.CITATION}


def test_validator_rejects_mixed_sections_label_box_and_denylist() -> None:
    inputs = s05_draft_inputs()
    good = build_sitrep(inputs)

    same_text = SitrepLine(text=good.facts[0].text, evidence_ids=good.assumptions[0].evidence_ids)
    mixed = good.model_copy(update={"assumptions": (*good.assumptions, same_text)})
    assert SitrepIssueCode.SECTIONS in _codes(mixed, inputs)

    unlabeled = HypothesisLine.model_construct(
        hypothesis=Hypothesis.BIOLOGIC, p=0.5, label="estimate", evidence_ids=("x",)
    )
    hyp = good.model_copy(update={"hypotheses": (unlabeled,)})
    assert SitrepIssueCode.LABEL in _codes(hyp, inputs)

    lv = s02_lost_vehicle_inputs()
    lv_good = build_sitrep(lv)
    assert lv_good.search_box is not None
    assert _codes(lv_good.model_copy(update={"search_box": None}), lv) == {
        SitrepIssueCode.SEARCH_BOX
    }
    assert _codes(good.model_copy(update={"search_box": lv_good.search_box}), inputs) == {
        SitrepIssueCode.SEARCH_BOX
    }

    word = _denylisted_word()
    dirty = dataclasses.replace(inputs, question_for_human=f"Should we {word} it?")
    assert _codes(build_sitrep(dirty), dirty) == {SitrepIssueCode.DENYLIST}
    assert find_denylisted(render_markdown(good)) == []


# --- the two catalog SITREPs ----------------------------------------------------------------


def test_lost_vehicle_sitrep_from_s02_runner_v1() -> None:
    result, notice = _s02()
    inputs = s02_lost_vehicle_inputs()
    sitrep = build_sitrep(inputs)
    assert validate_sitrep(sitrep, inputs) == []
    section = sitrep.search_box
    assert isinstance(section, SearchBoxSection)
    assert section.box == notice.search_box  # WO-022's box, no second type
    assert section.last_known == notice.last_known == notice.search_box.anchor
    basis = result.evidence.get(notice.search_box.assumption_row_id)
    assert basis.kind is EvidenceKind.ASSUMPTION and basis.feature is FeatureName.CURRENT_FIELD
    assert section.drift_basis == basis.assumes
    assert notice.search_box.assumption_row_id in section.evidence_ids
    lat, lon = truth_at(result, gt_of(result, notice.contact_id), notice.tick)
    assert section.box.contains(lat, lon)
    md = render_markdown(sitrep)
    assert md.splitlines()[-1].startswith("- drift basis (assumption): ")
    assert sitrep.recommended_rung is Rung.ASK and sitrep.status is SitrepStatus.FINAL
    assert set(notice.evidence_row_ids) <= set(sitrep.person_must_decide[0].evidence_ids)


def test_draft_sitrep_from_s05_prefix() -> None:
    table, cid = _s05()
    inputs = s05_draft_inputs()
    sitrep = build_sitrep(inputs)
    assert sitrep.contact_id == cid and sitrep.status is SitrepStatus.DRAFT
    assert sitrep.recommended_rung is Rung.HAND_TO_PERSON and sitrep.search_box is None
    assert max(r.t for r in inputs.contact_rows) < S05_PREFIX_TICKS
    report = score_contact(table, cid, TABLES, PRIORS)
    probs = {h.hypothesis: h.p for h in sitrep.hypotheses}
    assert probs == {h: round(p, 6) for h, p in report.posterior.probs.items()}
    assert [h.hypothesis for h in sitrep.hypotheses] == [h for h, _ in report.posterior.ranked()]
    # Hypotheses cite the three most recent rows of each feature the posterior used.
    cited = sitrep.hypotheses[0].evidence_ids
    used = [table.get(i) for i in report.used_row_ids]
    for row in used:
        feature_rows = table.feature_rows(cid, row.feature)
        recent = sorted(feature_rows, key=lambda r: r.t)[-HYPOTHESIS_ROWS_PER_FEATURE:]
        assert {r.row_id for r in recent} <= set(cited), row.feature
        assert sum(1 for i in cited if table.get(i).feature is row.feature) == len(recent)
    assert any(len(table.feature_rows(cid, r.feature)) > 3 for r in used)
    assert "draft" in sitrep.person_must_decide[0].text


def test_actions_and_flags_render_with_citations() -> None:
    base = s05_draft_inputs()
    ttc = base.contact_rows[-1].row_id
    actions = (
        ActionLine(rung=Rung.SLOW, t_s=240.0, clause="C_SLOW_BASIN", evidence_ids=(ttc,)),
        ActionLine(rung=Rung.HOLD, t_s=300.1234567, clause="C_HOLD_ASSET", evidence_ids=(ttc,)),
    )
    inputs = dataclasses.replace(
        base,
        actions_taken=actions,
        status=SitrepStatus.FINAL,
        approver_unreachable=True,
        uncertain=True,
        question_for_human="Is the fuel berth crew expecting a survey vehicle tonight?",
    )
    sitrep = build_sitrep(inputs)
    assert validate_sitrep(sitrep, inputs) == []
    md = render_markdown(sitrep)
    assert "- rung slow at t=240.0 s under standing-order clause C_SLOW_BASIN" in md
    assert "t=300.123457 s" in md  # 6 dp at the output boundary
    decide = [ln.text for ln in sitrep.person_must_decide]
    assert decide[0].startswith("The watch officer decides")
    assert inputs.question_for_human in decide
    assert any("backup approver" in t for t in decide)
    assert any("uncertain" in t for t in decide)
    assert sitrep.uncertain and sitrep.approver_unreachable
    assert any("uncertain" in ln.text for ln in sitrep.contact)
    with pytest.raises(ValueError):
        ActionLine(rung=Rung.SLOW, t_s=1.0, clause="has space", evidence_ids=(ttc,))


# --- inputs, JSON and determinism -----------------------------------------------------------


def test_inputs_are_checked() -> None:
    base = s05_draft_inputs()
    bad_post = {h: 0.5 for h in Hypothesis}
    with pytest.raises(SitrepError):
        dataclasses.replace(base, posterior=bad_post)
    with pytest.raises(SitrepError):
        dataclasses.replace(base, posterior={Hypothesis.BIOLOGIC: 1.0})
    with pytest.raises(SitrepError):
        dataclasses.replace(base, contact_id="c-9999")
    with pytest.raises(SitrepError):
        dataclasses.replace(base, decision_row_ids=("0" * 16,))
    assert dataclasses.replace(base, recommended_rung="hold").recommended_rung is Rung.HOLD


def test_json_dict(inputs: SitrepInputs) -> None:
    sitrep = build_sitrep(inputs)
    data = sitrep.to_dict()
    assert data["recommended_rung"] == inputs.recommended_rung.value
    assert data["label"] == STRESS_TEST_LABEL
    assert data["headings"] == list(headings_for(sitrep))
    assert json.loads(canonical_json(data)) == data
    body = {k: v for k, v in data.items() if k not in ("headings", "label")}
    assert Sitrep.model_validate_json(json.dumps(body)) == sitrep
    full = sitrep_json(sitrep)
    assert full["markdown"] == render_markdown(sitrep)
    for h in data["hypotheses"]:
        assert h["label"] == STRESS_TEST_LABEL and h["p"] == round(h["p"], 6)


def test_sitrep_inputs_canonical(inputs: SitrepInputs) -> None:
    a = sitrep_inputs(inputs)
    b = sitrep_inputs(dataclasses.replace(inputs))
    assert canonical_json(a) == canonical_json(b)
    assert a["recommended_rung"] == inputs.recommended_rung.value
    assert {r["row_id"] for r in a["evidence"]} == {r.row_id for r in inputs.contact_rows}
    assert a["label"] == STRESS_TEST_LABEL


_MD_SCRIPT = """
import hashlib
from soundings.ladder.sitrep import build_sitrep, render_markdown
from tests.test_sitrep import s02_lost_vehicle_inputs, s05_draft_inputs
for make in (s05_draft_inputs, s02_lost_vehicle_inputs):
    print(hashlib.sha256(render_markdown(build_sitrep(make())).encode()).hexdigest())
"""


def test_deterministic_markdown() -> None:
    """Two builds are byte-identical; PYTHONHASHSEED 0 and 1 print the same digests."""
    digests = []
    for make in (s05_draft_inputs, s02_lost_vehicle_inputs):
        a = render_markdown(build_sitrep(make()))
        b = render_markdown(build_sitrep(dataclasses.replace(make())))
        assert a == b
        digests.append(hashlib.sha256(a.encode()).hexdigest())
    outs = []
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "MODEL_MODE": "replay"}
        out = subprocess.run(
            [sys.executable, "-c", _MD_SCRIPT],
            capture_output=True,
            text=True,
            cwd=REPO,
            env=env,
            check=True,
        )
        outs.append(out.stdout.split())
    assert outs == [digests, digests]


def test_denylist_grep_clean() -> None:
    """``grep -riEf config/denylist.txt`` over the part A files finds nothing."""
    paths = [
        REPO / "soundings" / "ladder" / "sitrep.py",
        REPO / "soundings" / "ladder" / "audit.py",
        REPO / "soundings" / "audit" / "__init__.py",
        REPO / "soundings" / "audit" / "__main__.py",
        REPO / "tests" / "test_sitrep.py",
        REPO / "tests" / "test_audit.py",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for pattern in DENYLIST:
            assert not re.search(pattern.pattern, text, re.IGNORECASE), path.name


# --- WO-042 part B: the LLM writer behind the guard, the handoff --------------------------

SITREP_FIXTURES = REPO / "tests" / "fixtures" / "model_calls" / ROLE_SITREP_WRITER


@cache
def _s05_auto() -> S05Handoff:
    import tempfile

    return run_s05(Path(tempfile.mkdtemp()) / "auto", writer_mode="auto")


def _final(out: S05Handoff) -> Any:
    return next(p for p in out.service.packets if p.status is SitrepStatus.FINAL)


def s05_final_inputs() -> SitrepInputs:
    inputs = _final(_s05_auto()).inputs
    assert inputs is not None
    return inputs


def _check_citations(s: Sitrep, inputs: SitrepInputs) -> None:
    for name, ids in _all_lines(s):
        if name.startswith("unknown:") and name[len("unknown:") :].startswith(MISSING_PREFIX):
            assert ids == (), name
            continue
        assert ids, f"{name} cites nothing"
        assert all(i in inputs.index for i in ids), name


def test_llm_sitrep_every_line_cites_existing_rows() -> None:
    inputs = s05_final_inputs()
    result = guard_sitrep(bodies.good_body(inputs), inputs)
    assert result.accepted and result.value is not None
    assert result.value.writer is SitrepWriter.LLM
    _check_citations(result.value, inputs)
    assert validate_sitrep(result.value, inputs) == []


def test_llm_facts_and_assumptions_stay_separate() -> None:
    inputs = s02_lost_vehicle_inputs()
    assumption = next(r for r in inputs.contact_rows if r.kind is EvidenceKind.ASSUMPTION)
    fact = next(r for r in inputs.contact_rows if r.kind is EvidenceKind.FACT)
    body = {
        "contact_summary": f"Contact {inputs.contact_id} went silent inside the inner basin",
        "facts": [{"text": "Drift basis", "evidence_ids": [assumption.row_id]}],
        "assumptions": [],
        "unknowns": [],
        "person_must_decide": ["Whether the operator is told now"],
    }
    r = guard_sitrep(body, inputs)
    assert (r.accepted, r.violation) == (False, SitrepViolation.CITATION)
    body["facts"] = [{"text": "Last fix", "evidence_ids": [fact.row_id]}]
    body["assumptions"] = [{"text": "Drift", "evidence_ids": [fact.row_id]}]
    r = guard_sitrep(body, inputs)
    assert (r.accepted, r.violation) == (False, SitrepViolation.CITATION)
    body["assumptions"] = [
        {"text": "Drift follows the current", "evidence_ids": [assumption.row_id]}
    ]
    r = guard_sitrep(body, inputs)
    assert r.accepted and r.value is not None
    assert {i for ln in r.value.facts for i in ln.evidence_ids} == {fact.row_id}
    assert {i for ln in r.value.assumptions for i in ln.evidence_ids} == {assumption.row_id}


def test_rung_not_from_writer() -> None:
    """The accepted SITREP's rung is the computed one, whether the reply echoes it or not."""
    inputs = s05_final_inputs()
    body = bodies.good_body(inputs)
    assert body["recommended_rung"] == inputs.recommended_rung.value
    with_rung = guard_sitrep(body, inputs).value
    body.pop("recommended_rung")
    without = guard_sitrep(body, inputs).value
    assert with_rung is not None and without is not None
    assert with_rung.recommended_rung is without.recommended_rung is inputs.recommended_rung
    # Same reply, other computed rung: the SITREP follows the inputs, never the reply.
    other = dataclasses.replace(inputs, recommended_rung=Rung.ASK)
    moved = guard_sitrep(body, other).value
    assert moved is not None and moved.recommended_rung is Rung.ASK


def test_rung_change_rejected(tmp_path: Path) -> None:
    """The committed rung-change fixture is replayed, rejected, logged, and the template ships."""
    out = run_s05(tmp_path, writer_mode="auto", cid=REJECTION_CONTACT)
    final = _final(out)
    assert final.path is SitrepPath.FALLBACK_AFTER_REJECTION
    assert final.violation == SitrepViolation.RECOMMENDED_RUNG_MISMATCH.value
    assert final.inputs is not None
    assert final.sitrep == template_sitrep(final.inputs)
    assert final.sitrep.recommended_rung is Rung.HAND_TO_PERSON
    assert "hold (computed" not in final.markdown
    lines = [ln for ln in out.violations.read_text().splitlines() if ln.startswith("| ")]
    rows = [ln for ln in lines if ROLE_SITREP_WRITER in ln]
    assert len(rows) == 1
    assert final.fixture_key in rows[0] and "recommended_rung_mismatch" in rows[0]


GUARD_CASES = {
    "rung_change": (bodies.rung_change_body, SitrepViolation.RECOMMENDED_RUNG_MISMATCH),
    "bad_citation": (bodies.bad_citation_body, SitrepViolation.CITATION),
    "uncited_fact": (bodies.uncited_fact_body, SitrepViolation.CITATION),
    "extra_control_key": (bodies.extra_control_key_body, SitrepViolation.SCHEMA),
    "probability_in_prose": (
        bodies.probability_in_prose_body,
        SitrepViolation.PROBABILITY_IN_PROSE,
    ),
    "denylist": (bodies.denylist_body, SitrepViolation.DENYLIST),
}


@pytest.mark.parametrize("case", sorted(GUARD_CASES))
def test_guard_rejections_fall_back_to_template(case: str, tmp_path: Path) -> None:
    make, code = GUARD_CASES[case]
    inputs = s05_final_inputs()
    store = FixtureStore(tmp_path / "store")
    store.save(ROLE_SITREP_WRITER, sitrep_writer_prompt(inputs), make(inputs))
    log = tmp_path / "VIOLATIONS.md"
    role = SitrepWriterRole(ModelClient(mode=ModelMode.REPLAY, store=store), violations_path=log)
    outcome = role.write(inputs, when="t")
    assert (outcome.path, outcome.violation, outcome.llm_outcome) == (
        SitrepPath.FALLBACK_AFTER_REJECTION,
        code,
        "rejected",
    )
    assert outcome.sitrep == template_sitrep(inputs)
    assert outcome.sitrep.recommended_rung is inputs.recommended_rung
    rows = [ln for ln in log.read_text().splitlines() if ROLE_SITREP_WRITER in ln]
    assert len(rows) == 1 and code.value in rows[0]


def test_good_fixture_accepted_and_fixtures_consistent() -> None:
    files = sorted(SITREP_FIXTURES.glob("*.json"))
    assert len(files) == 2
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        assert set(record) == {"role", "key", "inputs", "output"}
        assert record["key"] == fixture_key(record["role"], record["inputs"]) == path.stem
    inputs = s05_final_inputs()
    assert fixture_key(ROLE_SITREP_WRITER, sitrep_writer_prompt(inputs)) in {p.stem for p in files}
    final = _final(_s05_auto())
    assert final.path is SitrepPath.FIXTURE_REPLAY
    assert final.sitrep.writer is SitrepWriter.LLM
    assert final.sitrep.recommended_rung is Rung.HAND_TO_PERSON
    assert validate_sitrep(final.sitrep, inputs) == []


def test_template_fallback_with_no_model_and_no_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("no live client may be built")

    monkeypatch.setattr(anthropic, "Anthropic", refuse)
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    empty = tmp_path / "no_fixtures"
    empty.mkdir()
    monkeypatch.setenv("SOUNDINGS_FIXTURE_ROOT", str(empty))
    out = run_s05(tmp_path / "run", writer_mode="auto", fixture_root=None)
    assert [p.path for p in out.service.packets] == [SitrepPath.TEMPLATE] * len(out.service.packets)
    final = _final(out)
    assert final.inputs is not None
    assert final.sitrep == template_sitrep(final.inputs)
    assert validate_sitrep(final.sitrep, final.inputs) == []
    _check_citations(final.sitrep, final.inputs)
    assert not out.violations.exists()
    calls = out.audit.rows(kind="llm_call")
    assert [c.payload["outcome"] for c in calls] == ["fallback_no_fixture"]


def test_draft_then_final_on_the_s05_stream() -> None:
    out = _s05_auto()
    statuses = [p.status for p in out.service.packets]
    assert statuses[:2] == [SitrepStatus.DRAFT, SitrepStatus.FINAL]
    assert all(s is SitrepStatus.UPDATE for s in statuses[2:])
    assert [s.kind for s in out.run.sitreps] == [s.value for s in statuses]
    assert [s.text for s in out.run.sitreps] == [p.markdown for p in out.service.packets]
    draft, final = out.service.packets[:2]
    assert draft.path is SitrepPath.TEMPLATE and draft.sitrep.writer is SitrepWriter.TEMPLATE
    assert "this draft is what the approver sees" in draft.markdown
    assert final.path is SitrepPath.FIXTURE_REPLAY
    assert {p.sitrep.recommended_rung for p in out.service.packets} == {Rung.HAND_TO_PERSON}
    # One handoff interruption (the final); the draft rides with the approval request.
    assert out.service.interruptions(S05_CONTACT) == 1
    assert [p.interruption for p in out.service.packets] == [False, True] + [False] * (
        len(statuses) - 2
    )
    assert all(p.channel == "ui" for p in out.service.packets)
    assert out.service.outbox["ui"] == out.service.packets
    # Exactly one model call for the handoff.
    assert len(out.audit.rows(kind="llm_call")) == 1
    for p in out.service.packets:
        assert p.inputs is not None
        _check_citations(p.sitrep, p.inputs)
        assert STRESS_TEST_LABEL in p.markdown


def test_unreachable_flag_on_the_final_sitrep(tmp_path: Path) -> None:
    out = run_s05(tmp_path, toggles=(ToggleName.APPROVER_UNREACHABLE,))
    final = _final(out)
    assert final.unreachable and final.sitrep.approver_unreachable
    assert "unreachable" in final.markdown
    assert out.run.sitreps[[s.kind for s in out.run.sitreps].index("final")].inputs.flags == (
        "unreachable",
    )


def test_lost_vehicle_sitrep_carries_the_search_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)  # a replay miss falls back here
    inputs = s02_lost_vehicle_inputs()
    service = HandoffService(
        ModelClient(mode=ModelMode.REPLAY, store=FixtureStore(tmp_path / "empty")),
        writer_mode="auto",
        violations_path=tmp_path / "v.md",
    )
    packet = service.deliver(inputs, t_s=0.0)
    assert packet.sitrep.search_box is not None
    assert packet.sitrep.search_box.box == inputs.search_box
    assert f"## {SEARCH_BOX_HEADING}" in packet.markdown
    assert validate_sitrep(packet.sitrep, inputs) == []


def test_handoff_deterministic(tmp_path: Path) -> None:
    """Two runs give identical SITREP markdown and audit chains; so do two hash seeds."""
    a = run_s05(tmp_path / "a", writer_mode="auto")
    b = run_s05(tmp_path / "b", writer_mode="auto")
    assert [p.markdown for p in a.service.packets] == [p.markdown for p in b.service.packets]
    assert [r.row_hash for r in a.audit.rows()] == [r.row_hash for r in b.audit.rows()]
    assert a.audit.verify().head == b.audit.verify().head
    outs = []
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "MODEL_MODE": "replay"}
        proc = subprocess.run(
            [sys.executable, "-m", "tests.support.handoff_runs", "auto"],
            capture_output=True,
            text=True,
            cwd=REPO,
            env=env,
            check=True,
        )
        outs.append(proc.stdout)
    assert outs[0] == outs[1]
    assert outs[0].splitlines()[-1] == f"head {a.audit.verify().head}"


def test_part_b_denylist_grep_clean() -> None:
    paths = [
        REPO / "soundings" / "llm" / "sitrep_writer.py",
        REPO / "soundings" / "ladder" / "handoff.py",
        REPO / "tests" / "support" / "handoff_runs.py",
        REPO / "tests" / "support" / "sitrep_bodies.py",
        *sorted(SITREP_FIXTURES.glob("*.json")),
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for pattern in DENYLIST:
            assert not re.search(pattern.pattern, text, re.IGNORECASE), path.name
