import { useState } from 'react'
import { postApprove, postDecline } from '../api'
import type { ApprovalRequestPayload } from '../events'

interface Props {
  pending: ApprovalRequestPayload | null
  // --- WO-061 begin: the live approval path ---
  /** The run the request belongs to (the answer is posted to it). */
  runId?: string | null
  /** Sim time now, for the remaining time on the request. */
  tS?: number
  /** True when a person is on this run's approval path (the stepped source). */
  personApproves?: boolean
  // --- WO-061 end ---
}

/** The approval card's plain line when no request is open. */
export const NO_APPROVAL_LINE = 'a person approves here when the ladder asks'

// The approval slot at the bottom of the right rail. While an approval_request is pending it
// shows the card: what, scope, who, the time left, and (WO-061) Approve / Decline. The
// buttons only ask: the server mints a token for exactly the requested scope and the
// ladder's guard decides on the next tick. Nothing here can set a rung.
export default function ApprovalButton({ pending, runId = null, tS = 0, personApproves = false }: Props) {
  // --- WO-061 begin ---
  const [answered, setAnswered] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  // --- WO-061 end ---
  if (pending) {
    const scope = Object.entries(pending.scope)
      .map(([k, v]) => `${k} ${String(v)}`)
      .join(', ')
    // --- WO-061 begin ---
    const left = Math.max(0, Math.round(pending.expires_t_s - tS))
    const mine = answered === pending.request_id
    const answer = (fn: typeof postApprove) => {
      if (!runId) return
      setProblem(null)
      setAnswered(pending.request_id)
      fn(runId, pending.request_id, pending.approver).catch((e: unknown) => {
        setAnswered(null)
        setProblem(e instanceof Error ? e.message : String(e))
      })
    }
    // --- WO-061 end ---
    return (
      <div className="approval approval-card" data-testid="approval-card" role="note">
        <strong>
          Approval requested · {pending.contact_id} · {pending.action_label}
        </strong>
        <span className="label" data-testid="approval-scope">
          scope {scope || 'as requested'} · approver {pending.approver} · request {pending.request_id}
        </span>
        {/* --- WO-061 begin --- */}
        <span className="label num" data-testid="approval-countdown" data-left={left}>
          {left} s left (expires t {Math.round(pending.expires_t_s)} s)
        </span>
        {personApproves && runId ? (
          mine ? (
            <span className="approval-sent label" data-testid="approval-sent">
              answer sent · the ladder's guard checks it on the next tick
            </span>
          ) : (
            <span className="approval-actions">
              <button type="button" className="approve-btn" data-testid="approve" onClick={() => answer(postApprove)}>
                Approve
              </button>
              <button type="button" className="decline-btn" data-testid="decline" onClick={() => answer(postDecline)}>
                Decline
              </button>
            </span>
          )
        ) : (
          <span className="approval-placeholder" data-testid="approval-placeholder">
            simulated approver answers on this run
          </span>
        )}
        {problem && (
          <span className="approval-problem label" data-testid="approval-problem">
            {problem}
          </span>
        )}
        {/* --- WO-061 end --- */}
      </div>
    )
  }
  return (
    <div className="approval" role="note">
      <span className="label">no approval pending ·</span>
      <span className="approval-placeholder" data-testid="approval-placeholder">
        {NO_APPROVAL_LINE}
      </span>
    </div>
  )
}
