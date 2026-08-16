"""S2 Task 1 contract tests for immutable release-candidate evidence."""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import stat
import struct
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as distribution_version
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_candidate_manifest.py"
CANDIDATE_SCHEMA = ROOT / "schemas" / "kestrel.release_candidate.v1.schema.json"
ARTIFACT_SCHEMA = ROOT / "schemas" / "kestrel.actions_artifact_observation.v1.schema.json"

sys.path.insert(0, str(ROOT))
from scripts import release_candidate_manifest as subject  # noqa: E402

VERSION = "0.6.0"
REPOSITORY = "John-MiracleWorker/Kestrel"
REPOSITORY_ID = 303
WORKFLOW_ID = 404
WORKFLOW_RUN_ID = 707
WORKFLOW_PATH = ".github/workflows/release-candidate.yml"
OCI_REPOSITORY = "ghcr.io/john-miracleworker/kestrel"
CANONICALIZATION_VECTOR_DIGEST = (
    "sha256:7d37d1815caf0bb822d1244edcde67b2872cac94c9a6238d036198c55d196054"
)
CHECK_NAMES = (
    "nine-row-exact-wheel",
    "oci-layout",
    "protected-main-ci",
    "release-payload",
    "release-rehearsal",
    "runtime-reliability-qualification",
)
CANDIDATE_RUN_CHECK_NAMES = {
    "nine-row-exact-wheel",
    "oci-layout",
    "release-payload",
}
CHECK_WORKFLOW_PATHS = {
    "nine-row-exact-wheel": WORKFLOW_PATH,
    "oci-layout": WORKFLOW_PATH,
    "protected-main-ci": ".github/workflows/ci.yml",
    "release-payload": WORKFLOW_PATH,
    "release-rehearsal": ".github/workflows/release-rehearsal.yml",
    "runtime-reliability-qualification": ".github/workflows/determinism.yml",
}
MAX_CANDIDATE_ARTIFACTS = 512
MAX_ATTESTATION_SUBJECTS = MAX_CANDIDATE_ARTIFACTS + 1
MAX_CANDIDATE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_PATH_BYTES = 4096
MAX_ARCHIVE_PATH_COMPONENTS = 256


def _canonical(value: object) -> bytes:
    """Independent canonical encoder for these integer-only ASCII-key fixtures."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _source_bundle_digest(entries: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(b"Kestrel-Source-Bundle-v1\0")
    for name in sorted(entries, key=lambda item: item.encode("utf-8")):
        encoded = name.encode("utf-8")
        data = entries[name]
        digest.update(struct.pack(">I", len(encoded)))
        digest.update(encoded)
        digest.update(struct.pack(">Q", len(data)))
        digest.update(data)
    return f"sha256:{digest.hexdigest()}"


def _run(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        list(args), cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _media_type(path: str) -> str:
    if path.endswith(".tar.gz"):
        return "application/gzip"
    if path.endswith(".json"):
        return "application/json"
    if path.endswith(".tar"):
        return "application/x-tar"
    if path.endswith((".whl", ".zip")):
        return "application/zip"
    if path.endswith((".txt", ".md")) or path.endswith("SHA256SUMS"):
        return "text/plain"
    return "application/octet-stream"


def _fixture_artifacts(root: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for top in ("release", "containers"):
        for path in sorted((root / top).rglob("*")):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(root).as_posix()
                raw = path.read_bytes()
                result.append(
                    {
                        "path": relative,
                        "media_type": _media_type(relative),
                        "sha256": _sha256(raw),
                        "size_bytes": len(raw),
                    }
                )
    return sorted(result, key=lambda item: str(item["path"]))


def _oci_platform_archive(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, raw in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.mtime = 0
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return output.getvalue()


def _oci_layer_tar(raw: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as archive:
        info = tarfile.TarInfo("kestrel-layer.txt")
        info.mode = 0o644
        info.mtime = 0
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    return output.getvalue()


@dataclass(frozen=True)
class SourceIdentity:
    root: Path
    archive: bytes
    commit_sha: str
    tree_sha: str


def _make_source(tmp_path: Path) -> SourceIdentity:
    root = tmp_path / "source"
    root.mkdir()
    _write(
        root / "pyproject.toml",
        (
            "[build-system]\nrequires = [\"setuptools\"]\n"
            "build-backend = \"setuptools.build_meta\"\n\n"
            "[project]\nname = \"nested-memvid-agent\"\n"
            f"version = \"{VERSION}\"\n"
        ).encode(),
    )
    _write(root / "src" / "nested_memvid_agent" / "__init__.py", b"VALUE = 1\n")
    _write(root / "scripts" / "release.sh", b"#!/bin/sh\nexit 0\n")
    _write(root / ".gitleaksignore", b"fixture-secret-pattern\n")
    (root / "scripts" / "release.sh").chmod(0o755)
    _run("git", "init", "-q", cwd=root)
    _run("git", "config", "user.name", "Kestrel Test", cwd=root)
    _run("git", "config", "user.email", "kestrel@example.invalid", cwd=root)
    _run("git", "branch", "-M", "main", cwd=root)
    _run("git", "add", ".", cwd=root)
    _run("git", "commit", "-q", "-m", "fixture", cwd=root)
    commit_sha = _run("git", "rev-parse", "HEAD", cwd=root)
    tree_sha = _run("git", "rev-parse", "HEAD^{tree}", cwd=root)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit_sha],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    return SourceIdentity(root, archive, commit_sha, tree_sha)


@dataclass
class CandidateFixture:
    root: Path
    source: SourceIdentity
    checks: list[dict[str, object]]
    receipts: dict[str, bytes]
    subjects: list[dict[str, object]]
    descriptor: dict[str, object]
    index: dict[str, object]
    manifests: dict[str, dict[str, object]]
    configs: dict[str, dict[str, object]]
    layers: dict[str, bytes]
    uncompressed_layers: dict[str, bytes]

    @property
    def manifest_path(self) -> Path:
        return self.root / "candidate-manifest.json"

    def artifacts(self) -> list[dict[str, object]]:
        return _fixture_artifacts(self.root)

    def expected_manifest(self) -> dict[str, object]:
        artifacts = self.artifacts()
        return {
            "schema": "kestrel.release_candidate.v1",
            "version": VERSION,
            "tag": f"v{VERSION}",
            "source": {
                "repository": REPOSITORY,
                "repository_id": REPOSITORY_ID,
                "commit_sha": self.source.commit_sha,
                "tree_sha": self.source.tree_sha,
                "archive_sha256": _sha256(self.source.archive),
                "size_bytes": len(self.source.archive),
            },
            "candidate_run": {
                "workflow_id": WORKFLOW_ID,
                "workflow_ref": "refs/heads/main",
                "workflow_sha": self.source.commit_sha,
                "run_id": WORKFLOW_RUN_ID,
                "run_attempt": 1,
            },
            "checks": copy.deepcopy(self.checks),
            "attestation_subjects": copy.deepcopy(self.subjects),
            "artifacts": artifacts,
            "artifact_set_digest": _sha256(_canonical(artifacts)),
            "planned_surfaces": ["ghcr", "github_release", "github_tag", "pypi"],
            "evidence": {
                "source_bundle_digest": _source_bundle_digest(self.receipts),
                "canonicalization_vector_digest": CANONICALIZATION_VECTOR_DIGEST,
            },
            "provenance": {
                "producer": "scripts/release_candidate_manifest.py",
                "provider": "github.com",
                "method": "candidate-run-finalization",
            },
            "confidence": 1,
            "validation_status": "validated",
        }

    def write_manifest(self, manifest: dict[str, object] | None = None) -> dict[str, object]:
        value = manifest or self.expected_manifest()
        self.manifest_path.write_bytes(_canonical(value))
        return value


def _oci_descriptor(raw: bytes, media_type: str) -> dict[str, object]:
    return {"mediaType": media_type, "digest": _sha256(raw), "size": len(raw)}


def _observed_check_receipt(
    *,
    name: str,
    offset: int,
    source_sha: str,
    artifact_set_digest: str,
) -> dict[str, object]:
    run_id = WORKFLOW_RUN_ID if name in CANDIDATE_RUN_CHECK_NAMES else 900 + offset
    workflow_id = WORKFLOW_ID if name in CANDIDATE_RUN_CHECK_NAMES else 1400 + offset
    artifacts: list[dict[str, object]] = []
    if name != "protected-main-ci":
        artifact_raw = f"{name}:{run_id}:{source_sha}".encode("ascii")
        artifacts.append(
            {
                "artifact_id": 2400 + offset,
                "name": f"kestrel-{name}-{source_sha}",
                "api_digest": _sha256(artifact_raw),
                "size_bytes": len(artifact_raw),
                "expired": False,
                "run_id": run_id,
                "run_attempt": 1,
                "source_sha": source_sha,
            }
        )
    receipt: dict[str, object] = {
        "schema": f"kestrel.check.{name}.v1",
        "name": name,
        "status": "success",
        "subject_sha": source_sha,
        "run_id": run_id,
        "run_attempt": 1,
        "workflow": {
            "repository": REPOSITORY,
            "repository_id": REPOSITORY_ID,
            "workflow_id": workflow_id,
            "workflow_path": CHECK_WORKFLOW_PATHS[name],
            "workflow_ref": "refs/heads/main",
            "workflow_sha": source_sha,
            "run_id": run_id,
            "run_attempt": 1,
            "event": "workflow_dispatch"
            if name in CANDIDATE_RUN_CHECK_NAMES
            else "push",
            "head_sha": source_sha,
            "status": "in_progress"
            if name in CANDIDATE_RUN_CHECK_NAMES
            else "completed",
            "conclusion": None
            if name in CANDIDATE_RUN_CHECK_NAMES
            else "success",
        },
        "jobs": [
            {
                "job_id": 3400 + offset,
                "name": f"{name}-aggregate",
                "run_id": run_id,
                "run_attempt": 1,
                "head_sha": source_sha,
                "status": "completed",
                "conclusion": "success",
            }
        ],
        "artifacts": artifacts,
        "evidence": {
            "workflow_observation_digest": _sha256(
                f"workflow:{name}:{run_id}".encode("ascii")
            ),
            "jobs_observation_digest": _sha256(
                f"jobs:{name}:{run_id}".encode("ascii")
            ),
            "artifacts_observation_digest": _sha256(
                f"artifacts:{name}:{run_id}".encode("ascii")
            ),
            "job_count": 1,
            "artifact_count": len(artifacts),
            "complete": True,
            "canonicalization_vector_digest": CANONICALIZATION_VECTOR_DIGEST,
        },
        "provenance": {
            "producer": WORKFLOW_PATH,
            "provider": "github.com",
            "method": "actions-check-observation",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    if name in CANDIDATE_RUN_CHECK_NAMES:
        receipt["artifact_set_digest"] = artifact_set_digest
    return receipt


def _make_candidate(tmp_path: Path) -> CandidateFixture:
    source = _make_source(tmp_path)
    root = tmp_path / "bundle"
    root.mkdir()
    _write(root / "source.tar", source.archive)

    release_files = {
        "nested_memvid_agent-0.6.0-py3-none-any.whl": b"wheel-0.6.0",
        "nested-memvid-agent-0.6.0.tar.gz": b"sdist-0.6.0",
        "sbom.cdx.json": _canonical({"bomFormat": "CycloneDX", "specVersion": "1.6"}),
        "oci-image-digests.json": _canonical({"schema": "kestrel.oci_image_digests.v1"}),
    }
    sums = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in sorted(release_files.items())
    ).encode("ascii")
    release_files["SHA256SUMS"] = sums
    for name, raw in release_files.items():
        _write(root / "release" / name, raw)

    configs: dict[str, dict[str, object]] = {}
    manifests: dict[str, dict[str, object]] = {}
    layers: dict[str, bytes] = {}
    uncompressed_layers: dict[str, bytes] = {}
    platform_descriptors: list[dict[str, object]] = []
    descriptor_platforms: list[dict[str, object]] = []
    layout_marker_raw = _canonical({"imageLayoutVersion": "1.0.0"})
    for architecture in ("amd64", "arm64"):
        uncompressed_layer = _oci_layer_tar(
            f"uncompressed-layer-{architecture}-bytes".encode()
        )
        layer = gzip.compress(uncompressed_layer, mtime=0)
        layers[architecture] = layer
        uncompressed_layers[architecture] = uncompressed_layer
        config = {
            "architecture": architecture,
            "config": {
                "Cmd": ["kestrel", "open"],
                "Env": ["PATH=/usr/local/bin:/usr/bin:/bin"],
            },
            "created": "2026-08-13T12:00:00Z",
            "history": [
                {
                    "created": "2026-08-13T12:00:00Z",
                    "created_by": "Kestrel deterministic fixture",
                }
            ],
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [_sha256(uncompressed_layer)]},
        }
        config_raw = _canonical(config)
        configs[architecture] = config
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": _oci_descriptor(config_raw, "application/vnd.oci.image.config.v1+json"),
            "layers": [
                _oci_descriptor(layer, "application/vnd.oci.image.layer.v1.tar+gzip")
            ],
        }
        manifest_raw = _canonical(manifest)
        manifests[architecture] = manifest
        for raw in (layer, config_raw, manifest_raw):
            _write(
                root
                / "containers"
                / "oci-layout"
                / "blobs"
                / "sha256"
                / hashlib.sha256(raw).hexdigest(),
                raw,
            )
        platform_descriptor = {
            **_oci_descriptor(
                manifest_raw, "application/vnd.oci.image.manifest.v1+json"
            ),
            "platform": {"architecture": architecture, "os": "linux"},
        }
        platform_descriptors.append(platform_descriptor)
        platform_index = _canonical(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [platform_descriptor],
            }
        )
        archive = _oci_platform_archive(
            {
                "oci-layout": layout_marker_raw,
                "index.json": platform_index,
                f"blobs/sha256/{hashlib.sha256(manifest_raw).hexdigest()}": manifest_raw,
                f"blobs/sha256/{hashlib.sha256(config_raw).hexdigest()}": config_raw,
                f"blobs/sha256/{hashlib.sha256(layer).hexdigest()}": layer,
            }
        )
        archive_path = f"containers/kestrel-linux-{architecture}.tar"
        _write(root / archive_path, archive)
        manifest_digest = _sha256(manifest_raw)
        descriptor_platforms.append(
            {
                "os": "linux",
                "architecture": architecture,
                "manifest_digest": manifest_digest,
                "manifest_ref": f"{OCI_REPOSITORY}@{manifest_digest}",
                "config_digest": _sha256(config_raw),
                "archive_path": archive_path,
                "archive_sha256": _sha256(archive),
            }
        )
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": platform_descriptors,
    }
    index_raw = _canonical(index)
    index_path = "containers/oci-layout/index.json"
    _write(root / index_path, index_raw)
    _write(
        root / "containers" / "oci-layout" / "oci-layout",
        layout_marker_raw,
    )
    descriptor = {
        "schema": "kestrel.oci_descriptor.v1",
        "repository": OCI_REPOSITORY,
        "source_sha": source.commit_sha,
        "index_digest": _sha256(index_raw),
        "index_ref": f"{OCI_REPOSITORY}@{_sha256(index_raw)}",
        "index_manifest_path": index_path,
        "platforms": descriptor_platforms,
    }
    _write(root / "containers" / "oci-descriptor.json", _canonical(descriptor))

    artifact_set_digest = _sha256(_canonical(_fixture_artifacts(root)))
    receipts: dict[str, bytes] = {}
    checks: list[dict[str, object]] = []
    for offset, name in enumerate(CHECK_NAMES):
        run_id = (
            WORKFLOW_RUN_ID if name in CANDIDATE_RUN_CHECK_NAMES else 900 + offset
        )
        receipt = _observed_check_receipt(
            name=name,
            offset=offset,
            source_sha=source.commit_sha,
            artifact_set_digest=artifact_set_digest,
        )
        raw = _canonical(receipt)
        receipts[name] = raw
        receipt_path = f"qualification/receipts/{name}.json"
        _write(root / receipt_path, raw)
        checks.append(
            {
                "name": name,
                "status": "success",
                "subject_sha": source.commit_sha,
                "run_id": run_id,
                "run_attempt": 1,
                "receipt_path": receipt_path,
                "receipt_sha256": _sha256(raw),
            }
        )

    subjects = [
        {
            "kind": "file",
            "name": f"release/{name}",
            "digest": _sha256(raw),
        }
        for name, raw in sorted(release_files.items())
    ]
    subjects.append(
        {
            "kind": "oci_index",
            "name": OCI_REPOSITORY,
            "digest": descriptor["index_digest"],
        }
    )
    subjects.sort(key=lambda item: (str(item["kind"]), str(item["name"])))
    _write(root / "attestations.json", _canonical(subjects))
    return CandidateFixture(
        root=root,
        source=source,
        checks=checks,
        receipts=receipts,
        subjects=subjects,
        descriptor=descriptor,
        index=index,
        manifests=manifests,
        configs=configs,
        layers=layers,
        uncompressed_layers=uncompressed_layers,
    )


@pytest.fixture()
def candidate(tmp_path: Path) -> CandidateFixture:
    return _make_candidate(tmp_path)


def _create_args(candidate: CandidateFixture, tmp_path: Path) -> list[str]:
    checks_path = tmp_path / "checks.json"
    checks_path.write_bytes(_canonical(candidate.checks))
    subjects_path = tmp_path / "subjects.json"
    subjects_path.write_bytes(_canonical(candidate.subjects))
    archive_path = tmp_path / "source-archive.tar"
    archive_path.write_bytes(candidate.source.archive)
    return [
        "create",
        "--source-root",
        str(candidate.source.root),
        "--bundle-root",
        str(candidate.root),
        "--version",
        VERSION,
        "--source-sha",
        candidate.source.commit_sha,
        "--source-tree",
        candidate.source.tree_sha,
        "--source-archive",
        str(archive_path),
        "--repository",
        REPOSITORY,
        "--repository-id",
        str(REPOSITORY_ID),
        "--workflow-id",
        str(WORKFLOW_ID),
        "--workflow-run-id",
        str(WORKFLOW_RUN_ID),
        "--workflow-run-attempt",
        "1",
        "--workflow-ref",
        "refs/heads/main",
        "--workflow-sha",
        candidate.source.commit_sha,
        "--checks",
        str(checks_path),
        "--attestation-subjects",
        str(subjects_path),
        "--output",
        str(candidate.manifest_path),
    ]


# ---------------------------------------------------------------------------
# Canonical JSON and schemas
# ---------------------------------------------------------------------------


def test_canonical_json_known_answers() -> None:
    assert subject.canonical_json_bytes({"b": "é", "a": 1}) == bytes.fromhex(
        "7b2261223a312c2262223a22c3a9227d"
    )
    assert subject.canonical_json_bytes(
        {"min": -9007199254740991, "max": 9007199254740991}
    ) == bytes.fromhex(
        "7b226d6178223a393030373139393235343734303939312c226d696e223a2d393030373139393235343734303939317d"
    )


def test_canonicalization_and_schema_validator_versions_are_pinned() -> None:
    assert distribution_version("rfc8785") == "0.1.4"
    assert distribution_version("jsonschema") == "4.26.0"


@pytest.mark.parametrize(
    "value",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": 1.25},
        {"value": 9007199254740992},
        {"value": "e\N{COMBINING ACUTE ACCENT}"},
        {"value": "\x00"},
        {1: "non-string-key"},
    ],
)
def test_canonical_json_rejects_non_i_json(value: object) -> None:
    with pytest.raises(ValueError):
        subject.canonical_json_bytes(value)


def _walk_schema(value: object, location: str = "$") -> list[str]:
    problems: list[str] = []
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#/"):
            problems.append(f"remote ref at {location}")
        if value.get("type") == "object" or "properties" in value:
            properties = value.get("properties")
            if not isinstance(properties, dict) or not properties:
                problems.append(f"unconstrained object at {location}")
            elif set(value.get("required", [])) != set(properties):
                problems.append(f"non-exact required list at {location}")
            if value.get("additionalProperties") is not False:
                problems.append(f"open object at {location}")
        if value.get("type") == "array":
            if not isinstance(value.get("items"), dict):
                problems.append(f"unconstrained array at {location}")
            if (
                "minItems" not in value
                or "maxItems" not in value
                or value.get("uniqueItems") is not True
            ):
                problems.append(f"unbounded array at {location}")
            if "x-sort-key" not in value:
                problems.append(f"unsorted array at {location}")
        for key, child in value.items():
            problems.extend(_walk_schema(child, f"{location}/{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            problems.extend(_walk_schema(child, f"{location}/{index}"))
    return problems


@pytest.mark.parametrize("path", [CANDIDATE_SCHEMA, ARTIFACT_SCHEMA])
def test_committed_schemas_are_valid_and_recursively_closed(path: Path) -> None:
    schema = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["unevaluatedProperties"] is False
    assert _walk_schema(schema) == []


def test_candidate_schema_accepts_independent_positive(candidate: CandidateFixture) -> None:
    schema = json.loads(CANDIDATE_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(candidate.expected_manifest())


def test_candidate_schema_declares_exact_collection_ceilings() -> None:
    schema = json.loads(CANDIDATE_SCHEMA.read_text(encoding="utf-8"))
    properties = schema["properties"]

    assert properties["artifacts"]["maxItems"] == MAX_CANDIDATE_ARTIFACTS
    assert (
        properties["attestation_subjects"]["maxItems"]
        == MAX_ATTESTATION_SUBJECTS
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-root",
        "missing-root",
        "mutable-approval",
        "workflow-attempt",
        "failed-check",
        "unknown-check",
        "bad-subject",
        "artifact-outside-roots",
        "confidence",
        "validation-status",
    ],
)
def test_candidate_schema_rejects_one_rule_mutants(
    candidate: CandidateFixture, mutation: str
) -> None:
    schema = json.loads(CANDIDATE_SCHEMA.read_text(encoding="utf-8"))
    manifest = candidate.expected_manifest()
    if mutation == "unknown-root":
        manifest["unknown"] = True
    elif mutation == "missing-root":
        del manifest["tag"]
    elif mutation == "mutable-approval":
        manifest["approval"] = {"actor": "owner"}
    elif mutation == "workflow-attempt":
        manifest["candidate_run"]["run_attempt"] = 2  # type: ignore[index]
    elif mutation == "failed-check":
        manifest["checks"][0]["status"] = "failure"  # type: ignore[index]
    elif mutation == "unknown-check":
        manifest["checks"][0]["name"] = "other"  # type: ignore[index]
    elif mutation == "bad-subject":
        manifest["attestation_subjects"][0]["kind"] = "tag"  # type: ignore[index]
    elif mutation == "artifact-outside-roots":
        manifest["artifacts"][0]["path"] = "qualification/x"  # type: ignore[index]
    elif mutation == "confidence":
        manifest["confidence"] = 0
    elif mutation == "validation-status":
        manifest["validation_status"] = "pending"
    else:  # pragma: no cover
        raise AssertionError(mutation)
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(manifest))


def test_candidate_semantics_reject_tag_drift(candidate: CandidateFixture) -> None:
    manifest = candidate.expected_manifest()
    manifest["tag"] = "v9.9.9"
    candidate.write_manifest(manifest)

    with pytest.raises(ValueError, match="tag must be derived from version"):
        subject.load_candidate_manifest(candidate.manifest_path)


@pytest.mark.parametrize(
    "workflow_ref", ["refs/heads/feature/release", "refs/tags/v0.6.0"]
)
def test_candidate_semantics_require_protected_main_candidate_ref(
    candidate: CandidateFixture, workflow_ref: str
) -> None:
    manifest = candidate.expected_manifest()
    manifest["candidate_run"]["workflow_ref"] = workflow_ref  # type: ignore[index]
    candidate.write_manifest(manifest)

    with pytest.raises(ValueError, match="refs/heads/main|protected main"):
        subject.load_candidate_manifest(candidate.manifest_path)


# ---------------------------------------------------------------------------
# Manifest construction, load, CLI, and complete bundle verification
# ---------------------------------------------------------------------------


def _build(candidate: CandidateFixture, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "version": VERSION,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "commit_sha": candidate.source.commit_sha,
        "tree_sha": candidate.source.tree_sha,
        "archive_sha256": _sha256(candidate.source.archive),
        "archive_size_bytes": len(candidate.source.archive),
        "workflow_id": WORKFLOW_ID,
        "workflow_ref": "refs/heads/main",
        "workflow_sha": candidate.source.commit_sha,
        "run_id": WORKFLOW_RUN_ID,
        "run_attempt": 1,
        "checks": candidate.checks,
        "attestation_subjects": candidate.subjects,
        "artifacts": candidate.artifacts(),
        "check_receipts": candidate.receipts,
    }
    values.update(overrides)
    return subject.build_candidate_manifest(**values)  # type: ignore[arg-type]


def test_build_candidate_manifest_matches_independent_reconstruction(
    candidate: CandidateFixture,
) -> None:
    assert _build(candidate) == candidate.expected_manifest()


def test_digest_helpers_match_independent_sha256(candidate: CandidateFixture) -> None:
    manifest = candidate.expected_manifest()
    artifacts = candidate.artifacts()
    assert subject.artifact_set_digest(artifacts) == _sha256(_canonical(artifacts))
    assert subject.candidate_manifest_digest(manifest) == _sha256(_canonical(manifest))


def _call_candidate_manifest_api(
    api: str,
    candidate: CandidateFixture,
    manifest: Mapping[str, object],
) -> object:
    if api == "digest":
        return subject.candidate_manifest_digest(manifest)
    if api == "verify":
        return subject.verify_candidate_bundle(
            manifest,
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )
    raise AssertionError(api)


@pytest.mark.parametrize("api", ["digest", "verify"])
def test_public_candidate_manifest_apis_bound_root_mapping_iteration(
    candidate: CandidateFixture,
    api: str,
) -> None:
    plain = candidate.expected_manifest()
    candidate.write_manifest(plain)

    class DeceptiveManifest(Mapping[str, object]):
        def __init__(self) -> None:
            self.iterated = 0

        def __getitem__(self, name: str) -> object:
            return plain.get(name, 1)

        def __iter__(self) -> Iterator[str]:
            for name in plain:
                self.iterated += 1
                yield name
            for index in range(1_000):
                self.iterated += 1
                yield f"extra-{index}"

        def __len__(self) -> int:
            raise AssertionError("candidate snapshot called custom root length")

    manifest = DeceptiveManifest()

    with pytest.raises(ValueError, match="too many properties"):
        _call_candidate_manifest_api(api, candidate, manifest)

    assert manifest.iterated == len(plain) + 1


@pytest.mark.parametrize("api", ["digest", "verify"])
def test_public_candidate_manifest_apis_snapshot_nested_values_immediately(
    candidate: CandidateFixture,
    api: str,
) -> None:
    plain = candidate.expected_manifest()
    candidate.write_manifest(plain)
    values = copy.deepcopy(plain)
    mutable_checks = values["checks"]
    assert isinstance(mutable_checks, list)
    ordered_keys = ["checks", *[name for name in plain if name != "checks"]]

    class MutatingManifest(Mapping[str, object]):
        def __getitem__(self, name: str) -> object:
            if name == "attestation_subjects":
                first_check = mutable_checks[0]
                assert isinstance(first_check, dict)
                first_check["status"] = "failure"
            return values[name]

        def __iter__(self) -> Iterator[str]:
            return iter(ordered_keys)

        def __len__(self) -> int:
            raise AssertionError("candidate snapshot called custom root length")

    result = _call_candidate_manifest_api(api, candidate, MutatingManifest())

    if api == "digest":
        assert result == _sha256(_canonical(plain))
    else:
        assert isinstance(result, dict)
        assert result["candidate_manifest_digest"] == _sha256(_canonical(plain))


@pytest.mark.parametrize("api", ["digest", "verify"])
@pytest.mark.parametrize(
    "field",
    ["checks", "attestation_subjects", "artifacts", "planned_surfaces"],
)
def test_public_candidate_manifest_apis_do_not_call_custom_collection_lengths(
    candidate: CandidateFixture,
    api: str,
    field: str,
) -> None:
    plain = candidate.expected_manifest()
    candidate.write_manifest(plain)
    manifest = copy.deepcopy(plain)

    class NoLengthList(list[object]):
        def __len__(self) -> int:
            raise AssertionError("candidate snapshot called custom collection length")

    original = manifest[field]
    assert isinstance(original, list)
    manifest[field] = NoLengthList(original)

    result = _call_candidate_manifest_api(api, candidate, manifest)

    if api == "digest":
        assert result == _sha256(_canonical(plain))
    else:
        assert isinstance(result, dict)
        assert result["candidate_manifest_digest"] == _sha256(_canonical(plain))


@pytest.mark.parametrize("api", ["digest", "verify"])
@pytest.mark.parametrize(
    ("field", "limit", "message"),
    [
        ("checks", len(CHECK_NAMES), "too many checks"),
        (
            "attestation_subjects",
            MAX_ATTESTATION_SUBJECTS,
            "too many attestation subjects",
        ),
        ("artifacts", MAX_CANDIDATE_ARTIFACTS, "too many artifacts"),
        ("planned_surfaces", 4, "too many planned surfaces"),
    ],
)
def test_public_candidate_manifest_apis_bound_nested_collection_iteration(
    candidate: CandidateFixture,
    api: str,
    field: str,
    limit: int,
    message: str,
) -> None:
    plain = candidate.expected_manifest()
    candidate.write_manifest(plain)
    manifest = copy.deepcopy(plain)
    source_values = manifest[field]
    assert isinstance(source_values, list)

    class DeceptiveList(list[object]):
        def __init__(self) -> None:
            self.iterated = 0

        def __len__(self) -> int:
            raise AssertionError("candidate snapshot called custom collection length")

        def __iter__(self) -> Iterator[object]:
            for index in range(limit + 1_000):
                self.iterated += 1
                yield source_values[index % list.__len__(source_values)]

    values = DeceptiveList()
    manifest[field] = values

    with pytest.raises(ValueError, match=message):
        _call_candidate_manifest_api(api, candidate, manifest)

    assert values.iterated == limit + 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"workflow_sha": "f" * 40},
        {"run_attempt": 2},
        {"run_attempt": True},
        {"repository_id": 0},
        {"version": "01.2.3"},
        {"workflow_ref": "refs/heads/feature/release"},
        {"workflow_ref": "refs/tags/v0.6.0"},
    ],
)
def test_build_rejects_cross_field_and_identity_drift(
    candidate: CandidateFixture, overrides: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        _build(candidate, **overrides)


def test_build_rejects_check_subject_sha_drift(candidate: CandidateFixture) -> None:
    checks = copy.deepcopy(candidate.checks)
    checks[0]["subject_sha"] = "f" * 40
    with pytest.raises(ValueError):
        _build(candidate, checks=checks)


def test_build_rejects_candidate_check_run_drift(candidate: CandidateFixture) -> None:
    checks = copy.deepcopy(candidate.checks)
    candidate_check = next(
        check for check in checks if check["name"] in CANDIDATE_RUN_CHECK_NAMES
    )
    candidate_check["run_id"] = WORKFLOW_RUN_ID + 1

    with pytest.raises(ValueError, match="candidate check run ID mismatch"):
        _build(candidate, checks=checks)


def test_build_rejects_duplicate_attestation_identity(
    candidate: CandidateFixture,
) -> None:
    subjects = copy.deepcopy(candidate.subjects)
    duplicate = copy.deepcopy(subjects[0])
    duplicate["digest"] = "sha256:" + "f" * 64
    subjects.append(duplicate)
    subjects.sort(key=lambda item: (str(item["kind"]), str(item["name"])))

    with pytest.raises(ValueError, match="attestation subject identity"):
        _build(candidate, attestation_subjects=subjects)


@pytest.mark.parametrize("mutation", ["omission", "injection"])
def test_build_and_load_reject_nonexact_release_attestation_subjects(
    candidate: CandidateFixture, mutation: str
) -> None:
    subjects = copy.deepcopy(candidate.subjects)
    if mutation == "omission":
        subjects.remove(next(item for item in subjects if item["kind"] == "file"))
    else:
        subjects.append(
            {
                "kind": "file",
                "name": "release/not-in-artifacts.bin",
                "digest": "sha256:" + "f" * 64,
            }
        )
    subjects.sort(key=lambda item: (str(item["kind"]), str(item["name"])))

    with pytest.raises(ValueError, match="file attestation subjects"):
        _build(candidate, attestation_subjects=subjects)

    manifest = candidate.expected_manifest()
    manifest["attestation_subjects"] = subjects
    candidate.write_manifest(manifest)
    with pytest.raises(ValueError, match="file attestation subjects"):
        subject.load_candidate_manifest(candidate.manifest_path)


def test_build_rejects_duplicate_artifact_identity(candidate: CandidateFixture) -> None:
    artifacts = candidate.artifacts()
    duplicate = copy.deepcopy(artifacts[0])
    duplicate["sha256"] = "sha256:" + "f" * 64
    artifacts.append(duplicate)
    artifacts.sort(key=lambda item: str(item["path"]))

    with pytest.raises(ValueError, match="artifact path identity"):
        _build(candidate, artifacts=artifacts)


@pytest.mark.parametrize("mutation", ["missing", "extra", "digest-drift"])
def test_build_rejects_nonexact_check_receipt_mapping(
    candidate: CandidateFixture, mutation: str
) -> None:
    receipts = copy.deepcopy(candidate.receipts)
    if mutation == "missing":
        receipts.pop(CHECK_NAMES[0])
    elif mutation == "extra":
        receipts["extra"] = b"{}"
    elif mutation == "digest-drift":
        receipts[CHECK_NAMES[0]] = b"{}"
    else:  # pragma: no cover
        raise AssertionError(mutation)

    with pytest.raises(ValueError, match="check receipt"):
        _build(candidate, check_receipts=receipts)


def test_load_requires_canonical_unique_integer_only_json(
    candidate: CandidateFixture, tmp_path: Path
) -> None:
    manifest = candidate.expected_manifest()
    candidate.write_manifest(manifest)
    assert subject.load_candidate_manifest(candidate.manifest_path) == manifest
    candidate.manifest_path.write_bytes(_canonical(manifest) + b"\n")
    with pytest.raises(ValueError):
        subject.load_candidate_manifest(candidate.manifest_path)
    candidate.manifest_path.write_bytes(
        b'{"schema":"kestrel.release_candidate.v1","schema":"duplicate"}'
    )
    with pytest.raises(ValueError):
        subject.load_candidate_manifest(candidate.manifest_path)
    candidate.manifest_path.write_bytes(b'{"confidence":1.0}')
    with pytest.raises(ValueError):
        subject.load_candidate_manifest(candidate.manifest_path)


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("artifacts", MAX_CANDIDATE_ARTIFACTS),
        ("attestation_subjects", MAX_ATTESTATION_SUBJECTS),
    ],
)
def test_candidate_collection_ceiling_fails_before_schema_validation(
    candidate: CandidateFixture,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    limit: int,
) -> None:
    manifest = candidate.expected_manifest()
    digest = "sha256:" + "1" * 64
    if field == "artifacts":
        values = [
            {
                "path": f"containers/f{index:04d}",
                "media_type": "application/octet-stream",
                "sha256": digest,
                "size_bytes": 1,
            }
            for index in range(limit + 1)
        ]
        manifest["artifacts"] = values
        manifest["artifact_set_digest"] = _sha256(_canonical(values))
    else:
        manifest["attestation_subjects"] = [
            {
                "kind": "file",
                "name": f"release/f{index:04d}",
                "digest": digest,
            }
            for index in range(limit + 1)
        ]

    schema_called = False

    def unexpected_schema_validation(*_args: object, **_kwargs: object) -> None:
        nonlocal schema_called
        schema_called = True
        raise AssertionError("oversized collection reached JSON Schema validation")

    monkeypatch.setattr(subject, "_validate_schema", unexpected_schema_validation)

    with pytest.raises(ValueError, match="too many"):
        subject._validated_manifest(manifest, label="candidate manifest")

    assert schema_called is False


def test_artifact_set_digest_rejects_oversized_input_before_canonicalizing() -> None:
    artifacts = [
        {
            "path": f"containers/f{index:04d}",
            "media_type": "application/octet-stream",
            "sha256": "sha256:" + "1" * 64,
            "size_bytes": 1,
        }
        for index in range(MAX_CANDIDATE_ARTIFACTS + 1)
    ]

    with pytest.raises(ValueError, match="too many artifacts"):
        subject.artifact_set_digest(artifacts)


def test_artifact_set_digest_bounds_deceptive_sequence_iteration() -> None:
    class DeceptiveArtifacts:
        def __init__(self) -> None:
            self.iterated = 0

        def __len__(self) -> int:
            raise AssertionError("bounded artifact snapshot called custom length")

        def __iter__(self) -> object:
            artifact = {
                "path": "release/payload.bin",
                "media_type": "application/octet-stream",
                "sha256": "sha256:" + "1" * 64,
                "size_bytes": 1,
            }
            for _index in range(MAX_CANDIDATE_ARTIFACTS + 1_000):
                self.iterated += 1
                yield artifact

    artifacts = DeceptiveArtifacts()

    with pytest.raises(ValueError, match="too many artifacts"):
        subject.artifact_set_digest(artifacts)  # type: ignore[arg-type]

    assert artifacts.iterated == MAX_CANDIDATE_ARTIFACTS + 1


def test_artifact_set_digest_does_not_call_custom_item_length() -> None:
    artifact = {
        "path": "release/payload.bin",
        "media_type": "application/octet-stream",
        "sha256": "sha256:" + "1" * 64,
        "size_bytes": 1,
    }

    class NoLengthArtifact(dict[str, object]):
        def __len__(self) -> int:
            raise AssertionError("bounded artifact item snapshot called custom length")

    assert subject.artifact_set_digest([NoLengthArtifact(artifact)]) == (
        subject.artifact_set_digest([artifact])
    )


def test_source_bundle_digest_does_not_call_custom_mapping_length() -> None:
    entries = {"one": b"one", "two": b"two"}

    class NoLengthEntries(Mapping[str, bytes]):
        def __getitem__(self, name: str) -> bytes:
            return entries[name]

        def __iter__(self) -> Iterator[str]:
            return iter(entries)

        def __len__(self) -> int:
            raise AssertionError("bounded source bundle snapshot called custom length")

    assert subject.source_bundle_digest(NoLengthEntries()) == (
        subject.source_bundle_digest(entries)
    )


@pytest.mark.parametrize(
    ("field", "limit", "message"),
    [
        ("checks", len(CHECK_NAMES), "too many checks"),
        (
            "attestation_subjects",
            MAX_ATTESTATION_SUBJECTS,
            "too many attestation subjects",
        ),
        ("artifacts", MAX_CANDIDATE_ARTIFACTS, "too many artifacts"),
    ],
)
def test_public_builder_ignores_reported_length_and_bounds_iteration(
    candidate: CandidateFixture, field: str, limit: int, message: str
) -> None:
    source_values = {
        "checks": candidate.checks,
        "attestation_subjects": candidate.subjects,
        "artifacts": candidate.artifacts(),
    }[field]

    class OversizedSequence:
        def __init__(self) -> None:
            self.length_called = False
            self.iterated = 0

        def __len__(self) -> int:
            self.length_called = True
            return limit + 1

        def __iter__(self) -> object:
            for index in range(limit + 1_000):
                self.iterated += 1
                yield source_values[index % len(source_values)]

    values = OversizedSequence()

    with pytest.raises(ValueError, match=message):
        _build(candidate, **{field: values})

    assert values.length_called is False
    assert values.iterated == limit + 1


@pytest.mark.parametrize(
    ("field", "limit", "message"),
    [
        ("checks", len(CHECK_NAMES), "too many checks"),
        (
            "attestation_subjects",
            MAX_ATTESTATION_SUBJECTS,
            "too many attestation subjects",
        ),
        ("artifacts", MAX_CANDIDATE_ARTIFACTS, "too many artifacts"),
    ],
)
def test_public_builder_bounds_deceptive_collection_iteration(
    candidate: CandidateFixture, field: str, limit: int, message: str
) -> None:
    source_values = {
        "checks": candidate.checks,
        "attestation_subjects": candidate.subjects,
        "artifacts": candidate.artifacts(),
    }[field]

    class DeceptiveSequence:
        def __init__(self) -> None:
            self.iterated = 0

        def __len__(self) -> int:
            raise AssertionError("bounded builder snapshot called custom length")

        def __iter__(self) -> object:
            for index in range(limit + 1_000):
                self.iterated += 1
                yield source_values[index % len(source_values)]

    values = DeceptiveSequence()

    with pytest.raises(ValueError, match=message):
        _build(candidate, **{field: values})

    assert values.iterated == limit + 1


def test_public_builder_bounds_deceptive_receipt_mapping_iteration(
    candidate: CandidateFixture,
) -> None:
    class DeceptiveReceipts:
        def __init__(self) -> None:
            self.iterated = 0

        def __len__(self) -> int:
            raise AssertionError("bounded receipt snapshot called custom length")

        def __iter__(self) -> object:
            for name in CHECK_NAMES:
                self.iterated += 1
                yield name
            for index in range(1_000):
                self.iterated += 1
                yield f"extra-{index}"

        def __getitem__(self, name: str) -> bytes:
            return candidate.receipts.get(name, b"{}")

    receipts = DeceptiveReceipts()

    with pytest.raises(ValueError, match="check receipt"):
        _build(candidate, check_receipts=receipts)

    assert receipts.iterated == len(CHECK_NAMES) + 1


def test_builder_rejects_mutable_receipt_cross_splice(
    candidate: CandidateFixture,
) -> None:
    target_name = CHECK_NAMES[0]
    trigger_name = CHECK_NAMES[1]
    original = candidate.receipts[target_name]
    replacement_value = json.loads(original)
    replacement_value["jobs"][0]["job_id"] += 1
    replacement = _canonical(replacement_value)
    assert len(replacement) == len(original)

    mutable_target = bytearray(original)

    receipt_values: dict[str, object] = dict(candidate.receipts)
    receipt_values[target_name] = mutable_target

    class MutatingReceipts(Mapping[str, object]):
        def __getitem__(self, name: str) -> object:
            if name == trigger_name:
                mutable_target[:] = replacement
            return receipt_values[name]

        def __iter__(self) -> Iterator[str]:
            return iter(receipt_values)

        def __len__(self) -> int:
            raise AssertionError("bounded receipt snapshot called custom length")

    checks = copy.deepcopy(candidate.checks)
    target_check = next(item for item in checks if item["name"] == target_name)
    target_check["receipt_sha256"] = _sha256(replacement)

    with pytest.raises(ValueError, match="check receipt digest mismatch"):
        _build(candidate, checks=checks, check_receipts=MutatingReceipts())


@pytest.mark.parametrize("mutation", ["checks", "receipts"])
def test_builder_validates_exact_check_and_receipt_sets_before_digesting(
    candidate: CandidateFixture,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    checks = copy.deepcopy(candidate.checks)
    receipts = dict(candidate.receipts)
    if mutation == "checks":
        checks[0]["name"] = "unexpected-check"
    else:
        receipts.pop(CHECK_NAMES[0])

    def forbidden_digest(_entries: object) -> str:
        raise AssertionError("invalid check identity reached evidence digesting")

    monkeypatch.setattr(subject, "source_bundle_digest", forbidden_digest)

    with pytest.raises(ValueError, match="exact"):
        _build(candidate, checks=checks, check_receipts=receipts)


def test_load_candidate_manifest_bounds_bytes_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "candidate-manifest.json"
    manifest.write_bytes(b"{" + b" " * 32)
    monkeypatch.setattr(
        subject, "MAX_CANDIDATE_MANIFEST_BYTES", 16, raising=False
    )

    with pytest.raises(ValueError, match="size limit"):
        subject.load_candidate_manifest(manifest)


@pytest.mark.parametrize(
    ("argument", "label", "limit_name"),
    [
        ("--checks", "checks", "MAX_CHECKS_INPUT_BYTES"),
        (
            "--attestation-subjects",
            "attestation subjects",
            "MAX_ATTESTATION_SUBJECTS_INPUT_BYTES",
        ),
    ],
)
def test_create_cli_bounds_json_inputs_before_parsing(
    candidate: CandidateFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argument: str,
    label: str,
    limit_name: str,
) -> None:
    args = _create_args(candidate, tmp_path)
    input_path = Path(args[args.index(argument) + 1])
    input_path.write_bytes(b"[" + b" " * 32)
    monkeypatch.setattr(subject, limit_name, 16)
    original_parse_json = subject._parse_json

    def guarded_parse_json(
        raw: bytes, *, label: str, canonical: bool
    ) -> object:
        if label == input_path.stem.replace("subjects", "attestation subjects"):
            raise AssertionError("oversized CLI input reached JSON parsing")
        return original_parse_json(raw, label=label, canonical=canonical)

    monkeypatch.setattr(subject, "_parse_json", guarded_parse_json)

    assert subject.main(args) == 1
    captured = capsys.readouterr()
    assert label in captured.err
    assert "size limit" in captured.err


@pytest.mark.parametrize(
    ("argument", "label", "count"),
    [
        ("--checks", "check", len(CHECK_NAMES) + 1),
        (
            "--attestation-subjects",
            "attestation subject",
            MAX_ATTESTATION_SUBJECTS + 1,
        ),
    ],
)
def test_create_cli_rejects_oversized_arrays_before_copying_items(
    candidate: CandidateFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argument: str,
    label: str,
    count: int,
) -> None:
    args = _create_args(candidate, tmp_path)
    input_path = Path(args[args.index(argument) + 1])
    input_path.write_bytes(_canonical([{} for _ in range(count)]))
    original_object = subject._object

    def guarded_object(value: object, *, label: str) -> dict[str, object]:
        if label == guarded_label:
            raise AssertionError("oversized CLI array copied an item before rejection")
        return original_object(value, label=label)

    guarded_label = label
    monkeypatch.setattr(subject, "_object", guarded_object)

    assert subject.main(args) == 1
    captured = capsys.readouterr()
    assert "too many" in captured.err


def test_cli_create_verify_digest_round_trip(
    candidate: CandidateFixture, tmp_path: Path
) -> None:
    created = _run_cli(*_create_args(candidate, tmp_path))
    assert created.returncode == 0, created.stderr
    expected = candidate.expected_manifest()
    assert candidate.manifest_path.read_bytes() == _canonical(expected)
    digest = _sha256(_canonical(expected))
    digested = _run_cli("digest", str(candidate.manifest_path))
    assert digested.returncode == 0
    assert digested.stdout.strip() == digest
    verified = _run_cli(
        "verify",
        str(candidate.manifest_path),
        "--bundle-root",
        str(candidate.root),
        "--source-root",
        str(candidate.source.root),
        "--expected-digest",
        digest,
    )
    assert verified.returncode == 0, verified.stderr
    summary = json.loads(verified.stdout)
    assert summary == {
        "artifact_set_digest": expected["artifact_set_digest"],
        "candidate_run": expected["candidate_run"],
        "candidate_manifest_digest": digest,
        "source_sha": candidate.source.commit_sha,
        "source_tree": candidate.source.tree_sha,
        "tag": f"v{VERSION}",
        "version": VERSION,
    }


def test_cli_create_is_idempotent_only_for_exact_existing_bytes(
    candidate: CandidateFixture, tmp_path: Path
) -> None:
    args = _create_args(candidate, tmp_path)
    assert _run_cli(*args).returncode == 0
    before = candidate.manifest_path.read_bytes()
    assert _run_cli(*args).returncode == 0
    assert candidate.manifest_path.read_bytes() == before
    candidate.manifest_path.write_bytes(b"{}")
    result = _run_cli(*args)
    assert result.returncode != 0
    assert candidate.manifest_path.read_bytes() == b"{}"


def test_cli_create_removes_new_output_when_post_write_verification_fails(
    candidate: CandidateFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = subject._parser().parse_args(_create_args(candidate, tmp_path))

    def fail_verification(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise subject.ReleaseCandidateError("injected post-write failure")

    monkeypatch.setattr(subject, "verify_candidate_bundle", fail_verification)
    with pytest.raises(ValueError, match="injected post-write failure"):
        subject._command_create(args)

    assert not candidate.manifest_path.exists()


def test_cli_create_requires_manifest_at_exact_bundle_path(
    candidate: CandidateFixture, tmp_path: Path
) -> None:
    args = _create_args(candidate, tmp_path)
    args[-1] = str(tmp_path / "outside.json")
    result = _run_cli(*args)
    assert result.returncode != 0
    assert not (tmp_path / "outside.json").exists()


def _verify(candidate: CandidateFixture, *, source_root: Path | None = None) -> None:
    manifest = candidate.write_manifest()
    summary = subject.verify_candidate_bundle(
        manifest,
        bundle_root=candidate.root,
        source_root=source_root or candidate.source.root,
    )
    assert summary["candidate_manifest_digest"] == _sha256(_canonical(manifest))


def _task7_run_identity_matches(
    verification: dict[str, object],
    run_observation: dict[str, object],
    artifact_receipt: dict[str, object],
) -> bool:
    candidate_run = verification.get("candidate_run")
    artifact = artifact_receipt.get("artifact")
    if not isinstance(candidate_run, dict) or not isinstance(artifact, dict):
        return False
    return candidate_run == {
        "workflow_id": run_observation.get("workflow_id"),
        "workflow_ref": f"refs/heads/{run_observation.get('head_branch')}",
        "workflow_sha": run_observation.get("head_sha"),
        "run_id": run_observation.get("id"),
        "run_attempt": run_observation.get("run_attempt"),
    } and (
        artifact.get("run_id") == candidate_run.get("run_id")
        and artifact.get("run_attempt") == candidate_run.get("run_attempt")
        and artifact.get("source_sha") == candidate_run.get("workflow_sha")
    )


def test_verify_summary_exposes_exact_task7_run_join_and_rejects_cross_splice(
    candidate: CandidateFixture,
) -> None:
    manifest = candidate.write_manifest()
    verification = subject.verify_candidate_bundle(
        manifest,
        bundle_root=candidate.root,
        source_root=candidate.source.root,
    )
    expected_run = {
        "workflow_id": WORKFLOW_ID,
        "workflow_ref": "refs/heads/main",
        "workflow_sha": candidate.source.commit_sha,
        "run_id": WORKFLOW_RUN_ID,
        "run_attempt": 1,
    }
    assert verification["candidate_run"] == expected_run

    run_observation = {
        "id": WORKFLOW_RUN_ID,
        "workflow_id": WORKFLOW_ID,
        "head_branch": "main",
        "head_sha": candidate.source.commit_sha,
        "run_attempt": 1,
    }
    artifact_receipt = {
        "artifact": {
            "run_id": WORKFLOW_RUN_ID,
            "run_attempt": 1,
            "source_sha": candidate.source.commit_sha,
        }
    }
    assert _task7_run_identity_matches(verification, run_observation, artifact_receipt)

    cross_spliced_run = copy.deepcopy(run_observation)
    cross_spliced_run["id"] = WORKFLOW_RUN_ID + 1
    cross_spliced_artifact = copy.deepcopy(artifact_receipt)
    cross_spliced_artifact["artifact"]["run_id"] = WORKFLOW_RUN_ID + 1  # type: ignore[index]
    assert not _task7_run_identity_matches(
        verification, cross_spliced_run, cross_spliced_artifact
    )


def _replace_check_receipt(
    candidate: CandidateFixture, name: str, receipt: dict[str, object]
) -> None:
    raw = _canonical(receipt)
    check = next(item for item in candidate.checks if item["name"] == name)
    candidate.receipts[name] = raw
    check["receipt_sha256"] = _sha256(raw)
    (candidate.root / str(check["receipt_path"])).write_bytes(raw)


def test_verify_accepts_git_and_exact_extracted_source_roots_with_dotfiles(
    candidate: CandidateFixture, tmp_path: Path
) -> None:
    _verify(candidate)
    extracted = tmp_path / "extracted-source"
    extracted.mkdir()
    with tarfile.open(fileobj=io.BytesIO(candidate.source.archive), mode="r:") as archive:
        archive.extractall(extracted, filter="data")
    _verify(candidate, source_root=extracted)


def test_cli_create_rejects_extracted_source_root(
    candidate: CandidateFixture, tmp_path: Path
) -> None:
    extracted = tmp_path / "extracted-source"
    extracted.mkdir()
    with tarfile.open(fileobj=io.BytesIO(candidate.source.archive), mode="r:") as archive:
        archive.extractall(extracted, filter="data")
    args = _create_args(candidate, tmp_path)
    args[args.index("--source-root") + 1] = str(extracted)

    result = _run_cli(*args)

    assert result.returncode != 0
    assert "exact Git worktree root" in result.stderr
    assert not candidate.manifest_path.exists()


def test_create_time_source_verification_requires_exact_local_main_identity(
    candidate: CandidateFixture,
) -> None:
    _run("git", "checkout", "-q", "-b", "feature/release", cwd=candidate.source.root)
    _write(candidate.source.root / "feature.txt", b"not protected main\n")
    _run("git", "add", "feature.txt", cwd=candidate.source.root)
    _run("git", "commit", "-q", "-m", "feature", cwd=candidate.source.root)
    commit_sha = _run("git", "rev-parse", "HEAD", cwd=candidate.source.root)
    tree_sha = _run("git", "rev-parse", "HEAD^{tree}", cwd=candidate.source.root)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit_sha],
        cwd=candidate.source.root,
        check=True,
        capture_output=True,
    ).stdout

    with pytest.raises(ValueError, match="protected-main identity"):
        subject._verify_source_root(
            candidate.source.root,
            archive_raw=archive,
            version=VERSION,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            require_git_identity=True,
        )


def test_create_time_source_verification_rejects_untracked_inputs(
    candidate: CandidateFixture,
) -> None:
    _write(
        candidate.source.root / "src" / "nested_memvid_agent" / "untracked.py",
        b"UNTRACKED_BUILD_INPUT = True\n",
    )

    with pytest.raises(ValueError, match="untracked"):
        subject._verify_source_root(
            candidate.source.root,
            archive_raw=candidate.source.archive,
            version=VERSION,
            commit_sha=candidate.source.commit_sha,
            tree_sha=candidate.source.tree_sha,
            require_git_identity=True,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema", "kestrel.check.wrong.v1"),
        ("name", "release-rehearsal"),
        ("run_id", WORKFLOW_RUN_ID + 1),
        ("run_attempt", 2),
        ("unexpected", "field"),
    ],
)
def test_build_and_verify_reject_receipt_identity_drift(
    candidate: CandidateFixture, field: str, replacement: object
) -> None:
    name = "release-payload"
    receipt = json.loads(candidate.receipts[name])
    receipt[field] = replacement
    _replace_check_receipt(candidate, name, receipt)

    with pytest.raises(ValueError, match="receipt identity"):
        _build(candidate)

    manifest = candidate.write_manifest()
    with pytest.raises(ValueError, match="receipt identity"):
        subject.verify_candidate_bundle(
            manifest,
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("workflow", "repository"), "attacker/Kestrel"),
        (("workflow", "repository_id"), REPOSITORY_ID + 1),
        (("workflow", "workflow_id"), WORKFLOW_ID + 1),
        (("workflow", "workflow_path"), ".github/workflows/other.yml"),
        (("workflow", "workflow_ref"), "refs/heads/other"),
        (("workflow", "workflow_sha"), "b" * 40),
        (("workflow", "run_id"), WORKFLOW_RUN_ID + 1),
        (("workflow", "run_attempt"), 2),
        (("workflow", "event"), "push"),
        (("workflow", "head_sha"), "b" * 40),
        (("workflow", "status"), "queued"),
        (("workflow", "status"), "completed"),
        (("workflow", "conclusion"), "failure"),
        (("workflow", "conclusion"), "success"),
        (("jobs", 0, "run_id"), WORKFLOW_RUN_ID + 1),
        (("jobs", 0, "run_attempt"), 2),
        (("jobs", 0, "head_sha"), "b" * 40),
        (("jobs", 0, "status"), "in_progress"),
        (("jobs", 0, "conclusion"), "failure"),
        (("artifacts", 0, "api_digest"), "sha256:" + "b" * 63),
        (("artifacts", 0, "run_id"), WORKFLOW_RUN_ID + 1),
        (("artifacts", 0, "run_attempt"), 2),
        (("artifacts", 0, "source_sha"), "b" * 40),
        (("artifacts", 0, "expired"), True),
        (("evidence", "workflow_observation_digest"), "sha256:" + "b" * 63),
        (("evidence", "job_count"), 2),
        (("evidence", "artifact_count"), 2),
        (("evidence", "complete"), False),
        (("evidence", "canonicalization_vector_digest"), "sha256:" + "b" * 64),
        (("provenance", "producer"), ".github/workflows/other.yml"),
        (("provenance", "provider"), "example.invalid"),
        (("provenance", "method"), "self-asserted"),
        (("confidence",), 0),
        (("validation_status",), "unvalidated"),
    ],
)
def test_build_rejects_observed_check_receipt_edge_drift(
    candidate: CandidateFixture,
    path: tuple[str | int, ...],
    replacement: object,
) -> None:
    name = "release-payload"
    receipt = json.loads(candidate.receipts[name])
    target: object = receipt
    for part in path[:-1]:
        if isinstance(part, int):
            assert isinstance(target, list)
            target = target[part]
        else:
            assert isinstance(target, dict)
            target = target[part]
    final = path[-1]
    assert isinstance(final, str)
    assert isinstance(target, dict)
    target[final] = replacement
    _replace_check_receipt(candidate, name, receipt)

    with pytest.raises(ValueError, match="receipt"):
        _build(candidate)


def test_build_rejects_incomplete_or_ambiguous_check_observations(
    candidate: CandidateFixture,
) -> None:
    name = "release-payload"
    receipt = json.loads(candidate.receipts[name])
    jobs = receipt["jobs"]
    assert isinstance(jobs, list)
    duplicate = copy.deepcopy(jobs[0])
    duplicate["name"] = "duplicate-job"
    jobs.append(duplicate)
    evidence = receipt["evidence"]
    assert isinstance(evidence, dict)
    evidence["job_count"] = 2
    _replace_check_receipt(candidate, name, receipt)

    with pytest.raises(ValueError, match="receipt job inventory"):
        _build(candidate)


def test_candidate_run_receipts_bind_the_exact_artifact_set(
    candidate: CandidateFixture,
) -> None:
    assert _build(candidate)["artifact_set_digest"] == subject.artifact_set_digest(
        candidate.artifacts()
    )

    changed = b'{"bomFormat":"CycloneDX","serialNumber":"changed"}'
    (candidate.root / "release" / "sbom.cdx.json").write_bytes(changed)
    sbom_subject = next(
        item
        for item in candidate.subjects
        if item["kind"] == "file" and item["name"] == "release/sbom.cdx.json"
    )
    sbom_subject["digest"] = _sha256(changed)

    with pytest.raises(ValueError, match="artifact set"):
        _build(candidate)


def test_check_receipts_bind_observed_workflow_job_artifact_and_evidence(
    candidate: CandidateFixture,
) -> None:
    artifact_set = _sha256(_canonical(candidate.artifacts()))
    for offset, name in enumerate(CHECK_NAMES):
        _replace_check_receipt(
            candidate,
            name,
            _observed_check_receipt(
                name=name,
                offset=offset,
                source_sha=candidate.source.commit_sha,
                artifact_set_digest=artifact_set,
            ),
        )

    manifest = _build(candidate)
    assert manifest["validation_status"] == "validated"


def test_candidate_run_receipt_binds_truthful_in_progress_workflow_state(
    candidate: CandidateFixture,
) -> None:
    name = "release-payload"
    receipt = json.loads(candidate.receipts[name])
    workflow = receipt["workflow"]
    assert isinstance(workflow, dict)
    workflow["status"] = "in_progress"
    workflow["conclusion"] = None
    _replace_check_receipt(candidate, name, receipt)

    assert _build(candidate)["validation_status"] == "validated"


def test_prerequisite_receipt_requires_terminal_successful_workflow_state(
    candidate: CandidateFixture,
) -> None:
    name = "release-rehearsal"
    receipt = json.loads(candidate.receipts[name])
    workflow = receipt["workflow"]
    assert isinstance(workflow, dict)
    workflow["status"] = "in_progress"
    workflow["conclusion"] = None
    _replace_check_receipt(candidate, name, receipt)

    with pytest.raises(ValueError, match="receipt workflow identity"):
        _build(candidate)


def test_qualification_receipt_size_is_bounded_before_json_parsing(
    candidate: CandidateFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = candidate.checks[0]
    name = str(check["name"])
    oversized = b"x" * 65
    receipt_path = candidate.root / str(check["receipt_path"])
    receipt_path.write_bytes(oversized)
    check["receipt_sha256"] = _sha256(oversized)
    parse_called = False

    def unexpected_parse(
        _raw: bytes, *, label: str, canonical: bool
    ) -> object:
        del canonical
        nonlocal parse_called
        if label == f"receipt {name}":
            parse_called = True
            raise AssertionError("oversized qualification receipt reached JSON parsing")
        raise AssertionError(f"unexpected JSON parse: {label}")

    monkeypatch.setattr(subject, "MAX_SOURCE_BUNDLE_ENTRY_BYTES", 64)
    monkeypatch.setattr(subject, "_parse_json", unexpected_parse)

    with pytest.raises(ValueError, match="receipt .* exceeds its size limit"):
        subject._verify_qualification_layout(
            candidate.root,
            candidate.checks,
            expected_artifact_set_digest=_sha256(_canonical(candidate.artifacts())),
            expected_repository=REPOSITORY,
            expected_repository_id=REPOSITORY_ID,
            expected_candidate_workflow_id=WORKFLOW_ID,
        )

    assert parse_called is False


def test_verify_rejects_source_tar_not_byte_equal_to_git_archive(
    candidate: CandidateFixture,
) -> None:
    altered = candidate.source.archive + b"hidden trailing bytes"
    (candidate.root / "source.tar").write_bytes(altered)
    manifest = candidate.expected_manifest()
    source = manifest["source"]
    assert isinstance(source, dict)
    source["archive_sha256"] = _sha256(altered)
    source["size_bytes"] = len(altered)
    candidate.write_manifest(manifest)

    with pytest.raises(ValueError, match="exact git archive"):
        subject.verify_candidate_bundle(
            manifest,
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


def test_verify_bounds_source_tar_before_parsing(
    candidate: CandidateFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = candidate.write_manifest()
    monkeypatch.setattr(
        subject, "MAX_SOURCE_ARCHIVE_BYTES", len(candidate.source.archive) - 1
    )

    def unexpected_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("oversized source archive must not be parsed")

    monkeypatch.setattr(subject, "_source_archive_identity", unexpected_parse)
    with pytest.raises(ValueError, match="size limit"):
        subject.verify_candidate_bundle(
            manifest,
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


@pytest.mark.parametrize("name", ["CON", "trailing."])
def test_verify_rejects_nonportable_artifact_paths(
    candidate: CandidateFixture, name: str
) -> None:
    raw = b"non-portable"
    _write(candidate.root / "release" / name, raw)
    candidate.subjects.append(
        {"kind": "file", "name": f"release/{name}", "digest": _sha256(raw)}
    )
    candidate.subjects.sort(key=lambda item: (str(item["kind"]), str(item["name"])))
    (candidate.root / "attestations.json").write_bytes(_canonical(candidate.subjects))
    manifest = candidate.write_manifest()

    with pytest.raises(ValueError, match="non-portable path"):
        subject.verify_candidate_bundle(
            manifest,
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "manifest-byte-drift",
        "source-tar-drift",
        "release-file-drift",
        "release-extra",
        "container-extra",
        "receipt-drift",
        "receipt-extra",
        "receipt-nested-extra",
        "attestations-drift",
    ],
)
def test_verify_rejects_bundle_inventory_or_byte_drift(
    candidate: CandidateFixture, mutation: str
) -> None:
    candidate.write_manifest()
    if mutation == "manifest-byte-drift":
        candidate.manifest_path.write_bytes(b"{}")
    elif mutation == "source-tar-drift":
        (candidate.root / "source.tar").write_bytes(b"other")
    elif mutation == "release-file-drift":
        (candidate.root / "release" / "sbom.cdx.json").write_bytes(b"{}")
    elif mutation == "release-extra":
        _write(candidate.root / "release" / "extra.bin", b"extra")
    elif mutation == "container-extra":
        _write(candidate.root / "containers" / "extra.bin", b"extra")
    elif mutation == "receipt-drift":
        (candidate.root / candidate.checks[0]["receipt_path"]).write_bytes(b"{}")  # type: ignore[operator]
    elif mutation == "receipt-extra":
        _write(candidate.root / "qualification" / "receipts" / "extra.json", b"{}")
    elif mutation == "receipt-nested-extra":
        _write(
            candidate.root / "qualification" / "receipts" / "nested" / "extra.json",
            b"{}",
        )
    elif mutation == "attestations-drift":
        (candidate.root / "attestations.json").write_bytes(b"[]")
    else:  # pragma: no cover
        raise AssertionError(mutation)
    with pytest.raises(ValueError):
        subject.verify_candidate_bundle(
            candidate.expected_manifest(),
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


def test_verify_rejects_bundle_symlinks(candidate: CandidateFixture, tmp_path: Path) -> None:
    candidate.write_manifest()
    target = candidate.root / "release" / "sbom.cdx.json"
    target.unlink()
    target.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        subject.verify_candidate_bundle(
            candidate.expected_manifest(),
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


def test_verify_binds_git_source_root(candidate: CandidateFixture) -> None:
    candidate.write_manifest()
    (candidate.source.root / "src" / "nested_memvid_agent" / "__init__.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        subject.verify_candidate_bundle(
            candidate.expected_manifest(),
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


def test_source_archive_rejects_cross_platform_path_collisions() -> None:
    commit_sha = "a" * 40
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output,
        mode="w:",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": commit_sha},
    ) as archive:
        for name in ("Case/one.txt", "case/two.txt"):
            raw = name.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))

    with pytest.raises(ValueError, match="portable path collision"):
        subject._source_archive_identity(
            output.getvalue(), expected_commit_sha=commit_sha
        )


def _source_test_archive(
    entries: list[tuple[str, bytes]], *, commit_sha: str = "a" * 40
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output,
        mode="w:",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": commit_sha},
    ) as archive:
        for name, raw in entries:
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return output.getvalue()


def test_source_archive_rejects_sparse_members_before_extraction() -> None:
    commit_sha = "a" * 40
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output,
        mode="w:",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": commit_sha},
    ) as archive:
        info = tarfile.TarInfo("sparse.bin")
        info.size = 1
        info.pax_headers = {
            "GNU.sparse.map": "0,1",
            "GNU.sparse.realsize": "1048576",
        }
        archive.addfile(info, io.BytesIO(b"X"))
    raw = output.getvalue()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        member = archive.getmembers()[0]
        assert member.sparse is not None
        assert member.size == 1048576

    with pytest.raises(ValueError, match="sparse"):
        subject._source_archive_identity(raw, expected_commit_sha=commit_sha)


def test_source_archive_bounds_each_member_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _source_test_archive([("large.bin", b"X" * 65)])
    monkeypatch.setattr(subject, "MAX_SOURCE_MEMBER_BYTES", 64, raising=False)

    with pytest.raises(ValueError, match="member.*size limit"):
        subject._source_archive_identity(raw, expected_commit_sha="a" * 40)


def test_source_archive_bounds_cumulative_logical_size_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _source_test_archive(
        [("one.bin", b"1" * 24), ("two.bin", b"2" * 24)]
    )
    monkeypatch.setattr(subject, "MAX_SOURCE_TOTAL_BYTES", 32, raising=False)

    with pytest.raises(ValueError, match="expanded size limit"):
        subject._source_archive_identity(raw, expected_commit_sha="a" * 40)


def test_source_archive_enforces_member_limit_without_materializing_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _source_test_archive([("one.bin", b"one")])

    def forbidden_getmembers(_archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
        raise AssertionError("source member validation must iterate lazily")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", forbidden_getmembers)
    _tree, files, _directories = subject._source_archive_identity(
        raw, expected_commit_sha="a" * 40
    )
    assert files == {"one.bin": (0o644, b"one")}


def test_source_archive_bounds_cumulative_path_components_before_trie_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _source_test_archive(
        [("one/deep/file.txt", b"one"), ("two/deep/file.txt", b"two")]
    )
    original_record = subject._record_portable_path
    recorded = 0

    def counted_record(
        parts: tuple[str, ...],
        identities: subject._PortablePathIndex,
        *,
        label: str,
        entry_kind: str | None = None,
    ) -> None:
        nonlocal recorded
        recorded += 1
        original_record(
            parts,
            identities,
            label=label,
            entry_kind=entry_kind,
        )

    monkeypatch.setattr(subject, "_record_portable_path", counted_record)
    monkeypatch.setattr(subject, "MAX_SOURCE_PATH_COMPONENTS", 5, raising=False)

    with pytest.raises(ValueError, match="cumulative path component limit"):
        subject._source_archive_identity(raw, expected_commit_sha="a" * 40)

    assert recorded == 1

    recorded = 0
    monkeypatch.setattr(subject, "MAX_SOURCE_PATH_COMPONENTS", 6, raising=False)
    _tree, files, _directory_index = subject._source_archive_identity(
        raw, expected_commit_sha="a" * 40
    )
    assert recorded == 2
    assert set(files) == {"one/deep/file.txt", "two/deep/file.txt"}


class _PrefixSliceGuard(tuple[str, ...]):
    def __getitem__(self, index: object) -> object:
        if isinstance(index, slice) and index != slice(None, -1, None):
            raise AssertionError("archive validator materialized a path prefix tuple")
        return super().__getitem__(index)  # type: ignore[call-overload]


def _indexed_directory_paths(index: object) -> set[str]:
    assert isinstance(index, subject._PortablePathIndex)
    directories: set[str] = set()
    pending: list[tuple[str, object]] = [("", index)]
    while pending:
        prefix, raw_node = pending.pop()
        assert isinstance(raw_node, subject._PortablePathIndex)
        for actual, child in raw_node.children.values():
            path = actual if not prefix else f"{prefix}/{actual}"
            if child.entry_kind == "directory" or child.children:
                directories.add(path)
            pending.append((path, child))
    return directories


def test_source_archive_collision_tracking_does_not_materialize_prefix_tuples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _source_test_archive(
        [("root/one/deep/file.txt", b"one"), ("root/two/deep/file.txt", b"two")]
    )
    original_safe_path = subject._safe_archive_path

    def guarded_safe_path(name: str, *, label: str) -> tuple[str, ...]:
        return _PrefixSliceGuard(original_safe_path(name, label=label))

    monkeypatch.setattr(subject, "_safe_archive_path", guarded_safe_path)

    _tree, files, directory_index = subject._source_archive_identity(
        raw, expected_commit_sha="a" * 40
    )

    assert set(files) == {"root/one/deep/file.txt", "root/two/deep/file.txt"}
    assert _indexed_directory_paths(directory_index) == {
        "root",
        "root/one",
        "root/one/deep",
        "root/two",
        "root/two/deep",
    }


def test_source_archive_returns_compact_directory_index() -> None:
    path_components = 128
    member_count = 64
    entries = [
        (
            "/".join(
                [
                    f"branch-{member:02d}",
                    *[f"part-{component:03d}" for component in range(path_components - 2)],
                    "file.txt",
                ]
            ),
            b"",
        )
        for member in range(member_count)
    ]

    _tree, files, directory_index = subject._source_archive_identity(
        _source_test_archive(entries), expected_commit_sha="a" * 40
    )

    assert isinstance(directory_index, subject._PortablePathIndex)
    pending = [directory_index]
    indexed_components = 0
    while pending:
        node = pending.pop()
        for actual, child in node.children.values():
            assert "/" not in actual
            indexed_components += 1
            pending.append(child)
    assert indexed_components == member_count * path_components
    assert len(files) == member_count


def test_extracted_source_root_compares_compact_directory_index(
    tmp_path: Path,
) -> None:
    commit_sha = "a" * 40
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output,
        mode="w:",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": commit_sha},
    ) as archive:
        empty = tarfile.TarInfo("empty")
        empty.type = tarfile.DIRTYPE
        empty.mode = 0o755
        archive.addfile(empty)
        nested = tarfile.TarInfo("nested/file.txt")
        nested.mode = 0o644
        nested.size = 4
        archive.addfile(nested, io.BytesIO(b"data"))

    _tree, files, directory_index = subject._source_archive_identity(
        output.getvalue(), expected_commit_sha=commit_sha
    )
    extracted = tmp_path / "extracted"
    (extracted / "empty").mkdir(parents=True)
    _write(extracted / "nested" / "file.txt", b"data")

    subject._verify_extracted_source_root(
        extracted,
        archive_files=files,
        archive_directory_index=directory_index,
    )

    (extracted / "empty").rmdir()
    with pytest.raises(ValueError, match="directory inventory mismatch"):
        subject._verify_extracted_source_root(
            extracted,
            archive_files=files,
            archive_directory_index=directory_index,
        )


def test_extracted_source_comparison_bounds_each_file_before_bulk_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extracted = tmp_path / "extracted"
    _write(extracted / "file.txt", b"x" * 65)
    directory_index = subject._PortablePathIndex()
    subject._record_portable_path(
        ("file.txt",),
        directory_index,
        label="source archive",
        entry_kind="file",
    )
    original_read = subject._read_regular
    observed_max: int | None = None

    def guarded_read(
        path: Path,
        *,
        label: str,
        allow_empty: bool = False,
        max_bytes: int | None = None,
    ) -> bytes:
        nonlocal observed_max
        if label == "source file file.txt":
            observed_max = max_bytes
            if max_bytes is None:
                raise AssertionError("source comparison performed an unbounded bulk read")
        return original_read(
            path,
            label=label,
            allow_empty=allow_empty,
            max_bytes=max_bytes,
        )

    monkeypatch.setattr(subject, "_read_regular", guarded_read)

    with pytest.raises(ValueError, match="size limit"):
        subject._verify_extracted_source_root(
            extracted,
            archive_files={"file.txt": (0o644, b"data")},
            archive_directory_index=directory_index,
        )

    assert observed_max == 4


@pytest.mark.parametrize(
    "entries",
    [
        [("path", b"file"), ("path/child", b"child")],
        [("path/child", b"child"), ("path", b"file")],
    ],
)
def test_source_archive_trie_rejects_file_ancestor_collisions(
    entries: list[tuple[str, bytes]],
) -> None:
    with pytest.raises(ValueError, match="file/directory path collision"):
        subject._source_archive_identity(
            _source_test_archive(entries), expected_commit_sha="a" * 40
        )


# ---------------------------------------------------------------------------
# Complete OCI graph binding
# ---------------------------------------------------------------------------


def _rewrite_descriptor(candidate: CandidateFixture, value: dict[str, object]) -> None:
    (candidate.root / "containers" / "oci-descriptor.json").write_bytes(_canonical(value))


def _rewrite_index(candidate: CandidateFixture, value: dict[str, object]) -> None:
    raw = _canonical(value)
    (candidate.root / "containers" / "oci-layout" / "index.json").write_bytes(raw)
    descriptor = copy.deepcopy(candidate.descriptor)
    descriptor["index_digest"] = _sha256(raw)
    descriptor["index_ref"] = f"{OCI_REPOSITORY}@{_sha256(raw)}"
    _rewrite_descriptor(candidate, descriptor)


def _rebind_candidate_artifacts(candidate: CandidateFixture) -> dict[str, object]:
    artifact_set = _sha256(_canonical(candidate.artifacts()))
    for name in CANDIDATE_RUN_CHECK_NAMES:
        receipt = json.loads(candidate.receipts[name])
        receipt["artifact_set_digest"] = artifact_set
        _replace_check_receipt(candidate, name, receipt)
    return candidate.write_manifest()


def test_verify_rejects_oversized_oci_descriptor_before_json_parsing(
    candidate: CandidateFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor_path = candidate.root / "containers" / "oci-descriptor.json"
    descriptor_path.write_bytes(b"x" * 65)
    parse_called = False
    original_parse = subject._parse_json

    def guarded_parse(raw: bytes, *, label: str, canonical: bool) -> object:
        nonlocal parse_called
        if label == "OCI descriptor":
            parse_called = True
            raise AssertionError("oversized OCI descriptor reached JSON parsing")
        return original_parse(raw, label=label, canonical=canonical)

    monkeypatch.setattr(subject, "MAX_OCI_METADATA_BYTES", 64, raising=False)
    monkeypatch.setattr(subject, "_parse_json", guarded_parse)

    with pytest.raises(ValueError, match="OCI descriptor exceeds its size limit"):
        subject._verify_oci_graph(
            candidate.root, expected_source_sha=candidate.source.commit_sha
        )

    assert parse_called is False


def test_verify_rejects_oversized_oci_config_descriptor_before_opening_blob(
    candidate: CandidateFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata_limit = 4096
    oversized_config_digest = "sha256:" + "f" * 64
    oversized_config_size = metadata_limit + 1
    blob_root = candidate.root / "containers" / "oci-layout" / "blobs" / "sha256"
    oversized_config_path = blob_root / oversized_config_digest.removeprefix("sha256:")

    manifest = copy.deepcopy(candidate.manifests["amd64"])
    manifest["config"] = {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": oversized_config_digest,
        "size": oversized_config_size,
    }
    manifest_raw = _canonical(manifest)
    manifest_descriptor = {
        **_oci_descriptor(
            manifest_raw, "application/vnd.oci.image.manifest.v1+json"
        ),
        "platform": {"architecture": "amd64", "os": "linux"},
    }
    _write(blob_root / hashlib.sha256(manifest_raw).hexdigest(), manifest_raw)

    index = copy.deepcopy(candidate.index)
    index["manifests"][0] = manifest_descriptor  # type: ignore[index]
    index_raw = _canonical(index)
    (candidate.root / "containers" / "oci-layout" / "index.json").write_bytes(
        index_raw
    )

    descriptor = copy.deepcopy(candidate.descriptor)
    descriptor["index_digest"] = _sha256(index_raw)
    descriptor["index_ref"] = f"{OCI_REPOSITORY}@{_sha256(index_raw)}"
    platform = descriptor["platforms"][0]  # type: ignore[index]
    platform["manifest_digest"] = manifest_descriptor["digest"]
    platform["manifest_ref"] = (
        f"{OCI_REPOSITORY}@{manifest_descriptor['digest']}"
    )
    platform["config_digest"] = oversized_config_digest
    _rewrite_descriptor(candidate, descriptor)

    opened = False
    original_open = subject._open_regular

    def guarded_open(path: Path, *, label: str) -> object:
        nonlocal opened
        if path == oversized_config_path:
            opened = True
        return original_open(path, label=label)

    monkeypatch.setattr(
        subject, "MAX_OCI_METADATA_BYTES", metadata_limit, raising=False
    )
    monkeypatch.setattr(subject, "_open_regular", guarded_open)

    with pytest.raises(ValueError, match="OCI config exceeds its size limit"):
        subject._verify_oci_graph(
            candidate.root, expected_source_sha=candidate.source.commit_sha
        )

    assert opened is False


def test_verify_rejects_platform_archive_unrelated_to_verified_oci_graph(
    candidate: CandidateFixture,
) -> None:
    archive = candidate.root / "containers" / "kestrel-linux-amd64.tar"
    archive.write_bytes(b"not an OCI archive")
    descriptor = copy.deepcopy(candidate.descriptor)
    descriptor["platforms"][0]["archive_sha256"] = _sha256(archive.read_bytes())  # type: ignore[index]
    _rewrite_descriptor(candidate, descriptor)
    manifest = candidate.write_manifest()

    with pytest.raises(ValueError, match="platform archive.*OCI graph"):
        subject.verify_candidate_bundle(
            manifest,
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


def _component_budget_platform_archive(
    tmp_path: Path,
) -> tuple[Path, bytes, dict[str, subject.OCIArchiveEntry]]:
    entries = {
        "one/deep/file.txt": b"one",
        "two/deep/file.txt": b"two",
    }
    raw = _oci_platform_archive(entries)
    path = tmp_path / "component-budget-platform.tar"
    path.write_bytes(raw)
    expected_files: dict[str, subject.OCIArchiveEntry] = {
        name: (data, None, _sha256(data), len(data))
        for name, data in entries.items()
    }
    return path, raw, expected_files


def test_oci_platform_archive_accepts_exact_cumulative_path_component_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, raw, expected_files = _component_budget_platform_archive(tmp_path)
    monkeypatch.setattr(
        subject,
        "MAX_ARCHIVE_TOTAL_PATH_COMPONENTS",
        6,
        raising=False,
    )

    subject._verify_oci_platform_archive(
        path,
        expected_archive_digest=_sha256(raw),
        expected_files=expected_files,
    )


def test_oci_platform_archive_rejects_cumulative_path_components_before_trie_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, raw, expected_files = _component_budget_platform_archive(tmp_path)
    original_record = subject._record_portable_path
    recorded = 0

    def counted_record(
        parts: tuple[str, ...],
        identities: subject._PortablePathIndex,
        *,
        label: str,
        entry_kind: str | None = None,
    ) -> None:
        nonlocal recorded
        recorded += 1
        original_record(
            parts,
            identities,
            label=label,
            entry_kind=entry_kind,
        )

    monkeypatch.setattr(subject, "_record_portable_path", counted_record)
    monkeypatch.setattr(
        subject,
        "MAX_ARCHIVE_TOTAL_PATH_COMPONENTS",
        5,
        raising=False,
    )

    with pytest.raises(ValueError, match="cumulative path component limit"):
        subject._verify_oci_platform_archive(
            path,
            expected_archive_digest=_sha256(raw),
            expected_files=expected_files,
        )

    assert recorded == 1


def test_verify_rejects_nonzero_bytes_after_oci_tar_end_marker(
    candidate: CandidateFixture,
) -> None:
    archive = candidate.root / "containers" / "kestrel-linux-amd64.tar"
    replacement = archive.read_bytes() + b"UNRELATED-TRAILING-BYTES"
    archive.write_bytes(replacement)
    descriptor = copy.deepcopy(candidate.descriptor)
    descriptor["platforms"][0]["archive_sha256"] = _sha256(replacement)  # type: ignore[index]
    _rewrite_descriptor(candidate, descriptor)
    manifest = _rebind_candidate_artifacts(candidate)

    with pytest.raises(ValueError, match="trailing bytes"):
        subject.verify_candidate_bundle(
            manifest,
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


@pytest.mark.parametrize("mutation", ["member-padding", "gnu-extension"])
def test_verify_rejects_hidden_bytes_in_oci_tar_structure(
    candidate: CandidateFixture, mutation: str
) -> None:
    archive_path = candidate.root / "containers" / "kestrel-linux-amd64.tar"
    original = archive_path.read_bytes()
    if mutation == "member-padding":
        replacement_bytes = bytearray(original)
        with tarfile.open(fileobj=io.BytesIO(original), mode="r:") as archive:
            member = next(iter(archive))
        padding_start = member.offset_data + member.size
        assert padding_start % tarfile.BLOCKSIZE != 0
        replacement_bytes[padding_start] = 0x41
        replacement = bytes(replacement_bytes)
    elif mutation == "gnu-extension":
        entries: list[tuple[str, bytes]] = []
        with tarfile.open(fileobj=io.BytesIO(original), mode="r:") as archive:
            for member in archive:
                if member.isfile():
                    extracted = archive.extractfile(member)
                    assert extracted is not None
                    entries.append((member.name, extracted.read()))
        output = io.BytesIO()
        with tarfile.open(
            fileobj=output, mode="w:", format=tarfile.GNU_FORMAT
        ) as archive:
            for index, (name, raw) in enumerate(entries):
                if index == 0:
                    hidden = b"UNRELATED-HIDDEN-BYTES\0"
                    extension = tarfile.TarInfo("././@LongLink")
                    extension.type = tarfile.GNUTYPE_LONGLINK
                    extension.size = len(hidden)
                    archive.addfile(extension, io.BytesIO(hidden))
                info = tarfile.TarInfo(name)
                info.mode = 0o644
                info.mtime = 0
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
        replacement = output.getvalue()
    else:  # pragma: no cover
        raise AssertionError(mutation)

    archive_path.write_bytes(replacement)
    descriptor = copy.deepcopy(candidate.descriptor)
    descriptor["platforms"][0]["archive_sha256"] = _sha256(replacement)  # type: ignore[index]
    _rewrite_descriptor(candidate, descriptor)
    manifest = _rebind_candidate_artifacts(candidate)

    with pytest.raises(ValueError, match="structure|padding|hidden"):
        subject.verify_candidate_bundle(
            manifest,
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


def test_verify_rejects_payload_bearing_oci_directory_record(
    candidate: CandidateFixture,
) -> None:
    archive_path = candidate.root / "containers" / "kestrel-linux-amd64.tar"
    entries: list[tuple[str, bytes]] = []
    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive:
            if member.isfile():
                extracted = archive.extractfile(member)
                assert extracted is not None
                entries.append((member.name, extracted.read()))

    def padded(raw: bytes) -> bytes:
        return raw + b"\0" * (-len(raw) % tarfile.BLOCKSIZE)

    chunks: list[bytes] = []
    for name, raw in entries:
        info = tarfile.TarInfo(name)
        info.mode = 0o644
        info.mtime = 0
        info.size = len(raw)
        chunks.extend([info.tobuf(format=tarfile.GNU_FORMAT), padded(raw)])
    hidden = b"HIDDEN-DIRECTORY-PAYLOAD-NOT-IN-OCI-GRAPH"
    directory = tarfile.TarInfo("blobs/")
    directory.type = tarfile.DIRTYPE
    directory.mode = 0o755
    directory.mtime = 0
    directory.size = len(hidden)
    chunks.extend(
        [
            directory.tobuf(format=tarfile.GNU_FORMAT),
            padded(hidden),
            b"\0" * (2 * tarfile.BLOCKSIZE),
        ]
    )
    replacement = b"".join(chunks)
    replacement += b"\0" * (-len(replacement) % tarfile.RECORDSIZE)
    archive_path.write_bytes(replacement)

    descriptor = copy.deepcopy(candidate.descriptor)
    descriptor["platforms"][0]["archive_sha256"] = _sha256(replacement)  # type: ignore[index]
    _rewrite_descriptor(candidate, descriptor)
    manifest = _rebind_candidate_artifacts(candidate)

    with pytest.raises(ValueError, match="directory.*zero-length"):
        subject.verify_candidate_bundle(
            manifest,
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


@pytest.mark.parametrize("mutation", ["layer-drift", "index-drift", "missing", "extra"])
def test_verify_rejects_valid_tar_that_diverges_from_oci_graph(
    candidate: CandidateFixture, mutation: str
) -> None:
    archive = candidate.root / "containers" / "kestrel-linux-amd64.tar"
    entries: dict[str, bytes] = {}
    with tarfile.open(archive, mode="r:") as opened:
        for member in opened:
            if member.isfile():
                extracted = opened.extractfile(member)
                assert extracted is not None
                entries[member.name] = extracted.read()
    if mutation == "layer-drift":
        layer_name = f"blobs/sha256/{hashlib.sha256(candidate.layers['amd64']).hexdigest()}"
        entries[layer_name] = b"different layer bytes"
    elif mutation == "index-drift":
        entries["index.json"] = _canonical(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [],
            }
        )
    elif mutation == "missing":
        entries.pop("oci-layout")
    elif mutation == "extra":
        entries["extra"] = b"surplus"
    else:  # pragma: no cover
        raise AssertionError(mutation)
    replacement = _oci_platform_archive(entries)
    archive.write_bytes(replacement)
    descriptor = copy.deepcopy(candidate.descriptor)
    descriptor["platforms"][0]["archive_sha256"] = _sha256(replacement)  # type: ignore[index]
    _rewrite_descriptor(candidate, descriptor)
    manifest = candidate.write_manifest()

    with pytest.raises(ValueError, match="verified OCI graph"):
        subject.verify_candidate_bundle(
            manifest,
            bundle_root=candidate.root,
            source_root=candidate.source.root,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "index-platform-swap",
        "index-manifest-digest",
        "manifest-config-digest",
        "config-architecture",
        "layer-byte-drift",
        "layer-missing",
        "orphan-blob",
        "layout-marker-missing",
        "layout-marker-invalid",
        "layout-extra",
        "container-root-extra",
        "platform-archive-digest",
        "descriptor-platform-swap",
    ],
)
def test_verify_rejects_oci_graph_mutants(
    candidate: CandidateFixture, mutation: str
) -> None:
    if mutation == "index-platform-swap":
        index = copy.deepcopy(candidate.index)
        index["manifests"][0]["platform"]["architecture"] = "arm64"  # type: ignore[index]
        _rewrite_index(candidate, index)
    elif mutation == "index-manifest-digest":
        index = copy.deepcopy(candidate.index)
        index["manifests"][0]["digest"] = "sha256:" + "f" * 64  # type: ignore[index]
        _rewrite_index(candidate, index)
    elif mutation == "manifest-config-digest":
        descriptor = candidate.descriptor["platforms"][0]  # type: ignore[index]
        manifest_path = (
            candidate.root
            / "containers"
            / "oci-layout"
            / "blobs"
            / "sha256"
            / str(descriptor["manifest_digest"]).removeprefix("sha256:")
        )
        manifest = copy.deepcopy(candidate.manifests["amd64"])
        manifest["config"]["digest"] = "sha256:" + "e" * 64  # type: ignore[index]
        manifest_path.write_bytes(_canonical(manifest))
    elif mutation == "config-architecture":
        descriptor = candidate.descriptor["platforms"][0]  # type: ignore[index]
        config_path = (
            candidate.root
            / "containers"
            / "oci-layout"
            / "blobs"
            / "sha256"
            / str(descriptor["config_digest"]).removeprefix("sha256:")
        )
        config = copy.deepcopy(candidate.configs["amd64"])
        config["architecture"] = "arm64"
        config_path.write_bytes(_canonical(config))
    elif mutation in {"layer-byte-drift", "layer-missing"}:
        layer_path = (
            candidate.root
            / "containers"
            / "oci-layout"
            / "blobs"
            / "sha256"
            / hashlib.sha256(candidate.layers["amd64"]).hexdigest()
        )
        if mutation == "layer-byte-drift":
            layer_path.write_bytes(b"changed")
        else:
            layer_path.unlink()
    elif mutation == "orphan-blob":
        _write(
            candidate.root / "containers" / "oci-layout" / "blobs" / "sha256" / ("0" * 64),
            b"orphan",
        )
    elif mutation == "layout-marker-missing":
        (candidate.root / "containers" / "oci-layout" / "oci-layout").unlink()
    elif mutation == "layout-marker-invalid":
        (candidate.root / "containers" / "oci-layout" / "oci-layout").write_bytes(b"{}")
    elif mutation == "layout-extra":
        _write(candidate.root / "containers" / "oci-layout" / "extra", b"extra")
    elif mutation == "container-root-extra":
        _write(candidate.root / "containers" / "extra", b"extra")
    elif mutation == "platform-archive-digest":
        descriptor = copy.deepcopy(candidate.descriptor)
        descriptor["platforms"][0]["archive_sha256"] = "sha256:" + "d" * 64  # type: ignore[index]
        _rewrite_descriptor(candidate, descriptor)
    elif mutation == "descriptor-platform-swap":
        descriptor = copy.deepcopy(candidate.descriptor)
        descriptor["platforms"][0]["architecture"] = "arm64"  # type: ignore[index]
        _rewrite_descriptor(candidate, descriptor)
    else:  # pragma: no cover
        raise AssertionError(mutation)
    with pytest.raises(ValueError):
        _verify(candidate)


def test_verify_rejects_oci_config_diff_id_not_derived_from_layer(
    candidate: CandidateFixture,
) -> None:
    blob_root = candidate.root / "containers" / "oci-layout" / "blobs" / "sha256"
    descriptor = copy.deepcopy(candidate.descriptor)
    platform = descriptor["platforms"][0]  # type: ignore[index]

    old_config_digest = str(platform["config_digest"])
    config = copy.deepcopy(candidate.configs["amd64"])
    config["rootfs"]["diff_ids"] = ["sha256:" + "f" * 64]  # type: ignore[index]
    config_raw = _canonical(config)
    config_descriptor = _oci_descriptor(
        config_raw, "application/vnd.oci.image.config.v1+json"
    )
    (blob_root / old_config_digest.removeprefix("sha256:")).unlink()
    _write(blob_root / hashlib.sha256(config_raw).hexdigest(), config_raw)

    old_manifest_digest = str(platform["manifest_digest"])
    manifest = copy.deepcopy(candidate.manifests["amd64"])
    manifest["config"] = config_descriptor
    manifest_raw = _canonical(manifest)
    manifest_descriptor = {
        **_oci_descriptor(
            manifest_raw, "application/vnd.oci.image.manifest.v1+json"
        ),
        "platform": {"architecture": "amd64", "os": "linux"},
    }
    (blob_root / old_manifest_digest.removeprefix("sha256:")).unlink()
    _write(blob_root / hashlib.sha256(manifest_raw).hexdigest(), manifest_raw)

    index = copy.deepcopy(candidate.index)
    index["manifests"][0] = manifest_descriptor  # type: ignore[index]
    index_raw = _canonical(index)
    (candidate.root / "containers" / "oci-layout" / "index.json").write_bytes(index_raw)

    platform["config_digest"] = config_descriptor["digest"]
    platform["manifest_digest"] = manifest_descriptor["digest"]
    platform["manifest_ref"] = f"{OCI_REPOSITORY}@{manifest_descriptor['digest']}"
    descriptor["index_digest"] = _sha256(index_raw)
    descriptor["index_ref"] = f"{OCI_REPOSITORY}@{_sha256(index_raw)}"
    _rewrite_descriptor(candidate, descriptor)

    with pytest.raises(ValueError, match="diff ID"):
        subject._verify_oci_graph(
            candidate.root, expected_source_sha=candidate.source.commit_sha
        )


@pytest.mark.parametrize(
    ("media_type", "encode"),
    [
        ("application/vnd.oci.image.layer.v1.tar", lambda raw: raw),
        ("application/vnd.oci.image.layer.v1.tar+gzip", lambda raw: gzip.compress(raw)),
    ],
)
def test_verify_rejects_non_tar_oci_layer_payloads(
    tmp_path: Path, media_type: str, encode: object
) -> None:
    uncompressed = b"this is not a tar archive"
    encoded = encode(uncompressed)  # type: ignore[operator]
    layer = tmp_path / "layer"
    layer.write_bytes(encoded)

    with pytest.raises(ValueError, match="tar archive"):
        subject._verify_layer_blob(
            layer,
            {
                "mediaType": media_type,
                "digest": _sha256(encoded),
                "size": len(encoded),
            },
            expected_diff_id=_sha256(uncompressed),
        )


@pytest.mark.parametrize(
    ("media_type", "encode"),
    [
        ("application/vnd.oci.image.layer.v1.tar", lambda raw: raw),
        ("application/vnd.oci.image.layer.v1.tar+gzip", lambda raw: gzip.compress(raw)),
    ],
)
def test_verify_accepts_structurally_complete_oci_tar_layers(
    tmp_path: Path, media_type: str, encode: object
) -> None:
    uncompressed = _oci_layer_tar(b"valid layer")
    encoded = encode(uncompressed)  # type: ignore[operator]
    layer = tmp_path / "layer"
    layer.write_bytes(encoded)

    assert subject._verify_layer_blob(
        layer,
        {
            "mediaType": media_type,
            "digest": _sha256(encoded),
            "size": len(encoded),
        },
        expected_diff_id=_sha256(uncompressed),
    ) == len(uncompressed)


@pytest.mark.parametrize(
    ("media_type", "encode"),
    [
        ("application/vnd.oci.image.layer.v1.tar", lambda raw: raw),
        ("application/vnd.oci.image.layer.v1.tar+gzip", lambda raw: gzip.compress(raw)),
    ],
)
def test_verify_bounds_oci_layer_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media_type: str,
    encode: object,
) -> None:
    uncompressed = b"X" * 65
    encoded = encode(uncompressed)  # type: ignore[operator]
    layer = tmp_path / "layer"
    layer.write_bytes(encoded)
    monkeypatch.setattr(
        subject, "MAX_OCI_LAYER_UNCOMPRESSED_BYTES", 64, raising=False
    )

    with pytest.raises(ValueError, match="expanded size limit"):
        subject._verify_layer_blob(
            layer,
            {
                "mediaType": media_type,
                "digest": _sha256(encoded),
                "size": len(encoded),
            },
            expected_diff_id=_sha256(uncompressed),
        )


@pytest.mark.parametrize(
    ("limit_name", "limit", "message"),
    [
        ("MAX_OCI_LAYER_COUNT", 1, "too many layers"),
        (
            "MAX_OCI_TOTAL_UNCOMPRESSED_BYTES",
            tarfile.RECORDSIZE * 2 - 1,
            "candidate.*expanded size limit",
        ),
    ],
)
def test_verify_enforces_candidate_wide_oci_layer_budget(
    candidate: CandidateFixture,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
    message: str,
) -> None:
    monkeypatch.setattr(subject, limit_name, limit, raising=False)

    with pytest.raises(ValueError, match=message):
        subject._verify_oci_graph(
            candidate.root, expected_source_sha=candidate.source.commit_sha
        )


@pytest.mark.parametrize(
    "media_type",
    [
        "application/vnd.example.unsupported-layer",
        "application/vnd.oci.image.layer.v1.tar",
    ],
)
def test_verify_validates_media_type_on_every_shared_oci_layer_reference(
    candidate: CandidateFixture,
    media_type: str,
) -> None:
    blob_root = candidate.root / "containers" / "oci-layout" / "blobs" / "sha256"
    descriptor = copy.deepcopy(candidate.descriptor)
    arm64_platform = descriptor["platforms"][1]  # type: ignore[index]

    old_config_digest = str(arm64_platform["config_digest"])
    config = copy.deepcopy(candidate.configs["arm64"])
    config["rootfs"]["diff_ids"] = [  # type: ignore[index]
        _sha256(candidate.uncompressed_layers["amd64"])
    ]
    config_raw = _canonical(config)
    config_descriptor = _oci_descriptor(
        config_raw, "application/vnd.oci.image.config.v1+json"
    )
    (blob_root / old_config_digest.removeprefix("sha256:")).unlink()
    _write(blob_root / hashlib.sha256(config_raw).hexdigest(), config_raw)

    old_manifest_digest = str(arm64_platform["manifest_digest"])
    manifest = copy.deepcopy(candidate.manifests["arm64"])
    manifest["config"] = config_descriptor
    manifest["layers"] = [
        _oci_descriptor(
            candidate.layers["amd64"],
            media_type,
        )
    ]
    manifest_raw = _canonical(manifest)
    manifest_descriptor = {
        **_oci_descriptor(
            manifest_raw, "application/vnd.oci.image.manifest.v1+json"
        ),
        "platform": {"architecture": "arm64", "os": "linux"},
    }
    (blob_root / old_manifest_digest.removeprefix("sha256:")).unlink()
    _write(blob_root / hashlib.sha256(manifest_raw).hexdigest(), manifest_raw)

    old_layer = blob_root / hashlib.sha256(candidate.layers["arm64"]).hexdigest()
    old_layer.unlink()
    index = copy.deepcopy(candidate.index)
    index["manifests"][1] = manifest_descriptor  # type: ignore[index]
    index_raw = _canonical(index)
    (candidate.root / "containers" / "oci-layout" / "index.json").write_bytes(
        index_raw
    )

    arm64_platform["config_digest"] = config_descriptor["digest"]
    arm64_platform["manifest_digest"] = manifest_descriptor["digest"]
    arm64_platform["manifest_ref"] = (
        f"{OCI_REPOSITORY}@{manifest_descriptor['digest']}"
    )
    platform_index = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [manifest_descriptor],
        }
    )
    archive = _oci_platform_archive(
        {
            "oci-layout": _canonical({"imageLayoutVersion": "1.0.0"}),
            "index.json": platform_index,
            f"blobs/sha256/{hashlib.sha256(manifest_raw).hexdigest()}": manifest_raw,
            f"blobs/sha256/{hashlib.sha256(config_raw).hexdigest()}": config_raw,
            (
                "blobs/sha256/"
                + hashlib.sha256(candidate.layers["amd64"]).hexdigest()
            ): candidate.layers["amd64"],
        }
    )
    archive_path = candidate.root / "containers" / "kestrel-linux-arm64.tar"
    archive_path.write_bytes(archive)
    arm64_platform["archive_sha256"] = _sha256(archive)
    descriptor["index_digest"] = _sha256(index_raw)
    descriptor["index_ref"] = f"{OCI_REPOSITORY}@{_sha256(index_raw)}"
    _rewrite_descriptor(candidate, descriptor)

    with pytest.raises(ValueError, match="layer media type"):
        subject._verify_oci_graph(
            candidate.root, expected_source_sha=candidate.source.commit_sha
        )


def test_verify_streams_large_oci_bytes_without_path_read_bytes(
    candidate: CandidateFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = candidate.write_manifest()
    blocked = {
        candidate.root / "containers" / "kestrel-linux-amd64.tar",
        candidate.root / "containers" / "kestrel-linux-arm64.tar",
        *(
            candidate.root
            / "containers"
            / "oci-layout"
            / "blobs"
            / "sha256"
            / hashlib.sha256(layer).hexdigest()
            for layer in candidate.layers.values()
        ),
    }
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path in blocked:
            raise AssertionError(f"bulk read of large candidate byte: {path}")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    subject.verify_candidate_bundle(
        manifest,
        bundle_root=candidate.root,
        source_root=candidate.source.root,
    )


# ---------------------------------------------------------------------------
# GitHub Actions artifact observation
# ---------------------------------------------------------------------------


ARTIFACT_NAME = "kestrel-release-candidate-0.6.0-" + ("a" * 40)
CREATED_AT = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _artifact(
    *,
    artifact_id: int = 5150,
    name: str = ARTIFACT_NAME,
    expires_delta: int = 30 * 86400,
    **overrides: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "id": artifact_id,
        "name": name,
        "size_in_bytes": 4096,
        "expired": False,
        "digest": "sha256:" + "a" * 64,
        "created_at": _timestamp(CREATED_AT),
        "expires_at": _timestamp(CREATED_AT + timedelta(seconds=expires_delta)),
        "workflow_run": {
            "id": WORKFLOW_RUN_ID,
            "repository_id": REPOSITORY_ID,
            "head_repository_id": REPOSITORY_ID,
            "head_branch": "main",
            "head_sha": "a" * 40,
        },
    }
    value.update(overrides)
    return value


def _artifact_pages(artifacts: list[dict[str, object]], *, total_count: int | None = None) -> bytes:
    midpoint = max(1, len(artifacts) // 2)
    pages = [
        {"total_count": len(artifacts) if total_count is None else total_count, "artifacts": artifacts[:midpoint]},
        {"total_count": len(artifacts) if total_count is None else total_count, "artifacts": artifacts[midpoint:]},
    ]
    return _canonical(pages)


def _run_observation(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "id": WORKFLOW_RUN_ID,
        "workflow_id": WORKFLOW_ID,
        "path": WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "run_attempt": 1,
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "success",
        "repository": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
    }
    value.update(overrides)
    return _canonical(value)


@pytest.mark.parametrize("retention_seconds", [30 * 86400, 30 * 86400 - 1])
def test_verify_actions_artifact_accepts_complete_pagination_and_retention_rounding(
    retention_seconds: int,
) -> None:
    observation = _artifact_pages(
        [_artifact(artifact_id=1, name="other"), _artifact(expires_delta=retention_seconds)]
    )
    run = _run_observation()
    receipt = subject.verify_actions_artifact(
        observation,
        run,
        expected_name=ARTIFACT_NAME,
        expected_run_id=WORKFLOW_RUN_ID,
        expected_run_attempt=1,
        expected_source_sha="a" * 40,
        retention_days=30,
    )
    assert receipt["artifact"] == {
        "artifact_id": 5150,
        "name": ARTIFACT_NAME,
        "api_digest": "sha256:" + "a" * 64,
        "size_bytes": 4096,
        "expired": False,
        "created_at": _timestamp(CREATED_AT),
        "expires_at": _timestamp(CREATED_AT + timedelta(seconds=retention_seconds)),
        "run_id": WORKFLOW_RUN_ID,
        "run_attempt": 1,
        "source_sha": "a" * 40,
    }
    schema = json.loads(ARTIFACT_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(receipt)
    assert receipt["evidence"]["source_bundle_digest"] == _source_bundle_digest(  # type: ignore[index]
        {"artifact-observation": observation, "workflow-run-observation": run}
    )


def test_verify_actions_artifact_accepts_branch_qualified_workflow_path() -> None:
    receipt = subject.verify_actions_artifact(
        _artifact_pages([_artifact()]),
        _run_observation(path=WORKFLOW_PATH + "@main"),
        expected_name=ARTIFACT_NAME,
        expected_run_id=WORKFLOW_RUN_ID,
        expected_run_attempt=1,
        expected_source_sha="a" * 40,
        retention_days=30,
    )
    assert receipt["artifact"]["run_id"] == WORKFLOW_RUN_ID  # type: ignore[index]


def test_verify_actions_artifact_accepts_exact_recovery_staging_transport() -> None:
    artifact = _artifact(
        name="kestrel-recovery-dependencies-" + "a" * 40,
    )
    direct = _canonical(artifact)
    receipt = subject.verify_actions_artifact(
        _artifact_pages([artifact]),
        _run_observation(
            path=".github/workflows/recovery-dependency-staging.yml@main"
        ),
        expected_name="kestrel-recovery-dependencies-" + "a" * 40,
        expected_run_id=WORKFLOW_RUN_ID,
        expected_run_attempt=1,
        expected_source_sha="a" * 40,
        retention_days=30,
        expected_workflow_path=(
            ".github/workflows/recovery-dependency-staging.yml"
        ),
        expected_artifact_id=5150,
        expected_api_digest="sha256:" + "a" * 64,
        direct_artifact_observation=direct,
    )

    assert receipt["artifact"]["artifact_id"] == 5150  # type: ignore[index]
    assert receipt["artifact"]["api_digest"] == "sha256:" + "a" * 64  # type: ignore[index]
    assert receipt["evidence"]["source_bundle_digest"] == _source_bundle_digest(  # type: ignore[index]
        {
            "artifact-observation": _artifact_pages([artifact]),
            "direct-artifact-observation": direct,
            "workflow-run-observation": _run_observation(
                path=".github/workflows/recovery-dependency-staging.yml@main"
            ),
        }
    )


@pytest.mark.parametrize("status", ["in_progress", "waiting"])
def test_verify_actions_artifact_accepts_active_authorization_transport(
    status: str,
) -> None:
    receipt = subject.verify_actions_artifact(
        _artifact_pages([_artifact()]),
        _run_observation(
            path=".github/workflows/release.yml",
            status=status,
            conclusion=None,
        ),
        expected_name=ARTIFACT_NAME,
        expected_run_id=WORKFLOW_RUN_ID,
        expected_run_attempt=1,
        expected_source_sha="a" * 40,
        retention_days=30,
        expected_workflow_path=".github/workflows/release.yml",
        require_completed_success=False,
        expected_artifact_id=5150,
        expected_api_digest="sha256:" + "a" * 64,
        direct_artifact_observation=_canonical(_artifact()),
    )

    assert receipt["artifact"]["run_id"] == WORKFLOW_RUN_ID  # type: ignore[index]


@pytest.mark.parametrize(
    ("expected_id", "expected_digest", "direct_mutation"),
    [
        (5151, "sha256:" + "a" * 64, None),
        (5150, "sha256:" + "b" * 64, None),
        (5150, "sha256:" + "a" * 64, {"size_in_bytes": 4097}),
    ],
)
def test_verify_actions_artifact_rejects_controller_transport_substitution(
    expected_id: int,
    expected_digest: str,
    direct_mutation: dict[str, object] | None,
) -> None:
    direct = _artifact()
    if direct_mutation is not None:
        direct.update(direct_mutation)

    with pytest.raises(ValueError, match="artifact"):
        subject.verify_actions_artifact(
            _artifact_pages([_artifact()]),
            _run_observation(
                path=".github/workflows/recovery-dependency-staging.yml"
            ),
            expected_name=ARTIFACT_NAME,
            expected_run_id=WORKFLOW_RUN_ID,
            expected_run_attempt=1,
            expected_source_sha="a" * 40,
            retention_days=30,
            expected_workflow_path=(
                ".github/workflows/recovery-dependency-staging.yml"
            ),
            expected_artifact_id=expected_id,
            expected_api_digest=expected_digest,
            direct_artifact_observation=_canonical(direct),
        )


@pytest.mark.parametrize(
    ("observation", "run"),
    [
        (_artifact_pages([_artifact()], total_count=2), _run_observation()),
        (
            _canonical(
                [
                    {"total_count": 1, "artifacts": [_artifact()]},
                    {"total_count": 2, "artifacts": []},
                ]
            ),
            _run_observation(),
        ),
        (_artifact_pages([_artifact(artifact_id=1), _artifact(artifact_id=1)]), _run_observation()),
        (_artifact_pages([_artifact(name="other")]), _run_observation()),
        (_artifact_pages([_artifact(), _artifact(artifact_id=2)]), _run_observation()),
        (_artifact_pages([_artifact()]), _run_observation(run_attempt=2)),
        (_artifact_pages([_artifact()]), _run_observation(id=999)),
        (_artifact_pages([_artifact()]), _run_observation(head_sha="f" * 40)),
        (_artifact_pages([_artifact()]), _run_observation(status="in_progress")),
        (_artifact_pages([_artifact()]), _run_observation(conclusion="failure")),
        (_artifact_pages([_artifact()]), _run_observation(path=".github/workflows/other.yml@main")),
        (_artifact_pages([_artifact()]), _run_observation(event="push")),
        (_artifact_pages([_artifact()]), _run_observation(head_branch="feature")),
        (_artifact_pages([_artifact()]), _run_observation(workflow_id=True)),
        (
            _artifact_pages([_artifact()]),
            _run_observation(
                repository={"id": REPOSITORY_ID, "full_name": "attacker/Kestrel"}
            ),
        ),
    ],
)
def test_verify_actions_artifact_rejects_incomplete_or_wrong_identity(
    observation: bytes, run: bytes
) -> None:
    with pytest.raises(ValueError):
        subject.verify_actions_artifact(
            observation,
            run,
            expected_name=ARTIFACT_NAME,
            expected_run_id=WORKFLOW_RUN_ID,
            expected_run_attempt=1,
            expected_source_sha="a" * 40,
            retention_days=30,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"expired": True},
        {"digest": "sha256:" + "A" * 64},
        {"size_in_bytes": 0},
        {"workflow_run": {"id": 999, "head_sha": "a" * 40}},
        {"workflow_run": {"id": WORKFLOW_RUN_ID, "head_sha": "f" * 40}},
    ],
)
def test_verify_actions_artifact_rejects_bad_artifact_fields(
    overrides: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        subject.verify_actions_artifact(
            _artifact_pages([_artifact(**overrides)]),
            _run_observation(),
            expected_name=ARTIFACT_NAME,
            expected_run_id=WORKFLOW_RUN_ID,
            expected_run_attempt=1,
            expected_source_sha="a" * 40,
            retention_days=30,
        )


@pytest.mark.parametrize(
    "workflow_run",
    [
        {
            "id": WORKFLOW_RUN_ID,
            "repository_id": REPOSITORY_ID + 1,
            "head_repository_id": REPOSITORY_ID,
            "head_branch": "main",
            "head_sha": "a" * 40,
        },
        {
            "id": WORKFLOW_RUN_ID,
            "repository_id": REPOSITORY_ID,
            "head_repository_id": REPOSITORY_ID + 1,
            "head_branch": "main",
            "head_sha": "a" * 40,
        },
        {
            "id": WORKFLOW_RUN_ID,
            "repository_id": REPOSITORY_ID,
            "head_repository_id": REPOSITORY_ID,
            "head_branch": "feature/release",
            "head_sha": "a" * 40,
        },
    ],
)
def test_verify_actions_artifact_joins_nested_repository_and_branch_identity(
    workflow_run: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="artifact workflow run"):
        subject.verify_actions_artifact(
            _artifact_pages([_artifact(workflow_run=workflow_run)]),
            _run_observation(),
            expected_name=ARTIFACT_NAME,
            expected_run_id=WORKFLOW_RUN_ID,
            expected_run_attempt=1,
            expected_source_sha="a" * 40,
            retention_days=30,
        )


@pytest.mark.parametrize("delta", [30 * 86400 + 1, 30 * 86400 - 2])
def test_verify_actions_artifact_rejects_retention_outside_one_second(delta: int) -> None:
    with pytest.raises(ValueError):
        subject.verify_actions_artifact(
            _artifact_pages([_artifact(expires_delta=delta)]),
            _run_observation(),
            expected_name=ARTIFACT_NAME,
            expected_run_id=WORKFLOW_RUN_ID,
            expected_run_attempt=1,
            expected_source_sha="a" * 40,
            retention_days=30,
        )


@pytest.mark.parametrize("retention_days", [1, 31])
def test_verify_actions_artifact_requires_fixed_thirty_day_retention(
    retention_days: int,
) -> None:
    with pytest.raises(ValueError, match="30 days"):
        subject.verify_actions_artifact(
            _artifact_pages(
                [_artifact(expires_delta=retention_days * 86400)]
            ),
            _run_observation(),
            expected_name=ARTIFACT_NAME,
            expected_run_id=WORKFLOW_RUN_ID,
            expected_run_attempt=1,
            expected_source_sha="a" * 40,
            retention_days=retention_days,
        )


@pytest.mark.parametrize(
    ("expected_attempt", "retention_days", "run_attempt"),
    [(True, 30, 1), (1, True, 1), (1, 30, True)],
)
def test_verify_actions_artifact_rejects_boolean_integer_substitution(
    expected_attempt: int,
    retention_days: int,
    run_attempt: int,
) -> None:
    with pytest.raises(ValueError):
        subject.verify_actions_artifact(
            _artifact_pages([_artifact()]),
            _run_observation(run_attempt=run_attempt),
            expected_name=ARTIFACT_NAME,
            expected_run_id=WORKFLOW_RUN_ID,
            expected_run_attempt=expected_attempt,
            expected_source_sha="a" * 40,
            retention_days=retention_days,
        )


def test_verify_actions_artifact_cli_is_idempotent_only_for_exact_bytes(
    tmp_path: Path,
) -> None:
    observation = tmp_path / "artifacts.json"
    observation.write_bytes(_artifact_pages([_artifact()]))
    run = tmp_path / "run.json"
    run.write_bytes(_run_observation())
    output = tmp_path / "receipt.json"
    args = (
        "verify-actions-artifact",
        str(observation),
        "--run-observation",
        str(run),
        "--expected-name",
        ARTIFACT_NAME,
        "--expected-run-id",
        str(WORKFLOW_RUN_ID),
        "--expected-run-attempt",
        "1",
        "--expected-source-sha",
        "a" * 40,
        "--retention-days",
        "30",
        "--output",
        str(output),
    )
    assert _run_cli(*args).returncode == 0
    before = output.read_bytes()
    assert _run_cli(*args).returncode == 0
    assert output.read_bytes() == before
    output.write_bytes(b"{}")
    assert _run_cli(*args).returncode != 0
    assert output.read_bytes() == b"{}"


@pytest.mark.parametrize("oversized_input", ["artifact", "run"])
def test_verify_actions_artifact_cli_bounds_observations_before_json_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oversized_input: str,
) -> None:
    observation = tmp_path / "artifacts.json"
    observation.write_bytes(
        b"x" * 65 if oversized_input == "artifact" else _artifact_pages([_artifact()])
    )
    run = tmp_path / "run.json"
    run.write_bytes(b"x" * 65 if oversized_input == "run" else _run_observation())
    output = tmp_path / "receipt.json"
    args = subject._parser().parse_args(
        [
            "verify-actions-artifact",
            str(observation),
            "--run-observation",
            str(run),
            "--expected-name",
            ARTIFACT_NAME,
            "--expected-run-id",
            str(WORKFLOW_RUN_ID),
            "--expected-run-attempt",
            "1",
            "--expected-source-sha",
            "a" * 40,
            "--retention-days",
            "30",
            "--output",
            str(output),
        ]
    )
    parse_called = False
    original_parse = subject._parse_json

    def guarded_parse(raw: bytes, *, label: str, canonical: bool) -> object:
        nonlocal parse_called
        if label in {"artifact observation", "workflow run observation"}:
            parse_called = True
        return original_parse(raw, label=label, canonical=canonical)

    monkeypatch.setattr(
        subject, "MAX_ACTIONS_OBSERVATION_BYTES", 64, raising=False
    )
    monkeypatch.setattr(subject, "_parse_json", guarded_parse)

    with pytest.raises(ValueError, match="observation exceeds its size limit"):
        args.handler(args)

    assert parse_called is False
    assert not output.exists()


# ---------------------------------------------------------------------------
# Digest-checked, traversal-safe Actions artifact extraction
# ---------------------------------------------------------------------------


def _zip(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in entries:
            archive.writestr(name, raw)
    return output.getvalue()


def _extract(tmp_path: Path, archive: bytes, *, output: Path | None = None) -> subprocess.CompletedProcess[str]:
    archive_path = tmp_path / "candidate.zip"
    archive_path.write_bytes(archive)
    return _run_cli(
        "extract-actions-artifact",
        str(archive_path),
        "--expected-digest",
        _sha256(archive),
        "--output",
        str(output or (tmp_path / "extracted")),
    )


def test_extract_actions_artifact_happy_path(tmp_path: Path) -> None:
    archive = _zip(
        [
            ("candidate-manifest.json", b"{}"),
            ("source.tar", b"source"),
            ("release/file.whl", b"wheel"),
        ]
    )
    result = _extract(tmp_path, archive)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "extracted" / "release" / "file.whl").read_bytes() == b"wheel"


def test_extract_accepts_gap_free_standard_data_descriptor(tmp_path: Path) -> None:
    raw = bytearray(_zip([("safe.txt", b"safe")]))
    central_offset = raw.index(b"PK\x01\x02")
    crc, compressed_size, file_size = struct.unpack_from("<3I", raw, 14)
    descriptor = struct.pack(
        "<4s3I", b"PK\x07\x08", crc, compressed_size, file_size
    )
    raw[central_offset:central_offset] = descriptor
    struct.pack_into("<H", raw, 6, struct.unpack_from("<H", raw, 6)[0] | 0x08)
    struct.pack_into("<3I", raw, 14, 0, 0, 0)
    shifted_central = central_offset + len(descriptor)
    struct.pack_into(
        "<H", raw, shifted_central + 8, struct.unpack_from("<H", raw, shifted_central + 8)[0] | 0x08
    )
    end_offset = raw.index(b"PK\x05\x06", shifted_central)
    struct.pack_into("<I", raw, end_offset + 16, shifted_central)

    result = _extract(tmp_path, bytes(raw))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "extracted" / "safe.txt").read_bytes() == b"safe"


def _zip_with_unknown_extra(location: str) -> bytes:
    raw = bytearray(_zip([("safe.txt", b"safe")]))
    field = struct.pack("<HH", 0xCAFE, 6) + b"SECRET"
    central_offset = raw.index(b"PK\x01\x02")
    local_name_length, local_extra_length = struct.unpack_from("<2H", raw, 26)
    if location in {"local", "mirrored"}:
        local_insert = 30 + local_name_length + local_extra_length
        raw[local_insert:local_insert] = field
        struct.pack_into("<H", raw, 28, local_extra_length + len(field))
        central_offset += len(field)

    central_growth = 0
    if location in {"central", "mirrored"}:
        central_name_length, central_extra_length = struct.unpack_from(
            "<2H", raw, central_offset + 28
        )
        central_insert = (
            central_offset + 46 + central_name_length + central_extra_length
        )
        raw[central_insert:central_insert] = field
        struct.pack_into(
            "<H", raw, central_offset + 30, central_extra_length + len(field)
        )
        central_growth = len(field)

    end_offset = raw.rindex(b"PK\x05\x06")
    central_size = struct.unpack_from("<I", raw, end_offset + 12)[0]
    struct.pack_into("<I", raw, end_offset + 12, central_size + central_growth)
    struct.pack_into("<I", raw, end_offset + 16, central_offset)
    return bytes(raw)


@pytest.mark.parametrize("location", ["local", "central", "mirrored"])
def test_extract_rejects_unapproved_or_mismatched_zip_extra_fields(
    tmp_path: Path, location: str
) -> None:
    result = _extract(tmp_path, _zip_with_unknown_extra(location))

    assert result.returncode != 0
    assert "extra field" in result.stderr
    assert not (tmp_path / "extracted").exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("member-count", "too many members"),
        ("forged-low-member-count", "too many members"),
        ("central-size", "central directory exceeds"),
    ],
)
def test_extract_preflights_zip_limits_before_zipfile_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    entries = [("safe.txt", b"safe")]
    if mutation == "forged-low-member-count":
        monkeypatch.setattr(subject, "MAX_ARCHIVE_MEMBERS", 10)
        entries = [(f"f{index:02d}", b"") for index in range(11)]
    raw = bytearray(_zip(entries))
    end_offset = raw.rindex(b"PK\x05\x06")
    if mutation == "member-count":
        struct.pack_into("<2H", raw, end_offset + 8, 10_001, 10_001)
    elif mutation == "forged-low-member-count":
        struct.pack_into("<2H", raw, end_offset + 8, 1, 1)
    else:
        monkeypatch.setattr(
            subject, "MAX_ZIP_CENTRAL_DIRECTORY_BYTES", 1, raising=False
        )
    archive_path = tmp_path / "candidate.zip"
    archive_path.write_bytes(raw)
    zipfile_called = False

    def forbidden_zipfile(*_args: object, **_kwargs: object) -> object:
        nonlocal zipfile_called
        zipfile_called = True
        raise AssertionError("ZipFile materialized records before bounded preflight")

    monkeypatch.setattr(subject.zipfile, "ZipFile", forbidden_zipfile)

    with pytest.raises(ValueError, match=message):
        subject.extract_actions_artifact(
            archive_path,
            expected_digest=_sha256(bytes(raw)),
            output=tmp_path / "preflight-output",
        )

    assert zipfile_called is False


def test_extract_streams_archive_without_path_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip([("safe.txt", b"safe")])
    archive_path = tmp_path / "candidate.zip"
    archive_path.write_bytes(archive)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == archive_path:
            raise AssertionError("archive must be streamed, not bulk-read")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    subject.extract_actions_artifact(
        archive_path,
        expected_digest=_sha256(archive),
        output=tmp_path / "streamed",
    )
    assert (tmp_path / "streamed" / "safe.txt").read_bytes() == b"safe"


def test_extract_uses_digest_checked_snapshot_when_source_path_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe = _zip([("safe.txt", b"safe")])
    replacement = _zip([("replacement.txt", b"replacement")])
    archive_path = tmp_path / "candidate.zip"
    archive_path.write_bytes(safe)
    original_zip_file = zipfile.ZipFile

    def swapping_zip_file(
        file: object, *args: object, **kwargs: object
    ) -> zipfile.ZipFile:
        if file == archive_path:
            archive_path.write_bytes(replacement)
        return original_zip_file(file, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(zipfile, "ZipFile", swapping_zip_file)
    subject.extract_actions_artifact(
        archive_path,
        expected_digest=_sha256(safe),
        output=tmp_path / "snapshot",
    )
    assert (tmp_path / "snapshot" / "safe.txt").read_bytes() == b"safe"
    assert not (tmp_path / "snapshot" / "replacement.txt").exists()


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("a" * (MAX_ARCHIVE_PATH_BYTES + 1), "encoded path length"),
        (
            "/".join(["a"] * (MAX_ARCHIVE_PATH_COMPONENTS + 1)),
            "path component count",
        ),
    ],
)
def test_zip_path_rejects_resource_exhaustion_before_prefix_tracking(
    name: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        subject._zip_path(zipfile.ZipInfo(name))


def test_portable_path_index_stores_one_node_per_component() -> None:
    index = subject._PortablePathIndex()
    parts = tuple("a" for _ in range(MAX_ARCHIVE_PATH_COMPONENTS))

    subject._record_portable_path(parts, index, label="artifact archive")

    node = index
    node_count = 0
    for _part in parts:
        assert list(node.children) == ["a"]
        _actual, node = node.children["a"]
        node_count += 1
    assert node_count == MAX_ARCHIVE_PATH_COMPONENTS
    assert node.children == {}

    with pytest.raises(ValueError, match="portable path collision"):
        subject._record_portable_path(
            (*parts[:-1], "A"), index, label="artifact archive"
        )


class _InMemoryZipMetadata:
    def __init__(self, members: list[zipfile.ZipInfo]) -> None:
        self._members = members

    def infolist(self) -> list[zipfile.ZipInfo]:
        return self._members


def _regular_zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def test_validate_zip_accepts_exact_cumulative_path_component_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = [
        _regular_zip_info("one/deep/file.txt"),
        _regular_zip_info("two/deep/file.txt"),
    ]
    monkeypatch.setattr(subject, "_validate_zip_physical_layout", lambda *_args: None)
    monkeypatch.setattr(
        subject,
        "MAX_ARCHIVE_TOTAL_PATH_COMPONENTS",
        6,
        raising=False,
    )

    validated = subject._validate_zip(_InMemoryZipMetadata(members))  # type: ignore[arg-type]

    assert ["/".join(parts) for _info, parts in validated] == [
        "one/deep/file.txt",
        "two/deep/file.txt",
    ]


def test_validate_zip_rejects_cumulative_path_components_before_trie_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = [
        _regular_zip_info("one/deep/file.txt"),
        _regular_zip_info("two/deep/file.txt"),
    ]
    original_record = subject._record_portable_path
    recorded = 0

    def counted_record(
        parts: tuple[str, ...],
        identities: subject._PortablePathIndex,
        *,
        label: str,
        entry_kind: str | None = None,
    ) -> None:
        nonlocal recorded
        recorded += 1
        original_record(
            parts,
            identities,
            label=label,
            entry_kind=entry_kind,
        )

    monkeypatch.setattr(subject, "_validate_zip_physical_layout", lambda *_args: None)
    monkeypatch.setattr(subject, "_record_portable_path", counted_record)
    monkeypatch.setattr(
        subject,
        "MAX_ARCHIVE_TOTAL_PATH_COMPONENTS",
        5,
        raising=False,
    )

    with pytest.raises(ValueError, match="cumulative path component limit"):
        subject._validate_zip(_InMemoryZipMetadata(members))  # type: ignore[arg-type]

    assert recorded == 1


def test_validate_zip_collision_tracking_does_not_materialize_prefix_tuples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = [
        _regular_zip_info("root/one/deep/file.txt"),
        _regular_zip_info("root/two/deep/file.txt"),
    ]
    original_zip_path = subject._zip_path
    monkeypatch.setattr(subject, "_validate_zip_physical_layout", lambda *_args: None)

    def guarded_zip_path(info: zipfile.ZipInfo) -> tuple[str, ...]:
        return _PrefixSliceGuard(original_zip_path(info))

    monkeypatch.setattr(subject, "_zip_path", guarded_zip_path)

    validated = subject._validate_zip(_InMemoryZipMetadata(members))  # type: ignore[arg-type]

    assert [info.filename for info, _parts in validated] == [
        "root/one/deep/file.txt",
        "root/two/deep/file.txt",
    ]


@pytest.mark.parametrize(
    "names",
    [("path", "path/child"), ("path/child", "path")],
)
def test_validate_zip_trie_rejects_file_ancestor_collisions(
    monkeypatch: pytest.MonkeyPatch, names: tuple[str, str]
) -> None:
    monkeypatch.setattr(subject, "_validate_zip_physical_layout", lambda *_args: None)
    archive = _InMemoryZipMetadata([_regular_zip_info(name) for name in names])

    with pytest.raises(ValueError, match="file/directory path collision"):
        subject._validate_zip(archive)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "a/../../escape",
        "/absolute",
        "C:/drive",
        "a\\backslash",
        "a/./b",
        "dir/file:stream",
        "CON",
        "CONIN$",
        "CONOUT$",
        "CLOCK$",
        "COM¹.txt",
        "LPT³.txt",
        "NUL .txt",
        "aux.txt",
        "dir/trailing.",
        "dir/trailing ",
    ],
)
def test_extract_rejects_unsafe_paths_without_partial_output(tmp_path: Path, name: str) -> None:
    result = _extract(tmp_path, _zip([(name, b"bad"), ("safe.txt", b"safe")]))
    assert result.returncode != 0
    assert not (tmp_path / "extracted").exists()


def test_extract_rejects_duplicate_and_file_directory_collisions(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate = _zip([("a.txt", b"one"), ("a.txt", b"two")])
    assert _extract(tmp_path, duplicate).returncode != 0
    collision = _zip([("a", b"file"), ("a/b", b"child")])
    assert _extract(tmp_path, collision).returncode != 0


def test_extract_rejects_cross_platform_directory_collisions(tmp_path: Path) -> None:
    archive = _zip([("Case/one.txt", b"one"), ("case/two.txt", b"two")])
    assert _extract(tmp_path, archive).returncode != 0


def test_extract_rejects_symlink_and_special_members(tmp_path: Path) -> None:
    symlink = zipfile.ZipInfo("link")
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    assert _extract(tmp_path, _zip([(symlink, b"target")])).returncode != 0
    fifo = zipfile.ZipInfo("fifo")
    fifo.external_attr = (stat.S_IFIFO | 0o644) << 16
    assert _extract(tmp_path, _zip([(fifo, b"")])).returncode != 0


def test_extract_rejects_payload_bearing_directory_member(tmp_path: Path) -> None:
    directory = zipfile.ZipInfo("hidden/")
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    archive = _zip([(directory, b"HIDDEN-DIRECTORY-PAYLOAD"), ("safe", b"safe")])

    result = _extract(tmp_path, archive)

    assert result.returncode != 0
    assert "directory" in result.stderr
    assert not (tmp_path / "extracted").exists()


def test_extract_rejects_compressed_bytes_hidden_in_zero_length_directory(
    tmp_path: Path,
) -> None:
    directory = zipfile.ZipInfo("hidden/")
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    directory.compress_type = zipfile.ZIP_DEFLATED
    raw = bytearray(_zip([(directory, b"")]))
    old_central_offset = raw.index(b"PK\x01\x02")
    hidden = b"SECRET"
    raw[old_central_offset:old_central_offset] = hidden
    original_compressed_size = struct.unpack_from("<I", raw, 18)[0]
    struct.pack_into("<I", raw, 18, original_compressed_size + len(hidden))
    central_offset = old_central_offset + len(hidden)
    struct.pack_into(
        "<I", raw, central_offset + 20, original_compressed_size + len(hidden)
    )
    end_offset = raw.index(b"PK\x05\x06", central_offset)
    struct.pack_into("<I", raw, end_offset + 16, central_offset)

    result = _extract(tmp_path, bytes(raw))

    assert result.returncode != 0
    assert "directory" in result.stderr
    assert not (tmp_path / "extracted").exists()


@pytest.mark.parametrize(
    "mutation", ["local-size-drift", "unclaimed-gap", "trailing-bytes"]
)
def test_extract_rejects_hidden_bytes_outside_central_directory_claims(
    tmp_path: Path, mutation: str
) -> None:
    directory = zipfile.ZipInfo("hidden/")
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    raw = bytearray(_zip([(directory, b"")]))
    hidden = b"SECRET"
    if mutation == "trailing-bytes":
        raw.extend(hidden)
    else:
        old_central_offset = raw.index(b"PK\x01\x02")
        raw[old_central_offset:old_central_offset] = hidden
        if mutation == "local-size-drift":
            struct.pack_into("<I", raw, 18, len(hidden))
        central_offset = old_central_offset + len(hidden)
        end_offset = raw.index(b"PK\x05\x06", central_offset)
        struct.pack_into("<I", raw, end_offset + 16, central_offset)

    result = _extract(tmp_path, bytes(raw))

    assert result.returncode != 0
    assert "ZIP" in result.stderr or "zip" in result.stderr
    assert not (tmp_path / "extracted").exists()


def test_extract_rejects_digest_mismatch_and_nonempty_or_symlink_output(
    tmp_path: Path,
) -> None:
    archive = _zip([("safe.txt", b"safe")])
    archive_path = tmp_path / "candidate.zip"
    archive_path.write_bytes(archive)
    bad_digest = _run_cli(
        "extract-actions-artifact",
        str(archive_path),
        "--expected-digest",
        "sha256:" + "0" * 64,
        "--output",
        str(tmp_path / "bad-digest"),
    )
    assert bad_digest.returncode != 0
    output = tmp_path / "existing"
    output.mkdir()
    _write(output / "stale", b"stale")
    assert _extract(tmp_path, archive, output=output).returncode != 0
    assert (output / "stale").read_bytes() == b"stale"
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "output-link"
    link.symlink_to(outside, target_is_directory=True)
    assert _extract(tmp_path, archive, output=link).returncode != 0
    assert not list(outside.iterdir())


def test_extract_accepts_existing_empty_directory_atomically(tmp_path: Path) -> None:
    output = tmp_path / "existing-empty"
    output.mkdir()
    result = _extract(tmp_path, _zip([("safe.txt", b"safe")]), output=output)
    assert result.returncode == 0, result.stderr
    assert (output / "safe.txt").read_bytes() == b"safe"
