"""YAML on disk to tracks, for every catalog scenario (WO-011 integration exit test).

For each S01..S08 at its own seed: ``run_scenario`` from the file (WO-010 loader and
harbor, current field, detection, acoustics, toggles) runs ``duration_ticks`` ticks,
detects a real ground-truth contact (not only false alarms), returns tracks that
re-validate as WO-010 ``Track``, maps every observed id to a known ``gt_id`` or
``fa-*``, and gives the same ``track_hash`` twice. The whole file runs in under 60 s.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from soundings.schemas.harbor import load_harbor
from soundings.schemas.scenario import load_scenario
from soundings.schemas.track import Track
from soundings.sim.hashing import track_hash
from soundings.sim.run import run_scenario
from soundings.sim.scenario import catalog_paths

_MODULE_START: list[float] = []


@pytest.fixture(scope="module", autouse=True)
def _module_clock() -> None:
    """Set once, before the first test of this module runs."""
    _MODULE_START.append(time.perf_counter())


@pytest.mark.parametrize("path", catalog_paths(), ids=lambda p: p.name[:3])
def test_yaml_to_tracks(path: Path) -> None:
    harbor = load_harbor()
    scenario = load_scenario(path, harbor)
    run = run_scenario(scenario, harbor=harbor)
    assert run.scenario_id == scenario.id and run.seed == scenario.seed
    assert run.n_ticks == scenario.duration_ticks
    assert run.tick_s == scenario.tick_s

    scenario_ids = {c.id for c in scenario.contacts}
    real = [d for d in run.detections if not d.is_false_alarm]
    assert real, "no detection of a ground-truth contact"
    assert {d.gt_id for d in real} <= scenario_ids

    for cid, track in run.tracks.items():
        assert track.contact_id == cid
        again = Track.model_validate_json(track.model_dump_json())
        assert again == track
        assert Track.model_validate(track.model_dump()) == track
        assert all(0 <= p.tick < run.n_ticks for p in track.points)

    assert set(run.contact_truth) == set(run.tracks)
    for truth in run.contact_truth.values():
        assert truth.gt_id in scenario_ids or truth.gt_id.startswith("fa-")
        assert truth.gt_id in run.truth_paths
    for d in run.detections:
        assert run.contact_truth[d.observed_contact_id].gt_id == d.gt_id
        assert d.is_false_alarm == d.gt_id.startswith("fa-")

    assert track_hash(run_scenario(load_scenario(path, harbor), harbor=harbor)) == track_hash(run)


def test_ground_truth_kept_apart_from_tracks() -> None:
    (path,) = [p for p in catalog_paths() if p.name.startswith("S04")]
    run = run_scenario(load_scenario(path))
    dumped = "".join(t.model_dump_json() for t in run.tracks.values())
    assert "gt-" not in dumped and "fa-" not in dumped and "is_false_alarm" not in dumped


def test_whole_file_under_60_s() -> None:
    # Last in this module (pytest keeps file order): the time since import covers every
    # test above plus one more full pass over the catalog.
    for path in catalog_paths():
        run_scenario(load_scenario(path))
    assert time.perf_counter() - _MODULE_START[0] < 60.0
