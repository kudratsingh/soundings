# Mentor brief — Saturday 10:00

Two sessions, one page each. Each page starts with the one decision we are stuck on, then three questions, then exactly what to put on screen. Step numbers are the 1-based order of `steps` in `docs/demo-script.yaml`, cited as `step N (m:ss)`.

Say this once at the start of each session: detection is simulated for this demonstration; every probability is a stress-test estimate from a visible table; the work stops at the handoff, and everything after it is a person's call.

**Fast run for the table.** `uv run python -m soundings.demo --script docs/demo-script.yaml --pace fast` prints all seven steps in about 5 s. `make demo` runs the same steps at real-time pace. As of 06:38 PDT, steps 3, 4 and 6 end at `INVESTIGATING` (the hold and handoff need runner v3, WO-043 part B), step 5 prints a placeholder (WO-061), and step 7 prints "eval: pending WO-051". If these have not landed by 10:00, show the files listed under each step.

---

## Page 1 — William Doyle (AltaSea / Port of LA startup hub)

**What we want from him:** the Tuesday customer, the registry owner, and one cost figure.

### Decision we are stuck on
**Who owns the registry of planned underwater work? Is that the same party that pays on a calm Tuesday?**

- *Our current default:* the Captain of the Port side (Coast Guard / DHS) owns the registry and the standing orders (`twists/01/response.md`, "Alternative" paragraph). The harbor department pays on Tuesday through fewer false alarms and divers kept clear of traffic, and the operators get lost-vehicle finding (`docs/DECK-INPUTS.md`, "The three tests").
- *What changes if he says otherwise:* if the port or the VTS operator owns it, then the `signed_by` field in `config/standing_orders.yaml` (now "Watch Commander (demo)") changes. So do the first pilot customer on deck slide 11 and the funding pathway order (DHS S&T versus port budget). The "who pays" line on slide 4 changes too. If nobody owns it today, the registry is the product and the operators are the first customer.

### Three questions
1. **Do underwater work crews already notify the port before they dive or launch?** Who do they tell, how (form, phone, radio, email), and how far ahead? Our filed plan holds operator, vehicle, modem id, work area, time window and a phone contact (`scenarios/S01-filed-survey-on-plan.yaml`, `registry_plans`). Is that more or less than what they send today?
2. **Who is on the VTS LA/LB watch at 2 AM, and what would they do with our report?** Our handoff goes to a "Watch Officer" in the UI with a 120 s timeout, then to a "Duty Supervisor" by SMS with 180 s (`config/standing_orders.yaml`, `approvers`). Is that the right seat? Does that seat already have authority to slow one basin or hold one berth?
3. **What does one hour of a closed fuel berth cost, and who bears it?** We carry `$0` (`config/ladder.yaml`, `costs.shutdown_usd_per_min_default: 0`) because we will not invent the number. A berth reopens 10 min after its last contact in the baseline (`config/eval.yaml`, `berth_reopen_after_s: 600`, a labeled assumption). Ask for a dollar range per hour, where it comes from, and whether we may cite him.

### What to show (about 4 min)
| order | what | on screen |
|---|---|---|
| 1 | step 1 (0:00): S01, filed survey clears at ask, zero interruptions | `scenarios/S01-filed-survey-on-plan.yaml` (the plan fields for question 1) |
| 2 | step 2 (0:30): S02, filed glider goes silent; lost-vehicle report with a search box goes to the operator, not the watch officer | `scenarios/S02-filed-vehicle-gone-silent.yaml` |
| 3 | step 7 (4:00): catalog vs close-the-berth vs send-everything-to-a-human, shutdown-minutes and the "$0: no mentor-validated figure yet" label | `artifacts/eval/report.md` if present; otherwise `config/ladder.yaml` `costs` and `config/eval.yaml` `baselines` |

**Have open:** `config/ladder.yaml` (the `boundary` table: what decides alone, what runs under standing orders, what a person approves) · `config/standing_orders.yaml` · `docs/DECK-INPUTS.md` ("Second customer and their Tuesday").

<div style="page-break-after: always"></div>

---

## Page 2 — Dr. Nick Wettels (retired submarine commander)

**What we want from him:** what is wrong with our contacts, what we forgot, and what the person making the external call needs first.

### Decision we are stuck on
**Is "under 300 s from a critical asset" the right moment to hold one berth on an unfiled contact that does not answer?**

- *Our current default:* hold runs under signed standing orders when threat mass ≥ 0.35, time to a critical asset ≤ 300 s, zone criticality ≥ 0.7 and no other hold is active. Slow runs at ≥ 0.25 and ≤ 600 s (`config/ladder.yaml`, `criteria`). The hold is limited to one gate or berth for 15 min (`config/standing_orders.yaml`, `scope.hold`). In S05 the contact makes 1.0 m/s from about 520 m east of Fuel Berth 1. It is under 300 s from that berth by tick 109 (`docs/log/DECISIONS.md`, 2026-10-16 22:50).
- *What changes if he says otherwise:* an earlier trigger (say 600 s) means more holds on benign contacts in the eval, and the threshold curve on deck slide 7 shows the price. A later trigger leaves the person less time. Either way the threshold changes only with the owner's approval and a line in `docs/log/DECISIONS.md` (CLAUDE.md §3). We do not change it in the room.

### Three questions
1. **Which of our eight scenarios is unrealistic, and are the kinematics plausible?** The values come from `scenarios/S0*.yaml`. S05 runs straight to Fuel Berth 2 at 1.0 m/s and 6 m depth. S06 orbits Fuel Berth 1 at 40 m radius, 0.6 m/s and 8 m depth. S07, the sea lion, wanders at 1.0 m/s from 2 m depth (0–6 m allowed), with a 0.6 weight on the current in its motion. S08, the debris, rides a 0.05–0.3 m/s current at 1.5 m (`config/current_field.yaml`). Which speed, depth or path would a real vehicle or animal not have in this harbor?
2. **What false alarm have we missed?** The only false alarm we simulate is biologic: 6 per hydrophone per hour, at most 3 m deep, 70% with clicks or breathing (`config/sim.yaml`, `detection`). "Surface vessel with AIS off" and "unauthorized diver" have likelihood rows (`config/likelihoods.yaml`) but no catalog scenario. What else lights up a harbor hydrophone at 2 AM?
3. **What would make the external call to the Coast Guard or EOD easier?** The system drafts the report and a person makes the call. The report has fixed sections: contact; what we know, each fact with its evidence id; what we assume; what we don't know; hypotheses (stress-test); actions already taken; recommended rung; and what a person must decide (`docs/SPEC-LADDER.md` §6). What should come first, what is missing, and what would he drop?

### What to show (about 5 min)
| order | what | on screen |
|---|---|---|
| 1 | step 3 (1:00): S05, Harbor Night, unfiled, closing on the fuel berth, no reply | `scenarios/S05-unfiled-closing-fuel-berth.yaml`; track plot from `make sim-plot scenario=S05` (writes `artifacts/S05.png`; repeat for S06, S07, S08 for question 1) |
| 2 | step 4 (2:00): hold on one berth under standing orders, report handed to the officer, a person approves | `config/ladder.yaml` `criteria` (the decision above) and the report sections for question 3 |
| 3 | step 6 (3:30): twist, network down; same decision on kinematics and hail alone | `config/standing_orders.yaml` `degraded_mode` (allowed rungs watch, ask, slow, hold within scope; never harbor closure or an external call) |

**Have open:** `config/sim.yaml` (detection: p = 0.95·exp(−(range/400 m)²), nothing beyond 800 m, position noise 5 m + 0.03·range, track gaps; `docs/SPEC-SIM.md` §2) · `config/likelihoods.yaml` (every number he may want to edit) · `scenarios/S05-unfiled-closing-fuel-berth.yaml`.

**Coverage note if he asks:** S05 starts about 630 m from Hydrophone 1, so the per-tick detection chance there is about 0.08. It rises steeply as the contact closes. These figures are computed from `config/sim.yaml` and `config/harbor.geojson`; they are simulated, not measured.

<div style="page-break-after: always"></div>

---

## What we will record from each session

Write it down in the room, then file it within the hour. Nothing in `config/` changes without the owner's approval and a `docs/log/DECISIONS.md` line.

**Both sessions**
- [ ] Date, time (`date "+%Y-%m-%d %H:%M %Z"`), who attended.
- [ ] May we quote or name him on the deck and Devpost? (yes / no / paraphrase only)

**Doyle → `docs/DECK-INPUTS.md` and `docs/log/DECISIONS.md`**
- [ ] Registry owner (office or role), and whether it is the same party that pays on Tuesday → DECK-INPUTS "Second customer and their Tuesday"; DECISIONS row; the `signed_by` field in standing orders if it changes (owner approval).
- [ ] Second customer confirmed or replaced (who, and why they pay) → DECK-INPUTS "The three tests" (ordinary-day).
- [ ] Do crews notify the port today: yes / no, channel, lead time, which fields → DECK-INPUTS "Second customer"; note any plan field we lack.
- [ ] VTS LA/LB night watch: who sits there, what authority they hold, what they would do with the report → DECK-INPUTS "The three tests" (crisis).
- [ ] Berth-closure cost: $ per hour (range), source, citable or not → `config/ladder.yaml` `costs.shutdown_usd_per_min_default` = $/h ÷ 60 (owner approval, DECISIONS row naming the source); the eval label then changes from "$0".
- [ ] Any name or intro for a first pilot customer → deck slide 11.

**Wettels → `docs/log/DECISIONS.md` (and a follow-up WO if the owner agrees)**
- [ ] Hold timing answer: keep 300 s, or the figure he gives and why → DECISIONS row; a `config/ladder.yaml` `criteria.hold` change only with owner approval.
- [ ] Scenario he finds unrealistic: id, which value (speed, depth, path), his suggested range → DECISIONS row; scenario edit in a follow-up WO.
- [ ] Missed false-alarm source: what it is, how it looks (depth, speed, sound) → DECISIONS row; candidate new scenario or toggle.
- [ ] Kinematics verdict on S05 / S06 / S07 / S08 → DECISIONS row; deck slide 10 (honesty) if a limit should be stated.
- [ ] Report order for the external call: what goes first, what is missing → note for the SITREP template (WO-042) and deck slide 6.
