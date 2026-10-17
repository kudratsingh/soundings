import type { Rung } from '../events'
import { RUNG_LABEL, RUNGS } from '../events'

interface Props {
  active: Rung
  livesOn: string[]
}

const STANDING_ORDERS: Rung[] = ['slow', 'hold']

export default function RungIndicator({ active, livesOn }: Props) {
  return (
    <ol className="rungs" aria-label="ladder rungs">
      {RUNGS.map((r) => {
        const isActive = r === active
        const lives = livesOn.includes(r)
        return (
          <li
            key={r}
            className={`rung${isActive ? ' active' : ''}${lives ? ' lives' : ''}`}
            data-testid={isActive ? 'rung-active' : 'rung'}
            data-rung={r}
          >
            <span className="rung-name">{RUNG_LABEL[r]}</span>
            {lives && <span className="rung-tag">we live here</span>}
            {STANDING_ORDERS.includes(r) && <span className="rung-tag muted">standing orders</span>}
          </li>
        )
      })}
    </ol>
  )
}
