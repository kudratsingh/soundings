"""WO-060 design pass B1: every text/background pair in the token file is at least 4.5:1.

``web/src/theme.css`` is the one file that defines colours. Its header lists each text /
background pair the screen uses as ``pair: --text on --bg``; this test resolves the tokens
(one level of ``var()``) and checks the WCAG 2.x contrast ratio of each pair.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
THEME = REPO / "web" / "src" / "theme.css"
MIN_RATIO = 4.5
ACCENT_TEXT = (
    "--text",
    "--text-muted",
    "--threat",
    "--benign",
    "--evidence",
    "--assumption",
    "--derived",
)

TOKEN = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);", re.MULTILINE)
PAIR = re.compile(r"pair:\s*(--[a-z0-9-]+)\s+on\s+(--[a-z0-9-]+)")
HEX = re.compile(r"^#([0-9a-fA-F]{6})$")


def _tokens() -> dict[str, str]:
    raw = dict(TOKEN.findall(THEME.read_text(encoding="utf-8")))
    out: dict[str, str] = {}
    for name, value in raw.items():
        value = value.strip()
        ref = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value)
        out[name] = raw[ref.group(1)].strip() if ref else value
    return out


def _pairs() -> list[tuple[str, str]]:
    return PAIR.findall(THEME.read_text(encoding="utf-8"))


def _luminance(hex_colour: str) -> float:
    m = HEX.match(hex_colour)
    assert m, f"not a #rrggbb colour: {hex_colour}"
    channels = [int(m.group(1)[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_contrast_helper_matches_wcag() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)


def test_token_file_declares_the_palette() -> None:
    tokens = _tokens()
    for name in ("--bg", "--surface", "--raised", *ACCENT_TEXT):
        assert name in tokens, name
    pairs = _pairs()
    assert len(pairs) >= 20
    used = {fg for fg, _ in pairs}
    assert set(ACCENT_TEXT) <= used


@pytest.mark.parametrize(("fg", "bg"), _pairs())
def test_every_text_background_pair_is_readable(fg: str, bg: str) -> None:
    tokens = _tokens()
    ratio = contrast(tokens[fg], tokens[bg])
    assert ratio >= MIN_RATIO, f"{fg} ({tokens[fg]}) on {bg} ({tokens[bg]}) is {ratio:.2f}:1"


def test_no_other_stylesheet_defines_the_palette() -> None:
    """Colours live in theme.css; the layout sheet only reads the variables."""
    sheet = (REPO / "web" / "src" / "index.css").read_text(encoding="utf-8")
    assert ":root" not in sheet
