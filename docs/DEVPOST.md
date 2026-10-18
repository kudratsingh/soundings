# Devpost — every field, ready to paste

Owner submits through the Devpost portal (due Sat Oct 17, 10:00 PM PT). Copy each block as is. The evidence sentence in the description is filled by `make deck` from `artifacts/eval/report.json`; do not type numbers into it by hand.

## Project name
Soundings

## Lens
Crisis Mitigation (Track 01 Marine, with NULL Labs)

## One-liner (tagline)
Soundings climbs the gentlest rung that resolves an unknown underwater contact — ask before slow, slow before hold, hold before you call — and on a quiet Tuesday it finds the research vehicle that went silent.

## Description (about 250 words)
**The problem.** A harbor watch officer sees an unknown underwater contact near a working berth. Is it a research vehicle on a survey, a diver cleaning a hull, a sea lion, or an unauthorized autonomous vehicle? Today's answer is to close the berth: every unknown becomes a shutdown or a page.

**The ladder.** The organizers' ladder is watch → ask → slow → hold → hand to a person. Soundings lives on ask and hand to a person. Watch is the floor; slow and hold run only under signed standing orders, scoped to one basin or one berth.

**The mechanism.** Ask is cheap: a registry of planned underwater work (a flight plan for the water) and a simulated acoustic hail clear most contacts with no one interrupted. The rest get a hypothesis-driven investigation using the fewest, cheapest probes. Deterministic guards over a typed evidence table decide every transition; the model only proposes probes, hypotheses and SITREP text, never a rung or an approval. The work stops at the handoff: a SITREP where every line links to its evidence row, sent to whoever is reachable. A filed vehicle that goes silent gets a search box.

**The evidence.** We ran the scenario catalog over five seeds against two blunt policies: close the berth on every contact, and send every contact to a person.

<!-- eval:begin -->
Evidence (simulated detection, stress-test estimate, 45 ground-truth contacts): Soundings resolved 68.9% at or below ask with 0 missed threats and 15 shutdown-minutes; closing the berth on every contact cost 1,293; sending everything to a person cost 30.7 interruptions per hour against 10.9.
<!-- eval:end -->

## Twists
**Twist 1 — funding pathway (written response, no build change).** What changed: nothing in the build; we answered where Soundings gets funded and placed it upstream of the Navy 25.2 UUV topics (the determination that a contact is unauthorized is what we build; everything after the handoff is a person's call). Why: the primary path is Navy SBIR/STTR (N152-113, N252-116/117) with Phase III naming commercial port protection; alternatives DHS S&T Maritime Safety and Security, DIU open solicitations, NSF SBIR. Proof: `twists/01/response.md` (sources inline), `twists/01/delta.json`, merge commit `1dded8e`.

**Twist 2 — (pending notice).** Must produce a toggle, scenario, or feature with a test. What changed / why / proof: TBD.

**Twist 3 — (pending notice).** Must produce a toggle, scenario, or feature with a test. What changed / why / proof: TBD.

## Built with (tools used)
- **Design pattern:** a state-machine agent design the owner has used before.
- **Anthropic API** (probe planner, hypothesis writer, SITREP writer, twist parser) — replayed from recorded fixtures by default, with a deterministic fallback for every role; no model output becomes a control.
- **OpenSeaMap** tiles on **Leaflet** (React + Vite + TypeScript single screen).
- **FastAPI** + Pydantic v2 + WebSocket live contact feed; SQLModel on SQLite (registry, contacts, evidence, audit log).
- numpy (seeded simulator, evidence features, posterior) and matplotlib (scenario plots).
- **OR-Tools** CP-SAT (slot proposals for conflicting dive plans, WO-082) and **shapely** (plan-conflict geometry, WO-081). [Owner check: keep this line only if WO-081 / WO-082 are merged by submission.]
- Python 3.12, uv, ruff, pytest + hypothesis; Playwright for the deck PDF.

## Responsible-use note (two lines)
Misuse: surveilling swimmers or divers outside designated security zones, or using the output to justify autonomous action against a person.
Limits: geofenced to declared zones; no identity inference; nothing above the signed standing-orders scope executes without a named human's approval; the system's work ends at the handoff; immutable audit log; 30-day retention.

## Honesty (paste into "Challenges" or the end of the description if there is room)
Detection is simulated for this demonstration (range falloff, noise, track gaps, biologic false alarms). Probabilities are stress-test estimates from a declared, visible catalog, not operational forecasts. The work stops at the handoff; physically stopping a vehicle is out of scope by the sponsor's brief.

## Pitch deck
Upload `docs/deck/deck.pdf` (built by `make deck`; 11 slides, 16:9).

## Video link
[video link placeholder — YouTube/Vimeo URL of the ≤ 3 min demo]

## Repo link
https://github.com/kudratsingh/soundings

## Team
[team list placeholder — add every teammate on Devpost before 10:00 PM]

## Hardware
n/a

## Best Build with the sponsor's Bob tool (prize field)
_(leave blank — not used)_
