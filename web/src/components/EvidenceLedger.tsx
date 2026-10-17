import { useEffect } from 'react'
import type { EvidenceRow } from '../events'

interface Props {
  rows: EvidenceRow[]
  highlight: string | null
  limit?: number
}

const LABEL = { fact: 'FACT', assumption: 'ASSUMPTION', derived: 'DERIVED' } as const

export default function EvidenceLedger({ rows, highlight, limit = 150 }: Props) {
  const shown = rows.slice(-limit).reverse()
  useEffect(() => {
    if (highlight) document.getElementById(`row-${highlight}`)?.scrollIntoView({ block: 'center' })
  }, [highlight])
  return (
    <section className="ledger" aria-label="evidence ledger">
      <h3>
        Evidence <span className="muted small">newest first · {rows.length} rows</span>
      </h3>
      <div className="legend small">
        <span className="chip fact">FACT</span> observed · <span className="chip assumption">ASSUMPTION</span> taken as given ·{' '}
        <span className="chip derived">DERIVED</span> computed from rows
      </div>
      <ul>
        {shown.map((r) => (
          <li
            key={r.row_id}
            id={`row-${r.row_id}`}
            data-testid="ledger-row"
            data-kind={r.kind}
            className={`row ${r.kind}${r.row_id === highlight ? ' hl' : ''}`}
          >
            <span className={`chip ${r.kind}`}>{LABEL[r.kind]}</span>
            <span className="feat">{r.feature}</span>
            <span className="val">
              {String(r.value)} {r.unit}
            </span>
            <span className="meta muted small">
              {r.source} · conf {typeof r.confidence === 'number' ? r.confidence.toFixed(2) : r.confidence} · probe {r.probe_id ?? '-'} · t{' '}
              {r.t}
            </span>
            {r.kind === 'assumption' && <span className="assumes small">assumes: {r.assumes}</span>}
          </li>
        ))}
      </ul>
    </section>
  )
}
