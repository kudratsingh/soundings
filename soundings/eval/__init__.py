"""Evaluation: baseline policies (WO-050) and the eval harness (WO-051).

``soundings.eval.policies`` holds the shared ``Policy`` interface, the ``PolicyRun``
record and the counting rules every policy is scored by. Importing this package imports
nothing else, so ``policies`` never pulls in the runner or the model client.
"""
