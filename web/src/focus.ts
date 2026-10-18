// Focus one case (WO-060D, owner directive 2026-10-17 18:03). Many cases run at once; the
// screen tells ONE case's story from start to finish. The focus policy has three modes:
//
//   sticky (default) — the first case that passes ASK (enters INVESTIGATING or any ladder
//     state) becomes the focus and stays the focus until the run ends or the user clicks
//     another case; before any case passes ASK the focus is the first detected case. It never
//     switches on its own, even when a more severe case appears. One refinement (S05 opens
//     and clears a benign investigation in one tick before the hero is asked): an
//     investigation that ends benign without a rung is a side story, so it stops counting
//     and the next case past ASK takes the focus.
//   auto — the pre-WO-060D behaviour: the most severe case, re-chosen on every update.
//   pinned — the user clicked a case; it holds until the user picks another or returns to
//     sticky.
//
// Everything here is a pure function over the folded stream (CaseIndex): no new data, no
// stream schema change, and nothing here can set a rung. Display only.

import type { CaseIndex, CaseSummary, Moment } from './cases'
import { caseOf, mostSevere, stateWords } from './cases'
import type { LadderState } from './events'

export type FocusMode = 'sticky' | 'auto' | 'pinned'

/** What sticky focuses on: the presenter may name one per step (docs/demo-script.yaml `focus`). */
export type FocusPolicy = 'first_escalation' | 'first_detected' | { contact: string }

export interface FocusState {
  mode: FocusMode
  /** The contact (or case) id the user clicked, in pinned mode. */
  pinned: string | null
  policy: FocusPolicy
}

export type FocusAction =
  | { type: 'pin'; id: string }
  | { type: 'sticky' }
  | { type: 'auto' }
  /** A new run starts: back to sticky with the given (or default) policy. */
  | { type: 'reset'; policy?: FocusPolicy }

export const initialFocus = (policy: FocusPolicy = 'first_escalation'): FocusState => ({ mode: 'sticky', pinned: null, policy })

export function focusReducer(s: FocusState, a: FocusAction): FocusState {
  switch (a.type) {
    case 'pin':
      return s.mode === 'pinned' && s.pinned === a.id ? s : { ...s, mode: 'pinned', pinned: a.id }
    case 'sticky':
      return { ...s, mode: 'sticky', pinned: null }
    case 'auto':
      return { ...s, mode: 'auto', pinned: null }
    case 'reset':
      return initialFocus(a.policy ?? 'first_escalation')
  }
}

/** The mode control cycles: pinned → sticky, sticky → auto, auto → sticky. */
export function nextModeAction(mode: FocusMode): FocusAction {
  return mode === 'sticky' ? { type: 'auto' } : { type: 'sticky' }
}

/** Parse the presenter's `focus` value: first_escalation | first_detected | <contact id>. */
export function parsePolicy(value: string | null | undefined): FocusPolicy {
  if (!value || value === 'first_escalation') return 'first_escalation'
  if (value === 'first_detected') return 'first_detected'
  return { contact: value }
}

/** States past ASK: the investigation opened, or a ladder state was reached. */
export const PAST_ASK = new Set<LadderState>([
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
export const LOCKING = new Set<LadderState>(['ADVISING_SLOW', 'HOLDING', 'AWAITING_APPROVAL', 'HANDED_OFF', 'WATCH_AFTER_HANDOFF', 'HAZARD_NOTICE', 'FAILED'])
/** An investigation that ended here (without a rung) is a cleared side story, not the story. */
export const BENIGN_END = new Set<LadderState>(['RESOLVED_BENIGN', 'CLEARED_AT_ASK', 'LOST_VEHICLE'])

/** The first escalation of one contact id that still counts: the seq of its first state past
 * ASK, unless the id has since ended benign without ever reaching a locking state. */
function escalationSeq(index: CaseIndex, cid: string): number | null {
  const tr = index.tracks[cid]?.transitions ?? []
  const first = tr.find((x) => PAST_ASK.has(x.to_state))
  if (!first) return null
  if (tr.some((x) => LOCKING.has(x.to_state))) return first.seq
  const state = tr[tr.length - 1].to_state
  return BENIGN_END.has(state) ? null : first.seq
}

/** The case that passed ASK first (earliest stream position) and is still the story: open, or
 * locked by a rung or a handoff. Stable: the stream is ordered, a locked id never unlocks, and
 * a re-acquired id escalates later than anything already chosen. The only move it makes on its
 * own is when an open investigation ends benign (its story is over); then the next one counts. */
export function firstEscalated(index: CaseIndex): string | null {
  let best: { seq: number; cid: string } | null = null
  for (const cid of index.order) {
    const seq = escalationSeq(index, cid)
    if (seq !== null && (!best || seq < best.seq)) best = { seq, cid }
  }
  return best ? caseOf(index, best.cid) : null
}

/** The first detected case (first-seen contact's lineage), or null. */
export function firstDetected(index: CaseIndex): string | null {
  const first = index.order[0]
  return first ? caseOf(index, first) : null
}

/** The sticky focus under a policy. */
export function stickyFocus(index: CaseIndex, policy: FocusPolicy): string | null {
  if (policy === 'first_detected') return firstDetected(index)
  if (typeof policy === 'object' && index.tracks[policy.contact]) return caseOf(index, policy.contact)
  return firstEscalated(index) ?? firstDetected(index)
}

/** The focused case id under the current state. */
export function focusedCase(index: CaseIndex, cases: CaseSummary[], s: FocusState): string | null {
  if (s.mode === 'pinned' && s.pinned && index.tracks[s.pinned]) return caseOf(index, s.pinned)
  if (s.mode === 'auto') return mostSevere(cases)
  return stickyFocus(index, s.policy)
}

// ---------------------------------------------------------------- key moments

export const FOCUS_MOMENTS_SHOWN = 5

/** The focused case's moments, oldest first (the last FOCUS_MOMENTS_SHOWN of them). */
export function focusMoments(index: CaseIndex, focus: string | null, shown = FOCUS_MOMENTS_SHOWN): Moment[] {
  if (!focus) return []
  return index.moments.filter((m) => caseOf(index, m.contact_id) === focus).slice(-shown)
}

/** One trailing chip for every other case, by current state: 'others · 3 cleared at ask · 1 handed to a person'. */
export function othersLine(cases: CaseSummary[], focus: string | null): string {
  const others = cases.filter((c) => c.id !== focus)
  if (!others.length) return ''
  const counts = new Map<string, number>()
  for (const c of others) counts.set(stateWords(c.state), (counts.get(stateWords(c.state)) ?? 0) + 1)
  return `others · ${[...counts].map(([w, n]) => `${n} ${w}`).join(' · ')}`
}

// ---------------------------------------------------------------- the trace line

export interface TracePart {
  state: LadderState
  /** Sim seconds (tick × tick_s) when the state was entered. */
  t_s: number
  /** Shown only when it differs from the previous part's time. */
  showTime: boolean
  /** Set on the first part of a re-acquired id. */
  reacquired?: string
}

/** The sequence of states of a case (all its ids, in stream order) with times. Built from the
 * transitions already streamed; nothing new is computed. A re-acquired id is marked where it
 * first appears; a repeated state of the same id (watch-after-handoff updates) is shown once. */
export function traceParts(index: CaseIndex, members: string[], tickS: number): TracePart[] {
  type Ev = { seq: number; tick: number; cid: string; state: LadderState; start: boolean }
  const evs: Ev[] = []
  members.forEach((cid) => {
    const t = index.tracks[cid]
    if (!t) return
    const first = t.transitions[0]
    // The id's start (DETECTED at its first fix) sorts just before its first transition.
    evs.push({ seq: first ? first.seq - 0.5 : Number.MAX_SAFE_INTEGER, tick: t.firstTick, cid, state: first?.from_state ?? 'DETECTED', start: true })
    for (const x of t.transitions) evs.push({ seq: x.seq, tick: x.tick, cid, state: x.to_state, start: false })
  })
  evs.sort((a, b) => a.seq - b.seq)
  const out: TracePart[] = []
  const lastOf: Record<string, LadderState> = {}
  let lastT: number | null = null
  for (const e of evs) {
    if (!e.start && lastOf[e.cid] === e.state) continue
    lastOf[e.cid] = e.state
    const t_s = Math.round(e.tick * tickS)
    const reacquired = e.start && e.cid !== members[0] ? e.cid : undefined
    out.push({ state: e.state, t_s, showTime: t_s !== lastT, ...(reacquired ? { reacquired } : {}) })
    lastT = t_s
  }
  return out
}

export function traceText(parts: TracePart[]): string {
  return parts
    .map((p, i) => `${i > 0 ? (p.reacquired ? ` ⟲ ${p.reacquired} ` : ' → ') : ''}${p.state}${p.showTime ? ` t ${p.t_s} s` : ''}`)
    .join('')
}

// ---------------------------------------------------------------- the transition that entered a step

const STEP_ENTRY: Record<string, (s: LadderState, from: LadderState) => boolean> = {
  watch: (s) => s === 'WATCHING',
  ask: (s) => s === 'ASKING',
  investigate: (s) => s === 'INVESTIGATING',
  recommendation: (_s, from) => from === 'INVESTIGATING',
  ladder: (s) => s === 'ADVISING_SLOW' || s === 'HOLDING' || s === 'AWAITING_APPROVAL',
  handoff: (s) => s === 'HANDED_OFF' || s === 'LOST_VEHICLE' || s === 'HAZARD_NOTICE',
  after: (s) => s === 'WATCH_AFTER_HANDOFF' || s === 'RESOLVED_DEPARTED',
}

/** 'WATCHING → ASKING · hail_due' for the transition that entered a step ('' when none). */
export function enteredBy(index: CaseIndex, contactId: string, kind: string): string {
  const t = index.tracks[contactId]
  if (!t) return ''
  if (kind === 'detected') return 'first fix'
  const match = STEP_ENTRY[kind]
  const tr = match ? t.transitions.find((x) => match(x.to_state, x.from_state)) : undefined
  return tr ? `${tr.from_state} → ${tr.to_state} · ${tr.reason_code || tr.event}` : ''
}
