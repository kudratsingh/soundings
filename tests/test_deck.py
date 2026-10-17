"""WO-090: the pitch deck takes every number from a file, and the Devpost fields are ready.

``tests/fixtures/deck/report.json`` is a SYNTHETIC report in the WO-051 ``EvalReport``
shape; its figures are test data only and never reach a committed slide.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from soundings.schemas.denylist import find_denylisted

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "deck" / "report.json"
TEMPLATE = REPO / "docs" / "deck" / "template.html"
INDEX = REPO / "docs" / "deck" / "index.html"
DEVPOST = REPO / "docs" / "DEVPOST.md"
DECK_INPUTS = REPO / "docs" / "DECK-INPUTS.md"
DECK_YAML = REPO / "docs" / "deck" / "deck.yaml"
LADDER = yaml.safe_load((REPO / "config" / "ladder.yaml").read_text())

SLIDE_ORDER = [
    "title",
    "seat",
    "ladder",
    "two-lives",
    "demo",
    "boundary",
    "evidence",
    "twists",
    "runs-on",
    "honesty",
    "next",
]
VOID = {"img", "br", "hr", "meta", "link", "input", "source", "wbr"}


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


build_deck = _load("build_deck")
export_deck = _load("export_deck")


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


class _Walker(HTMLParser):
    """Collects (a) text outside style/script and outside any data-src element and
    (b) every data-src element's source and text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.free_text: list[str] = []
        self.sourced: list[tuple[str, str]] = []
        self.slides: list[tuple[str, str]] = []
        self._stack: list[str] = []
        self._src_depth: int | None = None
        self._src_buf: list[str] = []
        self._src: str = ""
        self._skip: int = 0
        self._slide_depth: int | None = None
        self._slide_buf: list[str] = []
        self._slide_name = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in ("style", "script"):
            self._skip += 1
        if tag == "section" and "slide" in (a.get("class") or "").split():
            self._slide_depth = len(self._stack)
            self._slide_buf = []
            self._slide_name = a.get("data-slide") or ""
        if tag in VOID:
            if a.get("data-src"):
                self.sourced.append((a["data-src"] or "", ""))
            return
        if a.get("data-src") and self._src_depth is None:
            self._src_depth = len(self._stack)
            self._src_buf = []
            self._src = a["data-src"] or ""
        self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if a.get("data-src"):
            self.sourced.append((a["data-src"] or "", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script"):
            self._skip -= 1
        if tag in VOID or not self._stack:
            return
        self._stack.pop()
        depth = len(self._stack)
        if self._src_depth is not None and depth == self._src_depth:
            self.sourced.append((self._src, "".join(self._src_buf)))
            self._src_depth = None
        if self._slide_depth is not None and depth == self._slide_depth and tag == "section":
            self.slides.append((self._slide_name, "".join(self._slide_buf).strip()))
            self._slide_depth = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._slide_depth is not None:
            self._slide_buf.append(data)
        if self._src_depth is not None:
            self._src_buf.append(data)
        else:
            self.free_text.append(data)


def walk(page: str) -> _Walker:
    w = _Walker()
    w.feed(page)
    w.close()
    return w


def template_text(template: str) -> str:
    """Template text outside <style>, <script>, comments and tags."""
    t = re.sub(r"<style.*?</style>", " ", template, flags=re.S)
    t = re.sub(r"<script.*?</script>", " ", t, flags=re.S)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    return re.sub(r"<[^>]+>", " ", t)


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


@pytest.fixture(scope="module")
def page_with(tmp_path_factory: pytest.TempPathFactory) -> str:
    out = tmp_path_factory.mktemp("deck") / "index.html"
    page: str = build_deck.build(FIXTURE, out, devpost=None, curve=out.parent / "none.png")
    return page


@pytest.fixture(scope="module")
def page_without(tmp_path_factory: pytest.TempPathFactory) -> str:
    out = tmp_path_factory.mktemp("deck") / "index.html"
    page: str = build_deck.build(
        out.parent / "missing.json", out, devpost=None, curve=out.parent / "none.png"
    )
    return page


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["page_with", "page_without"])
def test_eleven_slides_in_hub_order(which: str, request: pytest.FixtureRequest) -> None:
    w = walk(request.getfixturevalue(which))
    assert [name for name, _ in w.slides] == SLIDE_ORDER
    demo = dict(w.slides)["demo"]
    assert demo == "", "slide 5 is the live-demo placeholder and holds no content"


def test_keyboard_navigation_present() -> None:
    t = TEMPLATE.read_text()
    for key in ("ArrowRight", "ArrowLeft", "' '", "Home", "End"):
        assert key in t
    assert "@page { size: 1920px 1080px" in t


def test_fmt_value() -> None:
    f = build_deck.fmt_value
    assert [f(0.25), f(0.35), f(0.7), f(412.5), f(77.7777), f(3), f(0.0), f(None)] == [
        "0.25",
        "0.35",
        "0.7",
        "412.5",
        "77.8",
        "3",
        "0",
        "n/a",
    ]
    assert f(1500) == "1,500"


# ---------------------------------------------------------------------------
# Every number comes from a file
# ---------------------------------------------------------------------------


def test_template_has_no_hand_typed_figures() -> None:
    text = template_text(TEMPLATE.read_text())
    assert not re.search(r"\d", text), re.findall(r".{0,30}\d.{0,30}", text)
    assert "%" not in text


@pytest.mark.parametrize("which", ["page_with", "page_without"])
def test_no_hand_typed_numbers(which: str, request: pytest.FixtureRequest) -> None:
    w = walk(request.getfixturevalue(which))
    free = "".join(w.free_text)
    assert not re.search(r"\d", free), re.findall(r".{0,30}\d.{0,30}", free)
    for src, _text in w.sourced:
        assert re.match(r"^(report|config|inputs|deck|file):", src), src


def test_report_values_match_fixture(page_with: str, report: dict[str, Any]) -> None:
    w = walk(page_with)
    checked = 0
    for src, text in w.sourced:
        if not src.startswith("report:"):
            continue
        path = src.removeprefix("report:")
        if path.startswith("threshold_curve"):
            continue
        value = build_deck.dig(report, path)
        if path == "seeds":
            assert text == ",".join(str(s) for s in value)
        elif path == "inputs_sha256":
            assert value.startswith(text) and len(text) >= 8
        elif isinstance(value, dict):
            continue
        else:
            assert text.startswith(build_deck.fmt_value(value)), (path, text, value)
        checked += 1
    assert checked >= 30
    assert build_deck.PENDING not in page_with


def test_directive_metrics_on_evidence_slide(page_with: str) -> None:
    srcs = {src for src, _ in walk(page_with).sourced}
    for policy in ("soundings", "bang", "human_queue"):
        for metric in (
            "missed_threats",
            "median_s_seen_to_handled",
            "interruptions_per_hour",
            "shutdown_minutes",
        ):
            assert f"report:outcomes.{policy}.{metric}" in srcs
    assert "report:outcomes.soundings.ask" in srcs
    assert "report:outcomes.bang.hold" in srcs
    assert "report:outcomes.human_queue.hand_to_person" in srcs
    assert "report:outcomes.bang.false_shutdowns" in srcs
    assert build_deck.CURVE_PENDING in page_with


def test_config_values_match_ladder(page_with: str) -> None:
    n = 0
    for src, text in walk(page_with).sourced:
        if src.startswith("config:ladder.criteria") or src.startswith("config:ladder.stopping"):
            value = build_deck.dig(LADDER, src.removeprefix("config:ladder."))
            assert text.startswith(build_deck.fmt_value(value)), (src, text)
            n += 1
    assert n == 7


def test_boundary_table_equals_config(page_with: str) -> None:
    cells = {src: text for src, text in walk(page_with).sourced if "boundary" in src}
    for rung in LADDER["rungs"]:
        for col in ("platform_alone", "standing_orders", "person_approves"):
            text = cells[f"config:ladder.boundary.{rung}.{col}"]
            items = LADDER["boundary"][rung][col] or []
            if items:
                assert text == "".join(build_deck.pretty(i) for i in items)
            else:
                assert text == "—"


def test_twist_rows_equal_deck_inputs(page_with: str) -> None:
    rows = build_deck.parse_twists(DECK_INPUTS.read_text())
    assert [r[0] for r in rows] == ["1", "2", "3"]
    sourced = dict(walk(page_with).sourced)
    for n, changed, _proof in rows:
        got = sourced[f"inputs:DECK-INPUTS.md#twists.{n}.changed"]
        if changed:
            assert got == re.sub(r"\*\*|`", "", changed)
        else:
            assert got == build_deck.TWIST_PENDING


def test_threshold_curve_embedded_when_present(tmp_path: Path) -> None:
    png = tmp_path / "curve.png"
    png.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
        )
    )
    page = build_deck.build(FIXTURE, tmp_path / "i.html", devpost=None, curve=png)
    assert 'src="data:image/png;base64,' in page
    assert build_deck.CURVE_PENDING not in page


def test_pending_policy_renders_placeholders(tmp_path: Path, report: dict[str, Any]) -> None:
    """WO-051 part A: the soundings policy is listed in policies_pending until runner v3."""
    partial = json.loads(json.dumps(report))
    del partial["outcomes"]["soundings"]
    del partial["false_alarms"]["soundings"]
    partial["policies_pending"] = {"soundings": "pending runner v3"}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(partial))
    page = build_deck.build(path, tmp_path / "i.html", devpost=None, curve=tmp_path / "x.png")
    w = walk(page)
    for src, text in w.sourced:
        if src.startswith(("report:outcomes.soundings", "report:false_alarms.soundings")):
            assert text.startswith(build_deck.PENDING), (src, text)
        elif src.startswith("report:outcomes.bang.") and src.count(".") == 2:
            value = build_deck.dig(partial, src.removeprefix("report:"))
            assert text.startswith(build_deck.fmt_value(value)), (src, text)
    assert not re.search(r"\d", "".join(w.free_text))
    copy = tmp_path / "DEVPOST.md"
    shutil.copy(DEVPOST, copy)
    build_deck.refresh_devpost(copy, build_deck.Report(partial))
    assert copy.read_text() == DEVPOST.read_text()


# ---------------------------------------------------------------------------
# Without report.json
# ---------------------------------------------------------------------------


def test_pending_without_report(page_without: str) -> None:
    assert build_deck.PENDING in page_without
    for src, text in walk(page_without).sourced:
        if src.startswith("report:"):
            assert not re.search(r"\d", text) or src == "report:inputs_sha256", (src, text)


def test_main_exits_zero_and_warns_without_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = build_deck.main(
        ["--report", str(tmp_path / "nope.json"), "--out", str(tmp_path / "i.html"), "--no-devpost"]
    )
    assert code == 0
    assert "eval pending" in capsys.readouterr().err
    assert (tmp_path / "i.html").exists()


def test_build_is_deterministic(tmp_path: Path) -> None:
    a = build_deck.build(FIXTURE, tmp_path / "a.html", devpost=None, curve=tmp_path / "x.png")
    b = build_deck.build(FIXTURE, tmp_path / "b.html", devpost=None, curve=tmp_path / "x.png")
    assert a == b


def test_template_key_mismatch_is_an_error() -> None:
    with pytest.raises(build_deck.DeckError):
        build_deck.render_template("{{ a }}", {"b": ""})


def test_committed_index_is_clean() -> None:
    page = INDEX.read_text()
    w = walk(page)
    assert [name for name, _ in w.slides] == SLIDE_ORDER
    assert not re.search(r"\d", "".join(w.free_text))
    assert find_denylisted(page) == []


# ---------------------------------------------------------------------------
# Vocabulary, honesty, IBM
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["page_with", "page_without"])
def test_denylist_clean(which: str, request: pytest.FixtureRequest) -> None:
    assert find_denylisted(request.getfixturevalue(which)) == []
    for path in (DEVPOST, DECK_INPUTS, DECK_YAML, TEMPLATE):
        assert find_denylisted(path.read_text()) == [], path


def test_denylisted_text_stops_the_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad = "pre" + "-" + "".join(["ha", "lt"])  # assembled so this file stays clean
    original = build_deck.next_items

    def poisoned(deck: dict[str, Any]) -> str:
        return str(original(deck)) + f"<p>{bad.replace('pre-', '')}</p>"

    monkeypatch.setattr(build_deck, "next_items", poisoned)
    with pytest.raises(build_deck.DeckError):
        build_deck.build(FIXTURE, tmp_path / "i.html", devpost=None)


def test_honesty_labels(page_with: str) -> None:
    text = " ".join(walk(page_with).free_text) + " ".join(t for _, t in walk(page_with).sourced)
    for phrase in (
        "Detection is simulated",
        "stress-test estimates",
        "The work stops at the handoff",
        "Misuse:",
        "Limits:",
    ):
        assert phrase in text, phrase
    deck = yaml.safe_load(DECK_YAML.read_text())
    assert "estimate" in deck["bom_label"]
    assert "estimate" in deck["tenth_budget"]["label"]


def test_no_ibm_bob() -> None:
    for path in (DECK_INPUTS, DEVPOST):
        assert "ibm bob" not in path.read_text().lower(), path


# ---------------------------------------------------------------------------
# Devpost
# ---------------------------------------------------------------------------


def _section(text: str, heading: str) -> str:
    return str(build_deck.md_section(text, heading))


def test_devpost_fields_present() -> None:
    text = DEVPOST.read_text()
    for heading in (
        "Project name",
        "Lens",
        "One-liner (tagline)",
        "Description (about 250 words)",
        "Twists",
        "Built with (tools used)",
        "Responsible-use note (two lines)",
        "Video link",
        "Repo link",
        "Team",
    ):
        assert _section(text, heading), heading
    assert _section(text, "Project name") == "Soundings"
    assert _section(text, "Lens").startswith("Crisis Mitigation")
    assert _section(text, "Repo link") == "https://github.com/kudratsingh/soundings"
    inputs = DECK_INPUTS.read_text()
    assert _section(text, "One-liner (tagline)") == _section(inputs, "One-liner (Devpost)")
    note = _section(text, "Responsible-use note (two lines)")
    assert note.split() == _section(inputs, "Responsible-use note (two lines, Devpost)").split()
    assert [line[:7] for line in note.splitlines()] == ["Misuse:", "Limits:"]
    assert _section(text, "Twists") == _section(
        inputs, "Twists (Devpost writeup; scored at final submission)"
    )
    built = _section(text, "Built with (tools used)")
    for tool in ("Incident Commander", "Anthropic API", "fixtures", "fallback", "OpenSeaMap"):
        assert tool in built, tool
    assert "Leaflet" in built and "FastAPI" in built
    prize = [h for h in re.findall(r"^## (.+)$", text, re.M) if "Bob" in h]
    assert prize and "leave blank" in _section(text, prize[0])


def _words(text: str) -> int:
    body = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    return len(re.findall(r"\S+", re.sub(r"\*\*", "", body)))


def test_devpost_description_length_and_refresh(tmp_path: Path) -> None:
    copy = tmp_path / "DEVPOST.md"
    shutil.copy(DEVPOST, copy)
    rep = build_deck.Report(json.loads(FIXTURE.read_text()))
    assert build_deck.refresh_devpost(copy, rep)
    filled = copy.read_text()
    assert "77.8%" in filled and "[eval pending" not in _section(
        filled, "Description (about 250 words)"
    )
    assert 220 <= _words(_section(filled, "Description (about 250 words)")) <= 270
    # idempotent
    build_deck.refresh_devpost(copy, rep)
    assert copy.read_text() == filled
    # and back to pending without a report
    build_deck.refresh_devpost(copy, build_deck.Report(None))
    assert copy.read_text() == DEVPOST.read_text()
    assert 180 <= _words(_section(DEVPOST.read_text(), "Description (about 250 words)")) <= 270


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_count_pdf_pages(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n<< /Type /Pages /Count 2 >>\n<< /Type /Page >>\n<</Type/Page>>\n")
    assert export_deck.count_pdf_pages(pdf) == 2


@pytest.mark.skipif(
    export_deck.find_cached_chromium() is None, reason="no cached Playwright chromium"
)
def test_pdf_has_eleven_pages(tmp_path: Path) -> None:
    html_path = tmp_path / "index.html"
    build_deck.build(FIXTURE, html_path, devpost=None, curve=tmp_path / "none.png")
    pdf = tmp_path / "deck.pdf"
    # a subprocess, so a Playwright sync session another test holds open cannot collide
    done = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "export_deck.py"),
            "--html",
            str(html_path),
            "--pdf",
            str(pdf),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert export_deck.count_pdf_pages(pdf) == 11


# ---------------------------------------------------------------------------
# --- WO-089 begin: slide 10 carries the physics cards, every value from config ---
# ---------------------------------------------------------------------------

PHYSICS = yaml.safe_load((REPO / "config" / "physics.yaml").read_text())


def _physics_leaves() -> dict[str, Any]:
    from soundings.physics_view import iter_params

    return dict(iter_params(PHYSICS))


@pytest.mark.parametrize("which", ["page_with", "page_without"])
def test_physics_cards_replace_the_disclaimer_line(
    which: str, request: pytest.FixtureRequest
) -> None:
    from soundings.physics import model_cards

    page = request.getfixturevalue(which)
    honesty = dict(walk(page).slides)["honesty"]
    assert "range falloff, noise, track gaps" not in honesty
    assert "Detection is simulated" in honesty and "demo value" in honesty
    sourced = walk(page).sourced
    heads = [
        t for s, t in sourced if re.fullmatch(r"file:soundings/physics\.py#model_cards\.\d+", s)
    ]
    cards = model_cards()
    assert len(heads) == len(cards) >= 4
    for card, text in zip(cards, heads, strict=True):
        assert text == f"{card.name}{card.equation}"
    not_modeled = dict(sourced)["file:soundings/physics.py#model_cards.not_modeled"]
    for item in ("hydrodynamics", "beam patterns", "multipath", "bottom type"):
        assert item in not_modeled


def test_physics_values_match_config(page_with: str) -> None:
    leaves = _physics_leaves()
    shown = {
        src.removeprefix("config:physics."): text
        for src, text in walk(page_with).sourced
        if src.startswith("config:physics.")
    }
    assert set(shown) == set(leaves), "every config/physics.yaml parameter is on slide 10"
    for key, node in leaves.items():
        assert build_deck.physics_value(dict(node)) in shown[key], key
    for node in leaves.values():
        assert str(node["source"]).strip() and node["label"] == "demo value"


def test_template_holds_the_physics_keys() -> None:
    t = TEMPLATE.read_text()
    assert "{{ physics_cards }}" in t and "{{ physics_not_modeled }}" in t
    assert "Detection is simulated for this demonstration: range falloff" not in t


# --- WO-089 end ---
