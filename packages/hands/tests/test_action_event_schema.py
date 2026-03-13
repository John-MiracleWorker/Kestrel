from shared_schemas import (
    build_action_event,
    build_execution_action_event,
    classify_risk_class,
    classify_runtime_class,
    normalize_action_event,
    stable_hash,
)


def test_action_event_contains_reversible_state_references():
    event = build_action_event(
        source="hands.test",
        action_type="demo.execute",
        status="success",
        before_state={"screenshot_hash": stable_hash("before"), "command_hash": stable_hash("cmd")},
        after_state={"screenshot_hash": stable_hash("after"), "policy_decision": "success"},
    )

    normalized = normalize_action_event(event)
    assert normalized["schema_version"] == "action_event.v1"
    assert normalized["before_state"]["screenshot_hash"]
    assert normalized["before_state"]["command_hash"]
    assert normalized["after_state"]["policy_decision"] == "success"


def test_execution_action_event_carries_runtime_and_risk_classes():
    event = build_execution_action_event(
        source="hands.test",
        action_type="python_executor.run",
        status="success",
        runtime_class=classify_runtime_class("docker"),
        risk_class=classify_risk_class(action_type="python_executor"),
        before_state={"command_hash": stable_hash("cmd"), "policy_decision": "running"},
        after_state={"command_hash": stable_hash("cmd"), "policy_decision": "success"},
    )

    normalized = normalize_action_event(event)
    assert normalized["metadata"]["runtime_class"] == "sandboxed_docker"
    assert normalized["metadata"]["risk_class"] == "medium"
