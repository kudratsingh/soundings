// WO-089: About -> Physics. One card per model: the equation, every parameter with its value,
// unit, source and "demo value" label, and what the model does NOT include. Everything here
// is served by GET /api/physics from config/physics.yaml; nothing is typed into the UI.

import type { PhysicsInfo } from '../api'

interface Props {
  physics: PhysicsInfo | null
}

const fmt = (v: number | string) => (typeof v === 'number' ? String(Number(v.toFixed(3))) : v)
const pretty = (name: string) => name.replaceAll('_', ' ').replaceAll('.', ' · ')

export default function PhysicsPanel({ physics }: Props) {
  if (!physics) return <p className="small muted">Physics cards are loading.</p>
  if (physics.pending)
    return (
      <div data-testid="physics-panel">
        <h3>{physics.heading}</h3>
        <p className="small muted">{physics.pending}</p>
      </div>
    )
  return (
    <div data-testid="physics-panel">
      <h3>{physics.heading}</h3>
      <p className="small">
        Each card is one model the simulator runs. Every value is a <span className="demo-label">{physics.label}</span>{' '}
        with its source; the map's sensor rings are the passive-sonar range where detection probability is one half, recomputed
        from the current noise so they shrink when a vessel passes. Source: {physics.config}.
      </p>
      <div className="physics-cards">
        {physics.cards.map((c) => (
          <section className="physics-card" key={c.name} data-testid="physics-card">
            <h4>{c.name}</h4>
            <code className="equation">{c.equation}</code>
            <table className="params small">
              <tbody>
                {c.parameters.map((p) => (
                  <tr key={p.key}>
                    <td>{pretty(p.name)}</td>
                    <td className="num">
                      {fmt(p.value)} {p.unit}
                    </td>
                    <td>
                      <span className="demo-label">{p.label}</span>
                    </td>
                    <td className="muted">{p.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="small not-modeled">
              <strong>What we do NOT model:</strong> {c.not_modeled.join(', ')}.
            </p>
          </section>
        ))}
      </div>
      <h4>Sensor ranges at base noise</h4>
      <table className="params small">
        <tbody>
          {physics.nodes.map((n) => (
            <tr key={n.node_id}>
              <td>{n.name}</td>
              <td className="num">{n.effective_range_m.toFixed(0)} m</td>
              <td className="num">NL {n.nl_db.toFixed(1)} dB</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
