from agent.evidence import EvidenceChain


def test_record_tool_decision_with_action_event_references():
    chain = EvidenceChain(task_id="11111111-1111-1111-1111-111111111111")
    decision = chain.record_tool_decision(
        tool_name="computer_use",
        args={
            "action_event": {
                "source": "brain.computer_use",
                "action": "click_at",
                "status": "success",
                "before": {"command_hash": "abc", "policy_decision": "auto_approved"},
                "after": {"command_hash": "abc", "policy_decision": "executed"},
            }
        },
        reasoning="Need GUI interaction",
    )

    evidence_types = [item.evidence_type.value for item in decision.evidence]
    assert "action_event" in evidence_types
