// Presenter mode data (WO-062P): the shot list from GET /api/demo-script, the presenter
// log, run starts for a step, and where a step's spotlight points. No network beyond the
// local API; nothing here touches a live model.

import type { RunSummary } from './api'
import { RUN_SOURCE } from './api'

export type StepKind = 'catalog' | 'action' | 'live' | 'twist' | 'eval'

export interface PresenterStep {
  index: number
  t: string
  mark_s: number
  kind: StepKind
  label: string
  caption?: string
  say: string
  speed?: '1x' | '20x'
  speed_factor: number | null
  scenario?: string
  seed?: number
  action?: string
  beats?: string[]
  toggles?: string[]
  run_ref: string | null
  continue_run?: boolean
  spotlight?: string
  pause_for_click?: boolean
  pending: string[]
  // --- WO-088 begin ---
  subcaption?: string
  // --- WO-088 end ---
  // --- WO-060D begin: which case the screen follows when the step's run starts ---
  focus?: string
  // --- WO-060D end ---
  // --- WO-062C begin ---
  auto_file?: AutoFile
  // --- WO-062C end ---
}

// --- WO-062C begin: the demo's scripted plan filing and the evidence card's eval table ---
/** A POST /api/plans body, resolved by the server for today + day_offset. */
export type PlanBody = Record<string, string | number | null>

export interface AutoFile {
  at_s: number
  accept_after_s: number
  accept_at_s: number
  in_force: PlanBody[]
  plan: PlanBody
}

/** The fixed notes of an `auto` presenter-log entry (soundings/demo/presenter.py AutoNote). */
export const AUTO_NOTES = {
  jobsFiled: 'auto: in-force jobs filed (scripted)',
  planFiled: 'auto: plan filed (stands in for the phone)',
  proposalAccepted: 'auto: proposal accepted',
} as const
export type AutoNoteText = (typeof AUTO_NOTES)[keyof typeof AUTO_NOTES]

/** `&auto=0` leaves the filing to a judge's phone on stage. */
export function autoFileEnabled(search: string = window.location.search): boolean {
  return new URLSearchParams(search).get('auto') !== '0'
}

/** File one plan through the registry endpoint the /registry form uses. True when it is
 * in the store afterwards (201, or 422 duplicate_plan_id: filed on an earlier pass). */
export async function filePlan(body: PlanBody): Promise<boolean> {
  const res = await fetch('/api/plans', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (res.status === 201) return true
  const data = (await res.json().catch(() => ({}))) as { reason?: string }
  if (res.status === 422 && data.reason === 'duplicate_plan_id') return true
  console.info(`presenter: auto filing ${String(body.plan_id)}: HTTP ${res.status} ${JSON.stringify(data)}`)
  return false
}

export function logAuto(index: number, note: AutoNoteText): void {
  fetch('/api/presenter-log', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index, kind: 'auto', note }),
  }).catch(() => undefined)
}

export type EvalMetric =
  | 'at_or_below_ask_pct'
  | 'missed_threats'
  | 'false_shutdowns'
  | 'median_s_seen_to_handled'
  | 'interruptions_per_hour'
  | 'shutdown_minutes'

export interface EvalRow extends Record<EvalMetric, number | null> {
  policy: string
  label: string
}

export interface EvalSummary {
  label: string
  suite: string | null
  seeds: number[] | null
  rows: EvalRow[]
}

/** GET /api/eval/summary; null when no report exists (404: run make eval). */
export async function fetchEvalSummary(): Promise<EvalSummary | null> {
  const res = await fetch('/api/eval/summary')
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`/api/eval/summary: HTTP ${res.status}`)
  return (await res.json()) as EvalSummary
}
// --- WO-062C end ---

export interface PresenterScript {
  end: string | null
  end_s: number | null
  steps: PresenterStep[]
  actions: Record<string, string | null>
}

export type PresenterLogKind = 'step' | 'resume' | 'reset'

export const PRESENTER_PARAM = 'presenter'

export function isPresenter(search: string = window.location.search): boolean {
  return new URLSearchParams(search).get(PRESENTER_PARAM) === '1'
}

export async function fetchDemoScript(): Promise<PresenterScript> {
  const res = await fetch('/api/demo-script')
  if (!res.ok) throw new Error(`/api/demo-script: HTTP ${res.status}`)
  return (await res.json()) as PresenterScript
}

/** Record a presenter event; the server stamps it and copies the caption from the script. */
export function logPresenter(index: number, kind: PresenterLogKind): void {
  fetch('/api/presenter-log', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index, kind }),
  }).catch(() => {
    // the log is a convenience for the recording; presenting never waits on it
  })
}

export async function startStepRun(
  scenario: string,
  seed: number | null,
  speed: number | null,
  toggles: string[] = [],
): Promise<RunSummary> {
  const res = await fetch('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // WO-062B found live presenter runs on the batch source (no hold, twist toggles 400); stage runs
    // use the same runner v3 source as the main screen.
    body: JSON.stringify({ scenario, seed, speed, toggles, source: RUN_SOURCE }),
  })
  if (!res.ok) throw new Error(`start run: HTTP ${res.status} ${await res.text()}`)
  return (await res.json()) as RunSummary
}

/** Spotlight names used by docs/demo-script.yaml; anything else is an element id or test id. */
export const SPOTLIGHT_SELECTORS: Record<string, string> = {
  'harbor-map': '.map-wrap',
  ladder: '.rungs',
  'evidence-ledger': '[data-testid="case-view"]', // design pass: the case view holds the evidence
  'state-timeline': '[data-testid="state-timeline"]',
  'contact-card': '[data-testid="contact-card"]',
  sitrep: '[data-testid="sitrep-open"]',
}

export function findSpotlight(name: string): HTMLElement | null {
  const sel = SPOTLIGHT_SELECTORS[name]
  if (sel) return document.querySelector<HTMLElement>(sel)
  return document.getElementById(name) ?? document.querySelector<HTMLElement>(`[data-testid="${CSS.escape(name)}"]`)
}

/** The five rungs the ladder_chips action lights in order. */
export const LADDER_CHIPS = ['watch', 'ask', 'slow', 'hold', 'hand to a person']

export const END_CARD = {
  title: 'Soundings',
  line: 'VTS for the harbor floor',
  repo: 'github.com/kudratsingh/soundings',
}

// --- WO-060D begin: tell the screen which case to follow for the run a step starts ---
export const FOCUS_EVENT = 'soundings:presenter-focus'

export function announceFocus(step: Pick<PresenterStep, 'focus'>): void {
  window.dispatchEvent(new CustomEvent(FOCUS_EVENT, { detail: { focus: step.focus ?? 'first_escalation' } }))
}
// --- WO-060D end ---
