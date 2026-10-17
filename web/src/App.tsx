// The watch-officer screen (WO-060): one screen, no routing. WO-061 and WO-062 each add one
// marked block here; nothing else changes. Nothing on this screen can set a rung.

import { useEffect, useMemo, useState } from 'react'
import type { AboutInfo, BoundaryInfo, HarborGeoJson, ScenarioInfo } from './api'
import { fetchAbout, fetchBoundary, fetchHarbor, fetchScenarios, startRun } from './api'
import AboutPanel from './components/AboutPanel'
import ApprovalButton from './components/ApprovalButton'
import ContactCard from './components/ContactCard'
import ContactList from './components/ContactList'
import EvidenceLedger from './components/EvidenceLedger'
import HarborMap from './components/HarborMap'
import RungIndicator from './components/RungIndicator'
import ScenarioPicker from './components/ScenarioPicker'
import SitrepDrawer from './components/SitrepDrawer'
import StateTimeline from './components/StateTimeline'
import type { Rung } from './events'
import { autoSelect, useRun } from './useRun'
// --- WO-062P begin ---
import PresenterMode from './components/PresenterMode'
// --- WO-062P end ---

export const TAGLINE = 'Resolve every unknown underwater contact at the gentlest rung that works.'

const RECENT_TICKS = 30

function useParams() {
  return useMemo(() => {
    const p = new URLSearchParams(window.location.search)
    return {
      scenario: p.get('scenario') ?? '',
      seed: p.get('seed') ?? '',
      speed: p.get('speed') ?? '',
      tilesOn: p.get('tiles') !== 'off',
    }
  }, [])
}

function App() {
  const params = useParams()
  const [scenarios, setScenarios] = useState<ScenarioInfo[]>([])
  const [about, setAbout] = useState<AboutInfo | null>(null)
  const [boundary, setBoundary] = useState<BoundaryInfo | null>(null)
  const [harbor, setHarbor] = useState<HarborGeoJson | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [picked, setPicked] = useState<string | null>(null)
  const [highlight, setHighlight] = useState<string | null>(null)
  const [aboutOpen, setAboutOpen] = useState(false)
  const [sitrepOpen, setSitrepOpen] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const run = useRun(runId)

  const begin = (scenario: string, seed: number | null, speed: number | null) => {
    setPicked(null)
    startRun(scenario, seed, speed)
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
    if (params.scenario) {
      begin(params.scenario, params.seed === '' ? null : Number(params.seed), params.speed === '' ? null : Number(params.speed))
    }
  }, [])

  const selected = picked ?? autoSelect(run)
  const visible = Object.values(run.contacts).filter((c) => run.tick - c.lastTick <= RECENT_TICKS || c.contact_id === selected)
  const caseIds = Object.keys(run.rungOf).sort()
  const activeRung: Rung = selected && run.rungOf[selected] ? run.rungOf[selected].rung : 'watch'
  const selectedState = selected ? run.stateOf[selected] : undefined
  const colors = about?.colors
  const styleVars = colors
    ? ({ '--fact': colors.fact, '--assumption': colors.assumption, '--derived': colors.derived } as React.CSSProperties)
    : undefined

  return (
    <div className="app" style={styleVars}>
      <header className="top">
        <div className="title">
          <strong>Soundings</strong>
          <span className="muted small">{TAGLINE}</span>
        </div>
        <div className="status">
          <span data-testid="scenario-label">
            {run.started ? `${run.started.scenario_id} · ${run.started.scenario_name} · seed ${run.started.seed}` : 'no run'}
          </span>
          <span className="clock">
            t = {run.t_s.toFixed(0)} s · tick {run.tick}
          </span>
          <span className="badge">model: replay</span>
          <span className="badge state" data-testid="state-badge">
            {selected ? `${selected}: ${selectedState ?? 'DETECTED'}` : 'no contact'}
          </span>
          {run.finished && <span className="badge">run finished</span>}
          <button type="button" data-testid="sitrep-open" onClick={() => setSitrepOpen((o) => !o)}>
            SITREP{run.sitreps.length ? ` (${run.sitreps.length})` : ''}
          </button>
          <button type="button" data-testid="about-open" onClick={() => setAboutOpen(true)}>
            About
          </button>
        </div>
        <ScenarioPicker scenarios={scenarios} initial={params} onStart={begin} />
      </header>
      {problem && <div className="problem">{problem}</div>}
      <main className="body">
        <HarborMap
          harbor={harbor}
          about={about}
          tilesOn={params.tilesOn}
          contacts={visible}
          selected={selected}
          onSelect={(id) => setPicked(id)}
        />
        <div className="side">
          <RungIndicator active={activeRung} livesOn={boundary?.lives_on ?? ['ask', 'hand_to_person']} />
          <ContactList ids={caseIds} stateOf={run.stateOf} rungOf={run.rungOf} selected={selected} onSelect={setPicked} auto={picked === null} />
          <ContactCard
            contact={selected ? (run.contacts[selected] ?? null) : null}
            state={selectedState}
            rung={selected ? run.rungOf[selected] : undefined}
            rows={selected ? (run.rowsByContact[selected] ?? []) : []}
            posterior={selected ? run.posteriorOf[selected] : undefined}
          />
          <ApprovalButton pending={run.pendingApproval} />
          <EvidenceLedger rows={selected ? (run.rowsByContact[selected] ?? []) : []} highlight={highlight} />
          <StateTimeline entries={run.timeline} selected={selected} />
        </div>
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
      <AboutPanel open={aboutOpen} about={about} boundary={boundary} onClose={() => setAboutOpen(false)} />
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
