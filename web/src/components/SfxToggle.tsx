// The visible mute toggle (WO-062P). Presenter mode shows it in the caption bar; the normal
// screen shows a small corner button. Muting never changes a run, only what is heard.

interface Props {
  muted: boolean
  onToggle: () => void
  className?: string
}

export default function SfxToggle({ muted, onToggle, className }: Props) {
  return (
    <button
      type="button"
      className={className ?? 'sfx-toggle'}
      data-testid="sfx-toggle"
      data-muted={muted ? '1' : '0'}
      aria-pressed={muted}
      title={muted ? 'Sound off: click to unmute' : 'Sound on: click to mute'}
      onClick={(e) => {
        onToggle()
        e.currentTarget.blur()
      }}
    >
      {muted ? 'sound off' : 'sound on'}
    </button>
  )
}
