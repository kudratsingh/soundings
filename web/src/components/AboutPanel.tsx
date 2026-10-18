import { useState } from 'react' // WO-089
import type { AboutInfo, BoundaryInfo } from '../api'
// --- WO-089 begin ---
import type { PhysicsInfo } from '../api'
import PhysicsPanel from './PhysicsPanel'
// --- WO-089 end ---

interface Props {
  open: boolean
  about: AboutInfo | null
  boundary: BoundaryInfo | null
  onClose(): void
  physics?: PhysicsInfo | null // WO-089
}

export default function AboutPanel({ open, about, boundary, onClose, physics = null }: Props) {
  // --- WO-089 begin: About | Physics tabs ---
  const [tab, setTab] = useState<'about' | 'physics'>('about')
  // --- WO-089 end ---
  if (!open) return null
  const h = about?.honesty
  return (
    <div className="modal-back" role="dialog" aria-modal="true" aria-label="About" data-testid="about-panel">
      <div className="modal">
        <header className="drawer-head">
          <h2>{about?.title ?? 'About'}</h2>
          {/* REH: focus lands on Close when the panel opens; Escape also closes it (App) */}
          <button type="button" onClick={onClose} autoFocus data-testid="about-close">
            Close
          </button>
        </header>
        {/* --- WO-089 begin: About | Physics tabs --- */}
        <nav className="tabs" role="tablist">
          <button type="button" role="tab" aria-selected={tab === 'about'} className={tab === 'about' ? 'on' : ''} onClick={() => setTab('about')}>
            About
          </button>
          <button
            type="button"
            role="tab"
            data-testid="physics-tab"
            aria-selected={tab === 'physics'}
            className={tab === 'physics' ? 'on' : ''}
            onClick={() => setTab('physics')}
          >
            Physics
          </button>
        </nav>
        {tab === 'physics' && <PhysicsPanel physics={physics} />}
        {tab === 'about' && (
          <>
        {/* --- WO-089 end --- */}
        {h && (
          <>
            <h3>{h.detection_heading}</h3>
            <ul className="small">
              {h.detection_assumptions.map((a) => (
                <li key={a}>{a}</li>
              ))}
            </ul>
            <table className="params small">
              <tbody>
                {Object.entries(h.detection_params).map(([k, v]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td>{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="small">The equations, parameters and sources are on the Physics tab.</p>{/* WO-089 */}
            <p>Probabilities are labeled “{h.probability_label}”.</p>
            <p>{h.shared_tables_note}</p>
            <p>Map: {h.map_note}.</p>
            <p>
              <strong>{h.handoff_line}</strong> {h.retention} Model mode: {h.model_mode}.
            </p>
          </>
        )}
        {boundary && (
          <>
            <h3>Boundary table</h3>
            <table className="boundary small">
              <thead>
                <tr>
                  <th>rung</th>
                  <th>action</th>
                  <th>who decides</th>
                </tr>
              </thead>
              <tbody>
                {boundary.rows.map((r) => (
                  <tr key={r.action} className={boundary.lives_on.includes(r.rung) ? 'lives' : ''}>
                    <td>{r.rung.replaceAll('_', ' ')}</td>
                    <td>{r.action}</td>
                    <td>{r.cell === r.yaml_cell ? r.cell : `${r.yaml_cell} (platform: ${r.cell})`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="small">
              Standing orders: {boundary.standing_orders.signed ? 'signed' : `not valid (${boundary.standing_orders.reason})`} by{' '}
              {boundary.standing_orders.signer}, valid until {boundary.standing_orders.valid_until}; {boundary.standing_orders.signature_label}.
            </p>
          </>
        )}
        {about && (
          <>
            <h3>Map tiles</h3>
            <p className="small">{about.tiles.map_attribution}</p>
            <p className="small">
              {about.tile_policy.osm_summary} {about.tile_policy.openseamap_summary} Policy: {about.tile_policy.osm_policy_url} (checked{' '}
              {about.tile_policy.verified_on}). {about.tile_policy.offline_note}
            </p>
          </>
        )}
        {/* --- WO-089 begin --- */}
          </>
        )}
        {/* --- WO-089 end --- */}
      </div>
    </div>
  )
}
