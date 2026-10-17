// The case model (WO-060 design pass B1). A CASE is one contact lineage (re-acquired ids
// merged) handled by one investigation/ladder instance. Everything the screen shows is
// grouped by case: this module folds the stream into per-contact tracks (incrementally,
// copy-on-write, inside the useRun reducer) and derives, purely, the case list, each case's
// steps, the NOW line and the KEY MOMENTS. Display only: nothing here can set a rung.

import type {
  ApprovalRequestPayload,
  ApprovalResultPayload,
  EvidenceRow,
  LadderState,
  PosteriorPayload,
  Rung,
  SitrepPayload,
  StreamEvent,
} from './events'
import { RUNG_LABEL } from './events'

// ---------------------------------------------------------------- incremental fold

export interface CaseTransition {
  seq: number
  tick: number
  from_state: LadderState
  to_state: LadderState
  event: string
  reason_code: string
  evidence_row_ids: string[]
  guard: string | null
}

export interface CaseSitrep {
  seq: number
  tick: number
  version: number
  kind: SitrepPayload['kind']
  writer: SitrepPayload['writer']
}

export interface CaseApproval {
  request: ApprovalRequestPayload
  tick: number
  result: (ApprovalResultPayload & { tick: number }) | null
}

/** One observed contact id: its own rows (copies inherited on re-acquisition are skipped). */
export interface ContactTrack {
  id: string
  firstTick: number
  /** The contact this one continues (from its `reacquired_from` row), if any. */
  continues: string | null
  continuesNote: string
  rows: EvidenceRow[]
  transitions: CaseTransition[]
  sitreps: CaseSitrep[]
  approvals: CaseApproval[]
  /** Tick of the last step-level event (transition, probe row, sitrep, approval). */
  lastEventTick: number
  /** Tick the contact entered its current state. */
  stateTick: number
}

export type MomentKind = 'threat' | 'benign' | 'info'

export interface Moment {
  seq: number
  tick: number
  contact_id: string
  label: string
  kind: MomentKind
}

export interface CaseIndex {
  tracks: Record<string, ContactTrack>
  /** First-seen order of contact ids (stable). */
  order: string[]
  moments: Moment[]
  /** request_id -> contact id, so an approval_result finds its case. */
  approvalOwner: Record<string, string>
  /** Every evidence row the stream has reported (copies included). */
  totalRows: number
}

export const emptyCaseIndex = (): CaseIndex => ({ tracks: {}, order: [], moments: [], approvalOwner: {}, totalRows: 0 })

const MOMENT_OF_STATE: Partial<Record<LadderState, [string, MomentKind]>> = {
  WATCHING: ['detected', 'info'],
  ASKING: ['hail sent', 'info'],
  INVESTIGATING: ['escalated to investigation', 'info'],
  ADVISING_SLOW: ['advisory', 'threat'],
  HOLDING: ['hold placed', 'threat'],
  AWAITING_APPROVAL: ['approval requested', 'threat'],
  HANDED_OFF: ['handed off', 'threat'],
  LOST_VEHICLE: ['lost vehicle', 'benign'],
  RESOLVED_DEPARTED: ['departed', 'benign'],
  CLEARED_AT_ASK: ['cleared', 'benign'],
  RESOLVED_BENIGN: ['cleared', 'benign'],
  HAZARD_NOTICE: ['hazard notice', 'threat'],
}

/** Moments kept in memory (the strip shows the last five). */
export const MOMENTS_KEPT = 200

const PROBE_FEATURES = new Set(['hail_reply'])

/** Fold one batch of events into the index. Pure: `prev` and its arrays are never mutated. */
export function foldCases(prev: CaseIndex, events: StreamEvent[]): CaseIndex {
  const next: CaseIndex = { ...prev, tracks: { ...prev.tracks }, order: prev.order, moments: prev.moments, approvalOwner: prev.approvalOwner }
  const copied = new Set<string>()
  let orderCopied = false
  let momentsCopied = false
  const track = (cid: string, tick: number): ContactTrack => {
    let t = next.tracks[cid]
    if (!t) {
      t = {
        id: cid,
        firstTick: tick,
        continues: null,
        continuesNote: '',
        rows: [],
        transitions: [],
        sitreps: [],
        approvals: [],
        lastEventTick: tick,
        stateTick: tick,
      }
      next.tracks[cid] = t
      copied.add(cid)
      if (!orderCopied) {
        next.order = next.order.slice()
        orderCopied = true
      }
      next.order.push(cid)
      return t
    }
    if (!copied.has(cid)) {
      t = { ...t, rows: t.rows.slice(), transitions: t.transitions.slice(), sitreps: t.sitreps.slice(), approvals: t.approvals.slice() }
      next.tracks[cid] = t
      copied.add(cid)
    }
    return t
  }
  const moment = (m: Moment) => {
    if (!momentsCopied) {
      next.moments = next.moments.slice(-(MOMENTS_KEPT - 1))
      momentsCopied = true
    }
    next.moments.push(m)
  }
  for (const e of events) {
    switch (e.type) {
      case 'tick':
        for (const c of e.payload.contacts) if (!next.tracks[c.contact_id]) track(c.contact_id, e.tick)
        break
      case 'evidence_row': {
        const row: EvidenceRow = { ...e.payload.row, row_id: e.payload.row_id }
        next.totalRows += 1
        // A re-acquired track's table starts with copies of its predecessor's rows, streamed
        // at their own tick under the new id before that id is ever observed. The case
        // already holds the originals, so rows of a not-yet-seen id (or older than its first
        // fix) are skipped here.
        const seen = next.tracks[row.contact_id]
        if (!seen || row.t < seen.firstTick) break
        const t = track(row.contact_id, e.tick)
        if (row.feature === 'reacquired_from' && typeof row.value === 'string') {
          t.continues = row.value // the predecessor this id continues
          t.continuesNote = row.assumes
        }
        t.rows.push(row)
        if (row.probe_id) t.lastEventTick = e.tick
        if (PROBE_FEATURES.has(row.feature) && (row.value === 'valid' || row.value === 'timeout')) {
          const already = t.rows.some((r) => r !== row && r.feature === 'hail_reply' && r.value === row.value)
          if (!already) {
            moment({
              seq: e.seq,
              tick: e.tick,
              contact_id: row.contact_id,
              label: row.value === 'valid' ? 'hail answered' : 'hail unanswered',
              kind: row.value === 'valid' ? 'benign' : 'info',
            })
          }
        }
        break
      }
      case 'transition': {
        const p = e.payload
        const t = track(p.contact_id, e.tick)
        t.transitions.push({
          seq: e.seq,
          tick: e.tick,
          from_state: p.from_state,
          to_state: p.to_state,
          event: p.event,
          reason_code: p.reason_code,
          evidence_row_ids: p.evidence_row_ids,
          guard: p.guard ? `${p.guard.name}: ${p.guard.result}` : null,
        })
        t.lastEventTick = e.tick
        t.stateTick = e.tick
        const m = MOMENT_OF_STATE[p.to_state]
        if (m) moment({ seq: e.seq, tick: e.tick, contact_id: p.contact_id, label: m[0], kind: m[1] })
        break
      }
      case 'sitrep': {
        const p = e.payload
        const t = track(p.contact_id, e.tick)
        t.sitreps.push({ seq: e.seq, tick: e.tick, version: p.version, kind: p.kind, writer: p.writer })
        t.lastEventTick = e.tick
        break
      }
      case 'approval_request': {
        const p = e.payload
        const t = track(p.contact_id, e.tick)
        t.approvals.push({ request: p, tick: e.tick, result: null })
        t.lastEventTick = e.tick
        next.approvalOwner = { ...next.approvalOwner, [p.request_id]: p.contact_id }
        break
      }
      case 'approval_result': {
        const cid = next.approvalOwner[e.payload.request_id]
        if (!cid) break
        const t = track(cid, e.tick)
        t.approvals = t.approvals.map((a) => (a.request.request_id === e.payload.request_id ? { ...a, result: { ...e.payload, tick: e.tick } } : a))
        t.lastEventTick = e.tick
        if (e.payload.outcome === 'granted') moment({ seq: e.seq, tick: e.tick, contact_id: cid, label: 'approved', kind: 'threat' })
        break
      }
      default:
        break
    }
  }
  return next
}

// ---------------------------------------------------------------- lineage and severity

/** The case id of a contact: the first-seen id of its lineage. */
export function caseOf(index: CaseIndex, contactId: string): string {
  let cur = contactId
  const seen = new Set<string>()
  for (;;) {
    const t = index.tracks[cur]
    if (!t || !t.continues || seen.has(cur) || !index.tracks[t.continues]) return cur
    seen.add(cur)
    cur = t.continues
  }
}

export type Tone = 'threat' | 'approval' | 'unknown' | 'benign'

const THREAT_STATES = new Set<LadderState>(['ADVISING_SLOW', 'HOLDING', 'HANDED_OFF', 'WATCH_AFTER_HANDOFF', 'HAZARD_NOTICE'])
const BENIGN_STATES = new Set<LadderState>(['CLEARED_AT_ASK', 'RESOLVED_BENIGN', 'LOST_VEHICLE', 'RESOLVED_DEPARTED'])
const ACTIVE_RANK: Partial<Record<LadderState, number>> = { INVESTIGATING: 0, ASKING: 1, WATCHING: 2, DETECTED: 3, FAILED: 4 }

export function toneOf(state: LadderState): Tone {
  if (state === 'AWAITING_APPROVAL') return 'approval'
  if (THREAT_STATES.has(state)) return 'threat'
  if (BENIGN_STATES.has(state)) return 'benign'
  return 'unknown'
}

/** Severity sort key: threat first, then needs-approval, then investigating, then resolved. */
export function severityKey(state: LadderState): [number, number] {
  const tone = toneOf(state)
  if (tone === 'threat') return [0, 0]
  if (tone === 'approval') return [1, 0]
  if (tone === 'unknown') return [2, ACTIVE_RANK[state] ?? 5]
  return [3, 0]
}

// ---------------------------------------------------------------- steps

export type StepKind = 'detected' | 'watch' | 'ask' | 'investigate' | 'recommendation' | 'ladder' | 'handoff' | 'after'
export const STEP_KINDS: StepKind[] = ['detected', 'watch', 'ask', 'investigate', 'recommendation', 'ladder', 'handoff', 'after']
export const STEP_TITLE: Record<StepKind, string> = {
  detected: 'Detected',
  watch: 'Watch',
  ask: 'Ask',
  investigate: 'Investigate',
  recommendation: 'Recommendation',
  ladder: 'Ladder',
  handoff: 'Handoff',
  after: 'After handoff',
}
const STEP_HINT: Record<StepKind, string> = {
  detected: 'a sensor hears the contact',
  watch: 'free observations: track, depth, speed, zone',
  ask: 'registry lookup and acoustic hail',
  investigate: 'planner picks probes; the posterior updates',
  recommendation: 'a stop clause fires and names the rung',
  ladder: 'advisory or hold under standing orders, or an approval request',
  handoff: 'SITREP to the watch officer',
  after: 'watch after handoff: did it leave, did a person act',
}

export type StepStatus = 'done' | 'current' | 'pending' | 'skipped'

export interface KeySlot {
  label: string
  row: EvidenceRow | null
}

export interface ProbeCard {
  probe_id: string
  tick: number
  why: string
  rows: EvidenceRow[]
}

export interface CaseStep {
  key: string
  kind: StepKind
  contact_id: string
  title: string
  status: StepStatus
  tick: number | null
  sentence: string
  /** Fixed slots: the rows the sentence rests on (empty slots render a dash). */
  slots: KeySlot[]
  /** The step's rows, grouped latest-per-feature (with how many rows each group has). */
  groups: { row: EvidenceRow; n: number }[]
  rowCount: number
  hail?: string
  probes?: ProbeCard[]
  posterior?: PosteriorPayload
  sitrep?: CaseSitrep
  approval?: CaseApproval
}

export interface ReacquiredMark {
  key: string
  kind: 'reacquired'
  contact_id: string
  from: string
  tick: number
  note: string
}

export type CaseItem = CaseStep | ReacquiredMark

const ASK_PROBES = new Set(['registry_lookup', 'acoustic_hail'])
const WATCH_SLOTS = ['speed_over_ground', 'depth', 'time_to_nearest_critical_asset']
const ASK_SLOTS = ['registry_match', 'hail_reply', 'modem_id']
const DETECT_SLOTS = ['sensor_health', 'zone_id']

export const NOT_CLOSING_S = 1e8

export function fmtValue(row: EvidenceRow): string {
  const v = row.value
  if (typeof v === 'number') {
    if (row.unit === 's' && v >= NOT_CLOSING_S) return 'not closing'
    const n = Number.isInteger(v) ? String(v) : Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2)
    return row.unit && row.unit !== 'category' && row.unit !== 'id' && row.unit !== 'bool' ? `${n} ${row.unit}` : n
  }
  return String(v)
}

const pretty = (f: string) => f.replaceAll('_', ' ')

function latestBy(rows: EvidenceRow[], feature: string): EvidenceRow | null {
  for (let i = rows.length - 1; i >= 0; i--) if (rows[i].feature === feature) return rows[i]
  return null
}

function group(rows: EvidenceRow[]): { row: EvidenceRow; n: number }[] {
  const by = new Map<string, { row: EvidenceRow; n: number }>()
  for (const r of rows) {
    const g = by.get(r.feature)
    if (g) {
      g.row = r
      g.n += 1
    } else by.set(r.feature, { row: r, n: 1 })
  }
  return [...by.values()]
}

function ttcPhrase(rows: EvidenceRow[]): string {
  const ttc = latestBy(rows, 'time_to_nearest_critical_asset')
  const asset = latestBy(rows, 'nearest_asset_id')
  if (!ttc || typeof ttc.value !== 'number') return ''
  if (ttc.value >= NOT_CLOSING_S) return `not closing on ${asset ? String(asset.value) : 'any asset'}`
  const min = ttc.value / 60
  return `${min < 10 ? min.toFixed(1) : min.toFixed(0)} min to ${asset ? String(asset.value) : 'the nearest asset'}`
}

export function hailOutcome(rows: EvidenceRow[], pastAsk: boolean): string {
  const reply = latestBy(rows, 'hail_reply')
  const sent = latestBy(rows, 'hail_sent_at')
  if (!sent && !reply) return pastAsk ? 'no hail sent' : 'hail pending'
  if (reply?.value === 'valid') {
    const modem = latestBy(rows, 'modem_id')
    return `hail answered${modem ? ` by ${String(modem.value)}` : ''}`
  }
  if (reply?.value === 'timeout') return 'hail unanswered (timed out)'
  return pastAsk ? 'hail unanswered (no reply)' : 'hail sent, waiting for a reply'
}

function registryPhrase(rows: EvidenceRow[]): string {
  const m = latestBy(rows, 'registry_match')
  const plan = latestBy(rows, 'plan_id')
  if (!m) return 'registry lookup pending'
  const v = String(m.value)
  if (v === 'none' || v === 'unknown') return 'no registry match'
  return `registry ${v} match${plan ? ` to ${String(plan.value)}` : ''}`
}

const STATE_PHRASE: Record<LadderState, string> = {
  DETECTED: 'just detected',
  WATCHING: 'watching',
  ASKING: 'asking',
  INVESTIGATING: 'investigating',
  ADVISING_SLOW: 'advising slow',
  HOLDING: 'holding',
  AWAITING_APPROVAL: 'awaiting approval',
  HANDED_OFF: 'handed to a person',
  WATCH_AFTER_HANDOFF: 'watching after handoff',
  RESOLVED_BENIGN: 'resolved benign',
  CLEARED_AT_ASK: 'cleared at ask',
  HAZARD_NOTICE: 'hazard notice sent',
  LOST_VEHICLE: 'lost vehicle notice sent',
  RESOLVED_DEPARTED: 'departed',
  FAILED: 'handed to a person (investigation failed)',
}

export const stateWords = (s: LadderState) => STATE_PHRASE[s] ?? s

const LADDER_STATES = new Set<LadderState>(['ADVISING_SLOW', 'HOLDING', 'AWAITING_APPROVAL'])
const AFTER_STATES = new Set<LadderState>(['WATCH_AFTER_HANDOFF', 'RESOLVED_DEPARTED'])
const ASK_TERMINAL = new Set<LadderState>(['CLEARED_AT_ASK', 'LOST_VEHICLE'])
const INVESTIGATION_TERMINAL = new Set<LadderState>(['RESOLVED_BENIGN', 'HAZARD_NOTICE', 'FAILED'])

/** The steps of one contact id within its case. `latest` adds placeholders for what comes next. */
export function stepsForTrack(
  t: ContactTrack,
  opts: { latest: boolean; posterior?: PosteriorPayload; rowIndex?: Map<string, EvidenceRow> },
): CaseStep[] {
  const tr = t.transitions
  const tickOf = (s: LadderState) => tr.find((x) => x.to_state === s)?.tick ?? null
  const reached = new Set(tr.map((x) => x.to_state))
  const state: LadderState = tr.length ? tr[tr.length - 1].to_state : 'DETECTED'
  const tAsk = tickOf('ASKING')
  const tInv = tickOf('INVESTIGATING')
  const recT = tr.find((x) => x.from_state === 'INVESTIGATING')
  const ladderT = tr.filter((x) => LADDER_STATES.has(x.to_state))
  const handT = tr.find((x) => x.to_state === 'HANDED_OFF' || x.to_state === 'LOST_VEHICLE' || x.to_state === 'HAZARD_NOTICE')
  const afterT = tr.filter((x) => AFTER_STATES.has(x.to_state))
  const terminalAtAsk = tr.find((x) => ASK_TERMINAL.has(x.to_state))
  const rows = t.rows
  const pastAsk = tInv !== null || terminalAtAsk !== undefined
  const steps: CaseStep[] = []
  const base = (kind: StepKind): CaseStep => ({
    key: `${t.id}:${kind}`,
    kind,
    contact_id: t.id,
    title: STEP_TITLE[kind],
    status: 'pending',
    tick: null,
    sentence: STEP_HINT[kind],
    slots: [],
    groups: [],
    rowCount: 0,
  })
  const slotsOf = (labels: string[], pool: EvidenceRow[]) => labels.map((f) => ({ label: pretty(f), row: latestBy(pool, f) }))

  // DETECTED
  {
    const s = base('detected')
    const tick = tickOf('WATCHING') ?? t.firstTick
    const cited = tr[0]?.evidence_row_ids ?? []
    const pool = rows.filter((r) => r.t <= tick)
    const citedRows = cited.map((id) => opts.rowIndex?.get(id) ?? rows.find((r) => r.row_id === id)).filter((r): r is EvidenceRow => !!r)
    const sensor = latestBy(pool, 'sensor_health')
    const zone = latestBy(pool, 'zone_id')
    s.status = 'done'
    s.tick = tick
    const sensorName = sensor ? sensor.source.replace(/^sensor:/, '') : 'a hydrophone'
    s.sentence = `Heard on ${sensorName}${zone ? ` in ${String(zone.value)}` : ''}.`
    s.slots = slotsOf(DETECT_SLOTS, pool)
    s.groups = group(citedRows.length ? citedRows : pool.filter((r) => r.t === tick))
    s.rowCount = s.groups.reduce((a, g) => a + g.n, 0)
    steps.push(s)
  }
  // WATCH
  {
    const s = base('watch')
    const obs = rows.filter((r) => !r.probe_id && r.feature !== 'reacquired_from')
    s.groups = group(obs)
    s.rowCount = obs.length
    s.slots = slotsOf(WATCH_SLOTS, obs)
    if (reached.has('WATCHING')) {
      s.tick = tickOf('WATCHING')
      s.status = state === 'WATCHING' ? 'current' : 'done'
      const speed = latestBy(obs, 'speed_over_ground')
      const depth = latestBy(obs, 'depth')
      const bits = [
        speed && typeof speed.value === 'number' ? `${speed.value.toFixed(1)} m/s` : null,
        depth && typeof depth.value === 'number' ? `${depth.value.toFixed(0)} m deep` : null,
        ttcPhrase(obs) || null,
      ].filter(Boolean)
      s.sentence = `${obs.length} free observations${bits.length ? `: ${bits.join(', ')}` : ''}.`
    } else if (!opts.latest) s.status = 'skipped'
    steps.push(s)
  }
  // ASK
  {
    const s = base('ask')
    const askRows = rows.filter((r) => r.probe_id && ASK_PROBES.has(r.probe_id))
    s.groups = group(askRows)
    s.rowCount = askRows.length
    s.slots = slotsOf(ASK_SLOTS, askRows)
    if (tAsk !== null) {
      s.tick = tAsk
      s.status = state === 'ASKING' ? 'current' : 'done'
      s.hail = hailOutcome(askRows, pastAsk)
      const end = terminalAtAsk ? ` · ${stateWords(terminalAtAsk.to_state)}` : tInv !== null ? ' · escalated' : ''
      s.sentence = `${capital(registryPhrase(askRows))}; ${s.hail}${end}.`
    }
    steps.push(s)
  }
  const afterAsk: StepKind[] = ['investigate', 'recommendation', 'ladder', 'handoff', 'after']
  if (terminalAtAsk) {
    // Cleared or noticed at ask: nothing past ask runs (the handoff step carries a notice).
    for (const k of afterAsk) {
      const s = base(k)
      if (k === 'handoff' && terminalAtAsk.to_state === 'LOST_VEHICLE') {
        s.title = 'Notice to operator'
        s.status = 'done'
        s.tick = terminalAtAsk.tick
        s.sitrep = t.sitreps[t.sitreps.length - 1]
        s.sentence = 'Lost-vehicle notice sent to the plan operator (the watch officer is not interrupted).'
      } else {
        s.status = 'skipped'
        s.sentence = `not needed: ${stateWords(terminalAtAsk.to_state)}`
      }
      steps.push(s)
    }
    return opts.latest ? steps : steps.filter((s) => s.status !== 'skipped')
  }
  // INVESTIGATE
  {
    const s = base('investigate')
    const invRows = rows.filter((r) => r.probe_id && !ASK_PROBES.has(r.probe_id))
    const byProbe = new Map<string, ProbeCard>()
    for (const r of invRows) {
      const key = `${r.probe_id}@${r.t}`
      const card = byProbe.get(key) ?? { probe_id: r.probe_id ?? '', tick: r.t, why: r.source, rows: [] }
      card.rows.push(r)
      byProbe.set(key, card)
    }
    s.probes = [...byProbe.values()]
    s.groups = group(invRows)
    s.rowCount = invRows.length
    s.posterior = opts.posterior
    if (tInv !== null) {
      s.tick = tInv
      s.status = state === 'INVESTIGATING' ? 'current' : 'done'
      const p = opts.posterior
      s.sentence = s.probes.length
        ? `${s.probes.length} planner probe${s.probes.length === 1 ? '' : 's'} run${p ? `; threat mass ${p.threat_mass.toFixed(2)} (stress-test estimate)` : ''}.`
        : 'Investigation open; planner probe choices and posterior updates stream with WO-060 part B.'
    }
    steps.push(s)
  }
  // RECOMMENDATION
  {
    const s = base('recommendation')
    if (recT) {
      s.tick = recT.tick
      s.status = 'done'
      s.sentence = `Stop clause ${recT.reason_code} fired: ${stateWords(recT.to_state)}.`
      s.groups = group(recT.evidence_row_ids.map((id) => opts.rowIndex?.get(id)).filter((r): r is EvidenceRow => !!r))
      s.rowCount = s.groups.length
    }
    steps.push(s)
  }
  if (recT && INVESTIGATION_TERMINAL.has(recT.to_state) && !handT) {
    for (const k of ['ladder', 'handoff', 'after'] as StepKind[]) {
      const s = base(k)
      s.status = 'skipped'
      s.sentence = `not needed: ${stateWords(recT.to_state)}`
      steps.push(s)
    }
    return opts.latest ? steps : steps.filter((x) => x.status !== 'skipped')
  }
  // LADDER
  {
    const s = base('ladder')
    const appr = t.approvals[t.approvals.length - 1]
    s.approval = appr
    if (ladderT.length || appr) {
      const last = ladderT[ladderT.length - 1]
      s.tick = ladderT[0]?.tick ?? appr?.tick ?? null
      s.status = LADDER_STATES.has(state) ? 'current' : 'done'
      const parts: string[] = ladderT.map((x) =>
        x.to_state === 'ADVISING_SLOW'
          ? 'advisory raised under standing orders'
          : x.to_state === 'HOLDING'
            ? 'hold placed under the standing-orders clause'
            : 'approval requested',
      )
      if (appr) parts.push(appr.result ? `approval ${appr.result.outcome.replace('_', ' ')}` : `waiting on ${appr.request.approver}`)
      s.sentence = `${capital([...new Set(parts)].join('; '))}.`
      if (last?.guard) s.sentence += ` Guard ${last.guard}.`
    } else if (handT) {
      s.status = 'skipped'
      s.sentence = 'not needed: straight to a person'
    }
    steps.push(s)
  }
  // HANDOFF
  {
    const s = base('handoff')
    if (handT) {
      s.tick = handT.tick
      s.status = state === handT.to_state ? 'current' : 'done'
      s.sitrep = t.sitreps[t.sitreps.length - 1]
      s.sentence =
        handT.to_state === 'HANDED_OFF'
          ? `Handed to a person${s.sitrep ? ` with SITREP v${s.sitrep.version} (${s.sitrep.writer})` : ''}. The work stops here.`
          : `${capital(stateWords(handT.to_state))}.`
    }
    steps.push(s)
  }
  // AFTER
  {
    const s = base('after')
    if (afterT.length) {
      s.tick = afterT[0].tick
      s.status = AFTER_STATES.has(state) && state !== 'RESOLVED_DEPARTED' ? 'current' : 'done'
      s.sentence = afterT.some((x) => x.to_state === 'RESOLVED_DEPARTED') ? 'The contact departed the zone.' : 'Watching after the handoff.'
    }
    steps.push(s)
  }
  return opts.latest ? steps : steps.filter((x) => x.status !== 'pending' && x.status !== 'skipped')
}

const capital = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s)

// ---------------------------------------------------------------- case summaries

export interface CaseSummary {
  id: string
  members: string[]
  latest: string
  state: LadderState
  rung: Rung
  tone: Tone
  sev: [number, number]
  situation: string
  stepper: StepStatus[]
  lastEventTick: number
  stateTick: number
}

export function caseGroups(index: CaseIndex): Map<string, string[]> {
  const groups = new Map<string, string[]>()
  for (const cid of index.order) {
    const root = caseOf(index, cid)
    const g = groups.get(root)
    if (g) g.push(cid)
    else groups.set(root, [cid])
  }
  return groups
}

const RUNG_OF: Partial<Record<LadderState, Rung>> = {
  DETECTED: 'watch',
  WATCHING: 'watch',
  ASKING: 'ask',
  INVESTIGATING: 'ask',
  ADVISING_SLOW: 'slow',
  HOLDING: 'hold',
  HANDED_OFF: 'hand_to_person',
  WATCH_AFTER_HANDOFF: 'hand_to_person',
  RESOLVED_BENIGN: 'ask',
  CLEARED_AT_ASK: 'ask',
  HAZARD_NOTICE: 'ask',
  LOST_VEHICLE: 'ask',
}

export function summarize(index: CaseIndex, rungOf: Record<string, { rung: Rung }>): CaseSummary[] {
  const out: CaseSummary[] = []
  for (const [root, members] of caseGroups(index)) {
    const latest = members[members.length - 1]
    const t = index.tracks[latest]
    const state: LadderState = t.transitions.length ? t.transitions[t.transitions.length - 1].to_state : 'DETECTED'
    const steps = stepsForTrack(t, { latest: true })
    const cur = [...steps].reverse().find((s) => s.status === 'current' || s.status === 'done')
    const situation = situationOf(state, steps, members.length)
    out.push({
      id: root,
      members,
      latest,
      state,
      rung: rungOf[latest]?.rung ?? RUNG_OF[state] ?? 'watch',
      tone: toneOf(state),
      sev: severityKey(state),
      situation: situation || cur?.sentence || '',
      stepper: steps.map((s) => s.status),
      lastEventTick: Math.max(...members.map((m) => index.tracks[m].lastEventTick)),
      stateTick: t.stateTick,
    })
  }
  return out.sort((a, b) => a.sev[0] - b.sev[0] || a.sev[1] - b.sev[1] || cmp(a.id, b.id))
}

const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0)

function situationOf(state: LadderState, steps: CaseStep[], n: number): string {
  const ask = steps.find((s) => s.kind === 'ask')
  const again = n > 1 ? `re-acquired ×${n - 1} · ` : ''
  switch (state) {
    case 'DETECTED':
    case 'WATCHING':
      return `${again}${steps.find((s) => s.kind === 'watch')?.sentence ?? 'watching'}`
    case 'ASKING':
    case 'CLEARED_AT_ASK':
    case 'LOST_VEHICLE':
      return `${again}${ask?.hail ?? 'asking'} · ${stateWords(state)}`
    case 'INVESTIGATING':
      return `${again}${ask?.hail ?? ''} · investigating`
    default:
      return `${again}${stateWords(state)}`
  }
}

/** The auto-follow choice: the most severe case; ties go to the latest escalation. */
export function mostSevere(cases: CaseSummary[]): string | null {
  let best: CaseSummary | null = null
  for (const c of cases) {
    if (!best || c.sev[0] < best.sev[0] || (c.sev[0] === best.sev[0] && (c.sev[1] < best.sev[1] || (c.sev[1] === best.sev[1] && c.stateTick > best.stateTick))))
      best = c
  }
  return best ? best.id : null
}

// ---------------------------------------------------------------- the case view and the NOW line

export function caseItems(
  index: CaseIndex,
  members: string[],
  posteriorOf: Record<string, PosteriorPayload>,
  rowIndex?: Map<string, EvidenceRow>,
): CaseItem[] {
  const out: CaseItem[] = []
  members.forEach((cid, i) => {
    const t = index.tracks[cid]
    if (!t) return
    if (i > 0) {
      out.push({ key: `${cid}:reacq`, kind: 'reacquired', contact_id: cid, from: t.continues ?? members[i - 1], tick: t.firstTick, note: t.continuesNote })
    }
    out.push(...stepsForTrack(t, { latest: i === members.length - 1, posterior: posteriorOf[cid], rowIndex }))
  })
  return out
}

export function nowLine(c: CaseSummary | undefined, items: CaseItem[], tickS: number): string {
  if (!c) return 'No contact yet. The harbor is quiet.'
  const steps = items.filter((x): x is CaseStep => x.kind !== 'reacquired' && x.contact_id === c.latest)
  const cur = [...steps].reverse().find((s) => s.status === 'current' || s.status === 'done')
  const next = steps.find((s) => s.status === 'pending')
  const who = c.latest !== c.id ? `${c.id} (as ${c.latest})` : c.id
  const since = cur?.tick != null ? ` since t ${Math.round(cur.tick * tickS)} s` : ''
  const what = cur ? ` — ${cur.sentence.replace(/\.$/, '')}` : ''
  const tail = next ? ` · next: ${next.title.toLowerCase()}` : ''
  return `${who} · ${stateWords(c.state)}${since}${what}${tail}`
}

export const rungLabel = (r: Rung) => RUNG_LABEL[r]
