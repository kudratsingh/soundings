// The rung ledger (WO-086): contacts so far per bucket of their highest rung, counted with the
// eval harness helpers, and beside it in red the counterfactual "berth closures under the
// blunt policy" (WO-050's bang baseline on the same seeded contacts). Display only: nothing
// here sets a rung, proposes an action or feeds back into the ladder.

import type { LedgerBucket, LedgerPayload } from '../events'
import { LEDGER_BUCKETS } from '../events'

export const LEDGER_LABELS: Record<LedgerBucket | 'blunt', string> = {
  "resolved_at_watch": "resolved at watch",
  "cleared_at_ask": "cleared at ask",
  "advisories": "advisories",
  "holds": "holds",
  "handed_off": "handed off",
  "blunt": "berth closures under the blunt policy",
}

export const LEDGER_TOOLTIPS: Record<LedgerBucket, string> = {
  "resolved_at_watch": "Contacts that never needed more than watching.",
  "cleared_at_ask": "Contacts whose highest rung was ask: a registry lookup, a hail or an investigation.",
  "advisories": "Contacts that reached a slow advisory under standing orders.",
  "holds": "Contacts that reached a hold of one asset under standing orders.",
  "handed_off": "Contacts handed to a person; the work stops at the handoff.",
}

/** The red number is a counterfactual, never something that happened (CLAUDE.md 2.6). The
 * second sentence is config/eval.yaml baselines.assumptions.approval_assumed. */
export const BLUNT_TOOLTIP = [
  "counterfactual: the bang baseline replayed on the same seeded contacts, one closure per unknown contact (distinct closure episodes in brackets).",
  "bang assumes the person approves every closure request; the approval is modeled, not observed.",
].join(' ')

export default function RungLedger({ ledger }: { ledger: LedgerPayload | null }) {
  const blunt = ledger?.blunt
  return (
    <section className="rung-ledger" data-testid="ledger-panel" aria-label="rung ledger">
      <ul className="ledger-counts">
        {LEDGER_BUCKETS.map((b) => (
          <li key={b} title={LEDGER_TOOLTIPS[b]}>
            <span className="num ledger-n" data-testid="ledger-count" data-bucket={b}>
              {ledger ? ledger.counts[b] : 0}
            </span>
            <span className="label">{LEDGER_LABELS[b]}</span>
          </li>
        ))}
      </ul>
      <div className="blunt" title={BLUNT_TOOLTIP}>
        {blunt && !blunt.available ? (
          <span className="num ledger-n blunt-count" data-testid="blunt-count" data-available="false">
            unavailable
          </span>
        ) : (
          <span className="num ledger-n blunt-count" data-testid="blunt-count" data-available="true">
            {blunt ? blunt.berth_closures : 0}
          </span>
        )}
        <span className="label">
          {LEDGER_LABELS.blunt}
          {blunt && blunt.available && (
            <span className="blunt-sub num" data-testid="blunt-sub">
              {' '}
              ({blunt.episodes} episode{blunt.episodes === 1 ? '' : 's'} · {blunt.closure_minutes.toFixed(0)} min)
            </span>
          )}
        </span>
      </div>
    </section>
  )
}
