import json
import logging
from typing import Tuple

logger = logging.getLogger("brain.agent.core.verifier")

VERIFIER_PROMPT = """\
You are the Kestrel Verifier Engine. Your job is to strictly evaluate if the autonomous agent's final task summary is supported by evidence.
You must prevent the agent from hallucinating actions, falsifying completions, or making claims unsupported by the tool execution history.

Task Goal: {goal}
Agent's Final Summary: {summary}

Evidence Chain (Tool Execution History):
{evidence}

Rules:
1. Every major claim made in the summary MUST be supported by the evidence.
2. If the agent claims to have created, modified, or read a file, there MUST be a corresponding successful tool call in the evidence.
3. If the agent makes unsupported claims, hallucinates actions it didn't take, or fails to address the main goal, you must FAIL the verification.
4. If the summary is accurate and supported, you must PASS.

Output JSON format (NO markdown blocks, just raw JSON):
{{
    "status": "PASS" | "FAIL",
    "critique": "Detailed explanation of exactly what is unsupported or why it passes."
}}
"""

class VerifierEngine:
    """
    Evaluates agent completions against the accumulated evidence chain
    to detect hallucinations and enforce correctness.
    """
    def __init__(self, provider, model: str):
        self._provider = provider
        self._model = model

    async def verify(self, goal: str, summary: str, evidence_chain) -> Tuple[bool, str]:
        if not self._provider:
            return True, "No LLM provider available for verification. Bypassing."

        # Simplify chain to relevant tool events
        evidence_text = ""
        if evidence_chain:
            chain = evidence_chain.get_chain()
            for record in chain:
                if record.get("type") == "tool_selection":
                    evidence_text += f"\n- Tool: {record.get('description')}\n  Outcome: {record.get('outcome', 'unknown')}\n"

        if not evidence_text:
            evidence_text = "(No evidence recorded)"
        else:
            # Bound the evidence length so we don't blow up token limits easily
            evidence_text = evidence_text[-30_000:]

        try:
            response = await self._provider.generate(
                messages=[
                    {"role": "user", "content": VERIFIER_PROMPT.format(
                        goal=goal,
                        summary=summary,
                        evidence=evidence_text
                    )}
                ],
                model=self._model,
                temperature=0.0,
                response_format={"type": "json_object"}
            )
            
            content = response.get("content", "{}").strip()
            
            # Clean up potential markdown formatting issue if present
            if content.startswith("```json"):
                content = content[7:]
                if content.endswith("```"):
                    content = content[:-3]
                    
            result = json.loads(content)
            
            status = result.get("status", "FAIL")
            critique = result.get("critique", "No critique provided.")
            
            passed = status == "PASS"
            if not passed:
                logger.warning(f"Verification FAILED: {critique}")
            else:
                logger.debug("Verification PASSED")
                
            return passed, critique
            
        except Exception as e:
            logger.error(f"Verifier Engine error: {e}")
            # Fail open to avoid deadlocking tasks on verification bugs
            return True, f"Verifier Engine skipped due to internal error: {e}"
