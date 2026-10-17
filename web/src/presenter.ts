// Presenter mode data (WO-062P): the shot list from GET /api/demo-script, the presenter
// log, run starts for a step, and where a step's spotlight points. No network beyond the
// local API; nothing here touches a live model.

import type { RunSummary } from './api'

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
}

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
    body: JSON.stringify({ scenario, seed, speed, toggles }),
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
