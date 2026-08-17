#!/usr/bin/env python3
"""Create and verify immutable Kestrel release-candidate evidence."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import tomllib
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast

import jsonschema  # type: ignore[import-untyped]
import rfc8785

CANDIDATE_SCHEMA = "kestrel.release_candidate.v1"
ARTIFACT_OBSERVATION_SCHEMA = "kestrel.actions_artifact_observation.v1"
OCI_DESCRIPTOR_SCHEMA = "kestrel.oci_descriptor.v1"
OCI_REPOSITORY = "ghcr.io/john-miracleworker/kestrel"
CANDIDATE_REPOSITORY = "John-MiracleWorker/Kestrel"
CANDIDATE_REF = "refs/heads/main"
CANDIDATE_WORKFLOW_PATH = ".github/workflows/release-candidate.yml"
PRODUCER = "scripts/release_candidate_manifest.py"
SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas"
CANONICALIZATION_VECTOR_DIGEST = (
    "sha256:7d37d1815caf0bb822d1244edcde67b2872cac94c9a6238d036198c55d196054"
)

SAFE_INTEGER_MIN = -9007199254740991
SAFE_INTEGER_MAX = 9007199254740991
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF_RE = re.compile(r"^refs/(heads|tags)/[A-Za-z0-9._/-]{1,200}$")
PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*$")
MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T"
    r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$"
)
HEX_BLOB_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_DEVICE_NAMES = frozenset(
    {
        "AUX",
        "CLOCK$",
        "CON",
        "CONIN$",
        "CONOUT$",
        "NUL",
        "PRN",
        *(f"COM{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
        *(f"LPT{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
    }
)

CHECK_NAMES = (
    "nine-row-exact-wheel",
    "oci-layout",
    "protected-main-ci",
    "release-payload",
    "release-rehearsal",
    "runtime-reliability-qualification",
)
CANDIDATE_RUN_CHECK_NAMES = frozenset(
    {"nine-row-exact-wheel", "oci-layout", "release-payload"}
)
CHECK_WORKFLOW_PATHS = {
    "nine-row-exact-wheel": CANDIDATE_WORKFLOW_PATH,
    "oci-layout": CANDIDATE_WORKFLOW_PATH,
    "protected-main-ci": ".github/workflows/ci.yml",
    "release-payload": CANDIDATE_WORKFLOW_PATH,
    "release-rehearsal": ".github/workflows/release-rehearsal.yml",
    "runtime-reliability-qualification": ".github/workflows/determinism.yml",
}
CHECK_RECEIPT_BASE_KEYS = frozenset(
    {
        "schema",
        "name",
        "status",
        "subject_sha",
        "run_id",
        "run_attempt",
        "workflow",
        "jobs",
        "artifacts",
        "evidence",
        "provenance",
        "confidence",
        "validation_status",
    }
)
CANDIDATE_CHECK_RECEIPT_KEYS = CHECK_RECEIPT_BASE_KEYS | frozenset(
    {"artifact_set_digest"}
)
CHECK_WORKFLOW_KEYS = frozenset(
    {
        "repository",
        "repository_id",
        "workflow_id",
        "workflow_path",
        "workflow_ref",
        "workflow_sha",
        "run_id",
        "run_attempt",
        "event",
        "head_sha",
        "status",
        "conclusion",
    }
)
CHECK_JOB_KEYS = frozenset(
    {
        "job_id",
        "name",
        "run_id",
        "run_attempt",
        "head_sha",
        "status",
        "conclusion",
    }
)
CHECK_ARTIFACT_KEYS = frozenset(
    {
        "artifact_id",
        "name",
        "api_digest",
        "size_bytes",
        "expired",
        "run_id",
        "run_attempt",
        "source_sha",
    }
)
CHECK_EVIDENCE_KEYS = frozenset(
    {
        "workflow_observation_digest",
        "jobs_observation_digest",
        "artifacts_observation_digest",
        "job_count",
        "artifact_count",
        "complete",
        "canonicalization_vector_digest",
    }
)
CHECK_PROVENANCE_KEYS = frozenset({"producer", "provider", "method"})
PLANNED_SURFACES = ("ghcr", "github_release", "github_tag", "pypi")
CANDIDATE_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "version",
        "tag",
        "source",
        "candidate_run",
        "checks",
        "attestation_subjects",
        "artifacts",
        "artifact_set_digest",
        "planned_surfaces",
        "evidence",
        "provenance",
        "confidence",
        "validation_status",
    }
)
CANDIDATE_OBJECT_PROPERTY_LIMITS = {
    "source": 6,
    "candidate_run": 5,
    "evidence": 2,
    "provenance": 3,
}
BUNDLE_TOP_LEVEL = (
    "attestations.json",
    "candidate-manifest.json",
    "containers",
    "qualification",
    "release",
    "source.tar",
)
EXPECTED_PLATFORMS = ("amd64", "arm64")
OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
    }
)
OCI_CONFIG_REQUIRED_KEYS = frozenset({"architecture", "os", "rootfs"})
OCI_CONFIG_ALLOWED_KEYS = OCI_CONFIG_REQUIRED_KEYS | frozenset(
    {
        "author",
        "config",
        "created",
        "history",
        "os.features",
        "os.version",
        "variant",
    }
)
OCI_LAYOUT_MARKER = {"imageLayoutVersion": "1.0.0"}
STREAM_CHUNK_BYTES = 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_PATH_BYTES = 4096
MAX_ARCHIVE_PATH_COMPONENTS = 256
MAX_ARCHIVE_PATH_COMPONENT_BYTES = 255
MAX_ARCHIVE_TOTAL_PATH_COMPONENTS = 65_536
MAX_SOURCE_PATH_COMPONENTS = MAX_ARCHIVE_TOTAL_PATH_COMPONENTS
MAX_ZIP_CENTRAL_DIRECTORY_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_ARTIFACTS = 512
MAX_ATTESTATION_SUBJECTS = MAX_CANDIDATE_ARTIFACTS + 1
MAX_CANDIDATE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_OCI_METADATA_BYTES = MAX_CANDIDATE_MANIFEST_BYTES
MAX_ACTIONS_OBSERVATION_BYTES = MAX_CANDIDATE_MANIFEST_BYTES
MAX_SOURCE_METADATA_BYTES = MAX_CANDIDATE_MANIFEST_BYTES
MAX_CHECKS_INPUT_BYTES = MAX_CANDIDATE_MANIFEST_BYTES
MAX_ATTESTATION_SUBJECTS_INPUT_BYTES = MAX_CANDIDATE_MANIFEST_BYTES
MAX_SOURCE_BUNDLE_ENTRY_BYTES = MAX_CANDIDATE_MANIFEST_BYTES
MAX_SOURCE_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_MEMBER_BYTES = MAX_SOURCE_ARCHIVE_BYTES
MAX_SOURCE_TOTAL_BYTES = MAX_SOURCE_ARCHIVE_BYTES
MAX_OCI_LAYER_UNCOMPRESSED_BYTES = MAX_ARCHIVE_TOTAL_BYTES
MAX_OCI_LAYER_COUNT = 128
MAX_OCI_TOTAL_UNCOMPRESSED_BYTES = MAX_ARCHIVE_TOTAL_BYTES
MAX_CHECK_JOBS = 256
MAX_CHECK_ARTIFACTS = 256
ARTIFACT_RETENTION_DAYS = 30
CANDIDATE_OBJECT_COLLECTION_LIMITS = {
    "checks": (len(CHECK_NAMES), 7, "check"),
    "attestation_subjects": (MAX_ATTESTATION_SUBJECTS, 3, "attestation subject"),
    "artifacts": (MAX_CANDIDATE_ARTIFACTS, 4, "artifact"),
}

JSONObject = dict[str, object]


class ReleaseCandidateError(ValueError):
    """Stable fail-closed validation error."""


def _validate_string(value: str, *, label: str) -> None:
    if unicodedata.normalize("NFC", value) != value:
        raise ReleaseCandidateError(f"{label} is not NFC normalized")
    for char in value:
        codepoint = ord(char)
        if codepoint == 0 or 0xD800 <= codepoint <= 0xDFFF:
            raise ReleaseCandidateError(f"{label} contains a forbidden codepoint")


def _validate_i_json(value: object, *, label: str) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not SAFE_INTEGER_MIN <= value <= SAFE_INTEGER_MAX:
            raise ReleaseCandidateError(f"{label} is outside the safe integer range")
        return
    if isinstance(value, float):
        raise ReleaseCandidateError(f"{label} must not be a float")
    if isinstance(value, str):
        _validate_string(value, label=label)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_i_json(item, label=f"{label}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReleaseCandidateError(f"{label} has a non-string key")
            _validate_string(key, label=f"{label} key")
            _validate_i_json(item, label=f"{label}.{key}")
        return
    raise ReleaseCandidateError(f"{label} has unsupported type {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """Return pinned RFC 8785 bytes for Kestrel's integer-only I-JSON subset."""

    _validate_i_json(value, label="value")
    return rfc8785.dumps(cast(Any, value))


def _reject_float(value: str) -> Any:
    raise ReleaseCandidateError(f"floats are forbidden: {value}")


def _reject_constant(value: str) -> Any:
    raise ReleaseCandidateError(f"non-finite numbers are forbidden: {value}")


def _parse_int(value: str) -> int:
    parsed = int(value)
    if not SAFE_INTEGER_MIN <= parsed <= SAFE_INTEGER_MAX:
        raise ReleaseCandidateError(f"integer is outside the safe range: {value}")
    return parsed


def _object_pairs(pairs: list[tuple[str, object]]) -> JSONObject:
    value: JSONObject = {}
    for key, item in pairs:
        if key in value:
            raise ReleaseCandidateError(f"duplicate object key: {key}")
        value[key] = item
    return value


def _parse_json(raw: bytes, *, label: str, canonical: bool) -> object:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ReleaseCandidateError(f"{label} has a UTF-8 BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseCandidateError(f"{label} is not valid UTF-8") from exc
    if canonical and text != text.strip():
        raise ReleaseCandidateError(f"{label} has leading or trailing whitespace")
    try:
        value = json.loads(
            text if canonical else text.strip(),
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            parse_int=_parse_int,
            object_pairs_hook=_object_pairs,
        )
    except json.JSONDecodeError as exc:
        raise ReleaseCandidateError(f"{label} is not valid JSON: {exc.msg}") from exc
    _validate_i_json(value, label=label)
    if canonical and canonical_json_bytes(value) != raw:
        raise ReleaseCandidateError(f"{label} is not byte-canonical")
    return value


def _object(value: object, *, label: str) -> JSONObject:
    if not isinstance(value, dict):
        raise ReleaseCandidateError(f"{label} must be an object")
    return cast(JSONObject, value)


def _array(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ReleaseCandidateError(f"{label} must be an array")
    return value


def _sha256(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _immutable_json_scalar(value: object, *, label: str) -> object:
    if value is None or type(value) in {bool, int, float, str}:
        return value
    raise ReleaseCandidateError(f"{label} must be a JSON scalar")


def _bounded_object_copy(
    value: Mapping[str, object], *, label: str, max_properties: int
) -> JSONObject:
    result: JSONObject = {}
    for count, key in enumerate(value, start=1):
        if count > max_properties:
            raise ReleaseCandidateError(f"{label} has too many properties")
        if not isinstance(key, str):
            raise ReleaseCandidateError(f"{label} property names must be strings")
        if key in result:
            raise ReleaseCandidateError(f"{label} has a duplicate property")
        result[key] = _immutable_json_scalar(
            value[key], label=f"{label} property {key}"
        )
    return result


def _bounded_object_sequence(
    values: Sequence[Mapping[str, object]],
    *,
    label: str,
    item_label: str,
    max_items: int,
    max_properties: int,
) -> list[JSONObject]:
    result: list[JSONObject] = []
    for count, item in enumerate(values, start=1):
        if count > max_items:
            raise ReleaseCandidateError(f"candidate has too many {label}")
        if not isinstance(item, Mapping):
            raise ReleaseCandidateError(f"{item_label} must be an object")
        result.append(
            _bounded_object_copy(
                item,
                label=item_label,
                max_properties=max_properties,
            )
        )
    return result


def _bounded_scalar_sequence(
    values: object,
    *,
    label: str,
    item_label: str,
    max_items: int,
) -> list[object]:
    if not isinstance(values, list):
        raise ReleaseCandidateError(f"{label} must be an array")
    result: list[object] = []
    for count, item in enumerate(values, start=1):
        if count > max_items:
            raise ReleaseCandidateError(f"candidate manifest has too many {label}")
        result.append(_immutable_json_scalar(item, label=item_label))
    return result


def _candidate_manifest_snapshot(
    manifest: Mapping[str, object], *, label: str
) -> JSONObject:
    result: JSONObject = {}
    for count, name in enumerate(manifest, start=1):
        if count > len(CANDIDATE_MANIFEST_KEYS):
            raise ReleaseCandidateError(f"{label} has too many properties")
        if not isinstance(name, str):
            raise ReleaseCandidateError(f"{label} property names must be strings")
        if name in result:
            raise ReleaseCandidateError(f"{label} has a duplicate property")
        if name not in CANDIDATE_MANIFEST_KEYS:
            raise ReleaseCandidateError(f"{label} has an unexpected property: {name}")
        value = manifest[name]
        object_limit = CANDIDATE_OBJECT_PROPERTY_LIMITS.get(name)
        if object_limit is not None:
            if not isinstance(value, Mapping):
                raise ReleaseCandidateError(f"{label} property {name} must be an object")
            result[name] = _bounded_object_copy(
                value,
                label=f"{label} property {name}",
                max_properties=object_limit,
            )
            continue
        collection_limits = CANDIDATE_OBJECT_COLLECTION_LIMITS.get(name)
        if collection_limits is not None:
            if not isinstance(value, list):
                raise ReleaseCandidateError(f"{label} property {name} must be an array")
            max_items, max_properties, item_label = collection_limits
            result[name] = _bounded_object_sequence(
                value,
                label=name.replace("_", " "),
                item_label=item_label,
                max_items=max_items,
                max_properties=max_properties,
            )
            continue
        if name == "planned_surfaces":
            result[name] = _bounded_scalar_sequence(
                value,
                label="planned surfaces",
                item_label="planned surface",
                max_items=len(PLANNED_SURFACES),
            )
            continue
        result[name] = _immutable_json_scalar(
            value, label=f"{label} property {name}"
        )
    return result


def _immutable_bounded_bytes(
    value: object, *, label: str, max_bytes: int | None = None
) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ReleaseCandidateError(f"{label} must be bytes")
    limit = MAX_SOURCE_BUNDLE_ENTRY_BYTES if max_bytes is None else max_bytes
    view = memoryview(value)
    try:
        if view.nbytes > limit:
            raise ReleaseCandidateError(f"{label} exceeds its size limit")
        return view.tobytes()
    finally:
        view.release()


def _bounded_binary_mapping(
    entries: Mapping[str, object], *, label: str, max_items: int
) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for count, name in enumerate(entries, start=1):
        if count > max_items:
            raise ReleaseCandidateError(f"{label} has too many entries")
        if not isinstance(name, str) or not name:
            raise ReleaseCandidateError(f"{label} entry name must be a nonempty string")
        if name in result:
            raise ReleaseCandidateError(f"{label} has a duplicate entry")
        result[name] = _immutable_bounded_bytes(
            entries[name], label=f"{label} entry {name}"
        )
    return result


def source_bundle_digest(entries: Mapping[str, bytes]) -> str:
    """Digest named source envelopes with an unambiguous length-framed format."""

    snapshot = _bounded_binary_mapping(
        cast(Mapping[str, object], entries),
        label="source bundle",
        max_items=len(CHECK_NAMES),
    )
    digest = hashlib.sha256()
    digest.update(b"Kestrel-Source-Bundle-v1\0")
    for name in sorted(snapshot, key=lambda item: item.encode("utf-8")):
        encoded = name.encode("utf-8")
        raw = snapshot[name]
        if len(encoded) > 0xFFFFFFFF or len(raw) > 0xFFFFFFFFFFFFFFFF:
            raise ReleaseCandidateError("source bundle entry exceeds framing limits")
        digest.update(struct.pack(">I", len(encoded)))
        digest.update(encoded)
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
    return f"sha256:{digest.hexdigest()}"


def artifact_set_digest(artifacts: Sequence[Mapping[str, object]]) -> str:
    snapshot = _bounded_object_sequence(
        artifacts,
        label="artifacts",
        item_label="artifact",
        max_items=MAX_CANDIDATE_ARTIFACTS,
        max_properties=4,
    )
    return _sha256(canonical_json_bytes(snapshot))


def candidate_manifest_digest(manifest: Mapping[str, object]) -> str:
    value = _candidate_manifest_snapshot(manifest, label="candidate manifest")
    _preflight_candidate_collections(value, label="candidate manifest")
    return _sha256(canonical_json_bytes(value))


def _schema(name: str) -> JSONObject:
    path = SCHEMA_ROOT / f"{name}.schema.json"
    value = _parse_json(
        _read_regular(
            path,
            label=str(path),
            max_bytes=MAX_CANDIDATE_MANIFEST_BYTES,
        ),
        label=str(path),
        canonical=False,
    )
    schema = _object(value, label=str(path))
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def _validate_schema(name: str, value: object, *, label: str) -> None:
    errors = sorted(
        jsonschema.Draft202012Validator(_schema(name)).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        raise ReleaseCandidateError(f"{label} fails schema validation: {errors[0].message}")


def _require_pattern(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ReleaseCandidateError(f"{label} has an invalid format")
    return value


def _positive_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReleaseCandidateError(f"{label} must be a positive integer")
    if not 1 <= value <= SAFE_INTEGER_MAX:
        raise ReleaseCandidateError(f"{label} must be a positive safe integer")
    return value


def _nonnegative_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReleaseCandidateError(f"{label} must be a nonnegative integer")
    if not 0 <= value <= SAFE_INTEGER_MAX:
        raise ReleaseCandidateError(f"{label} must be a nonnegative safe integer")
    return value


def _validate_manifest_semantics(manifest: JSONObject) -> None:
    version = _require_pattern(manifest.get("version"), VERSION_RE, label="version")
    if manifest.get("tag") != f"v{version}":
        raise ReleaseCandidateError("tag must be derived from version")
    source = _object(manifest.get("source"), label="source")
    commit_sha = _require_pattern(source.get("commit_sha"), SHA_RE, label="source commit")
    candidate_run = _object(manifest.get("candidate_run"), label="candidate_run")
    if candidate_run.get("workflow_ref") != CANDIDATE_REF:
        raise ReleaseCandidateError("candidate workflow ref must be protected main")
    if candidate_run.get("workflow_sha") != commit_sha:
        raise ReleaseCandidateError("workflow SHA must equal source commit SHA")
    candidate_run_id = candidate_run.get("run_id")

    checks = [_object(item, label="check") for item in _array(manifest.get("checks"), label="checks")]
    names = [str(item.get("name")) for item in checks]
    if names != list(CHECK_NAMES):
        raise ReleaseCandidateError("checks must be the exact sorted six-name set")
    for check in checks:
        name = str(check["name"])
        if check.get("subject_sha") != commit_sha:
            raise ReleaseCandidateError(f"check subject SHA mismatch: {name}")
        if name in CANDIDATE_RUN_CHECK_NAMES and check.get("run_id") != candidate_run_id:
            raise ReleaseCandidateError(f"candidate check run ID mismatch: {name}")
        if check.get("receipt_path") != f"qualification/receipts/{name}.json":
            raise ReleaseCandidateError(f"check receipt path mismatch: {name}")

    subjects = [
        _object(item, label="attestation subject")
        for item in _array(manifest.get("attestation_subjects"), label="attestation_subjects")
    ]
    if subjects != sorted(subjects, key=lambda item: (str(item["kind"]), str(item["name"]))):
        raise ReleaseCandidateError("attestation subjects are not sorted")
    subject_identities = [(str(item["kind"]), str(item["name"])) for item in subjects]
    if len(subject_identities) != len(set(subject_identities)):
        raise ReleaseCandidateError("attestation subject identity is duplicated")
    oci_subjects = [item for item in subjects if item.get("kind") == "oci_index"]
    if len(oci_subjects) != 1 or oci_subjects[0].get("name") != OCI_REPOSITORY:
        raise ReleaseCandidateError("the exact OCI index attestation subject is required")

    artifacts = [
        _object(item, label="artifact")
        for item in _array(manifest.get("artifacts"), label="artifacts")
    ]
    if artifacts != sorted(artifacts, key=lambda item: str(item["path"])):
        raise ReleaseCandidateError("artifacts are not sorted")
    artifact_paths = [str(item["path"]) for item in artifacts]
    if len(artifact_paths) != len(set(artifact_paths)):
        raise ReleaseCandidateError("artifact path identity is duplicated")
    expected_file_subjects = [
        {
            "kind": "file",
            "name": str(item["path"]),
            "digest": str(item["sha256"]),
        }
        for item in artifacts
        if str(item["path"]).startswith("release/")
    ]
    actual_file_subjects = [item for item in subjects if item.get("kind") == "file"]
    if actual_file_subjects != expected_file_subjects:
        raise ReleaseCandidateError(
            "file attestation subjects do not match release artifacts"
        )
    if artifact_set_digest(artifacts) != manifest.get("artifact_set_digest"):
        raise ReleaseCandidateError("artifact_set_digest does not match artifacts")
    if manifest.get("planned_surfaces") != sorted(PLANNED_SURFACES):
        raise ReleaseCandidateError("planned surfaces are not the exact fixed set")


def _preflight_candidate_collections(value: JSONObject, *, label: str) -> None:
    limits = {
        "checks": len(CHECK_NAMES),
        "attestation_subjects": MAX_ATTESTATION_SUBJECTS,
        "artifacts": MAX_CANDIDATE_ARTIFACTS,
        "planned_surfaces": len(PLANNED_SURFACES),
    }
    for name, limit in limits.items():
        collection = value.get(name)
        if isinstance(collection, list) and len(collection) > limit:
            readable = name.replace("_", " ")
            raise ReleaseCandidateError(f"{label} has too many {readable}")


def _validated_manifest(value: object, *, label: str) -> JSONObject:
    manifest = _object(value, label=label)
    _preflight_candidate_collections(manifest, label=label)
    _validate_schema(CANDIDATE_SCHEMA, manifest, label=label)
    _validate_manifest_semantics(manifest)
    return manifest


def build_candidate_manifest(
    *,
    version: str,
    repository: str,
    repository_id: int,
    commit_sha: str,
    tree_sha: str,
    archive_sha256: str,
    archive_size_bytes: int,
    workflow_id: int,
    workflow_ref: str,
    workflow_sha: str,
    run_id: int,
    run_attempt: int,
    checks: Sequence[Mapping[str, object]],
    attestation_subjects: Sequence[Mapping[str, object]],
    artifacts: Sequence[Mapping[str, object]],
    check_receipts: Mapping[str, bytes],
) -> JSONObject:
    """Construct the immutable candidate-time record."""

    copied_checks = _bounded_object_sequence(
        checks,
        label="checks",
        item_label="check",
        max_items=len(CHECK_NAMES),
        max_properties=7,
    )
    check_names = tuple(item.get("name") for item in copied_checks)
    if check_names != CHECK_NAMES:
        raise ReleaseCandidateError("checks must be the exact sorted six-name set")
    receipt_snapshot = _bounded_binary_mapping(
        cast(Mapping[str, object], check_receipts),
        label="check receipt mapping",
        max_items=len(CHECK_NAMES),
    )
    if set(receipt_snapshot) != set(CHECK_NAMES):
        raise ReleaseCandidateError("check receipt mapping is not the exact six-name set")
    copied_subjects = _bounded_object_sequence(
        attestation_subjects,
        label="attestation subjects",
        item_label="attestation subject",
        max_items=MAX_ATTESTATION_SUBJECTS,
        max_properties=3,
    )
    copied_artifacts = _bounded_object_sequence(
        artifacts,
        label="artifacts",
        item_label="artifact",
        max_items=MAX_CANDIDATE_ARTIFACTS,
        max_properties=4,
    )

    _require_pattern(version, VERSION_RE, label="version")
    _require_pattern(repository, REPOSITORY_RE, label="repository")
    _require_pattern(commit_sha, SHA_RE, label="commit SHA")
    _require_pattern(tree_sha, SHA_RE, label="tree SHA")
    _require_pattern(archive_sha256, DIGEST_RE, label="archive digest")
    _require_pattern(workflow_ref, REF_RE, label="workflow ref")
    _require_pattern(workflow_sha, SHA_RE, label="workflow SHA")
    if workflow_sha != commit_sha:
        raise ReleaseCandidateError("workflow SHA must equal source commit SHA")
    if isinstance(run_attempt, bool) or run_attempt != 1:
        raise ReleaseCandidateError("candidate workflow run attempt must be 1")
    manifest: JSONObject = {
        "schema": CANDIDATE_SCHEMA,
        "version": version,
        "tag": f"v{version}",
        "source": {
            "repository": repository,
            "repository_id": _positive_integer(repository_id, label="repository ID"),
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "archive_sha256": archive_sha256,
            "size_bytes": _positive_integer(archive_size_bytes, label="archive size"),
        },
        "candidate_run": {
            "workflow_id": _positive_integer(workflow_id, label="workflow ID"),
            "workflow_ref": workflow_ref,
            "workflow_sha": workflow_sha,
            "run_id": _positive_integer(run_id, label="run ID"),
            "run_attempt": run_attempt,
        },
        "checks": copied_checks,
        "attestation_subjects": copied_subjects,
        "artifacts": copied_artifacts,
        "artifact_set_digest": _sha256(canonical_json_bytes(copied_artifacts)),
        "planned_surfaces": sorted(PLANNED_SURFACES),
        "evidence": {
            "source_bundle_digest": source_bundle_digest(receipt_snapshot),
            "canonicalization_vector_digest": CANONICALIZATION_VECTOR_DIGEST,
        },
        "provenance": {
            "producer": PRODUCER,
            "provider": "github.com",
            "method": "candidate-run-finalization",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    checked = _validated_manifest(manifest, label="candidate manifest")
    for item in _array(checked["checks"], label="checks"):
        check = _object(item, label="check")
        name = str(check["name"])
        if _sha256(receipt_snapshot[name]) != check["receipt_sha256"]:
            raise ReleaseCandidateError(f"check receipt digest mismatch: {name}")
        _validate_check_receipt(
            receipt_snapshot[name],
            check,
            expected_artifact_set_digest=str(checked["artifact_set_digest"]),
            expected_repository=repository,
            expected_repository_id=repository_id,
            expected_candidate_workflow_id=workflow_id,
        )
    return checked


def load_candidate_manifest(path: Path) -> JSONObject:
    value = _parse_json(
        _read_regular(
            path,
            label="candidate manifest",
            max_bytes=MAX_CANDIDATE_MANIFEST_BYTES,
        ),
        label=str(path),
        canonical=True,
    )
    return _validated_manifest(value, label=str(path))


def _open_regular(path: Path, *, label: str) -> BinaryIO:
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise ReleaseCandidateError(f"{label} is not an accessible regular file: {path}") from exc
    if not stat.S_ISREG(path_info.st_mode):
        raise ReleaseCandidateError(f"{label} is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseCandidateError(f"{label} is not an accessible regular file: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or _file_signature(info) != _file_signature(path_info):
            raise ReleaseCandidateError(f"{label} is not a regular file: {path}")
        return cast(BinaryIO, os.fdopen(descriptor, "rb"))
    except BaseException:
        os.close(descriptor)
        raise


def _file_signature(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _assert_unchanged(handle: BinaryIO, before: os.stat_result, *, label: str) -> None:
    if _file_signature(os.fstat(handle.fileno())) != _file_signature(before):
        raise ReleaseCandidateError(f"{label} changed while it was being read")


def _read_regular(
    path: Path,
    *,
    label: str,
    allow_empty: bool = False,
    max_bytes: int | None = None,
) -> bytes:
    with _open_regular(path, label=label) as handle:
        before = os.fstat(handle.fileno())
        if max_bytes is not None and before.st_size > max_bytes:
            raise ReleaseCandidateError(f"{label} exceeds its size limit")
        raw = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        _assert_unchanged(handle, before, label=label)
    if max_bytes is not None and len(raw) > max_bytes:
        raise ReleaseCandidateError(f"{label} exceeds its size limit")
    if not raw and not allow_empty:
        raise ReleaseCandidateError(f"{label} is empty: {path}")
    return raw


def _file_identity(
    path: Path,
    *,
    label: str,
    allow_empty: bool = False,
    max_bytes: int | None = None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _open_regular(path, label=label) as handle:
        before = os.fstat(handle.fileno())
        if max_bytes is not None and before.st_size > max_bytes:
            raise ReleaseCandidateError(f"{label} exceeds its size limit")
        while chunk := handle.read(STREAM_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise ReleaseCandidateError(f"{label} exceeds its size limit")
        _assert_unchanged(handle, before, label=label)
    if size == 0 and not allow_empty:
        raise ReleaseCandidateError(f"{label} is empty: {path}")
    return f"sha256:{digest.hexdigest()}", size


def _require_real_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ReleaseCandidateError(f"{label} is missing: {path}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ReleaseCandidateError(f"{label} is not a real directory: {path}")


def _require_exact_directory_entries(
    path: Path, expected: set[str], *, label: str
) -> None:
    _require_real_directory(path, label=label)
    actual = {entry.name for entry in path.iterdir()}
    if actual != expected:
        raise ReleaseCandidateError(
            f"{label} inventory mismatch: expected {sorted(expected)}, found {sorted(actual)}"
        )


def _walk_regular_files(
    root: Path,
    *,
    label: str,
    require_artifact_path: bool = True,
    max_entries: int = MAX_ARCHIVE_MEMBERS,
) -> dict[str, Path]:
    _require_real_directory(root, label=label)
    result: dict[str, Path] = {}
    portable_identities = _PortablePathIndex()
    pending = [root]
    entry_count = 0
    while pending:
        directory = pending.pop()
        for entry in os.scandir(directory):
            entry_count += 1
            if entry_count > max_entries:
                raise ReleaseCandidateError(f"{label} has too many entries")
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if require_artifact_path and PATH_RE.fullmatch(relative) is None:
                raise ReleaseCandidateError(f"{label} has an unsafe path: {relative}")
            parts = _safe_archive_path(relative, label=label)
            _record_portable_path(parts, portable_identities, label=label)
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                result[relative] = path
            else:
                raise ReleaseCandidateError(f"{label} has a non-regular entry: {relative}")
    return result


def _media_type(relative: str) -> str:
    if relative.endswith(".tar.gz"):
        return "application/gzip"
    if relative.endswith(".json"):
        return "application/json"
    if relative.endswith(".tar"):
        return "application/x-tar"
    if relative.endswith((".whl", ".zip")):
        return "application/zip"
    if relative.endswith((".txt", ".md")) or relative.endswith("SHA256SUMS"):
        return "text/plain"
    return "application/octet-stream"


def _artifact_inventory(bundle_root: Path) -> list[JSONObject]:
    artifacts: list[JSONObject] = []
    for top in ("release", "containers"):
        files = _walk_regular_files(
            bundle_root / top,
            label=top,
            max_entries=MAX_CANDIDATE_ARTIFACTS,
        )
        for relative, path in files.items():
            if len(artifacts) >= MAX_CANDIDATE_ARTIFACTS:
                raise ReleaseCandidateError("candidate has too many artifacts")
            full_relative = f"{top}/{relative}"
            digest, size = _file_identity(path, label="artifact")
            media_type = _media_type(full_relative)
            if MEDIA_TYPE_RE.fullmatch(media_type) is None:
                raise ReleaseCandidateError(f"invalid media type for {full_relative}")
            artifacts.append(
                {
                    "path": full_relative,
                    "media_type": media_type,
                    "sha256": digest,
                    "size_bytes": size,
                }
            )
    return sorted(artifacts, key=lambda item: str(item["path"]))


def _verify_bundle_layout(bundle_root: Path, *, manifest_optional: bool) -> None:
    _require_real_directory(bundle_root, label="bundle root")
    entries = sorted(path.name for path in bundle_root.iterdir())
    expected = list(BUNDLE_TOP_LEVEL)
    if manifest_optional:
        expected.remove("candidate-manifest.json")
        if entries == sorted(expected):
            return
    if entries != sorted(BUNDLE_TOP_LEVEL):
        raise ReleaseCandidateError(
            f"bundle top-level layout mismatch: expected {sorted(BUNDLE_TOP_LEVEL)}, found {entries}"
        )
    for name in BUNDLE_TOP_LEVEL:
        path = bundle_root / name
        if name in {"release", "containers", "qualification"}:
            _require_real_directory(path, label=name)
        elif name == "source.tar":
            _file_identity(path, label=name, max_bytes=MAX_SOURCE_ARCHIVE_BYTES)
        elif name in {"candidate-manifest.json", "attestations.json"}:
            _read_regular(path, label=name, max_bytes=MAX_CANDIDATE_MANIFEST_BYTES)
        else:
            raise ReleaseCandidateError(f"unexpected bundle entry: {name}")


def _validate_check_receipt(
    raw: bytes,
    check: Mapping[str, object],
    *,
    expected_artifact_set_digest: str,
    expected_repository: str,
    expected_repository_id: int,
    expected_candidate_workflow_id: int,
) -> JSONObject:
    name = str(check["name"])
    receipt = _object(
        _parse_json(raw, label=f"receipt {name}", canonical=True),
        label=f"receipt {name}",
    )
    expected_schema = f"kestrel.check.{name}.v1"
    expected_keys = (
        CANDIDATE_CHECK_RECEIPT_KEYS
        if name in CANDIDATE_RUN_CHECK_NAMES
        else CHECK_RECEIPT_BASE_KEYS
    )
    if set(receipt) != expected_keys:
        raise ReleaseCandidateError(f"receipt identity mismatch for {name}")
    run_id = _positive_integer(receipt.get("run_id"), label=f"receipt {name} run ID")
    run_attempt = receipt.get("run_attempt")
    if (
        receipt.get("schema") != expected_schema
        or receipt.get("name") != name
        or receipt.get("status") != check["status"]
        or receipt.get("subject_sha") != check["subject_sha"]
        or run_id != check["run_id"]
        or isinstance(run_attempt, bool)
        or run_attempt != check["run_attempt"]
    ):
        raise ReleaseCandidateError(f"receipt identity mismatch for {name}")
    if name in CANDIDATE_RUN_CHECK_NAMES:
        artifact_set = _require_pattern(
            receipt.get("artifact_set_digest"),
            DIGEST_RE,
            label=f"receipt {name} artifact set digest",
        )
        if artifact_set != expected_artifact_set_digest:
            raise ReleaseCandidateError(
                f"receipt artifact set mismatch for {name}"
            )

    subject_sha = str(check["subject_sha"])
    workflow = _object(receipt.get("workflow"), label=f"receipt {name} workflow")
    if set(workflow) != CHECK_WORKFLOW_KEYS:
        raise ReleaseCandidateError(f"receipt workflow identity mismatch for {name}")
    workflow_id = _positive_integer(
        workflow.get("workflow_id"), label=f"receipt {name} workflow ID"
    )
    workflow_run_id = _positive_integer(
        workflow.get("run_id"), label=f"receipt {name} workflow run ID"
    )
    workflow_repository_id = _positive_integer(
        workflow.get("repository_id"), label=f"receipt {name} repository ID"
    )
    candidate_run_receipt = name in CANDIDATE_RUN_CHECK_NAMES
    expected_event = "workflow_dispatch" if candidate_run_receipt else "push"
    expected_workflow_status = (
        "in_progress" if candidate_run_receipt else "completed"
    )
    expected_workflow_conclusion: str | None = (
        None if candidate_run_receipt else "success"
    )
    if (
        workflow.get("repository") != expected_repository
        or workflow_repository_id != expected_repository_id
        or workflow.get("workflow_path") != CHECK_WORKFLOW_PATHS[name]
        or workflow.get("workflow_ref") != CANDIDATE_REF
        or workflow.get("workflow_sha") != subject_sha
        or workflow_run_id != run_id
        or isinstance(workflow.get("run_attempt"), bool)
        or workflow.get("run_attempt") != run_attempt
        or workflow.get("event") != expected_event
        or workflow.get("head_sha") != subject_sha
        or workflow.get("status") != expected_workflow_status
        or workflow.get("conclusion") != expected_workflow_conclusion
        or (
            candidate_run_receipt
            and workflow_id != expected_candidate_workflow_id
        )
    ):
        raise ReleaseCandidateError(f"receipt workflow identity mismatch for {name}")

    job_values = _array(receipt.get("jobs"), label=f"receipt {name} jobs")
    if not job_values or len(job_values) > MAX_CHECK_JOBS:
        raise ReleaseCandidateError(f"receipt job inventory mismatch for {name}")
    jobs = [
        _object(value, label=f"receipt {name} job") for value in job_values
    ]
    job_ids: set[int] = set()
    job_order: list[tuple[str, int]] = []
    successful_jobs = 0
    for job in jobs:
        if set(job) != CHECK_JOB_KEYS:
            raise ReleaseCandidateError(f"receipt job identity mismatch for {name}")
        job_id = _positive_integer(
            job.get("job_id"), label=f"receipt {name} job ID"
        )
        job_name = job.get("name")
        if not isinstance(job_name, str) or not job_name:
            raise ReleaseCandidateError(f"receipt job identity mismatch for {name}")
        if job_id in job_ids:
            raise ReleaseCandidateError(f"receipt job inventory mismatch for {name}")
        job_ids.add(job_id)
        job_order.append((job_name, job_id))
        conclusion = job.get("conclusion")
        if conclusion == "success":
            successful_jobs += 1
        if (
            _positive_integer(
                job.get("run_id"), label=f"receipt {name} job run ID"
            )
            != run_id
            or isinstance(job.get("run_attempt"), bool)
            or job.get("run_attempt") != run_attempt
            or job.get("head_sha") != subject_sha
            or job.get("status") != "completed"
            or conclusion not in {"success", "skipped"}
        ):
            raise ReleaseCandidateError(f"receipt job identity mismatch for {name}")
    if job_order != sorted(job_order) or successful_jobs == 0:
        raise ReleaseCandidateError(f"receipt job inventory mismatch for {name}")

    artifact_values = _array(
        receipt.get("artifacts"), label=f"receipt {name} artifacts"
    )
    if len(artifact_values) > MAX_CHECK_ARTIFACTS or (
        name != "protected-main-ci" and not artifact_values
    ):
        raise ReleaseCandidateError(f"receipt artifact inventory mismatch for {name}")
    artifacts = [
        _object(value, label=f"receipt {name} artifact")
        for value in artifact_values
    ]
    artifact_ids: set[int] = set()
    artifact_names: set[str] = set()
    artifact_order: list[tuple[str, int]] = []
    for artifact in artifacts:
        if set(artifact) != CHECK_ARTIFACT_KEYS:
            raise ReleaseCandidateError(
                f"receipt artifact identity mismatch for {name}"
            )
        artifact_id = _positive_integer(
            artifact.get("artifact_id"), label=f"receipt {name} artifact ID"
        )
        artifact_name = artifact.get("name")
        if not isinstance(artifact_name, str) or not artifact_name:
            raise ReleaseCandidateError(
                f"receipt artifact identity mismatch for {name}"
            )
        if artifact_id in artifact_ids or artifact_name in artifact_names:
            raise ReleaseCandidateError(
                f"receipt artifact inventory mismatch for {name}"
            )
        artifact_ids.add(artifact_id)
        artifact_names.add(artifact_name)
        artifact_order.append((artifact_name, artifact_id))
        _require_pattern(
            artifact.get("api_digest"),
            DIGEST_RE,
            label=f"receipt {name} artifact digest",
        )
        _positive_integer(
            artifact.get("size_bytes"), label=f"receipt {name} artifact size"
        )
        if (
            artifact.get("expired") is not False
            or _positive_integer(
                artifact.get("run_id"),
                label=f"receipt {name} artifact run ID",
            )
            != run_id
            or isinstance(artifact.get("run_attempt"), bool)
            or artifact.get("run_attempt") != run_attempt
            or artifact.get("source_sha") != subject_sha
        ):
            raise ReleaseCandidateError(
                f"receipt artifact identity mismatch for {name}"
            )
    if artifact_order != sorted(artifact_order):
        raise ReleaseCandidateError(f"receipt artifact inventory mismatch for {name}")

    evidence = _object(receipt.get("evidence"), label=f"receipt {name} evidence")
    if set(evidence) != CHECK_EVIDENCE_KEYS:
        raise ReleaseCandidateError(f"receipt evidence mismatch for {name}")
    for field in (
        "workflow_observation_digest",
        "jobs_observation_digest",
        "artifacts_observation_digest",
    ):
        _require_pattern(
            evidence.get(field),
            DIGEST_RE,
            label=f"receipt {name} {field}",
        )
    if (
        _nonnegative_integer(
            evidence.get("job_count"), label=f"receipt {name} job count"
        )
        != len(jobs)
        or _nonnegative_integer(
            evidence.get("artifact_count"),
            label=f"receipt {name} artifact count",
        )
        != len(artifacts)
        or evidence.get("complete") is not True
        or evidence.get("canonicalization_vector_digest")
        != CANONICALIZATION_VECTOR_DIGEST
    ):
        raise ReleaseCandidateError(f"receipt evidence mismatch for {name}")

    provenance = _object(
        receipt.get("provenance"), label=f"receipt {name} provenance"
    )
    if set(provenance) != CHECK_PROVENANCE_KEYS or provenance != {
        "producer": CANDIDATE_WORKFLOW_PATH,
        "provider": "github.com",
        "method": "actions-check-observation",
    }:
        raise ReleaseCandidateError(f"receipt provenance mismatch for {name}")
    if (
        isinstance(receipt.get("confidence"), bool)
        or receipt.get("confidence") != 1
        or receipt.get("validation_status") != "validated"
    ):
        raise ReleaseCandidateError(f"receipt validation mismatch for {name}")
    return receipt


def _verify_qualification_layout(
    bundle_root: Path,
    checks: Sequence[Mapping[str, object]],
    *,
    expected_artifact_set_digest: str,
    expected_repository: str,
    expected_repository_id: int,
    expected_candidate_workflow_id: int,
) -> dict[str, bytes]:
    qualification = bundle_root / "qualification"
    _require_real_directory(qualification, label="qualification")
    entries = list(qualification.iterdir())
    if len(entries) != 1 or entries[0].name != "receipts":
        raise ReleaseCandidateError("qualification must contain only receipts/")
    receipt_root = entries[0]
    files = _walk_regular_files(receipt_root, label="qualification receipts")
    expected_names = {f"{name}.json" for name in CHECK_NAMES}
    if set(files) != expected_names:
        raise ReleaseCandidateError("qualification receipt inventory is not exact")
    receipts: dict[str, bytes] = {}
    for check in checks:
        name = str(check["name"])
        relative = str(check["receipt_path"])
        if relative != f"qualification/receipts/{name}.json":
            raise ReleaseCandidateError(f"receipt path is not exact for {name}")
        raw = _read_regular(
            bundle_root / relative,
            label=f"receipt {name}",
            max_bytes=MAX_SOURCE_BUNDLE_ENTRY_BYTES,
        )
        if _sha256(raw) != check["receipt_sha256"]:
            raise ReleaseCandidateError(f"receipt digest mismatch for {name}")
        _validate_check_receipt(
            raw,
            check,
            expected_artifact_set_digest=expected_artifact_set_digest,
            expected_repository=expected_repository,
            expected_repository_id=expected_repository_id,
            expected_candidate_workflow_id=expected_candidate_workflow_id,
        )
        receipts[name] = raw
    return receipts


def _descriptor_object(value: object, *, label: str) -> JSONObject:
    descriptor = _object(value, label=label)
    if set(descriptor) != {"mediaType", "digest", "size"}:
        raise ReleaseCandidateError(f"{label} fields are not exact")
    _require_pattern(descriptor.get("mediaType"), MEDIA_TYPE_RE, label=f"{label} mediaType")
    _require_pattern(descriptor.get("digest"), DIGEST_RE, label=f"{label} digest")
    _positive_integer(descriptor.get("size"), label=f"{label} size")
    return descriptor


def _blob_path(blob_root: Path, digest: str) -> Path:
    return blob_root / digest.removeprefix("sha256:")


def _read_digest_bound(
    path: Path,
    digest: str,
    *,
    label: str,
    max_bytes: int,
    expected_size: int | None = None,
) -> bytes:
    if expected_size is not None and expected_size > max_bytes:
        raise ReleaseCandidateError(f"{label} exceeds its size limit")
    raw = _read_regular(path, label=label, max_bytes=max_bytes)
    if _sha256(raw) != digest:
        raise ReleaseCandidateError(f"{label} digest mismatch")
    if expected_size is not None and len(raw) != expected_size:
        raise ReleaseCandidateError(f"{label} descriptor size mismatch")
    return raw


def _copy_regular_to_snapshot(
    path: Path,
    snapshot: BinaryIO,
    *,
    label: str,
    max_bytes: int | None = None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _open_regular(path, label=label) as source:
        before = os.fstat(source.fileno())
        if max_bytes is not None and before.st_size > max_bytes:
            raise ReleaseCandidateError(f"{label} exceeds its size limit")
        while chunk := source.read(STREAM_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise ReleaseCandidateError(f"{label} exceeds its size limit")
            snapshot.write(chunk)
        _assert_unchanged(source, before, label=label)
    snapshot.flush()
    snapshot.seek(0)
    return f"sha256:{digest.hexdigest()}", size


def _stream_digest(
    handle: BinaryIO,
    *,
    max_bytes: int,
    label: str,
    output: BinaryIO | None = None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(STREAM_CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
        if size > max_bytes:
            raise ReleaseCandidateError(f"{label} exceeds its expanded size limit")
        if output is not None:
            output.write(chunk)
    if output is not None:
        output.flush()
        output.seek(0)
    return f"sha256:{digest.hexdigest()}", size


def _validate_oci_layer_tar(snapshot: BinaryIO, *, observed_size: int) -> None:
    if (
        observed_size < tarfile.BLOCKSIZE * 2
        or observed_size % tarfile.BLOCKSIZE != 0
    ):
        raise ReleaseCandidateError("OCI layer is not a complete tar archive")
    snapshot.seek(0)
    try:
        archive = tarfile.open(fileobj=snapshot, mode="r:")
    except tarfile.TarError as exc:
        raise ReleaseCandidateError("OCI layer is not a valid tar archive") from exc
    try:
        with archive:
            for count, _member in enumerate(archive, start=1):
                if count > MAX_ARCHIVE_MEMBERS:
                    raise ReleaseCandidateError("OCI layer tar archive has too many members")
            end_offset = archive.offset
    except (EOFError, OSError, tarfile.TarError) as exc:
        raise ReleaseCandidateError("OCI layer is not a valid tar archive") from exc
    if (
        end_offset < 0
        or end_offset % tarfile.BLOCKSIZE != 0
        or observed_size - end_offset < tarfile.BLOCKSIZE * 2
    ):
        raise ReleaseCandidateError("OCI layer is not a complete tar archive")
    snapshot.seek(end_offset)
    while trailing := snapshot.read(STREAM_CHUNK_BYTES):
        if any(trailing):
            raise ReleaseCandidateError(
                "OCI layer tar archive has nonzero trailing bytes"
            )


def _verify_layer_blob(
    path: Path,
    descriptor: Mapping[str, object],
    *,
    expected_diff_id: str,
    max_uncompressed_bytes: int | None = None,
    expansion_label: str = "OCI layer",
) -> int:
    layer_digest = _require_pattern(descriptor.get("digest"), DIGEST_RE, label="layer digest")
    layer_size = _positive_integer(descriptor.get("size"), label="layer size")
    media_type = str(descriptor.get("mediaType"))
    if media_type not in OCI_LAYER_MEDIA_TYPES:
        raise ReleaseCandidateError("OCI layer media type is not supported")
    expansion_limit = MAX_OCI_LAYER_UNCOMPRESSED_BYTES
    if max_uncompressed_bytes is not None:
        expansion_limit = min(expansion_limit, max_uncompressed_bytes)
    if expansion_limit < 0:
        raise ReleaseCandidateError(f"{expansion_label} exceeds its expanded size limit")
    with tempfile.TemporaryFile(mode="w+b") as snapshot:
        observed_digest, observed_size = _copy_regular_to_snapshot(
            path,
            cast(BinaryIO, snapshot),
            label="OCI layer",
            max_bytes=min(layer_size, MAX_ARCHIVE_MEMBER_BYTES),
        )
        if observed_digest != layer_digest:
            raise ReleaseCandidateError("OCI layer digest mismatch")
        if observed_size != layer_size:
            raise ReleaseCandidateError("OCI layer descriptor size mismatch")
        with tempfile.TemporaryFile(mode="w+b") as uncompressed_snapshot:
            try:
                if media_type.endswith("+gzip"):
                    with gzip.GzipFile(fileobj=snapshot, mode="rb") as uncompressed:
                        diff_id, uncompressed_size = _stream_digest(
                            cast(BinaryIO, uncompressed),
                            max_bytes=expansion_limit,
                            label=expansion_label,
                            output=cast(BinaryIO, uncompressed_snapshot),
                        )
                else:
                    diff_id, uncompressed_size = _stream_digest(
                        cast(BinaryIO, snapshot),
                        max_bytes=expansion_limit,
                        label=expansion_label,
                        output=cast(BinaryIO, uncompressed_snapshot),
                    )
            except (EOFError, OSError) as exc:
                raise ReleaseCandidateError("OCI layer compression is invalid") from exc
            _validate_oci_layer_tar(
                cast(BinaryIO, uncompressed_snapshot), observed_size=uncompressed_size
            )
    if diff_id != expected_diff_id:
        raise ReleaseCandidateError("OCI layer diff ID mismatch")
    return uncompressed_size


def _parse_canonical_object(raw: bytes, *, label: str) -> JSONObject:
    return _object(_parse_json(raw, label=label, canonical=True), label=label)


OCIArchiveEntry = tuple[bytes | None, Path | None, str, int]


def _compare_oci_archive_member(
    source: BinaryIO,
    *,
    expected_raw: bytes | None,
    expected_path: Path | None,
    expected_digest: str,
    expected_size: int,
) -> None:
    if (expected_raw is None) == (expected_path is None):
        raise ReleaseCandidateError("OCI archive comparison source is invalid")

    def compare(expected: BinaryIO) -> None:
        digest = hashlib.sha256()
        observed_size = 0
        while True:
            observed = source.read(STREAM_CHUNK_BYTES)
            wanted = expected.read(STREAM_CHUNK_BYTES)
            if observed != wanted:
                raise ReleaseCandidateError(
                    "OCI platform archive does not match the verified OCI graph"
                )
            if not observed:
                break
            digest.update(observed)
            observed_size += len(observed)
        if (
            observed_size != expected_size
            or f"sha256:{digest.hexdigest()}" != expected_digest
        ):
            raise ReleaseCandidateError(
                "OCI platform archive does not match the verified OCI graph"
            )

    if expected_path is None:
        compare(cast(BinaryIO, io.BytesIO(cast(bytes, expected_raw))))
        return
    with _open_regular(expected_path, label="OCI graph blob") as expected:
        before = os.fstat(expected.fileno())
        compare(expected)
        _assert_unchanged(expected, before, label="OCI graph blob")


def _validate_tar_member_structure(
    snapshot: BinaryIO,
    member: tarfile.TarInfo,
    *,
    expected_offset: int,
) -> int:
    if (
        member.offset != expected_offset
        or member.offset_data != member.offset + tarfile.BLOCKSIZE
    ):
        raise ReleaseCandidateError(
            "OCI platform archive has hidden or unsupported structural records"
        )
    data_end = member.offset_data + member.size
    padded_end = (
        (data_end + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE
    ) * tarfile.BLOCKSIZE
    snapshot.seek(data_end)
    padding = snapshot.read(padded_end - data_end)
    if len(padding) != padded_end - data_end or any(padding):
        raise ReleaseCandidateError(
            "OCI platform archive has nonzero or incomplete member padding"
        )
    return padded_end


def _verify_oci_platform_archive(
    path: Path,
    *,
    expected_archive_digest: str,
    expected_files: Mapping[str, OCIArchiveEntry],
) -> None:
    with tempfile.TemporaryFile(mode="w+b") as snapshot:
        observed_digest, _observed_size = _copy_regular_to_snapshot(
            path,
            cast(BinaryIO, snapshot),
            label="OCI platform archive",
            max_bytes=MAX_ARCHIVE_TOTAL_BYTES,
        )
        if observed_digest != expected_archive_digest:
            raise ReleaseCandidateError("OCI platform archive digest mismatch")
        try:
            archive = tarfile.open(fileobj=snapshot, mode="r:")
        except tarfile.TarError as exc:
            raise ReleaseCandidateError(
                "OCI platform archive is not a valid tar for the verified OCI graph"
            ) from exc
        try:
            with archive:
                if archive.pax_headers:
                    raise ReleaseCandidateError(
                        "OCI platform archive has unsupported global headers"
                    )
                seen: set[str] = set()
                files: set[str] = set()
                directories: set[str] = set()
                portable_identities = _PortablePathIndex()
                total_size = 0
                total_path_components = 0
                raw_offset = 0
                for count, member in enumerate(archive, start=1):
                    if count > MAX_ARCHIVE_MEMBERS:
                        raise ReleaseCandidateError(
                            "OCI platform archive has too many members"
                        )
                    parts = _safe_archive_path(
                        member.name.rstrip("/"), label="OCI platform archive"
                    )
                    total_path_components += len(parts)
                    if total_path_components > MAX_ARCHIVE_TOTAL_PATH_COMPONENTS:
                        raise ReleaseCandidateError(
                            "OCI platform archive exceeds its cumulative path component limit"
                        )
                    _record_portable_path(
                        parts,
                        portable_identities,
                        label="OCI platform archive",
                    )
                    relative = "/".join(parts)
                    if relative in seen:
                        raise ReleaseCandidateError(
                            "OCI platform archive has a duplicate member"
                        )
                    seen.add(relative)
                    if member.pax_headers or member.sparse is not None:
                        raise ReleaseCandidateError(
                            "OCI platform archive has unsupported member metadata"
                        )
                    if member.isdir() and member.size != 0:
                        raise ReleaseCandidateError(
                            "OCI platform archive directory is not zero-length"
                        )
                    raw_offset = _validate_tar_member_structure(
                        cast(BinaryIO, snapshot),
                        member,
                        expected_offset=raw_offset,
                    )
                    if member.isdir():
                        directories.add(relative)
                        continue
                    if not member.isfile():
                        raise ReleaseCandidateError(
                            "OCI platform archive member is not regular"
                        )
                    if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                        raise ReleaseCandidateError(
                            "OCI platform archive member exceeds its size limit"
                        )
                    total_size += member.size
                    if total_size > MAX_ARCHIVE_TOTAL_BYTES:
                        raise ReleaseCandidateError(
                            "OCI platform archive exceeds its expanded size limit"
                        )
                    expected = expected_files.get(relative)
                    if expected is None:
                        raise ReleaseCandidateError(
                            "OCI platform archive has bytes outside the verified OCI graph"
                        )
                    expected_raw, expected_path, expected_digest, expected_size = expected
                    if member.size != expected_size:
                        raise ReleaseCandidateError(
                            "OCI platform archive does not match the verified OCI graph"
                        )
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ReleaseCandidateError(
                            "OCI platform archive member cannot be read"
                        )
                    with extracted:
                        _compare_oci_archive_member(
                            cast(BinaryIO, extracted),
                            expected_raw=expected_raw,
                            expected_path=expected_path,
                            expected_digest=expected_digest,
                            expected_size=expected_size,
                        )
                    files.add(relative)
                if files != set(expected_files):
                    raise ReleaseCandidateError(
                        "OCI platform archive does not contain the verified OCI graph"
                    )
                expected_directories = {
                    "/".join(parts[:length])
                    for relative in expected_files
                    for parts in [relative.split("/")]
                    for length in range(1, len(parts))
                }
                if not directories <= expected_directories:
                    raise ReleaseCandidateError(
                        "OCI platform archive has directories outside the verified OCI graph"
                    )
                if files & directories:
                    raise ReleaseCandidateError(
                        "OCI platform archive has a file/directory collision"
                    )
                end_offset = raw_offset
                if (
                    end_offset % tarfile.BLOCKSIZE != 0
                    or _observed_size - end_offset < tarfile.BLOCKSIZE * 2
                    or _observed_size % tarfile.BLOCKSIZE != 0
                ):
                    raise ReleaseCandidateError(
                        "OCI platform archive has invalid trailing bytes after its end marker"
                    )
                snapshot.seek(end_offset)
                while trailing := snapshot.read(STREAM_CHUNK_BYTES):
                    if any(trailing):
                        raise ReleaseCandidateError(
                            "OCI platform archive has nonzero trailing bytes after its end marker"
                        )
        except tarfile.TarError as exc:
            raise ReleaseCandidateError(
                "OCI platform archive cannot be parsed as the verified OCI graph"
            ) from exc


def _verify_oci_graph(bundle_root: Path, *, expected_source_sha: str) -> JSONObject:
    container_root = bundle_root / "containers"
    layout_root = container_root / "oci-layout"
    blobs_root = layout_root / "blobs"
    _require_exact_directory_entries(
        container_root,
        {
            "kestrel-linux-amd64.tar",
            "kestrel-linux-arm64.tar",
            "oci-descriptor.json",
            "oci-layout",
        },
        label="containers",
    )
    _require_exact_directory_entries(
        layout_root,
        {"blobs", "index.json", "oci-layout"},
        label="OCI layout",
    )
    _require_exact_directory_entries(blobs_root, {"sha256"}, label="OCI blobs root")
    marker_raw = _read_regular(
        layout_root / "oci-layout",
        label="OCI layout marker",
        max_bytes=MAX_OCI_METADATA_BYTES,
    )
    marker = _parse_canonical_object(marker_raw, label="OCI layout marker")
    if marker != OCI_LAYOUT_MARKER:
        raise ReleaseCandidateError("OCI layout marker mismatch")

    descriptor_raw = _read_regular(
        container_root / "oci-descriptor.json",
        label="OCI descriptor",
        max_bytes=MAX_OCI_METADATA_BYTES,
    )
    descriptor = _parse_canonical_object(descriptor_raw, label="OCI descriptor")
    expected_descriptor_keys = {
        "schema",
        "repository",
        "source_sha",
        "index_digest",
        "index_ref",
        "index_manifest_path",
        "platforms",
    }
    if set(descriptor) != expected_descriptor_keys:
        raise ReleaseCandidateError("OCI descriptor fields are not exact")
    if descriptor.get("schema") != OCI_DESCRIPTOR_SCHEMA:
        raise ReleaseCandidateError("OCI descriptor schema mismatch")
    if descriptor.get("repository") != OCI_REPOSITORY:
        raise ReleaseCandidateError("OCI repository mismatch")
    if descriptor.get("source_sha") != expected_source_sha:
        raise ReleaseCandidateError("OCI source SHA mismatch")
    index_digest = _require_pattern(descriptor.get("index_digest"), DIGEST_RE, label="index digest")
    if descriptor.get("index_ref") != f"{OCI_REPOSITORY}@{index_digest}":
        raise ReleaseCandidateError("OCI index ref mismatch")
    if descriptor.get("index_manifest_path") != "containers/oci-layout/index.json":
        raise ReleaseCandidateError("OCI index path is not exact")
    index_raw = _read_digest_bound(
        layout_root / "index.json",
        index_digest,
        label="OCI index",
        max_bytes=MAX_OCI_METADATA_BYTES,
    )
    index = _parse_canonical_object(index_raw, label="OCI index")
    if set(index) != {"schemaVersion", "mediaType", "manifests"}:
        raise ReleaseCandidateError("OCI index fields are not exact")
    if index.get("schemaVersion") != 2 or index.get("mediaType") != OCI_INDEX_MEDIA_TYPE:
        raise ReleaseCandidateError("OCI index identity mismatch")

    platform_records = [
        _object(item, label="OCI descriptor platform")
        for item in _array(descriptor.get("platforms"), label="OCI descriptor platforms")
    ]
    if [item.get("architecture") for item in platform_records] != list(EXPECTED_PLATFORMS):
        raise ReleaseCandidateError("OCI descriptor platforms are not exact and sorted")
    index_records = [
        _object(item, label="OCI index platform")
        for item in _array(index.get("manifests"), label="OCI index manifests")
    ]
    if len(index_records) != 2:
        raise ReleaseCandidateError("OCI index must contain exactly two platform manifests")

    blob_root = blobs_root / "sha256"
    blob_files = _walk_regular_files(blob_root, label="OCI blobs")
    if any(HEX_BLOB_RE.fullmatch(name) is None for name in blob_files):
        raise ReleaseCandidateError("OCI blob filename is not a lowercase SHA-256")
    reachable: set[str] = set()
    index_by_architecture: dict[str, JSONObject] = {}
    for item in index_records:
        if set(item) != {"mediaType", "digest", "size", "platform"}:
            raise ReleaseCandidateError("OCI index descriptor fields are not exact")
        if item.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE:
            raise ReleaseCandidateError("OCI index descriptor media type mismatch")
        digest = _require_pattern(item.get("digest"), DIGEST_RE, label="manifest digest")
        size = _positive_integer(item.get("size"), label="manifest size")
        platform = _object(item.get("platform"), label="OCI index platform identity")
        if set(platform) != {"architecture", "os"} or platform.get("os") != "linux":
            raise ReleaseCandidateError("OCI index platform identity mismatch")
        architecture = str(platform.get("architecture"))
        if architecture in index_by_architecture:
            raise ReleaseCandidateError("duplicate OCI index platform")
        if architecture not in EXPECTED_PLATFORMS:
            raise ReleaseCandidateError("unexpected OCI index platform")
        _read_digest_bound(
            _blob_path(blob_root, digest),
            digest,
            label="OCI manifest",
            max_bytes=MAX_OCI_METADATA_BYTES,
            expected_size=size,
        )
        reachable.add(digest.removeprefix("sha256:"))
        index_by_architecture[architecture] = item
    if tuple(index_by_architecture) != EXPECTED_PLATFORMS:
        raise ReleaseCandidateError("OCI index platforms are not sorted")

    seen_manifest_digests: set[str] = set()
    seen_config_digests: set[str] = set()
    verified_layers: dict[str, tuple[str, str, int, int]] = {}
    total_layer_expansion = 0
    for record in platform_records:
        if set(record) != {
            "os",
            "architecture",
            "manifest_digest",
            "manifest_ref",
            "config_digest",
            "archive_path",
            "archive_sha256",
        }:
            raise ReleaseCandidateError("OCI descriptor platform fields are not exact")
        architecture = str(record["architecture"])
        if record.get("os") != "linux" or architecture not in EXPECTED_PLATFORMS:
            raise ReleaseCandidateError("OCI descriptor platform identity mismatch")
        manifest_digest = _require_pattern(
            record.get("manifest_digest"), DIGEST_RE, label="platform manifest digest"
        )
        if manifest_digest in seen_manifest_digests:
            raise ReleaseCandidateError("OCI platform manifest digest is duplicated")
        seen_manifest_digests.add(manifest_digest)
        if record.get("manifest_ref") != f"{OCI_REPOSITORY}@{manifest_digest}":
            raise ReleaseCandidateError("OCI platform manifest ref mismatch")
        index_record = index_by_architecture[architecture]
        if index_record.get("digest") != manifest_digest:
            raise ReleaseCandidateError("OCI descriptor and index manifest digest mismatch")
        manifest_raw = _read_digest_bound(
            _blob_path(blob_root, manifest_digest),
            manifest_digest,
            label="OCI platform manifest",
            max_bytes=MAX_OCI_METADATA_BYTES,
            expected_size=cast(int, index_record["size"]),
        )
        manifest = _parse_canonical_object(manifest_raw, label="OCI platform manifest")
        if set(manifest) != {"schemaVersion", "mediaType", "config", "layers"}:
            raise ReleaseCandidateError("OCI platform manifest fields are not exact")
        if manifest.get("schemaVersion") != 2 or manifest.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE:
            raise ReleaseCandidateError("OCI platform manifest identity mismatch")

        config = _descriptor_object(manifest.get("config"), label="OCI config descriptor")
        if config.get("mediaType") != OCI_CONFIG_MEDIA_TYPE:
            raise ReleaseCandidateError("OCI config media type mismatch")
        config_digest = _require_pattern(config.get("digest"), DIGEST_RE, label="OCI config digest")
        if config_digest != record.get("config_digest"):
            raise ReleaseCandidateError("OCI descriptor config digest mismatch")
        if config_digest in seen_config_digests:
            raise ReleaseCandidateError("OCI config digest is duplicated")
        seen_config_digests.add(config_digest)
        config_raw = _read_digest_bound(
            _blob_path(blob_root, config_digest),
            config_digest,
            label="OCI config",
            max_bytes=MAX_OCI_METADATA_BYTES,
            expected_size=cast(int, config["size"]),
        )
        reachable.add(config_digest.removeprefix("sha256:"))
        config_value = _parse_canonical_object(config_raw, label="OCI config")
        config_keys = set(config_value)
        if not OCI_CONFIG_REQUIRED_KEYS <= config_keys <= OCI_CONFIG_ALLOWED_KEYS:
            raise ReleaseCandidateError("OCI config fields are not exact")
        if config_value.get("architecture") != architecture or config_value.get("os") != "linux":
            raise ReleaseCandidateError("OCI config platform mismatch")
        rootfs = _object(config_value.get("rootfs"), label="OCI config rootfs")
        if set(rootfs) != {"type", "diff_ids"} or rootfs.get("type") != "layers":
            raise ReleaseCandidateError("OCI config rootfs fields are invalid")
        diff_ids = _array(rootfs.get("diff_ids"), label="OCI config diff_ids")
        validated_diff_ids = [
            _require_pattern(diff_id, DIGEST_RE, label="OCI diff ID")
            for diff_id in diff_ids
        ]

        layers = [
            _descriptor_object(item, label="OCI layer descriptor")
            for item in _array(manifest.get("layers"), label="OCI layers")
        ]
        if not layers or len(layers) != len(diff_ids):
            raise ReleaseCandidateError("OCI layer graph does not match config rootfs")
        layer_digests: set[str] = set()
        for layer, diff_id in zip(layers, validated_diff_ids, strict=True):
            layer_digest = _require_pattern(layer.get("digest"), DIGEST_RE, label="layer digest")
            layer_media_type = str(layer.get("mediaType"))
            if layer_media_type not in OCI_LAYER_MEDIA_TYPES:
                raise ReleaseCandidateError("OCI layer media type is not supported")
            layer_size = _positive_integer(layer.get("size"), label="layer size")
            if layer_digest in layer_digests:
                raise ReleaseCandidateError("duplicate layer digest in manifest")
            layer_digests.add(layer_digest)
            verified_layer = verified_layers.get(layer_digest)
            if verified_layer is not None:
                if verified_layer[0] != diff_id:
                    raise ReleaseCandidateError("shared OCI layer diff ID mismatch")
                if verified_layer[1] != layer_media_type:
                    raise ReleaseCandidateError(
                        "shared OCI layer media type mismatch"
                    )
                if verified_layer[2] != layer_size:
                    raise ReleaseCandidateError(
                        "shared OCI layer descriptor size mismatch"
                    )
            else:
                if len(verified_layers) >= MAX_OCI_LAYER_COUNT:
                    raise ReleaseCandidateError("candidate OCI graph has too many layers")
                remaining_expansion = (
                    MAX_OCI_TOTAL_UNCOMPRESSED_BYTES - total_layer_expansion
                )
                if remaining_expansion < 0:
                    raise ReleaseCandidateError(
                        "candidate OCI layers exceed their expanded size limit"
                    )
                uncompressed_size = _verify_layer_blob(
                    _blob_path(blob_root, layer_digest),
                    layer,
                    expected_diff_id=diff_id,
                    max_uncompressed_bytes=remaining_expansion,
                    expansion_label="candidate OCI layers",
                )
                total_layer_expansion += uncompressed_size
                verified_layers[layer_digest] = (
                    diff_id,
                    layer_media_type,
                    layer_size,
                    uncompressed_size,
                )
            reachable.add(layer_digest.removeprefix("sha256:"))

        archive_path = str(record.get("archive_path"))
        expected_archive_path = f"containers/kestrel-linux-{architecture}.tar"
        if archive_path != expected_archive_path:
            raise ReleaseCandidateError("OCI platform archive path is not exact")
        archive_digest = _require_pattern(
            record.get("archive_sha256"), DIGEST_RE, label="platform archive digest"
        )
        platform_index_raw = canonical_json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": OCI_INDEX_MEDIA_TYPE,
                "manifests": [index_record],
            }
        )
        expected_archive_files: dict[str, OCIArchiveEntry] = {
            "oci-layout": (
                marker_raw,
                None,
                _sha256(marker_raw),
                len(marker_raw),
            ),
            "index.json": (
                platform_index_raw,
                None,
                _sha256(platform_index_raw),
                len(platform_index_raw),
            ),
            f"blobs/sha256/{manifest_digest.removeprefix('sha256:')}": (
                manifest_raw,
                None,
                manifest_digest,
                len(manifest_raw),
            ),
            f"blobs/sha256/{config_digest.removeprefix('sha256:')}": (
                config_raw,
                None,
                config_digest,
                len(config_raw),
            ),
        }
        for layer in layers:
            layer_digest = str(layer["digest"])
            expected_archive_files[
                f"blobs/sha256/{layer_digest.removeprefix('sha256:')}"
            ] = (
                None,
                _blob_path(blob_root, layer_digest),
                layer_digest,
                cast(int, layer["size"]),
            )
        _verify_oci_platform_archive(
            bundle_root / archive_path,
            expected_archive_digest=archive_digest,
            expected_files=expected_archive_files,
        )

    if set(blob_files) != reachable:
        raise ReleaseCandidateError("OCI blob inventory contains missing or unreachable bytes")
    return descriptor


def _git_object(kind: str, raw: bytes) -> bytes:
    return hashlib.sha1(
        f"{kind} {len(raw)}\0".encode() + raw,
        usedforsecurity=False,
    ).digest()


class _PortablePathIndex:
    def __init__(self) -> None:
        self.children: dict[str, tuple[str, _PortablePathIndex]] = {}
        self.entry_kind: str | None = None


class _TreeNode:
    def __init__(self) -> None:
        self.files: dict[str, tuple[int, bytes]] = {}
        self.directories: dict[str, _TreeNode] = {}


def _tree_digest(node: _TreeNode) -> bytes:
    entries: list[tuple[bytes, bytes]] = []
    for name, (mode, raw) in node.files.items():
        encoded = name.encode("utf-8")
        git_mode = b"100755" if mode & 0o111 else b"100644"
        entries.append((encoded, git_mode + b" " + encoded + b"\0" + _git_object("blob", raw)))
    for name, child in node.directories.items():
        encoded = name.encode("utf-8")
        entries.append((encoded + b"/", b"40000 " + encoded + b"\0" + _tree_digest(child)))
    body = b"".join(entry for _sort_key, entry in sorted(entries, key=lambda item: item[0]))
    return _git_object("tree", body)


def _safe_archive_path(name: str, *, label: str) -> tuple[str, ...]:
    if not name or "\\" in name or name.startswith("/"):
        raise ReleaseCandidateError(f"{label} has an unsafe path: {name!r}")
    _validate_string(name, label=f"{label} path")
    if len(name.encode("utf-8")) > MAX_ARCHIVE_PATH_BYTES:
        raise ReleaseCandidateError(f"{label} exceeds the encoded path length limit")
    raw_parts = name.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ReleaseCandidateError(f"{label} has an unsafe path: {name!r}")
    if len(raw_parts) > MAX_ARCHIVE_PATH_COMPONENTS:
        raise ReleaseCandidateError(f"{label} exceeds the path component count limit")
    path = PurePosixPath(name)
    if path.is_absolute():
        raise ReleaseCandidateError(f"{label} has an unsafe path: {name!r}")
    for part in path.parts:
        _validate_string(part, label=f"{label} path")
        if len(part.encode("utf-8")) > MAX_ARCHIVE_PATH_COMPONENT_BYTES:
            raise ReleaseCandidateError(
                f"{label} has a component exceeding its encoded length limit"
            )
        device_stem = part.rstrip(" .").split(".", maxsplit=1)[0].rstrip(" ").upper()
        if (
            part.endswith((" ", "."))
            or device_stem in WINDOWS_DEVICE_NAMES
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
        ):
            raise ReleaseCandidateError(f"{label} has a non-portable path: {name!r}")
    return path.parts


def _record_portable_path(
    parts: tuple[str, ...],
    identities: _PortablePathIndex,
    *,
    label: str,
    entry_kind: str | None = None,
) -> None:
    if entry_kind not in {None, "directory", "file"}:
        raise ReleaseCandidateError(f"{label} has an invalid path entry kind")
    node = identities
    actual_prefix: list[str] = []
    previous_prefix: list[str] = []
    for position, part in enumerate(parts):
        folded = part.casefold()
        existing = node.children.get(folded)
        if existing is None:
            child = _PortablePathIndex()
            node.children[folded] = (part, child)
            previous_part = part
        else:
            previous_part, child = existing
        actual_prefix.append(part)
        previous_prefix.append(previous_part)
        if previous_part != part:
            raise ReleaseCandidateError(
                f"{label} has a portable path collision: "
                f"{'/'.join(previous_prefix)!r} and {'/'.join(actual_prefix)!r}"
            )
        if position < len(parts) - 1 and child.entry_kind == "file":
            raise ReleaseCandidateError(f"{label} has a file/directory path collision")
        node = child
    if entry_kind == "file":
        if node.entry_kind == "directory" or node.children:
            raise ReleaseCandidateError(f"{label} has a file/directory path collision")
    elif entry_kind == "directory" and node.entry_kind == "file":
        raise ReleaseCandidateError(f"{label} has a file/directory path collision")
    if entry_kind is not None and node.entry_kind is None:
        node.entry_kind = entry_kind


def _source_archive_identity(
    raw: bytes, *, expected_commit_sha: str
) -> tuple[str, dict[str, tuple[int, bytes]], _PortablePathIndex]:
    root = _TreeNode()
    files: dict[str, tuple[int, bytes]] = {}
    seen: set[str] = set()
    portable_identities = _PortablePathIndex()
    total_size = 0
    total_path_components = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except tarfile.TarError as exc:
        raise ReleaseCandidateError("source archive is not a valid tar") from exc
    with archive:
        if archive.pax_headers.get("comment") != expected_commit_sha:
            raise ReleaseCandidateError("source archive commit comment mismatch")
        for count, member in enumerate(archive, start=1):
            if count > MAX_ARCHIVE_MEMBERS:
                raise ReleaseCandidateError("source archive has too many members")
            parts = _safe_archive_path(member.name.rstrip("/"), label="source archive")
            total_path_components += len(parts)
            if total_path_components > MAX_SOURCE_PATH_COMPONENTS:
                raise ReleaseCandidateError(
                    "source archive exceeds its cumulative path component limit"
                )
            normalized = "/".join(parts)
            if normalized in seen:
                raise ReleaseCandidateError(f"duplicate source archive member: {normalized}")
            seen.add(normalized)
            if member.sparse is not None:
                raise ReleaseCandidateError(
                    f"source archive member is sparse: {normalized}"
                )
            if member.size < 0:
                raise ReleaseCandidateError(
                    f"source archive member has a negative size: {normalized}"
                )
            if member.isdir():
                if member.size != 0:
                    raise ReleaseCandidateError(
                        f"source archive directory is not zero-length: {normalized}"
                    )
                _record_portable_path(
                    parts,
                    portable_identities,
                    label="source archive",
                    entry_kind="directory",
                )
                continue
            if not member.isfile():
                raise ReleaseCandidateError(f"source archive member is not regular: {normalized}")
            _record_portable_path(
                parts,
                portable_identities,
                label="source archive",
                entry_kind="file",
            )
            if member.size > MAX_SOURCE_MEMBER_BYTES:
                raise ReleaseCandidateError(
                    f"source archive member exceeds its size limit: {normalized}"
                )
            total_size += member.size
            if total_size > MAX_SOURCE_TOTAL_BYTES:
                raise ReleaseCandidateError(
                    "source archive exceeds its expanded size limit"
                )
            handle = archive.extractfile(member)
            if handle is None:
                raise ReleaseCandidateError(f"source archive member cannot be read: {normalized}")
            data = handle.read(member.size + 1)
            if len(data) != member.size:
                raise ReleaseCandidateError(f"source archive member size mismatch: {normalized}")
            files[normalized] = (member.mode, data)
            node = root
            for directory in parts[:-1]:
                node = node.directories.setdefault(directory, _TreeNode())
            node.files[parts[-1]] = (member.mode, data)
    return _tree_digest(root).hex(), files, portable_identities


def _git_output(root: Path, *args: str) -> str | None:
    git_executable = shutil.which("git")
    if git_executable is None:
        return None
    # Every argument is internal, shell execution is disabled, and the executable is absolute.
    completed = subprocess.run(  # nosec B603
        [git_executable, *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _require_exact_git_archive(
    source_root: Path, *, commit_sha: str, archive_raw: bytes
) -> None:
    git_executable = shutil.which("git")
    if git_executable is None:
        raise ReleaseCandidateError("git is required to verify the exact git archive")
    with tempfile.TemporaryFile(mode="w+b") as expected:
        completed = subprocess.run(  # nosec B603
            [git_executable, "archive", "--format=tar", commit_sha],
            cwd=source_root,
            stdout=expected,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise ReleaseCandidateError("exact git archive regeneration failed")
        expected.flush()
        expected.seek(0)
        observed = io.BytesIO(archive_raw)
        while True:
            expected_chunk = expected.read(STREAM_CHUNK_BYTES)
            observed_chunk = observed.read(STREAM_CHUNK_BYTES)
            if expected_chunk != observed_chunk:
                raise ReleaseCandidateError(
                    "source.tar is not byte-equal to the exact git archive"
                )
            if not expected_chunk:
                break


def _source_version(root: Path) -> str:
    raw = _read_regular(
        root / "pyproject.toml",
        label="source pyproject.toml",
        max_bytes=MAX_SOURCE_METADATA_BYTES,
    )
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseCandidateError("source pyproject.toml is invalid") from exc
    project = value.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("version"), str):
        raise ReleaseCandidateError("source project version is missing")
    return cast(str, project["version"])


def _require_git_main_identity(source_root: Path, *, commit_sha: str) -> None:
    main_candidates = {
        value
        for ref in ("refs/heads/main^{commit}", "refs/remotes/origin/main^{commit}")
        if (value := _git_output(source_root, "rev-parse", "--verify", ref)) is not None
    }
    if commit_sha not in main_candidates:
        raise ReleaseCandidateError(
            "source commit is not the exact local protected-main identity"
        )


def _require_no_untracked_inputs(source_root: Path) -> None:
    untracked = _git_output(
        source_root,
        "ls-files",
        "--others",
        "--exclude-standard",
    )
    if untracked is None:
        raise ReleaseCandidateError("unable to inspect source worktree inputs")
    if untracked:
        raise ReleaseCandidateError("source worktree contains untracked inputs")


def _verify_extracted_source_root(
    source_root: Path,
    *,
    archive_files: Mapping[str, tuple[int, bytes]],
    archive_directory_index: _PortablePathIndex,
) -> None:
    actual_files = _walk_regular_files(
        source_root,
        label="extracted source root",
        require_artifact_path=False,
    )
    if set(actual_files) != set(archive_files):
        raise ReleaseCandidateError("extracted source root file inventory mismatch")
    pending = [(source_root, archive_directory_index)]
    while pending:
        directory, expected_node = pending.pop()
        actual_directories: dict[str, Path] = {}
        for entry in os.scandir(directory):
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                continue
            actual_directories[entry.name] = Path(entry.path)
        expected_directories = {
            actual: child
            for actual, child in expected_node.children.values()
            if child.entry_kind == "directory" or child.children
        }
        if set(actual_directories) != set(expected_directories):
            raise ReleaseCandidateError("extracted source root directory inventory mismatch")
        pending.extend(
            (actual_directories[name], expected_directories[name])
            for name in expected_directories
        )
    for relative, (mode, expected) in archive_files.items():
        path = actual_files[relative]
        actual = _read_regular(
            path,
            label=f"source file {relative}",
            allow_empty=True,
            max_bytes=len(expected),
        )
        if actual != expected:
            raise ReleaseCandidateError(f"source file bytes mismatch: {relative}")
        executable = bool(path.stat().st_mode & 0o111)
        if executable != bool(mode & 0o111):
            raise ReleaseCandidateError(f"source file mode mismatch: {relative}")


def _verify_source_root(
    source_root: Path,
    *,
    archive_raw: bytes,
    version: str,
    commit_sha: str,
    tree_sha: str,
    require_git_identity: bool,
) -> None:
    _require_real_directory(source_root, label="source root")
    if _source_version(source_root) != version:
        raise ReleaseCandidateError("source project version mismatch")
    archive_tree, archive_files, archive_directory_index = _source_archive_identity(
        archive_raw, expected_commit_sha=commit_sha
    )
    if archive_tree != tree_sha:
        raise ReleaseCandidateError("source archive tree SHA mismatch")
    top_level = _git_output(source_root, "rev-parse", "--show-toplevel")
    is_git_root = top_level is not None and Path(top_level).resolve() == source_root.resolve()
    if not is_git_root:
        if require_git_identity:
            raise ReleaseCandidateError("source root must be the exact Git worktree root")
        _verify_extracted_source_root(
            source_root,
            archive_files=archive_files,
            archive_directory_index=archive_directory_index,
        )
        return
    if _git_output(source_root, "rev-parse", "HEAD") != commit_sha:
        raise ReleaseCandidateError("source root HEAD mismatch")
    if _git_output(source_root, "rev-parse", "HEAD^{tree}") != tree_sha:
        raise ReleaseCandidateError("source root tree mismatch")
    if require_git_identity:
        _require_git_main_identity(source_root, commit_sha=commit_sha)
        _require_no_untracked_inputs(source_root)
    _require_exact_git_archive(source_root, commit_sha=commit_sha, archive_raw=archive_raw)
    for relative, (mode, expected) in archive_files.items():
        path = source_root / relative
        actual = _read_regular(
            path,
            label=f"source file {relative}",
            allow_empty=True,
            max_bytes=len(expected),
        )
        if actual != expected:
            raise ReleaseCandidateError(f"source file bytes mismatch: {relative}")
        executable = bool(path.stat().st_mode & 0o111)
        if executable != bool(mode & 0o111):
            raise ReleaseCandidateError(f"source file mode mismatch: {relative}")
    if require_git_identity:
        _require_no_untracked_inputs(source_root)


def _verify_attestation_subjects(
    bundle_root: Path, manifest: JSONObject, descriptor: JSONObject
) -> None:
    subjects = [
        _object(item, label="attestation subject")
        for item in _array(manifest["attestation_subjects"], label="attestation subjects")
    ]
    raw = _read_regular(
        bundle_root / "attestations.json",
        label="attestations.json",
        max_bytes=MAX_CANDIDATE_MANIFEST_BYTES,
    )
    if raw != canonical_json_bytes(subjects):
        raise ReleaseCandidateError("attestations.json does not equal manifest subjects")
    expected_files: dict[str, str] = {}
    for relative, path in _walk_regular_files(bundle_root / "release", label="release").items():
        digest, _size = _file_identity(path, label="release file")
        expected_files[f"release/{relative}"] = digest
    actual_files = {
        str(item["name"]): str(item["digest"])
        for item in subjects
        if item.get("kind") == "file"
    }
    if actual_files != expected_files:
        raise ReleaseCandidateError("file attestation subjects do not match release files")
    oci = [item for item in subjects if item.get("kind") == "oci_index"]
    if len(oci) != 1 or oci[0].get("digest") != descriptor.get("index_digest"):
        raise ReleaseCandidateError("OCI attestation subject digest mismatch")


def verify_candidate_bundle(
    manifest: Mapping[str, object],
    *,
    bundle_root: Path,
    source_root: Path,
    _require_git_identity: bool = False,
) -> JSONObject:
    """Read-only verification of every candidate byte and identity edge."""

    checked = _validated_manifest(
        _candidate_manifest_snapshot(manifest, label="candidate manifest"),
        label="candidate manifest",
    )
    _verify_bundle_layout(bundle_root, manifest_optional=False)
    manifest_raw = _read_regular(
        bundle_root / "candidate-manifest.json",
        label="candidate manifest",
        max_bytes=MAX_CANDIDATE_MANIFEST_BYTES,
    )
    if manifest_raw != canonical_json_bytes(checked):
        raise ReleaseCandidateError("bundle candidate-manifest.json bytes do not match")
    source = _object(checked["source"], label="source")
    source_tar = _read_regular(
        bundle_root / "source.tar",
        label="source.tar",
        max_bytes=MAX_SOURCE_ARCHIVE_BYTES,
    )
    if _sha256(source_tar) != source["archive_sha256"] or len(source_tar) != source["size_bytes"]:
        raise ReleaseCandidateError("source.tar does not match source binding")
    artifacts = _artifact_inventory(bundle_root)
    if artifacts != checked["artifacts"]:
        raise ReleaseCandidateError("artifact inventory does not match manifest")
    descriptor = _verify_oci_graph(bundle_root, expected_source_sha=str(source["commit_sha"]))
    _verify_attestation_subjects(bundle_root, checked, descriptor)
    _verify_source_root(
        source_root,
        archive_raw=source_tar,
        version=str(checked["version"]),
        commit_sha=str(source["commit_sha"]),
        tree_sha=str(source["tree_sha"]),
        require_git_identity=_require_git_identity,
    )
    checks = [
        _object(item, label="check") for item in _array(checked["checks"], label="checks")
    ]
    receipts = _verify_qualification_layout(
        bundle_root,
        checks,
        expected_artifact_set_digest=str(checked["artifact_set_digest"]),
        expected_repository=str(source["repository"]),
        expected_repository_id=cast(int, source["repository_id"]),
        expected_candidate_workflow_id=cast(
            int,
            _object(checked["candidate_run"], label="candidate_run")[
                "workflow_id"
            ],
        ),
    )
    evidence = _object(checked["evidence"], label="evidence")
    if source_bundle_digest(receipts) != evidence.get("source_bundle_digest"):
        raise ReleaseCandidateError("source evidence bundle digest mismatch")
    if evidence.get("canonicalization_vector_digest") != CANONICALIZATION_VECTOR_DIGEST:
        raise ReleaseCandidateError("canonicalization vector digest mismatch")
    candidate_run = _object(checked["candidate_run"], label="candidate_run")
    return {
        "artifact_set_digest": checked["artifact_set_digest"],
        "candidate_run": dict(candidate_run),
        "candidate_manifest_digest": candidate_manifest_digest(checked),
        "source_sha": source["commit_sha"],
        "source_tree": source["tree_sha"],
        "tag": checked["tag"],
        "version": checked["version"],
    }


def _parse_timestamp(value: object, *, label: str) -> datetime:
    text = _require_pattern(value, TIMESTAMP_RE, label=label)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ReleaseCandidateError(f"{label} is not a valid UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != text:
        raise ReleaseCandidateError(f"{label} is not canonical")
    return parsed


def _artifact_pages(value: object) -> list[JSONObject]:
    pages = [_object(value, label="artifact page")] if isinstance(value, dict) else [
        _object(item, label="artifact page") for item in _array(value, label="artifact pages")
    ]
    if not pages:
        raise ReleaseCandidateError("artifact pagination is empty")
    total_counts: set[int] = set()
    artifacts: list[JSONObject] = []
    artifact_ids: set[int] = set()
    for page in pages:
        total_count = page.get("total_count")
        if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
            raise ReleaseCandidateError("artifact page total_count is invalid")
        total_counts.add(total_count)
        for item in _array(page.get("artifacts"), label="artifact page artifacts"):
            artifact = _object(item, label="artifact")
            artifact_id = _positive_integer(artifact.get("id"), label="artifact ID")
            if artifact_id in artifact_ids:
                raise ReleaseCandidateError("artifact observation has a duplicate artifact ID")
            artifact_ids.add(artifact_id)
            artifacts.append(artifact)
    if len(total_counts) != 1 or next(iter(total_counts)) != len(artifacts):
        raise ReleaseCandidateError("artifact pagination is incomplete or inconsistent")
    return artifacts


def verify_actions_artifact(
    observation: bytes,
    run_observation: bytes,
    *,
    expected_name: str,
    expected_run_id: int,
    expected_run_attempt: int,
    expected_source_sha: str,
    retention_days: int,
    expected_workflow_path: str = CANDIDATE_WORKFLOW_PATH,
    require_completed_success: bool = True,
    expected_artifact_id: int | None = None,
    expected_api_digest: str | None = None,
    direct_artifact_observation: bytes | None = None,
) -> JSONObject:
    """Validate exhaustive Actions metadata and emit an external receipt."""

    observation_snapshot = _immutable_bounded_bytes(
        observation,
        label="artifact observation",
        max_bytes=MAX_ACTIONS_OBSERVATION_BYTES,
    )
    run_observation_snapshot = _immutable_bounded_bytes(
        run_observation,
        label="workflow run observation",
        max_bytes=MAX_ACTIONS_OBSERVATION_BYTES,
    )
    direct_observation_snapshot = (
        None
        if direct_artifact_observation is None
        else _immutable_bounded_bytes(
            direct_artifact_observation,
            label="direct artifact observation",
            max_bytes=MAX_ACTIONS_OBSERVATION_BYTES,
        )
    )
    if isinstance(expected_run_attempt, bool) or expected_run_attempt != 1:
        raise ReleaseCandidateError("only workflow run attempt 1 is accepted")
    if type(require_completed_success) is not bool:
        raise ReleaseCandidateError("artifact workflow completion policy is invalid")
    _positive_integer(expected_run_id, label="expected run ID")
    _require_pattern(expected_source_sha, SHA_RE, label="expected source SHA")
    if expected_workflow_path not in {
        CANDIDATE_WORKFLOW_PATH,
        ".github/workflows/recovery-dependency-staging.yml",
        ".github/workflows/release.yml",
    }:
        raise ReleaseCandidateError("artifact workflow path policy is invalid")
    if (expected_artifact_id is None) != (expected_api_digest is None):
        raise ReleaseCandidateError("artifact expected transport identity is incomplete")
    if expected_artifact_id is not None:
        _positive_integer(expected_artifact_id, label="expected artifact ID")
        _require_pattern(expected_api_digest, DIGEST_RE, label="expected artifact digest")
    validated_retention_days = _positive_integer(
        retention_days, label="retention days"
    )
    if validated_retention_days != ARTIFACT_RETENTION_DAYS:
        raise ReleaseCandidateError("artifact retention must be exactly 30 days")
    run = _object(
        _parse_json(
            run_observation_snapshot,
            label="workflow run observation",
            canonical=False,
        ),
        label="workflow run observation",
    )
    if _positive_integer(run.get("id"), label="workflow run ID") != expected_run_id:
        raise ReleaseCandidateError("workflow run ID mismatch")
    _positive_integer(run.get("workflow_id"), label="workflow ID")
    if run.get("path") not in {
        expected_workflow_path,
        f"{expected_workflow_path}@main",
    }:
        raise ReleaseCandidateError("workflow run path is not the expected artifact workflow")
    if run.get("event") != "workflow_dispatch" or run.get("head_branch") != "main":
        raise ReleaseCandidateError("workflow run is not a main candidate dispatch")
    repository = _object(run.get("repository"), label="workflow run repository")
    repository_id = _positive_integer(
        repository.get("id"), label="workflow run repository ID"
    )
    if repository.get("full_name") != CANDIDATE_REPOSITORY:
        raise ReleaseCandidateError("workflow run repository identity mismatch")
    if isinstance(run.get("run_attempt"), bool) or run.get("run_attempt") != 1:
        raise ReleaseCandidateError("workflow run attempt mismatch")
    if run.get("head_sha") != expected_source_sha:
        raise ReleaseCandidateError("workflow run source SHA mismatch")
    if require_completed_success:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise ReleaseCandidateError("workflow run is not a completed success")
    elif (
        (run.get("status"), run.get("conclusion"))
        not in {
            ("in_progress", None),
            ("waiting", None),
            ("completed", "success"),
        }
    ):
        raise ReleaseCandidateError("active artifact workflow run state is invalid")
    artifacts = _artifact_pages(
        _parse_json(
            observation_snapshot,
            label="artifact observation",
            canonical=False,
        )
    )
    matches = [item for item in artifacts if item.get("name") == expected_name]
    if len(matches) != 1:
        raise ReleaseCandidateError(
            f"expected exactly one artifact named {expected_name}, found {len(matches)}"
        )
    artifact = matches[0]
    artifact_id = _positive_integer(artifact.get("id"), label="artifact ID")
    digest = _require_pattern(artifact.get("digest"), DIGEST_RE, label="artifact digest")
    if expected_artifact_id is not None and (
        artifact_id != expected_artifact_id or digest != expected_api_digest
    ):
        raise ReleaseCandidateError("artifact server transport identity mismatch")
    size = _positive_integer(artifact.get("size_in_bytes"), label="artifact size")
    if artifact.get("expired") is not False:
        raise ReleaseCandidateError("artifact is expired")
    created = _parse_timestamp(artifact.get("created_at"), label="artifact created_at")
    expires = _parse_timestamp(artifact.get("expires_at"), label="artifact expires_at")
    observed_retention = int((expires - created).total_seconds())
    configured_retention = validated_retention_days * 86400
    if observed_retention not in {configured_retention, configured_retention - 1}:
        raise ReleaseCandidateError("artifact retention interval mismatch")
    artifact_run = _object(artifact.get("workflow_run"), label="artifact workflow_run")
    if (
        _positive_integer(artifact_run.get("id"), label="artifact workflow run ID")
        != expected_run_id
        or _positive_integer(
            artifact_run.get("repository_id"),
            label="artifact workflow run repository ID",
        )
        != repository_id
        or _positive_integer(
            artifact_run.get("head_repository_id"),
            label="artifact workflow run head repository ID",
        )
        != repository_id
        or artifact_run.get("head_branch") != run.get("head_branch")
        or artifact_run.get("head_branch") != "main"
        or artifact_run.get("head_sha") != expected_source_sha
    ):
        raise ReleaseCandidateError("artifact workflow run identity mismatch")
    if direct_observation_snapshot is not None:
        direct = _object(
            _parse_json(
                direct_observation_snapshot,
                label="direct artifact observation",
                canonical=False,
            ),
            label="direct artifact observation",
        )
        for field in (
            "id",
            "name",
            "size_in_bytes",
            "expired",
            "digest",
            "created_at",
            "expires_at",
        ):
            if direct.get(field) != artifact.get(field):
                raise ReleaseCandidateError(
                    "direct artifact observation identity mismatch"
                )
        direct_run = _object(
            direct.get("workflow_run"), label="direct artifact workflow_run"
        )
        for field in (
            "id",
            "repository_id",
            "head_repository_id",
            "head_branch",
            "head_sha",
        ):
            if direct_run.get(field) != artifact_run.get(field):
                raise ReleaseCandidateError(
                    "direct artifact workflow run identity mismatch"
                )
    source_records = {
        "artifact-observation": observation_snapshot,
        "workflow-run-observation": run_observation_snapshot,
    }
    if direct_observation_snapshot is not None:
        source_records["direct-artifact-observation"] = direct_observation_snapshot
    receipt: JSONObject = {
        "schema": ARTIFACT_OBSERVATION_SCHEMA,
        "artifact": {
            "artifact_id": artifact_id,
            "name": expected_name,
            "api_digest": digest,
            "size_bytes": size,
            "expired": False,
            "created_at": artifact["created_at"],
            "expires_at": artifact["expires_at"],
            "run_id": expected_run_id,
            "run_attempt": 1,
            "source_sha": expected_source_sha,
        },
        "evidence": {
            "source_bundle_digest": source_bundle_digest(source_records),
            "canonicalization_vector_digest": CANONICALIZATION_VECTOR_DIGEST,
        },
        "provenance": {
            "producer": PRODUCER,
            "provider": "github.com",
            "method": "actions-artifact-observation",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    _validate_schema(ARTIFACT_OBSERVATION_SCHEMA, receipt, label="artifact receipt")
    return receipt


ZIP_END_RECORD_BYTES = 22
ZIP_MAX_COMMENT_BYTES = 0xFFFF
ZIP_ALLOWED_EXTRA_FIELD_IDS: frozenset[int] = frozenset()


def _preflight_zip(snapshot: BinaryIO, *, archive_size: int) -> None:
    if archive_size < ZIP_END_RECORD_BYTES:
        raise ReleaseCandidateError("ZIP end record is missing")
    tail_size = min(
        archive_size,
        ZIP_END_RECORD_BYTES + ZIP_MAX_COMMENT_BYTES,
    )
    tail_offset = archive_size - tail_size
    snapshot.seek(tail_offset)
    tail = snapshot.read(tail_size)
    if len(tail) != tail_size:
        raise ReleaseCandidateError("ZIP end record preflight is truncated")

    search_end = len(tail)
    end_record: tuple[int, int, int, int, int, int, int] | None = None
    while search_end >= ZIP_END_RECORD_BYTES:
        position = tail.rfind(b"PK\x05\x06", 0, search_end)
        if position < 0:
            break
        if position + ZIP_END_RECORD_BYTES <= len(tail):
            (
                signature,
                disk_number,
                central_disk,
                entries_on_disk,
                total_entries,
                central_size,
                central_offset,
                comment_length,
            ) = struct.unpack(
                "<4s4H2IH", tail[position : position + ZIP_END_RECORD_BYTES]
            )
            if (
                signature == b"PK\x05\x06"
                and position + ZIP_END_RECORD_BYTES + comment_length == len(tail)
            ):
                end_record = (
                    tail_offset + position,
                    disk_number,
                    central_disk,
                    entries_on_disk,
                    total_entries,
                    central_size,
                    central_offset,
                )
                break
        search_end = position
    if end_record is None:
        raise ReleaseCandidateError("ZIP end record is missing or malformed")

    (
        end_offset,
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
    ) = end_record
    if disk_number != 0 or central_disk != 0 or entries_on_disk != total_entries:
        raise ReleaseCandidateError("ZIP end record is not a single-disk archive")
    if total_entries == 0:
        raise ReleaseCandidateError("artifact archive is empty")
    if total_entries > MAX_ARCHIVE_MEMBERS:
        raise ReleaseCandidateError("artifact archive has too many members")
    if central_size > MAX_ZIP_CENTRAL_DIRECTORY_BYTES:
        raise ReleaseCandidateError("ZIP central directory exceeds its size limit")
    if central_size < total_entries * 46:
        raise ReleaseCandidateError("ZIP central directory is smaller than its entry count")
    if central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        raise ReleaseCandidateError("ZIP64 archives are not accepted")
    if central_offset + central_size != end_offset:
        raise ReleaseCandidateError("ZIP central directory offsets are not exact")

    snapshot.seek(central_offset)
    observed_entries = 0
    while snapshot.tell() < end_offset:
        if observed_entries >= MAX_ARCHIVE_MEMBERS:
            raise ReleaseCandidateError("artifact archive has too many members")
        record_offset = snapshot.tell()
        header = snapshot.read(46)
        if len(header) != 46:
            raise ReleaseCandidateError("ZIP central directory preflight is truncated")
        values = struct.unpack("<4s6H3I5H2I", header)
        if values[0] != b"PK\x01\x02":
            raise ReleaseCandidateError(
                "ZIP central directory preflight has an invalid record"
            )
        filename_length = values[10]
        extra_length = values[11]
        comment_length = values[12]
        if filename_length == 0 or filename_length > MAX_ARCHIVE_PATH_BYTES:
            raise ReleaseCandidateError(
                "ZIP central directory filename exceeds its encoded path length limit"
            )
        record_end = (
            record_offset
            + 46
            + filename_length
            + extra_length
            + comment_length
        )
        if record_end > end_offset:
            raise ReleaseCandidateError(
                "ZIP central directory preflight record exceeds its boundary"
            )
        snapshot.seek(record_offset + 46 + filename_length)
        extra = snapshot.read(extra_length)
        if len(extra) != extra_length:
            raise ReleaseCandidateError(
                "ZIP central directory preflight extra field is truncated"
            )
        _validate_zip_extra_fields(extra, label="ZIP central directory preflight")
        snapshot.seek(record_end)
        observed_entries += 1
    if snapshot.tell() != end_offset or observed_entries != total_entries:
        raise ReleaseCandidateError("ZIP central directory entry count is not exact")
    snapshot.seek(0)


def _validate_zip_extra_fields(extra: bytes, *, label: str) -> None:
    offset = 0
    seen: set[int] = set()
    while offset < len(extra):
        if len(extra) - offset < 4:
            raise ReleaseCandidateError(f"{label} has a malformed ZIP extra field")
        field_id, field_size = struct.unpack_from("<2H", extra, offset)
        offset += 4
        field_end = offset + field_size
        if field_end > len(extra):
            raise ReleaseCandidateError(f"{label} has a truncated ZIP extra field")
        if field_id in seen:
            raise ReleaseCandidateError(f"{label} has a duplicate ZIP extra field")
        seen.add(field_id)
        if field_id not in ZIP_ALLOWED_EXTRA_FIELD_IDS:
            raise ReleaseCandidateError(f"{label} has an unapproved ZIP extra field")
        offset = field_end


def _zip_path(info: zipfile.ZipInfo) -> tuple[str, ...]:
    name = info.filename
    if not name or "\\" in name or name.startswith("/"):
        raise ReleaseCandidateError(f"unsafe archive member path: {name!r}")
    if len(name) >= 2 and name[1] == ":":
        raise ReleaseCandidateError(f"drive-qualified archive member path: {name!r}")
    trimmed = name[:-1] if name.endswith("/") else name
    return _safe_archive_path(trimmed, label="artifact archive")


def _validate_zip_physical_layout(
    archive: zipfile.ZipFile, archive_members: Sequence[zipfile.ZipInfo]
) -> None:
    handle = archive.fp
    start_dir = getattr(archive, "start_dir", None)
    if handle is None or isinstance(start_dir, bool) or not isinstance(start_dir, int):
        raise ReleaseCandidateError("ZIP physical layout is unavailable")
    ordered_members = sorted(archive_members, key=lambda item: item.header_offset)
    if ordered_members[0].header_offset != 0:
        raise ReleaseCandidateError("ZIP local records are not gap-free")
    for index, info in enumerate(ordered_members):
        next_offset = (
            ordered_members[index + 1].header_offset
            if index + 1 < len(ordered_members)
            else start_dir
        )
        if info.header_offset < 0 or next_offset <= info.header_offset:
            raise ReleaseCandidateError("ZIP local records are not gap-free")
        handle.seek(info.header_offset)
        header = handle.read(30)
        if len(header) != 30:
            raise ReleaseCandidateError("ZIP local header is truncated")
        (
            signature,
            _extract_version,
            flag_bits,
            compress_type,
            _modified_time,
            _modified_date,
            crc,
            compressed_size,
            file_size,
            filename_length,
            extra_length,
        ) = struct.unpack("<4s5H3I2H", header)
        if signature != b"PK\x03\x04":
            raise ReleaseCandidateError("ZIP local header signature mismatch")
        filename = handle.read(filename_length)
        extra = handle.read(extra_length)
        if len(filename) != filename_length or len(extra) != extra_length:
            raise ReleaseCandidateError("ZIP local header metadata is truncated")
        try:
            expected_filename = info.filename.encode(
                "utf-8" if flag_bits & 0x800 else "cp437"
            )
        except UnicodeEncodeError as exc:
            raise ReleaseCandidateError("ZIP local filename encoding is invalid") from exc
        if flag_bits != info.flag_bits or compress_type != info.compress_type:
            raise ReleaseCandidateError("ZIP local header does not match central record")
        if filename != expected_filename:
            raise ReleaseCandidateError("ZIP local header does not match central record")
        _validate_zip_extra_fields(extra, label="ZIP local header")
        if extra != info.extra:
            raise ReleaseCandidateError(
                "ZIP local and central extra fields do not match"
            )
        if compressed_size == 0xFFFFFFFF or file_size == 0xFFFFFFFF:
            raise ReleaseCandidateError("ZIP64 local records are not accepted")
        data_end = (
            info.header_offset
            + 30
            + filename_length
            + extra_length
            + info.compress_size
        )
        if data_end > next_offset:
            raise ReleaseCandidateError("ZIP local record overlaps its successor")
        descriptor_size = next_offset - data_end
        if flag_bits & 0x08:
            if (
                crc not in {0, info.CRC}
                or compressed_size not in {0, info.compress_size}
                or file_size not in {0, info.file_size}
                or descriptor_size not in {12, 16}
            ):
                raise ReleaseCandidateError(
                    "ZIP local header does not match central record"
                )
            handle.seek(data_end)
            descriptor = handle.read(descriptor_size)
            if len(descriptor) != descriptor_size:
                raise ReleaseCandidateError("ZIP data descriptor is truncated")
            if descriptor_size == 16:
                descriptor_signature, descriptor_crc, descriptor_compressed, descriptor_size_value = (
                    struct.unpack("<4s3I", descriptor)
                )
                if descriptor_signature != b"PK\x07\x08":
                    raise ReleaseCandidateError("ZIP data descriptor signature mismatch")
            else:
                descriptor_crc, descriptor_compressed, descriptor_size_value = struct.unpack(
                    "<3I", descriptor
                )
            if (
                descriptor_crc != info.CRC
                or descriptor_compressed != info.compress_size
                or descriptor_size_value != info.file_size
            ):
                raise ReleaseCandidateError(
                    "ZIP data descriptor does not match central record"
                )
        elif (
            crc != info.CRC
            or compressed_size != info.compress_size
            or file_size != info.file_size
            or descriptor_size != 0
        ):
            raise ReleaseCandidateError("ZIP local header does not match central record")

    handle.seek(start_dir)
    for info in archive_members:
        central_offset = handle.tell()
        header = handle.read(46)
        if len(header) != 46:
            raise ReleaseCandidateError("ZIP central record is truncated")
        values = struct.unpack("<4s6H3I5H2I", header)
        (
            signature,
            _create_version,
            _extract_version,
            flag_bits,
            compress_type,
            _modified_time,
            _modified_date,
            crc,
            compressed_size,
            file_size,
            filename_length,
            extra_length,
            comment_length,
            disk_number,
            _internal_attributes,
            external_attributes,
            local_offset,
        ) = values
        if signature != b"PK\x01\x02":
            raise ReleaseCandidateError("ZIP central record signature mismatch")
        filename = handle.read(filename_length)
        extra = handle.read(extra_length)
        comment = handle.read(comment_length)
        if (
            len(filename) != filename_length
            or len(extra) != extra_length
            or len(comment) != comment_length
        ):
            raise ReleaseCandidateError("ZIP central record metadata is truncated")
        try:
            expected_filename = info.filename.encode(
                "utf-8" if flag_bits & 0x800 else "cp437"
            )
        except UnicodeEncodeError as exc:
            raise ReleaseCandidateError("ZIP central filename encoding is invalid") from exc
        _validate_zip_extra_fields(extra, label="ZIP central record")
        if (
            disk_number != 0
            or compressed_size == 0xFFFFFFFF
            or file_size == 0xFFFFFFFF
            or local_offset == 0xFFFFFFFF
            or flag_bits != info.flag_bits
            or compress_type != info.compress_type
            or crc != info.CRC
            or compressed_size != info.compress_size
            or file_size != info.file_size
            or external_attributes != info.external_attr
            or local_offset != info.header_offset
            or filename != expected_filename
            or extra != info.extra
            or comment != info.comment
        ):
            raise ReleaseCandidateError("ZIP central record does not match parsed metadata")
        if handle.tell() <= central_offset:
            raise ReleaseCandidateError("ZIP central record does not advance")

    central_end = handle.tell()
    end_record = handle.read(22)
    if len(end_record) != 22:
        raise ReleaseCandidateError("ZIP end record is truncated")
    (
        signature,
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        comment_length,
    ) = struct.unpack("<4s4H2IH", end_record)
    if (
        signature != b"PK\x05\x06"
        or disk_number != 0
        or central_disk != 0
        or entries_on_disk != len(archive_members)
        or total_entries != len(archive_members)
        or central_size != central_end - start_dir
        or central_offset != start_dir
    ):
        raise ReleaseCandidateError("ZIP end record does not match central directory")
    comment = handle.read(comment_length)
    if len(comment) != comment_length or comment != archive.comment:
        raise ReleaseCandidateError("ZIP archive comment does not match end record")
    if handle.read(1):
        raise ReleaseCandidateError("ZIP archive has trailing bytes")


def _validate_zip(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
    archive_members = archive.infolist()
    if len(archive_members) > MAX_ARCHIVE_MEMBERS:
        raise ReleaseCandidateError("artifact archive has too many members")
    if not archive_members:
        raise ReleaseCandidateError("artifact archive is empty")
    _validate_zip_physical_layout(archive, archive_members)
    members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    names: set[str] = set()
    portable_identities = _PortablePathIndex()
    total_size = 0
    total_path_components = 0
    for info in archive_members:
        parts = _zip_path(info)
        total_path_components += len(parts)
        if total_path_components > MAX_ARCHIVE_TOTAL_PATH_COMPONENTS:
            raise ReleaseCandidateError(
                "artifact archive exceeds its cumulative path component limit"
            )
        normalized = "/".join(parts)
        if normalized in names:
            raise ReleaseCandidateError(f"duplicate archive member: {normalized}")
        names.add(normalized)
        if info.flag_bits & 0x1:
            raise ReleaseCandidateError(f"encrypted archive member: {normalized}")
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        is_directory = info.is_dir()
        if is_directory:
            if file_type not in {0, stat.S_IFDIR}:
                raise ReleaseCandidateError(f"special archive directory: {normalized}")
            if info.file_size != 0 or info.compress_size != 0:
                raise ReleaseCandidateError(
                    f"archive directory has payload: {normalized}"
                )
            _record_portable_path(
                parts,
                portable_identities,
                label="artifact archive",
                entry_kind="directory",
            )
        else:
            if file_type not in {0, stat.S_IFREG}:
                raise ReleaseCandidateError(f"non-regular archive member: {normalized}")
            if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ReleaseCandidateError(f"archive member exceeds size limit: {normalized}")
            total_size += info.file_size
            if total_size > MAX_ARCHIVE_TOTAL_BYTES:
                raise ReleaseCandidateError("artifact archive exceeds total size limit")
            _record_portable_path(
                parts,
                portable_identities,
                label="artifact archive",
                entry_kind="file",
            )
        members.append((info, parts))
    return members


def extract_actions_artifact(
    archive_path: Path, *, expected_digest: str, output: Path
) -> None:
    """Safely extract one digest-bound Actions artifact without partial output."""

    _require_pattern(expected_digest, DIGEST_RE, label="expected artifact digest")
    if output.exists() or output.is_symlink():
        _require_real_directory(output, label="artifact output")
        if any(output.iterdir()):
            raise ReleaseCandidateError("artifact output must be absent or empty")
    _require_real_directory(output.parent, label="artifact output parent")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    try:
        with tempfile.TemporaryFile(mode="w+b", dir=output.parent) as snapshot:
            archive_digest, archive_size = _copy_regular_to_snapshot(
                archive_path,
                cast(BinaryIO, snapshot),
                label="artifact archive",
                max_bytes=MAX_ARCHIVE_TOTAL_BYTES,
            )
            if archive_digest != expected_digest:
                raise ReleaseCandidateError("artifact archive digest mismatch")
            _preflight_zip(cast(BinaryIO, snapshot), archive_size=archive_size)
            try:
                archive = zipfile.ZipFile(snapshot)
            except zipfile.BadZipFile as exc:
                raise ReleaseCandidateError("artifact archive is not a valid zip") from exc
            with archive:
                members = _validate_zip(archive)
                for info, parts in members:
                    target = staging.joinpath(*parts)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(
                        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644  # codeql[py/overly-permissive-file] — staged recovery asset: 0o644 is the required world-readable archive contract (secrets stay 0o600)
                    )
                    with os.fdopen(descriptor, "wb") as handle:
                        with archive.open(info, mode="r") as source:
                            written = 0
                            while chunk := source.read(STREAM_CHUNK_BYTES):
                                written += len(chunk)
                                if written > info.file_size:
                                    raise ReleaseCandidateError(
                                        f"archive member size mismatch: {info.filename}"
                                    )
                                handle.write(chunk)
                            if written != info.file_size:
                                raise ReleaseCandidateError(
                                    f"archive member size mismatch: {info.filename}"
                                )
                        handle.flush()
                        os.fsync(handle.fileno())
        if output.exists():
            output.rmdir()
        os.replace(staging, output)
        _fsync_directory(output.parent)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ReleaseCandidateError(f"artifact extraction failed: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _write_once(path: Path, raw: bytes, *, idempotent: bool) -> bool:
    _require_real_directory(path.parent, label="output parent")
    if path.exists() or path.is_symlink():
        if (
            idempotent
            and stat.S_ISREG(path.lstat().st_mode)
            and _read_regular(
                path,
                label="existing output",
                allow_empty=True,
                max_bytes=len(raw),
            )
            == raw
        ):
            return False
        raise ReleaseCandidateError(f"output conflict: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if (
                idempotent
                and path.is_file()
                and not path.is_symlink()
                and _read_regular(
                    path,
                    label="existing output",
                    allow_empty=True,
                    max_bytes=len(raw),
                )
                == raw
            ):
                return False
            raise ReleaseCandidateError(f"output conflict: {path}") from None
        _fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_created_output(path: Path, raw: bytes) -> None:
    if (
        _read_regular(
            path,
            label="created output",
            allow_empty=True,
            max_bytes=len(raw),
        )
        != raw
    ):
        raise ReleaseCandidateError("created output changed before cleanup")
    path.unlink()
    _fsync_directory(path.parent)


def _read_json_objects(
    path: Path,
    *,
    label: str,
    item_label: str,
    max_bytes: int,
    max_items: int,
) -> list[JSONObject]:
    value = _parse_json(
        _read_regular(
            path,
            label=label,
            max_bytes=max_bytes,
        ),
        label=label,
        canonical=True,
    )
    items = _array(value, label=label)
    if len(items) > max_items:
        raise ReleaseCandidateError(f"{label} has too many items")
    return [_object(item, label=item_label) for item in items]


def _command_create(args: argparse.Namespace) -> int:
    bundle_root = Path(args.bundle_root)
    output = Path(args.output)
    if output.parent.absolute() != bundle_root.absolute() or output.name != "candidate-manifest.json":
        raise ReleaseCandidateError("candidate output must be bundle-root/candidate-manifest.json")
    _verify_bundle_layout(bundle_root, manifest_optional=True)
    source_archive = _read_regular(
        Path(args.source_archive),
        label="source archive",
        max_bytes=MAX_SOURCE_ARCHIVE_BYTES,
    )
    if (
        _read_regular(
            bundle_root / "source.tar",
            label="bundle source.tar",
            max_bytes=MAX_SOURCE_ARCHIVE_BYTES,
        )
        != source_archive
    ):
        raise ReleaseCandidateError("bundle source.tar does not equal supplied source archive")
    _verify_source_root(
        Path(args.source_root),
        archive_raw=source_archive,
        version=args.version,
        commit_sha=args.source_sha,
        tree_sha=args.source_tree,
        require_git_identity=True,
    )
    checks = _read_json_objects(
        Path(args.checks),
        label="checks",
        item_label="check",
        max_bytes=MAX_CHECKS_INPUT_BYTES,
        max_items=len(CHECK_NAMES),
    )
    subjects = _read_json_objects(
        Path(args.attestation_subjects),
        label="attestation subjects",
        item_label="attestation subject",
        max_bytes=MAX_ATTESTATION_SUBJECTS_INPUT_BYTES,
        max_items=MAX_ATTESTATION_SUBJECTS,
    )
    descriptor = _verify_oci_graph(bundle_root, expected_source_sha=args.source_sha)
    artifacts = _artifact_inventory(bundle_root)
    receipts = _verify_qualification_layout(
        bundle_root,
        checks,
        expected_artifact_set_digest=artifact_set_digest(artifacts),
        expected_repository=args.repository,
        expected_repository_id=args.repository_id,
        expected_candidate_workflow_id=args.workflow_id,
    )
    manifest = build_candidate_manifest(
        version=args.version,
        repository=args.repository,
        repository_id=args.repository_id,
        commit_sha=args.source_sha,
        tree_sha=args.source_tree,
        archive_sha256=_sha256(source_archive),
        archive_size_bytes=len(source_archive),
        workflow_id=args.workflow_id,
        workflow_ref=args.workflow_ref,
        workflow_sha=args.workflow_sha,
        run_id=args.workflow_run_id,
        run_attempt=args.workflow_run_attempt,
        checks=checks,
        attestation_subjects=subjects,
        artifacts=artifacts,
        check_receipts=receipts,
    )
    _verify_attestation_subjects(bundle_root, manifest, descriptor)
    raw = canonical_json_bytes(manifest)
    created = _write_once(output, raw, idempotent=True)
    try:
        verify_candidate_bundle(
            manifest,
            bundle_root=bundle_root,
            source_root=Path(args.source_root),
            _require_git_identity=True,
        )
    except Exception as verification_error:
        if created:
            try:
                _remove_created_output(output, raw)
            except Exception as cleanup_error:
                raise ReleaseCandidateError(
                    f"post-write verification and cleanup failed: {cleanup_error}"
                ) from verification_error
        raise
    print(candidate_manifest_digest(manifest))
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    expected = _require_pattern(args.expected_digest, DIGEST_RE, label="expected digest")
    manifest = load_candidate_manifest(Path(args.manifest))
    if candidate_manifest_digest(manifest) != expected:
        raise ReleaseCandidateError("candidate manifest digest mismatch")
    summary = verify_candidate_bundle(
        manifest,
        bundle_root=Path(args.bundle_root),
        source_root=Path(args.source_root),
    )
    print(canonical_json_bytes(summary).decode("utf-8"))
    return 0


def _command_digest(args: argparse.Namespace) -> int:
    print(candidate_manifest_digest(load_candidate_manifest(Path(args.manifest))))
    return 0


def _command_verify_actions_artifact(args: argparse.Namespace) -> int:
    receipt = verify_actions_artifact(
        _read_regular(
            Path(args.observation),
            label="artifact observation",
            max_bytes=MAX_ACTIONS_OBSERVATION_BYTES,
        ),
        _read_regular(
            Path(args.run_observation),
            label="workflow run observation",
            max_bytes=MAX_ACTIONS_OBSERVATION_BYTES,
        ),
        expected_name=args.expected_name,
        expected_run_id=args.expected_run_id,
        expected_run_attempt=args.expected_run_attempt,
        expected_source_sha=args.expected_source_sha,
        retention_days=args.retention_days,
    )
    _write_once(Path(args.output), canonical_json_bytes(receipt), idempotent=True)
    return 0


def _command_extract(args: argparse.Namespace) -> int:
    extract_actions_artifact(
        Path(args.archive), expected_digest=args.expected_digest, output=Path(args.output)
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("--source-root", required=True)
    create.add_argument("--bundle-root", required=True)
    create.add_argument("--version", required=True)
    create.add_argument("--source-sha", required=True)
    create.add_argument("--source-tree", required=True)
    create.add_argument("--source-archive", required=True)
    create.add_argument("--repository", required=True)
    create.add_argument("--repository-id", required=True, type=int)
    create.add_argument("--workflow-id", required=True, type=int)
    create.add_argument("--workflow-run-id", required=True, type=int)
    create.add_argument("--workflow-run-attempt", required=True, type=int)
    create.add_argument("--workflow-ref", required=True)
    create.add_argument("--workflow-sha", required=True)
    create.add_argument("--checks", required=True)
    create.add_argument("--attestation-subjects", required=True)
    create.add_argument("--output", required=True)
    create.set_defaults(handler=_command_create)

    verify = commands.add_parser("verify")
    verify.add_argument("manifest")
    verify.add_argument("--bundle-root", required=True)
    verify.add_argument("--source-root", required=True)
    verify.add_argument("--expected-digest", required=True)
    verify.set_defaults(handler=_command_verify)

    digest = commands.add_parser("digest")
    digest.add_argument("manifest")
    digest.set_defaults(handler=_command_digest)

    artifact = commands.add_parser("verify-actions-artifact")
    artifact.add_argument("observation")
    artifact.add_argument("--run-observation", required=True)
    artifact.add_argument("--expected-name", required=True)
    artifact.add_argument("--expected-run-id", required=True, type=int)
    artifact.add_argument("--expected-run-attempt", required=True, type=int)
    artifact.add_argument("--expected-source-sha", required=True)
    artifact.add_argument("--retention-days", required=True, type=int)
    artifact.add_argument("--output", required=True)
    artifact.set_defaults(handler=_command_verify_actions_artifact)

    extract = commands.add_parser("extract-actions-artifact")
    extract.add_argument("archive")
    extract.add_argument("--expected-digest", required=True)
    extract.add_argument("--output", required=True)
    extract.set_defaults(handler=_command_extract)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return cast(int, args.handler(args))
    except (ReleaseCandidateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
