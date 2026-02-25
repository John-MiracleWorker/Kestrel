import asyncio
import logging
from agent.core.verifier import VerifierEngine
from agent.evidence import EvidenceChain, DecisionType

# Configure basic logging to see verifier output
logging.basicConfig(level=logging.DEBUG)

class MockProvider:
    """A minimal mock provider to simulate LLM verification response."""
    def __init__(self, expected_status="PASS", expected_critique="Looks good"):
        self.expected_status = expected_status
        self.expected_critique = expected_critique

    async def generate(self, messages, model, temperature=0.0, response_format=None):
        import json
        return {
            "content": json.dumps({
                "status": self.expected_status,
                "critique": self.expected_critique
            })
        }

async def test_verifier():
    print("--- Testing Deterministic / Mock LLM Verifier Pass ---")
    provider = MockProvider("PASS", "The summary is fully supported by the host_write tool call.")
    verifier = VerifierEngine(provider=provider, model="gpt-4o-mini")
    
    # Mock evidence chain
    evidence_chain = EvidenceChain(task_id="test-task-1")
    evidence_chain.record_tool_decision(
        tool_name="host_write",
        args={"path": "test.txt", "content": "hello"},
        reasoning="Writing to file as instructed."
    )
    evidence_chain.record_outcome(evidence_chain._decisions[-1].id, "success")
    
    passed, critique = await verifier.verify(
        goal="Write a file called test.txt with text 'hello'",
        summary="I have created the file test.txt with the requested text.",
        evidence_chain=evidence_chain
    )
    
    assert passed is True
    print(f"Result: PASSED. Critique: {critique}")
    
    print("\n--- Testing Verifier Fail ---")
    provider = MockProvider("FAIL", "The agent claims to have run a build script, but there is no terminal or build tool execution in the evidence.")
    verifier = VerifierEngine(provider=provider, model="gpt-4o-mini")
    
    # Mock evidence chain with MISSING evidence
    evidence_chain = EvidenceChain(task_id="test-task-2")
    evidence_chain.record_tool_decision(
        tool_name="host_read",
        args={"path": "package.json"},
        reasoning="Reading dependencies"
    )
    
    passed, critique = await verifier.verify(
        goal="Build the project",
        summary="I have successfully built the project using npm run build.",
        evidence_chain=evidence_chain
    )
    
    assert passed is False
    print(f"Result: FAILED. Critique: {critique}")

if __name__ == "__main__":
    asyncio.run(test_verifier())
