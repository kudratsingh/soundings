import type { LadderState } from '../events'
import type { RungView } from '../useRun'

interface Props {
  ids: string[]
  stateOf: Record<string, LadderState>
  rungOf: Record<string, RungView>
  selected: string | null
  onSelect(id: string | null): void
  auto: boolean
}

export default function ContactList({ ids, stateOf, rungOf, selected, onSelect, auto }: Props) {
  return (
    <div className="contact-list">
      <button type="button" className={`pill${auto ? ' on' : ''}`} onClick={() => onSelect(null)}>
        auto
      </button>
      {ids.map((id) => (
        <button
          type="button"
          key={id}
          className={`pill${id === selected ? ' on' : ''}`}
          onClick={() => onSelect(id)}
          title={stateOf[id] ?? 'DETECTED'}
        >
          {id} <span className="muted small">{rungOf[id]?.rung.replaceAll('_', ' ') ?? 'watch'}</span>
        </button>
      ))}
    </div>
  )
}
