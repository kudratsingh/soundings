import type { EvidenceRow, LadderState, PosteriorPayload } from '../events'
import type { ContactView, RungView } from '../useRun'

interface Props {
  contact: ContactView | null
  state: LadderState | undefined
  rung: RungView | undefined
  rows: EvidenceRow[]
  posterior: PosteriorPayload | undefined
}

const SHOWN = [
  'time_to_nearest_critical_asset',
  'speed_over_ground',
  'depth',
  'registry_match',
  'hail_reply',
  'zone_id',
]

function fmt(v: EvidenceRow['value'], unit: string): string {
  if (typeof v === 'number' && unit === 's' && v >= 1e8) return 'not closing'
  if (typeof v === 'number') return `${Number.isInteger(v) ? v : v.toFixed(2)} ${unit === 'category' || unit === 'id' ? '' : unit}`.trim()
  return String(v)
}

export default function ContactCard({ contact, state, rung, rows, posterior }: Props) {
  if (!contact) return <section className="card" data-testid="contact-card"><p className="muted">No contact yet.</p></section>
  const latest: Record<string, EvidenceRow> = {}
  for (const r of rows) latest[r.feature] = r
  const top2 = posterior ? Object.entries(posterior.probs).sort((a, b) => b[1] - a[1]).slice(0, 2) : []
  return (
    <section className="card" data-testid="contact-card">
      <header className="card-head">
        <strong>{contact.contact_id}</strong>
        <span className="badge">{state ?? 'DETECTED'}</span>
        {rung && <span className="muted small">rung: {rung.rung.replaceAll('_', ' ')}</span>}
      </header>
      <dl className="facts">
        {SHOWN.filter((f) => latest[f]).map((f) => (
          <div key={f}>
            <dt>{f.replaceAll('_', ' ')}</dt>
            <dd>
              {fmt(latest[f].value, latest[f].unit)} <span className={`chip ${latest[f].kind}`}>{latest[f].kind.toUpperCase()}</span>
            </dd>
          </div>
        ))}
        <div>
          <dt>last fix</dt>
          <dd>
            {contact.last.lat.toFixed(5)}, {contact.last.lon.toFixed(5)} · depth {contact.last.depth_m.toFixed(1)} m · tick {contact.lastTick}
          </dd>
        </div>
      </dl>
      {posterior ? (
        <div className="posterior">
          {top2.map(([h, p]) => (
            <div key={h}>
              {h}: {(p * 100).toFixed(1)}% <span className="muted small">{posterior.label}</span>
            </div>
          ))}
          <div className="mass">
            <span style={{ width: `${posterior.threat_mass * 100}%` }} className="threat" />
            <span style={{ width: `${posterior.benign_mass * 100}%` }} className="benign" />
          </div>
        </div>
      ) : (
        <p className="muted small">Hypotheses (stress-test estimate) stream with the investigation runner (WO-060 part B).</p>
      )}
    </section>
  )
}
