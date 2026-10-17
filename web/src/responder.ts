// Responder display helpers (WO-088): labels and the battery fraction for ResponderLayer.
// Pure functions only; nothing here can dispatch, position or recall the responder.

import type { ResponderPayload, ResponderStateName } from './events'

export const RESPONDER_STATE_LABEL: Record<ResponderStateName, string> = {
  DOCKED: 'docked',
  TRANSIT: 'dispatched to look',
  ON_STATION: 'looking',
  RETURNING: 'return to dock',
}

/** Battery left as a fraction of capacity, clamped to [0, 1]. */
export function batteryFraction(r: Pick<ResponderPayload, 'battery_min' | 'battery_capacity_min'>): number {
  if (!(r.battery_capacity_min > 0)) return 0
  return Math.min(1, Math.max(0, r.battery_min / r.battery_capacity_min))
}

/** The one-line label: `resp_1 · looking at c-0003 · 25 m standoff`. */
export function responderLabel(r: ResponderPayload): string {
  const parts = [r.id, RESPONDER_STATE_LABEL[r.state]]
  if (r.target_contact_id && (r.state === 'TRANSIT' || r.state === 'ON_STATION')) {
    parts[1] = `${parts[1]} at ${r.target_contact_id}`
  }
  if (r.state === 'ON_STATION') parts.push(`${Math.round(r.standoff_m)} m standoff`)
  return parts.join(' · ')
}

/** The marker's HTML: a heading arrow, the 'looking' ring and the battery bar. */
export function responderIconHtml(headingDeg: number, batteryPct: number, batteryMin: number): string {
  const low = batteryPct < 25 ? ' low' : ''
  return (
    `<span class="responder-ring"></span>` +
    `<svg class="responder-arrow" viewBox="0 0 20 20" width="20" height="20" style="transform:rotate(${headingDeg}deg)">` +
    `<path d="M10 1 L17 18 L10 14 L3 18 Z" /></svg>` +
    `<span class="responder-battery${low}" data-testid="responder-battery" title="battery ${batteryMin} min">` +
    `<span style="width:${batteryPct}%"></span></span>`
  )
}
