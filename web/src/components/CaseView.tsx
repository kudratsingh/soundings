// CASE VIEW (WO-060 design pass): the selected case as one scrollable column of steps in
// chronological order: DETECTED, WATCH, ASK, INVESTIGATE, RECOMMENDATION, LADDER, HANDOFF,
// AFTER. Each step card has a title, its tick and sim time, one plain sentence, the rows the
// sentence rests on, and a collapsed evidence section. Steps not reached yet are dim
// placeholders. At the bottom, two collapsed drawers: every row of this case (virtualized,
// grouped by tick, filter chips) and every transition of every case (for debugging).
// Display only: nothing here can set a rung.

import { memo, useMemo, useState } from 'react'
import type { CaseItem, CaseStep, CaseSummary, ReacquiredMark } from '../cases'
import { fmtValue } from '../cases'
import type { EvidenceKind, EvidenceRow, LadderState } from '../events'
import { RUNG_LABEL } from '../events'
import type { TimelineEntry } from '../useRun'
import VirtualList from './VirtualList'

const KIND_LABEL: Record<EvidenceKind, string> = { fact: 'fact', assumption: 'assumption', derived: 'derived' }

export const EvidenceLine = memo(function EvidenceLine({ row, highlight }: { row: EvidenceRow; highlight?: boolean }) {
  const conf = typeof row.confidence === 'number' ? row.confidence.toFixed(2) : row.confidence
  const tip = `${KIND_LABEL[row.kind]} · confidence ${conf} · probe ${row.probe_id ?? 'none'} · row ${row.row_id}${row.assumes ? ` · assumes: ${row.assumes}` : ''}`
  return (
    <div
      className={`ev ${row.kind}${highlight ? ' hl' : ''}`}
      id={`row-${row.row_id}`}
      data-testid="ledger-row"
      data-kind={row.kind}
      title={tip}
    >
      <i className={`prov ${row.kind}`} aria-label={KIND_LABEL[row.kind]} />
      <span className="ev-feat">{row.feature.replaceAll('_', ' ')}</span>
      <span className="ev-val num">{fmtValue(row)}</span>
      <span className="ev-src">{row.source}</span>
    </div>
  )
})

const when = (tick: number | null, tickS: number) => (tick === null ? '' : `t ${Math.round(tick * tickS)} s · tick ${tick}`)

function Reacquired({ m, tickS }: { m: ReacquiredMark; tickS: number }) {
  return (
    <article className="step reacq" data-testid="step" data-step="reacquired" data-contact={m.contact_id}>
      <header className="step-head">
        <i className="step-dot assumption" />
        <h3>Re-acquired as {m.contact_id}</h3>
        <span className="when num">{when(m.tick, tickS)}</span>
      </header>
      <p className="sentence">
        Continues {m.from} (assumption{m.note ? `: ${m.note}` : ''}).
      </p>
    </article>
  )
}

interface StepProps {
  step: CaseStep
  tickS: number
  open: boolean
  highlight: string | null
  onToggle(key: string): void
  onOpenSitrep(): void
}

const StepCard = memo(function StepCard({ step, tickS, open, highlight, onToggle, onOpenSitrep }: StepProps) {
  const reached = step.status === 'done' || step.status === 'current'
  return (
    <article
      className={`step ${step.status}`}
      data-testid="step"
      data-step={step.kind}
      data-status={step.status}
      data-contact={step.contact_id}
    >
      <header className="step-head">
        <i className={`step-dot ${step.status}`} />
        <h3>{step.title}</h3>
        {step.status === 'current' && <span className="now-tag">now</span>}
        <span className="when num">{reached ? when(step.tick, tickS) : step.status === 'skipped' ? 'skipped' : 'next'}</span>
      </header>
      <p className="sentence">{step.sentence}</p>
      {reached && step.kind === 'ask' && step.hail && (
        <p className={`hail-result ${step.hail.startsWith('hail answered') ? 'benign' : 'unknown'}`} data-testid="hail-result">
          {step.hail}
        </p>
      )}
      {reached && step.kind === 'investigate' && (
        <div className="invest">
          {step.probes && step.probes.length > 0 && (
            <ul className="probes">
              {step.probes.slice(-6).map((p) => (
                <li key={`${p.probe_id}@${p.tick}`} className="probe-card" data-testid="probe-card" data-source={p.source ?? ''}>
                  <strong>{p.probe_id.replaceAll('_', ' ')}</strong> <span className="label num">tick {p.tick}</span>
                  <span className="label"> · {p.why} · {p.rows.length} rows</span>
                </li>
              ))}
            </ul>
          )}
          {step.posterior ? (
            <div className="posterior" data-testid="posterior" data-threat-mass={step.posterior.threat_mass}>
              {Object.entries(step.posterior.probs)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 2)
                .map(([h, p]) => (
                  <div key={h} className="num">
                    {h.replaceAll('_', ' ')} {(p * 100).toFixed(1)}% <span className="label">{step.posterior?.label}</span>
                  </div>
                ))}
              <div className="num label" data-testid="posterior-mass">
                threat mass {step.posterior.threat_mass.toFixed(2)} · benign mass {step.posterior.benign_mass.toFixed(2)} · {step.posterior.label}
              </div>
              <div className="mass" aria-label="threat and benign mass">
                <span className="threat" style={{ transform: `scaleX(${step.posterior.threat_mass})` }} />
                <span className="benign" style={{ transform: `scaleX(${step.posterior.benign_mass})` }} />
              </div>
            </div>
          ) : (
            <p className="label" data-testid="posterior-placeholder">
              Hypotheses (stress-test estimate) appear when the first probe lands.
            </p>
          )}
        </div>
      )}
      {/* --- WO-060B begin: one line per ladder action --- */}
      {reached && step.kind === 'ladder' && step.ladderLines && step.ladderLines.length > 0 && (
        <ul className="ladder-lines">
          {step.ladderLines.map((l, i) => (
            <li key={`${l.tick}-${i}`} className={`ladder-line tone-${l.tone}`} data-testid="ladder-line">
              <span className="label num">t {Math.round(l.tick * tickS)} s</span> {l.text}
            </li>
          ))}
        </ul>
      )}
      {/* --- WO-060B end --- */}
      {reached && step.kind === 'ladder' && step.approval && (
        <p className="label">
          Approval {step.approval.request.request_id}: {step.approval.request.action_label} ·{' '}
          {step.approval.result ? step.approval.result.outcome.replace('_', ' ') : `expires t ${Math.round(step.approval.request.expires_t_s)} s`}
        </p>
      )}
      {reached && (step.kind === 'handoff' || step.kind === 'after') && step.sitrep && (
        <button type="button" className="link" onClick={onOpenSitrep}>
          {step.sitrep.kind === 'lost_vehicle' ? 'Open the notice' : `Open SITREP v${step.sitrep.version}${step.sitrep.status ? ` ${step.sitrep.status}` : ''}`} ({step.sitrep.writer})
        </button>
      )}
      {reached && step.slots.length > 0 && (
        <div className="slots">
          {step.slots.map((s) =>
            s.row ? (
              <EvidenceLine key={s.label} row={s.row} highlight={s.row.row_id === highlight} />
            ) : (
              <div key={s.label} className="ev empty">
                <i className="prov none" />
                <span className="ev-feat">{s.label}</span>
                <span className="ev-val">—</span>
              </div>
            ),
          )}
        </div>
      )}
      {reached && step.rowCount > 0 && (
        <>
          <button type="button" className="ev-toggle" aria-expanded={open} onClick={() => onToggle(step.key)}>
            {open ? '▾' : '▸'} evidence · <span className="num">{step.rowCount}</span> row{step.rowCount === 1 ? '' : 's'}
            {step.groups.length < step.rowCount ? ` in ${step.groups.length} features` : ''}
          </button>
          {open && (
            <div className="ev-list">
              {step.groups.map((g) => (
                <div key={g.row.feature} className="ev-group">
                  <EvidenceLine row={g.row} highlight={g.row.row_id === highlight} />
                  {g.n > 1 && <span className="ev-n num">×{g.n}</span>}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </article>
  )
})

type Filter = 'all' | EvidenceKind | 'probes'
type DrawerItem = { kind: 'tick'; tick: number; n: number } | { kind: 'row'; row: EvidenceRow }

function EvidenceDrawer({ rows, highlight }: { rows: EvidenceRow[]; highlight: string | null }) {
  const [filter, setFilter] = useState<Filter>('all')
  const items = useMemo(() => {
    const kept = rows.filter((r) => (filter === 'all' ? true : filter === 'probes' ? r.probe_id !== null : r.kind === filter))
    const out: DrawerItem[] = []
    let i = 0
    while (i < kept.length) {
      const t = kept[i].t
      let j = i
      while (j < kept.length && kept[j].t === t) j++
      out.push({ kind: 'tick', tick: t, n: j - i })
      for (let k = i; k < j; k++) out.push({ kind: 'row', row: kept[k] })
      i = j
    }
    return out
  }, [rows, filter])
  return (
    <div className="ledger" aria-label="evidence for this case">
      <div className="filters" role="group" aria-label="filter rows">
        {(['all', 'fact', 'assumption', 'derived', 'probes'] as Filter[]).map((f) => (
          <button key={f} type="button" className={`chip-btn ${f}${filter === f ? ' on' : ''}`} aria-pressed={filter === f} onClick={() => setFilter(f)}>
            {f}
          </button>
        ))}
      </div>
      <VirtualList
        items={items}
        rowHeight={24}
        height={312}
        testId="evidence-drawer-list"
        itemKey={(it) => (it.kind === 'tick' ? `t${it.tick}` : it.row.row_id)}
        render={(it) =>
          it.kind === 'tick' ? (
            <div className="tick-head num">
              tick {it.tick} · {it.n} row{it.n === 1 ? '' : 's'}
            </div>
          ) : (
            <EvidenceLine row={it.row} highlight={it.row.row_id === highlight} />
          )
        }
        empty={<p className="label empty">No rows match.</p>}
      />
    </div>
  )
}

function TransitionsDrawer({ entries, caseIds }: { entries: TimelineEntry[]; caseIds: Set<string> }) {
  return (
    <VirtualList
      items={entries}
      rowHeight={24}
      height={240}
      testId="transitions-drawer-list"
      itemKey={(t) => String(t.seq)}
      render={(t) => (
        <div className={`tr-row${caseIds.has(t.contact_id) ? ' mine' : ''}`} data-testid="transition-row">
          <span className="num label">tick {t.tick}</span>
          <span>{t.case_id}</span>
          <span className="label">{t.from_state} →</span>
          <strong>{t.to_state}</strong>
          <span className="label">{t.reason_code}</span>
        </div>
      )}
    />
  )
}

interface Props {
  summary: CaseSummary | undefined
  items: CaseItem[]
  rows: EvidenceRow[]
  timeline: TimelineEntry[]
  tickS: number
  highlight: string | null
  flash: number | undefined
  onOpenSitrep(): void
}

/** The state path of the case's current id, for the stepper (kept as data-testid=state-timeline). */
function statePath(timeline: TimelineEntry[], cid: string): { state: LadderState; tick: number }[] {
  const out: { state: LadderState; tick: number }[] = []
  for (const t of timeline) {
    if (t.contact_id !== cid) continue
    if (out.length === 0) out.push({ state: t.from_state, tick: t.tick })
    out.push({ state: t.to_state, tick: t.tick })
  }
  return out
}

export default function CaseView({ summary, items, rows, timeline, tickS, highlight, flash, onOpenSitrep }: Props) {
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [evOpen, setEvOpen] = useState(false)
  const [trOpen, setTrOpen] = useState(false)
  const toggle = (key: string) => setOpen((o) => ({ ...o, [key]: !o[key] }))
  const path = useMemo(() => (summary ? statePath(timeline, summary.latest) : []), [timeline, summary?.latest])
  const members = useMemo(() => new Set(summary?.members ?? []), [summary?.members])
  return (
    <section className="panel case-panel" aria-label="case view">
      <header className="case-head" data-testid="contact-card">
        {flash !== undefined && <span key={flash} className="flash" />}
        <div className="case-title">
          <h2>{summary ? `Case ${summary.id}` : 'No case selected'}</h2>
          {summary && (
            <>
              <span className={`pill tone-${summary.tone}`} data-testid="state-badge">
                {summary.state.replaceAll('_', ' ')}
              </span>
              <span className="label">rung {RUNG_LABEL[summary.rung]}</span>
              {summary.members.length > 1 && <span className="label">ids {summary.members.join(' → ')}</span>}
            </>
          )}
        </div>
        <ol className="stepper" data-testid="state-timeline" aria-label={`state path of ${summary?.latest ?? 'no contact'}`}>
          {path.length === 0 && <li className="label">no transitions yet</li>}
          {path.map((p, i) => (
            <li key={`${p.state}-${i}`} className={i === path.length - 1 ? 'cur' : ''} title={`tick ${p.tick}`}>
              {p.state}
            </li>
          ))}
        </ol>
      </header>
      <div className="case-scroll" data-testid="case-view" data-case={summary?.id ?? ''}>
        {!summary && <p className="label empty">Steps appear here once a sensor hears a contact.</p>}
        {summary && (
          <div className="steps">
            {items.map((it) =>
              it.kind === 'reacquired' ? (
                <Reacquired key={it.key} m={it} tickS={tickS} />
              ) : (
                <StepCard
                  key={it.key}
                  step={it}
                  tickS={tickS}
                  open={Boolean(open[it.key])}
                  highlight={highlight}
                  onToggle={toggle}
                  onOpenSitrep={onOpenSitrep}
                />
              ),
            )}
          </div>
        )}
        <div className="drawer-block">
          <button type="button" className="drawer-toggle" aria-expanded={evOpen} data-testid="evidence-drawer" onClick={() => setEvOpen((o) => !o)}>
            {evOpen ? '▾' : '▸'} All evidence for this case <span className="label num">{rows.length} rows</span>
          </button>
          {evOpen && <EvidenceDrawer rows={rows} highlight={highlight} />}
        </div>
        <div className="drawer-block">
          <button type="button" className="drawer-toggle" aria-expanded={trOpen} data-testid="transitions-drawer" onClick={() => setTrOpen((o) => !o)}>
            {trOpen ? '▾' : '▸'} All transitions (every case) <span className="label num">{timeline.length}</span>
          </button>
          {trOpen && <TransitionsDrawer entries={timeline} caseIds={members} />}
        </div>
      </div>
    </section>
  )
}

