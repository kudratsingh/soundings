import { useEffect, useState } from 'react'
import type { ScenarioInfo } from '../api'

interface Props {
  scenarios: ScenarioInfo[]
  initial: { scenario: string; seed: string; speed: string }
  onStart(scenario: string, seed: number | null, speed: number | null): void
  /** A run is on screen: the button restarts it (design pass). */
  running?: boolean
  /** REH: the catalog run on screen; the fields follow it when a new run starts. */
  current?: { key: string; scenario: string; seed: string; speed: string } | null
}

const SPEEDS = ['1', '5', '20', '50', '100', '0']

export default function ScenarioPicker({ scenarios, initial, onStart, running = false, current = null }: Props) {
  const [scenario, setScenario] = useState(initial.scenario || 'S02')
  const [seed, setSeed] = useState(initial.seed)
  const [speed, setSpeed] = useState(initial.speed || '20')
  // --- REH begin ---
  useEffect(() => {
    if (!current) return
    setScenario(current.scenario)
    setSeed(current.seed)
    if (SPEEDS.includes(current.speed)) setSpeed(current.speed)
  }, [current?.key])
  // --- REH end ---
  return (
    <form
      className="picker"
      onSubmit={(e) => {
        e.preventDefault()
        onStart(scenario, seed === '' ? null : Number(seed), Number(speed))
      }}
    >
      <select aria-label="scenario" value={scenario} onChange={(e) => setScenario(e.target.value)}>
        {scenarios.map((s) => (
          <option key={s.id} value={s.id}>
            {s.id} · {s.name}
          </option>
        ))}
      </select>
      <input aria-label="seed" placeholder="seed" value={seed} onChange={(e) => setSeed(e.target.value.replace(/\D/g, ''))} size={3} />
      <select aria-label="speed" value={speed} onChange={(e) => setSpeed(e.target.value)}>
        {SPEEDS.map((s) => (
          <option key={s} value={s}>
            {s === '0' ? 'unpaced' : `${s}×`}
          </option>
        ))}
      </select>
      <button type="submit" data-testid="start-run">
        {running ? 'Reset' : 'Start'}
      </button>
    </form>
  )
}
