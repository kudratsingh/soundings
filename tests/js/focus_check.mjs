// WO-060D: drives the pure focus functions in web/src/focus.ts over a synthetic stream and
// prints one JSON object of observations for tests/test_focus_policy.py to assert on.
// Run with: node --experimental-strip-types tests/js/focus_check.mjs (Node >= 22.6).
import { register } from 'node:module'

// The web sources import siblings without an extension (bundler resolution): add `.ts`.
register(
  'data:text/javascript,' +
    encodeURIComponent(`
export async function resolve(spec, ctx, next) {
  if ((spec.startsWith('./') || spec.startsWith('../')) && !/\\.[cm]?[jt]sx?$/.test(spec)) {
    try { return await next(spec + '.ts', ctx) } catch {}
  }
  return next(spec, ctx)
}`),
)

const web = new URL('../../web/src/', import.meta.url)
const cases = await import(new URL('cases.ts', web).href)
const focus = await import(new URL('focus.ts', web).href)

let seq = 0
const tick = (t, ids) => ({ type: 'tick', seq: ++seq, tick: t, payload: { contacts: ids.map((contact_id) => ({ contact_id })) } })
const tr = (t, cid, from_state, to_state) => ({
  type: 'transition',
  seq: ++seq,
  tick: t,
  payload: { contact_id: cid, from_state, to_state, event: 'e', reason_code: `${from_state}->${to_state}`.toLowerCase(), evidence_row_ids: [], guard: null },
})
const rungOf = {}
const out = {}
const sticky = focus.initialFocus()
const auto = focus.focusReducer(sticky, { type: 'auto' })
const at = (index, state) => focus.focusedCase(index, cases.summarize(index, rungOf), state)

// t1: two contacts detected; nobody past ask -> sticky is the first detected case.
let ix = cases.foldCases(cases.emptyCaseIndex(), [tick(1, ['c-0001', 'c-0002'])])
ix = cases.foldCases(ix, [tr(1, 'c-0001', 'DETECTED', 'WATCHING'), tr(1, 'c-0002', 'DETECTED', 'WATCHING')])
out.before_escalation = at(ix, sticky)
// t5: c-0002 is asked and escalates first (the hero); c-0001 clears at ask.
ix = cases.foldCases(ix, [tr(5, 'c-0002', 'WATCHING', 'ASKING'), tr(5, 'c-0001', 'WATCHING', 'ASKING')])
ix = cases.foldCases(ix, [tr(8, 'c-0002', 'ASKING', 'INVESTIGATING'), tr(8, 'c-0001', 'ASKING', 'CLEARED_AT_ASK')])
out.first_escalation = focus.firstEscalated(ix)
out.after_escalation = at(ix, sticky)
// t10: a later, more severe case appears (c-0003 straight to HOLDING) while the hero is
// still investigating: sticky holds, auto jumps.
ix = cases.foldCases(ix, [tick(10, ['c-0003'])])
ix = cases.foldCases(ix, [
  tr(10, 'c-0003', 'DETECTED', 'WATCHING'),
  tr(11, 'c-0003', 'WATCHING', 'ASKING'),
  tr(12, 'c-0003', 'ASKING', 'INVESTIGATING'),
  tr(13, 'c-0003', 'INVESTIGATING', 'HOLDING'),
])
out.more_severe_sticky = at(ix, sticky)
out.more_severe_auto = at(ix, auto)
// The hero holds and is handed off; sticky stays on it the whole way.
ix = cases.foldCases(ix, [tr(20, 'c-0002', 'INVESTIGATING', 'HOLDING')])
out.hero_holding = at(ix, sticky)
ix = cases.foldCases(ix, [tr(30, 'c-0002', 'HOLDING', 'HANDED_OFF')])
out.hero_handed_off = at(ix, sticky)
// Pinned holds whatever happens next; sticky returns to the hero.
const pinned = focus.focusReducer(sticky, { type: 'pin', id: 'c-0001' })
ix = cases.foldCases(ix, [tick(40, ['c-0004']), tr(40, 'c-0004', 'DETECTED', 'WATCHING'), tr(41, 'c-0004', 'WATCHING', 'HANDED_OFF')])
out.pinned_mode = pinned.mode
out.pinned_holds = at(ix, pinned)
out.back_to_sticky = at(ix, focus.focusReducer(pinned, { type: 'sticky' }))
out.mode_cycle = [focus.nextModeAction('pinned').type, focus.nextModeAction('sticky').type, focus.nextModeAction('auto').type]
// Policies the presenter may name.
out.policy_first_detected = at(ix, focus.initialFocus(focus.parsePolicy('first_detected')))
out.policy_contact = at(ix, focus.initialFocus(focus.parsePolicy('c-0003')))
out.policy_unknown_contact = at(ix, focus.initialFocus(focus.parsePolicy('c-0999')))
out.reset = focus.focusReducer(pinned, { type: 'reset' })
// Key moments of the focused case only, oldest first; everyone else in one chip.
out.moments = focus.focusMoments(ix, 'c-0002').map((m) => `${m.contact_id}:${m.label}`)
out.others = focus.othersLine(cases.summarize(ix, rungOf), 'c-0002')
// The trace line and the transition that entered a step.
out.trace = focus.traceText(focus.traceParts(ix, ['c-0002'], 2))
out.entered_ask = focus.enteredBy(ix, 'c-0002', 'ask')
out.entered_handoff = focus.enteredBy(ix, 'c-0002', 'handoff')
out.entered_detected = focus.enteredBy(ix, 'c-0002', 'detected')
// A re-acquired lineage continues the hero's case: the trace shows both ids.
ix = cases.foldCases(ix, [tick(50, ['c-0005'])])
ix = cases.foldCases(ix, [
  {
    type: 'evidence_row',
    seq: ++seq,
    tick: 50,
    payload: { row_id: 'r1', row: { contact_id: 'c-0005', t: 50, feature: 'reacquired_from', value: 'c-0002', assumes: 'same track', kind: 'assumption', source: 'matcher', probe_id: null, unit: 'id', confidence: 0.8 } },
  },
  tr(50, 'c-0005', 'DETECTED', 'WATCHING'),
])
out.reacquired_focus = at(ix, sticky)
out.reacquired_trace = focus.traceText(focus.traceParts(ix, ['c-0002', 'c-0005'], 2))
// S05's shape: a benign investigation opens and clears in one tick before the hero is asked;
// the side story stops counting and the hero takes the focus. A re-acquired id of the side
// story that escalates later never steals the focus back.
let jx = cases.foldCases(cases.emptyCaseIndex(), [tick(1, ['c-0101', 'c-0102'])])
jx = cases.foldCases(jx, [tr(3, 'c-0101', 'ASKING', 'INVESTIGATING')])
out.side_story_open = at(jx, sticky)
jx = cases.foldCases(jx, [tr(3, 'c-0101', 'INVESTIGATING', 'RESOLVED_BENIGN')])
out.side_story_cleared = at(jx, sticky)
jx = cases.foldCases(jx, [tr(8, 'c-0102', 'ASKING', 'INVESTIGATING'), tr(9, 'c-0102', 'INVESTIGATING', 'HANDED_OFF')])
out.hero_after_side_story = at(jx, sticky)
jx = cases.foldCases(jx, [tick(12, ['c-0103'])])
jx = cases.foldCases(jx, [
  {
    type: 'evidence_row',
    seq: ++seq,
    tick: 12,
    payload: { row_id: 'r2', row: { contact_id: 'c-0103', t: 12, feature: 'reacquired_from', value: 'c-0101', assumes: 'same track', kind: 'assumption', source: 'matcher', probe_id: null, unit: 'id', confidence: 0.8 } },
  },
  tr(14, 'c-0103', 'ASKING', 'INVESTIGATING'),
  tr(15, 'c-0103', 'INVESTIGATING', 'HOLDING'),
])
out.side_story_reacquired = at(jx, sticky)
console.log(JSON.stringify(out))
