"""Registry of planned underwater work (WO-020).

Plans are filed by people (the ``/registry`` form or ``/api/plans``) or seeded from a
scenario file. Nothing here imports or is driven by a model (CLAUDE.md 2.2). The matcher
(WO-022) reads plans only as frozen ``PlanView`` objects.
"""

from soundings.registry.models import (
    IN_FORCE,
    ConflictConfig,
    ConflictView,
    PlanCreate,
    PlanRejected,
    PlanSource,
    PlanStatus,
    PlanView,
    RegistryConfig,
    RejectReason,
    datetime_to_tick,
    load_conflict_config,
    load_registry_config,
    scenario_epoch,
    tick_to_datetime,
)
from soundings.registry.slots import (  # WO-082
    BindingConstraint,
    PinReason,
    PlanShift,
    SlotConfig,
    SlotProposal,
    load_slot_config,
    propose_slots,
)
from soundings.registry.store import (
    DEFAULT_DB_PATH,
    ProposalNotAcceptable,  # WO-082
    ProposalStale,  # WO-082
    RegistryStore,
    db_path_from_env,
    plan_views_from_scenario,
    plans_from_scenario,
)

__all__ = [
    "DEFAULT_DB_PATH",
    # --- WO-082 begin ---
    "BindingConstraint",
    "PinReason",
    "PlanShift",
    "ProposalNotAcceptable",
    "ProposalStale",
    "SlotConfig",
    "SlotProposal",
    "load_slot_config",
    "propose_slots",
    # --- WO-082 end ---
    "IN_FORCE",
    "ConflictConfig",
    "ConflictView",
    "PlanCreate",
    "PlanRejected",
    "PlanSource",
    "PlanStatus",
    "PlanView",
    "RegistryConfig",
    "RegistryStore",
    "RejectReason",
    "datetime_to_tick",
    "db_path_from_env",
    "load_conflict_config",
    "load_registry_config",
    "plan_views_from_scenario",
    "plans_from_scenario",
    "scenario_epoch",
    "tick_to_datetime",
]
