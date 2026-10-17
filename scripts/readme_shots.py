"""Capture the three README screenshots from the watch-officer UI (WO-091).

Usage (after ``make web``; no server needs to be running):

    uv run python scripts/readme_shots.py [--out docs/screenshots] [--executable-path BIN]

Starts the API + built UI on a free loopback port, opens it in a dark-scheme headless
chromium with every non-loopback request aborted (map tiles off, no network), and saves:

1. ``01-map-evidence.png``   S05 (Harbor Night): a contact on the map and the evidence ledger
2. ``02-rung-mid-run.png``   S01 (Tuesday): the rung indicator at ``ask`` mid-run
3. ``03-sitrep-or-lost.png`` S02: the LOST_VEHICLE notice in the SITREP drawer

Chromium is never downloaded: the newest cached ``chromium_headless_shell-*`` under the
Playwright cache is reused unless ``--executable-path`` is given. No model calls.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # run as a file from anywhere

WEB_DIST = REPO / "web" / "dist"
DEFAULT_OUT = REPO / "docs" / "screenshots"
VIEWPORT = {"width": 1440, "height": 860}
LOOPBACK = {"127.0.0.1", "localhost"}
MAX_BYTES = 400 * 1024


def find_chromium() -> str | None:
    roots = [Path.home() / "Library" / "Caches" / "ms-playwright"]
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for root in roots:
        for build in sorted(root.glob("chromium_headless_shell-*"), reverse=True):
            for name in ("chrome-headless-shell", "headless_shell"):
                hits = sorted(build.glob(f"*/{name}"))
                if hits:
                    return str(hits[0])
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_server() -> str:
    import uvicorn

    from soundings.api import create_app

    port = _free_port()
    config = uvicorn.Config(create_app(WEB_DIST), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise SystemExit("server did not start within 20 s")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}"


def _offline(route: Any) -> None:
    if urlparse(route.request.url).hostname in LOOPBACK:
        route.continue_()
    else:
        route.abort()


def _page(browser: Any) -> Any:
    page = browser.new_page(viewport=VIEWPORT, color_scheme="dark")
    page.route("**/*", _offline)
    return page


def _save(page: Any, path: Path) -> None:
    page.screenshot(path=str(path))
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise SystemExit(f"{path.name} is {size} bytes (> {MAX_BYTES})")
    print(f"wrote {path} ({size // 1024} KB)")


def capture(base: str, out: Path, executable_path: str | None) -> None:
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable_path)

        page = _page(browser)
        page.goto(f"{base}/?scenario=S05&seed=11&speed=20&tiles=off")
        page.wait_for_selector("[data-testid=contact-marker]", timeout=30_000)
        page.wait_for_function(
            "/ASKING|INVESTIGATING/.test("
            "document.querySelector('[data-testid=state-badge]')?.innerText ?? '')",
            timeout=120_000,
        )
        _save(page, out / "01-map-evidence.png")
        page.close()

        page = _page(browser)
        page.goto(f"{base}/?scenario=S01&seed=1&speed=20&tiles=off")
        page.wait_for_selector("[data-testid=rung-active][data-rung=ask]", timeout=60_000)
        _save(page, out / "02-rung-mid-run.png")
        page.close()

        page = _page(browser)
        page.goto(f"{base}/?scenario=S02&seed=3&speed=40&tiles=off")
        page.wait_for_function(
            "document.querySelector('[data-testid=state-badge]')?.innerText.includes('LOST_VEHICLE')",
            timeout=120_000,
        )
        page.click("[data-testid=sitrep-open]")
        page.wait_for_selector("[data-testid=sitrep-drawer]")
        page.wait_for_timeout(500)
        _save(page, out / "03-sitrep-or-lost.png")
        page.close()

        browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--executable-path", default=None)
    args = parser.parse_args(argv)
    if not (WEB_DIST / "index.html").is_file():
        raise SystemExit("web/dist is not built (run `make web`)")
    exe = args.executable_path or find_chromium()
    if exe is None:
        raise SystemExit("no cached chromium (run `uv run playwright install chromium` once)")
    capture(start_server(), args.out, exe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
