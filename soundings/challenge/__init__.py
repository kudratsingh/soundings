"""Simulated acoustic challenge-response (WO-021). See ``soundings.challenge.hail``."""

from soundings.challenge.hail import (
    HONESTY_NOTE,
    HailConfig,
    HailResult,
    HailSimulator,
    KnownOperatorEntry,
    KnownOperatorTable,
    hail_evidence_rows,
    hail_latency_ticks,
    load_hail_config,
    load_known_operators,
)

__all__ = [
    "HONESTY_NOTE",
    "HailConfig",
    "HailResult",
    "HailSimulator",
    "KnownOperatorEntry",
    "KnownOperatorTable",
    "hail_evidence_rows",
    "hail_latency_ticks",
    "load_hail_config",
    "load_known_operators",
]
