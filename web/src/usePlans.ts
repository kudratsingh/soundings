// WO-061 QR addendum: the registry's in-force plans, polled every second. Polling (not the
// run WebSocket) because a plan filed from a phone arrives in wall-clock time; putting it in
// the seeded run stream would break byte-identical replays (CLAUDE.md 2.4).

import { useEffect, useState } from 'react'
import type { PlanView } from './api'
import { fetchActivePlans } from './api'

export const PLAN_POLL_MS = 1000

export function usePlans(): PlanView[] {
  const [plans, setPlans] = useState<PlanView[]>([])
  useEffect(() => {
    let alive = true
    let last = ''
    const poll = () => {
      fetchActivePlans()
        .then((p) => {
          const key = JSON.stringify(p)
          if (alive && key !== last) {
            last = key
            setPlans(p)
          }
        })
        .catch(() => undefined) // the registry may be unavailable; keep the last plans
    }
    poll()
    const timer = window.setInterval(poll, PLAN_POLL_MS)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])
  return plans
}
