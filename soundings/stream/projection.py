"""WO-084's 60 s projection per streamed reading (WO-086).

``projection_series`` runs WO-084's causal Kalman filter once over an observed track and
reads ``projection_view``'s fields at every reading, so entry ``i`` equals
``projection_view`` of the first ``i + 1`` readings without re-filtering each prefix.
Observed tracks only; never ground truth. A stress-test estimate: it assumes the contact
holds its velocity, and its ellipse covers only that model's uncertainty.
"""

from __future__ import annotations

import math

from soundings.canonical import round6
from soundings.investigation.kalman import (
    KalmanConfig,
    ProjectionView,
    _track_columns,
    default_kalman_config,
    initial_state,
    predict,
    project,
    reading_sigmas,
    update,
)
from soundings.schemas.geo import from_local_m
from soundings.schemas.track import Track


def projection_series(track: Track, cfg: KalmanConfig | None = None) -> list[ProjectionView | None]:
    """``projection_view`` of every prefix of ``track`` (one per reading), in one causal
    pass: entry ``i`` equals ``projection_view`` of the first ``i + 1`` readings."""
    cfg = default_kalman_config() if cfg is None else cfg
    pts = track.points
    if not pts:
        return []
    cols = _track_columns(track)
    sig = reading_sigmas(cols, cfg)
    t = cols.t_s
    state = initial_state(float(t[0]), (float(cols.x_m[0]), float(cols.y_m[0])), float(sig[0]), cfg)
    out: list[ProjectionView | None] = [None]
    for i in range(1, len(pts)):
        state = predict(state, float(t[i] - t[i - 1]), cfg)
        state = update(state, (float(cols.x_m[i]), float(cols.y_m[i])), float(sig[i]), cfg)
        x, y, vx, vy = state.x
        proj = project(state, cfg.projection_s, cfg)
        lat, lon = from_local_m(x, y)
        proj_lat, proj_lon = from_local_m(proj.x_m, proj.y_m)
        out.append(
            ProjectionView(
                contact_id=track.contact_id,
                tick=pts[i].tick,
                lat=round6(lat),
                lon=round6(lon),
                vx=round6(vx),
                vy=round6(vy),
                speed_mps=round6(math.hypot(vx, vy)),
                heading_deg=round6(math.degrees(math.atan2(vx, vy)) % 360.0) % 360.0,
                proj_lat=round6(proj_lat),
                proj_lon=round6(proj_lon),
                ellipse=proj.ellipse,
            )
        )
    return out


__all__ = ["projection_series"]
