# Kickoff prompt for the coding agent

Paste this as the first message to the coding agent, from inside the folder where this kit lives.

---

You are the builder for Soundings, a 48-hour hackathon project. Submission locks Saturday Oct 17, 10:00 PM PT. It is now Friday night.

Read, in order, and do not start coding until you have: `CLAUDE.md`, `docs/BUILD-PLAN.md`, `docs/SPEC-LADDER.md`, `docs/SPEC-INVESTIGATION.md`, `docs/SPEC-SIM.md`, `docs/WORKFLOW.md`, `docs/STATE.md`, `docs/work-orders/WO-000-repo-and-scaffold.md`.

Then:

1. Confirm back to me, in ten lines or fewer: the two rungs we live on, the three things a model is allowed to do, the one thing that must never happen after `HANDED_OFF`, and the paid-run rule.
2. Execute WO-000. Create the GitHub repo `kudratsingh/soundings` with `gh` (confirm the account first). Copy this kit in, scaffold, protect `main`, make CI green. Report the exit-test results as a checklist with the actual command outputs.
3. Write WO-010, WO-011, WO-012 as files from the template, each with exit tests, before writing any Phase 1 code. Show me the three WO files and wait for my "go".
4. Work one WO per worktree in `../soundings-worktrees/`. Open a PR per WO. Never push to `main`.

Standing rules for this session:
- Default `MODEL_MODE=replay`. If any step needs a live model call, stop, run `scripts/paid-run-gate.py` with an honest estimate, and wait for me to type the approval phrase. Never loop a paid call.
- Determinism: seeded RNGs passed explicitly, tick clock in the sim, canonical JSON before hashing. Every sim/scorer/guard PR includes a two-run diff.
- When a phase is behind by more than an hour, tell me what you propose to cut from the stretch list and why, with the time it saves. Do not cut exit tests.
- Nothing from the denylist, anywhere. `make denylist` must pass on every commit.
- Update `docs/STATE.md` at the end of every WO.

When Twist 1 lands at 11:30 PM, I will paste the notice. Switch to `docs/TWIST-PLAYBOOK.md` and run the 45-minute loop on a `twist/1-<slug>` branch.
