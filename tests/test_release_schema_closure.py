"""Recursive closure gate for S2 release-control JSON schemas."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas"
VECTOR_ROOT = ROOT / "tests" / "fixtures" / "release-control" / "v3"
POSITIVE_VECTORS = VECTOR_ROOT / "positive-contract-vectors.json"
MUTANT_VECTORS = VECTOR_ROOT / "one-rule-mutants.json"

TASK2_VECTOR_SCHEMAS = {
    "kestrel.canonicalization_vectors.v1.schema.json",
    "kestrel.credential_scope_authority.v1.schema.json",
    "kestrel.dispatch_admission.v1.schema.json",
    "kestrel.dispatch_identity.v1.schema.json",
    "kestrel.dispatch_tombstone.v1.schema.json",
    "kestrel.github_release_authority.v3.schema.json",
    "kestrel.pypi_upload_authority_prerequisite.v3.schema.json",
    "kestrel.recovery_execution_closure.v1.schema.json",
    "kestrel.recovery_repository_authority.v1.schema.json",
    "kestrel.release_commit_outcome.v2.schema.json",
    "kestrel.release_dispatch_intent.v2.schema.json",
    "kestrel.release_dispatch_reconciliation.v1.schema.json",
    "kestrel.release_dispatch_transaction.v1.schema.json",
    "kestrel.release_github_ghcr_verification.v2.schema.json",
    "kestrel.release_preparation_outcome.v2.schema.json",
    "kestrel.release_prerequisites.v2.schema.json",
    "kestrel.release_pypi_outcome.v2.schema.json",
    "kestrel.release_reconciliation.v2.schema.json",
    "kestrel.release_recovery_capsule.v1.schema.json",
    "kestrel.release_server_authorization.v3.schema.json",
    "kestrel.release_shared.v1.schema.json",
    "kestrel.repository_writer_inventory.v1.schema.json",
    "kestrel.runtime_credential_verification.v1.schema.json",
    "kestrel.source_observation.v1.schema.json",
}

SIGNED_VECTOR_NAMES = {
    "credential-scope",
    "dispatch-admission",
    "dispatch-intent",
    "dispatch-tombstone",
    "github-authority",
    "pypi-authority",
    "recovery-repository-authority",
    "writer-inventory-post-containment",
    "writer-inventory-pre-admission",
    "writer-inventory-pre-send",
}

KNOWN_KEY_FINGERPRINT = "sha256:7959022879dce518da9c176e536be639e71b165dccf703a01dd936e08349cad6"

CORE_SCHEMAS = (
    "kestrel.actions_artifact_observation.v1.schema.json",
    "kestrel.canonicalization_vectors.v1.schema.json",
    "kestrel.credential_scope_authority.v1.schema.json",
    "kestrel.dispatch_admission.v1.schema.json",
    "kestrel.dispatch_identity.v1.schema.json",
    "kestrel.dispatch_tombstone.v1.schema.json",
    "kestrel.github_release_authority.v3.schema.json",
    "kestrel.pypi_upload_authority_prerequisite.v3.schema.json",
    "kestrel.recovery_execution_closure.v1.schema.json",
    "kestrel.recovery_repository_authority.v1.schema.json",
    "kestrel.release_candidate.v1.schema.json",
    "kestrel.release_commit_outcome.v2.schema.json",
    "kestrel.release_dispatch_intent.v2.schema.json",
    "kestrel.release_dispatch_reconciliation.v1.schema.json",
    "kestrel.release_dispatch_transaction.v1.schema.json",
    "kestrel.release_github_ghcr_verification.v2.schema.json",
    "kestrel.release_preparation_outcome.v2.schema.json",
    "kestrel.release_prerequisites.v2.schema.json",
    "kestrel.release_pypi_outcome.v2.schema.json",
    "kestrel.release_reconciliation.v2.schema.json",
    "kestrel.release_recovery_capsule.v1.schema.json",
    "kestrel.release_server_authorization.v3.schema.json",
    "kestrel.repository_writer_inventory.v1.schema.json",
    "kestrel.release_shared.v1.schema.json",
    "kestrel.runtime_credential_verification.v1.schema.json",
    "kestrel.source_observation.v1.schema.json",
    "kestrel.source_registry.v1.schema.json",
)


def _walk_schema(value: object, location: str = "$") -> list[str]:
    problems: list[str] = []
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#/"):
            problems.append(f"remote ref at {location}")
        if value.get("type") == "object" or "properties" in value:
            properties = value.get("properties")
            constrained_map = (
                not isinstance(properties, dict)
                and isinstance(value.get("propertyNames"), dict)
                and isinstance(value.get("additionalProperties"), dict)
                and isinstance(value.get("minProperties"), int)
                and isinstance(value.get("maxProperties"), int)
            )
            if constrained_map:
                pass
            elif not isinstance(properties, dict) or not properties:
                problems.append(f"unconstrained object at {location}")
            else:
                if set(value.get("required", [])) != set(properties):
                    problems.append(f"non-exact required list at {location}")
                if value.get("additionalProperties") is not False:
                    problems.append(f"open object at {location}")
        if value.get("type") == "array":
            if not isinstance(value.get("items"), dict):
                problems.append(f"unconstrained array at {location}")
            if "minItems" not in value or "maxItems" not in value:
                problems.append(f"unbounded array at {location}")
            if value.get("uniqueItems") is not True:
                problems.append(f"non-unique array at {location}")
            if "x-sort-key" not in value:
                problems.append(f"unsorted array at {location}")
        for key, child in value.items():
            problems.extend(_walk_schema(child, f"{location}/{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            problems.extend(_walk_schema(child, f"{location}/{index}"))
    return problems


def test_closure_walker_rejects_an_empty_object_contract() -> None:
    """Catch an unconstrained `{}` record being blessed as recursively closed."""

    schema = {
        "type": "object",
        "required": [],
        "additionalProperties": False,
        "properties": {},
    }

    assert _walk_schema(schema) == ["unconstrained object at $"]


def test_closure_walker_accepts_a_bounded_typed_map_contract() -> None:
    schema = {
        "type": "object",
        "propertyNames": {"pattern": "^[a-z]+$"},
        "additionalProperties": {"enum": ["read", "write"]},
        "minProperties": 0,
        "maxProperties": 32,
    }

    assert _walk_schema(schema) == []


@pytest.mark.parametrize("name", CORE_SCHEMAS)
def test_core_release_schema_is_valid_local_and_recursively_closed(name: str) -> None:
    path = SCHEMA_ROOT / name
    raw = path.read_bytes()
    schema = json.loads(raw)

    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"https://kestrel.dev/schemas/{name}"
    assert schema["unevaluatedProperties"] is False
    assert _walk_schema(schema) == []


def test_committed_source_registry_is_canonical_sorted_and_schema_valid() -> None:
    raw = (ROOT / "release-control-source-registry.json").read_bytes()
    value = json.loads(raw)
    schema = json.loads((SCHEMA_ROOT / "kestrel.source_registry.v1.schema.json").read_bytes())

    jsonschema.Draft202012Validator(schema).validate(value)
    assert raw == json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    entries = value["entries"]
    assert entries == sorted(
        entries,
        key=lambda item: (
            item["receipt_schema"],
            "" if item["phase"] is None else item["phase"],
            "" if item["mode"] is None else item["mode"],
            item["name"],
        ),
    )
    assert len(entries) == len(
        {
            (
                item["receipt_schema"],
                item["phase"],
                item["mode"],
                item["name"],
            )
            for item in entries
        }
    )


def test_release_control_vector_inventory_is_complete_and_digest_frozen() -> None:
    inventory = VECTOR_ROOT / "VECTORS.sha256"
    lines = inventory.read_text(encoding="ascii").splitlines()
    assert lines == sorted(lines)
    listed: set[str] = set()
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([1-9][0-9]*)  ([A-Za-z0-9._/-]+)", line)
        assert match is not None
        digest, size_text, relative = match.groups()
        assert relative not in listed
        assert not relative.startswith("/") and ".." not in Path(relative).parts
        listed.add(relative)
        raw = (VECTOR_ROOT / relative).read_bytes()
        assert len(raw) == int(size_text)
        assert hashlib.sha256(raw).hexdigest() == digest
    expected = {
        path.relative_to(VECTOR_ROOT).as_posix()
        for path in VECTOR_ROOT.rglob("*")
        if path.is_file() and path.name != "VECTORS.sha256"
    }
    assert listed == expected


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _contract_vector_bundle(path: Path, *, expected_schema: str) -> list[dict[str, object]]:
    raw = path.read_bytes()
    value = json.loads(raw)
    assert raw == _canonical(value)
    assert set(value) == {"schema", "vectors"}
    assert value["schema"] == expected_schema
    vectors = value["vectors"]
    assert isinstance(vectors, list)
    assert [item["name"] for item in vectors] == sorted(  # type: ignore[index]
        item["name"]
        for item in vectors  # type: ignore[index]
    )
    return vectors


def test_positive_contract_vectors_are_canonical_complete_and_signed() -> None:
    from scripts import release_control_receipt as receipts

    vectors = _contract_vector_bundle(
        POSITIVE_VECTORS,
        expected_schema="kestrel.release_control_positive_vectors.v1",
    )
    represented: set[str] = set()
    signed: set[str] = set()
    for item in vectors:
        assert set(item) == {"name", "record", "schema_file", "signature_base64"}
        name = item["name"]
        schema_file = item["schema_file"]
        assert isinstance(name, str)
        assert isinstance(schema_file, str)
        represented.add(schema_file)
        schema = json.loads((SCHEMA_ROOT / schema_file).read_bytes())
        jsonschema.Draft202012Validator(schema).validate(item["record"])
        encoded_signature = item["signature_base64"]
        if encoded_signature is None:
            continue
        assert isinstance(encoded_signature, str)
        signature = base64.b64decode(encoded_signature, validate=True)
        assert base64.b64encode(signature).decode("ascii") == encoded_signature
        assert receipts.verify_detached_signature(
            receipt=_canonical(item["record"]),
            signature=signature,
            expected_fingerprint=KNOWN_KEY_FINGERPRINT,
            namespace=receipts.SIGNING_NAMESPACE,
        )
        signed.add(name)

    assert represented == TASK2_VECTOR_SCHEMAS
    assert signed == SIGNED_VECTOR_NAMES


def test_one_rule_mutants_each_fail_their_named_schema() -> None:
    vectors = _contract_vector_bundle(
        MUTANT_VECTORS,
        expected_schema="kestrel.release_control_one_rule_mutants.v1",
    )
    represented: set[str] = set()
    for item in vectors:
        assert set(item) == {"name", "record", "schema_file"}
        schema_file = item["schema_file"]
        assert isinstance(schema_file, str)
        represented.add(schema_file)
        schema = json.loads((SCHEMA_ROOT / schema_file).read_bytes())
        assert list(jsonschema.Draft202012Validator(schema).iter_errors(item["record"]))

    assert represented == TASK2_VECTOR_SCHEMAS


def test_dispatch_vectors_independently_recompute_every_cross_record_binding() -> None:
    vectors = _contract_vector_bundle(
        POSITIVE_VECTORS,
        expected_schema="kestrel.release_control_positive_vectors.v1",
    )
    records = {item["name"]: item["record"] for item in vectors}
    transaction = records["dispatch-transaction"]
    intent = records["dispatch-intent"]
    identity = records["dispatch-identity"]
    reconciliation = records["dispatch-reconciliation-adopted"]
    admission = records["dispatch-admission"]
    assert isinstance(transaction, dict)
    assert isinstance(intent, dict)
    assert isinstance(identity, dict)
    assert isinstance(reconciliation, dict)
    assert isinstance(admission, dict)

    inputs = dict(transaction["inputs"])
    recorded_binding = inputs.pop("dispatch_binding")
    binding = (
        "sha256:"
        + hashlib.sha256(
            _canonical({"inputs": inputs, "ref": transaction["target"]["short_ref"]})  # type: ignore[index]
        ).hexdigest()
    )
    request = {"inputs": transaction["inputs"], "ref": transaction["target"]["short_ref"]}  # type: ignore[index]
    assert recorded_binding == transaction["dispatch_binding"] == binding
    assert transaction["canonical_request_sha256"] == (
        "sha256:" + hashlib.sha256(_canonical(request)).hexdigest()
    )
    assert intent["transaction_digest"] == (
        "sha256:" + hashlib.sha256(_canonical(transaction)).hexdigest()
    )
    assert intent["request_digest"] == transaction["canonical_request_sha256"]
    assert identity["dispatch_inputs_digest"] == (
        "sha256:" + hashlib.sha256(_canonical(transaction["inputs"])).hexdigest()
    )
    assert (
        reconciliation["transaction"]["transaction_nonce"]
        == transaction[  # type: ignore[index]
            "transaction_nonce"
        ]
    )
    assert admission["reconciliation_digest"] == (
        "sha256:" + hashlib.sha256(_canonical(reconciliation)).hexdigest()
    )
    assert admission["containment_digest"] == (
        "sha256:" + hashlib.sha256(_canonical(reconciliation["containment"])).hexdigest()  # type: ignore[index]
    )


def _server_authorization_vector(*, recovery: bool) -> dict[str, object]:
    def digest(character: str) -> str:
        return "sha256:" + character * 64

    candidate = {
        "candidate_manifest_digest": digest("0"),
        "artifact_set_digest": digest("1"),
        "version": "1.2.3",
        "tag": "v1.2.3",
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "candidate_run_id": 101,
        "candidate_run_attempt": 1,
    }
    promotion_run = {
        "repository_id": 303,
        "workflow_id": 404,
        "workflow_path": ".github/workflows/release.yml",
        "run_id": 708 if recovery else 707,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "ref": "refs/tags/v1.2.3" if recovery else "refs/heads/main",
        "head_sha": "a" * 40,
        "workflow_sha": "a" * 40,
        "actor": {"login": "kestrel-release-dispatcher[bot]", "id": 202},
        "triggering_actor": {
            "login": "kestrel-release-dispatcher[bot]",
            "id": 202,
        },
        "transaction_nonce": ("fedcba9876543210" * 4 if recovery else "0123456789abcdef" * 4),
        "rest_observation_digest": digest("2"),
        "context_observation_digest": digest("3"),
    }
    return {
        "schema": "kestrel.release_server_authorization.v3",
        "authorization_kind": "execution" if recovery else "transaction",
        "mode": "recover_committed" if recovery else "initiate",
        "candidate": candidate,
        "promotion_run": promotion_run,
        "environment": {
            "name": "release",
            "id": 505,
            "policies_digest": digest("4"),
        },
        "approval_history": {
            "records": [
                {
                    "environment": {"name": "release", "id": 505},
                    "reviewer": {
                        "login": "John-MiracleWorker",
                        "id": 606,
                        "type": "User",
                    },
                    "state": "approved",
                    "observed_record_digest": digest("5"),
                }
            ],
            "complete_response_digest": digest("6"),
        },
        "admission_authority": {
            "receipt_digest": digest("7"),
            "signature_digest": digest("8"),
            "verification_digest": digest("9"),
        },
        "repository_state": {
            "repository_writers_observation_digest": digest("a"),
            "actions_authority_digest": digest("b"),
            "immutable_releases_observation_digest": digest("c"),
            "tag_ruleset_observation_digest": digest("d"),
            "ingress_observation_digest": digest("e"),
        },
        "bindings": {
            "transaction_authorization_digest": (
                "sha256:c9caa303f4b4fa484020d2d8ad7a0f1e9b858339ca3d8de1faa87c832ea06af0"
                if recovery
                else None
            ),
            "recovery_capsule_manifest_digest": digest("f") if recovery else None,
            "commit_marker_digest": digest("0") if recovery else None,
        },
        "authorized_at": ("2026-08-13T20:00:00Z" if recovery else "2026-08-12T20:00:00Z"),
        "evidence": {
            "source_bundle_digest": digest("1"),
            "canonicalization_vector_digest": (
                "sha256:7d37d1815caf0bb822d1244edcde67b2872cac94c9a6238d036198c55d196054"
            ),
        },
        "provenance": {
            "producer": "scripts/release_promotion_transaction.py",
            "provider": "github.com",
            "method": "server-observation-after-protected-environment",
        },
        "confidence": 1,
        "validation_status": "validated",
    }


@pytest.mark.parametrize(
    ("name", "recovery", "size", "digest"),
    [
        (
            "initiate.json",
            False,
            3166,
            "c9caa303f4b4fa484020d2d8ad7a0f1e9b858339ca3d8de1faa87c832ea06af0",
        ),
        (
            "recovery.json",
            True,
            3381,
            "184332a79bc964c29c9a3a2b9ba05afd8d1fcc19f3549fde0d21200be9242a91",
        ),
    ],
)
def test_server_authorization_known_answers_are_independently_reconstructed(
    name: str, recovery: bool, size: int, digest: str
) -> None:
    raw = _canonical(_server_authorization_vector(recovery=recovery))

    assert len(raw) == size
    assert hashlib.sha256(raw).hexdigest() == digest
    assert (VECTOR_ROOT / "server-authorization" / name).read_bytes() == raw
    schema = json.loads(
        (SCHEMA_ROOT / "kestrel.release_server_authorization.v3.schema.json").read_bytes()
    )
    jsonschema.Draft202012Validator(schema).validate(json.loads(raw))
