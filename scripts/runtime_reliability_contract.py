"""Stable contract constants for the cross-platform runtime reliability lane."""

from __future__ import annotations

RUNTIME_RELIABILITY_TESTS = (
    "tests/test_channels.py::test_run_manager_channel_turn_is_durable_and_isolated_from_primary_replay",
    "tests/test_channels.py::test_server_exposes_channel_ingest_route",
    "tests/test_channels.py::test_public_channel_webhook_allows_explicit_unsigned_channel",
    "tests/test_lan_scan_manager.py::test_manual_confirm_requires_exact_consent_and_cached_authority_without_writes[nonzero-cas]",
    "tests/test_full_agent_runtime.py::test_run_manager_heartbeat_renews_and_releases_its_run_lease",
    "tests/test_full_agent_runtime.py::test_cancelling_queued_run_finishes_publication_fence_without_worker",
    "tests/test_full_agent_runtime.py::test_approval_heartbeat_delayed_renewal_cannot_cancel_after_finalization",
    "tests/test_full_agent_runtime.py::test_approved_repair_scheduler_flow_binds_real_validation_and_review_receipts",
    "tests/test_full_agent_runtime.py::test_cross_manager_task_approval_waits_for_origin_lease_and_wakes_scheduler",
)

# Conservative successful-path ceilings for the complete sequential pytest
# invocation. These include each selected test's own waits plus its cleanup or
# finalizer boundaries, and intentionally overcount overlapping thread waits.
RUNTIME_RELIABILITY_TEST_SUCCESS_PATH_BUDGET_SECONDS = dict(
    zip(
        RUNTIME_RELIABILITY_TESTS,
        (
            35.0,  # channel poll 5 + terminal event 15 + finalizer 15
            16.0,  # channel poll 10 + shutdown attempts 5 + 1
            22.0,  # accepted response correlation + terminal API observation
            2.0,  # controlled manual controller execution + shutdown
            15.0,  # heartbeat renewal
            105.0,  # worker start 15 + durable 30 + publication 60
            30.0,  # execution completion 15 + heartbeat cleanup 15
            210.0,  # five handoffs 150 + terminal publication 60
            165.0,  # lease handoffs/results 75 + durable/publication 90
        ),
        strict=True,
    )
)
RUNTIME_RELIABILITY_SCHEDULING_RESERVE_SECONDS = 300.0
RUNTIME_RELIABILITY_REQUIRED_REPEATS = 20
RUNTIME_RELIABILITY_ITERATION_TIMEOUT_SECONDS = 900.0
RUNTIME_RELIABILITY_ISOLATION = "fresh_interpreter_and_basetemp_per_repeat"
