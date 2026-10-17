"""Screenshot the registry form after filing a catalog plan and one outside-zone plan.

Usage (with the API running, e.g. ``make dev`` and ``SOUNDINGS_DB`` pointing at a tmp file):

    uv run python scripts/registry_form_shot.py --base http://localhost:8000 \
        [--plan-from S02] [--out artifacts/registry-form.png] [--executable-path BIN]

It files the first ``registry_plans`` entry of the scenario through the form, then files a
plan with area ``not_a_zone`` (an option injected into the dropdown, since the form lists
only declared security zones) to show the inline rejection, and saves a full-page PNG.
Needs a Playwright chromium build (``uv run playwright install chromium``, once). No model
calls; WO-022 reuses this script for the Phase 2 manual check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a file from anywhere

from soundings.registry.store import plans_from_scenario  # noqa: E402
from soundings.schemas._base import REPO_ROOT  # noqa: E402
from soundings.schemas.scenario import load_scenario  # noqa: E402

SCENARIO_DIR = REPO_ROOT / "scenarios"
DEFAULT_OUT = REPO_ROOT / "artifacts" / "registry-form.png"


def scenario_path(scenario_id: str) -> Path:
    matches = sorted(SCENARIO_DIR.glob(f"{scenario_id}-*.yaml"))
    if not matches:
        raise SystemExit(f"no scenario file for {scenario_id!r} under {SCENARIO_DIR}")
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--plan-from", default="S02", dest="plan_from")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--executable-path",
        default=None,
        help="a chromium binary to use instead of Playwright's pinned build (no download)",
    )
    args = parser.parse_args(argv)

    scenario = load_scenario(scenario_path(args.plan_from))
    plans = plans_from_scenario(scenario)
    if not plans:
        raise SystemExit(f"{args.plan_from} has no registry_plans")
    plan = plans[0]
    assert plan.start_at is not None and plan.end_at is not None
    offset = plan.start_at.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}"

    from playwright.sync_api import sync_playwright  # dev dependency, imported on use

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.executable_path)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            timezone_id="America/Los_Angeles",
            color_scheme="light",
        )
        page = context.new_page()
        page.goto(f"{args.base.rstrip('/')}/registry")
        page.wait_for_selector(f"#area option[value='{plan.area}']", state="attached")

        page.select_option("#area", plan.area)
        page.fill("#plan_id", plan.plan_id or "")
        page.fill("#operator", plan.operator)
        page.fill("#vehicle", plan.vehicle)
        page.fill("#modem_id", plan.modem_id)
        page.fill("#contact_channel", plan.contact_channel)
        page.fill("#start_at", plan.start_at.strftime("%Y-%m-%dT%H:%M"))
        page.fill("#end_at", plan.end_at.strftime("%Y-%m-%dT%H:%M"))
        page.fill("#utc_offset", offset)
        page.fill("#start_tick", "" if plan.start_tick is None else str(plan.start_tick))
        page.fill("#end_tick", "" if plan.end_tick is None else str(plan.end_tick))
        page.click("#submit")
        page.wait_for_function(
            "document.getElementById('result').textContent.length > 0", timeout=10_000
        )
        page.wait_for_selector(f"#plans-body code:text-is('{plan.plan_id}')", timeout=10_000)
        first_result = page.inner_text("#result")

        page.evaluate(
            """() => {
                const opt = document.createElement('option');
                opt.value = 'not_a_zone';
                opt.textContent = 'Not a declared zone (not_a_zone)';
                document.getElementById('area').appendChild(opt);
            }"""
        )
        page.select_option("#area", "not_a_zone")
        page.fill("#plan_id", "")
        page.click("#submit")
        page.wait_for_function(
            "document.getElementById('result').textContent.includes('area_not_security_zone')",
            timeout=10_000,
        )
        second_result = page.inner_text("#result")
        page.screenshot(path=str(args.out), full_page=True)
        browser.close()

    print(f"filed: {first_result}")
    print(f"rejected: {second_result}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
