"""Stable contract constants for the cross-platform runtime reliability lane."""

from __future__ import annotations

RUNTIME_RELIABILITY_TESTS = (
    "tests/test_channels.py::test_run_manager_channel_turn_is_durable_and_isolated_from_primary_replay",
    "tests/test_channels.py::test_server_exposes_channel_ingest_route",
    "tests/test_full_agent_runtime.py::test_run_manager_heartbeat_renews_and_releases_its_run_lease",
    "tests/test_full_agent_runtime.py::test_cancelling_queued_run_finishes_publication_fence_without_worker",
    "tests/test_full_agent_runtime.py::test_approval_heartbeat_delayed_renewal_cannot_cancel_after_finalization",
    "tests/test_full_agent_runtime.py::test_approved_repair_scheduler_flow_binds_real_validation_and_review_receipts",
    "tests/test_full_agent_runtime.py::test_cross_manager_task_approval_waits_for_origin_lease_and_wakes_scheduler",
)

RUNTIME_RELIABILITY_REQUIRED_REPEATS = 20
RUNTIME_RELIABILITY_ITERATION_TIMEOUT_SECONDS = 300.0
RUNTIME_RELIABILITY_ISOLATION = "fresh_interpreter_and_basetemp_per_repeat"
