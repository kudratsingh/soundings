// WO-061 QR addendum: a conflicted plan's slot proposal (WO-082 CP-SAT). The card shows
// the proposed shifts or "no feasible slot" with the binding constraint, and two buttons.
// Nothing changes until the operator accepts; declining changes only the proposal. The
// card never touches a run or a contact.

import { useEffect, useState } from 'react'
import type { PlanView, SlotProposal } from '../api'
import { acceptProposal, declineProposal, proposeSlot } from '../api'

export const PROPOSAL_LABELS = {
  heading: 'proposal: the operator accepts or declines',
  proposes: 'Soundings proposes',
  none: 'no feasible slot',
  accept: 'Accept',
  decline: 'Decline',
  conflict: 'conflicts with',
} as const

const hhmm = (iso: string) => iso.slice(11, 16)

interface Props {
  plans: PlanView[]
}

export default function ProposalCard({ plans }: Props) {
  const [closed, setClosed] = useState<Set<string>>(new Set())
  // The newest conflicted plan the operator has not answered yet (the one just filed).
  const target = plans
    .filter((p) => p.status === 'conflicted' && !closed.has(p.plan_id))
    .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')) || b.plan_id.localeCompare(a.plan_id))[0]
  const [proposal, setProposal] = useState<SlotProposal | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const planId = target?.plan_id ?? null

  useEffect(() => {
    setProposal(null)
    setProblem(null)
    if (!planId) return
    proposeSlot(planId)
      .then(setProposal)
      .catch((e: unknown) => setProblem(e instanceof Error ? e.message : String(e)))
  }, [planId])

  if (!target) return null
  const conflict = target.conflicts[0]
  const close = () => setClosed((s) => new Set([...s, target.plan_id]))
  return (
    <aside className="proposal-card" data-testid="proposal-card" aria-label={PROPOSAL_LABELS.heading}>
      <strong className="label">{PROPOSAL_LABELS.heading}</strong>
      <span className="proposal-conflict">
        {target.plan_id} {PROPOSAL_LABELS.conflict} {conflict ? `${conflict.other_plan_id} ${hhmm(conflict.overlap_start)}-${hhmm(conflict.overlap_end)}` : 'another plan'}
      </span>
      {proposal && proposal.status === 'feasible' && (
        <ul className="proposal-shifts" data-testid="proposal-shifts">
          {proposal.shifts
            .filter((s) => s.shift_min !== 0)
            .map((s) => (
              <li key={s.plan_id}>
                {PROPOSAL_LABELS.proposes}: {s.plan_id} start {hhmm(s.start_at)} → {hhmm(s.new_start_at)}
              </li>
            ))}
        </ul>
      )}
      {proposal && proposal.status === 'infeasible' && (
        <span className="proposal-none">
          {PROPOSAL_LABELS.none}
          {proposal.binding_constraint ? ` (${proposal.binding_constraint})` : ''} · {proposal.detail}
        </span>
      )}
      {problem && <span className="label">{problem}</span>}
      {proposal && (
        <span className="proposal-actions">
          {proposal.status === 'feasible' && (
            <button
              type="button"
              data-testid="proposal-accept"
              onClick={() =>
                acceptProposal(target.plan_id, proposal.proposal_id)
                  .then(close)
                  .catch((e: unknown) => setProblem(String(e)))
              }
            >
              {PROPOSAL_LABELS.accept}
            </button>
          )}
          <button
            type="button"
            data-testid="proposal-decline"
            onClick={() =>
              declineProposal(target.plan_id, proposal.proposal_id)
                .then(close)
                .catch((e: unknown) => setProblem(String(e)))
            }
          >
            {PROPOSAL_LABELS.decline}
          </button>
        </span>
      )}
    </aside>
  )
}
