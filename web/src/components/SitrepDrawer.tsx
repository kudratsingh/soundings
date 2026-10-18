import type { SitrepPayload } from '../events'

interface Props {
  open: boolean
  sitreps: (SitrepPayload & { tick: number })[]
  onClose(): void
  onCite(contactId: string, rowId: string): void
  /** WO-060D: the focused case's contact ids; its SITREPs are listed first. */
  focusIds?: Set<string>
}

export default function SitrepDrawer({ open, sitreps, onClose, onCite, focusIds }: Props) {
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
          // --- WO-060D: the focused case first (stable within each group) ---
          .sort((a, b) => Number(Boolean(focusIds?.has(b.contact_id))) - Number(Boolean(focusIds?.has(a.contact_id))))
          .map((s) => (
            <article
              key={`${s.contact_id}-${s.version}`}
              className={`sitrep${focusIds?.has(s.contact_id) ? ' focused' : ''}`}
              data-testid="sitrep-entry"
              data-kind={s.kind}
              data-focused={focusIds?.has(s.contact_id) ? '1' : '0'}
            >
              <h4>
                {s.kind === 'lost_vehicle' ? 'LOST_VEHICLE notice' : 'SITREP'} · {s.contact_id} · v{s.version} · tick {s.tick}{' '}
                <span className="muted small">writer: {s.writer}</span>
              </h4>
              {/* --- WO-060B begin: the WO-042 SITREP (draft / final / update) --- */}
              {s.kind === 'sitrep' && s.markdown && (
                <>
                  <p className="small" data-testid="sitrep-status">
                    {s.status ?? 'final'} · recommended rung {String(s.sitrep?.recommended_rung ?? '').replaceAll('_', ' ')} (computed; not editable) · writer {s.writer}
                    {s.path ? ` · ${s.path}` : ''}
                  </p>
                  <pre className="sitrep-md" data-testid="sitrep-markdown">
                    {s.markdown}
                  </pre>
                </>
              )}
              {/* --- WO-060B end --- */}
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
