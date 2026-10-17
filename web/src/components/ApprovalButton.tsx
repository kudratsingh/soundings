import type { ApprovalRequestPayload } from '../events'

interface Props {
  pending: ApprovalRequestPayload | null
}

// The approval slot at the bottom of the right rail. The approval path is wired in WO-061:
// until then nothing here is clickable. While an approval_request is pending this shows the
// approval card (what, scope, who, until when; WO-061 mounts its Approve button in it);
// otherwise one quiet line.
export default function ApprovalButton({ pending }: Props) {
  if (pending) {
    const scope = Object.entries(pending.scope)
      .map(([k, v]) => `${k} ${String(v)}`)
      .join(', ')
    return (
      <div className="approval approval-card" data-testid="approval-card" role="note">
        <strong>
          Approval requested · {pending.contact_id} · {pending.action_label}
        </strong>
        <span className="label">
          scope {scope || 'as requested'} · approver {pending.approver} · expires t {Math.round(pending.expires_t_s)} s · request {pending.request_id}
        </span>
        <span className="approval-placeholder" data-testid="approval-placeholder">
          approval wired in WO-061
        </span>
      </div>
    )
  }
  return (
    <div className="approval" role="note">
      <span className="label">no approval pending ·</span>
      <span className="approval-placeholder" data-testid="approval-placeholder">
        approval wired in WO-061
      </span>
    </div>
  )
}
