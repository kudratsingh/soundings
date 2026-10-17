"""WO-062P browser smoke: presenter mode captions, Space advances, reset in under 2 s.

Opens ``/?presenter=1&tiles=off`` with every non-loopback request aborted. The caption bar
shows step 0's caption and ``1 / 7``; Space lands on step 1 (``2 / 7``); ``reset to step
0`` brings back step 0's caption and a fresh run of its scenario at its seed within 2 s.
Skips without a cached chromium or a built ``web/dist`` (see ``conftest.py``).
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from soundings.demo.script import DemoScript
from tests.e2e.conftest import REPO

LOOPBACK = {"127.0.0.1", "localhost"}
SCRIPT = DemoScript.load(REPO / "docs" / "demo-script.yaml")


def _offline(route: Any) -> None:
    if urlparse(route.request.url).hostname in LOOPBACK:
        route.continue_()
    else:
        route.abort()


def test_presenter_steps_and_reset(live_server: str, browser: Any) -> None:
    n = len(SCRIPT.steps)
    first, second = SCRIPT.steps[0], SCRIPT.steps[1]
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    page.route("**/*", _offline)
    page.goto(f"{live_server}/?presenter=1&tiles=off")
    bar = page.locator("[data-testid=presenter-bar]")
    caption = page.locator("[data-testid=presenter-caption]")
    counter = page.locator("[data-testid=presenter-counter]")
    page.wait_for_function(
        "c => document.querySelector('[data-testid=presenter-caption]')?.textContent === c",
        arg=first.caption,
        timeout=20_000,
    )
    assert counter.inner_text() == f"1 / {n}"
    page.wait_for_selector("[data-testid=presenter-bar][data-applied='0']", timeout=20_000)
    first_run = bar.get_attribute("data-run-id") or ""
    assert f"-{first.scenario}-s" in first_run
    # The caption is 44 px and at most two lines.
    size = page.evaluate(
        "() => getComputedStyle(document.querySelector('[data-testid=presenter-caption]')).fontSize"
    )
    assert size == "44px"
    height = caption.bounding_box()["height"]
    assert height <= 2 * 44 * 1.25

    page.keyboard.press("Space")
    page.wait_for_function(
        "c => document.querySelector('[data-testid=presenter-counter]')?.textContent === c",
        arg=f"2 / {n}",
        timeout=5_000,
    )
    assert caption.inner_text() == second.caption
    page.wait_for_selector("[data-testid=presenter-chips]", timeout=5_000)
    page.keyboard.press("ArrowLeft")
    page.wait_for_function(
        "c => document.querySelector('[data-testid=presenter-counter]')?.textContent === c",
        arg=f"1 / {n}",
        timeout=5_000,
    )
    page.wait_for_selector("[data-testid=presenter-bar][data-applied='0']", timeout=20_000)
    page.keyboard.press("ArrowRight")
    page.wait_for_selector("[data-testid=presenter-bar][data-applied='1']", timeout=5_000)
    before = bar.get_attribute("data-run-id") or ""

    started = time.monotonic()
    page.click("[data-testid=presenter-reset]")
    page.wait_for_function(
        """([cap, old]) => {
            const bar = document.querySelector('[data-testid=presenter-bar]')
            const c = document.querySelector('[data-testid=presenter-caption]')
            return c?.textContent === cap && bar?.getAttribute('data-applied') === '0'
              && bar.getAttribute('data-run-id') !== old
              && bar.getAttribute('data-following') === '1'
        }""",
        arg=[first.caption, before],
        timeout=5_000,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"reset took {elapsed:.2f} s"
    assert counter.inner_text() == f"1 / {n}"
    after = bar.get_attribute("data-run-id") or ""
    assert after != before and f"-{first.scenario}-s{first.seed or 1}" in after
    label = page.locator("[data-testid=scenario-label]").inner_text()
    assert label.startswith(f"{first.scenario} ·")

    # The presenter log saw every landing, with the script's captions.
    with urlopen(f"{live_server}/api/presenter-log") as res:  # loopback only
        log = json.loads(res.read())
    kinds = [(e["kind"], e["index"]) for e in log]
    assert ("step", 1) in kinds and kinds[-1] == ("reset", 0)
    assert all(e["caption"] == SCRIPT.steps[e["index"]].caption for e in log)
    toggle = page.locator("[data-testid=sfx-toggle]")
    assert toggle.get_attribute("data-muted") == "0"  # presenter mode defaults to sound on
    page.close()
