"""Runner v2 CLI (WO-033): ``python -m soundings.runner_v2 --scenario S05 --seed 11``.

Prints the nested canonical JSONL transition log of ``ScenarioRunnerV2`` (ladder
transitions with the investigation transitions nested), then ``transition_log_hash=`` and
``nested_log_hash=``. Same scenario and seed, same bytes. WO-022's
``python -m soundings.runner`` stays the v1 CLI.
"""

from __future__ import annotations

import sys

from soundings.runner import main_v2

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main_v2())
