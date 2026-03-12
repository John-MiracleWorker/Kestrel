import json
import logging
import re
from typing import Any, Tuple

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

    @staticmethod
    def _is_critical_claim(goal: str, summary: str) -> bool:
        text = f"{goal}\n{summary}".lower()
        return bool(
            re.search(
                r"\b(write|modified?|created?|deleted?|deploy(?:ed)?|install(?:ed)?|mcp|push(?:ed)?|commit(?:ted)?|sent|emailed?)\b",
                text,
            )
        )

    @staticmethod
    def _artifact_refs(action_receipts: list[dict[str, Any]] | None) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        for receipt in action_receipts or []:
            for artifact in receipt.get("artifact_manifest", []) or []:
                refs.append(
                    {
                        "artifact_id": str(artifact.get("artifact_id") or ""),
                        "uri": str(artifact.get("uri") or ""),
                        "name": str(artifact.get("name") or ""),
                    }
                )
        return refs

    async def verify_detailed(
        self,
        goal: str,
        summary: str,
        evidence_chain,
        *,
        action_receipts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        receipt_ids = [
            str(receipt.get("receipt_id") or "")
            for receipt in (action_receipts or [])
            if receipt.get("receipt_id")
        ]
        artifact_refs = self._artifact_refs(action_receipts)

        if self._is_critical_claim(goal, summary) and not receipt_ids:
            critique = "Mutating completion claims require at least one supporting action receipt."
            return {
                "passed": False,
                "critique": critique,
                "claims": [
                    {
                        "claim_text": summary,
                        "verdict": "fail",
                        "confidence": 1.0,
                        "rationale": critique,
                        "supporting_receipt_ids": [],
                        "artifact_refs": artifact_refs,
                    }
                ],
            }

        if not self._provider:
            critique = "No LLM provider available for verification. Bypassing."
            return {
                "passed": True,
                "critique": critique,
                "claims": [
                    {
                        "claim_text": summary,
                        "verdict": "pass",
                        "confidence": 0.25,
                        "rationale": critique,
                        "supporting_receipt_ids": receipt_ids,
                        "artifact_refs": artifact_refs,
                    }
                ],
            }

        evidence_text = ""
        if evidence_chain:
            chain = evidence_chain.get_chain()
            for record in chain:
                if record.get("type") == "tool_selection":
                    evidence_text += (
                        f"\n- Tool: {record.get('description')}\n"
                        f"  Outcome: {record.get('outcome', 'unknown')}\n"
                    )

        if action_receipts:
            evidence_text += "\nAction Receipts:"
            for receipt in action_receipts[-10:]:
                evidence_text += (
                    f"\n- Receipt {receipt.get('receipt_id', '')}: "
                    f"{receipt.get('runtime_class', '')}/{receipt.get('risk_class', '')} "
                    f"failure={receipt.get('failure_class', '')} "
                    f"artifacts={len(receipt.get('artifact_manifest', []) or [])}"
                )

        if not evidence_text:
            evidence_text = "(No evidence recorded)"
        else:
            evidence_text = evidence_text[-30_000:]

        try:
            response = await self._provider.generate(
                messages=[
                    {
                        "role": "user",
                        "content": VERIFIER_PROMPT.format(
                            goal=goal,
                            summary=summary,
                            evidence=evidence_text,
                        ),
                    }
                ],
                model=self._model,
                temperature=0.0,
                response_format={"type": "json_object"},
            )

            content = response.get("content", "{}").strip()
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

            return {
                "passed": passed,
                "critique": critique,
                "claims": [
                    {
                        "claim_text": summary,
                        "verdict": "pass" if passed else "fail",
                        "confidence": 0.9 if passed else 0.15,
                        "rationale": critique,
                        "supporting_receipt_ids": receipt_ids,
                        "artifact_refs": artifact_refs,
                    }
                ],
            }

        except Exception as exc:
            logger.error(f"Verifier Engine error: {exc}")
            if self._is_critical_claim(goal, summary):
                critique = (
                    "Verifier Engine failed during a critical side-effect claim. "
                    f"Completion is blocked until verification succeeds: {exc}"
                )
                return {
                    "passed": False,
                    "critique": critique,
                    "claims": [
                        {
                            "claim_text": summary,
                            "verdict": "fail",
                            "confidence": 1.0,
                            "rationale": critique,
                            "supporting_receipt_ids": receipt_ids,
                            "artifact_refs": artifact_refs,
                        }
                    ],
                }
            critique = f"Verifier Engine skipped due to internal error: {exc}"
            return {
                "passed": True,
                "critique": critique,
                "claims": [
                    {
                        "claim_text": summary,
                        "verdict": "pass",
                        "confidence": 0.1,
                        "rationale": critique,
                        "supporting_receipt_ids": receipt_ids,
                        "artifact_refs": artifact_refs,
                    }
                ],
            }

    async def verify(self, goal: str, summary: str, evidence_chain) -> Tuple[bool, str]:
        result = await self.verify_detailed(goal, summary, evidence_chain)
        return bool(result.get("passed")), str(result.get("critique", ""))
