"""The ladder: watch, ask, slow, hold, hand to a person (SPEC-LADDER).

A hand-rolled state machine (``machine.LadderMachine``) over a transition table that is
data (``table.TRANSITIONS``), with a checkpoint per transition and an idempotency key per
action. Guards plug in through ``guard_port.GuardPort``; nothing here calls a model.
"""
