// CASES (WO-060 design pass): one row per case (a contact lineage), sorted by severity:
// threat first, then needs-approval, then investigating, then cleared/resolved. Up/down keys
// move the selection. Replaces the contact chips.
// WO-060D: the badge shows the focus mode (sticky: the first case past ask holds the screen;
// auto: the most severe case; pinned: the row the user clicked). Clicking it returns a pinned
// focus to sticky, and toggles sticky/auto. Every concurrent case keeps its row.

import type { KeyboardEvent } from 'react'
import type { CaseSummary, StepStatus } from '../cases'
import { RUNG_LABEL } from '../events'
import type { FocusMode } from '../focus' // WO-060D

interface Props {
  cases: CaseSummary[]
  selected: string | null
  // --- WO-060D begin ---
  mode: FocusMode
  onMode(): void
  // --- WO-060D end ---
  tick: number
  tickS: number
  flashOf: Record<string, number>
  onSelect(id: string | null): void
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

export default function CaseList({ cases, selected, mode, onMode, tick, tickS, flashOf, onSelect }: Props) {
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const ids = cases.map((c) => c.id)
    const i = selected ? ids.indexOf(selected) : -1
    const j = e.key === 'ArrowDown' ? Math.min(ids.length - 1, i + 1) : Math.max(0, i - 1)
    if (ids[j]) onSelect(ids[j])
  }
  return (
    <section className="panel cases" aria-label="cases">
      <header className="panel-head">
        <h2>Cases</h2>
        <span className="label">{cases.length} · by severity</span>
        {/* --- WO-060D begin: the focus mode (fixed width: switching never moves the row) --- */}
        <button
          type="button"
          className={`auto-btn focus-btn mode-${mode}`}
          data-testid="focus-mode"
          data-mode={mode}
          title={
            mode === 'pinned'
              ? 'focus pinned to the case you clicked; click to return to sticky'
              : mode === 'sticky'
                ? 'focus stays on the first case past ask; click for auto (most severe)'
                : 'focus follows the most severe case; click for sticky'
          }
          onClick={onMode}
        >
          focus: {mode}
        </button>
        {/* --- WO-060D end --- */}
      </header>
      <div className="cases-list" data-testid="cases" role="listbox" tabIndex={0} aria-label="cases by severity" onKeyDown={onKey}>
        {cases.length === 0 && <p className="label empty">No contacts yet.</p>}
        {cases.map((c) => (
          <div
            key={c.id}
            role="option"
            aria-selected={c.id === selected}
            aria-current={c.id === selected ? 'true' : undefined} // WO-060D: the focused row
            data-focused={c.id === selected ? '1' : '0'} // WO-060D
            className={`case-row tone-${c.tone}${c.id === selected ? ' sel' : ''}`}
            data-testid="case-row"
            data-case={c.id}
            data-state={c.state}
            data-members={c.members.join(',')}
            data-members-n={c.members.length}
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
        ))}
      </div>
    </section>
  )
}
