// Hand-written mirror of soundings/stream/events.py (SCHEMA_VERSION 1).
// web/src/generated/stream-events.schema.json is the machine copy; a pytest keeps it equal
// to the Pydantic models. Part B adds emitters for posterior and approval events.

export const SCHEMA_VERSION = 1

export type Rung = 'watch' | 'ask' | 'slow' | 'hold' | 'hand_to_person'
export const RUNGS: Rung[] = ['watch', 'ask', 'slow', 'hold', 'hand_to_person']

export type LadderState =
  | 'DETECTED'
  | 'WATCHING'
  | 'ASKING'
  | 'INVESTIGATING'
  | 'ADVISING_SLOW'
  | 'HOLDING'
  | 'AWAITING_APPROVAL'
  | 'HANDED_OFF'
  | 'WATCH_AFTER_HANDOFF'
  | 'RESOLVED_BENIGN'
  | 'CLEARED_AT_ASK'
  | 'HAZARD_NOTICE'
  | 'LOST_VEHICLE'
  | 'RESOLVED_DEPARTED'
  | 'FAILED'

export type EvidenceKind = 'fact' | 'assumption' | 'derived'
export type EvidenceValue = boolean | number | string

export interface EvidenceRow {
  contact_id: string
  t: number
  feature: string
  value: EvidenceValue
  unit: string
  source: string
  confidence: number | 'unknown'
  kind: EvidenceKind
  probe_id: string | null
  derived_from: string[]
  assumes: string
  row_id: string
}

export interface RunStartedPayload {
  scenario_id: string
  scenario_name: string
  seed: number
  tick_s: number
  n_ticks: number
  speed: number
  toggles: string[]
  source: 'batch' | 'stepped'
  runner: string
}

export interface TickContact {
  contact_id: string
  lat: number
  lon: number
  depth_m: number
  confidence: number
  sensor_id: string
  // --- WO-086 begin ---
  /** WO-084's 60 s projection from the observed track (null below two readings). */
  projection?: ProjectionView | null
  // --- WO-086 end ---
}

export interface TickPayload {
  contacts: TickContact[]
}

export interface EvidenceRowPayload {
  row_id: string
  row: EvidenceRow
}

export interface PosteriorPayload {
  contact_id: string
  probs: Record<string, number>
  entropy: number
  top2_margin: number
  threat_mass: number
  benign_mass: number
  label: 'stress-test estimate'
}

export interface GuardInfo {
  name: string
  result: string
  inputs: Record<string, unknown>
}

export interface TransitionPayload {
  contact_id: string
  case_id: string
  from_state: LadderState
  event: string
  to_state: LadderState
  reason_code: string
  evidence_row_ids: string[]
  guard: GuardInfo | null
  audit_row_hash: string | null
}

export interface RungPayload {
  contact_id: string
  rung: Rung
  state: LadderState
}

export interface ApprovalRequestPayload {
  contact_id: string
  request_id: string
  action: string
  action_label: string
  scope: Record<string, unknown>
  recommended_rung: Rung
  expires_t_s: number
  approver: string
}

export interface ApprovalResultPayload {
  request_id: string
  outcome: 'granted' | 'timeout' | 'backup_paged'
  token_hash: string | null
}

export interface PositionFix {
  lat: number
  lon: number
  tick: number
  sigma_m: number
  row_id: string | null
}

export interface SearchBox {
  center_lat: number
  center_lon: number
  radius_m: number
  min_lat: number
  max_lat: number
  min_lon: number
  max_lon: number
}

export interface NoticeLine {
  section: 'Contact' | 'What we know' | 'What we assume' | 'Search box'
  text: string
  evidence_row_ids: string[]
}

export interface LostVehicleNotice {
  case_id: string
  contact_id: string
  tick: number
  plan_id: string
  operator: string
  vehicle: string
  contact_channel: string
  last_known: PositionFix
  silence_s: number
  search_box: SearchBox
  evidence_row_ids: string[]
  lines: NoticeLine[]
}

export interface SitrepPayload {
  contact_id: string
  version: number
  writer: 'template' | 'llm'
  kind: 'lost_vehicle' | 'sitrep'
  notice: LostVehicleNotice | null
  sitrep: Record<string, unknown> | null
}

export interface RunFinishedPayload {
  terminal: Record<string, LadderState>
  track_hash: string
  transition_log_hash: string
  n_events: number
}

// --- WO-089 begin: live sensor range rings ---
export interface SensorRangeRow {
  node_id: string
  effective_range_m: number
  nl_db: number
}

export interface SensorRangesPayload {
  nodes: SensorRangeRow[]
}
// --- WO-089 end ---
// --- WO-088 begin ---
export type ResponderStateName = 'DOCKED' | 'TRANSIT' | 'ON_STATION' | 'RETURNING'

export interface ResponderTrailPoint {
  tick: number
  lat: number
  lon: number
}

/** The harbor's responder: a probe dispatched to look, held at standoff (display only). */
export interface ResponderPayload {
  id: string
  state: ResponderStateName
  lat: number
  lon: number
  heading_deg: number
  speed_mps: number
  battery_min: number
  battery_capacity_min: number
  standoff_m: number
  target_contact_id: string | null
  distance_to_target_m: number | null
  dispatched: boolean
  trail: ResponderTrailPoint[]
}
// --- WO-088 end ---
// --- WO-086 begin: projected track, rung ledger and blunt-policy counter ---
export interface ProjectionEllipse {
  semi_major_m: number
  semi_minor_m: number
  /** Compass bearing of the major axis, [0, 180). */
  orientation_deg: number
  p: number
}

/** A stress-test estimate: where the contact is in 60 s if it holds its velocity. */
export interface ProjectionView {
  contact_id: string
  tick: number
  lat: number
  lon: number
  /** East velocity, m/s. */
  vx: number
  /** North velocity, m/s. */
  vy: number
  speed_mps: number
  heading_deg: number
  proj_lat: number
  proj_lon: number
  ellipse: ProjectionEllipse
}

export type LedgerBucket = 'resolved_at_watch' | 'cleared_at_ask' | 'advisories' | 'holds' | 'handed_off'
export const LEDGER_BUCKETS: LedgerBucket[] = ['resolved_at_watch', 'cleared_at_ask', 'advisories', 'holds', 'handed_off']

/** Counterfactual: the bang baseline on the same seeded contacts (approvals assumed). */
export interface BluntPayload {
  berth_closures: number
  episodes: number
  closure_minutes: number
  available: boolean
  label: 'counterfactual: bang baseline'
}

export interface LedgerPayload {
  counts: Record<LedgerBucket, number>
  contacts_seen: number
  blunt: BluntPayload
}
// --- WO-086 end ---

interface Envelope<T extends string, P> {
  v: 1
  run_id: string
  seq: number
  tick: number
  t_s: number
  type: T
  payload: P
}

export type StreamEvent =
  | Envelope<'run_started', RunStartedPayload>
  | Envelope<'tick', TickPayload>
  | Envelope<'evidence_row', EvidenceRowPayload>
  | Envelope<'posterior', PosteriorPayload>
  | Envelope<'transition', TransitionPayload>
  | Envelope<'rung', RungPayload>
  | Envelope<'approval_request', ApprovalRequestPayload>
  | Envelope<'approval_result', ApprovalResultPayload>
  | Envelope<'sitrep', SitrepPayload>
  | Envelope<'run_finished', RunFinishedPayload>
  | Envelope<'error', { message: string }>
  | Envelope<'sensor_ranges', SensorRangesPayload> // WO-089
  // --- WO-088 begin ---
  | Envelope<'responder', ResponderPayload>
  // --- WO-088 end ---
  // --- WO-086 begin ---
  | Envelope<'ledger', LedgerPayload>
// --- WO-086 end ---

export const RUNG_LABEL: Record<Rung, string> = {
  watch: 'watch',
  ask: 'ask',
  slow: 'slow',
  hold: 'hold',
  hand_to_person: 'hand to a person',
}
