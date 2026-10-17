"""Render the pitch deck (``docs/deck/index.html``) from its template (WO-090).

Usage::

    uv run python scripts/build_deck.py [--report artifacts/eval/report.json]
        [--out docs/deck/index.html] [--devpost docs/DEVPOST.md | --no-devpost]

Every number on a slide comes from a file at build time, never from the template:

- eval metrics from ``artifacts/eval/report.json`` (WO-051 ``EvalReport``);
- the boundary table and the slow/hold criteria from ``config/ladder.yaml``;
- the twist rows, the honesty text and the responsible-use note from ``docs/DECK-INPUTS.md``;
- the team line, the BOM estimates and the "next" items from ``docs/deck/deck.yaml``;
- the physics cards on slide 10 (WO-089): equations from ``soundings.physics.model_cards``
  and every parameter from ``config/physics.yaml``.

Each such value is emitted inside an element whose ``data-src`` names its source
(``report:<path>``, ``config:ladder.<path>``, ``config:physics.<path>``,
``file:soundings/physics.py#model_cards.<i>``, ``inputs:<section>``, ``deck:<path>``), so
``tests/test_deck.py`` can check every figure against its file. Without ``report.json``
the eval values render as ``[eval pending]``, a warning goes to stderr and the exit code
is 0. The rendered HTML (and ``docs/DEVPOST.md``) must be denylist-clean: a hit exits 1.

The same run refreshes the evidence sentence between ``<!-- eval:begin -->`` and
``<!-- eval:end -->`` in ``docs/DEVPOST.md``. No model calls, no network.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # run as a file from anywhere

from soundings.schemas.denylist import find_denylisted  # noqa: E402

TEMPLATE = REPO_ROOT / "docs" / "deck" / "template.html"
DECK_YAML = REPO_ROOT / "docs" / "deck" / "deck.yaml"
DEFAULT_OUT = REPO_ROOT / "docs" / "deck" / "index.html"
DEFAULT_REPORT = REPO_ROOT / "artifacts" / "eval" / "report.json"
DEFAULT_CURVE = REPO_ROOT / "artifacts" / "eval" / "threshold_curve.png"
LADDER_YAML = REPO_ROOT / "config" / "ladder.yaml"
DECK_INPUTS = REPO_ROOT / "docs" / "DECK-INPUTS.md"
DEVPOST = REPO_ROOT / "docs" / "DEVPOST.md"

PENDING = "[eval pending]"
TWIST_PENDING = "[twist pending]"
CURVE_PENDING = "[threshold curve pending]"
EVAL_BEGIN = "<!-- eval:begin -->"
EVAL_END = "<!-- eval:end -->"
KEY_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

POLICY_ORDER = ("soundings", "bang", "human_queue")
POLICY_LABEL = {
    "soundings": "Soundings",
    "bang": "Close the berth (blunt policy)",
    "human_queue": "Send everything to a person",
}
HIST_RUNGS = ("watch", "ask", "slow", "hold", "hand_to_person")
RUNG_LABEL = {
    "watch": "watch",
    "ask": "ask",
    "slow": "slow",
    "hold": "hold",
    "hand_to_person": "hand to a person",
}
RUNG_CAPTION = {
    "watch": "track, score, compare with the harbor's pattern of life",
    "ask": "registry lookup and an acoustic hail: most contacts clear here",
    "slow": "an advisory for one basin, under signed standing orders",
    "hold": "one gate or berth, bounded in time, under signed standing orders",
    "hand_to_person": "a SITREP with its evidence; the person decides",
}
WORD_FIX = {"uscg": "USCG", "eod": "EOD", "sitrep": "SITREP"}


class DeckError(RuntimeError):
    """A template, input or vocabulary problem that must stop the build."""


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def fmt_value(value: Any) -> str:
    """The one number format on the slides: ints with thousands separators; floats to two
    decimals under 10 and one decimal from 10 up, trailing zeros dropped (0.25 stays 0.25)."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        text = f"{value:,.2f}" if abs(value) < 10 else f"{value:,.1f}"
        return text.rstrip("0").rstrip(".")
    return str(value)


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def inline_md(text: str) -> str:
    """Escape, then the two inline marks DECK-INPUTS uses: **bold** and `code`."""
    out = esc(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"`(.+?)`", r"<code>\1</code>", out)
    return out


def src_span(src: str, inner_html: str, cls: str = "src", tag: str = "span") -> str:
    return f'<{tag} class="{cls}" data-src="{esc(src)}">{inner_html}</{tag}>'


def pretty(token: str) -> str:
    words = [WORD_FIX.get(w, w) for w in token.split("_")]
    return " ".join(words)


def dig(data: Any, path: str) -> Any:
    """Follow a dotted path through dicts and lists; KeyError when absent."""
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class Report:
    """``report.json`` or its absence; every eval figure goes through ``num``."""

    def __init__(self, data: dict[str, Any] | None) -> None:
        self.data = data

    @property
    def present(self) -> bool:
        return self.data is not None

    def pending_reason(self, policy: str) -> str | None:
        if self.data is None:
            return None
        pending = self.data.get("policies_pending") or {}
        reason = pending.get(policy)
        return str(reason) if reason else None

    def get(self, path: str) -> Any:
        if self.data is None:
            raise KeyError(path)
        return dig(self.data, path)

    def num(self, path: str, suffix: str = "", cls: str = "num") -> str:
        src = f"report:{path}"
        if self.data is None:
            return src_span(src, esc(PENDING), cls="num pending")
        parts = path.split(".")
        if len(parts) >= 2 and parts[0] in ("outcomes", "false_alarms"):
            reason = self.pending_reason(parts[1])
            if reason is not None:
                return (
                    f'<span class="num pending" data-src="{esc(src)}" '
                    f'title="{esc(reason)}">{esc(PENDING)}</span>'
                )
        try:
            value = self.get(path)
        except (KeyError, IndexError, ValueError):
            return src_span(src, esc(PENDING), cls="num pending")
        return src_span(src, esc(fmt_value(value)) + esc(suffix), cls=cls)

    def value(self, path: str) -> Any:
        try:
            return self.get(path)
        except (KeyError, IndexError, ValueError):
            return None


def load_report(path: Path) -> Report:
    if not path.exists():
        print(
            f"warning: {path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path}"
            " not found; eval figures render as '[eval pending]' (run `make eval`)",
            file=sys.stderr,
        )
        return Report(None)
    return Report(json.loads(path.read_text(encoding="utf-8")))


def md_section(text: str, heading: str) -> str:
    """Body of the ``## heading`` section of a Markdown file (up to the next ``## ``)."""
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip() == heading
            continue
        if inside:
            out.append(line)
    if not inside and not out:
        raise DeckError(f"docs/DECK-INPUTS.md has no '## {heading}' section")
    return "\n".join(out).strip()


def parse_twists(deck_inputs: str) -> list[tuple[str, str, str]]:
    """Rows of the ``| twist | what changed in the build | proof |`` table."""
    body = md_section(deck_inputs, "Slide: twists")
    rows: list[tuple[str, str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() == "twist" or set(cells[0]) <= {"-", " ", ":"}:
            continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


# ---------------------------------------------------------------------------
# Fragments
# ---------------------------------------------------------------------------


def ladder_chips(ladder: dict[str, Any]) -> str:
    lives = set(ladder.get("lives_on", []))
    parts: list[str] = []
    for k, rung in enumerate(ladder["rungs"]):
        if k:
            parts.append('<li class="rung-arrow" aria-hidden="true">→</li>')
        cls = "rung lives" if rung in lives else "rung"
        tag = '<span class="rung-tag">we live here</span>' if rung in lives else ""
        parts.append(
            f'<li class="{cls}" data-rung="{esc(rung)}">{tag}'
            f'<span class="name">{esc(RUNG_LABEL.get(rung, pretty(rung)))}</span>'
            f'<span class="what">{esc(RUNG_CAPTION.get(rung, ""))}</span></li>'
        )
    return "".join(parts)


def boundary_rows(ladder: dict[str, Any]) -> str:
    lives = set(ladder.get("lives_on", []))
    rows: list[str] = []
    for rung in ladder["rungs"]:
        cells = ladder["boundary"][rung]
        tds = []
        for col in ("platform_alone", "standing_orders", "person_approves"):
            items = cells.get(col) or []
            chip_cls = "chip person" if col == "person_approves" else "chip"
            inner = (
                "".join(f'<span class="{chip_cls}">{esc(pretty(i))}</span>' for i in items)
                if items
                else '<span class="none">—</span>'
            )
            tds.append(src_span(f"config:ladder.boundary.{rung}.{col}", inner, tag="td"))
        rung_cls = "rungcell lives" if rung in lives else "rungcell"
        rows.append(
            f'<tr data-rung="{esc(rung)}"><td class="{rung_cls}">'
            f"{esc(RUNG_LABEL.get(rung, pretty(rung)))}</td>{''.join(tds)}</tr>"
        )
    return "".join(rows)


def cfg_num(ladder: dict[str, Any], path: str, suffix: str = "") -> str:
    return src_span(
        f"config:ladder.{path}", esc(fmt_value(dig(ladder, path))) + esc(suffix), cls="num"
    )


def criteria_line(ladder: dict[str, Any]) -> str:
    return (
        f"Slow when the threat mass is at least {cfg_num(ladder, 'criteria.slow.threat_mass_min')}"
        f" and a critical asset is under "
        f"{cfg_num(ladder, 'criteria.slow.time_to_critical_asset_max_s', ' s')} away. "
        f"Hold when it is at least {cfg_num(ladder, 'criteria.hold.threat_mass_min')}, under "
        f"{cfg_num(ladder, 'criteria.hold.time_to_critical_asset_max_s', ' s')}, zone criticality"
        f" at least {cfg_num(ladder, 'criteria.hold.zone_criticality_min')}, with at most "
        f"{cfg_num(ladder, 'criteria.hold.active_holds_max')} other holds active. "
        f"Hand to a person at a threat mass of "
        f"{cfg_num(ladder, 'stopping.threat_mass_handoff')}. Stress-test settings, not forecasts."
    )


def histogram(rep: Report) -> str:
    rows: list[str] = []
    for policy in POLICY_ORDER:
        label = esc(POLICY_LABEL[policy])
        reason = rep.pending_reason(policy)
        if not rep.present or reason is not None or rep.value(f"outcomes.{policy}") is None:
            text = f"{PENDING} ({reason})" if reason else PENDING
            bar = (
                f'<div class="hist-bar">'
                f"{src_span(f'report:outcomes.{policy}', esc(text), cls='num pending')}</div>"
            )
        else:
            segs = []
            for rung in HIST_RUNGS:
                count = rep.value(f"outcomes.{policy}.{rung}") or 0
                if not count:
                    continue
                segs.append(
                    f'<div class="seg {rung}" style="flex-grow:{int(count)}" '
                    f'title="{esc(RUNG_LABEL[rung])}">'
                    f"{rep.num(f'outcomes.{policy}.{rung}')}</div>"
                )
            bar = f'<div class="hist-bar">{"".join(segs)}</div>'
        rows.append(
            f'<div class="hist-row" data-policy="{policy}">'
            f'<div class="hist-name">{label}</div>{bar}</div>'
        )
    return "".join(rows)


def legend() -> str:
    return (
        "".join(f'<span><i class="seg {r}"></i>{esc(RUNG_LABEL[r])}</span>' for r in HIST_RUNGS)
        + '<span class="muted">max rung per ground-truth contact</span>'
    )


def kpi(rep: Report, metric: str, caption: str, suffix: str = "") -> str:
    others = " · ".join(
        f"{esc(POLICY_LABEL[p].split(' (')[0].lower())}: "
        f"{rep.num(f'outcomes.{p}.{metric}', suffix)}"
        for p in POLICY_ORDER[1:]
    )
    return (
        f'<div class="kpi" data-metric="{metric}">'
        f'<span class="big">{rep.num(f"outcomes.soundings.{metric}", suffix)}</span>'
        f'<span class="cap">{esc(caption)} · Soundings</span>'
        f'<span class="vs">{others}</span></div>'
    )


def kpis(rep: Report) -> str:
    return "".join(
        [
            kpi(rep, "missed_threats", "missed threats"),
            kpi(rep, "median_s_seen_to_handled", "median seconds, seen → handled", " s"),
            kpi(rep, "interruptions_per_hour", "person interruptions per hour"),
            kpi(rep, "shutdown_minutes", "shutdown-minutes"),
        ]
    )


def curve_html(rep: Report, curve_default: Path) -> str:
    rel = rep.value("threshold_curve.png") if rep.present else None
    path = (REPO_ROOT / str(rel)) if rel else curve_default
    src = f"report:threshold_curve.png={rel}" if rel else f"file:{_rel(curve_default)}"
    if path.exists() and path.suffix.lower() == ".png":
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return (
            f'<img data-src="{esc(src)}" alt="threshold curve: outcomes as the threshold moves" '
            f'src="data:image/png;base64,{data}">'
        )
    return src_span(src, esc(CURVE_PENDING), cls="num pending")


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def eval_label(rep: Report) -> str:
    return (
        f"{rep.num('label', cls='src text')} · suite {rep.num('suite', cls='src text')}"
        f" · seeds {seeds_text(rep)}"
        f" · simulated detection"
    )


def seeds_text(rep: Report) -> str:
    seeds = rep.value("seeds") if rep.present else None
    if not seeds:
        return rep.num("seeds")
    return src_span("report:seeds", esc(",".join(str(s) for s in seeds)), cls="num")


def eval_notes(rep: Report) -> str:
    return (
        f"<p>{rep.num('outcomes.soundings.gt_contacts')} ground-truth contacts per policy; "
        f"Soundings resolved {rep.num('outcomes.soundings.at_or_below_ask_pct', '%')} at or "
        f"below ask. False shutdowns: Soundings {rep.num('outcomes.soundings.false_shutdowns')},"
        f" close the berth {rep.num('outcomes.bang.false_shutdowns')}.</p>"
        f"<p>Sim false alarms are kept out of the denominator: "
        f"{rep.num('false_alarms.soundings.false_alarms')} of them, costing Soundings "
        f"{rep.num('false_alarms.soundings.interruptions')} interruptions.</p>"
        f"<p>Shutdown cost: {rep.num('outcomes.bang.shutdown_usd', ' USD')} — no "
        f"mentor-validated $/minute figure yet, so it stays at zero and labeled.</p>"
    )


def twist_rows(deck_inputs: str) -> str:
    rows = []
    for n, changed, proof in parse_twists(deck_inputs):
        changed_html = inline_md(changed) if changed else esc(TWIST_PENDING)
        proof_html = inline_md(proof) if proof else esc(TWIST_PENDING)
        cls = "src" if changed else "src pending"
        base = f"inputs:DECK-INPUTS.md#twists.{n}"
        rows.append(
            f'<tr data-twist="{esc(n)}">'
            f"{src_span(f'{base}.twist', esc(n), cls='src', tag='td')}"
            f"{src_span(f'{base}.changed', changed_html, cls=cls, tag='td')}"
            f"{src_span(f'{base}.proof', proof_html, cls=cls, tag='td')}"
            "</tr>"
        )
    return "".join(rows)


def usd_range(text: str) -> tuple[int, int]:
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text)]
    if not nums:
        raise DeckError(f"deck.yaml BOM line without a figure: {text!r}")
    return nums[0], nums[-1]


def bom_rows(deck: dict[str, Any]) -> str:
    rows = []
    lo = hi = 0
    for k, line in enumerate(deck["bom"]):
        a, b = usd_range(str(line["usd"]))
        lo += a
        hi += b
        rows.append(
            f"<tr><td>{esc(line['item'])}</td><td>{esc(line['role'])}</td>"
            f"{src_span(f'deck:bom.{k}.usd', esc(str(line['usd'])), cls='num', tag='td')}</tr>"
        )
    total = f"{lo:,}–{hi:,}"
    rows.append(
        f"<tr><td><b>Per node, estimate</b></td><td></td>"
        f"{src_span('deck:bom (sum of lines)', esc(total), cls='num', tag='td')}</tr>"
    )
    return "".join(rows)


def tenth_title(deck: dict[str, Any]) -> str:
    tb = deck["tenth_budget"]
    return (
        f"{esc(tb['label'])}: "
        f"{src_span('deck:tenth_budget.usd', esc(str(tb['usd'])) + ' USD', cls='num')}"
    )


def tenth_lines(deck: dict[str, Any]) -> str:
    return "".join(
        f"<li>{src_span(f'deck:tenth_budget.lines.{k}', esc(t))}</li>"
        for k, t in enumerate(deck["tenth_budget"]["lines"])
    )


def responsible_use(deck_inputs: str) -> str:
    body = md_section(deck_inputs, "Responsible-use note (two lines, Devpost)")
    flat = " ".join(line.strip() for line in body.splitlines() if line.strip())
    paras = [p for p in re.split(r"\s+(?=Limits:)", flat) if p]
    return "".join(
        f"<p>{src_span('inputs:DECK-INPUTS.md#responsible-use', inline_md(p))}</p>" for p in paras
    )


def next_items(deck: dict[str, Any]) -> str:
    return "".join(
        f'<div class="panel col"><h3>{esc(item["title"])}</h3>'
        f"<p>{src_span(f'deck:next.{k}.text', esc(item['text']))}</p></div>"
        for k, item in enumerate(deck["next"])
    )


def provenance(rep: Report) -> str:
    if not rep.present:
        text = esc(PENDING + ": artifacts/eval/report.json")
        return src_span("report:inputs_sha256", text, "num pending")
    sha = str(rep.value("inputs_sha256") or "")[:12]
    return "eval inputs hash " + src_span("report:inputs_sha256", esc(sha), cls="num")


def bang_line(rep: Report) -> str:
    return (
        f"Across our scenario catalog, closing the berth on every contact cost "
        f"{rep.num('outcomes.bang.shutdown_minutes')} shutdown-minutes and "
        f"{rep.num('outcomes.bang.false_shutdowns')} false shutdowns (simulated, stress-test)."
    )


def ask_line(rep: Report) -> str:
    return (
        f"Soundings resolved {rep.num('outcomes.soundings.at_or_below_ask_pct', '%')} of "
        f"catalog contacts at or below ask (simulated detection, stress-test estimate)."
    )


# --- WO-089 begin: physics cards (slide 10) ---
PHYSICS_SRC = "file:soundings/physics.py#model_cards"
LEGACY_DETECTION_LINE = (
    "Detection is simulated for this demonstration: range falloff, noise, track gaps, "
    "biologic false alarms."
)
"""Slide 10's line until WO-089 part 1 (``soundings/physics.py``) is on the branch."""


def physics_value(param: dict[str, Any]) -> str:
    """A parameter as the slide shows it: the deck's number format, then the unit."""
    unit = str(param.get("unit") or "")
    return fmt_value(param["value"]) + (f" {unit}" if unit else "")


def physics_cards_html() -> str:
    """One card per model: its name and equation (from ``model_cards``) and each parameter's
    value and unit from ``config/physics.yaml`` under ``data-src="config:physics.<key>"``,
    with the source in the element's title. Every value is a demo value."""
    from soundings.physics_view import physics_available, physics_cards

    if not physics_available():
        return f'<p class="legacy">{esc(LEGACY_DETECTION_LINE)}</p>'
    out: list[str] = []
    for i, card in enumerate(physics_cards()):
        head = f'<h4>{esc(card["name"])}</h4><code class="eq">{esc(card["equation"])}</code>'
        rows = "".join(
            f'<li title="{esc(p["source"])}">'
            f'<span class="src text" data-src="config:physics.{esc(p["key"])}">'
            f"{esc(p['name'].replace('_', ' ').replace('.', ' '))} "
            f'<span class="num">{esc(physics_value(p))}</span></span></li>'
            for p in card["parameters"]
        )
        out.append(
            f'<div class="pcard">{src_span(f"{PHYSICS_SRC}.{i}", head, cls="src text", tag="div")}'
            f'<ul class="pparams">{rows}</ul></div>'
        )
    return "".join(out)


def physics_not_modeled() -> str:
    """The union of every card's "what we do NOT model", in card order."""
    from soundings.physics_view import NOT_MODELED_FLOOR, physics_cards

    seen: list[str] = []
    for card in physics_cards():
        for item in card["not_modeled"]:
            if item not in seen:
                seen.append(item)
    seen = seen or list(NOT_MODELED_FLOOR)
    return src_span(f"{PHYSICS_SRC}.not_modeled", esc(", ".join(seen)), cls="src text")


# --- WO-089 end ---


def llm_sentence(rep: Report) -> str:
    return rep.num("llm.sentence", cls="src text")


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_template(template: str, values: dict[str, str]) -> str:
    keys = set(KEY_RE.findall(template))
    missing = sorted(keys - values.keys())
    unused = sorted(values.keys() - keys)
    if missing or unused:
        raise DeckError(f"template keys missing values: {missing}; values without keys: {unused}")
    return KEY_RE.sub(lambda m: values[m.group(1)], template)


def build_values(
    rep: Report, ladder: dict[str, Any], deck: dict[str, Any], deck_inputs: str, curve: Path
) -> dict[str, str]:
    return {
        "team": src_span("deck:team", esc(deck["team"])),
        "event": src_span("deck:event", esc(deck["event"])),
        "bang_line": bang_line(rep),
        "ladder_chips": ladder_chips(ladder),
        "ask_line": ask_line(rep),
        "boundary_rows": boundary_rows(ladder),
        "criteria_line": criteria_line(ladder),
        "eval_label": eval_label(rep),
        "histogram": histogram(rep),
        "legend": legend(),
        "kpis": kpis(rep),
        "curve": curve_html(rep, curve),
        "eval_notes": eval_notes(rep),
        "twist_rows": twist_rows(deck_inputs),
        "bom_label": src_span("deck:bom_label", esc(deck["bom_label"])),
        "bom_rows": bom_rows(deck),
        "tenth_title": tenth_title(deck),
        "tenth_lines": tenth_lines(deck),
        "llm_sentence": llm_sentence(rep),
        "physics_cards": physics_cards_html(),  # WO-089
        "physics_not_modeled": physics_not_modeled(),  # WO-089
        "responsible_use": responsible_use(deck_inputs),
        "next_items": next_items(deck),
        "repo": src_span("deck:repo", esc(deck["repo"])),
        "provenance": provenance(rep),
    }


def devpost_evidence(rep: Report) -> str:
    """Plain-Markdown evidence sentence for the Devpost description."""
    if not rep.present or rep.pending_reason("soundings") is not None:
        return (
            "Evidence: [eval pending — run `make eval && make deck` to fill this sentence "
            "from artifacts/eval/report.json]."
        )

    def v(path: str, suffix: str = "") -> str:
        return fmt_value(rep.value(path)) + suffix

    return (
        f"Evidence (simulated detection, stress-test estimate, "
        f"{v('outcomes.soundings.gt_contacts')} ground-truth contacts): Soundings resolved "
        f"{v('outcomes.soundings.at_or_below_ask_pct', '%')} at or below ask with "
        f"{v('outcomes.soundings.missed_threats')} missed threats and "
        f"{v('outcomes.soundings.shutdown_minutes')} shutdown-minutes; closing the berth on "
        f"every contact cost {v('outcomes.bang.shutdown_minutes')}; sending everything to a "
        f"person cost {v('outcomes.human_queue.interruptions_per_hour')} interruptions per "
        f"hour against {v('outcomes.soundings.interruptions_per_hour')}."
    )


def refresh_devpost(path: Path, rep: Report) -> bool:
    if not path.exists():
        print(f"warning: {path} not found; Devpost evidence not refreshed", file=sys.stderr)
        return False
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(EVAL_BEGIN) + r".*?" + re.escape(EVAL_END), re.S)
    if not pattern.search(text):
        raise DeckError(f"{path.name} has no {EVAL_BEGIN} … {EVAL_END} block")
    block = f"{EVAL_BEGIN}\n{devpost_evidence(rep)}\n{EVAL_END}"
    new = pattern.sub(lambda _m: block, text)
    hits = find_denylisted(new)
    if hits:
        raise DeckError(f"{path.name} holds denylisted vocabulary: {sorted(set(hits))}")
    if new != text:
        path.write_text(new, encoding="utf-8")
    return True


def build(
    report_path: Path = DEFAULT_REPORT,
    out: Path = DEFAULT_OUT,
    devpost: Path | None = DEVPOST,
    curve: Path = DEFAULT_CURVE,
) -> str:
    rep = load_report(report_path)
    ladder = yaml.safe_load(LADDER_YAML.read_text(encoding="utf-8"))
    deck = yaml.safe_load(DECK_YAML.read_text(encoding="utf-8"))
    deck_inputs = DECK_INPUTS.read_text(encoding="utf-8")
    page = render_template(
        TEMPLATE.read_text(encoding="utf-8"),
        build_values(rep, ladder, deck, deck_inputs, curve),
    )
    hits = find_denylisted(page)
    if hits:
        raise DeckError(f"rendered deck holds denylisted vocabulary: {sorted(set(hits))}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    if devpost is not None:
        refresh_devpost(devpost, rep)
    return page


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--devpost", type=Path, default=DEVPOST)
    parser.add_argument("--no-devpost", action="store_true", help="leave docs/DEVPOST.md alone")
    parser.add_argument("--curve", type=Path, default=DEFAULT_CURVE)
    args = parser.parse_args(argv)
    try:
        build(args.report, args.out, None if args.no_devpost else args.devpost, args.curve)
    except DeckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"deck: wrote {_rel(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
