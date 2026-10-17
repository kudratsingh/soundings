"""Seeded, tick-based simulator (docs/SPEC-SIM.md). Same seed, same inputs, same bytes.

Entry points: ``soundings.sim.run.run_scenario`` (a ``Scenario`` to a ``SimRun``),
``soundings.sim.scenario.resolve_scenario`` / ``catalog_paths``, and the CLI
``python -m soundings.sim``. ``SimRun.tracks`` is observed data; ground truth lives in
``contact_truth``, ``truth_paths`` and ``detections`` and is never read by the
investigation.
"""
