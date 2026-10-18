// The presenter evidence card (WO-062C): on the `rung_ledger_eval` shot, above the caption
// bar, the current run's rung ledger beside the red blunt-policy counter (the same `ledger`
// events the ladder rail shows, WO-086) and the three-policy eval table from
// GET /api/eval/summary (artifacts/eval/report.json, `make eval`). Display only: every
// number is a stress-test estimate over the simulated catalog (CLAUDE.md 2.6), and nothing
// here sets a rung or feeds back into the ladder.

import { useEffect, useState } from 'react'
import type { LedgerPayload } from '../events'
import { LEDGER_BUCKETS } from '../events'
import type { EvalMetric, EvalSummary } from '../presenter'
import { fetchEvalSummary } from '../presenter'
import { LEDGER_LABELS } from './RungLedger'

export const EVIDENCE_LABELS = {
  estimate: 'stress-test estimate',
  ledgerTitle: 'This run: highest rung per contact',
  evalTitle: 'Catalog eval: three policies on the same seeded contacts',
  missing: 'run make eval',
  loading: 'loading the eval report…',
  foot: 'Simulated catalog; the baselines are counterfactual logs replayed on the same seeded sim.',
} as const

export const METRIC_LABELS: Record<EvalMetric, string> = {
  at_or_below_ask_pct: 'resolved at watch or ask',
  missed_threats: 'missed threats',
  false_shutdowns: 'false shutdowns',
  median_s_seen_to_handled: 'seconds seen → handled (median)',
  interruptions_per_hour: 'interruptions per hour',
  shutdown_minutes: 'berth-closed minutes',
}

const METRICS: EvalMetric[] = [
  'at_or_below_ask_pct',
  'missed_threats',
  'false_shutdowns',
  'median_s_seen_to_handled',
  'interruptions_per_hour',
  'shutdown_minutes',
]

export function formatMetric(metric: EvalMetric, value: number | null): string {
  if (value === null) return '–'
  if (metric === 'at_or_below_ask_pct') return `${value.toFixed(0)}%`
  if (metric === 'interruptions_per_hour') return value.toFixed(1)
  return Math.round(value).toLocaleString('en-US')
}

export default function EvidenceCard({ ledger }: { ledger: LedgerPayload | null }) {
  const [summary, setSummary] = useState<EvalSummary | null | undefined>(undefined)
  const [problem, setProblem] = useState<string | null>(null)
  useEffect(() => {
    fetchEvalSummary()
      .then(setSummary)
      .catch((e: unknown) => setProblem(String(e)))
  }, [])
  const blunt = ledger?.blunt
  return (
    <section className="presenter-evidence" data-testid="presenter-evidence" aria-label="rung ledger and eval">
      <div className="pe-ledger" data-testid="pe-ledger">
        <strong className="pe-title">{EVIDENCE_LABELS.ledgerTitle}</strong>
        <ul>
          {LEDGER_BUCKETS.map((b) => (
            <li key={b}>
              <span className="pe-n num" data-testid="pe-ledger-count" data-bucket={b}>
                {ledger ? ledger.counts[b] : 0}
              </span>
              <span>{LEDGER_LABELS[b]}</span>
            </li>
          ))}
        </ul>
        <div className="pe-blunt" data-testid="pe-blunt">
          <span className="pe-n num">{blunt && !blunt.available ? 'unavailable' : (blunt?.berth_closures ?? 0)}</span>
          <span>{LEDGER_LABELS.blunt}</span>
        </div>
        <span className="pe-est">{EVIDENCE_LABELS.estimate} · counterfactual</span>
      </div>
      <div className="pe-eval" data-testid="pe-eval">
        <strong className="pe-title">{EVIDENCE_LABELS.evalTitle}</strong>
        {summary === undefined && !problem && <p className="pe-missing">{EVIDENCE_LABELS.loading}</p>}
        {(summary === null || problem) && (
          <p className="pe-missing" data-testid="pe-eval-missing">
            {problem ?? EVIDENCE_LABELS.missing}
          </p>
        )}
        {summary && (
          <table data-testid="pe-eval-table">
            <thead>
              <tr>
                <th scope="col">{summary.label}</th>
                {summary.rows.map((r) => (
                  <th key={r.policy} scope="col" className={r.policy === 'soundings' ? 'pe-ours' : ''}>
                    {r.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {METRICS.map((m) => (
                <tr key={m}>
                  <th scope="row">{METRIC_LABELS[m]}</th>
                  {summary.rows.map((r) => (
                    <td key={r.policy} className={`num${r.policy === 'soundings' ? ' pe-ours' : ''}`} data-metric={m} data-policy={r.policy}>
                      {formatMetric(m, r[m])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <span className="pe-est">
          {EVIDENCE_LABELS.estimate} · {EVIDENCE_LABELS.foot}
          {summary?.seeds?.length ? ` Seeds ${summary.seeds.join(', ')}.` : ''}
        </span>
      </div>
    </section>
  )
}
