import L from 'leaflet'
import { useEffect, useMemo, useRef } from 'react'
import { Circle, CircleMarker, MapContainer, Marker, Polygon, Polyline, TileLayer, Tooltip, useMapEvents } from 'react-leaflet'
import type { CircleMarker as LCircleMarker } from 'leaflet'
import type { AboutInfo, HarborGeoJson } from '../api'
import type { ContactView } from '../useRun'
import type { SensorRangeRow } from '../events' // WO-089

const CENTER: [number, number] = [33.736, -118.262]

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
    <Circle ref={ref} center={[lat, lon]} radius={radius} pathOptions={{ color: '#0e7490', weight: 1, dashArray: '4 4', fillOpacity: 0.03 }}>
      <Tooltip>{nl === null ? name : `${name}: range ${radius.toFixed(0)} m at NL ${nl.toFixed(1)} dB (simulated)`}</Tooltip>
    </Circle>
  )
}
// --- WO-089 end ---

function ClickCatcher({ onMapClick }: { onMapClick?(lat: number, lon: number): void }) {
  useMapEvents({ click: (e) => onMapClick?.(e.latlng.lat, e.latlng.lng) })
  return null
}

function ContactMarker({ c, selected, onSelect }: { c: ContactView; selected: boolean; onSelect(id: string): void }) {
  const ref = useRef<LCircleMarker | null>(null)
  useEffect(() => {
    const el = ref.current?.getElement()
    if (el) {
      el.setAttribute('data-testid', 'contact-marker')
      el.setAttribute('data-contact', c.contact_id)
    }
  })
  return (
    <CircleMarker
      ref={ref}
      center={[c.last.lat, c.last.lon]}
      radius={selected ? 8 : 6}
      pathOptions={{ color: selected ? '#0b2a3a' : '#2563eb', weight: selected ? 4 : 1.5, fillColor: '#60a5fa', fillOpacity: 0.85 }}
      eventHandlers={{ click: () => onSelect(c.contact_id) }}
    >
      <Tooltip>{c.contact_id}</Tooltip>
    </CircleMarker>
  )
}

const zoneColor = (crit: number) => `rgba(220, 38, 38, ${0.08 + 0.35 * crit})`

function ringFor(coords: unknown): [number, number][] {
  const ring = (coords as number[][][])[0] ?? []
  return ring.map(([lon, lat]) => [lat, lon] as [number, number])
}

export default function HarborMap({ harbor, about, tilesOn, contacts, selected, onSelect, onMapClick, overlay, ranges }: Props) {
  const features = harbor?.features ?? []
  const zones = features.filter((f) => f.properties.kind === 'zone')
  const assets = features.filter((f) => f.properties.kind === 'asset')
  const nodes = features.filter((f) => f.properties.kind === 'sensor_node')
  const icons = useMemo(() => {
    const out: Record<string, L.DivIcon> = {}
    for (const a of assets) {
      const size = Math.round(8 + 12 * Number(a.properties.criticality))
      out[String(a.properties.asset_id)] = L.divIcon({
        className: 'asset-diamond',
        html: `<span style="width:${size}px;height:${size}px"></span>`,
        iconSize: [size, size],
      })
    }
    return out
  }, [assets])

  return (
    <div className="map-wrap">
      <MapContainer center={CENTER} zoom={14} className="map" attributionControl={false}>
        {tilesOn && about && (
          <>
            <TileLayer url={about.tiles.base.url} attribution={about.tiles.base.attribution} />
            <TileLayer url={about.tiles.seamarks.url} attribution={about.tiles.seamarks.attribution} />
          </>
        )}
        <ClickCatcher onMapClick={onMapClick} />
        {zones.map((z) => (
          <Polygon
            key={String(z.properties.zone_id)}
            positions={ringFor(z.geometry.coordinates)}
            pathOptions={{ color: '#b91c1c', weight: 1, fillColor: zoneColor(Number(z.properties.criticality)), fillOpacity: 1 }}
          >
            <Tooltip>{`${z.properties.name} (criticality ${z.properties.criticality})`}</Tooltip>
          </Polygon>
        ))}
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
            <Marker key={String(a.properties.asset_id)} position={[lat, lon]} icon={icons[String(a.properties.asset_id)]}>
              <Tooltip>{`${a.properties.name} (criticality ${a.properties.criticality})`}</Tooltip>
            </Marker>
          )
        })}
        {contacts.map((c) => (
          <Polyline
            key={`trk-${c.contact_id}`}
            positions={c.track}
            pathOptions={{ color: c.contact_id === selected ? '#0b2a3a' : '#3b82f6', weight: c.contact_id === selected ? 3 : 1.5, opacity: 0.7 }}
          />
        ))}
        {contacts.map((c) => (
          <ContactMarker key={c.contact_id} c={c} selected={c.contact_id === selected} onSelect={onSelect} />
        ))}
        {overlay}
      </MapContainer>
      <div className="map-attribution" data-testid="map-attribution">
        {about?.tiles.map_attribution ?? '© OpenStreetMap contributors · © OpenSeaMap contributors'}
      </div>
    </div>
  )
}
