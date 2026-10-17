// The judge-injection panel (WO-061), collapsed by default and opened by the Judge tab or
// `?judge=1`. A judge places one contact by clicking the map, picks how it moves and how it
// sounds, and presses Go: the server writes a live scenario file and starts its run, and
// the screen follows it. The panel also shows the QR code to the registry form (file a plan
// from a phone) and the model-path badge. A judge describes a contact; nothing here names
// a rung or a state, and the ladder decides what happens (CLAUDE.md 2.2).

import { useEffect, useRef, useState } from 'react'
import { CircleMarker, Tooltip } from 'react-leaflet'
import type { AcousticProfile, InjectReply, InjectReq, JudgeBehavior, ModelModeInfo, QrInfo } from '../api'
import { ApiError, fetchModelMode, modelBadge, postInject, postPrepare } from '../api'
import '../judge.css'

export const JUDGE_LABELS = {
  tab: 'Judge',
  title: 'Judge panel: place one contact',
  close: 'Close',
  place: 'Place contact',
  placing: 'Click the map inside a security zone',
  move: 'Move the pin',
  behavior: 'How it moves',
  acoustic: 'How it sounds',
  go: 'Go',
  filed: 'Filed survey',
  unfiled: 'Unfiled, answers hail',
  closing: 'Closing on an asset',
  loitering: 'Loitering near an asset',
  silent: 'Filed, then silent',
  propeller: 'Propeller tonal',
  biologic: 'Biologic clicks',
  quiet: 'Quiet',
  behaviorDefault: 'Behavior default',
  outside: 'That point is outside the declared security zones. Click inside a zone.',
  started: 'Started',
  replay: 'Replay later with',
  qrTitle: 'File a plan from a phone',
  qrHint: 'Scan to open the registry form; the plan appears on the map.',
  loopback: 'A phone cannot reach localhost; open the screen on the LAN address.',
  helpTitle: 'What each choice becomes (fixed table)',
  helpFiled: 'Lawnmower over the clicked zone; a filed plan covers the zone and window; answers hail.',
  helpUnfiled: 'Lawnmower over the clicked zone; nothing filed; a known operator modem answers hail.',
  helpClosing: 'Transit to the nearest critical asset at 1.0 m/s; nothing filed; no reply.',
  helpLoitering: 'Orbit the nearest critical asset at 40 m; nothing filed; no reply.',
  helpSilent: 'Filed survey that goes silent and drifts from tick 60.',
  helpLadder: 'The contact enters at watch; the ladder and its guards decide every rung.',
  pin: 'judge pin',
} as const

const BEHAVIORS: JudgeBehavior[] = ['filed', 'unfiled', 'closing', 'loitering', 'silent']
const ACOUSTICS: AcousticProfile[] = ['propeller', 'biologic', 'quiet']
const HELP: Record<JudgeBehavior, string> = {
  filed: JUDGE_LABELS.helpFiled,
  unfiled: JUDGE_LABELS.helpUnfiled,
  closing: JUDGE_LABELS.helpClosing,
  loitering: JUDGE_LABELS.helpLoitering,
  silent: JUDGE_LABELS.helpSilent,
}

/** The pin on the map (passed through HarborMap's overlay). */
export function JudgePin({ pin }: { pin: [number, number] | null }) {
  if (!pin) return null
  return (
    <CircleMarker center={pin} radius={8} interactive={false} className="judge-pin" pathOptions={{ weight: 2, fillOpacity: 0.25, dashArray: '2 3' }}>
      <Tooltip permanent direction="top" offset={[0, -8]} className="judge-pin-label">
        {JUDGE_LABELS.pin}
      </Tooltip>
    </CircleMarker>
  )
}

interface Props {
  open: boolean
  onOpenChange(open: boolean): void
  placing: boolean
  onPlacing(placing: boolean): void
  pin: [number, number] | null
  onRun(runId: string): void
  qr: QrInfo | null
}

export default function JudgePanel({ open, onOpenChange, placing, onPlacing, pin, onRun, qr }: Props) {
  const [behavior, setBehavior] = useState<JudgeBehavior>('closing')
  const [acoustic, setAcoustic] = useState<AcousticProfile | 'default'>('default')
  const [error, setError] = useState<string | null>(null)
  const [started, setStarted] = useState<InjectReply | null>(null)
  const [model, setModel] = useState<ModelModeInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const prepared = useRef<string>('')

  useEffect(() => {
    if (!open) return
    fetchModelMode()
      .then(setModel)
      .catch(() => setModel(null))
  }, [open])

  const request = (): InjectReq | null =>
    pin ? { lat: Number(pin[0].toFixed(6)), lon: Number(pin[1].toFixed(6)), behavior, acoustic: acoustic === 'default' ? null : acoustic } : null

  // While the judge is choosing, the server builds that run in the background (Go then
  // starts it at once). A 422 here is the out-of-zone message; anything else waits for Go.
  useEffect(() => {
    const req = request()
    if (!open || !req) return
    const key = JSON.stringify(req)
    if (key === prepared.current) return
    prepared.current = key
    setError(null)
    postPrepare(req).catch((e: unknown) => {
      if (e instanceof ApiError && e.status === 422) setError(JUDGE_LABELS.outside)
    })
  }, [open, pin?.[0], pin?.[1], behavior, acoustic])

  const go = () => {
    const req = request()
    if (!req || busy) return
    setBusy(true)
    setError(null)
    postInject(req)
      .then((r) => {
        setStarted(r)
        prepared.current = ''
        onRun(r.run_id)
        onOpenChange(false)
      })
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 422) setError(JUDGE_LABELS.outside)
        else setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => setBusy(false))
  }

  if (!open) {
    return (
      <div className="judge-dock">
        {started && (
          <span className="judge-started label" data-testid="judge-started">
            {JUDGE_LABELS.started} {started.scenario_id} (seed {started.seed}) · {started.path}
          </span>
        )}
        <button type="button" className="judge-tab" data-testid="judge-open" onClick={() => onOpenChange(true)}>
          {JUDGE_LABELS.tab}
        </button>
      </div>
    )
  }

  return (
    <section className="judge-panel" data-testid="judge-panel" aria-label={JUDGE_LABELS.title}>
      <header className="judge-head">
        <strong>{JUDGE_LABELS.title}</strong>
        <span className="badge" data-testid="judge-model-badge">
          {model ? modelBadge(model) : 'model: replay'}
        </span>
        <button type="button" data-testid="judge-close" onClick={() => onOpenChange(false)}>
          {JUDGE_LABELS.close}
        </button>
      </header>
      <div className="judge-body">
        <div className="judge-controls">
          <button type="button" data-testid="judge-place" className={placing ? 'on' : ''} onClick={() => onPlacing(!placing)}>
            {pin ? JUDGE_LABELS.move : JUDGE_LABELS.place}
          </button>
          {placing && <span className="label judge-hint">{JUDGE_LABELS.placing}</span>}
          {pin && !placing && (
            <span className="label num" data-testid="judge-pin-at">
              {pin[0].toFixed(5)}, {pin[1].toFixed(5)}
            </span>
          )}
          {pin && (
            <>
              <fieldset className="judge-behaviors">
                <legend className="label">{JUDGE_LABELS.behavior}</legend>
                {BEHAVIORS.map((b) => (
                  <label key={b} className="judge-radio" title={HELP[b]}>
                    <input type="radio" name="judge-behavior" data-testid={`judge-behavior-${b}`} checked={behavior === b} onChange={() => setBehavior(b)} />
                    {JUDGE_LABELS[b]}
                  </label>
                ))}
              </fieldset>
              <label className="judge-acoustic label">
                {JUDGE_LABELS.acoustic}
                <select data-testid="judge-acoustic" value={acoustic} onChange={(e) => setAcoustic(e.target.value as AcousticProfile | 'default')}>
                  <option value="default">{JUDGE_LABELS.behaviorDefault}</option>
                  {ACOUSTICS.map((a) => (
                    <option key={a} value={a}>
                      {JUDGE_LABELS[a]}
                    </option>
                  ))}
                </select>
              </label>
              <button type="button" className="judge-go" data-testid="judge-go" onClick={go}>
                {busy ? '…' : JUDGE_LABELS.go}
              </button>
            </>
          )}
          {error && (
            <p className="judge-error" data-testid="judge-error" role="alert">
              {error}
            </p>
          )}
          {started && (
            <p className="label judge-started" data-testid="judge-started">
              {JUDGE_LABELS.started} {started.scenario_id} (seed {started.seed}) · {started.path}
            </p>
          )}
          <details className="judge-help">
            <summary className="label">{JUDGE_LABELS.helpTitle}</summary>
            <ul>
              {BEHAVIORS.map((b) => (
                <li key={b}>
                  <strong>{JUDGE_LABELS[b]}</strong>: {HELP[b]}
                </li>
              ))}
              <li>{JUDGE_LABELS.helpLadder}</li>
            </ul>
          </details>
        </div>
        <figure className="judge-qr">
          <img data-testid="judge-qr" src="/api/qr?target=/registry" alt={JUDGE_LABELS.qrTitle} width={116} height={116} />
          <figcaption>
            <strong className="label">{JUDGE_LABELS.qrTitle}</strong>
            <span className="label">{JUDGE_LABELS.qrHint}</span>
            <span className="judge-qr-url num" data-testid="judge-qr-url" data-url={qr?.url ?? ''}>
              {qr?.url ?? ''}
            </span>
            {qr?.loopback && <span className="judge-warn label">{JUDGE_LABELS.loopback}</span>}
          </figcaption>
        </figure>
      </div>
    </section>
  )
}
