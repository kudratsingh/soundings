"""WO-061: the judge-injection mapping is exactly the documented table (tests/test_inject.py)."""

from __future__ import annotations

import itertools
import re
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from soundings.inject import (
    BEHAVIOR_TABLE,
    AcousticProfile,
    InjectRequest,
    JudgeBehavior,
    OutsideZonesError,
    build_live_scenario,
    critical_asset,
    live_dir,
    live_seed,
    load_known_ops,
    scenario_yaml,
    write_live_scenario,
)
from soundings.schemas.denylist import assert_clean
from soundings.schemas.enums import BehaviorType, Hypothesis
from soundings.schemas.harbor import load_harbor
from soundings.schemas.scenario import load_scenario
from soundings.sim.scenario import catalog_paths

REPO = Path(__file__).resolve().parents[1]
PANEL = REPO / "web" / "src" / "components" / "JudgePanel.tsx"
HARBOR = load_harbor()
OPS = load_known_ops()
STAMP = datetime(2026, 10, 17, 12, 0, 0)  # naive: taken as PT
BERTH = next(a for a in HARBOR.assets if a.asset_id == "fuel_berth_2")
POINT = (BERTH.lat - 0.0036, BERTH.lon)  # ~400 m south of fuel_berth_2, inside a zone
UUV = "UNAUTHORIZED_UUV"


def _req(behavior: JudgeBehavior, acoustic: AcousticProfile | None = None) -> InjectRequest:
    return InjectRequest(lat=POINT[0], lon=POINT[1], behavior=behavior, acoustic=acoustic)


@pytest.mark.parametrize(
    ("behavior", "acoustic"),
    list(itertools.product(JudgeBehavior, [*AcousticProfile, None])),
)
def test_every_pair_round_trips(
    tmp_path: Path, behavior: JudgeBehavior, acoustic: AcousticProfile | None
) -> None:
    sc = build_live_scenario(_req(behavior, acoustic), STAMP, HARBOR, OPS)
    path = write_live_scenario(sc, tmp_path)
    back = load_scenario(path, HARBOR)
    assert back == sc
    assert re.fullmatch(r"L-\d{8}T\d{6}", back.id)
    assert back.duration_ticks == 600 and back.tick_s == 2 and back.hour_of_day == 12


def test_mapping_matches_the_table() -> None:
    zone = HARBOR.zone_at(*POINT)
    assert zone is not None
    target = critical_asset(HARBOR, *POINT).asset_id
    want = {
        JudgeBehavior.FILED: (BehaviorType.LAWNMOWER, True, True, "quiet", "FILED_ON_PLAN"),
        JudgeBehavior.UNFILED: (
            BehaviorType.LAWNMOWER,
            False,
            True,
            "quiet",
            "UNFILED_KNOWN_OPERATOR",
        ),
        JudgeBehavior.CLOSING: (BehaviorType.TRANSIT, False, False, "propeller", UUV),
        JudgeBehavior.LOITERING: (BehaviorType.ORBIT, False, False, "propeller", UUV),
        JudgeBehavior.SILENT: (
            BehaviorType.LAWNMOWER_THEN_DRIFT,
            True,
            True,
            "quiet",
            "FILED_OFF_PLAN",
        ),
    }
    for behavior, (btype, filed, replies, acoustic, truth) in want.items():
        row = BEHAVIOR_TABLE[behavior]
        assert (row.behavior_type, row.filed, row.replies_to_hail) == (btype, filed, replies)
        assert row.default_acoustic.value == acoustic and row.ground_truth is Hypothesis(truth)
        sc = build_live_scenario(_req(behavior), STAMP, HARBOR, OPS)
        (c,) = sc.contacts
        assert c.behavior.type is btype and sc.ground_truth is Hypothesis(truth)
        assert c.registry.filed is filed and c.registry.replies_to_hail is replies
        assert (c.start.lat, c.start.lon) == POINT
    closing = build_live_scenario(_req(JudgeBehavior.CLOSING), STAMP, HARBOR, OPS).contacts[0]
    assert closing.behavior.target_asset == target and closing.behavior.speed_mps == 1.0
    assert closing.acoustic.propeller_tonal is True and closing.acoustic.tonal_band.value == "mid"
    loiter = build_live_scenario(_req(JudgeBehavior.LOITERING), STAMP, HARBOR, OPS).contacts[0]
    assert loiter.behavior.target_asset == target and loiter.behavior.radius_m == 40
    assert loiter.acoustic.tonal_band.value == "low"
    filed = build_live_scenario(_req(JudgeBehavior.FILED), STAMP, HARBOR, OPS)
    (plan,) = filed.registry_plans
    assert plan.plan_id == "plan-live-20261017T120000" and plan.area == zone.zone_id
    assert (plan.window.start_tick, plan.window.end_tick) == (0, 600)
    assert filed.contacts[0].behavior.area == zone.zone_id
    assert filed.contacts[0].registry.plan_id == plan.plan_id
    unfiled = build_live_scenario(_req(JudgeBehavior.UNFILED), STAMP, HARBOR, OPS).contacts[0]
    assert unfiled.registry.modem_id == OPS[0]["modem_id"] == "MOD-2290"
    silent = build_live_scenario(_req(JudgeBehavior.SILENT), STAMP, HARBOR, OPS)
    assert silent.contacts[0].behavior.fault_tick == 60
    assert silent.contacts[0].registry.silent_after_tick == 60
    assert silent.registry_plans and silent.contacts[0].registry.plan_id


def test_acoustic_profiles() -> None:
    def ac(p: AcousticProfile) -> tuple[bool | None, bool | None]:
        c = build_live_scenario(_req(JudgeBehavior.FILED, p), STAMP, HARBOR, OPS).contacts[0]
        return c.acoustic.propeller_tonal, c.acoustic.biologic_signature

    assert ac(AcousticProfile.PROPELLER) == (True, False)
    assert ac(AcousticProfile.BIOLOGIC) == (False, True)
    assert ac(AcousticProfile.QUIET) == (False, False)


def test_outside_every_zone_is_refused() -> None:
    far = InjectRequest(lat=33.60, lon=-118.10, behavior=JudgeBehavior.CLOSING)
    assert HARBOR.zone_at(far.lat, far.lon) is None
    with pytest.raises(OutsideZonesError, match="outside the declared security zones"):
        build_live_scenario(far, STAMP, HARBOR, OPS)


def test_same_request_same_bytes(tmp_path: Path) -> None:
    a = scenario_yaml(build_live_scenario(_req(JudgeBehavior.LOITERING), STAMP, HARBOR, OPS))
    b = scenario_yaml(build_live_scenario(_req(JudgeBehavior.LOITERING), STAMP, HARBOR, OPS))
    assert a == b
    p1 = write_live_scenario(
        build_live_scenario(_req(JudgeBehavior.LOITERING), STAMP, HARBOR, OPS), tmp_path / "x"
    )
    p2 = write_live_scenario(
        build_live_scenario(_req(JudgeBehavior.LOITERING), STAMP, HARBOR, OPS), tmp_path / "y"
    )
    assert p1.read_bytes() == p2.read_bytes()


def test_name_collision_moves_the_stamp(tmp_path: Path) -> None:
    sc = build_live_scenario(_req(JudgeBehavior.FILED), STAMP, HARBOR, OPS)
    first = write_live_scenario(sc, tmp_path)
    second = write_live_scenario(sc, tmp_path)
    third = write_live_scenario(sc, tmp_path)
    assert [p.name for p in (first, second, third)] == [
        "L-20261017T120000.yaml",
        "L-20261017T120001.yaml",
        "L-20261017T120002.yaml",
    ]
    moved = load_scenario(second, HARBOR)
    assert moved.id == "L-20261017T120001"
    assert moved.registry_plans[0].plan_id == "plan-live-20261017T120001"
    assert not list(tmp_path.glob("*.tmp"))


def test_written_expected_is_empty(tmp_path: Path) -> None:
    path = write_live_scenario(
        build_live_scenario(_req(JudgeBehavior.CLOSING), STAMP, HARBOR, OPS), tmp_path
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["expected"] == {}
    assert load_scenario(path, HARBOR).expected.terminal_state is None


def test_live_seed_is_stable() -> None:
    req = _req(JudgeBehavior.CLOSING)
    assert live_seed(req) == live_seed(_req(JudgeBehavior.CLOSING))
    assert live_seed(req) != live_seed(_req(JudgeBehavior.LOITERING))
    assert 0 <= live_seed(req) < 16**8
    assert live_seed(req.model_copy(update={"seed": 42})) == 42


def test_live_dir_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SOUNDINGS_LIVE_DIR", raising=False)
    assert live_dir() == REPO / "scenarios" / "live"
    monkeypatch.setenv("SOUNDINGS_LIVE_DIR", str(tmp_path))
    assert live_dir() == tmp_path


def test_catalog_excludes_live(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from soundings.api import create_app

    live = REPO / "scenarios" / "live"
    live.mkdir(exist_ok=True)
    sc = build_live_scenario(_req(JudgeBehavior.CLOSING), STAMP, HARBOR, OPS)
    path = write_live_scenario(sc, live)
    try:
        names = [p.name for p in catalog_paths()]
        assert names and all(re.match(r"^S\d{2}-", n) for n in names)
        ids = [s["id"] for s in TestClient(create_app()).get("/api/scenarios").json()]
        assert ids == sorted(ids) and all(re.fullmatch(r"S\d{2}", i) for i in ids)
        assert ids[0] == "S01" and "S08" in ids
        assert path.stem not in ids
    finally:
        path.unlink()


def test_live_files_are_gitignored() -> None:
    import subprocess

    out = subprocess.run(
        ["git", "check-ignore", "-q", "scenarios/live/L-20261017T120000.yaml"], cwd=REPO
    )
    assert out.returncode == 0


def _panel_labels() -> list[str]:
    text = PANEL.read_text(encoding="utf-8")
    m = re.search(r"export const JUDGE_LABELS = \{(.*?)\n\} as const", text, re.S)
    assert m, "JudgePanel.tsx must export JUDGE_LABELS"
    return re.findall(r"'((?:[^'\\]|\\.)*)'", m.group(1))


def test_vocabulary() -> None:
    labels = _panel_labels()
    assert len(labels) >= 12
    for text in [*(b.value for b in JudgeBehavior), *(a.value for a in AcousticProfile), *labels]:
        assert_clean(text)
    for row in BEHAVIOR_TABLE.values():
        assert_clean(row.help)
    after = re.compile(r"\b(after (the )?handoff|take over|escort|remove)\b", re.I)
    assert not [t for t in labels if after.search(t)]
    assert {
        "Filed survey",
        "Unfiled, answers hail",
        "Closing on an asset",
        "Loitering near an asset",
        "Filed, then silent",
    } <= set(labels)
