"""``track_hash``: one content hash per run, over what the sim reports (CLAUDE.md 2.4).

The hash covers the observed tracks, the detections, and the observed-to-truth map,
each sorted by observed id (detections by tick, then observed id), serialized with
``soundings.canonical.canonical_json`` (sorted keys, floats at 6 dp). Two runs with the
same scenario and seed give the same hex digest, byte for byte, on any machine.
"""

from __future__ import annotations

from typing import Any

from soundings.canonical import stable_hash
from soundings.sim.types import SimRun


def track_hash_payload(run: SimRun) -> dict[str, Any]:
    """The exact structure ``track_hash`` hashes.

    ``ais_tracks`` (WO-080) is added only when the run has a vessel feed, so runs without
    vessels (S01..S08) hash exactly as before.
    """
    payload: dict[str, Any] = {
        "scenario_id": run.scenario_id,
        "seed": run.seed,
        "tick_s": run.tick_s,
        "n_ticks": run.n_ticks,
        "tracks": [run.tracks[cid] for cid in sorted(run.tracks)],
        "detections": sorted(run.detections, key=lambda d: (d.tick, d.observed_contact_id)),
        "contact_truth": {cid: run.contact_truth[cid] for cid in sorted(run.contact_truth)},
    }
    if run.ais_tracks:
        payload["ais_tracks"] = {k: list(run.ais_tracks[k]) for k in sorted(run.ais_tracks)}
    return payload


def track_hash(run: SimRun) -> str:
    """``stable_hash`` (sha256 hex) of ``track_hash_payload(run)``."""
    return stable_hash(track_hash_payload(run))
