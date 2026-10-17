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
// A person's answer to an approval request names the request only; the server mints the
// token for exactly the requested scope and the ladder's guard decides. Judge injection
// describes a contact (position, behavior, acoustic profile); nothing here names a rung.
async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const j = (await res.json()) as { detail?: unknown }
      if (typeof j.detail === 'string') detail = j.detail
    } catch {
      /* keep the status line */
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export interface ApproveReply {
  run_id: string
  request_id: string
  contact_id?: string
  token_hash?: string
  outcome?: string
  status: string
}

export const postApprove = (runId: string, requestId: string, approver = 'Watch Officer') =>
  postJson<ApproveReply>(`/api/runs/${encodeURIComponent(runId)}/approve`, { request_id: requestId, approver })
export const postDecline = (runId: string, requestId: string, approver = 'Watch Officer') =>
  postJson<ApproveReply>(`/api/runs/${encodeURIComponent(runId)}/decline`, { request_id: requestId, approver })

export type JudgeBehavior = 'filed' | 'unfiled' | 'closing' | 'loitering' | 'silent'
export type AcousticProfile = 'propeller' | 'biologic' | 'quiet'

export interface InjectReq {
  lat: number
  lon: number
  behavior: JudgeBehavior
  acoustic: AcousticProfile | null
}

export interface InjectReply {
  run_id: string
  scenario_id: string
  path: string
  seed: number
}

/** Start a catalog run on a named source (`?source=stepped`: a person approves). */
export const startRunOn = (source: string, scenario: string, seed: number | null, speed: number | null, toggles: string[] = []) =>
  postJson<RunSummary>('/api/runs', { scenario, seed, speed, toggles, source })

export const postInject = (req: InjectReq) => postJson<InjectReply>('/api/inject', req)
export const postPrepare = (req: InjectReq) => postJson<{ prepared: boolean; scenario_id: string }>('/api/inject/prepare', req)

export interface QrInfo {
  url: string
  loopback: boolean
  lan?: boolean
}

export const fetchQrInfo = () => getJson<QrInfo>('/api/qr/info?target=/registry')

export interface PlanConflict {
  other_plan_id: string
  overlap_start: string
  overlap_end: string
  overlap_minutes: number
  reasons: string[]
  resolved_by: string | null
}

export interface PlanView {
  plan_id: string
  operator: string
  vehicle: string
  area: string
  start_at: string
  end_at: string
  status: 'active' | 'conflicted' | 'expired'
  conflicts: PlanConflict[]
  created_at?: string | null
}

export interface PlanShift {
  plan_id: string
  shift_min: number
  new_start_at: string
  new_end_at: string
  start_at: string
  end_at: string
}

export interface SlotProposal {
  proposal_id: string
  target_plan_id: string
  zone_id: string
  status: 'feasible' | 'infeasible'
  shifts: PlanShift[]
  total_delay_min: number
  binding_constraint: string | null
  detail: string
}

export const fetchActivePlans = () => getJson<PlanView[]>('/api/plans?status=active')
export const proposeSlot = (planId: string) => postJson<SlotProposal>(`/api/plans/${encodeURIComponent(planId)}/proposal`, {})
export const acceptProposal = (planId: string, proposalId: string) =>
  postJson<unknown>(`/api/plans/${encodeURIComponent(planId)}/proposal/accept`, { proposal_id: proposalId })
export const declineProposal = (planId: string, proposalId: string) =>
  postJson<unknown>(`/api/plans/${encodeURIComponent(planId)}/proposal/decline`, { proposal_id: proposalId })
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
