import type { SitrepPayload } from '../events'

interface Props {
  open: boolean
  sitreps: (SitrepPayload & { tick: number })[]
  onClose(): void
  onCite(contactId: string, rowId: string): void
}

export default function SitrepDrawer({ open, sitreps, onClose, onCite }: Props) {
  if (!open) return null
  return (
    <aside className="drawer" data-testid="sitrep-drawer" aria-label="SITREP">
      <header className="drawer-head">
        <h3>SITREP</h3>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </header>
      {sitreps.length === 0 ? (
        <p className="muted">no SITREP yet</p>
      ) : (
        sitreps
          .slice()
          .reverse()
          .map((s) => (
            <article key={`${s.contact_id}-${s.version}`} className="sitrep">
              <h4>
                {s.kind === 'lost_vehicle' ? 'LOST_VEHICLE notice' : 'SITREP'} · {s.contact_id} · v{s.version} · tick {s.tick}{' '}
                <span className="muted small">writer: {s.writer}</span>
              </h4>
              {s.notice && (
                <>
                  <p className="small">
                    To {s.notice.operator} via {s.notice.contact_channel} (plan {s.notice.plan_id}, {s.notice.vehicle}). The watch officer is not
                    interrupted.
                  </p>
                  {(['Contact', 'What we know', 'What we assume', 'Search box'] as const).map((section) => (
                    <section key={section}>
                      <h5>{section}</h5>
                      <ul>
                        {s.notice!.lines
                          .filter((l) => l.section === section)
                          .map((l, i) => (
                            <li key={i} className={section === 'What we assume' ? 'assume-line' : ''}>
                              {l.text}{' '}
                              {l.evidence_row_ids.map((id) => (
                                <button type="button" key={id} className="cite" onClick={() => onCite(s.contact_id, id)}>
                                  {id.slice(0, 6)}
                                </button>
                              ))}
                            </li>
                          ))}
                      </ul>
                    </section>
                  ))}
                </>
              )}
            </article>
          ))
      )}
    </aside>
  )
}
