import type { ApprovalRequestPayload } from '../events'

interface Props {
  pending: ApprovalRequestPayload | null
}

// Part A: the approval path is wired in WO-061. Until then the control is an inert label;
// the real button is rendered only while an approval_request is pending.
export default function ApprovalButton({ pending }: Props) {
  return (
    <div className="approval">
      <span className="approval-placeholder" data-testid="approval-placeholder" role="note">
        approval wired in WO-061
      </span>
      <span className="muted small">
        {pending ? `request ${pending.request_id}: ${pending.action_label}` : 'No approval pending'}
      </span>
    </div>
  )
}
