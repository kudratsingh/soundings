// The responder on the harbor map (WO-088): the harbor's own small sensor carrier, a probe
// dispatched to look at a contact. It shows an icon pointing along its heading, a trail of
// the last 60 positions, the 25 m standoff circle around the contact it is looking at, a
// battery bar, and a pulsing ring while it is on station ("looking"). Display only:
// nothing here can dispatch, position or recall it (CLAUDE.md 2.2, section 8 A2).

import L from 'leaflet'
import { useMemo } from 'react'
import { Circle, Marker, Polyline, Tooltip } from 'react-leaflet'
import type { ResponderPayload } from '../events'
import { batteryFraction, responderIconHtml, responderLabel } from '../responder'

interface Props {
  responder: ResponderPayload | null
  /** The position of the contact it is looking at (from the tick stream), if seen. */
  target: [number, number] | null
}

export default function ResponderLayer({ responder, target }: Props) {
  const r = responder
  const looking = r?.state === 'ON_STATION'
  const heading = r ? Math.round(r.heading_deg) : 0
  const pct = r ? Math.round(batteryFraction(r) * 100) : 0
  const batteryMin = r ? Math.round(r.battery_min) : 0
  const icon = useMemo(
    () =>
      L.divIcon({
        className: `responder-icon${looking ? ' looking' : ''}`,
        html: responderIconHtml(heading, pct, batteryMin),
        iconSize: [24, 30],
        iconAnchor: [12, 12],
      }),
    [looking, heading, pct, batteryMin],
  )
  if (!r) return null
  const trail = r.trail.map((p) => [p.lat, p.lon] as [number, number])
  const showStandoff = target !== null && (r.state === 'TRANSIT' || r.state === 'ON_STATION')
  return (
    <>
      {trail.length > 1 && (
        <Polyline
          positions={trail}
          className="responder-trail"
          pathOptions={{ color: '#0f766e', weight: 2, opacity: 0.8, dashArray: '2 4' }}
        />
      )}
      {showStandoff && target && (
        <Circle
          center={target}
          radius={r.standoff_m}
          className="responder-standoff"
          pathOptions={{ color: '#d97706', weight: 1.5, dashArray: '4 3', fillOpacity: 0.04 }}
        >
          <Tooltip>{`${Math.round(r.standoff_m)} m standoff: the platform stays outside this circle`}</Tooltip>
        </Circle>
      )}
      <Marker position={[r.lat, r.lon]} icon={icon} interactive={false} keyboard={false} title={r.id}>
        <Tooltip permanent direction="right" offset={[12, 0]} className="responder-label">
          {responderLabel(r)}
          <span className="responder-battery-text">{` · battery ${Math.round(r.battery_min)} min`}</span>
        </Tooltip>
      </Marker>
    </>
  )
}
