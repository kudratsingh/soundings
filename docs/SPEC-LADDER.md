# Spec — the ladder, the boundary table, standing orders, handoff

The organizers' ladder is watch → ask → slow → hold → hand to a person. Soundings lives on **ask** and **hand to a person**. This spec makes the ladder a typed state machine with deterministic guards, in the pattern of a state-machine agent design the owner has used before (hand-rolled, checkpoint per transition, idempotency key per action, no control derived from model output).

---

## 1. States

```
DETECTED            contact entered the zone; evidence table opened
WATCHING            free probes running (rung: watch)
ASKING              registry + hail + cheap probes (rung: ask)
INVESTIGATING       hypothesis loop per SPEC-INVESTIGATION (rung: ask)
ADVISING_SLOW       standing-orders advisory issued to vessels in one basin (rung: slow)
HOLDING             standing-orders hold on one gate/berth ≤ limit (rung: hold)
AWAITING_APPROVAL   a person has been asked to approve a rung above standing orders
HANDED_OFF          SITREP delivered; handling has ended (terminal for handling)
WATCH_AFTER_HANDOFF observation continues; may update SITREP; may not climb
RESOLVED_BENIGN     terminal
CLEARED_AT_ASK      terminal
HAZARD_NOTICE       terminal (debris/drift notice issued)
LOST_VEHICLE        terminal (search box SITREP to the operator)
RESOLVED_DEPARTED   terminal (left the zone after handoff)
FAILED              terminal (internal error; audit row explains)
```

There is no state for interdiction. The kinetic denylist (`config/denylist.txt`: intercept, halt, capture, pursue, neutralize, net, jam, disable, ram, strike, weapon, interceptor) is enforced by a test over all identifiers, strings, and UI labels.

## 2. Events

`contact_detected, probe_result, posterior_updated, stop_rule_fired(outcome), standing_order_check(result), approval(granted|denied|timeout), contact_departed, contact_persisted(interval), error`.

Added 2026-10-16 (DECISIONS.md 23:05): `timer(limit)`, `scope_exceeded`, `handoff_complete` name the events behind the HOLDING → AWAITING_APPROVAL and HANDED_OFF → WATCH_AFTER_HANDOFF rows.

## 3. Transition table (exhaustive; anything else is impossible)

| from | event | guard | to |
|---|---|---|---|
| DETECTED | contact_detected | — | WATCHING |
| WATCHING | probe_result | free probes complete | ASKING |
| ASKING | stop_rule_fired(CLEARED_AT_ASK) | registry exact ∧ hail valid | CLEARED_AT_ASK |
| ASKING | stop_rule_fired(LOST_VEHICLE) | — | LOST_VEHICLE |
| ASKING | probe_result | not cleared | INVESTIGATING |
| INVESTIGATING | stop_rule_fired(RESOLVED_BENIGN) | — | RESOLVED_BENIGN |
| INVESTIGATING | stop_rule_fired(HAZARD_NOTICE) | — | HAZARD_NOTICE |
| INVESTIGATING | stop_rule_fired(HANDOFF) | standing orders permit `slow` ∧ criteria met | ADVISING_SLOW |
| INVESTIGATING | stop_rule_fired(HANDOFF) | standing orders permit `hold` ∧ criteria met | HOLDING |
| INVESTIGATING | stop_rule_fired(HANDOFF) | nothing pre-authorized | HANDED_OFF |
| ADVISING_SLOW | standing_order_check(hold_permitted) | criteria met | HOLDING |
| ADVISING_SLOW | standing_order_check(not_permitted) | — | HANDED_OFF |
| HOLDING | timer(limit) ∨ scope exceeded | — | AWAITING_APPROVAL |
| HOLDING | contact_departed | — | RESOLVED_DEPARTED |
| AWAITING_APPROVAL | approval(granted) | approval token valid ∧ scope matches | HANDED_OFF (with the approved action recorded) |
| AWAITING_APPROVAL | approval(denied) | — | HANDED_OFF (hold released) |
| AWAITING_APPROVAL | approval(timeout) | backup approver exists | AWAITING_APPROVAL (backup paged; hold stays within limit) |
| AWAITING_APPROVAL | approval(timeout) | no backup | HANDED_OFF (hold released at limit; SITREP flags "unreachable") |
| HANDED_OFF | — | — | WATCH_AFTER_HANDOFF |
| WATCH_AFTER_HANDOFF | contact_departed | — | RESOLVED_DEPARTED |
| WATCH_AFTER_HANDOFF | contact_persisted | — | WATCH_AFTER_HANDOFF (SITREP update) |
| any non-terminal | error | — | FAILED |

Property test: generate random event sequences; assert the machine never enters a state not in §1, never climbs a rung without a guard returning `permit`, and never leaves `HANDED_OFF` except to `WATCH_AFTER_HANDOFF`.

## 4. Boundary table (config: `config/ladder.yaml`)

This is the answer to the sponsor's question. It is data, shown on a slide and in the UI, and the guards read it.

| rung | platform decides alone | under signed standing orders | a person must approve |
|---|---|---|---|
| watch | track, score, pattern-of-life compare | — | — |
| ask | registry lookup, acoustic hail, camera cue, request a look | — | — |
| slow | — | advisory to vessels in **one** basin, ≤ 30 min | harbor-wide slowdown |
| hold | — | hold **one** gate or berth op, ≤ 15 min | longer, wider, or a second hold |
| hand to a person | SITREP with evidence + recommended rung | — | the call to USCG / EOD, any closure, anything after |

Guard inputs: `threat_mass`, `benign_mass`, `time_to_nearest_critical_asset`, `zone_criticality`, `standing_orders.signed`, `standing_orders.scope`, `active_holds`, `approver.reachable`, `approval.token`.

Criteria for the standing-orders rungs (defaults; owner approval to change):
- `slow`: `threat_mass ≥ 0.25` ∧ `time_to_nearest_critical_asset < 600 s`.
- `hold`: `threat_mass ≥ 0.35` ∧ `time_to_nearest_critical_asset < 300 s` ∧ `zone_criticality ≥ 0.7` ∧ `active_holds == 0`.

## 5. Standing orders (config: `config/standing_orders.yaml`)

```yaml
signed_by: "Watch Commander (demo)"
signed_at: "2026-10-16T21:00:00-07:00"
valid_until: "2026-10-18T23:59:00-07:00"
scope:
  slow:
    basins: [inner_harbor_west]
    max_minutes: 30
  hold:
    max_gates: 1
    max_minutes: 15
approvers:
  primary: {name: "Watch Officer", channel: "ui", timeout_s: 120}
  backup:  {name: "Duty Supervisor", channel: "sms_sim", timeout_s: 180}
degraded_mode:
  when: ["network_down", "approver_unreachable"]
  allowed_rungs: [watch, ask, slow, hold]   # within scope above; never beyond
  never: [harbor_closure, external_call]
```

The signature is a hash over the file; an unsigned or expired file means nothing above `ask` executes. This is what the system does at 3 AM when the network is down and nobody answers — and it is the answer to the kickoff deck's crisis test.

## 6. SITREP (LLM writer with template fallback)

Fixed sections: `Contact` · `What we know (facts, each with evidence ids)` · `What we assume` · `What we don't know` · `Hypotheses (posterior, labeled stress-test)` · `Actions already taken (rung, time, standing-order clause)` · `Recommended rung (computed; not editable by the writer)` · `What a person must decide` · `Search box (LOST_VEHICLE only)`.

The writer receives the evidence table and the computed recommendation; the guard verifies the output's recommended rung equals the computed one, every fact line cites an evidence id that exists, and no denylist word appears. On any failure the template fallback renders.

## 7. Approval

An approval is a signed token `{contact_id, action, scope, approver, issued_at, expires_at}` created only by the UI button or the simulated SMS reply. The guard checks token validity, scope equality with the requested action, and expiry. The audit log records the token hash. An approval can never widen scope beyond what was requested.

## 8. Audit log

Append-only SQLite table with a hash chain (`prev_hash, row_hash`). Rows: transitions, probes (with idempotency key), guard decisions (with inputs), approvals, SITREPs, LLM calls (role, fixture key, accepted|rejected, violation reason). `soundings audit verify` recomputes the chain. Retention label: 30 days (demo).

---

## Amendments (2026-10-17)

Appended; the text above is unchanged except the source line in the opening paragraph (no other repository is named; owner directive 2026-10-17). Each item names its source. Where an item differs from the text above, the item wins.

**Events (§2).** `timer(limit)`, `scope_exceeded`, `handoff_complete` were added 2026-10-16 (DECISIONS 23:05); see the note under §2. `HOLDING × timer(limit) ∨ scope exceeded` is two table rows (DECISIONS 23:15, WO-040).

**Table order (§3; DECISIONS 23:05).** When the `slow` and `hold` criteria both pass, the `slow` row fires first (table order; gentlest rung first). "Nothing pre-authorized" is read literally.

**Hold at its limit while AWAITING_APPROVAL (§3; DECISIONS 23:25; WO-043 part A ruling 06:12).**
- A hold is released at its limit even while AWAITING_APPROVAL; the state stays AWAITING_APPROVAL until the approval resolves. The release is recorded (ledger, listeners, audit) without a transition.
- `HOLDING × timer(hold_limit)` adds `release_hold` after `request_approval` (`LadderMachine(strict_holds=True)`, always set by the driver).
- A granted `hold_longer` is the hold in force; `WATCH_AFTER_HANDOFF × contact_departed` releases it. Lifting a restriction is the only non-watch action allowed after HANDED_OFF.

**SITREP timing (§6; DECISIONS 23:05; WO-042 part B 07:30).**
- A **draft** SITREP is written at AWAITING_APPROVAL; the **final** SITREP at HANDED_OFF.
- The recommended rung is the investigation's computed rung, else the machine's rung (the S05 draft says hand_to_person while the machine sits at hold). The final SITREP counts as one handoff interruption.

**Citation exemption (§6; WO-042).** Fact lines cite ≥ 1 evidence row id that exists. Lines under "What we don't know" that name a missing feature carry the prefix `missing: <feature>` instead of a row id (an absent feature has no row); this is the only exemption from the row-exists check. Every other line cites ≥ 1 existing row id, or the template fallback renders.

**Default headless approver (§7; DECISIONS 23:05).** In sim and headless runs the approver grants 60 s after the request. Options `denied` and `unreachable` exist for the twist toggle (`approver_unreachable`).

**Signature policy (§5; DECISIONS 23:05; WO-041).** The standing-orders signature is sha256 over the canonical orders, committed for "Watch Commander (demo)". It is tamper-evident, not authenticated, and labeled so in the About text; HMAC/PKI is out of scope. Unparseable or off-shape orders verify as `invalid` (never signed). The `standing_orders_expired` toggle is wired through an expired epoch (DECISIONS 23:25).

**Approval token TTL (§7; DECISIONS 23:25).** The token TTL lives in the additive `approval.token_ttl_s` block of `config/ladder.yaml` (600 s). Not a threshold change. Token `approver` is the approvers key (primary/backup), not the display name.

**Separation advisories (§4 slow row; WO-083; DECISIONS 06:37; CLAUDE.md §8 A9).**
- A separation advisory is a `slow` (`advisory_one_basin`) and goes through the same standing-orders slow guard in `soundings/ladder/guards.py`.
- Its criterion replaces the threat_mass/time criteria: CPA < `separation_m` ∧ 0 < TCPA < `advisory_window_s`.
- Standing-orders slow scope stays one basin (`inner_harbor_west`). Out-of-scope or unsigned/expired-orders advisories are BLOCKED, shown as "advisory blocked: out of standing-orders scope", audited with their geometry, and **not** counted as interruptions.
- No ladder state is added; no filed contact leaves CLEARED_AT_ASK. The operator accepts or ignores an advisory.

**Responder boundary rows (§4; WO-088; CLAUDE.md §8 A2; DECISIONS 07:51).**

| responder action | platform decides alone | a person must approve | never |
|---|---|---|---|
| dispatch to look, inside the geofence, at standoff ≥ 25 m | ✓ (rung ask) | | |
| leave the geofence | | ✓ | |
| standoff below 25 m | | ✓ | |
| a second vehicle | | ✓ | |
| any responder action after HANDED_OFF | | | ✓ |

- Always: re-stations if the contact moves, never closes faster than the contact, never between the contact and an asset, one dispatch per contact; the planner requests a look, never a position (guard-enforced, logged to VIOLATIONS.md). §2.1 and §2.5 of CLAUDE.md stand.

**Audit commit semantics (§8; WO-042; owner directive 2026-10-17).**
- One row per transition, probe (with idempotency key), guard decision (with inputs hash and granted scope), approval (token hash only), action (with idempotency key), SITREP, and model call (`llm_call`: role, fixture key, path, accepted|rejected, reason).
- Every mutation carries an idempotency key (`stable_hash([contact_id, kind, scope, from_state, to_state, event.tick])` for ladder actions, WO-040); a re-delivered event does not re-execute.
- If the audit write fails, the action does not commit. On `main` today a refused append leaves the log unchanged; tying the action's commit to the append is the owner's rule and is verified by the audit tests that land with it.
- Hash chain `row_hash = sha256(prev_hash + canonical_json(row))`; `soundings audit verify` recomputes it.
