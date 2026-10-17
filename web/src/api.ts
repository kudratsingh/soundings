// REST helpers for the watch-officer screen (WO-060). WO-061 and WO-062 each add one
// marked block below; nothing else here changes.

export interface ScenarioInfo {
  id: string
  name: string
  seed: number
  duration_ticks: number
  tick_s: number
}

export interface RunSummary {
  run_id: string
  scenario: string
  seed: number
  speed: number
  source: string
  n_events: number
  finished: boolean
}

export interface BoundaryRow {
  rung: string
  action: string
  yaml_cell: string
  cell: string
}

export interface StandingOrdersInfo {
  signed: boolean
  reason: string
  signer: string | null
  signed_at: string | null
  valid_until: string | null
  checked_at: string
  signature_label: string
}

export interface BoundaryInfo {
  rungs: string[]
  lives_on: string[]
  rows: BoundaryRow[]
  never: string[]
  standing_orders: StandingOrdersInfo
}

export interface AboutInfo {
  title: string
  honesty: {
    detection_heading: string
    detection_params: Record<string, number>
    detection_assumptions: string[]
    probability_label: string
    shared_tables_note: string
    map_note: string
    handoff_line: string
    retention: string
    model_mode: string
  }
  standing_orders: StandingOrdersInfo
  tiles: {
    base: { url: string; attribution: string }
    seamarks: { url: string; attribution: string }
    map_attribution: string
  }
  tile_policy: {
    verified_on: string
    osm_policy_url: string
    osm_summary: string
    openseamap_summary: string
    offline_note: string
  }
  colors: { fact: string; assumption: string; derived: string }
  default_speed: number
}

export interface HarborFeature {
  type: 'Feature'
  properties: Record<string, string | number>
  geometry: { type: string; coordinates: unknown }
}

export interface HarborGeoJson {
  type: 'FeatureCollection'
  name: string
  features: HarborFeature[]
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`)
  return (await res.json()) as T
}

export const fetchScenarios = () => getJson<ScenarioInfo[]>('/api/scenarios')
export const fetchBoundary = () => getJson<BoundaryInfo>('/api/boundary')
export const fetchAbout = () => getJson<AboutInfo>('/api/about')
export const fetchHarbor = () => getJson<HarborGeoJson>('/api/harbor')

// --- WO-060B begin: runs start on the runner v3 source ("v3") with optional toggles ---
export const RUN_SOURCE = 'v3'

export async function startRun(scenario: string, seed: number | null, speed: number | null, toggles: string[] = []) {
  const res = await fetch('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scenario, seed, speed, toggles, source: RUN_SOURCE }),
  })
  // --- WO-060B end ---
  if (!res.ok) throw new Error(`start run: HTTP ${res.status} ${await res.text()}`)
  return (await res.json()) as RunSummary
}

export function runSocketUrl(runId: string, after: number): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/ws/runs/${encodeURIComponent(runId)}?after=${after}`
}

// --- WO-061 block (approve endpoint) ---
// --- end WO-061 block ---

// --- WO-062 block (demo runner) ---
// --- end WO-062 block ---

// --- WO-089 begin: physics cards and sensor ranges ---
export interface PhysicsParam {
  key: string
  name: string
  value: number | string
  unit: string
  source: string
  label: string
}

export interface PhysicsCard {
  name: string
  equation: string
  parameters: PhysicsParam[]
  not_modeled: string[]
}

export interface PhysicsNode {
  node_id: string
  name: string
  effective_range_m: number
  nl_db: number
}

export interface PhysicsInfo {
  heading: string
  label: string
  cards: PhysicsCard[]
  nodes: PhysicsNode[]
  ring_class: string
  sea_state: number | null
  config: string
  pending: string | null
}

export const fetchPhysics = () => getJson<PhysicsInfo>('/api/physics')
// --- WO-089 end ---

// --- WO-087 block (model mode for the judge-panel badge) ---
// The badge reads `last_path`: "live" -> "model: live", "fallback" -> "model: fallback",
// "replay" or null -> "model: replay". It never claims live unless a live reply was used.
export type ModelCallPath = 'live' | 'replay' | 'fallback'

export interface ModelModeInfo {
  mode: string
  live_budget_total: number
  live_budget_remaining: number
  last_path: ModelCallPath | null
}

export const fetchModelMode = () => getJson<ModelModeInfo>('/api/model_mode')

export function modelBadge(info: ModelModeInfo): string {
  return `model: ${info.last_path ?? 'replay'}`
}
// --- end WO-087 block ---
