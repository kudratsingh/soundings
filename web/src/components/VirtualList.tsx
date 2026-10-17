// A small windowed list (WO-060 design pass): fixed row height, fixed viewport height, only
// the visible rows (plus a little overscan) are in the DOM. New items append at the bottom.
// When the reader sits at the bottom the list follows; when they have scrolled up, what they
// read stays put and a small 'N new' pill offers the jump. No dependency.

import type { ReactNode } from 'react'
import { useLayoutEffect, useRef, useState } from 'react'

interface Props<T> {
  items: T[]
  rowHeight: number
  height: number
  itemKey(item: T, index: number): string
  render(item: T, index: number): ReactNode
  /** Start at the bottom and follow new rows while the reader is there. */
  follow?: boolean
  className?: string
  testId?: string
  empty?: ReactNode
}

const OVERSCAN = 6

export default function VirtualList<T>({ items, rowHeight, height, itemKey, render, follow = true, className, testId, empty }: Props<T>) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [top, setTop] = useState(0)
  const atBottom = useRef(true)
  const seen = useRef(items.length)
  const [unseen, setUnseen] = useState(0)
  const total = items.length * rowHeight

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    if (follow && atBottom.current) {
      el.scrollTop = Math.max(0, total - height)
      setTop(el.scrollTop)
      seen.current = items.length
      setUnseen(0)
    } else if (items.length > seen.current) {
      setUnseen(items.length - seen.current)
    } else if (items.length < seen.current) {
      seen.current = items.length
      setUnseen(0)
    }
  }, [items.length, total, height, follow])

  const first = Math.max(0, Math.floor(top / rowHeight) - OVERSCAN)
  const last = Math.min(items.length, Math.ceil((top + height) / rowHeight) + OVERSCAN)
  const shown: ReactNode[] = []
  for (let i = first; i < last; i++) {
    shown.push(
      <div key={itemKey(items[i], i)} className="vrow" style={{ transform: `translateY(${i * rowHeight}px)`, height: rowHeight }}>
        {render(items[i], i)}
      </div>,
    )
  }
  return (
    <div className={`vlist ${className ?? ''}`} style={{ height }} data-testid={testId}>
      <div
        ref={ref}
        className="vlist-scroll"
        style={{ height }}
        onScroll={(e) => {
          const el = e.currentTarget
          setTop(el.scrollTop)
          atBottom.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 4
          if (atBottom.current) {
            seen.current = items.length
            setUnseen(0)
          }
        }}
      >
        <div className="vlist-inner" style={{ height: total }}>
          {shown}
        </div>
      </div>
      {items.length === 0 && empty}
      {unseen > 0 && (
        <button
          type="button"
          className="new-pill"
          data-testid="new-pill"
          onClick={() => {
            const el = ref.current
            if (el) el.scrollTop = el.scrollHeight
          }}
        >
          {unseen} new ↓
        </button>
      )}
    </div>
  )
}
