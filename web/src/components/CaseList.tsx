// CASES (WO-060 design pass): one row per case (a contact lineage), sorted by severity:
// threat first, then needs-approval, then investigating, then cleared/resolved. 'auto'
// follows the most severe case. Up/down keys move the selection. Replaces the contact chips.
// WO-060E: during a run the focused case is the one row shown; every other case folds into one
// `others · n` row with a severity summary. Clicking it opens the rest (a click there pins the
// focus); `show all` (hidden in presenter mode) draws the whole list for debugging.

import type { KeyboardEvent } from 'react'
import { Fragment } from 'react' // WO-060E
import type { CaseSummary, StepStatus } from '../cases'
import { RUNG_LABEL } from '../events'
// --- WO-060E begin ---
import { casesView, othersLabel, othersSummary } from '../quiet'
// --- WO-060E end ---

interface Props {
  cases: CaseSummary[]
  selected: string | null
  auto: boolean
  tick: number
  tickS: number
  flashOf: Record<string, number>
  onSelect(id: string | null): void
  // --- WO-060E begin: fold the other cases ---
  /** The `others` row is open (default closed on every run start). */
  othersOpen?: boolean
  onOthersToggle?(): void
  /** Debugging: draw every case and every event; `null` hides the control (presenter mode). */
  showAll?: boolean | null
  onShowAll?(): void
  // --- WO-060E end ---
}

function Stepper({ steps }: { steps: StepStatus[] }) {
  return (
    <span className="mini-stepper" aria-hidden="true">
      {steps.map((s, i) => (
        <i key={i} className={`ms ${s}`} />
      ))}
    </span>
  )
}

export function ago(ticks: number, tickS: number): string {
  const s = Math.max(0, Math.round(ticks * tickS))
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min`
}

export default function CaseList({
  cases,
  selected,
  auto,
  tick,
  tickS,
  flashOf,
  onSelect,
  othersOpen = false, // WO-060E
  onOthersToggle, // WO-060E
  showAll = false, // WO-060E
  onShowAll, // WO-060E
}: Props) {
  // --- WO-060E begin ---
  const view = casesView(cases, selected, othersOpen, Boolean(showAll))
  // --- WO-060E end ---
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const ids = view.shown.map((c) => c.id) // WO-060E: the rows on screen
    const i = selected ? ids.indexOf(selected) : -1
    const j = e.key === 'ArrowDown' ? Math.min(ids.length - 1, i + 1) : Math.max(0, i - 1)
    if (ids[j]) onSelect(ids[j])
  }
  return (
    <section className="panel cases" aria-label="cases">
      <header className="panel-head">
        <h2>Cases</h2>
        <span className="label">{cases.length} · by severity</span>
        <button type="button" className={`auto-btn${auto ? ' on' : ''}`} aria-pressed={auto} data-testid="case-auto" onClick={() => onSelect(null)}>
          auto
        </button>
        {/* --- WO-060E begin: debugging only; never shown in presenter mode --- */}
        {showAll !== null && (
          <button
            type="button"
            className={`auto-btn show-all-btn${showAll ? ' on' : ''}`}
            aria-pressed={Boolean(showAll)}
            data-testid="show-all"
            title="debugging: every case, every sound, every transition"
            onClick={onShowAll}
          >
            show all
          </button>
        )}
        {/* --- WO-060E end --- */}
      </header>
      <div className="cases-list" data-testid="cases" role="listbox" tabIndex={0} aria-label="cases by severity" onKeyDown={onKey}>
        {cases.length === 0 && <p className="label empty">No contacts yet.</p>}
        {view.shown.map((c, i) => (
          <Fragment key={c.id}>
            <div
              role="option"
              aria-selected={c.id === selected}
              className={`case-row tone-${c.tone}${c.id === selected ? ' sel' : ''}${i > 0 && view.othersRow ? ' unfolded' : ''}`}
              data-testid="case-row"
              data-case={c.id}
              data-state={c.state}
              data-members={c.members.join(',')}
              data-members-n={c.members.length}
              data-focused={c.id === selected ? '1' : '0'} // WO-060E
              onClick={() => onSelect(c.id)}
            >
              {flashOf[c.id] !== undefined && <span key={flashOf[c.id]} className="flash" />}
              <i className="dot" />
              <span className="case-id">{c.id}</span>
              <span className={`pill tone-${c.tone}`}>{c.state.replaceAll('_', ' ')}</span>
              <span className="case-rung">{RUNG_LABEL[c.rung]}</span>
              <span className="case-sit" title={c.situation}>
                {c.situation}
              </span>
              <Stepper steps={c.stepper} />
              <span className="case-ago num">{ago(tick - c.lastEventTick, tickS)}</span>
            </div>
            {/* --- WO-060E begin: every other case behind one row, right under the focus --- */}
            {i === 0 && view.othersRow && (
              <button
                type="button"
                className={`others-row${othersOpen ? ' open' : ''}`}
                data-testid="others-row"
                data-n={view.others.length}
                aria-expanded={othersOpen}
                title={othersOpen ? 'fold the other cases' : 'open the other cases (a click on one pins the focus)'}
                onClick={onOthersToggle}
              >
                <span className="others-caret" aria-hidden="true">
                  {othersOpen ? '▾' : '▸'}
                </span>
                <span className="others-n num" data-testid="others-n">
                  {othersLabel(view.others)}
                </span>
                <span className="others-sum" data-testid="others-summary">
                  {othersSummary(view.others)}
                </span>
              </button>
            )}
            {/* --- WO-060E end --- */}
          </Fragment>
        ))}
      </div>
    </section>
  )
}
