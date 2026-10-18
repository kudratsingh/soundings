// The harbor map (WO-060; radar-scope styling pulled forward from WO-086 in the design pass).
// Dark scope by default: a CSS filter on the OSM base tiles only (the OpenSeaMap seamarks and
// the attribution stay as served, so the tile policy is unchanged); `?style=chart` keeps the
// light chart. Zones are thin outlines with a light fill and a small label, assets small
// labelled diamonds, sensor rings subtle (WO-089 live ranges). Each contact draws a fading
// trail of its last 60 fixes, a 60 s velocity leader, and its case colour (benign teal,
// unknown white, threat amber); the selected case gets a halo and a label. The map never
// pans by itself unless 'follow selected' is on.
// WO-060D: with a focused case, 'follow selected' starts on (it pans only when the focused
// contact leaves the middle of the view) and every other contact is drawn at reduced opacity
// in its state colour; the responder overlay is untouched.

import L from 'leaflet'
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { Circle, CircleMarker, MapContainer, Marker, Polygon, Polyline, TileLayer, Tooltip, useMap, useMapEvents } from 'react-leaflet'
import type { CircleMarker as LCircleMarker } from 'leaflet'
import type { AboutInfo, HarborGeoJson } from '../api'
import type { Tone } from '../cases'
import type { ContactView } from '../useRun'
import type { SensorRangeRow } from '../events' // WO-089
// --- WO-086 begin ---
import { ellipseLatLngs, leaderEnd as projectedLeaderEnd } from '../radar'
// --- WO-086 end ---

const CENTER: [number, number] = [33.736, -118.262]
/** WO-060D: opacity factor for contacts outside the focused case. */
export const DIM = 0.35
export const TRAIL_FIXES = 60
const LEADER_S = 60

export type MapStyle = 'radar' | 'chart'

interface Props {
  harbor: HarborGeoJson | null
  about: AboutInfo | null
  tilesOn: boolean
  contacts: ContactView[]
  selected: string | null
  onSelect(contactId: string): void
  onMapClick?(lat: number, lon: number): void
  overlay?: React.ReactNode
  // --- WO-089 begin: live passive-sonar range per node (shrinks when a vessel passes) ---
  ranges?: Record<string, Pick<SensorRangeRow, 'effective_range_m' | 'nl_db'>>
  // --- WO-089 end ---
  /** Case colour of each contact id (design pass). */
  toneOf?: Record<string, Tone>
  /** Contact ids of the selected case (all of its lineage). */
  selectedIds?: Set<string>
  /** The case id each contact id belongs to (for the label). */
  caseLabel?: Record<string, string>
  mapStyle?: MapStyle
  tickS?: number
  /** WO-060D: draw contacts outside the focused case at reduced opacity. */
  dimOthers?: boolean
}

const TONE_COLOR: Record<Tone, string> = {
  threat: '#f5b13d',
  approval: '#f5b13d',
  unknown: '#f2f5f8',
  benign: '#3fd2c0',
}

// --- WO-089 begin: one ring per sensor node, radius from the live sensor_ranges ---
function SensorRing({ nodeId, name, lat, lon, radius, nl }: { nodeId: string; name: string; lat: number; lon: number; radius: number; nl: number | null }) {
  const ref = useRef<L.Circle | null>(null)
  useEffect(() => {
    const el = ref.current?.getElement()
    if (el) {
      el.setAttribute('data-testid', 'sensor-ring')
      el.setAttribute('data-node', nodeId)
      el.setAttribute('data-radius', radius.toFixed(1))
    }
  })
  return (
    <Circle ref={ref} center={[lat, lon]} radius={radius} className="sensor-ring" pathOptions={{ weight: 1, dashArray: '3 5', fillOpacity: 0.02 }}>
      <Tooltip>{nl === null ? name : `${name}: range ${radius.toFixed(0)} m at NL ${nl.toFixed(1)} dB (simulated)`}</Tooltip>
    </Circle>
  )
}
// --- WO-089 end ---

function ClickCatcher({ onMapClick }: { onMapClick?(lat: number, lon: number): void }) {
  useMapEvents({ click: (e) => onMapClick?.(e.latlng.lat, e.latlng.lng) })
  return null
}

function Follow({ at, on }: { at: [number, number] | null; on: boolean }) {
  const map = useMap()
  const last = useRef(0)
  useEffect(() => {
    if (!on || !at) return
    const now = performance.now()
    if (now - last.current < 1000) return
    last.current = now
    if (!map.getBounds().pad(-0.2).contains(at)) map.panTo(at, { animate: false })
  }, [on, at, map])
  return null
}

/** Fit the harbor to the map, and refit when the panel resizes, until the user pans or zooms
 * (the initial view only: nothing here follows a contact). */
function FitHarbor({ bounds }: { bounds: [number, number][] }) {
  const map = useMap()
  const touched = useRef(false)
  useEffect(() => {
    if (bounds.length < 2) return
    const el = map.getContainer()
    const fit = () => {
      map.invalidateSize({ animate: false })
      const size = map.getSize()
      if (!touched.current && size.x > 50 && size.y > 50) {
        map.fitBounds(L.latLngBounds(bounds), { padding: [28, 28], animate: false })
      }
    }
    const ro = new ResizeObserver(fit)
    ro.observe(el)
    const mark = () => {
      touched.current = true
    }
    el.addEventListener('pointerdown', mark)
    el.addEventListener('wheel', mark, { passive: true })
    return () => {
      ro.disconnect()
      el.removeEventListener('pointerdown', mark)
      el.removeEventListener('wheel', mark)
    }
  }, [bounds, map])
  return null
}

/** The 60 s velocity leader end from the last few fixes (one fix per tick). */
export function leaderEnd(track: [number, number][], tickS: number, seconds = LEADER_S): [number, number] | null {
  const n = track.length
  const k = Math.min(5, n - 1)
  if (k < 2) return null
  const [la, lo] = track[n - 1]
  const [pa, po] = track[n - 1 - k]
  const perTick: [number, number] = [(la - pa) / k, (lo - po) / k]
  const ticks = seconds / Math.max(tickS, 0.001)
  return [la + perTick[0] * ticks, lo + perTick[1] * ticks]
}

const ContactLayer = memo(function ContactLayer({
  c,
  selected,
  tone,
  label,
  tickS,
  onSelect,
  projected = false, // WO-086
  dim = false, // WO-060D
}: {
  c: ContactView
  selected: boolean
  tone: Tone
  label: string
  tickS: number
  onSelect(id: string): void
  /** WO-086: draw WO-084's 60 s projection ellipse for this contact. */
  projected?: boolean
  /** WO-060D: not the focused case: reduced opacity, still in its state colour. */
  dim?: boolean
}) {
  const ref = useRef<LCircleMarker | null>(null)
  useEffect(() => {
    const el = ref.current?.getElement()
    if (el) {
      el.setAttribute('data-testid', 'contact-marker')
      el.setAttribute('data-contact', c.contact_id)
      el.setAttribute('data-dim', dim ? '1' : '0') // WO-060D
    }
  })
  const color = TONE_COLOR[tone]
  const k = dim ? DIM : 1 // WO-060D
  const trail = c.track.slice(-TRAIL_FIXES)
  const third = Math.max(1, Math.floor(trail.length / 3))
  const segs = [trail.slice(0, third + 1), trail.slice(third, 2 * third + 1), trail.slice(2 * third)]
  const pos: [number, number] = [c.last.lat, c.last.lon]
  // --- WO-086 begin: leader from WO-084's smoothed velocity when it is streamed ---
  const proj = c.last.projection ?? null
  const lead = proj ? projectedLeaderEnd(pos[0], pos[1], proj.vx, proj.vy, LEADER_S) : leaderEnd(c.track, tickS)
  const ellipse = projected && proj ? ellipseLatLngs([proj.proj_lat, proj.proj_lon], proj.ellipse.semi_major_m, proj.ellipse.semi_minor_m, proj.ellipse.orientation_deg) : null
  // --- WO-086 end ---
  return (
    <>
      {/* --- WO-086 begin --- */}
      {ellipse && (
        <Polygon
          positions={ellipse}
          interactive={false}
          className="projection-ellipse"
          pathOptions={{ color, weight: 1, opacity: 0.8, dashArray: '4 3', fillOpacity: 0.06 }}
        />
      )}
      {/* --- WO-086 end --- */}
      {segs.map((s, i) =>
        s.length > 1 ? (
          <Polyline
            key={i}
            positions={s}
            interactive={false}
            className="contact-trail"
            pathOptions={{ color, weight: selected ? 2.5 : 1.5, opacity: [0.18, 0.4, 0.75][i] * k }}
          />
        ) : null,
      )}
      {lead && <Polyline positions={[pos, lead]} interactive={false} className="velocity-leader" pathOptions={{ color, weight: 1.5, opacity: 0.9 * k, dashArray: '2 4' }} />}
      {selected && <CircleMarker center={pos} radius={15} interactive={false} className="halo" pathOptions={{ color, weight: 1.5, opacity: 0.9, fillOpacity: 0.08 }} />}
      <CircleMarker
        ref={ref}
        center={pos}
        radius={selected ? 7 : 5}
        pathOptions={{ color: '#06121c', weight: 1.5, opacity: k, fillColor: color, fillOpacity: k }}
        eventHandlers={{ click: () => onSelect(c.contact_id) }}
      >
        <Tooltip permanent={selected} direction="right" offset={[10, 0]} className={`contact-label${selected ? ' sel' : ''}`}>
          {label}
        </Tooltip>
      </CircleMarker>
    </>
  )
})

function ringFor(coords: unknown): [number, number][] {
  const ring = (coords as number[][][])[0] ?? []
  return ring.map(([lon, lat]) => [lat, lon] as [number, number])
}

function Legend() {
  return (
    <div className="legend-panel" data-testid="legend-panel">
      <div>
        <i className="lg dot threat" /> threat
      </div>
      <div>
        <i className="lg dot unknown" /> unknown
      </div>
      <div>
        <i className="lg dot benign" /> benign
      </div>
      <div>
        <i className="lg line" /> trail · last 60 fixes
      </div>
      <div>
        <i className="lg dash" /> 60 s velocity leader
      </div>
      {/* --- WO-086 begin --- */}
      <div>
        <i className="lg ring" /> 60 s projection, 95% ellipse (stress-test estimate)
      </div>
      {/* --- WO-086 end --- */}
      <div>
        <i className="lg zone" /> security zone
      </div>
      <div>
        <i className="lg diamond" /> critical asset
      </div>
      <div>
        <i className="lg ring" /> sensor range (simulated)
      </div>
    </div>
  )
}

export default function HarborMap({
  harbor,
  about,
  tilesOn,
  contacts,
  selected,
  onSelect,
  onMapClick,
  overlay,
  ranges,
  toneOf = {},
  selectedIds,
  caseLabel = {},
  mapStyle = 'radar',
  tickS = 2,
  dimOthers = false, // WO-060D
}: Props) {
  const [legend, setLegend] = useState(false)
  const [follow, setFollow] = useState(true) // WO-060D: the focused contact is followed by default
  const features = harbor?.features ?? []
  const zones = useMemo(() => features.filter((f) => f.properties.kind === 'zone'), [features])
  const assets = useMemo(() => features.filter((f) => f.properties.kind === 'asset'), [features])
  const nodes = useMemo(() => features.filter((f) => f.properties.kind === 'sensor_node'), [features])
  const icons = useMemo(() => {
    const out: Record<string, L.DivIcon> = {}
    // Assets that differ only by a number or side (Fuel Berth 1/2) share one label.
    const base = (n: string) => n.replace(/\s*(\d+|\((west|east|north|south)\))$/i, '')
    const count = new Map<string, number>()
    for (const a of assets) count.set(base(String(a.properties.name)), (count.get(base(String(a.properties.name))) ?? 0) + 1)
    const labelled = new Set<string>()
    for (const a of assets) {
      const b = base(String(a.properties.name))
      const n = count.get(b) ?? 1
      const text = labelled.has(b) ? '' : n > 1 ? `${b} ×${n}` : String(a.properties.name)
      labelled.add(b)
      out[String(a.properties.asset_id)] = L.divIcon({
        className: 'asset-mark',
        html: `<span class="asset-diamond"></span>${text ? `<span class="asset-name">${text.replace(/[<>&]/g, '')}</span>` : ''}`,
        iconSize: [10, 10],
        iconAnchor: [5, 5],
      })
    }
    return out
  }, [assets])
  const harborBounds = useMemo(() => zones.flatMap((z) => ringFor(z.geometry.coordinates)), [zones])
  const isSel = (id: string) => (selectedIds ? selectedIds.has(id) : id === selected)
  const followAt = useMemo(() => {
    const c = contacts.find((x) => isSel(x.contact_id) && x.contact_id === selected) ?? contacts.find((x) => isSel(x.contact_id))
    return c ? ([c.last.lat, c.last.lon] as [number, number]) : null
  }, [contacts, selected, selectedIds])
  // --- WO-086 begin: the selected case's most recent member that has a projection ---
  const projectedId = useMemo(() => {
    let best: ContactView | null = null
    for (const c of contacts) {
      if (!isSel(c.contact_id) || !c.last.projection) continue
      if (!best || c.lastTick > best.lastTick || (c.lastTick === best.lastTick && c.contact_id === selected)) best = c
    }
    return best?.contact_id ?? null
  }, [contacts, selected, selectedIds])
  // --- WO-086 end ---

  return (
    <div className={`map-wrap ${mapStyle}`}>
      <MapContainer center={CENTER} zoom={14} zoomSnap={0.25} className="map" attributionControl={false} zoomControl={false}>
        {tilesOn && about && (
          <>
            <TileLayer url={about.tiles.base.url} attribution={about.tiles.base.attribution} className="base-tiles" />
            <TileLayer url={about.tiles.seamarks.url} attribution={about.tiles.seamarks.attribution} className="seamark-tiles" />
          </>
        )}
        <ClickCatcher onMapClick={onMapClick} />
        <FitHarbor bounds={harborBounds} />
        <Follow at={followAt} on={follow} />
        {zones.map((z) => {
          const crit = Number(z.properties.criticality)
          return (
            <Polygon
              key={String(z.properties.zone_id)}
              positions={ringFor(z.geometry.coordinates)}
              className="zone"
              pathOptions={{ weight: 1, fillOpacity: 0.08 + 0.07 * crit }}
            >
              <Tooltip permanent direction="center" className="zone-label">
                {String(z.properties.name)}
              </Tooltip>
            </Polygon>
          )
        })}
        {/* --- WO-089 begin: rings from the live sensor_ranges (harbor range_m until the first one) --- */}
        {nodes.map((n) => {
          const [lon, lat] = n.geometry.coordinates as number[]
          const id = String(n.properties.node_id)
          const live = ranges?.[id]
          return (
            <SensorRing
              key={id}
              nodeId={id}
              name={String(n.properties.name)}
              lat={lat}
              lon={lon}
              radius={live ? live.effective_range_m : Number(n.properties.range_m)}
              nl={live ? live.nl_db : null}
            />
          )
        })}
        {/* --- WO-089 end --- */}
        {assets.map((a) => {
          const [lon, lat] = a.geometry.coordinates as number[]
          return (
            <Marker key={String(a.properties.asset_id)} position={[lat, lon]} icon={icons[String(a.properties.asset_id)]} keyboard={false}>
              <Tooltip>{`${a.properties.name} (criticality ${a.properties.criticality})`}</Tooltip>
            </Marker>
          )
        })}
        {contacts.map((c) => (
          <ContactLayer
            key={c.contact_id}
            c={c}
            selected={isSel(c.contact_id)}
            tone={toneOf[c.contact_id] ?? 'unknown'}
            label={caseLabel[c.contact_id] && caseLabel[c.contact_id] !== c.contact_id ? `${caseLabel[c.contact_id]} (${c.contact_id})` : c.contact_id}
            tickS={tickS}
            onSelect={onSelect}
            projected={c.contact_id === projectedId} // WO-086
            dim={dimOthers && !isSel(c.contact_id)} // WO-060D
          />
        ))}
        {overlay}
      </MapContainer>
      <div className="map-tools">
        <button type="button" data-testid="legend" aria-expanded={legend} onClick={() => setLegend((v) => !v)}>
          Legend
        </button>
        <button type="button" data-testid="follow" aria-pressed={follow} className={follow ? 'on' : ''} onClick={() => setFollow((v) => !v)}>
          {follow ? 'following selected' : 'follow selected'}
        </button>
      </div>
      {legend && <Legend />}
      <div className="map-attribution" data-testid="map-attribution">
        {about?.tiles.map_attribution ?? '© OpenStreetMap contributors · © OpenSeaMap contributors'}
      </div>
    </div>
  )
}
