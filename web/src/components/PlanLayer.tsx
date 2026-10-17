// WO-061 QR addendum: filed plans on the map (HarborMap overlay). Each plan's zone is drawn
// dashed with a marker at its centroid; a conflicted plan's marker is marked so. Display only.

import L from 'leaflet'
import { Fragment, useMemo } from 'react'
import { Marker, Polygon, Tooltip } from 'react-leaflet'
import type { HarborGeoJson, PlanView } from '../api'

interface Props {
  plans: PlanView[]
  harbor: HarborGeoJson | null
}

function zoneRing(harbor: HarborGeoJson | null, zoneId: string): [number, number][] {
  const f = harbor?.features.find((x) => x.properties.kind === 'zone' && x.properties.zone_id === zoneId)
  const ring = ((f?.geometry.coordinates as number[][][] | undefined) ?? [])[0] ?? []
  return ring.map(([lon, lat]) => [lat, lon] as [number, number])
}

function centroid(ring: [number, number][]): [number, number] | null {
  if (!ring.length) return null
  const lat = ring.reduce((s, p) => s + p[0], 0) / ring.length
  const lon = ring.reduce((s, p) => s + p[1], 0) / ring.length
  return [lat, lon]
}

const esc = (s: string) => s.replace(/[<>&"]/g, '')

export default function PlanLayer({ plans, harbor }: Props) {
  const placed = useMemo(() => {
    const perZone = new Map<string, number>()
    return plans.map((p) => {
      const ring = zoneRing(harbor, p.area)
      const c = centroid(ring)
      const k = perZone.get(p.area) ?? 0
      perZone.set(p.area, k + 1)
      const at: [number, number] | null = c ? [c[0] + k * 0.0004, c[1] + k * 0.0004] : null
      const conflicted = p.status === 'conflicted'
      const icon = L.divIcon({
        className: 'plan-mark',
        html: `<span class="plan-dot${conflicted ? ' conflicted' : ''}" data-testid="plan-marker" data-plan="${esc(p.plan_id)}" data-conflicted="${conflicted}"></span>`,
        iconSize: [12, 12],
        iconAnchor: [6, 6],
      })
      return { p, ring, at, icon, conflicted }
    })
  }, [plans, harbor])
  return (
    <>
      {placed.map(({ p, ring, at, icon, conflicted }) => (
        <Fragment key={p.plan_id}>
          {ring.length > 2 && <Polygon positions={ring} interactive={false} className="plan-zone" pathOptions={{ weight: 1.5, dashArray: '6 6', fillOpacity: 0.04 }} />}
          {at && (
            <Marker position={at} icon={icon} interactive>
              <Tooltip direction="top" offset={[0, -6]}>
                {p.plan_id} · {p.operator} · {conflicted ? 'conflicted' : 'filed'}
              </Tooltip>
            </Marker>
          )}
        </Fragment>
      ))}
    </>
  )
}
