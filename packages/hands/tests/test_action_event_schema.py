from pathlib import Path
import sys

SHARED_PATH = Path(__file__).resolve().parents[2] / "shared"
if str(SHARED_PATH) not in sys.path:
    sys.path.append(str(SHARED_PATH))

from action_event_schema import build_action_event, normalize_action_event, stable_hash


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
