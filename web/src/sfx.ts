// Sound events (WO-062P). One module, fed by the same WebSocket events the audit log
// records: each `transition` into a sounding state, and a run that starts with twist
// toggles, and (WO-088) a `responder` event that starts a dispatch to look. Routine ticks,
// evidence rows and rungs make no sound. The files (five, plus WO-088's one) are made
// by scripts/gen_sfx.py (numpy, -18 dBFS peak, each < 1 s) and served from /sfx/.
//
// Mute: presenter mode defaults to UNMUTED, the normal screen to muted; each mode keeps its
// own choice in localStorage (wrapped: storage can be missing or throw).

import { useEffect, useRef } from 'react'
import type { LadderState } from './events'
import type { RunState, TimelineEntry } from './useRun'

export type SoundName =
  | 'hail_ping'
  | 'advisory_chime'
  | 'hold_tone'
  | 'handoff_chime'
  | 'twist_klaxon_lite'
  // --- WO-088 begin ---
  | 'responder_dispatch'
// --- WO-088 end ---

export const SOUND_NAMES: SoundName[] = [
  'hail_ping',
  'advisory_chime',
  'hold_tone',
  'handoff_chime',
  'twist_klaxon_lite',
  // --- WO-088 begin ---
  'responder_dispatch',
  // --- WO-088 end ---
]

/** A transition into one of these states is the audit event that sounds. */
export const STATE_SOUND: Partial<Record<LadderState, SoundName>> = {
  ASKING: 'hail_ping', // hail sent
  ADVISING_SLOW: 'advisory_chime', // advisory raised
  HOLDING: 'hold_tone', // hold placed
  HANDED_OFF: 'handoff_chime', // handed to a person
}

/** A run whose run_started carries toggles (or a presenter twist step) sounds this. */
export const TWIST_SOUND: SoundName = 'twist_klaxon_lite'

// --- WO-088 begin ---
/** A `responder` event that starts a dispatch to look sounds this (two short notes). */
export const RESPONDER_DISPATCH_SOUND: SoundName = 'responder_dispatch'

/** How many dispatch cues are new since `seen` (never negative). Pure. */
export function newDispatches(seen: number, total: number): number {
  return Math.max(0, total - seen)
}
// --- WO-088 end ---

/** The same sound at most once per window, so a 20x replay does not stack pings. */
export const MIN_GAP_MS = 250

/** Sounds for new transitions, in order, each sound at most once per batch. Pure. */
export function soundsForTransitions(entries: Pick<TimelineEntry, 'to_state'>[]): SoundName[] {
  const out: SoundName[] = []
  for (const e of entries) {
    const s = STATE_SOUND[e.to_state]
    if (s && !out.includes(s)) out.push(s)
  }
  return out
}

const STORAGE_KEY = { presenter: 'soundings.sfx.muted.presenter', screen: 'soundings.sfx.muted' }

export function loadMuted(presenter: boolean): boolean {
  try {
    const v = window.localStorage.getItem(presenter ? STORAGE_KEY.presenter : STORAGE_KEY.screen)
    if (v === '1') return true
    if (v === '0') return false
  } catch {
    // storage unavailable: fall through to the default
  }
  return !presenter
}

export function saveMuted(presenter: boolean, muted: boolean): void {
  try {
    window.localStorage.setItem(presenter ? STORAGE_KEY.presenter : STORAGE_KEY.screen, muted ? '1' : '0')
  } catch {
    // storage unavailable: the choice lasts for this page only
  }
}

export interface SfxLogEntry {
  t_ms: number
  sound: SoundName
  muted: boolean
}

declare global {
  interface Window {
    /** Every sound event this page decided on (played or muted), for the recording tests. */
    __soundingsSfx?: SfxLogEntry[]
  }
}

const cache = new Map<SoundName, HTMLAudioElement>()
const lastPlayed = new Map<SoundName, number>()

export function soundUrl(name: SoundName): string {
  return `${import.meta.env.BASE_URL}sfx/${name}.wav`
}

/** Play one sound now unless muted or played within MIN_GAP_MS; failures are silent. */
export function playSound(name: SoundName, muted: boolean): void {
  const now = performance.now()
  const last = lastPlayed.get(name)
  if (last !== undefined && now - last < MIN_GAP_MS) return
  lastPlayed.set(name, now)
  ;(window.__soundingsSfx ??= []).push({ t_ms: Math.round(now), sound: name, muted })
  if (muted || typeof Audio === 'undefined') return
  let base = cache.get(name)
  if (!base) {
    base = new Audio(soundUrl(name))
    base.preload = 'auto'
    cache.set(name, base)
  }
  const voice = base.cloneNode(true) as HTMLAudioElement
  voice.play().catch(() => {
    // autoplay refused before the first key press, or no audio device: stay silent
  })
}

/** Follow a run and sound its audit events. Muted events still advance, never replay. */
export function useSfx(run: RunState, muted: boolean): void {
  const seen = useRef<{ runId: string | null; n: number; twist: boolean }>({ runId: null, n: 0, twist: false })
  const mutedRef = useRef(muted)
  useEffect(() => {
    mutedRef.current = muted
  }, [muted])

  useEffect(() => {
    const s = seen.current
    if (s.runId !== run.runId) {
      seen.current = { runId: run.runId, n: 0, twist: false }
    }
    const cur = seen.current
    if (!cur.twist && run.started && run.started.toggles.length > 0) {
      cur.twist = true
      playSound(TWIST_SOUND, mutedRef.current)
    }
    if (run.timeline.length > cur.n) {
      const fresh = run.timeline.slice(cur.n)
      cur.n = run.timeline.length
      for (const sound of soundsForTransitions(fresh)) playSound(sound, mutedRef.current)
    }
  }, [run.runId, run.started, run.timeline])

  // --- WO-088 begin ---
  const dispatchSeen = useRef<{ runId: string | null; n: number }>({ runId: null, n: 0 })
  useEffect(() => {
    if (dispatchSeen.current.runId !== run.runId) dispatchSeen.current = { runId: run.runId, n: 0 }
    const cur = dispatchSeen.current
    if (newDispatches(cur.n, run.responderDispatches) > 0) {
      cur.n = run.responderDispatches
      playSound(RESPONDER_DISPATCH_SOUND, mutedRef.current)
    }
  }, [run.runId, run.responderDispatches])
  // --- WO-088 end ---
}
