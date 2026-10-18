// Quiet the other cases (WO-060E, owner directive 2026-10-17 17:52-17:58: "for all scenarios
// it should only focus on the 1 agent being paged"; "events from all diff agents makes the UI
// too confusing for now and for the demo"). Every case keeps running concurrently; this is
// organisation, not serialisation. During a run the screen tells the focused case's story:
//
//   - CASES: the focused case is one row; every other case folds into one `others · n` row
//     with a severity summary; clicking it opens the rest (a click there pins the focus).
//   - sounds: only the focused case's transitions (and the run-wide twist) make a sound.
//   - key moments: only the focused case's moments, plus one trailing `others` chip.
//   - drawers: 'All transitions (every case)' is hidden; 'All evidence' stays collapsed.
//   - map: other contacts are dimmed and unlabelled; the focused one keeps its label, trail,
//     leader and ellipse; the responder stays as it is.
//
// One `show all` control (hidden in presenter mode) reveals everything for debugging.
// Pure functions over the folded stream: no new data, no stream schema change, nothing here
// can set a rung. Display only.

import type { CaseIndex, CaseSummary, Moment } from './cases'
import { caseOf, severityKey, stateWords } from './cases'
import type { LadderState } from './events'

// --- WO-060E begin: minimal focus (WO-060D's focus policy supersedes this when merged) ---

/** States past ASK: the investigation opened, or a ladder state was reached. */
export const PAST_ASK_STATES = new Set<LadderState>([
  'INVESTIGATING',
  'ADVISING_SLOW',
  'HOLDING',
  'AWAITING_APPROVAL',
  'HANDED_OFF',
  'WATCH_AFTER_HANDOFF',
  'RESOLVED_BENIGN',
  'HAZARD_NOTICE',
  'FAILED',
])

/** States that lock a case as the story: a ladder rung, a handoff, a hazard notice. */
export const LOCKING_STATES = new Set<LadderState>(['ADVISING_SLOW', 'HOLDING', 'AWAITING_APPROVAL', 'HANDED_OFF', 'WATCH_AFTER_HANDOFF', 'HAZARD_NOTICE', 'FAILED'])
/** An investigation that ended here without a rung is a cleared side story, not the story. */
export const BENIGN_END_STATES = new Set<LadderState>(['RESOLVED_BENIGN', 'CLEARED_AT_ASK', 'LOST_VEHICLE'])

/** The seq of an id's first state past ASK, unless it has since ended benign without a rung. */
function escalationSeq(index: CaseIndex, cid: string): number | null {
  const tr = index.tracks[cid]?.transitions ?? []
  const first = tr.find((x) => PAST_ASK_STATES.has(x.to_state))
  if (!first) return null
  if (tr.some((x) => LOCKING_STATES.has(x.to_state))) return first.seq
  return BENIGN_END_STATES.has(tr[tr.length - 1].to_state) ? null : first.seq
}

/** The seq at which an id first reached a locking state (the officer is being paged), or null. */
function lockSeq(index: CaseIndex, cid: string): number | null {
  return index.tracks[cid]?.transitions.find((x) => LOCKING_STATES.has(x.to_state))?.seq ?? null
}

/** The earliest id (stream order) for which `seqOf` gives a seq, as its case, or null. */
function earliest(index: CaseIndex, seqOf: (index: CaseIndex, cid: string) => number | null): string | null {
  let best: { seq: number; cid: string } | null = null
  for (const cid of index.order) {
    const seq = seqOf(index, cid)
    if (seq !== null && (!best || seq < best.seq)) best = { seq, cid }
  }
  return best ? caseOf(index, best.cid) : null
}

/** Sticky focus (the paged case): the first case that reached a rung or a handoff (it never
 * lets go: a locked case stays locked); before that, the first case past ASK that is still
 * open; else the first detected case; else null. It moves on its own only when a case is
 * paged first, or an open investigation ends benign (its story is over). */
export function stickyFocusCase(index: CaseIndex): string | null {
  const paged = earliest(index, lockSeq) ?? earliest(index, escalationSeq)
  if (paged) return paged
  const first = index.order[0]
  return first ? caseOf(index, first) : null
}

/** The focused case: the pinned one (a click) when it exists, else the sticky one. */
export function quietFocus(index: CaseIndex, pinned: string | null): string | null {
  if (pinned && index.tracks[pinned]) return caseOf(index, pinned)
  return stickyFocusCase(index)
}
// --- WO-060E end ---

/** What the CASES panel draws by default: the focused row, then one `others` row. */
export interface CasesView {
  /** Rows drawn as case rows, in order. */
  shown: CaseSummary[]
  /** Cases folded behind the `others` row (empty when open or nothing to fold). */
  folded: CaseSummary[]
  /** Every case other than the focus (what `others · n` counts). */
  others: CaseSummary[]
  /** Whether the `others` row is drawn. */
  othersRow: boolean
}

/** Severity order (most severe first), the same key the cases list sorts by. */
function bySeverity(a: CaseSummary, b: CaseSummary): number {
  const [a0, a1] = severityKey(a.state)
  const [b0, b1] = severityKey(b.state)
  return a0 - b0 || a1 - b1 || a.id.localeCompare(b.id)
}

/** The rows of the CASES panel. `showAll` (debugging) draws the whole list as before; otherwise
 * the focused row comes first and the rest fold behind `others · n` unless `open`. */
export function casesView(cases: CaseSummary[], focus: string | null, open: boolean, showAll: boolean): CasesView {
  if (showAll) return { shown: cases, folded: [], others: cases.filter((c) => c.id !== focus), othersRow: false }
  const mine = cases.filter((c) => c.id === focus)
  const others = cases.filter((c) => c.id !== focus)
  if (!mine.length) {
    // No focus yet: nothing to fold behind (the first case becomes the focus once detected).
    return { shown: cases, folded: [], others, othersRow: false }
  }
  return {
    shown: open ? [...mine, ...others] : mine,
    folded: open ? [] : others,
    others,
    othersRow: others.length > 0,
  }
}

/** The `others` row's severity summary, most severe group first:
 * '4 cleared at ask · 1 watching · 1 handed to a person'. Empty when there are no others. */
export function othersSummary(others: CaseSummary[]): string {
  const counts = new Map<string, number>()
  for (const c of [...others].sort(bySeverity)) counts.set(stateWords(c.state), (counts.get(stateWords(c.state)) ?? 0) + 1)
  return [...counts].map(([w, n]) => `${n} ${w}`).join(' · ')
}

/** The `others · n` label. */
export function othersLabel(others: CaseSummary[]): string {
  return `others · ${others.length}`
}

// ---------------------------------------------------------------- sounds

/** A transition of a contact sounds only when it belongs to the focused case (or show all). */
export function cueAllowed(contactId: string | null | undefined, focusIds: ReadonlySet<string> | null, showAll: boolean): boolean {
  if (showAll || focusIds === null) return true
  return contactId !== null && contactId !== undefined && focusIds.has(contactId)
}

/** The transitions that may sound, in order. Pure. */
export function cueEntries<T extends { contact_id: string }>(entries: T[], focusIds: ReadonlySet<string> | null, showAll: boolean): T[] {
  if (showAll || focusIds === null) return entries
  return entries.filter((e) => cueAllowed(e.contact_id, focusIds, showAll))
}

/** How the sound hook is gated: the focused case's contact ids (null: no gate yet). */
export interface CueGate {
  focusIds: ReadonlySet<string> | null
  showAll: boolean
}

// ---------------------------------------------------------------- key moments

export const QUIET_MOMENTS_SHOWN = 5

/** The focused case's moments (oldest first, the last few), or every moment with show all. */
export function quietMoments(index: CaseIndex, focus: string | null, showAll: boolean, shown = QUIET_MOMENTS_SHOWN): Moment[] {
  if (showAll || !focus) return index.moments
  return index.moments.filter((m) => caseOf(index, m.contact_id) === focus).slice(-shown)
}

// ---------------------------------------------------------------- map

/** Opacity factor for contacts outside the focused case (WO-060D uses the same value). */
export const QUIET_DIM = 0.35

/** Whether a contact is drawn quiet (dimmed, unlabelled, no leader or ellipse). */
export function isQuiet(contactId: string, focusIds: ReadonlySet<string>, hasFocus: boolean, showAll: boolean): boolean {
  return hasFocus && !showAll && !focusIds.has(contactId)
}
