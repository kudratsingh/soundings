"""Browser smoke fixtures (WO-060): uvicorn on a free loopback port, a cached chromium.

No browser is ever downloaded. Chromium is Playwright's pinned build when it is installed,
otherwise the newest cached ``chromium_headless_shell-*`` under the Playwright cache (the
same reuse ``scripts/registry_form_shot.py --executable-path`` documents). Without one, or
without a built ``web/dist``, the tests skip; ``SOUNDINGS_E2E_REQUIRED=1`` turns that skip
into a failure.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
WEB_DIST = REPO / "web" / "dist"


def _skip(reason: str) -> None:
    if os.environ.get("SOUNDINGS_E2E_REQUIRED") == "1":
        pytest.fail(f"e2e required but unavailable: {reason}")
    pytest.skip(reason)


def _cache_roots() -> list[Path]:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    roots = [Path(env)] if env and env != "0" else []
    home = Path.home()
    if sys.platform == "darwin":
        roots.append(home / "Library" / "Caches" / "ms-playwright")
    roots.append(home / ".cache" / "ms-playwright")
    return roots


def find_chromium() -> str | None:
    """A chromium binary to launch, or None. Never downloads one."""
    for root in _cache_roots():
        builds = sorted(root.glob("chromium_headless_shell-*"), reverse=True)
        for build in builds:
            for name in ("chrome-headless-shell", "headless_shell"):
                hits = sorted(build.glob(f"*/{name}"))
                if hits:
                    return str(hits[0])
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def live_server() -> Iterator[str]:
    if not (WEB_DIST / "index.html").is_file():
        _skip("web/dist is not built (run `pnpm -C web build`)")
    import uvicorn

    from soundings.api import create_app

    port = _free_port()
    config = uvicorn.Config(
        create_app(WEB_DIST),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ws="websockets-sansio",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("uvicorn did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _skip("playwright is not installed")
    with sync_playwright() as pw:
        path: str | None = None
        if not Path(pw.chromium.executable_path).exists():
            path = find_chromium()
            if path is None:
                _skip("no cached chromium (uv run playwright install chromium, once)")
        b = pw.chromium.launch(executable_path=path)
        yield b
        b.close()
