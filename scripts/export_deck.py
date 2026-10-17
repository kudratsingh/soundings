"""Print the rendered deck to ``docs/deck/deck.pdf``, one 1920x1080 page per slide (WO-090).

Usage::

    uv run python scripts/export_deck.py [--html docs/deck/index.html]
        [--pdf docs/deck/deck.pdf] [--executable-path BIN]

Uses Playwright (Python, a dev dependency) with a chromium build that is already on this
machine: ``--executable-path``, else the newest cached ``chromium_headless_shell-*`` (then
``chromium-*``) under ``$PLAYWRIGHT_BROWSERS_PATH`` or ``~/Library/Caches/ms-playwright``
(``~/.cache/ms-playwright`` on Linux), else Playwright's own pinned build if it is
installed. It never downloads a browser: with none found it exits 2. No network: the deck
is one self-contained file opened from disk.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HTML = REPO_ROOT / "docs" / "deck" / "index.html"
DEFAULT_PDF = REPO_ROOT / "docs" / "deck" / "deck.pdf"
SLIDE_W, SLIDE_H = 1920, 1080

_PAGE_RE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def count_pdf_pages(path: Path) -> int:
    """Page objects in a PDF (``/Type /Page``, not ``/Pages``); no PDF library needed."""
    return len(_PAGE_RE.findall(path.read_bytes()))


def _cache_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0":
        roots.append(Path(env))
    home = Path.home()
    roots += [home / "Library" / "Caches" / "ms-playwright", home / ".cache" / "ms-playwright"]
    return roots


def _build_number(path: Path) -> int:
    m = re.search(r"-(\d+)$", path.name)
    return int(m.group(1)) if m else -1


def find_cached_chromium() -> Path | None:
    """The newest cached headless shell (or full chromium) binary, if any."""
    patterns = (
        ("chromium_headless_shell-*", ("chrome-headless-shell-*/chrome-headless-shell",)),
        (
            "chromium-*",
            (
                "chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
                "chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/"
                "Google Chrome for Testing",
                "chrome-linux*/chrome",
            ),
        ),
    )
    for root in _cache_roots():
        if not root.is_dir():
            continue
        for dir_glob, bin_globs in patterns:
            for build in sorted(root.glob(dir_glob), key=_build_number, reverse=True):
                for bin_glob in bin_globs:
                    for exe in sorted(build.glob(bin_glob)):
                        if exe.is_file() and os.access(exe, os.X_OK):
                            return exe
    return None


def export(html_path: Path, pdf_path: Path, executable: Path | None) -> int:
    from playwright.sync_api import sync_playwright  # dev dependency, imported on use

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        exe = executable or find_cached_chromium()
        if exe is None and not Path(pw.chromium.executable_path).exists():
            print(
                "error: no cached chromium found (looked under "
                + ", ".join(str(r) for r in _cache_roots())
                + "); pass --executable-path. This script never downloads a browser.",
                file=sys.stderr,
            )
            return 2
        browser = pw.chromium.launch(executable_path=str(exe) if exe else None)
        try:
            page = browser.new_page(viewport={"width": SLIDE_W, "height": SLIDE_H})
            page.goto(html_path.resolve().as_uri())
            page.emulate_media(media="print")
            page.pdf(
                path=str(pdf_path),
                width=f"{SLIDE_W}px",
                height=f"{SLIDE_H}px",
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                prefer_css_page_size=True,
            )
        finally:
            browser.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--executable-path", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.html.exists():
        print(f"error: {args.html} not found; run scripts/build_deck.py first", file=sys.stderr)
        return 1
    code = export(args.html, args.pdf, args.executable_path)
    if code == 0:
        print(f"deck: wrote {args.pdf} ({count_pdf_pages(args.pdf)} pages)")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
