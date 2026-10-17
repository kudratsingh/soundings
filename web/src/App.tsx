// The watch-officer screen (WO-060): one screen, no routing. WO-061 and WO-062 each add one
// marked block here; nothing else changes. Nothing on this screen can set a rung.
//
// Design pass B1 (2026-10-17): a case-centric command center. A case is one contact lineage
// (re-acquired ids merged). The top bar holds one line; under it the NOW line for the
// selected case and the last five KEY MOMENTS across cases; the map fills the left; the right
// rail holds the CASES list, the CASE VIEW (one column of steps) and the ladder.

import { useEffect, useMemo, useRef, useState } from 'react'
import type { AboutInfo, BoundaryInfo, HarborGeoJson, ModelModeInfo, ScenarioInfo } from './api'
import { fetchAbout, fetchBoundary, fetchHarbor, fetchModelMode, fetchScenarios, modelBadge, startRun } from './api'
import type { CaseSummary, Tone } from './cases'
import { caseItems, caseOf, mostSevere, nowLine, summarize } from './cases'
import AboutPanel from './components/AboutPanel'
import ApprovalButton from './components/ApprovalButton'
import CaseList from './components/CaseList'
import CaseView from './components/CaseView'
import { KeyMoments, NowLine } from './components/GlobalStrip'
import type { MapStyle } from './components/HarborMap'
import HarborMap from './components/HarborMap'
import RungIndicator from './components/RungIndicator'
import ScenarioPicker from './components/ScenarioPicker'
import SitrepDrawer from './components/SitrepDrawer'
import type { EvidenceRow, Rung } from './events'
import { RUNG_LABEL } from './events'
import { useRun } from './useRun'
// --- WO-062P begin ---
import PresenterMode from './components/PresenterMode'
// --- WO-062P end ---
// --- WO-089 begin ---
import type { PhysicsInfo } from './api'
import { fetchPhysics } from './api'
// --- WO-089 end ---
// --- WO-088 begin ---
import ResponderLayer from './components/ResponderLayer'
// --- WO-088 end ---
// --- WO-086 begin ---
import RungLedger from './components/RungLedger'
// --- WO-086 end ---

export const TAGLINE = 'Resolve every unknown underwater contact at the gentlest rung that works.'

const RECENT_TICKS = 30
/** A key moment flashes its case's card border while it is this fresh (ticks). */
const FLASH_TICKS = 3

// --- WO-088 begin ---
/** Where the contact the responder is looking at was last seen (null when not seen). */
function responderTarget(run: ReturnType<typeof useRun>): [number, number] | null {
  const cid = run.responder?.target_contact_id
  const c = cid ? run.contacts[cid] : undefined
  return c ? [c.last.lat, c.last.lon] : null
}
// --- WO-088 end ---

/** A counter that moves only when some element of `list` changes identity (copy-on-write). */
function useIdentityVersion(list: readonly unknown[]): number {
  const prev = useRef<readonly unknown[]>([])
  const version = useRef(0)
  if (list.length !== prev.current.length || list.some((x, i) => x !== prev.current[i])) {
    prev.current = list
    version.current += 1
  }
  return version.current
}

function useParams() {
  return useMemo(() => {
    const p = new URLSearchParams(window.location.search)
    return {
      scenario: p.get('scenario') ?? '',
      seed: p.get('seed') ?? '',
      speed: p.get('speed') ?? '',
      tilesOn: p.get('tiles') !== 'off',
      mapStyle: (p.get('style') === 'chart' ? 'chart' : 'radar') as MapStyle,
      // --- WO-060B begin: run-start toggles (?toggles=network_down,approver_unreachable) ---
      toggles: (p.get('toggles') ?? '').split(',').filter(Boolean),
      // --- WO-060B end ---
    }
  }, [])
}

/** The boundary-table line for the active rung: who decides what there. */
function boundaryReason(boundary: BoundaryInfo | null, rung: Rung): string {
  if (!boundary) return ''
  const rows = boundary.rows.filter((r) => r.rung === rung)
  if (!rows.length) return `${RUNG_LABEL[rung]}: nothing is decided here`
  const byCell = new Map<string, string[]>()
  for (const r of rows) byCell.set(r.cell, [...(byCell.get(r.cell) ?? []), r.action.replaceAll('_', ' ')])
  const parts = [...byCell].map(([cell, actions]) => `${cell.replaceAll('_', ' ')}: ${actions.join(', ')}`)
  return `${RUNG_LABEL[rung]} · ${parts.join(' · ')}`
}

function App() {
  const params = useParams()
  const [scenarios, setScenarios] = useState<ScenarioInfo[]>([])
  const [about, setAbout] = useState<AboutInfo | null>(null)
  const [boundary, setBoundary] = useState<BoundaryInfo | null>(null)
  const [harbor, setHarbor] = useState<HarborGeoJson | null>(null)
  const [model, setModel] = useState<ModelModeInfo | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [picked, setPicked] = useState<string | null>(null)
  const [highlight, setHighlight] = useState<string | null>(null)
  const [aboutOpen, setAboutOpen] = useState(false)
  const [sitrepOpen, setSitrepOpen] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const run = useRun(runId)
  // --- WO-089 begin: physics cards; rings fall back to base-noise ranges before a run ---
  const [physics, setPhysics] = useState<PhysicsInfo | null>(null)
  useEffect(() => {
    fetchPhysics().then(setPhysics).catch((e: unknown) => setProblem(String(e)))
  }, [])
  const ranges = Object.keys(run.sensorRanges).length
    ? run.sensorRanges
    : Object.fromEntries((physics?.nodes ?? []).map((n) => [n.node_id, n]))
  // --- WO-089 end ---

  const begin = (scenario: string, seed: number | null, speed: number | null) => {
    setPicked(null)
    setHighlight(null)
    startRun(scenario, seed, speed, params.toggles) // WO-060B: toggles from the URL
      .then((r) => {
        setRunId(r.run_id)
        const q = new URLSearchParams(window.location.search)
        q.set('scenario', scenario)
        if (seed !== null) q.set('seed', String(seed))
        if (speed !== null) q.set('speed', String(speed))
        window.history.replaceState(null, '', `?${q.toString()}`)
      })
      .catch((e: unknown) => setProblem(String(e)))
  }

  useEffect(() => {
    fetchScenarios().then(setScenarios).catch((e: unknown) => setProblem(String(e)))
    fetchAbout().then(setAbout).catch((e: unknown) => setProblem(String(e)))
    fetchBoundary().then(setBoundary).catch((e: unknown) => setProblem(String(e)))
    fetchHarbor().then(setHarbor).catch((e: unknown) => setProblem(String(e)))
    fetchModelMode()
      .then(setModel)
      .catch(() => setModel(null)) // the badge falls back to "model: replay"
    // --- WO-060B begin: the badge follows the model path as calls happen (never overclaims) ---
    const poll = window.setInterval(() => {
      fetchModelMode()
        .then(setModel)
        .catch(() => setModel(null))
    }, 5000)
    // --- WO-060B end ---
    if (params.scenario) {
      begin(params.scenario, params.seed === '' ? null : Number(params.seed), params.speed === '' ? null : Number(params.speed))
    }
    return () => window.clearInterval(poll) // WO-060B
  }, [])

  const tickS = run.started?.tick_s ?? 2
  const cases = useMemo(() => summarize(run.cases, run.rungOf), [run.cases, run.rungOf])
  const selectedCase = picked !== null ? caseOf(run.cases, picked) : mostSevere(cases)
  const summary: CaseSummary | undefined = cases.find((c) => c.id === selectedCase)
  const memberKey = summary?.members.join(',') ?? ''
  const memberTracks = summary ? summary.members.map((m) => run.cases.tracks[m]) : []
  // The member tracks are copy-on-write: the case view recomputes only when its own case moved.
  const tracksVersion = useIdentityVersion(memberTracks)
  const caseRows = useMemo(() => {
    const out: EvidenceRow[] = []
    for (const t of memberTracks) if (t) out.push(...t.rows)
    return out.sort((a, b) => a.t - b.t)
  }, [memberKey, tracksVersion])
  const items = useMemo(() => {
    if (!summary) return []
    const index = new Map(caseRows.map((r) => [r.row_id, r] as const))
    return caseItems(run.cases, summary.members, run.posteriorOf, index)
  }, [memberKey, caseRows, run.posteriorOf, summary?.state])
  const now = nowLine(summary, items, tickS)

  // Map: case colour per contact id, the selected lineage, and labels.
  const { toneOf, caseLabel } = useMemo(() => {
    const tone: Record<string, Tone> = {}
    const label: Record<string, string> = {}
    for (const c of cases)
      for (const m of c.members) {
        tone[m] = c.tone
        label[m] = c.id
      }
    return { toneOf: tone, caseLabel: label }
  }, [cases])
  const selectedIds = useMemo(() => new Set(summary?.members ?? []), [memberKey])
  const visible = Object.values(run.contacts).filter((c) => run.tick - c.lastTick <= RECENT_TICKS || selectedIds.has(c.contact_id))

  // Key moments that are fresh flash their case's border.
  const flashOf = useMemo(() => {
    const out: Record<string, number> = {}
    for (const m of run.cases.moments) if (run.tick - m.tick <= FLASH_TICKS) out[caseOf(run.cases, m.contact_id)] = m.seq
    return out
  }, [run.cases, run.tick])

  const activeRung: Rung = summary?.rung ?? 'watch'
  const speed = run.started ? run.started.speed : null

  return (
    <div className={`app style-${params.mapStyle}`}>
      <header className="topbar">
        <strong className="mark" title={TAGLINE}>
          Soundings
        </strong>
        <span className="scn" data-testid="scenario-label">
          {run.started ? `${run.started.scenario_id} · ${run.started.scenario_name} · seed ${run.started.seed}` : 'no run'}
        </span>
        {speed !== null && <span className="tb-item num">{speed === 0 ? 'unpaced' : `${speed}×`}</span>}
        <span className="clock num" data-testid="clock">
          t {run.t_s.toFixed(0)} s · tick {run.tick}
        </span>
        <span className="badge" data-testid="model-badge">
          {model ? modelBadge(model) : 'model: replay'}
        </span>
        {run.finished && <span className="badge">run finished</span>}
        <span className="spacer" />
        <ScenarioPicker scenarios={scenarios} initial={params} onStart={begin} running={run.started !== null} />
        <button type="button" data-testid="sitrep-open" onClick={() => setSitrepOpen((o) => !o)}>
          SITREP{run.sitreps.length ? ` (${run.sitreps.length})` : ''}
        </button>
        <button type="button" data-testid="about-open" onClick={() => setAboutOpen(true)}>
          About
        </button>
      </header>
      <div className="strip">
        <NowLine text={now} tone={summary?.tone ?? 'unknown'} />
        <KeyMoments moments={run.cases.moments} caseOf={(cid) => caseOf(run.cases, cid)} tickS={tickS} onSelect={setPicked} />
        <span className="rows-total label num" data-testid="rows-total" data-rows={run.cases.totalRows}>
          {run.cases.totalRows.toLocaleString('en-US')} evidence rows · {cases.length} case{cases.length === 1 ? '' : 's'}
        </span>
      </div>
      {problem && <div className="problem">{problem}</div>}
      <main className="body">
        <HarborMap
          harbor={harbor}
          about={about}
          tilesOn={params.tilesOn}
          contacts={visible}
          selected={summary?.latest ?? null}
          onSelect={(id) => setPicked(id)}
          ranges={ranges} // WO-089
          toneOf={toneOf}
          selectedIds={selectedIds}
          caseLabel={caseLabel}
          mapStyle={params.mapStyle}
          tickS={tickS}
          // --- WO-088 begin ---
          overlay={<ResponderLayer responder={run.responder} target={responderTarget(run)} />}
          // --- WO-088 end ---
        />
        <aside className="rail">
          <CaseList cases={cases} selected={selectedCase} auto={picked === null} tick={run.tick} tickS={tickS} flashOf={flashOf} onSelect={setPicked} />
          <CaseView
            summary={summary}
            items={items}
            rows={caseRows}
            timeline={run.timeline}
            tickS={tickS}
            highlight={highlight}
            flash={summary ? flashOf[summary.id] : undefined}
            onOpenSitrep={() => setSitrepOpen(true)}
          />
          <section className="ladder-rail" aria-label="ladder">
            {/* --- WO-086 begin: rung ledger and the blunt-policy counter (display only) --- */}
            <RungLedger ledger={run.ledger} />
            {/* --- WO-086 end --- */}
            <RungIndicator active={activeRung} livesOn={boundary?.lives_on ?? ['ask', 'hand_to_person']} />
            <p className="boundary-reason label" data-testid="boundary-reason">
              {boundaryReason(boundary, activeRung)}
            </p>
            <ApprovalButton pending={run.pendingApproval} />
          </section>
        </aside>
      </main>
      <SitrepDrawer
        open={sitrepOpen}
        sitreps={run.sitreps}
        onClose={() => setSitrepOpen(false)}
        onCite={(cid, rowId) => {
          setPicked(cid)
          setHighlight(rowId)
        }}
      />
      <AboutPanel open={aboutOpen} about={about} boundary={boundary} onClose={() => setAboutOpen(false)} physics={physics} /* WO-089 */ />
      {/* --- WO-061 block (judge panel) --- */}
      {/* --- end WO-061 block --- */}
      {/* --- WO-062 block (demo runner) --- */}
      {/* --- end WO-062 block --- */}
      {/* --- WO-062P begin --- */}
      <PresenterMode run={run} onRun={setRunId} />
      {/* --- WO-062P end --- */}
    </div>
  )
}

export default App
