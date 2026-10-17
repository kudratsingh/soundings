// The global strip under the top bar (WO-060 design pass): the NOW line for the selected case
// and KEY MOMENTS, the last five important events across all cases. Clicking a moment
// selects its case. Routine rows never appear here.

import type { Moment } from '../cases'

interface NowProps {
  text: string
  tone: string
}

export function NowLine({ text, tone }: NowProps) {
  return (
    <p className={`now-line tone-${tone}`} data-testid="now-line" title={text}>
      <span className="now-label">Now</span>
      <span className="now-text">{text}</span>
    </p>
  )
}

interface MomentsProps {
  moments: Moment[]
  caseOf(contactId: string): string
  tickS: number
  onSelect(caseId: string): void
}

export const KEY_MOMENTS_SHOWN = 5

export function KeyMoments({ moments, caseOf, tickS, onSelect }: MomentsProps) {
  const last = moments.slice(-KEY_MOMENTS_SHOWN).reverse()
  return (
    <div className="key-moments" data-testid="key-moments" aria-label="key moments">
      <span className="now-label">Key moments</span>
      <ol>
        {last.length === 0 && <li className="label">none yet</li>}
        {last.map((m, slot) => {
          const cid = caseOf(m.contact_id)
          // keyed by slot: a new moment rewrites the text in place instead of sliding chips
          return (
            <li key={slot}>
              <button type="button" className={`moment num tone-${m.kind}`} data-testid="moment-chip" data-case={cid} onClick={() => onSelect(cid)}>
                {`${cid} · ${m.label} · t ${Math.round(m.tick * tickS)}`}
              </button>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
