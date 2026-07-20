from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import nested_memvid_agent.private_artifacts as private_artifacts_module
from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.layers import (
    LayeredMemorySystem,
    _load_or_create_memory_integrity_key,
)
from nested_memvid_agent.models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.nested_learning import (
    LearningSignal,
    NestedLearningKernel,
    ValidationEvidence,
    resolve_validation_evidence,
)
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.repair_integrity import write_repair_artifact
from nested_memvid_agent.retention import RetentionCompactor
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution
from nested_memvid_agent.self_profile import (
    SELF_PROFILE_SCHEMA,
    TRUSTED_ONBOARDING_LOCATOR,
    TRUSTED_ONBOARDING_ORIGIN,
    TRUSTED_ONBOARDING_SOURCE,
)
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools


def test_default_sink_rejects_direct_stable_put_and_upsert(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", InMemoryBackend)
    record = MemoryRecord(
        id="direct-stable",
        title="Direct stable write",
        content="A caller cannot write this semantic record directly.",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.9,
    )

    with pytest.raises(ValueError, match="Direct semantic memory writes are rejected"):
        memory.put(record)
    with pytest.raises(ValueError, match="Direct semantic memory upserts are rejected"):
        memory.upsert(record)

    assert list(memory.iter_records(MemoryLayer.SEMANTIC)) == []


@pytest.mark.parametrize("structured", [False, True])
def test_kernel_rejects_raw_scores_and_fabricated_evidence_despite_repeat_claim(
    structured: bool,
) -> None:
    evidence = None
    if structured:
        fake_refs = tuple(
            EvidenceRef(source="memory_record", locator=f"fake-receipt-{index}")
            for index in range(5)
        )
        evidence = ValidationEvidence(
            test_refs=(fake_refs[0],),
            lint_refs=(fake_refs[1],),
            repair_refs=(fake_refs[2],),
            review_refs=(fake_refs[3],),
            task_refs=fake_refs,
            human_explicit=True,
            validation_status="operator_approved",
        )
    signal = LearningSignal(
        title="Fabricated repeated procedure",
        content="A claimed repeat count and plausible locators are not validation.",
        kind=MemoryKind.PROCEDURE,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=None if structured else 1.0,
        validation_evidence=evidence,
        repeat_count=999,
        requested_target_layer=MemoryLayer.PROCEDURAL,
    )

    decision = NestedLearningKernel().decide(signal)

    assert decision.accepted is False
    requirements = decision.to_payload()["promotion_requirements"]
    assert requirements["claimed_repeat_count"] == 999
    assert requirements["observed_repeat_count"] == 0


def test_sink_rejects_serialized_resolved_status_without_runtime_capability(
    tmp_path: Path,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    title = "Serialized status forgery"
    content = "Serialized resolved fields alone must not authorize a stable write."
    candidate_id = _put_claim_candidate(memory, title=title, content=content)
    receipt_id = _put_runtime_receipt(memory, candidate_id)
    evidence = _resolved_evidence((receipt_id,))
    signal, record = _stable_record(
        evidence=evidence,
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        title=title,
        content=content,
    )
    record.evidence.append(EvidenceRef(source="memory_record", locator=candidate_id))
    assert NestedLearningKernel().decide(signal).accepted

    with pytest.raises(ValueError, match="runtime validation capability"):
        memory.put_validated(
            record,
            authority="nested_learning",
            source_record_ids=(candidate_id, receipt_id),
        )

    assert list(memory.iter_records(MemoryLayer.SEMANTIC)) == []


def test_sink_rejects_resolution_and_source_envelope_mismatch(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    title = "Mismatched procedure"
    content = "A source envelope cannot substitute an unrelated receipt."
    candidate_id = _put_claim_candidate(
        memory,
        title=title,
        content=content,
        kind=MemoryKind.PROCEDURE,
    )
    receipt_a = _put_runtime_receipt(memory, candidate_id, index=1)
    receipt_b = _put_runtime_receipt(memory, candidate_id, index=2)
    unrelated = _put_runtime_receipt(memory, candidate_id, index=3)
    evidence = _resolved_evidence((receipt_a, receipt_b))
    _, record = _stable_record(
        evidence=evidence,
        layer=MemoryLayer.PROCEDURAL,
        kind=MemoryKind.PROCEDURE,
        title=title,
        content=content,
    )
    record.evidence.append(EvidenceRef(source="memory_record", locator=candidate_id))

    with pytest.raises(ValueError, match="not_bound"):
        memory.put_validated(
            record,
            authority="nested_learning",
            source_record_ids=(candidate_id, receipt_a, unrelated),
            validation_evidence=evidence,
        )

    assert list(memory.iter_records(MemoryLayer.PROCEDURAL)) == []


def test_resolved_distinct_receipts_can_promote_and_conflicts_stay_episodic(
    tmp_path: Path,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    first_candidate = _put_claim_candidate(
        memory,
        title="Feature alpha",
        content="Feature alpha is enabled.",
    )
    first_receipt = _put_runtime_receipt(memory, first_candidate)
    first_evidence = _resolved_evidence((first_receipt,))
    _, first = _stable_record(
        evidence=first_evidence,
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        title="Feature alpha",
        content="Feature alpha is enabled.",
    )
    first.evidence.append(EvidenceRef(source="memory_record", locator=first_candidate))
    first_id = memory.put_validated(
        first,
        authority="nested_learning",
        source_record_ids=(first_candidate, first_receipt),
        validation_evidence=first_evidence,
    )

    second_candidate = _put_claim_candidate(
        memory,
        title="Feature alpha",
        content="Feature alpha is not enabled.",
    )
    second_receipt = _put_runtime_receipt(memory, second_candidate, index=2)
    second_evidence = _resolved_evidence((second_receipt,))
    _, second = _stable_record(
        evidence=second_evidence,
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        title="Feature alpha",
        content="Feature alpha is not enabled.",
    )
    second.evidence.append(EvidenceRef(source="memory_record", locator=second_candidate))
    second_id = memory.put_validated(
        second,
        authority="nested_learning",
        source_record_ids=(second_candidate, second_receipt),
        validation_evidence=second_evidence,
    )

    semantic = list(memory.iter_records(MemoryLayer.SEMANTIC))
    conflict_audits = [
        record
        for record in memory.iter_records(MemoryLayer.EPISODIC)
        if record.metadata.get("frame_type") == "conflict_set"
    ]
    assert {record.id for record in semantic} == {first_id, second_id}
    assert all(record.metadata.get("stable_write_envelope") for record in semantic)
    assert conflict_audits
    assert set(conflict_audits[0].metadata["source_record_ids"]) == {
        first_id,
        second_id,
    }


def test_trusted_onboarding_receipt_is_required_for_self_memory(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    arguments = {
        "title": "Kestrel onboarding profile",
        "content": '{"agent_name":"Kestrel","user_name":"Taylor"}',
        "schema": SELF_PROFILE_SCHEMA,
        "validation_status": "user_confirmed",
        "confidence": 0.92,
        "source": TRUSTED_ONBOARDING_SOURCE,
        "locator": TRUSTED_ONBOARDING_LOCATOR,
    }

    untrusted = registry.execute(
        ToolCall(name="self.remember", arguments=arguments),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    trusted = registry.execute(
        ToolCall(name="self.remember", arguments=arguments),
        ToolContext(
            memory=memory,
            config=AgentConfig(),
            workspace=tmp_path,
            run_id="onboarding-run",
            trusted_request_origin=TRUSTED_ONBOARDING_ORIGIN,
        ),
    )

    assert untrusted.success is False
    assert trusted.success is True
    stable = list(memory.iter_records(MemoryLayer.SELF))
    receipts = [
        record
        for record in memory.iter_records(MemoryLayer.EPISODIC)
        if record.metadata.get("authenticated_onboarding_receipt") is True
    ]
    assert len(stable) == 1
    assert len(receipts) == 1
    source_ids = stable[0].metadata["stable_write_envelope"]["source_record_ids"]
    assert receipts[0].id in source_ids
    assert len(source_ids) == 2
    candidate = memory.get_record(MemoryLayer.EPISODIC, source_ids[0])
    assert candidate is not None
    assert candidate.title == stable[0].title
    assert candidate.content == stable[0].content


def test_stable_import_is_staged_untrusted_and_authority_fields_are_stripped(
    tmp_path: Path,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    arguments = {
        "records": [
            {
                "id": "imported-stable-forgery",
                "title": "Imported stable forgery",
                "content": "Imported stable content must remain untrusted staging.",
                "layer": "semantic",
                "kind": "fact",
                "confidence": 0.99,
                "metadata": {
                    "validation_status": "operator_approved",
                    "stable_write_envelope": {"authority": "nested_learning"},
                    "nested_learning": {"decision": {"accepted": True}},
                },
            }
        ]
    }
    call = ToolCall(name="memory.import", arguments=arguments, id="approved-import")

    result = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_memory_import=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: arguments},
        ),
    )

    assert result.success
    assert result.data["stable_import_status"] == "untrusted_episodic_staging"
    assert list(memory.iter_records(MemoryLayer.SEMANTIC)) == []
    staged = list(memory.iter_records(MemoryLayer.EPISODIC))
    assert len(staged) == 1
    assert staged[0].metadata["import_requested_layer"] == "semantic"
    assert staged[0].metadata["stable_recall_eligible"] is False
    assert "stable_write_envelope" not in staged[0].metadata
    assert "nested_learning" not in staged[0].metadata


def test_memory_learn_resolves_authenticated_receipts_for_semantic_and_procedural(
    tmp_path: Path,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    semantic_title = "Authenticated semantic fact"
    semantic_content = "Four authenticated runtime receipts can support stable learning."
    semantic_candidate = _put_claim_candidate(
        memory,
        title=semantic_title,
        content=semantic_content,
    )
    receipts = {
        bucket: _put_bucket_receipt(memory, bucket, index, semantic_candidate)
        for index, bucket in enumerate(("test", "lint", "repair", "review"), start=1)
    }
    registry = build_default_tools()
    semantic_evidence = {
        f"{bucket}_refs": [{"source": "memory_record", "locator": receipt_id}]
        for bucket, receipt_id in receipts.items()
    }
    semantic_evidence["task_refs"] = [{"source": "memory_record", "locator": receipts["test"]}]

    semantic = registry.execute(
        ToolCall(
            name="memory.learn",
            arguments={
                "title": semantic_title,
                "content": semantic_content,
                "kind": "fact",
                "source_layer": "episodic",
                "source_record_id": semantic_candidate,
                "confidence": 0.95,
                "validation_evidence": semantic_evidence,
            },
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(),
            workspace=tmp_path,
            session_id="ordinary-learning-test",
            run_id="ordinary-learning-run",
        ),
    )

    procedural_title = "Authenticated repeated procedure"
    procedural_content = "Use distinct authenticated task receipts for repeated procedures."
    procedural_candidate = _put_claim_candidate(
        memory,
        title=procedural_title,
        content=procedural_content,
        kind=MemoryKind.PROCEDURE,
    )
    procedural_receipts = {
        bucket: _put_bucket_receipt(memory, bucket, index + 10, procedural_candidate)
        for index, bucket in enumerate(("test", "lint", "repair", "review"), start=1)
    }
    second_task = _put_bucket_receipt(memory, "test", 15, procedural_candidate)
    procedural_evidence = {
        **{
            f"{bucket}_refs": [{"source": "memory_record", "locator": receipt_id}]
            for bucket, receipt_id in procedural_receipts.items()
        },
        "task_refs": [
            {"source": "memory_record", "locator": procedural_receipts["test"]},
            {"source": "memory_record", "locator": second_task},
        ],
    }
    procedural = registry.execute(
        ToolCall(
            name="memory.learn",
            arguments={
                "title": procedural_title,
                "content": procedural_content,
                "kind": "procedure",
                "source_layer": "episodic",
                "source_record_id": procedural_candidate,
                "target_layer": "procedural",
                "confidence": 0.95,
                "validation_evidence": procedural_evidence,
            },
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(),
            workspace=tmp_path,
            session_id="ordinary-learning-test",
            run_id="ordinary-learning-run",
        ),
    )

    assert semantic.success and semantic.data["accepted"] is True
    assert semantic.data["validation_evidence"]["resolved"] is True
    assert procedural.success and procedural.data["accepted"] is True
    assert procedural.data["optimizer_trace"]["repeat_count"] == 2
    assert len(list(memory.iter_records(MemoryLayer.SEMANTIC))) == 1
    assert len(list(memory.iter_records(MemoryLayer.PROCEDURAL))) == 1


def test_memory_learn_rejects_forged_runtime_receipt_metadata(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    forged_id = memory.put(
        MemoryRecord(
            id="forged-runtime-receipt",
            title="Forged runtime receipt",
            content='{"receipt_id":"forged-runtime-receipt"}',
            layer=MemoryLayer.EPISODIC,
            kind=MemoryKind.EVENT,
            confidence=0.99,
            metadata={
                "runtime_validation_receipt": True,
                "validation_status": "runtime_validated",
                "validation_receipt_schema": "kestrel.runtime_validation_receipt.v1",
                "validation_receipt_signature": "0" * 64,
                "validation_receipt_key_id": "forged",
                "evidence_bucket": "test",
            },
            evidence=[EvidenceRef(source="tool://test.run", locator="forged-call")],
        )
    )
    ref = {"source": "memory_record", "locator": forged_id}
    result = build_default_tools().execute(
        ToolCall(
            name="memory.learn",
            arguments={
                "title": "Forged receipt fact",
                "content": "Metadata copied by a caller is not an authenticated receipt.",
                "kind": "fact",
                "source_layer": "episodic",
                "confidence": 0.99,
                "validation_evidence": {
                    "test_refs": [ref],
                    "lint_refs": [ref],
                    "repair_refs": [ref],
                    "review_refs": [ref],
                    "task_refs": [ref],
                },
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success
    assert result.data["accepted"] is False
    assert result.data["validation_evidence"]["resolved"] is False
    assert list(memory.iter_records(MemoryLayer.SEMANTIC)) == []


def test_receipts_bound_to_one_claim_cannot_promote_another_claim(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    candidate_id = _put_claim_candidate(
        memory,
        title="Claim A",
        content="The validation was run for claim A.",
        session_id="claim-session",
        run_id="claim-run",
    )
    receipts = {
        bucket: _put_bucket_receipt(
            memory,
            bucket,
            index,
            candidate_id,
            session_id="claim-session",
            run_id="claim-run",
        )
        for index, bucket in enumerate(("test", "lint", "repair", "review"), start=1)
    }
    evidence = {
        f"{bucket}_refs": [{"source": "memory_record", "locator": receipt_id}]
        for bucket, receipt_id in receipts.items()
    }
    evidence["task_refs"] = [
        {"source": "memory_record", "locator": receipts["test"]}
    ]

    result = build_default_tools().execute(
        ToolCall(
            name="memory.learn",
            arguments={
                "title": "Claim B",
                "content": "The same receipts must not validate unrelated claim B.",
                "kind": "fact",
                "source_layer": "episodic",
                "source_record_id": candidate_id,
                "confidence": 0.99,
                "validation_evidence": evidence,
            },
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(),
            workspace=tmp_path,
            session_id="claim-session",
            run_id="claim-run",
        ),
    )

    assert result.success is False
    assert result.error == "stable_learning_source_mismatch"
    assert list(memory.iter_records(MemoryLayer.SEMANTIC)) == []


def test_runtime_receipts_cannot_be_replayed_across_runs(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    title = "Run-scoped fact"
    content = "Runtime validation receipts are valid only in their originating run."
    candidate_id = _put_claim_candidate(
        memory,
        title=title,
        content=content,
        session_id="shared-session",
        run_id="run-a",
    )
    receipts = {
        bucket: _put_bucket_receipt(
            memory,
            bucket,
            index,
            candidate_id,
            session_id="shared-session",
            run_id="run-a",
        )
        for index, bucket in enumerate(("test", "lint", "repair", "review"), start=1)
    }
    evidence = {
        f"{bucket}_refs": [{"source": "memory_record", "locator": receipt_id}]
        for bucket, receipt_id in receipts.items()
    }

    result = build_default_tools().execute(
        ToolCall(
            name="memory.learn",
            arguments={
                "title": title,
                "content": content,
                "kind": "fact",
                "source_layer": "episodic",
                "source_record_id": candidate_id,
                "confidence": 0.99,
                "validation_evidence": evidence,
            },
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(),
            workspace=tmp_path,
            session_id="shared-session",
            run_id="run-b",
        ),
    )

    assert result.success
    assert result.data["accepted"] is False
    assert result.data["validation_evidence"]["resolved"] is False
    assert list(memory.iter_records(MemoryLayer.SEMANTIC)) == []


def test_caller_human_flag_cannot_raise_three_bucket_score(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    title = "Three-bucket claim"
    content = "Three objective buckets remain below the semantic promotion threshold."
    candidate_id = _put_claim_candidate(
        memory,
        title=title,
        content=content,
        session_id="human-forgery-session",
        run_id="human-forgery-run",
    )
    receipts = {
        bucket: _put_bucket_receipt(
            memory,
            bucket,
            index,
            candidate_id,
            session_id="human-forgery-session",
            run_id="human-forgery-run",
        )
        for index, bucket in enumerate(("test", "lint", "repair"), start=1)
    }
    evidence = {
        f"{bucket}_refs": [{"source": "memory_record", "locator": receipt_id}]
        for bucket, receipt_id in receipts.items()
    }
    evidence["task_refs"] = [
        {"source": "memory_record", "locator": receipts["test"]}
    ]
    evidence["human_explicit"] = True

    result = build_default_tools().execute(
        ToolCall(
            name="memory.learn",
            arguments={
                "title": title,
                "content": content,
                "kind": "fact",
                "source_layer": "episodic",
                "source_record_id": candidate_id,
                "confidence": 0.99,
                "explicit_instruction": True,
                "validation_evidence": evidence,
            },
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(),
            workspace=tmp_path,
            session_id="human-forgery-session",
            run_id="human-forgery-run",
        ),
    )

    assert result.success
    assert result.data["accepted"] is True
    assert result.data["action"] == "promote_provisional"
    assert result.data["validation_score"] == 0.75
    assert result.data["validation_evidence"]["human_explicit"] is False
    records = list(memory.iter_records(MemoryLayer.SEMANTIC))
    assert len(records) == 1
    assert records[0].metadata["promotion_status"] == "provisional"


def test_mutating_subject_after_validation_invalidates_receipt(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    title = "Immutable validated subject"
    content = "The signed digest covers the exact candidate claim."
    candidate_id = _put_claim_candidate(memory, title=title, content=content)
    receipt_id = _put_runtime_receipt(memory, candidate_id)
    candidate = memory.get_record(MemoryLayer.EPISODIC, candidate_id)
    assert candidate is not None
    candidate.content = "The subject was changed after validation."
    memory.upsert(candidate)
    evidence = _resolved_evidence((receipt_id,))
    _, stable = _stable_record(
        evidence=evidence,
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        title=title,
        content=content,
    )
    stable.evidence.append(EvidenceRef(source="memory_record", locator=candidate_id))

    with pytest.raises(ValueError, match="validation_receipt_subject_mismatch"):
        memory.put_validated(
            stable,
            authority="nested_learning",
            source_record_ids=(candidate_id, receipt_id),
            validation_evidence=evidence,
        )


def test_memory_validation_key_authenticates_receipt_after_runtime_restart(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    first = build_memory_system("memory", memory_dir)
    candidate_id = _put_claim_candidate(
        first,
        title="Restart-safe receipt",
        content="The owner-only signing key survives a new runtime instance.",
    )
    receipt_id = _put_runtime_receipt(first, candidate_id)
    receipt = first.get_record(MemoryLayer.EPISODIC, receipt_id)
    assert receipt is not None
    first.close_all()

    second = build_memory_system("memory", memory_dir)

    assert second.is_authenticated_validation_receipt(
        receipt,
        require_subject_binding=True,
    )
    assert (memory_dir / ".validation-integrity.key").stat().st_mode & 0o777 == 0o600


def test_memory_validation_key_concurrent_first_open_is_single_identity(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"

    with ThreadPoolExecutor(max_workers=12) as pool:
        keys = list(
            pool.map(
                lambda _: _load_or_create_memory_integrity_key(memory_dir),
                range(24),
            )
        )

    assert len(set(keys)) == 1
    assert len(keys[0]) == 32
    key_path = memory_dir / ".validation-integrity.key"
    assert key_path.read_text(encoding="utf-8") == keys[0].hex()
    if os.name != "nt":
        assert key_path.stat().st_mode & 0o777 == 0o600


def test_memory_validation_key_recovers_partial_orphan_temp(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    temporary = memory_dir / ".validation-integrity.key.tmp"
    temporary.write_text("partial", encoding="utf-8")
    temporary.chmod(0o600)

    recovered = _load_or_create_memory_integrity_key(memory_dir)

    assert len(recovered) == 32
    assert (memory_dir / ".validation-integrity.key").read_text(
        encoding="utf-8"
    ) == recovered.hex()
    assert not temporary.exists()


def test_memory_validation_key_recovers_post_link_crash_state(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    temporary = memory_dir / ".validation-integrity.key.tmp"
    final = memory_dir / ".validation-integrity.key"
    expected = os.urandom(32)
    temporary.write_text(expected.hex(), encoding="utf-8")
    temporary.chmod(0o600)
    os.link(temporary, final)
    assert temporary.stat().st_nlink == 2

    recovered = _load_or_create_memory_integrity_key(memory_dir)

    assert recovered == expected
    assert not temporary.exists()
    assert final.stat().st_nlink == 1


@pytest.mark.parametrize("failure_point", ["write", "file_fsync", "publish"])
def test_memory_validation_key_fault_before_publication_cleans_temp_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    memory_dir = tmp_path / "memory"
    final = memory_dir / ".validation-integrity.key"
    temporary = memory_dir / ".validation-integrity.key.tmp"

    with monkeypatch.context() as fault:
        if failure_point == "write":
            def fail_partial_write(descriptor: int, payload: bytes) -> None:
                assert os.write(descriptor, payload[:7]) == 7
                raise OSError("injected memory-key write failure")

            fault.setattr(
                private_artifacts_module,
                "_write_private_bytes",
                fail_partial_write,
            )
        elif failure_point == "file_fsync":
            fault.setattr(
                private_artifacts_module,
                "_sync_private_file",
                lambda _descriptor: (_ for _ in ()).throw(
                    OSError("injected memory-key file fsync failure")
                ),
            )
        else:
            fault.setattr(
                private_artifacts_module,
                "_publish_private_file_exclusive",
                lambda _temporary, _resolved: (_ for _ in ()).throw(
                    OSError("injected memory-key publish failure")
                ),
            )

        with pytest.raises(OSError, match="injected memory-key"):
            _load_or_create_memory_integrity_key(memory_dir)

    assert not final.exists()
    assert not temporary.exists()
    recovered = _load_or_create_memory_integrity_key(memory_dir)
    assert final.read_text(encoding="utf-8") == recovered.hex()
    assert not temporary.exists()


def test_memory_validation_key_directory_fsync_failure_leaves_recoverable_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_dir = tmp_path / "memory"
    final = memory_dir / ".validation-integrity.key"
    temporary = memory_dir / ".validation-integrity.key.tmp"

    with monkeypatch.context() as fault:
        fault.setattr(
            private_artifacts_module,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(
                OSError("injected memory-key directory fsync failure")
            ),
        )
        with pytest.raises(OSError, match="directory fsync failure"):
            _load_or_create_memory_integrity_key(memory_dir)

    published = bytes.fromhex(final.read_text(encoding="utf-8"))
    assert len(published) == 32
    assert not temporary.exists()
    assert _load_or_create_memory_integrity_key(memory_dir) == published


def test_memory_validation_key_publish_never_overwrites_external_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_dir = tmp_path / "memory"
    final = memory_dir / ".validation-integrity.key"
    temporary = memory_dir / ".validation-integrity.key.tmp"
    winner = os.urandom(32)
    original_publish = private_artifacts_module._publish_private_file_exclusive

    def publish_after_external_winner(temp_path: Path, final_path: Path) -> None:
        final_path.write_text(winner.hex(), encoding="utf-8")
        final_path.chmod(0o600)
        original_publish(temp_path, final_path)

    monkeypatch.setattr(
        private_artifacts_module,
        "_publish_private_file_exclusive",
        publish_after_external_winner,
    )

    loaded = _load_or_create_memory_integrity_key(memory_dir)

    assert loaded == winner
    assert final.read_text(encoding="utf-8") == winner.hex()
    assert not temporary.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX link semantics required")
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_memory_validation_key_rejects_link_aliases(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    memory_dir = tmp_path / alias_kind
    memory_dir.mkdir()
    outside = tmp_path / f"{alias_kind}-outside-key"
    outside.write_text("ab" * 32, encoding="utf-8")
    key_path = memory_dir / ".validation-integrity.key"
    if alias_kind == "symlink":
        key_path.symlink_to(outside)
    else:
        os.link(outside, key_path)

    with pytest.raises((OSError, ValueError), match="link|symbolic"):
        _load_or_create_memory_integrity_key(memory_dir)


def test_policy_promotion_binds_signed_artifacts_to_exact_proposal(
    tmp_path: Path,
) -> None:
    _initialize_git_workspace(tmp_path)
    artifact_ids = _put_signed_policy_artifacts(tmp_path, prefix="exact")
    memory = build_memory_system("memory", tmp_path / "memory")
    result = _promote_policy(
        memory,
        workspace=tmp_path,
        artifact_ids=artifact_ids,
        title="Exact approval policy",
        content="Every high-risk action requires exact-call owner approval.",
        run_id="policy-run-a",
        call_id="policy-call-a",
    )

    assert result.success
    record = memory.get_record(MemoryLayer.POLICY, str(result.data["record_id"]))
    assert record is not None
    envelope_ids = set(record.metadata["stable_write_envelope"]["source_record_ids"])
    resolution_ids = set(record.metadata["validation_evidence"]["resolution_artifact_ids"])
    proposal_ids = envelope_ids - resolution_ids
    assert len(proposal_ids) == 1
    assert len(resolution_ids) == 5
    assert set(record.metadata["resolved_artifact_bindings"]) == resolution_ids
    proposal_id = next(iter(proposal_ids))
    proposal = memory.get_record(MemoryLayer.EPISODIC, proposal_id)
    assert proposal is not None
    assert proposal.title == record.title
    assert proposal.content == record.content
    for receipt_id in resolution_ids:
        receipt = memory.get_record(MemoryLayer.EPISODIC, receipt_id)
        assert receipt is not None
        binding = memory.validation_receipt_subject(receipt)
        assert binding is not None
        assert binding[0] == proposal_id
        assert binding[3] == "policy-run-a"


def test_policy_receipts_cannot_replay_across_claims_or_runs(tmp_path: Path) -> None:
    _initialize_git_workspace(tmp_path)
    artifact_ids = _put_signed_policy_artifacts(tmp_path, prefix="replay")
    memory = build_memory_system("memory", tmp_path / "memory")
    first = _promote_policy(
        memory,
        workspace=tmp_path,
        artifact_ids=artifact_ids,
        title="Original policy claim",
        content="Require owner approval for the original claim.",
        run_id="policy-origin-run",
        call_id="policy-origin-call",
    )
    assert first.success
    original = memory.get_record(MemoryLayer.POLICY, str(first.data["record_id"]))
    assert original is not None
    receipt_ids = tuple(original.metadata["validation_evidence"]["resolution_artifact_ids"])

    cross_claim = _promote_policy_with_memory_receipts(
        memory,
        workspace=tmp_path,
        receipt_ids=receipt_ids,
        title="Unrelated policy claim",
        content="Receipt replay must not authorize this unrelated policy.",
        run_id="policy-origin-run",
        call_id="policy-cross-claim-call",
    )
    cross_run = _promote_policy_with_memory_receipts(
        memory,
        workspace=tmp_path,
        receipt_ids=receipt_ids,
        title=original.title,
        content=original.content,
        run_id="policy-replay-run",
        call_id="policy-cross-run-call",
    )

    assert cross_claim.success is False
    assert cross_claim.error == "policy_evidence_unresolved"
    assert cross_run.success is False
    assert cross_run.error == "policy_evidence_unresolved"
    assert len(list(memory.iter_records(MemoryLayer.POLICY))) == 1


def test_raw_signed_policy_artifacts_cannot_be_rematerialized_for_a_claim(
    tmp_path: Path,
) -> None:
    _initialize_git_workspace(tmp_path)
    artifact_ids = _put_signed_policy_artifacts(tmp_path, prefix="raw_replay")
    memory = build_memory_system("memory", tmp_path / "memory")
    title = "Raw artifact replay policy"
    content = "A raw repair receipt cannot be attached to a later policy claim."
    proposal_id = _stage_policy_proposal(
        memory,
        workspace=tmp_path,
        title=title,
        content=content,
        run_id="policy-raw-run",
        call_id="policy-raw-proposal",
    )
    refs = [
        {"source": "repair.validate", "locator": artifact_id}
        for artifact_id in artifact_ids
    ]

    result = _execute_policy_promotion(
        memory,
        workspace=tmp_path,
        evidence={
            "test_refs": [refs[0]],
            "lint_refs": [refs[1]],
            "repair_refs": [refs[2]],
            "review_refs": [refs[3]],
            "task_refs": refs,
            "human_explicit": True,
        },
        title=title,
        content=content,
        source_record_id=proposal_id,
        run_id="policy-raw-run",
        call_id="policy-raw-promote",
    )

    assert result.success is False
    assert result.error == "policy_evidence_unresolved"
    assert list(memory.iter_records(MemoryLayer.POLICY)) == []


def test_policy_evidence_receipt_must_match_its_objective_bucket(tmp_path: Path) -> None:
    _initialize_git_workspace(tmp_path)
    artifact_ids = _put_signed_policy_artifacts(tmp_path, prefix="wrong_bucket")
    memory = build_memory_system("memory", tmp_path / "memory")
    title = "Bucket-bound policy evidence"
    content = "Lint evidence cannot masquerade as a test execution receipt."
    proposal_id = _stage_policy_proposal(
        memory,
        workspace=tmp_path,
        title=title,
        content=content,
        run_id="policy-bucket-run",
        call_id="policy-bucket-proposal",
    )
    receipt_ids = _put_policy_receipts(
        memory,
        workspace=tmp_path,
        proposal_id=proposal_id,
        artifact_ids=artifact_ids,
        run_id="policy-bucket-run",
        prefix="policy-bucket",
    )
    refs = [
        {"source": "memory_record", "locator": receipt_id}
        for receipt_id in receipt_ids
    ]

    result = _execute_policy_promotion(
        memory,
        workspace=tmp_path,
        evidence={
            "test_refs": [refs[1]],
            "lint_refs": [refs[0]],
            "repair_refs": [refs[2]],
            "review_refs": [refs[3]],
            "task_refs": refs,
            "human_explicit": True,
        },
        title=title,
        content=content,
        source_record_id=proposal_id,
        run_id="policy-bucket-run",
        call_id="policy-bucket-promote",
    )

    assert result.success is False
    assert result.error == "policy_evidence_unresolved"
    assert list(memory.iter_records(MemoryLayer.POLICY)) == []


def test_policy_recall_survives_restart_and_fails_closed_on_tombstoned_evidence(
    tmp_path: Path,
) -> None:
    from nested_memvid_agent.agent import _is_trusted_policy_record

    _initialize_git_workspace(tmp_path)
    artifact_ids = _put_signed_policy_artifacts(tmp_path, prefix="restart")
    memory_dir = tmp_path / "memory"
    memory = build_memory_system("memory", memory_dir)
    promoted = _promote_policy(
        memory,
        workspace=tmp_path,
        artifact_ids=artifact_ids,
        title="Restart durable policy",
        content="Keep exact-call approval gates active after restart.",
        run_id="policy-restart-run",
        call_id="policy-restart-call",
    )
    assert promoted.success
    memory.close_all()

    reopened = build_memory_system("memory", memory_dir)
    policy = reopened.get_record(MemoryLayer.POLICY, str(promoted.data["record_id"]))
    assert policy is not None
    assert _is_trusted_policy_record(
        policy,
        memory=reopened,
        spec=reopened.specs[MemoryLayer.POLICY],
        state_path=tmp_path / "state" / "agent.db",
        workspace=tmp_path,
    )

    receipt_id = str(policy.metadata["validation_evidence"]["resolution_artifact_ids"][0])
    assert reopened.tombstone(
        MemoryLayer.EPISODIC,
        receipt_id,
        reason="adversarial recall test",
    )
    assert not _is_trusted_policy_record(
        policy,
        memory=reopened,
        spec=reopened.specs[MemoryLayer.POLICY],
        state_path=tmp_path / "state" / "agent.db",
        workspace=tmp_path,
    )


def test_retention_compaction_does_not_promote_stale_caller_scores(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            id="stale-raw-score",
            title="Stale caller score",
            content="Old caller-controlled score metadata cannot become stable memory.",
            layer=MemoryLayer.WORKING,
            kind=MemoryKind.PROCEDURE,
            confidence=0.9,
            created_at=datetime.now(UTC) - timedelta(days=30),
            metadata={
                "validation_score": 1.0,
                "repeat_count": 999,
                "explicit_instruction": True,
            },
        )
    )

    report = RetentionCompactor(memory).compact_layer(MemoryLayer.WORKING, dry_run=False)

    assert report["promoted_ids"] == []
    assert list(memory.iter_records(MemoryLayer.SEMANTIC)) == []
    assert list(memory.iter_records(MemoryLayer.PROCEDURAL)) == []
    assert list(memory.iter_records(MemoryLayer.SELF)) == []
    assert list(memory.iter_records(MemoryLayer.POLICY)) == []


def _put_claim_candidate(
    memory: LayeredMemorySystem,
    *,
    title: str,
    content: str,
    kind: MemoryKind = MemoryKind.FACT,
    session_id: str = "integrity-test",
    run_id: str | None = "integrity-run",
) -> str:
    return memory.put(
        MemoryRecord(
            title=title,
            content=content,
            layer=MemoryLayer.EPISODIC,
            kind=kind,
            confidence=0.95,
            metadata={"session_id": session_id, "run_id": run_id},
            evidence=[EvidenceRef(source="test-fixture", locator=run_id or session_id)],
        )
    )


def _put_runtime_receipt(
    memory: LayeredMemorySystem,
    subject_record_id: str,
    *,
    index: int = 1,
    session_id: str = "integrity-test",
    run_id: str | None = "integrity-run",
) -> str:
    return memory.put_runtime_validation_receipt(
        tool_name="runtime.validation",
        tool_call_id=f"runtime-validation-call-{index}",
        evidence_bucket="test",
        command=("validate",),
        output_sha256=f"{index:064x}",
        session_id=session_id,
        run_id=run_id,
        subject_record_id=subject_record_id,
    )


def _put_bucket_receipt(
    memory: LayeredMemorySystem,
    bucket: str,
    index: int,
    subject_record_id: str,
    *,
    session_id: str = "ordinary-learning-test",
    run_id: str | None = "ordinary-learning-run",
) -> str:
    return memory.put_runtime_validation_receipt(
        tool_name=f"{bucket}.validator",
        tool_call_id=f"{bucket}-call-{index}",
        evidence_bucket=bucket,
        command=(bucket, str(index)),
        output_sha256=f"{index:064x}",
        session_id=session_id,
        run_id=run_id,
        subject_record_id=subject_record_id,
    )


def _resolved_evidence(receipt_ids: tuple[str, ...]) -> ValidationEvidence:
    refs = tuple(EvidenceRef(source="memory_record", locator=item) for item in receipt_ids)
    evidence = ValidationEvidence(
        test_refs=(refs[0],),
        lint_refs=(refs[-1],),
        repair_refs=(refs[0],),
        review_refs=(refs[-1],),
        task_refs=refs,
    )
    return resolve_validation_evidence(
        evidence,
        status="runtime_validated",
        artifact_ids=receipt_ids,
    )


def _stable_record(
    *,
    evidence: ValidationEvidence,
    layer: MemoryLayer,
    kind: MemoryKind,
    title: str,
    content: str,
) -> tuple[LearningSignal, MemoryRecord]:
    signal = LearningSignal(
        title=title,
        content=content,
        kind=kind,
        source_layer=MemoryLayer.EPISODIC,
        confidence=0.95,
        validation_score=None,
        validation_evidence=evidence,
        repeat_count=999,
        metadata={"session_id": "integrity-test", "run_id": "integrity-run"},
        requested_target_layer=layer,
    )
    kernel = NestedLearningKernel()
    decision = kernel.decide(signal)
    assert decision.accepted
    return signal, kernel.to_memory_record(signal, decision)


def _initialize_git_workspace(workspace: Path) -> None:
    subprocess.run(
        ["git", "init", "-q"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )


def _put_signed_policy_artifacts(workspace: Path, *, prefix: str) -> tuple[str, ...]:
    artifact_ids: list[str] = []
    for index in range(5):
        artifact_id = f"repair_validation_{prefix}_{index}"
        write_repair_artifact(
            workspace,
            "repair_validations",
            artifact_id,
            {
                "schema_version": 1,
                "validation_id": artifact_id,
                "tool": "repair.validate",
                "success": True,
                "returncode": 0,
                "output_sha256": f"{index + 1:064x}",
            },
        )
        artifact_ids.append(artifact_id)
    return tuple(artifact_ids)


def _promote_policy(
    memory: LayeredMemorySystem,
    *,
    workspace: Path,
    artifact_ids: tuple[str, ...],
    title: str,
    content: str,
    run_id: str,
    call_id: str,
) -> ToolExecution:
    proposal_id = _stage_policy_proposal(
        memory,
        workspace=workspace,
        title=title,
        content=content,
        run_id=run_id,
        call_id=f"{call_id}-proposal",
    )
    receipt_ids = _put_policy_receipts(
        memory,
        workspace=workspace,
        proposal_id=proposal_id,
        artifact_ids=artifact_ids,
        run_id=run_id,
        prefix=call_id,
    )
    refs = [{"source": "memory_record", "locator": item} for item in receipt_ids]
    return _execute_policy_promotion(
        memory,
        workspace=workspace,
        evidence={
            "test_refs": [refs[0]],
            "lint_refs": [refs[1]],
            "repair_refs": [refs[2]],
            "review_refs": [refs[3]],
            "task_refs": refs,
            "human_explicit": True,
        },
        title=title,
        content=content,
        source_record_id=proposal_id,
        run_id=run_id,
        call_id=call_id,
    )


def _promote_policy_with_memory_receipts(
    memory: LayeredMemorySystem,
    *,
    workspace: Path,
    receipt_ids: tuple[str, ...],
    title: str,
    content: str,
    run_id: str,
    call_id: str,
) -> ToolExecution:
    proposal_id = _stage_policy_proposal(
        memory,
        workspace=workspace,
        title=title,
        content=content,
        run_id=run_id,
        call_id=f"{call_id}-proposal",
    )
    refs = [
        {"source": "memory_record", "locator": receipt_id}
        for receipt_id in receipt_ids
    ]
    return _execute_policy_promotion(
        memory,
        workspace=workspace,
        evidence={
            "test_refs": [refs[0]],
            "lint_refs": [refs[1]],
            "repair_refs": [refs[2]],
            "review_refs": [refs[3]],
            "task_refs": refs,
            "human_explicit": True,
        },
        title=title,
        content=content,
        source_record_id=proposal_id,
        run_id=run_id,
        call_id=call_id,
    )


def _stage_policy_proposal(
    memory: LayeredMemorySystem,
    *,
    workspace: Path,
    title: str,
    content: str,
    run_id: str,
    call_id: str,
) -> str:
    execution = _execute_policy_call(
        memory,
        workspace=workspace,
        arguments={
            "title": title,
            "content": content,
            "confidence": 0.99,
            "stage_proposal": True,
        },
        run_id=run_id,
        call_id=call_id,
    )
    assert execution.success
    return str(execution.data["proposal_id"])


def _put_policy_receipts(
    memory: LayeredMemorySystem,
    *,
    workspace: Path,
    proposal_id: str,
    artifact_ids: tuple[str, ...],
    run_id: str,
    prefix: str,
) -> tuple[str, ...]:
    review_id = f"repair_review_{prefix.replace('-', '_')}"
    write_repair_artifact(
        workspace,
        "repair_reviews",
        review_id,
        {
            "schema_version": 1,
            "review_id": review_id,
            "validation_id": artifact_ids[0],
            "validation": {
                "validation_id": artifact_ids[0],
                "tool": "repair.validate",
                "success": True,
                "returncode": 0,
            },
            "commit_gate": {"commit_allowed": True},
        },
    )
    definitions = (
        ("test.run", "test", None, None),
        ("lint.run", "lint", None, None),
        ("repair.validate", "repair", "repair.validate", artifact_ids[0]),
        ("repair.review", "review", "repair.review", review_id),
        ("test.run", "test", None, None),
    )
    receipt_ids: list[str] = []
    for index, (tool_name, bucket, artifact_source, artifact_locator) in enumerate(
        definitions,
        start=1,
    ):
        receipt_ids.append(
            memory.put_runtime_validation_receipt(
                tool_name=tool_name,
                tool_call_id=f"{prefix}-{bucket}-{index}",
                evidence_bucket=bucket,
                command=(tool_name, str(index)),
                output_sha256=f"{index:064x}",
                session_id="policy-integrity-session",
                run_id=run_id,
                signed_artifact_source=artifact_source,
                signed_artifact_locator=artifact_locator,
                subject_record_id=proposal_id,
            )
        )
    return tuple(receipt_ids)


def _execute_policy_promotion(
    memory: LayeredMemorySystem,
    *,
    workspace: Path,
    evidence: dict[str, object],
    title: str,
    content: str,
    source_record_id: str,
    run_id: str,
    call_id: str,
) -> ToolExecution:
    arguments = {
        "title": title,
        "content": content,
        "source_record_id": source_record_id,
        "confidence": 0.99,
        "validation_evidence": evidence,
    }
    return _execute_policy_call(
        memory,
        workspace=workspace,
        arguments=arguments,
        run_id=run_id,
        call_id=call_id,
    )


def _execute_policy_call(
    memory: LayeredMemorySystem,
    *,
    workspace: Path,
    arguments: dict[str, object],
    run_id: str,
    call_id: str,
) -> ToolExecution:
    state_path = workspace / "state" / "agent.db"
    state = AgentStateStore(state_path)
    approval = state.create_approval(
        approval_id=f"approval-{call_id}",
        run_id=run_id,
        tool_call_id=call_id,
        tool_name="memory.policy_promote",
        arguments=arguments,
        risk="high",
    )
    approval, applied = state.decide_approval_once(
        approval["approval_id"],
        status="approved",
        decision={
            "approved": True,
            "arguments": arguments,
            "principal": "owner",
        },
        principal="owner",
    )
    assert applied
    call = ToolCall(name="memory.policy_promote", arguments=arguments, id=call_id)
    execution = build_default_tools().execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(
                allow_policy_writes=True,
                state_path=state_path,
            ),
            workspace=workspace,
            session_id="policy-integrity-session",
            run_id=run_id,
            approved_tool_call_ids=frozenset({call_id}),
            approved_tool_call_arguments={call_id: arguments},
            approval_receipts={call_id: approval},
        ),
    )
    if execution.success:
        state.record_approval_result(
            approval["approval_id"],
            {
                "tool": execution.call.name,
                "tool_call_id": execution.call.id,
                "arguments": execution.call.arguments,
                "success": execution.success,
                "content": execution.content,
                "data": execution.data,
                "error": execution.error,
            },
        )
    return execution
