# Spec — the ladder, the boundary table, standing orders, handoff

The organizers' ladder is watch → ask → slow → hold → hand to a person. Soundings lives on **ask** and **hand to a person**. This spec makes the ladder a typed state machine with deterministic guards, in the same pattern as the owner's Incident Commander (hand-rolled, checkpoint per transition, idempotency key per action, no control derived from model output).

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
