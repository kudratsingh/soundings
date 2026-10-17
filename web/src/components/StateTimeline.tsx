import type { TimelineEntry } from '../useRun'

interface Props {
  entries: TimelineEntry[]
  selected: string | null
}

export default function StateTimeline({ entries, selected }: Props) {
  const shown = entries.slice(-200).reverse()
  return (
    <section className="timeline" data-testid="state-timeline" aria-label="state timeline">
      <h3>State timeline</h3>
      <ol>
        {shown.map((t) => (
          <li key={t.seq} className={t.contact_id === selected ? 'sel' : ''}>
            <span className="muted small">tick {t.tick}</span> {t.case_id} {t.from_state} → <strong>{t.to_state}</strong>{' '}
            <span className="muted small">
              {t.reason_code} · {t.guard ? `guard ${t.guard.name}: ${t.guard.result}` : 'no guard (ask rung)'}
            </span>
          </li>
        ))}
      </ol>
    </section>
  )
}
