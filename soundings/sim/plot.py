"""Draw one scenario run over the harbor map: ``make sim-plot scenario=S04``.

``python -m soundings.sim.plot --scenario S04 --out artifacts/S04.png [--seed N]``
writes a PNG with zone polygons, sensor nodes with their ``range_m`` and ``2 * range_m``
circles, assets, the ground-truth paths, and observed points coloured by observed
contact id (false alarms folded into one grey class). Axes are local metres about the
harbor origin, so range circles are true circles.

``matplotlib`` is a dev dependency imported lazily inside ``plot_run`` with the Agg
backend, so importing ``soundings.sim`` never needs it.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from soundings.schemas.geo import to_local_m
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.sim.__main__ import run_ref
from soundings.sim.types import SimRun

# Categorical slots in fixed order (validated reference palette, light mode).
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
MUTED = "#a3a29c"
SURFACE = "#fcfcfb"
ZONE_FILL = "#ecebe7"


def plot_run(run: SimRun, harbor: Harbor, out: Path, title: str) -> Path:
    """Render ``run`` over ``harbor`` to ``out`` (PNG)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Polygon

    fig, ax = plt.subplots(figsize=(9, 9), dpi=120)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    for zone in sorted(harbor.zones, key=lambda z: z.zone_id):
        pts = [to_local_m(lat, lon) for lat, lon in zone.polygon]
        ax.add_patch(Polygon(pts, closed=True, facecolor=ZONE_FILL, edgecolor=MUTED, linewidth=0.8))
        # Label in the zone's top-left corner, clear of the nodes and assets.
        lx = min(p[0] for p in pts) + 25.0
        ly = max(p[1] for p in pts) - 25.0
        ax.text(lx, ly, zone.zone_id, fontsize=7, color=TEXT_SECONDARY, ha="left", va="top")

    for node in sorted(harbor.sensor_nodes, key=lambda n: n.node_id):
        x, y = to_local_m(node.lat, node.lon)
        for radius, style in ((node.range_m, "--"), (2 * node.range_m, ":")):
            ax.add_patch(
                Circle(
                    (x, y), radius, fill=False, edgecolor=TEXT_SECONDARY, linestyle=style, lw=0.8
                )
            )
        ax.plot(x, y, marker="s", color=TEXT_PRIMARY, markersize=7, zorder=5)
        ax.annotate(
            node.node_id,
            (x, y),
            xytext=(-8, -12),
            textcoords="offset points",
            fontsize=8,
            ha="right",
            color=TEXT_PRIMARY,
        )

    for asset in sorted(harbor.assets, key=lambda a: a.asset_id):
        x, y = to_local_m(asset.lat, asset.lon)
        ax.plot(x, y, marker="^", color=TEXT_SECONDARY, markersize=7, zorder=5)
        ax.annotate(
            asset.asset_id,
            (x, y),
            xytext=(7, 3),
            textcoords="offset points",
            fontsize=7,
            color=TEXT_SECONDARY,
        )

    # Ground-truth paths (real contacts only; false alarms would clutter the map).
    first_truth = True
    for gt_id in sorted(run.truth_paths):
        if gt_id.startswith("fa-"):
            continue
        xy = [to_local_m(p.lat, p.lon) for p in run.truth_paths[gt_id]]
        ax.plot(
            [p[0] for p in xy],
            [p[1] for p in xy],
            color=TEXT_PRIMARY,
            lw=1.5,
            zorder=7,
            label="ground-truth path" if first_truth else None,
        )
        first_truth = False

    real = [c for c in sorted(run.tracks) if not run.contact_truth[c].gt_id.startswith("fa-")]
    false_alarms = [c for c in sorted(run.tracks) if c not in real]
    for i, cid in enumerate(real):
        pts = [to_local_m(p.lat, p.lon) for p in run.tracks[cid].points]
        ax.scatter(
            [p[0] for p in pts],
            [p[1] for p in pts],
            s=14,
            color=SERIES[i % len(SERIES)],
            edgecolors=SURFACE,
            linewidths=0.5,
            zorder=6,
            label=f"{cid} (observed, {run.contact_truth[cid].gt_id})",
        )
    if false_alarms:
        xs: list[float] = []
        ys: list[float] = []
        for cid in false_alarms:
            for p in run.tracks[cid].points:
                x, y = to_local_m(p.lat, p.lon)
                xs.append(x)
                ys.append(y)
        ax.scatter(
            xs, ys, s=8, color=MUTED, zorder=5, label=f"false alarms ({len(false_alarms)} ids)"
        )

    xs_all = [to_local_m(lat, lon)[0] for z in harbor.zones for lat, lon in z.polygon]
    ys_all = [to_local_m(lat, lon)[1] for z in harbor.zones for lat, lon in z.polygon]
    pad = 300.0
    ax.set_xlim(min(xs_all) - pad, max(xs_all) + pad)
    ax.set_ylim(min(ys_all) - pad, max(ys_all) + pad)
    ax.set_aspect("equal")
    ax.grid(color=ZONE_FILL, lw=0.5)
    for spine in ax.spines.values():
        spine.set_color(MUTED)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=8)
    ax.set_xlabel("metres east of harbor origin", color=TEXT_SECONDARY)
    ax.set_ylabel("metres north of harbor origin", color=TEXT_SECONDARY)
    ax.set_title(title, color=TEXT_PRIMARY, fontsize=11, loc="left")
    ax.legend(loc="lower left", fontsize=8, frameon=True, facecolor=SURFACE, edgecolor=MUTED)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Fixed metadata so the PNG bytes do not carry a build time or version string.
    fig.tight_layout()
    fig.savefig(out, format="png", facecolor=SURFACE, metadata={"Software": None})
    plt.close(fig)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m soundings.sim.plot", description=__doc__)
    parser.add_argument("--scenario", required=True, help="smoke, a catalog id (S04), or a path")
    parser.add_argument("--out", required=True, help="output PNG path")
    parser.add_argument("--seed", type=int, default=None, help="seed (default: the scenario's)")
    parser.add_argument("--ticks", type=int, default=None, help="ticks (default: the scenario's)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from soundings.sim.scenario import UnknownScenarioError, resolve_scenario
    from soundings.sim.smoke import smoke_harbor

    try:
        if args.scenario == "smoke":
            scenario = resolve_scenario("smoke")
            harbor = smoke_harbor()
        else:
            scenario = resolve_scenario(args.scenario)
            harbor = load_harbor()
    except UnknownScenarioError as exc:
        print(f"unknown scenario {args.scenario!r}: {exc}", file=sys.stderr)
        return 2
    seed = scenario.seed if args.seed is None else args.seed
    run = run_ref(args.scenario, seed, args.ticks)
    minutes = run.n_ticks * run.tick_s / 60.0
    title = (
        f"{scenario.id}: {scenario.name}  (seed {seed}, {run.n_ticks} ticks, "
        f"{math.floor(minutes)} min, simplified harbor for demonstration)"
    )
    out = plot_run(run, harbor, Path(args.out), title)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
