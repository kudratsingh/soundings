// Presenter mode (WO-062P), mounted by one marked block in App.tsx. `/?presenter=1` adds a
// caption bar (bottom, two lines, 44 px) with a step counter, a reset control and the mute
// toggle, driven by the shot list at GET /api/demo-script. Space or ArrowRight advances,
// ArrowLeft goes back. Each step starts its scenario through POST /api/runs at the step's
// speed (or shows its named action), highlights its spotlight, and waits for a click when
// pause_for_click is set. Without the parameter it only follows the run for sound (muted
// by default) and shows a small mute toggle. Presenting never sets a rung.

import { useCallback, useEffect, useRef, useState } from 'react'
import type { PresenterLogKind, PresenterScript, PresenterStep } from '../presenter'
import { END_CARD, LADDER_CHIPS, fetchDemoScript, findSpotlight, isPresenter, logPresenter, startStepRun } from '../presenter'
import { TWIST_SOUND, loadMuted, playSound, saveMuted, useSfx } from '../sfx'
import type { RunState } from '../useRun'
import SfxToggle from './SfxToggle'
import '../presenter.css'

interface Props {
  run: RunState
  onRun: (runId: string) => void
}

interface Nav {
  index: number
  kind: PresenterLogKind
  nonce: number
}

const SPOTLIGHT_CLASS = 'presenter-spotlight'

function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el) return false
  return el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA' || el.isContentEditable
}

// --- WO-061 begin: the judge-panel presenter actions (App.tsx listens for these) ---
export const JUDGE_ACTIONS = ['file_plan_qr', 'judge_drops_rogue'] as const

function judgeActions(step: PresenterStep): void {
  const named = [step.action, ...(step.beats ?? [])]
  for (const action of JUDGE_ACTIONS)
    if (named.includes(action)) window.dispatchEvent(new CustomEvent('soundings:presenter-action', { detail: { action } }))
}
// --- WO-061 end ---

/** Start what a step starts; returns the run id, or null when the step starts no run. */
async function applyStep(step: PresenterStep, previousRunRef: string | null): Promise<string | null> {
  for (const note of step.pending) console.info(`presenter: step ${step.index + 1}: ${note}`)
  const speed = step.speed_factor
  if (step.kind === 'catalog' && step.scenario) {
    if (step.continue_run && previousRunRef === step.scenario) return null
    return (await startStepRun(step.scenario, step.seed ?? null, speed)).run_id
  }
  if ((step.kind === 'twist' || step.action === 'twist_toggles') && step.run_ref) {
    try {
      return (await startStepRun(step.run_ref, step.seed ?? null, speed, step.toggles ?? [])).run_id
    } catch (e) {
      // runner v1 takes no toggles yet (WO-060 part B): keep the current run, sound the twist
      console.info(`presenter: step ${step.index + 1}: twist toggles pending (${String(e)})`)
      return null
    }
  }
  return null
}

function PresenterOverlay({ run, onRun, muted, onToggleMute }: Props & { muted: boolean; onToggleMute: () => void }) {
  const [script, setScript] = useState<PresenterScript | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [nav, setNav] = useState<Nav>({ index: 0, kind: 'step', nonce: 0 })
  const [paused, setPaused] = useState(false)
  const [stepRunId, setStepRunId] = useState<string | null>(null)
  const [applied, setApplied] = useState<number | null>(null)
  const generation = useRef(0)
  const runRef = useRef<string | null>(null)
  const mutedRef = useRef(muted)
  useEffect(() => {
    mutedRef.current = muted
  }, [muted])

  useEffect(() => {
    fetchDemoScript().then(setScript).catch((e: unknown) => setProblem(String(e)))
  }, [])

  const steps = script?.steps ?? []
  const step: PresenterStep | undefined = steps[nav.index]

  // Land on a step: log it, start its scenario or action, then mark it applied.
  useEffect(() => {
    if (!step) return
    const gen = ++generation.current
    setApplied(null)
    setPaused(Boolean(step.pause_for_click))
    logPresenter(step.index, nav.kind)
    judgeActions(step) // WO-061
    applyStep(step, runRef.current)
      .then((runId) => {
        if (gen !== generation.current) return
        if (runId) {
          runRef.current = step.run_ref
          setStepRunId(runId)
          onRun(runId)
        } else if (step.action === 'twist_toggles') {
          playSound(TWIST_SOUND, mutedRef.current)
        }
        setApplied(step.index)
      })
      .catch((e: unknown) => {
        if (gen === generation.current) setProblem(String(e))
      })
  }, [script, nav.nonce])

  // Highlight the spotlight element (it may render a moment after the step lands).
  useEffect(() => {
    const name = step?.spotlight
    if (!name) return
    let el: HTMLElement | null = null
    let tries = 0
    const timer = window.setInterval(() => {
      el = findSpotlight(name)
      tries += 1
      if (el || tries > 30) {
        el?.classList.add(SPOTLIGHT_CLASS)
        window.clearInterval(timer)
      }
    }, 100)
    return () => {
      window.clearInterval(timer)
      el?.classList.remove(SPOTLIGHT_CLASS)
    }
  }, [step?.spotlight, nav.nonce])

  const go = useCallback(
    (index: number, kind: PresenterLogKind = 'step') => {
      if (!steps.length) return
      const clamped = Math.max(0, Math.min(steps.length - 1, index))
      setNav((n) => (clamped === n.index && kind === 'step' ? n : { index: clamped, kind, nonce: n.nonce + 1 }))
    },
    [steps.length],
  )

  const reset = useCallback(() => go(0, 'reset'), [go])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target)) return
      if (e.key === ' ' || e.code === 'Space' || e.key === 'ArrowRight') {
        e.preventDefault()
        if (paused) return
        go(nav.index + 1)
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault()
        go(nav.index - 1)
      }
    }
    const onKeyUp = (e: KeyboardEvent) => {
      if (!isTyping(e.target) && (e.key === ' ' || e.code === 'Space')) e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('keyup', onKeyUp)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('keyup', onKeyUp)
    }
  }, [go, nav.index, paused])

  // A click anywhere outside the bar resumes a pause_for_click step.
  useEffect(() => {
    if (!paused) return
    const onDown = (e: PointerEvent) => {
      if ((e.target as HTMLElement | null)?.closest('[data-presenter-control]')) return
      setPaused(false)
      logPresenter(nav.index, 'resume')
    }
    window.addEventListener('pointerdown', onDown)
    return () => window.removeEventListener('pointerdown', onDown)
  }, [paused, nav.index])

  const following = run.runId === stepRunId && run.started !== null
  const total = steps.length

  return (
    <>
      {step?.action === 'ladder_chips' && (
        <div className="presenter-chips" data-testid="presenter-chips" aria-label="the ladder">
          {LADDER_CHIPS.map((name, i) => (
            <span key={name} className="presenter-chip" style={{ animationDelay: `${0.4 + i * 0.7}s` }}>
              {name}
            </span>
          ))}
        </div>
      )}
      {step?.action === 'end_card' && (
        <div className="presenter-endcard" data-testid="presenter-endcard">
          <strong>{END_CARD.title}</strong>
          <span>{END_CARD.line}</span>
          <span className="presenter-endcard-repo">{END_CARD.repo}</span>
        </div>
      )}
      <div
        className="presenter-bar"
        data-testid="presenter-bar"
        data-step={nav.index}
        data-applied={applied === null ? '' : String(applied)}
        data-run-id={stepRunId ?? ''}
        data-following={following ? '1' : '0'}
      >
        <p className="presenter-caption" data-testid="presenter-caption">
          {problem ? `presenter: ${problem}` : (step?.caption ?? (script ? '' : 'loading the shot list…'))}
        </p>
        {/* --- WO-088 begin --- */}
        {!problem && step?.subcaption && (
          <p className="presenter-subcaption" data-testid="presenter-subcaption">
            {step.subcaption}
          </p>
        )}
        {/* --- WO-088 end --- */}
        <div className="presenter-controls" data-presenter-control>
          <span className="presenter-counter" data-testid="presenter-counter">
            {total ? `${nav.index + 1} / ${total}` : '– / –'}
          </span>
          {paused && (
            <span className="presenter-hint" data-testid="presenter-pause-hint">
              click to continue
            </span>
          )}
          <SfxToggle muted={muted} onToggle={onToggleMute} className="presenter-button" />
          <button
            type="button"
            className="presenter-button"
            data-testid="presenter-reset"
            onClick={(e) => {
              reset()
              e.currentTarget.blur()
            }}
          >
            reset to step 0
          </button>
        </div>
      </div>
    </>
  )
}

export default function PresenterMode({ run, onRun }: Props) {
  const [presenter] = useState(() => isPresenter())
  const [muted, setMuted] = useState(() => loadMuted(presenter))
  useSfx(run, muted)
  const toggle = () =>
    setMuted((m) => {
      saveMuted(presenter, !m)
      return !m
    })
  if (!presenter) return <SfxToggle muted={muted} onToggle={toggle} />
  return <PresenterOverlay run={run} onRun={onRun} muted={muted} onToggleMute={toggle} />
}
