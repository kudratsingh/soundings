// The run stream: a WebSocket feeding a pure reducer. Events are buffered and folded in
// batches so a 13k-event replay stays smooth; a dropped socket reconnects with ?after=<seq>.

import { useEffect, useRef, useState } from 'react'
import { runSocketUrl } from './api'
import type {
  ApprovalRequestPayload,
  EvidenceRow,
  LadderState,
  PosteriorPayload,
  Rung,
  RunFinishedPayload,
  RunStartedPayload,
  SensorRangeRow, // WO-089
  SitrepPayload,
  StreamEvent,
  TickContact,
  TransitionPayload,
} from './events'
import { RUNGS } from './events'

export const TRACK_POINTS = 400

export interface ContactView {
  contact_id: string
  last: TickContact
  lastTick: number
  track: [number, number][]
}

export interface RungView {
  rung: Rung
  state: LadderState
  tick: number
}

export interface TimelineEntry extends TransitionPayload {
  tick: number
  seq: number
}

export interface RunState {
  runId: string | null
  started: RunStartedPayload | null
  lastSeq: number
  tick: number
  t_s: number
  contacts: Record<string, ContactView>
  rows: EvidenceRow[]
  rowsByContact: Record<string, EvidenceRow[]>
  timeline: TimelineEntry[]
  stateOf: Record<string, LadderState>
  rungOf: Record<string, RungView>
  posteriorOf: Record<string, PosteriorPayload>
  pendingApproval: ApprovalRequestPayload | null
  sitreps: (SitrepPayload & { tick: number })[]
  finished: RunFinishedPayload | null
  error: string | null
  // --- WO-089 begin ---
  sensorRanges: Record<string, SensorRangeRow>
  // --- WO-089 end ---
}

export const initialRunState = (runId: string | null): RunState => ({
  runId,
  started: null,
  lastSeq: -1,
  tick: 0,
  t_s: 0,
  contacts: {},
  rows: [],
  rowsByContact: {},
  timeline: [],
  stateOf: {},
  rungOf: {},
  posteriorOf: {},
  pendingApproval: null,
  sitreps: [],
  finished: null,
  error: null,
  // --- WO-089 begin ---
  sensorRanges: {},
  // --- WO-089 end ---
})

/** Fold a batch of events into a new state (pure: the input state is not mutated). */
export function reduceEvents(prev: RunState, events: StreamEvent[]): RunState {
  if (events.length === 0) return prev
  const s: RunState = {
    ...prev,
    contacts: { ...prev.contacts },
    rows: prev.rows.slice(),
    rowsByContact: { ...prev.rowsByContact },
    timeline: prev.timeline.slice(),
    stateOf: { ...prev.stateOf },
    rungOf: { ...prev.rungOf },
    posteriorOf: { ...prev.posteriorOf },
    sitreps: prev.sitreps.slice(),
  }
  const touchedRows = new Set<string>()
  for (const e of events) {
    if (e.seq <= s.lastSeq) continue // a resumed socket may repeat nothing; be safe anyway
    s.lastSeq = e.seq
    s.tick = e.tick
    s.t_s = e.t_s
    switch (e.type) {
      case 'run_started':
        s.started = e.payload
        break
      case 'tick':
        for (const c of e.payload.contacts) {
          const old = s.contacts[c.contact_id]
          const track = old ? old.track.slice(-(TRACK_POINTS - 1)) : []
          track.push([c.lat, c.lon])
          s.contacts[c.contact_id] = { contact_id: c.contact_id, last: c, lastTick: e.tick, track }
        }
        break
      case 'evidence_row': {
        const row = { ...e.payload.row, row_id: e.payload.row_id }
        s.rows.push(row)
        const cid = row.contact_id
        if (!touchedRows.has(cid)) {
          s.rowsByContact[cid] = (s.rowsByContact[cid] ?? []).slice()
          touchedRows.add(cid)
        }
        s.rowsByContact[cid].push(row)
        break
      }
      case 'transition':
        s.timeline.push({ ...e.payload, tick: e.tick, seq: e.seq })
        s.stateOf[e.payload.contact_id] = e.payload.to_state
        break
      case 'rung':
        s.rungOf[e.payload.contact_id] = { rung: e.payload.rung, state: e.payload.state, tick: e.tick }
        break
      case 'posterior':
        s.posteriorOf[e.payload.contact_id] = e.payload
        break
      case 'approval_request':
        s.pendingApproval = e.payload
        break
      case 'approval_result':
        if (s.pendingApproval?.request_id === e.payload.request_id) s.pendingApproval = null
        break
      case 'sitrep':
        s.sitreps.push({ ...e.payload, tick: e.tick })
        break
      case 'run_finished':
        s.finished = e.payload
        break
      case 'error':
        s.error = e.payload.message
        break
      // --- WO-089 begin: the latest ring per node (sent when a ring changes) ---
      case 'sensor_ranges': {
        const next: Record<string, SensorRangeRow> = {}
        for (const r of e.payload.nodes) next[r.node_id] = r
        s.sensorRanges = next
        break
      }
      // --- WO-089 end ---
    }
  }
  return s
}

const rungIndex = (r: Rung) => RUNGS.indexOf(r)

/** The contact to show when the officer has not picked one: highest rung, then latest. */
export function autoSelect(s: RunState): string | null {
  let best: string | null = null
  let bestKey: [number, number] = [-1, -1]
  for (const [cid, rv] of Object.entries(s.rungOf)) {
    const key: [number, number] = [rungIndex(rv.rung), rv.tick]
    if (key[0] > bestKey[0] || (key[0] === bestKey[0] && key[1] > bestKey[1])) {
      best = cid
      bestKey = key
    }
  }
  if (best !== null) return best
  const seen = Object.values(s.contacts).sort((a, b) => b.lastTick - a.lastTick)
  return seen.length ? seen[0].contact_id : null
}

export function useRun(runId: string | null): RunState {
  const [state, setState] = useState<RunState>(() => initialRunState(runId))
  const buffer = useRef<StreamEvent[]>([])
  const lastSeq = useRef(-1)

  useEffect(() => {
    setState(initialRunState(runId))
    buffer.current = []
    lastSeq.current = -1
    if (!runId) return
    let closed = false
    let finished = false
    let socket: WebSocket | null = null
    let retry: number | undefined

    const connect = () => {
      socket = new WebSocket(runSocketUrl(runId, lastSeq.current))
      socket.onmessage = (msg) => {
        const event = JSON.parse(String(msg.data)) as StreamEvent
        if (event.seq <= lastSeq.current && event.type !== 'error') return
        lastSeq.current = event.seq
        if (event.type === 'run_finished' || event.type === 'error') finished = true
        buffer.current.push(event)
      }
      socket.onclose = () => {
        if (!closed && !finished) retry = window.setTimeout(connect, 1000)
      }
    }
    connect()
    const flush = window.setInterval(() => {
      if (buffer.current.length === 0) return
      const batch = buffer.current
      buffer.current = []
      setState((prev) => reduceEvents(prev, batch))
    }, 100)
    return () => {
      closed = true
      window.clearInterval(flush)
      if (retry !== undefined) window.clearTimeout(retry)
      socket?.close()
    }
  }, [runId])

  return state
}
