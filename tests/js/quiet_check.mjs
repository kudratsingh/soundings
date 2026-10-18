// WO-060E: drives the pure functions in web/src/quiet.ts (and the cue mapping in sfx.ts) over a
// synthetic stream and prints one JSON object of observations for tests/test_quiet_others.py.
// Run with: node --experimental-strip-types tests/js/quiet_check.mjs (Node >= 22.6).
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
const quiet = await import(new URL('quiet.ts', web).href)
const sfx = await import(new URL('sfx.ts', web).href)

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
const ids = (rows) => rows.map((c) => c.id)

// Six contacts; c-0002 is the first past ask and the first paged; the rest clear or hand off later.
let ix = cases.foldCases(cases.emptyCaseIndex(), [tick(1, ['c-0001', 'c-0002', 'c-0003', 'c-0004', 'c-0005', 'c-0006'])])
ix = cases.foldCases(ix, ['c-0001', 'c-0002', 'c-0003', 'c-0004', 'c-0005', 'c-0006'].map((c) => tr(1, c, 'DETECTED', 'WATCHING')))
out.focus_before_ask = quiet.quietFocus(ix, null)
const early = [tr(4, 'c-0001', 'WATCHING', 'ASKING'), tr(5, 'c-0002', 'WATCHING', 'ASKING'), tr(5, 'c-0003', 'WATCHING', 'ASKING')]
ix = cases.foldCases(ix, early)
ix = cases.foldCases(ix, [
  tr(8, 'c-0002', 'ASKING', 'INVESTIGATING'),
  tr(8, 'c-0002', 'INVESTIGATING', 'ADVISING_SLOW'),
  tr(8, 'c-0001', 'ASKING', 'CLEARED_AT_ASK'),
  tr(9, 'c-0003', 'ASKING', 'CLEARED_AT_ASK'),
  tr(9, 'c-0004', 'WATCHING', 'ASKING'),
  tr(10, 'c-0004', 'ASKING', 'CLEARED_AT_ASK'),
  tr(11, 'c-0005', 'WATCHING', 'ASKING'),
  tr(12, 'c-0005', 'ASKING', 'CLEARED_AT_ASK'),
  tr(13, 'c-0006', 'WATCHING', 'ASKING'),
  tr(14, 'c-0006', 'ASKING', 'INVESTIGATING'),
  tr(15, 'c-0006', 'INVESTIGATING', 'HANDED_OFF'),
  tr(22, 'c-0002', 'ADVISING_SLOW', 'HOLDING'),
])
const summaries = cases.summarize(ix, rungOf)
const focus = quiet.quietFocus(ix, null)
out.focus = focus
out.focus_pinned = quiet.quietFocus(ix, 'c-0005')
out.focus_pinned_unknown = quiet.quietFocus(ix, 'c-0999')

// CASES: the default (folded) view, the open view, show all.
const folded = quiet.casesView(summaries, focus, false, false)
out.default_shown = ids(folded.shown)
out.default_folded_n = folded.folded.length
out.default_others_row = folded.othersRow
out.others_label = quiet.othersLabel(folded.others)
out.others_summary = quiet.othersSummary(folded.others)
const opened = quiet.casesView(summaries, focus, true, false)
out.open_shown = ids(opened.shown)
out.open_others_row = opened.othersRow
const all = quiet.casesView(summaries, focus, false, true)
out.show_all_shown = ids(all.shown)
out.show_all_others_row = all.othersRow
out.no_focus_shown_n = quiet.casesView(summaries, null, false, false).shown.length
out.total = summaries.length

// Sounds: the paged case's cues only; show all lets every case through.
const focusIds = new Set(summaries.find((c) => c.id === focus).members)
const timeline = [...early, ...['c-0006', 'c-0002'].map((c) => tr(30, c, 'HOLDING', 'HANDED_OFF'))].map((e) => ({ ...e.payload, tick: e.tick, seq: e.seq }))
out.cues_focused = sfx.soundsForTransitions(quiet.cueEntries(timeline, focusIds, false))
out.cues_focused_contacts = quiet.cueEntries(timeline, focusIds, false).map((e) => e.contact_id)
out.cues_show_all_contacts = quiet.cueEntries(timeline, focusIds, true).map((e) => e.contact_id)
out.cues_no_focus_n = quiet.cueEntries(timeline, null, false).length
out.responder_other = quiet.cueAllowed('c-0006', focusIds, false)
out.responder_mine = quiet.cueAllowed('c-0002', focusIds, false)
out.responder_none = quiet.cueAllowed(null, focusIds, false)
out.twist_sound = sfx.TWIST_SOUND

// Key moments: the focused case's only (show all: every case).
out.moments_cases = [...new Set(quiet.quietMoments(ix, focus, false).map((m) => cases.caseOf(ix, m.contact_id)))]
out.moments_show_all_cases = [...new Set(quiet.quietMoments(ix, focus, true).map((m) => cases.caseOf(ix, m.contact_id)))].sort()

// Map: quiet = outside the focused case, only while there is a focus and show all is off.
out.map_quiet = ['c-0001', 'c-0002'].map((c) => quiet.isQuiet(c, focusIds, true, false))
out.map_quiet_show_all = quiet.isQuiet('c-0001', focusIds, true, true)
out.map_quiet_no_focus = quiet.isQuiet('c-0001', focusIds, false, false)
out.dim = quiet.QUIET_DIM

console.log(JSON.stringify(out))
