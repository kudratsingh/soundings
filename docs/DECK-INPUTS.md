# Deck inputs — filled as phases complete

The deck is built from this file on Saturday afternoon. Pitch order from the hub: 0:30 the problem · show it working first · twist, how it holds up · next, where it goes, how feasible.

## One-liner (Devpost)
Soundings climbs the gentlest rung that resolves an unknown underwater contact — ask before slow, slow before hold, hold before you call — and on a quiet Tuesday it finds the research vehicle that went silent.

## Lens
Crisis Mitigation (Track 01 Marine, NULL Labs)

## The seat and the moment
Harbor watch officer, Harbor Night: an unauthorized autonomous vehicle is in a working harbor. The blunt answer is to close the berth. Soundings asks first.

## Second customer and their Tuesday
Operators of planned underwater work at the Port of LA — research AUV/ROV teams (AltaSea tenants, USC Wrigley), hull-cleaning divers, pile/pipeline inspectors, dredging crews. They file a plan; their contact clears at "ask"; a filed vehicle that goes silent gets a search box. (Validate with William Doyle on Saturday; record what he said here.)

## Which way we lean (say it out loud)
Crisis. So the Tuesday clip goes first.

## The three tests (kickoff deck)
- Ordinary-day: who pays on a calm Tuesday — the harbor department (fewer false alarms, divers kept safe from traffic) and the operators (registry + lost-vehicle finding).
- Crisis: at 3 AM with the network down and staff gone — the node runs at the edge; signed standing orders pre-authorize watch/ask and a scoped slow/hold; the SITREP queues for whoever is reachable; nothing escalates on its own.
- Travel: a tenth of the budget — one hydrophone node and a web form; no cloud dependency; SMS handoff; target BOM stated as an estimate.

## Slide: the ladder (organizers' words)
watch → ask → slow → hold → hand to a person. We live on **ask** and **hand to a person**.

## Slide: the boundary table
(paste `config/ladder.yaml` boundary section as a table)

## Slide: evidence table from S05 (facts vs assumptions in two colours)
(screenshot from Phase 4)

## Slide: eval
(from `artifacts/eval/report.md`: rung histogram per policy; missed threats; false shutdowns; median seconds seen→handled; interruptions/hour; shutdown-minutes; "where the LLM layer added nothing")

## Slide: twists
| twist | what changed in the build | proof |
|---|---|---|
| 1 | none — written response: Navy SBIR (N152-113, N252-116/117) primary; DHS S&T alternative; DIU fast on-ramp; NSF SBIR non-defense. Soundings sits upstream of the Navy 25.2 topics | twists/01/response.md + sources |
| 2 | | |
| 3 | | |

## Slide: what we don't know / honesty
Detection simulated for this demonstration (range falloff, noise, gaps, biologic false alarms). Probabilities are stress-test estimates. The work stops at the handoff; physically stopping a vehicle is out of scope by the brief (CLAUDE.md §2.1).

## Responsible-use note (two lines, Devpost)
Misuse: surveilling swimmers or divers outside designated security zones, or using the output to justify autonomous action against a person. Limits: geofenced to declared zones; no identity inference; nothing above the signed standing-orders scope executes without a named human's approval; the system's work ends at the handoff; immutable audit log; 30-day retention.

## Tools used (Devpost)
Python/FastAPI/SQLModel; React/Vite/Leaflet with OpenSeaMap tiles; Anthropic API (probe planner, hypothesis writer, SITREP writer, twist parser — all replayed from recorded fixtures by default, deterministic fallbacks for every role); the owner's open-source agent state-machine pattern (Incident Commander) reused as the design, with new code written this weekend; IBM Bob if used (say how).

## Twists (Devpost writeup; scored at final submission)
**Twist 1 — funding pathway (written response, no build change).** What changed: nothing in the build; we answered where Soundings gets funded and placed it upstream of the Navy 25.2 UUV topics (the determination that a contact is unauthorized is what we build; everything after the handoff is a person's call). Why: the primary path is Navy SBIR/STTR (N152-113, N252-116/117) with Phase III naming commercial port protection; alternatives DHS S&T Maritime Safety and Security, DIU open solicitations, NSF SBIR. Proof: `twists/01/response.md` (sources inline), `twists/01/delta.json`, merge commit `1dded8e`.

**Twist 2 — (pending notice).** Must produce a toggle, scenario, or feature with a test. What changed / why / proof: TBD.

**Twist 3 — (pending notice).** Must produce a toggle, scenario, or feature with a test. What changed / why / proof: TBD.
