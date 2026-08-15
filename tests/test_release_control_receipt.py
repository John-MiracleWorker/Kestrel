"""S2 Task 2 contracts for canonical release-control receipts."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import inspect
import io
import json
import struct
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_control_receipt.py"
SOURCE_REGISTRY = ROOT / "release-control-source-registry.json"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "release-control" / "v3"
CANONICALIZATION_FIXTURE = FIXTURE_ROOT / "canonicalization-vectors.json"
CANONICALIZATION_DIGEST = "sha256:7d37d1815caf0bb822d1244edcde67b2872cac94c9a6238d036198c55d196054"

sys.path.insert(0, str(ROOT))
from scripts import release_control_receipt as subject  # noqa: E402


@pytest.fixture(autouse=True)
def _independent_owner_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subject,
        "_fetch_owner_signing_keys_from_github",
        lambda principal: [
            {
                "id": 404,
                "key": KNOWN_PUBLIC_KEY.decode("ascii"),
                "title": "Kestrel release controller",
            }
        ],
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _positive_contract_vector(name: str) -> tuple[bytes, bytes | None]:
    bundle = json.loads((FIXTURE_ROOT / "positive-contract-vectors.json").read_bytes())
    matches = [item for item in bundle["vectors"] if item["name"] == name]
    assert len(matches) == 1
    item = matches[0]
    signature = item["signature_base64"]
    return _canonical(item["record"]), (None if signature is None else base64.b64decode(signature))


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _trust_test_gh_binary(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subject,
        "PINNED_GH_BINARY_DIGESTS",
        {(sys.platform, subject.platform.machine()): _sha256(path.read_bytes())},
    )


def _registry_entry(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "receipt_schema": "kestrel.github_release_authority.v3",
        "phase": "admission",
        "mode": "initiate",
        "name": "repository-rest",
        "provider": "github.com",
        "locator": "GET /repos/John-MiracleWorker/Kestrel",
        "authentication_mode": "github-owner",
        "body_mode": "singleton-json",
        "count_mode": "one",
        "freshness_class": "current",
    }
    value.update(overrides)
    return value


def _registry(*entries: dict[str, object]) -> dict[str, object]:
    return {
        "schema": "kestrel.source_registry.v1",
        "entries": sorted(
            entries,
            key=lambda item: (
                str(item["receipt_schema"]),
                "" if item["phase"] is None else str(item["phase"]),
                "" if item["mode"] is None else str(item["mode"]),
                str(item["name"]),
            ),
        ),
    }


def _source(
    name: str,
    captured_at: str,
    *,
    freshness_class: str = "current",
    body: bytes = b"{}",
) -> dict[str, object]:
    return {
        "schema": "kestrel.source_observation.v1",
        "name": name,
        "provider": "github.com",
        "locator": f"GET /{name}",
        "authenticated_as": "John-MiracleWorker",
        "freshness_class": freshness_class,
        "captured_at": captured_at,
        "page_count": 1,
        "record_count": 1,
        "complete": True,
        "body_encoding": "base64",
        "body": base64.b64encode(body).decode("ascii"),
    }


def _committed_source(
    *,
    receipt_schema: str,
    phase: str | None,
    mode: str | None,
    name: str,
    body: object,
    now: datetime,
) -> bytes:
    registry = json.loads(SOURCE_REGISTRY.read_bytes())
    entry = next(
        item
        for item in registry["entries"]
        if item["receipt_schema"] == receipt_schema
        and item["phase"] == phase
        and item["mode"] == mode
        and item["name"] == name
    )
    identity = (
        _canonical({"login": "John-MiracleWorker"})
        if entry["authentication_mode"] in {"github-owner", "controller-owner"}
        else None
    )
    envelope = subject.capture_source(
        registry=registry,
        receipt_schema=receipt_schema,
        phase=phase,
        mode=mode,
        name=name,
        raw_input=_canonical(body),
        identity_observation=identity,
        _clock=lambda: now,
    )
    return _canonical(envelope)


def _authority_candidate_manifest(*, repository_id: int = 303) -> dict[str, object]:
    artifact = {
        "path": "release/nested_memvid_agent-1.2.3-py3-none-any.whl",
        "media_type": "application/zip",
        "sha256": "sha256:" + "d" * 64,
        "size_bytes": 1,
    }
    artifacts = [artifact]
    check_names = [
        "nine-row-exact-wheel",
        "oci-layout",
        "protected-main-ci",
        "release-payload",
        "release-rehearsal",
        "runtime-reliability-qualification",
    ]
    return {
        "schema": "kestrel.release_candidate.v1",
        "version": "1.2.3",
        "tag": "v1.2.3",
        "source": {
            "repository": "John-MiracleWorker/Kestrel",
            "repository_id": repository_id,
            "commit_sha": "a" * 40,
            "tree_sha": "b" * 40,
            "archive_sha256": "sha256:" + "c" * 64,
            "size_bytes": 1,
        },
        "candidate_run": {
            "workflow_id": 404,
            "workflow_ref": "refs/heads/main",
            "workflow_sha": "a" * 40,
            "run_id": 101,
            "run_attempt": 1,
        },
        "checks": [
            {
                "name": name,
                "status": "success",
                "subject_sha": "a" * 40,
                "run_id": 101,
                "run_attempt": 1,
                "receipt_path": f"qualification/receipts/{name}.json",
                "receipt_sha256": "sha256:" + str(index) * 64,
            }
            for index, name in enumerate(check_names, start=1)
        ],
        "attestation_subjects": [
            {
                "kind": "file",
                "name": artifact["path"],
                "digest": artifact["sha256"],
            },
            {
                "kind": "oci_index",
                "name": "ghcr.io/john-miracleworker/kestrel",
                "digest": "sha256:" + "e" * 64,
            },
        ],
        "artifacts": artifacts,
        "artifact_set_digest": _sha256(_canonical(artifacts)),
        "planned_surfaces": ["ghcr", "github_release", "github_tag", "pypi"],
        "evidence": {
            "source_bundle_digest": "sha256:" + "f" * 64,
            "canonicalization_vector_digest": CANONICALIZATION_DIGEST,
        },
        "provenance": {
            "producer": "scripts/release_candidate_manifest.py",
            "provider": "github.com",
            "method": "candidate-run-finalization",
        },
        "confidence": 1,
        "validation_status": "validated",
    }


def test_canonicalization_fixture_is_exact_known_answer() -> None:
    expected = (
        b'{"schema":"kestrel.canonicalization_vectors.v1","vectors":['
        b'{"canonical_hex":"7b2261223a312c2262223a22c3a9227d",'
        b'"input_hex":"7b2262223a22c3a9222c2261223a317d",'
        b'"name":"object-order-nfc"},'
        b'{"canonical_hex":"7b226d6178223a393030373139393235343734303939312c'
        b'226d696e223a2d393030373139393235343734303939317d",'
        b'"input_hex":"7b226d6178223a393030373139393235343734303939312c'
        b'226d696e223a2d393030373139393235343734303939317d",'
        b'"name":"safe-integer-bounds"}]}'
    )

    assert len(expected) == 443
    assert _sha256(expected) == CANONICALIZATION_DIGEST
    assert CANONICALIZATION_FIXTURE.read_bytes() == expected
    assert subject.canonical_json_bytes(json.loads(expected)) == expected
    assert subject.canonicalization_vector_digest() == CANONICALIZATION_DIGEST


@pytest.mark.parametrize(
    "value",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": 1.5},
        {"value": 9007199254740992},
        {"value": "e\N{COMBINING ACUTE ACCENT}"},
        {"value": "\x00"},
        {1: "non-string-key"},
    ],
)
def test_canonical_json_rejects_non_i_json(value: object) -> None:
    with pytest.raises(ValueError):
        subject.canonical_json_bytes(value)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":1}',
        b'{"value":1.5}',
        b'{"value":9007199254740992}',
        b'{"value":"e\\u0301"}',
        b"\xef\xbb\xbf{}",
        b"{}\n",
        b'{"b":1,"a":2}',
    ],
)
def test_strict_canonical_json_rejects_wire_mutants(raw: bytes) -> None:
    with pytest.raises(ValueError):
        subject.strict_canonical_json(raw, label="fixture")


def test_external_json_boundary_preserves_release_body_newlines() -> None:
    value = {"body": "line one\n\nline two", "id": 1}
    raw = b'{"body":"line one\\n\\nline two","id":1}'

    assert subject.canonical_external_json_bytes(value) == raw
    assert subject.parse_external_json_bytes(raw, label="GitHub Release") == value
    with pytest.raises(ValueError, match="control character"):
        subject.canonical_json_bytes(value)


def test_source_bundle_digest_matches_independent_length_framing() -> None:
    sources = {"z-last": b'{"z":1}', "a-first": b'{"a":1}'}
    expected = hashlib.sha256()
    expected.update(b"Kestrel-Source-Bundle-v1\0")
    for name, raw in sorted(sources.items(), key=lambda item: item[0].encode("utf-8")):
        encoded = name.encode("utf-8")
        expected.update(struct.pack(">I", len(encoded)))
        expected.update(encoded)
        expected.update(struct.pack(">Q", len(raw)))
        expected.update(raw)

    assert subject.source_bundle_digest(sources) == "sha256:" + expected.hexdigest()


def test_source_bundle_digest_rejects_duplicate_or_unbounded_inputs() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        subject.source_bundle_digest([("same", b"one"), ("same", b"two")])

    class EndlessSources:
        def __iter__(self) -> object:
            for index in range(10_000):
                yield f"source-{index}", b"{}"

    with pytest.raises(ValueError, match="too many"):
        subject.source_bundle_digest(EndlessSources())  # type: ignore[arg-type]


def test_capture_source_derives_singleton_metadata_identity_and_clock() -> None:
    raw = b'{"id":303}'
    now = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC)
    envelope = subject.capture_source(
        registry=_registry(_registry_entry()),
        receipt_schema="kestrel.github_release_authority.v3",
        phase="admission",
        mode="initiate",
        name="repository-rest",
        raw_input=raw,
        identity_observation=b'{"login":"John-MiracleWorker"}',
        _clock=lambda: now,
    )

    assert envelope == {
        "schema": "kestrel.source_observation.v1",
        "name": "repository-rest",
        "provider": "github.com",
        "locator": "GET /repos/John-MiracleWorker/Kestrel",
        "authenticated_as": "John-MiracleWorker",
        "freshness_class": "current",
        "captured_at": "2026-08-13T20:00:00Z",
        "page_count": 1,
        "record_count": 1,
        "complete": True,
        "body_encoding": "base64",
        "body": base64.b64encode(raw).decode("ascii"),
    }


def test_capture_source_accepts_body_larger_than_registry_metadata_limit() -> None:
    raw = b"x" * (subject.MAX_REGISTRY_STRING_BYTES + 1)
    entry = _registry_entry(
        provider="local",
        authentication_mode="local",
        body_mode="singleton-bytes",
    )

    envelope = subject.capture_source(
        registry=_registry(entry),
        receipt_schema="kestrel.github_release_authority.v3",
        phase="admission",
        mode="initiate",
        name="repository-rest",
        raw_input=raw,
        identity_observation=None,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
    )

    assert subject.source_observation_body(_canonical(envelope)) == raw


def test_capture_source_derives_paginated_counts_and_actions_identity() -> None:
    entry = _registry_entry(
        name="promotion-runs-rest",
        authentication_mode="github-actions-run",
        body_mode="paginated-json",
        count_mode="sum-page-array",
    )
    first_url = str(entry["locator"])
    second_url = f"{first_url}&page=2"
    raw = _canonical(
        {
            "pages": [
                {
                    "number": 1,
                    "request_url": first_url,
                    "response_headers": [["link", f'<{second_url}>; rel="next"']],
                    "body": [{"id": 1}],
                },
                {
                    "number": 2,
                    "request_url": second_url,
                    "response_headers": [],
                    "body": [{"id": 2}, {"id": 3}],
                },
            ]
        }
    )
    context = _canonical({"repository_id": 303, "run_id": 707})

    envelope = subject.capture_source(
        registry=_registry(entry),
        receipt_schema="kestrel.github_release_authority.v3",
        phase="admission",
        mode="initiate",
        name="promotion-runs-rest",
        raw_input=raw,
        identity_observation=context,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
    )

    assert envelope["authenticated_as"] == "github-actions:303:707"
    assert envelope["page_count"] == 2
    assert envelope["record_count"] == 3
    assert base64.b64decode(str(envelope["body"]), validate=True) == raw


def test_capture_source_preserves_external_json_control_characters() -> None:
    entry = _registry_entry(
        body_mode="paginated-json",
        count_mode="sum-page-array",
    )
    raw = subject.canonical_external_json_bytes(
        {
            "pages": [
                {
                    "number": 1,
                    "request_url": entry["locator"],
                    "response_headers": [],
                    "body": [{"body": "line one\n\nline two"}],
                }
            ]
        }
    )

    envelope = subject.capture_source(
        registry=_registry(entry),
        receipt_schema="kestrel.github_release_authority.v3",
        phase="admission",
        mode="initiate",
        name="repository-rest",
        raw_input=raw,
        identity_observation=b'{"login":"John-MiracleWorker"}',
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
    )

    assert subject.source_observation_body(_canonical(envelope)) == raw


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("unknown-name", "registry"),
        ("duplicate-entry", "duplicate"),
        ("unordered-entry", "sorted"),
        ("missing-identity", "identity"),
        ("extra-page-wrapper", "paginated"),
        ("non-array-page", "page"),
    ],
)
def test_capture_source_rejects_registry_and_page_mutants(mutation: str, message: str) -> None:
    first = _registry_entry(
        name="promotion-runs-rest",
        authentication_mode="github-actions-run",
        body_mode="paginated-json",
        count_mode="sum-page-array",
    )
    second = _registry_entry(name="z-source")
    registry = _registry(first, second)
    raw = _canonical(
        {
            "pages": [
                {
                    "number": 1,
                    "request_url": first["locator"],
                    "response_headers": [],
                    "body": [],
                }
            ]
        }
    )
    identity: bytes | None = _canonical({"repository_id": 303, "run_id": 707})
    name = "promotion-runs-rest"
    if mutation == "unknown-name":
        name = "other"
    elif mutation == "duplicate-entry":
        registry["entries"] = [first, first]
    elif mutation == "unordered-entry":
        registry["entries"] = [second, first]
    elif mutation == "missing-identity":
        identity = None
    elif mutation == "extra-page-wrapper":
        raw = _canonical({"pages": [], "extra": True})
    elif mutation == "non-array-page":
        raw = _canonical(
            {
                "pages": [
                    {
                        "number": 1,
                        "request_url": first["locator"],
                        "response_headers": [],
                        "body": {},
                    }
                ]
            }
        )
    else:  # pragma: no cover
        raise AssertionError(mutation)

    with pytest.raises(ValueError, match=message):
        subject.capture_source(
            registry=registry,
            receipt_schema="kestrel.github_release_authority.v3",
            phase="admission",
            mode="initiate",
            name=name,
            raw_input=raw,
            identity_observation=identity,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        )


def test_receipt_freshness_accepts_exact_window_and_derives_expiry() -> None:
    first = _source("first", "2026-08-13T20:00:00Z")
    last = _source("last", "2026-08-13T20:02:00Z")
    historical = _source("historical", "2020-01-01T00:00:00Z", freshness_class="historical")
    acknowledgement = {
        "begins_at": "2026-08-13T19:59:59Z",
        "expires_at": "2026-08-13T20:07:00Z",
    }

    observed_at, expires_at = subject.validate_receipt_freshness(
        [last, historical, first],
        acknowledgement=acknowledgement,
        _clock=lambda: datetime(2026, 8, 13, 20, 10, 3, tzinfo=UTC),
    )

    assert observed_at == "2026-08-13T20:02:00Z"
    assert expires_at == "2026-08-13T20:07:00Z"


@pytest.mark.parametrize(
    ("captures", "now", "message"),
    [
        (
            ["2026-08-13T20:00:00Z", "2026-08-13T20:02:01Z"],
            "2026-08-13T20:02:01Z",
            "120",
        ),
        (
            ["2026-08-13T20:00:01Z"],
            "2026-08-13T20:00:00Z",
            "future",
        ),
        ([], "2026-08-13T20:00:00Z", "current"),
    ],
)
def test_receipt_freshness_rejects_invalid_windows(
    captures: list[str], now: str, message: str
) -> None:
    sources = [_source(str(index), capture) for index, capture in enumerate(captures)]
    with pytest.raises(ValueError, match=message):
        subject.validate_receipt_freshness(
            sources,
            acknowledgement={
                "begins_at": "2026-08-13T19:00:00Z",
                "expires_at": "2026-08-14T00:00:00Z",
            },
            _clock=lambda: datetime.fromisoformat(now.replace("Z", "+00:00")),
        )


def test_verify_receipt_time_passes_observed_and_fails_exact_expiry() -> None:
    assert subject.verify_receipt_time(
        observed_at="2026-08-13T20:00:00Z",
        expires_at="2026-08-13T20:05:00Z",
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="expired"):
        subject.verify_receipt_time(
            observed_at="2026-08-13T20:00:00Z",
            expires_at="2026-08-13T20:05:00Z",
            _clock=lambda: datetime(2026, 8, 13, 20, 5, 0, tzinfo=UTC),
        )


def test_write_once_is_exactly_idempotent_and_rejects_divergence(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    raw = b'{"schema":"example"}'

    assert subject.write_once(output, raw) is True
    assert subject.write_once(output, raw) is False
    with pytest.raises(ValueError, match="conflict"):
        subject.write_once(output, b"{}")
    assert output.read_bytes() == raw


def test_write_once_has_no_hardlink_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_link(source: object, target: object) -> None:
        raise AssertionError(f"hardlink attempted: {source!r} -> {target!r}")

    monkeypatch.setattr(subject.os, "link", forbidden_link)
    output = tmp_path / "portable.json"

    assert subject.write_once(output, b'{"portable":true}') is True
    assert output.read_bytes() == b'{"portable":true}'


def test_windows_directory_durability_is_not_silently_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushed: list[Path] = []
    monkeypatch.setattr(
        subject,
        "_flush_windows_directory",
        lambda path: flushed.append(path),
    )

    subject._fsync_directory(tmp_path, _platform_name="nt")  # noqa: SLF001

    assert flushed == [tmp_path]


def test_windows_directory_flush_opens_a_generic_write_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCall:
        def __init__(self, result: int) -> None:
            self.argtypes: list[object] = []
            self.restype: object | None = None
            self.result = result
            self.calls: list[tuple[object, ...]] = []

        def __call__(self, *args: object) -> int:
            self.calls.append(args)
            return self.result

    class FakeKernel32:
        def __init__(self) -> None:
            self.CreateFileW = FakeCall(101)
            self.FlushFileBuffers = FakeCall(1)
            self.CloseHandle = FakeCall(1)

    kernel32 = FakeKernel32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)

    subject._flush_windows_directory(tmp_path)  # noqa: SLF001

    assert kernel32.CreateFileW.calls[0][1] == 0x40000000
    assert kernel32.FlushFileBuffers.calls == [(101,)]
    assert kernel32.CloseHandle.calls == [(101,)]


def test_capture_source_cli_has_no_caller_time_or_metadata_options() -> None:
    help_result = subprocess.run(
        [sys.executable, str(SCRIPT), "capture-source", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert help_result.returncode == 0
    assert "--captured-at" not in help_result.stdout
    assert "--provider" not in help_result.stdout
    assert "--locator" not in help_result.stdout
    assert "--authenticated-as" not in help_result.stdout
    assert "--as-of" not in help_result.stdout


def test_internal_clock_is_not_exposed_by_cli() -> None:
    parser = subject._parser()
    option_strings = {option for action in parser._actions for option in action.option_strings}
    assert "--clock" not in option_strings
    assert "--now" not in option_strings


def test_timestamp_helpers_reject_subseconds_offsets_and_non_utc() -> None:
    for value in (
        "2026-08-13T20:00:00.000Z",
        "2026-08-13T20:00:00+00:00",
        "2026-08-13T16:00:00-04:00",
        "2026-08-13 20:00:00Z",
    ):
        with pytest.raises(ValueError):
            subject.parse_timestamp(value, label="timestamp")


def test_capture_source_preserves_semantically_valid_noncanonical_raw_json() -> None:
    raw = b'{ "id": 303 }'
    envelope = subject.capture_source(
        registry=_registry(_registry_entry()),
        receipt_schema="kestrel.github_release_authority.v3",
        phase="admission",
        mode="initiate",
        name="repository-rest",
        raw_input=raw,
        identity_observation=b'{"login":"John-MiracleWorker"}',
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
    )

    assert base64.b64decode(str(envelope["body"]), validate=True) == raw


@pytest.mark.parametrize("mutation", ["legacy", "loop", "early-terminal"])
def test_capture_source_requires_proven_complete_pagination(mutation: str) -> None:
    entry = _registry_entry(
        name="promotion-runs-rest",
        authentication_mode="github-actions-run",
        body_mode="paginated-json",
        count_mode="sum-page-array",
    )
    first_url = str(entry["locator"])
    second_url = f"{first_url}&page=2"
    if mutation == "legacy":
        value: object = {"pages": [[], []]}
    else:
        next_url = first_url if mutation == "loop" else second_url
        first_headers = (
            [] if mutation == "early-terminal" else [["link", f'<{next_url}>; rel="next"']]
        )
        value = {
            "pages": [
                {
                    "number": 1,
                    "request_url": first_url,
                    "response_headers": first_headers,
                    "body": [],
                },
                {
                    "number": 2,
                    "request_url": second_url,
                    "response_headers": [],
                    "body": [],
                },
            ]
        }

    with pytest.raises(ValueError, match="pag|link|loop|complete|page"):
        subject.capture_source(
            registry=_registry(entry),
            receipt_schema="kestrel.github_release_authority.v3",
            phase="admission",
            mode="initiate",
            name="promotion-runs-rest",
            raw_input=_canonical(value),
            identity_observation=_canonical({"repository_id": 303, "run_id": 707}),
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        )


def test_dispatch_source_registry_has_exact_phase_and_mode_closure() -> None:
    registry = json.loads((ROOT / "release-control-source-registry.json").read_bytes())
    entries = registry["entries"]
    dispatch = [
        entry
        for entry in entries
        if entry["receipt_schema"]
        in {
            "kestrel.dispatch_admission.v1",
            "kestrel.dispatch_identity.v1",
            "kestrel.dispatch_tombstone.v1",
            "kestrel.release_dispatch_intent.v2",
            "kestrel.release_dispatch_reconciliation.v1",
        }
    ]
    assert len(dispatch) == 26
    preparation_names = {
        "candidate-manifest",
        "candidate-workflow-contents",
        "default-branch-workflow-contents",
        "dispatcher-observation",
        "prior-intents-observation",
        "repository-rest",
        "workflow-rest",
    }
    for mode in ("initiate", "recover_committed"):
        assert {
            entry["name"]
            for entry in dispatch
            if entry["receipt_schema"] == "kestrel.release_dispatch_intent.v2"
            and entry["phase"] == "prepare"
            and entry["mode"] == mode
        } == preparation_names
    assert {
        entry["name"]
        for entry in dispatch
        if entry["receipt_schema"] == "kestrel.release_dispatch_reconciliation.v1"
    } == {
        "containment",
        "dispatch-intent",
        "dispatch-intent-signature",
        "dispatch-request",
        "dispatch-response",
        "identity-artifact-observations",
        "workflow-runs-observation",
    }
    historical_names = {
        "candidate-manifest",
        "dispatch-intent",
        "dispatch-intent-signature",
        "dispatch-request",
        "dispatch-response",
        "reconciliation",
    }
    assert all(
        entry["freshness_class"] == "historical"
        for entry in dispatch
        if entry["name"] in historical_names
    )


def test_validate_receipt_freshness_rejects_acknowledgement_undercoverage() -> None:
    sources = [_source("current", "2026-08-13T20:00:00Z")]
    for acknowledgement in (
        {
            "begins_at": "2026-08-13T20:00:01Z",
            "expires_at": "2026-08-13T20:05:00Z",
        },
        {
            "begins_at": "2026-08-13T19:59:00Z",
            "expires_at": "2026-08-13T20:04:59Z",
        },
    ):
        with pytest.raises(ValueError, match="acknowledgement"):
            subject.validate_receipt_freshness(
                sources,
                acknowledgement=acknowledgement,
                _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
            )


def test_source_snapshot_is_derived_from_exact_envelope_bytes() -> None:
    envelope = _source("repository-rest", "2026-08-13T20:00:00Z")
    raw = subject.canonical_json_bytes(envelope)

    assert subject.source_snapshot(raw) == {
        "name": "repository-rest",
        "provider": "github.com",
        "locator": "GET /repository-rest",
        "authenticated_as": "John-MiracleWorker",
        "freshness_class": "current",
        "captured_at": "2026-08-13T20:00:00Z",
        "page_count": 1,
        "record_count": 1,
        "sha256": _sha256(raw),
        "size_bytes": len(raw),
        "complete": True,
    }


def test_source_snapshot_rejects_unvalidated_envelope() -> None:
    envelope = _source("repository-rest", "2026-08-13T20:00:00Z")
    envelope["complete"] = False
    with pytest.raises(ValueError, match="complete"):
        subject.source_snapshot(_canonical(envelope))


def test_contract_source_reader_rejects_a_mislabeled_envelope() -> None:
    registry = json.loads((ROOT / "release-control-source-registry.json").read_bytes())
    raw_body = _canonical(
        {
            "id": 303,
            "full_name": "John-MiracleWorker/Kestrel",
            "default_branch": "main",
            "default_branch_sha": "a" * 40,
        }
    )
    envelope = subject.capture_source(
        registry=registry,
        receipt_schema="kestrel.release_dispatch_intent.v2",
        phase="prepare",
        mode="initiate",
        name="repository-rest",
        raw_input=raw_body,
        identity_observation=_canonical({"login": "John-MiracleWorker"}),
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
    )
    envelope["name"] = "workflow-rest"

    with pytest.raises(ValueError, match="registry|name|locator"):
        subject.source_observation_body_for_contract(
            _canonical(envelope),
            registry=registry,
            receipt_schema="kestrel.release_dispatch_intent.v2",
            phase="prepare",
            mode="initiate",
            name="repository-rest",
            _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
        )


def test_freshness_math_uses_utc_timedelta_exactly() -> None:
    source = _source("current", "2026-08-13T23:59:59Z")
    observed, expires = subject.validate_receipt_freshness(
        [source],
        acknowledgement={
            "begins_at": "2026-08-13T23:59:59Z",
            "expires_at": "2026-08-14T00:04:59Z",
        },
        _clock=lambda: datetime(2026, 8, 13, 23, 59, 59, tzinfo=UTC),
    )
    assert observed == "2026-08-13T23:59:59Z"
    assert expires == "2026-08-14T00:04:59Z"
    assert subject.parse_timestamp(expires, label="expiry") - subject.parse_timestamp(
        observed, label="observed"
    ) == timedelta(seconds=300)


# ---------------------------------------------------------------------------
# Credential scope authority and runtime verification
# ---------------------------------------------------------------------------


POLICY_PATH = ROOT / "release-control-credential-policy.json"
SCOPE_SCHEMA = ROOT / "schemas" / "kestrel.credential_scope_authority.v1.schema.json"
RUNTIME_SCHEMA = ROOT / "schemas" / "kestrel.runtime_credential_verification.v1.schema.json"


def _policy() -> dict[str, object]:
    return json.loads(POLICY_PATH.read_bytes())


def _principal() -> bytes:
    return _canonical({"login": "release-reader[bot]", "id": 202, "type": "Bot"})


def _scope_context() -> bytes:
    return _canonical(
        {
            "schema": "kestrel.credential_controller_context.v1",
            "issuer": "John-MiracleWorker",
            "signing_key_fingerprint": KNOWN_KEY_FINGERPRINT,
            "issued_at": "2026-08-13T20:00:00Z",
            "expires_at": "2026-08-13T21:00:00Z",
            "captured_at": "2026-08-13T20:00:00Z",
            "complete": True,
        }
    )


def _policy_purpose(name: str) -> dict[str, object]:
    purposes = _policy()["purposes"]
    assert isinstance(purposes, list)
    matches = [item for item in purposes if item["purpose"] == name]  # type: ignore[index]
    assert len(matches) == 1
    result = matches[0]
    assert isinstance(result, dict)
    return result


def _grants_snapshot(purpose: str = "hosted_smoke_read") -> bytes:
    policy = _policy_purpose(purpose)
    repository_names = policy["repositories"]
    assert isinstance(repository_names, list)
    repositories = [
        {"full_name": name, "id": 303 + index} for index, name in enumerate(repository_names)
    ]
    grants = []
    for grant in policy["grants"]:  # type: ignore[union-attr]
        grants.append(
            {
                "repository_full_name": grant["repository_full_name"],
                "repository_id": next(
                    item["id"]
                    for item in repositories
                    if item["full_name"] == grant["repository_full_name"]
                ),
                "permission": grant["permission"],
                "level": grant["level"],
            }
        )
    return _canonical(
        {
            "schema": "kestrel.credential_grants_snapshot.v1",
            "repositories": repositories,
            "grants": grants,
            "endpoint_allowlist": policy["endpoint_allowlist"],
            "captured_at": "2026-08-13T20:00:00Z",
            "complete": True,
        }
    )


def _scope_authority(
    *,
    purpose: str = "hosted_smoke_read",
    token: bytes = b"high-entropy-test-token-1234567890",
    **overrides: object,
) -> dict[str, object]:
    value = subject.create_credential_scope_authority(
        purpose=purpose,
        credential_id=f"credential-{purpose}",
        principal_observation=_principal(),
        grants_snapshot=_grants_snapshot(purpose),
        token_fingerprint=_sha256(token),
        controller_context=_scope_context(),
    )
    value.update(overrides)
    return value


def test_credential_policy_is_exact_canonical_and_complete() -> None:
    raw = POLICY_PATH.read_bytes()
    policy = json.loads(raw)
    assert raw == _canonical(policy)
    assert set(policy) == {"schema", "purposes"}
    assert policy["schema"] == "kestrel.release_control_credential_policy.v1"
    purposes = policy["purposes"]
    assert [item["purpose"] for item in purposes] == sorted(
        [
            "hosted_smoke_dispatch",
            "hosted_smoke_read",
            "promotion_dispatcher",
            "promotion_reconciliation_reader",
            "recovery_reader",
            "release_guard",
        ]
    )
    assert len({item["purpose"] for item in purposes}) == 6
    for item in purposes:
        assert set(item) == {
            "purpose",
            "repositories",
            "grants",
            "endpoint_allowlist",
            "read_only",
        }
        assert item["repositories"] == sorted(item["repositories"])
        assert item["endpoint_allowlist"] == sorted(item["endpoint_allowlist"])
        assert item["grants"] == sorted(
            item["grants"],
            key=lambda grant: (grant["repository_full_name"], grant["permission"]),
        )


def test_credential_policy_fixes_exact_repository_grant_matrix() -> None:
    expected = {
        "hosted_smoke_dispatch": (
            "John-MiracleWorker/Kestrel",
            (("Actions", "write"), ("Metadata", "read")),
            False,
        ),
        "hosted_smoke_read": (
            "John-MiracleWorker/Kestrel",
            (
                ("Actions", "read"),
                ("Administration", "read"),
                ("Contents", "read"),
                ("Metadata", "read"),
            ),
            True,
        ),
        "promotion_dispatcher": (
            "John-MiracleWorker/Kestrel",
            (("Actions", "write"), ("Metadata", "read")),
            False,
        ),
        "promotion_reconciliation_reader": (
            "John-MiracleWorker/Kestrel",
            (("Actions", "read"), ("Metadata", "read")),
            True,
        ),
        "recovery_reader": (
            "John-MiracleWorker/Kestrel-Release-Recovery",
            (("Contents", "read"), ("Metadata", "read")),
            True,
        ),
        "release_guard": (
            "John-MiracleWorker/Kestrel",
            (
                ("Actions", "read"),
                ("Administration", "read"),
                ("Attestations", "read"),
                ("Contents", "read"),
                ("Metadata", "read"),
                ("Packages", "read"),
            ),
            True,
        ),
    }

    for purpose, (repository, grants, read_only) in expected.items():
        policy = _policy_purpose(purpose)
        assert policy["repositories"] == [repository]
        assert [
            (grant["permission"], grant["level"])
            for grant in policy["grants"]  # type: ignore[union-attr]
        ] == list(grants)
        assert policy["read_only"] is read_only


def test_create_credential_scope_authority_matches_schema_and_policy() -> None:
    receipt = _scope_authority()
    schema = json.loads(SCOPE_SCHEMA.read_bytes())

    import jsonschema

    jsonschema.Draft202012Validator(schema).validate(receipt)
    assert receipt["purpose"] == "hosted_smoke_read"
    assert receipt["revoked"] is False
    assert receipt["confidence"] == 1
    assert receipt["validation_status"] == "validated"
    assert receipt["token_fingerprint"] == _sha256(b"high-entropy-test-token-1234567890")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("extra-grant", "grant"),
        ("missing-grant", "grant"),
        ("write-readonly", "read-only"),
        ("wrong-repository", "repository"),
        ("wrong-endpoint", "endpoint"),
        ("duplicate-grant", "duplicate"),
        ("bad-fingerprint", "fingerprint"),
        ("stale-context", "expiry"),
    ],
)
def test_create_credential_scope_authority_rejects_policy_mutants(
    mutation: str, message: str
) -> None:
    snapshot = json.loads(_grants_snapshot())
    context = json.loads(_scope_context())
    fingerprint = _sha256(b"high-entropy-test-token-1234567890")
    if mutation == "extra-grant":
        snapshot["grants"].append(
            {
                "repository_full_name": "John-MiracleWorker/Kestrel",
                "repository_id": 303,
                "permission": "Secrets",
                "level": "read",
            }
        )
    elif mutation == "missing-grant":
        snapshot["grants"].pop()
    elif mutation == "write-readonly":
        snapshot["grants"][0]["level"] = "write"
    elif mutation == "wrong-repository":
        snapshot["repositories"][0]["full_name"] = "attacker/Kestrel"
    elif mutation == "wrong-endpoint":
        snapshot["endpoint_allowlist"].append("GET /user/emails")
        snapshot["endpoint_allowlist"].sort()
    elif mutation == "duplicate-grant":
        snapshot["grants"].append(dict(snapshot["grants"][0]))
    elif mutation == "bad-fingerprint":
        fingerprint = "sha256:" + "A" * 64
    elif mutation == "stale-context":
        context["expires_at"] = context["issued_at"]
    else:  # pragma: no cover
        raise AssertionError(mutation)

    with pytest.raises(ValueError, match=message):
        subject.create_credential_scope_authority(
            purpose="hosted_smoke_read",
            credential_id="credential-hosted-smoke-read",
            principal_observation=_principal(),
            grants_snapshot=_canonical(snapshot),
            token_fingerprint=fingerprint,
            controller_context=_canonical(context),
        )


def _endpoint_probes(scope: dict[str, object]) -> bytes:
    allowed = scope["endpoint_allowlist"]
    assert isinstance(allowed, list)
    results = [
        {"endpoint": endpoint, "http_status": 200, "response_digest": _sha256(endpoint.encode())}
        for endpoint in allowed
    ]
    results.append(
        {
            "endpoint": "POST /repos/{repository}/actions/workflows/{workflow_id}/dispatches",
            "http_status": 403,
            "response_digest": "sha256:" + "f" * 64,
        }
    )
    results.sort(key=lambda item: item["endpoint"])
    return _canonical(
        {
            "schema": "kestrel.credential_endpoint_probes.v1",
            "credential_id": scope["credential_id"],
            "results": results,
            "captured_at": "2026-08-13T20:01:00Z",
            "complete": True,
        }
    )


def test_runtime_credential_verification_matches_fingerprint_and_endpoint_policy(
    tmp_path: Path,
) -> None:
    token = b"high-entropy-test-token-1234567890"
    scope = _scope_authority(token=token)
    scope_bytes = _canonical(scope)
    verification = subject.verify_runtime_credential(
        scope_authority=scope_bytes,
        scope_authority_signature=_scope_signature(tmp_path, scope_bytes),
        owner_signing_keys_observation=_owner_signing_keys_observation(),
        identity_probe=_principal(),
        endpoint_probe_observations=_endpoint_probes(scope),
        token_bytes=token,
        _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
    )
    schema = json.loads(RUNTIME_SCHEMA.read_bytes())

    import jsonschema

    jsonschema.Draft202012Validator(schema).validate(verification)
    assert verification["scope_authority_digest"] == _sha256(_canonical(scope))
    assert verification["token_fingerprint"] == _sha256(token)
    assert token not in _canonical(verification)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("wrong-token", "fingerprint"),
        ("revoked", "revoked"),
        ("expired", "expired"),
        ("wrong-principal", "principal"),
        ("missing-allowed", "allowed endpoint"),
        ("allowed-denied", "allowed endpoint"),
        ("no-forbidden", "forbidden endpoint"),
        ("forbidden-succeeds", "forbidden endpoint"),
    ],
)
def test_runtime_credential_verification_rejects_scope_and_probe_mutants(
    tmp_path: Path, mutation: str, message: str
) -> None:
    token = b"high-entropy-test-token-1234567890"
    scope = _scope_authority(token=token)
    identity = json.loads(_principal())
    probes = json.loads(_endpoint_probes(scope))
    now = datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC)
    if mutation == "wrong-token":
        token = b"different-high-entropy-token-123456789"
    elif mutation == "revoked":
        scope["revoked"] = True
    elif mutation == "expired":
        now = datetime(2026, 8, 13, 21, 0, 0, tzinfo=UTC)
    elif mutation == "wrong-principal":
        identity["id"] += 1
    elif mutation == "missing-allowed":
        allowed = set(scope["endpoint_allowlist"])  # type: ignore[arg-type]
        probes["results"] = [item for item in probes["results"] if item["endpoint"] != min(allowed)]
    elif mutation == "allowed-denied":
        allowed = set(scope["endpoint_allowlist"])  # type: ignore[arg-type]
        target = next(item for item in probes["results"] if item["endpoint"] in allowed)
        target["http_status"] = 403
    elif mutation == "no-forbidden":
        allowed = set(scope["endpoint_allowlist"])  # type: ignore[arg-type]
        probes["results"] = [item for item in probes["results"] if item["endpoint"] in allowed]
    elif mutation == "forbidden-succeeds":
        allowed = set(scope["endpoint_allowlist"])  # type: ignore[arg-type]
        target = next(item for item in probes["results"] if item["endpoint"] not in allowed)
        target["http_status"] = 200
    else:  # pragma: no cover
        raise AssertionError(mutation)

    with pytest.raises(ValueError, match=message):
        scope_bytes = _canonical(scope)
        subject.verify_runtime_credential(
            scope_authority=scope_bytes,
            scope_authority_signature=_scope_signature(tmp_path, scope_bytes),
            owner_signing_keys_observation=_owner_signing_keys_observation(
                captured_at=now.strftime("%Y-%m-%dT%H:%M:%SZ")
            ),
            identity_probe=_canonical(identity),
            endpoint_probe_observations=_canonical(probes),
            token_bytes=token,
            _clock=lambda: now,
        )


def test_credential_cli_never_accepts_raw_token_argument() -> None:
    help_result = subprocess.run(
        [sys.executable, str(SCRIPT), "verify-runtime-credential", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "--token" not in help_result.stdout
    assert "--secret" not in help_result.stdout
    assert "--authorization" not in help_result.stdout


# ---------------------------------------------------------------------------
# OpenSSH detached signature trust boundary
# ---------------------------------------------------------------------------


SIGNING_NAMESPACE = "kestrel-release-control-v1"
SIGNING_PRINCIPAL = "John-MiracleWorker"
KNOWN_PUBLIC_KEY = (
    b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHm1Vi6P5lT5QHixEuipi6eQH4U65pW+1+DjkQutBJZk"
)
KNOWN_KEY_FINGERPRINT = "sha256:7959022879dce518da9c176e536be639e71b165dccf703a01dd936e08349cad6"
KNOWN_SIGNATURE_DIGEST = "sha256:acbfaecdac5ca2bec3cd424a8c8a240be18495212cc962fc4678fe6424a2e674"


def _known_identity_file(tmp_path: Path) -> Path:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    identity = tmp_path / "test-release-controller"
    identity.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    identity.chmod(0o600)
    assert (
        private_key.public_key().public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
        == KNOWN_PUBLIC_KEY
    )
    return identity


def _scope_signature(tmp_path: Path, scope: bytes) -> bytes:
    return subject.sign_receipt_detached(
        receipt=scope,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )


def _attacker_identity_file(tmp_path: Path) -> tuple[Path, str]:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
    identity = tmp_path / "attacker-release-controller"
    identity.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    identity.chmod(0o600)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    )
    return identity, subject.ssh_public_key_fingerprint(public_key.decode("ascii"))


def _known_signed_receipt() -> bytes:
    return _canonical(
        {
            "schema": "known-signature-vector.v1",
            "signing_key_fingerprint": KNOWN_KEY_FINGERPRINT,
        }
    )


def _owner_signing_keys_observation(**overrides: object) -> bytes:
    keys = overrides.pop(
        "keys",
        [
            {
                "id": 404,
                "key": KNOWN_PUBLIC_KEY.decode("ascii"),
                "title": "Kestrel release controller",
            }
        ],
    )
    owner_login = overrides.pop("owner_login", SIGNING_PRINCIPAL)
    captured_at = overrides.pop("captured_at", "2026-08-13T20:00:00Z")
    complete = overrides.pop("complete", True)
    assert overrides == {}
    registry = json.loads(SOURCE_REGISTRY.read_bytes())
    entry = next(
        item
        for item in registry["entries"]
        if item["receipt_schema"] == subject.SOURCE_OBSERVATION_SCHEMA
        and item["phase"] == "release-control"
        and item["mode"] is None
        and item["name"] == "owner-signing-keys-observation"
    )
    body = _canonical(
        {
            "pages": [
                {
                    "number": 1,
                    "request_url": entry["locator"],
                    "response_headers": [],
                    "body": keys,
                }
            ]
        }
    )
    captured = subject.parse_timestamp(captured_at, label="test owner keys captured_at")
    value = subject.capture_source(
        registry=registry,
        receipt_schema=subject.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="owner-signing-keys-observation",
        raw_input=body,
        identity_observation=_canonical({"login": owner_login}),
        _clock=lambda: captured,
    )
    value["complete"] = complete
    return _canonical(value)


def _repository_writer_inventory(
    phase: str,
    *,
    captured_at: str = "2026-08-13T20:00:00Z",
    nonce_run_ids: list[int] | None = None,
) -> bytes:
    pre_send = phase == "pre_send"
    return _canonical(
        {
            "schema": "kestrel.repository_writer_inventory.v1",
            "phase": phase,
            "repository": {
                "full_name": "John-MiracleWorker/Kestrel",
                "id": 303,
            },
            "owner": {"login": "John-MiracleWorker", "id": 606, "type": "User"},
            "repository_writers": [
                {
                    "login": "John-MiracleWorker",
                    "id": 606,
                    "type": "User",
                    "role_name": "admin",
                }
            ],
            "invitations": [],
            "write_deploy_keys": [],
            "installed_apps": (
                [
                    {
                        "app_id": 909,
                        "installation_id": 1001,
                        "bot_login": "kestrel-release-dispatcher[bot]",
                        "bot_id": 808,
                        "permissions": {"actions": "write", "metadata": "read"},
                    }
                ]
                if pre_send
                else []
            ),
            "actions_write_principals": (
                [
                    {
                        "kind": "GitHubApp",
                        "login": "kestrel-release-dispatcher[bot]",
                        "id": 808,
                        "app_id": 909,
                        "installation_id": 1001,
                    }
                ]
                if pre_send
                else []
            ),
            "mutation_capable_runs": [],
            "nonce_run_ids": [] if nonce_run_ids is None else nonce_run_ids,
            "captured_at": captured_at,
            "complete": True,
            "evidence": {
                "source_bundle_digest": "sha256:" + "1" * 64,
                "canonicalization_vector_digest": CANONICALIZATION_DIGEST,
            },
            "provenance": {
                "producer": "kestrel-release-controller",
                "provider": "github.com",
                "method": "complete-writer-inventory",
            },
            "confidence": 1,
            "validation_status": "validated",
        }
    )


def _signed_writer_inventory_arguments(
    phase: str,
    *,
    nonce_run_ids: list[int] | None = None,
    captured_at: str | None = None,
) -> dict[str, bytes]:
    captured = (
        "2026-08-13T20:00:02Z"
        if captured_at is None and phase == "post_containment"
        else ("2026-08-13T20:00:00Z" if captured_at is None else captured_at)
    )
    inventory = _repository_writer_inventory(
        phase, captured_at=captured, nonce_run_ids=nonce_run_ids
    )
    with tempfile.TemporaryDirectory(prefix="kestrel-writer-inventory-test-") as root:
        signature = subject.sign_receipt_detached(
            receipt=inventory,
            identity_file=_known_identity_file(Path(root)),
            principal=SIGNING_PRINCIPAL,
            namespace=SIGNING_NAMESPACE,
        )
    return {
        "post_containment_writer_inventory": inventory,
        "post_containment_writer_inventory_signature": signature,
        "owner_signing_keys_observation": _owner_signing_keys_observation(),
    }


def _pre_admission_writer_inventory_arguments() -> dict[str, object]:
    signed = _signed_writer_inventory_arguments("pre_admission", nonce_run_ids=[1101])
    return {
        "pre_admission_writer_inventory": signed["post_containment_writer_inventory"],
        "pre_admission_writer_inventory_signature": signed[
            "post_containment_writer_inventory_signature"
        ],
        "owner_signing_keys_observation": signed["owner_signing_keys_observation"],
        "minimum_writer_inventory_captured_at": datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
    }


@pytest.mark.parametrize(
    ("phase", "nonce_run_ids"),
    [("pre_send", []), ("post_containment", [1101]), ("pre_admission", [1101])],
)
def test_signed_repository_writer_inventory_enforces_each_phase_boundary(
    tmp_path: Path, phase: str, nonce_run_ids: list[int]
) -> None:
    journal, _, _ = _prepared_dispatch()
    inventory = _repository_writer_inventory(phase, nonce_run_ids=nonce_run_ids)
    signature = subject.sign_receipt_detached(
        receipt=inventory,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )

    checked = subject.verify_repository_writer_inventory(
        inventory=inventory,
        signature=signature,
        owner_signing_keys_observation=_owner_signing_keys_observation(),
        journal=journal,
        phase=phase,
        expected_run_id=1101 if phase == "pre_admission" else None,
        _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
    )

    assert checked["phase"] == phase


@pytest.mark.parametrize(
    "mutation",
    ["invitation", "deploy-key", "app", "writer", "run", "nonce-duplicate"],
)
def test_signed_repository_writer_inventory_rejects_hidden_authority(
    tmp_path: Path, mutation: str
) -> None:
    journal, _, _ = _prepared_dispatch()
    value = json.loads(_repository_writer_inventory("pre_admission", nonce_run_ids=[1101]))
    if mutation == "invitation":
        value["invitations"] = [{"id": 1, "login": "other", "permission": "write"}]
    elif mutation == "deploy-key":
        value["write_deploy_keys"] = [{"id": 1, "title": "writer"}]
    elif mutation == "app":
        value["installed_apps"] = [
            {
                "app_id": 999,
                "installation_id": 998,
                "bot_login": "other[bot]",
                "bot_id": 997,
                "permissions": {"actions": "write", "metadata": "read"},
            }
        ]
    elif mutation == "writer":
        value["actions_write_principals"] = [
            {
                "kind": "User",
                "login": "other",
                "id": 999,
                "app_id": None,
                "installation_id": None,
            }
        ]
    elif mutation == "run":
        value["mutation_capable_runs"] = [
            {"run_id": 7, "workflow_id": 707, "status": "in_progress"}
        ]
    elif mutation == "nonce-duplicate":
        value["nonce_run_ids"] = [1101, 1102]
    inventory = _canonical(value)
    signature = subject.sign_receipt_detached(
        receipt=inventory,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )

    with pytest.raises(ValueError, match="writer|invitation|deploy|App|run|nonce|authority"):
        subject.verify_repository_writer_inventory(
            inventory=inventory,
            signature=signature,
            owner_signing_keys_observation=_owner_signing_keys_observation(),
            journal=journal,
            phase="pre_admission",
            expected_run_id=1101,
            _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
        )


def test_openssh_signature_matches_frozen_known_answer(tmp_path: Path) -> None:
    receipt = _known_signed_receipt()
    assert len(receipt) == 138
    assert _sha256(receipt) == (
        "sha256:7044dcbc617880fbb646ca9e1ec34133c3760027303bd8974a35a79e0792be7b"
    )

    signature = subject.sign_receipt_detached(
        receipt=receipt,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )

    assert len(signature) == 326
    assert _sha256(signature) == KNOWN_SIGNATURE_DIGEST
    assert subject.signature_public_key_fingerprint(signature) == KNOWN_KEY_FINGERPRINT
    assert subject.verify_detached_signature(
        receipt=receipt,
        signature=signature,
        expected_fingerprint=KNOWN_KEY_FINGERPRINT,
        namespace=SIGNING_NAMESPACE,
    )


def test_signature_crypto_does_not_execute_an_unpinned_openssh_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signing and verification are byte-pinned Python crypto, not PATH tooling."""

    def forbidden_subprocess(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"unexpected subprocess execution: {args!r} {kwargs!r}")

    monkeypatch.setattr(subject.subprocess, "run", forbidden_subprocess)
    receipt = _known_signed_receipt()
    signature = subject.sign_receipt_detached(
        receipt=receipt,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )

    assert _sha256(signature) == KNOWN_SIGNATURE_DIGEST
    assert subject.verify_detached_signature(
        receipt=receipt,
        signature=signature,
        expected_fingerprint=KNOWN_KEY_FINGERPRINT,
        namespace=SIGNING_NAMESPACE,
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("receipt", "signature"),
        ("signature", "signature"),
        ("namespace", "namespace"),
        ("fingerprint", "fingerprint"),
    ],
)
def test_openssh_signature_rejects_binding_mutants(
    tmp_path: Path, mutation: str, message: str
) -> None:
    receipt = _known_signed_receipt()
    signature = subject.sign_receipt_detached(
        receipt=receipt,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    namespace = SIGNING_NAMESPACE
    fingerprint = KNOWN_KEY_FINGERPRINT
    if mutation == "receipt":
        receipt = _canonical(
            {
                "schema": "different-signature-vector.v1",
                "signing_key_fingerprint": KNOWN_KEY_FINGERPRINT,
            }
        )
    elif mutation == "signature":
        signature = signature.replace(b"U1NI", b"A1NI", 1)
    elif mutation == "namespace":
        namespace = "other-release-control-v1"
    elif mutation == "fingerprint":
        fingerprint = "sha256:" + "b" * 64
    else:  # pragma: no cover
        raise AssertionError(mutation)

    with pytest.raises(ValueError, match=message):
        subject.verify_detached_signature(
            receipt=receipt,
            signature=signature,
            expected_fingerprint=fingerprint,
            namespace=namespace,
        )


def test_owner_signature_requires_fresh_exact_singleton_registered_key(
    tmp_path: Path,
) -> None:
    receipt = _known_signed_receipt()
    signature = subject.sign_receipt_detached(
        receipt=receipt,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )

    assert subject.verify_owner_detached_signature(
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=_owner_signing_keys_observation(),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
        _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
    )


def test_owner_signature_rejects_caller_forged_key_not_in_live_github(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _known_signed_receipt()
    signature = subject.sign_receipt_detached(
        receipt=receipt,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    attacker_key = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
    attacker_public = attacker_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    )
    monkeypatch.setattr(
        subject,
        "_fetch_owner_signing_keys_from_github",
        lambda principal: [
            {"id": 405, "key": attacker_public.decode("ascii"), "title": "attacker"}
        ],
    )

    with pytest.raises(ValueError, match="independent GitHub observation"):
        subject.verify_owner_detached_signature(
            receipt=receipt,
            signature=signature,
            owner_signing_keys_observation=_owner_signing_keys_observation(),
            principal=SIGNING_PRINCIPAL,
            namespace=SIGNING_NAMESPACE,
            _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("observation", "message"),
    [
        (_owner_signing_keys_observation(keys=[]), "exactly one"),
        (
            _owner_signing_keys_observation(
                keys=[
                    {"id": 404, "key": KNOWN_PUBLIC_KEY.decode(), "title": "first"},
                    {"id": 405, "key": KNOWN_PUBLIC_KEY.decode(), "title": "second"},
                ]
            ),
            "exactly one",
        ),
        (_owner_signing_keys_observation(owner_login="attacker"), "authentication"),
        (_owner_signing_keys_observation(complete=False), "complete"),
        (_owner_signing_keys_observation(captured_at="2026-08-13T19:57:59Z"), "stale"),
    ],
)
def test_owner_signature_rejects_registration_mutants(
    tmp_path: Path, observation: bytes, message: str
) -> None:
    receipt = _known_signed_receipt()
    signature = subject.sign_receipt_detached(
        receipt=receipt,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    with pytest.raises(ValueError, match=message):
        subject.verify_owner_detached_signature(
            receipt=receipt,
            signature=signature,
            owner_signing_keys_observation=observation,
            principal=SIGNING_PRINCIPAL,
            namespace=SIGNING_NAMESPACE,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        )


def test_signing_rejects_identity_not_bound_by_receipt(tmp_path: Path) -> None:
    other = tmp_path / "other-key"
    other.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    other.chmod(0o600)
    with pytest.raises(ValueError, match="fingerprint"):
        subject.sign_receipt_detached(
            receipt=_known_signed_receipt(),
            identity_file=other,
            principal=SIGNING_PRINCIPAL,
            namespace=SIGNING_NAMESPACE,
        )


def test_sign_cli_emits_exact_known_signature_without_overwrite(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    signature = tmp_path / "receipt.json.sig"
    receipt.write_bytes(_known_signed_receipt())
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "sign",
            str(receipt),
            "--identity-file",
            str(_known_identity_file(tmp_path)),
            "--principal",
            SIGNING_PRINCIPAL,
            "--namespace",
            SIGNING_NAMESPACE,
            "--output-signature",
            str(signature),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert _sha256(signature.read_bytes()) == KNOWN_SIGNATURE_DIGEST
    replay = subprocess.run(result.args, cwd=ROOT, check=False, capture_output=True)
    assert replay.returncode == 1
    assert b"output path must be empty" in replay.stderr
    assert _sha256(signature.read_bytes()) == KNOWN_SIGNATURE_DIGEST


def test_runtime_cli_validates_signature_and_never_echoes_secret(tmp_path: Path) -> None:
    scope_path = tmp_path / "scope.json"
    signature_path = tmp_path / "scope.sig"
    identity_path = tmp_path / "identity.json"
    probes_path = tmp_path / "probes.json"
    owner_keys_path = tmp_path / "owner-keys.json"
    output_path = tmp_path / "verification.json"
    token = b"do-not-echo-this-runtime-secret"
    scope = _scope_authority(token=token)
    scope_path.write_bytes(_canonical(scope))
    signature_path.write_bytes(b"invalid signature bytes\n")
    identity_path.write_bytes(_principal())
    probes_path.write_bytes(_endpoint_probes(scope))
    owner_keys_path.write_bytes(
        _owner_signing_keys_observation(
            captured_at=datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "verify-runtime-credential",
            "--scope-authority",
            str(scope_path),
            "--scope-authority-signature",
            str(signature_path),
            "--owner-signing-keys-observation",
            str(owner_keys_path),
            "--identity-probe-observation",
            str(identity_path),
            "--endpoint-probe-observations",
            str(probes_path),
            "--output",
            str(output_path),
        ],
        cwd=ROOT,
        input=token,
        check=False,
        capture_output=True,
    )

    assert result.returncode == 1
    assert b"signature" in result.stderr.lower()
    assert token not in result.stdout
    assert token not in result.stderr
    assert not output_path.exists()


def test_runtime_credential_rejects_self_certified_attacker_scope(
    tmp_path: Path,
) -> None:
    """A signature's embedded key cannot establish owner authority."""

    token = b"high-entropy-test-token-1234567890"
    attacker_identity, attacker_fingerprint = _attacker_identity_file(tmp_path)
    context = json.loads(_scope_context())
    context["signing_key_fingerprint"] = attacker_fingerprint
    scope = subject.create_credential_scope_authority(
        purpose="hosted_smoke_read",
        credential_id="credential-hosted-smoke-read",
        principal_observation=_principal(),
        grants_snapshot=_grants_snapshot(),
        token_fingerprint=_sha256(token),
        controller_context=_canonical(context),
    )
    scope_bytes = _canonical(scope)
    attacker_signature = subject.sign_receipt_detached(
        receipt=scope_bytes,
        identity_file=attacker_identity,
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )

    with pytest.raises(ValueError, match="fingerprint|owner|registered"):
        subject.verify_runtime_credential(
            scope_authority=scope_bytes,
            scope_authority_signature=attacker_signature,
            owner_signing_keys_observation=_owner_signing_keys_observation(),
            identity_probe=_principal(),
            endpoint_probe_observations=_endpoint_probes(scope),
            token_bytes=token,
            _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
        )


def test_runtime_cli_requires_an_owner_signing_key_observation() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "verify-runtime-credential", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--owner-signing-keys-observation" in result.stdout


# ---------------------------------------------------------------------------
# One-wire dispatch preparation, containment, and reconciliation
# ---------------------------------------------------------------------------


DISPATCH_NONCE = "01" * 32
DISPATCH_SOURCE_SHA = "a" * 40
DISPATCH_REQUEST_DIGEST = "sha256:" + "1" * 64
DISPATCH_TOKEN_FINGERPRINT = "sha256:" + "d" * 64


def _dispatch_components() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, str],
]:
    repository = {"full_name": "John-MiracleWorker/Kestrel", "id": 303}
    workflow = {
        "id": 707,
        "path": ".github/workflows/release.yml",
        "state": "active",
        "default_branch_sha": DISPATCH_SOURCE_SHA,
        "observation_digest": "sha256:" + "2" * 64,
    }
    target = {
        "mode": "initiate",
        "short_ref": "main",
        "full_ref": "refs/heads/main",
        "head_sha": DISPATCH_SOURCE_SHA,
        "workflow_ref": (
            "John-MiracleWorker/Kestrel/.github/workflows/release.yml@refs/heads/main"
        ),
        "workflow_sha": DISPATCH_SOURCE_SHA,
    }
    actor = {
        "login": "kestrel-release-dispatcher[bot]",
        "id": 808,
        "app_id": 909,
        "installation_id": 1001,
    }
    inputs = {
        "candidate_run_id": "101",
        "candidate_manifest_digest": "sha256:" + "4" * 64,
        "mode": "initiate",
    }
    return repository, workflow, target, actor, inputs


def _prepared_dispatch() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    repository, workflow, target, actor, inputs = _dispatch_components()
    return subject.prepare_dispatch_records(
        repository=repository,
        workflow=workflow,
        target=target,
        actor=actor,
        inputs=inputs,
        _nonce_source=lambda count: bytes.fromhex(DISPATCH_NONCE),
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )


def test_prepare_dispatch_records_freezes_nonce_binding_and_exact_request() -> None:
    journal, intent, request = _prepared_dispatch()
    expected_without_binding = {
        "candidate_run_id": "101",
        "candidate_manifest_digest": "sha256:" + "4" * 64,
        "mode": "initiate",
        "transaction_nonce": DISPATCH_NONCE,
    }
    expected_binding = _sha256(_canonical({"inputs": expected_without_binding, "ref": "main"}))
    expected_inputs = dict(expected_without_binding, dispatch_binding=expected_binding)

    assert request == {"ref": "main", "inputs": expected_inputs}
    assert journal["schema"] == "kestrel.release_dispatch_transaction.v1"
    assert journal["state"] == "prepared"
    assert journal["transaction_nonce"] == DISPATCH_NONCE
    assert journal["logical_dispatch_ordinal"] == 1
    assert journal["api_version"] == "2026-03-10"
    assert journal["method"] == "POST"
    assert journal["response_contract"] == "api-2026-03-10-always-run-details"
    assert journal["dispatch_binding"] == expected_binding
    assert journal["canonical_request_sha256"] == _sha256(_canonical(request))
    assert journal["send_started_at"] is None
    assert journal["monotonic_deadline_seconds"] == 700
    assert set(request) == {"ref", "inputs"}
    assert "return_run_details" not in _canonical(request).decode()
    assert intent["schema"] == "kestrel.release_dispatch_intent.v2"
    assert intent["request_digest"] == journal["canonical_request_sha256"]
    assert intent["transaction_digest"] == _sha256(_canonical(journal))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("nonce-short", "32"),
        ("nonce-long", "32"),
        ("workflow-inactive", "active"),
        ("head-mismatch", "candidate"),
        ("ref-mismatch", "ref"),
        ("extra-input", "input"),
        ("bad-candidate-run", "candidate run"),
        ("mode-mismatch", "mode"),
    ],
)
def test_prepare_dispatch_records_rejects_envelope_mutants(mutation: str, message: str) -> None:
    repository, workflow, target, actor, inputs = _dispatch_components()
    nonce = bytes.fromhex(DISPATCH_NONCE)
    if mutation == "nonce-short":
        nonce = nonce[:-1]
    elif mutation == "nonce-long":
        nonce += b"x"
    elif mutation == "workflow-inactive":
        workflow["state"] = "disabled_manually"
    elif mutation == "head-mismatch":
        target["workflow_sha"] = "b" * 40
    elif mutation == "ref-mismatch":
        target["full_ref"] = "refs/heads/other"
    elif mutation == "extra-input":
        inputs["unexpected"] = "value"
    elif mutation == "bad-candidate-run":
        inputs["candidate_run_id"] = "0101"
    elif mutation == "mode-mismatch":
        inputs["mode"] = "recover_committed"
    else:  # pragma: no cover
        raise AssertionError(mutation)

    with pytest.raises(ValueError, match=message):
        subject.prepare_dispatch_records(
            repository=repository,
            workflow=workflow,
            target=target,
            actor=actor,
            inputs=inputs,
            _nonce_source=lambda count: nonce,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
            _monotonic=lambda: 100.0,
        )


def _response_body(run_id: int = 1101) -> bytes:
    return _canonical(
        {
            "workflow_run_id": run_id,
            "run_url": (
                f"https://api.github.com/repos/John-MiracleWorker/Kestrel/actions/runs/{run_id}"
            ),
            "html_url": (f"https://github.com/John-MiracleWorker/Kestrel/actions/runs/{run_id}"),
        }
    )


@pytest.mark.parametrize(
    ("status", "body", "prewrite", "expected"),
    [
        (200, _response_body(), False, "response_details_received"),
        (204, b"", False, "outcome_unknown"),
        (200, b"{}", False, "outcome_unknown"),
        (502, b'{"message":"gateway"}', False, "outcome_unknown"),
        (422, b'{"message":"invalid ref"}', False, "not_accepted"),
        (None, None, True, "not_accepted"),
        (None, None, False, "outcome_unknown"),
    ],
)
def test_classify_dispatch_transport_is_fail_closed(
    status: int | None,
    body: bytes | None,
    prewrite: bool,
    expected: str,
) -> None:
    journal, _, _ = _prepared_dispatch()
    headers = _canonical(
        [
            ["content-type", "application/json"],
            ["x-github-request-id", "ABCD:1234:5678:9ABC"],
        ]
    )
    result = subject.classify_dispatch_transport(
        journal=journal,
        http_status=status,
        response_headers=headers if status else None,
        response_body=body,
        response_observed_at=(datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC) if status else None),
        locally_proven_prewrite_failure=prewrite,
    )
    assert result["classification"] == expected
    assert (
        result["returned_run"] is not None
        if expected == "response_details_received"
        else result["returned_run"] is None
    )


def test_dispatch_nonacceptance_requires_authenticated_github_error_proof() -> None:
    journal, _, _ = _prepared_dispatch()

    result = subject.classify_dispatch_transport(
        journal=journal,
        http_status=422,
        response_headers=_canonical([["content-type", "application/json"]]),
        response_body=b'{"message":"invalid ref"}',
        response_observed_at=datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
        locally_proven_prewrite_failure=False,
    )

    assert result["classification"] == "outcome_unknown"


def test_dispatch_transport_rejects_returned_identity_confusion() -> None:
    journal, _, _ = _prepared_dispatch()
    mutant = json.loads(_response_body())
    mutant["run_url"] = mutant["run_url"].replace("1101", "1102")
    result = subject.classify_dispatch_transport(
        journal=journal,
        http_status=200,
        response_headers=b"{}",
        response_body=_canonical(mutant),
        response_observed_at=datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
        locally_proven_prewrite_failure=False,
    )
    assert result["classification"] == "outcome_unknown"
    assert result["returned_run"] is None


def test_dispatch_transport_accepts_semantically_valid_noncanonical_github_json() -> None:
    """Catch treating GitHub's JSON response bytes as a JCS-signed record."""

    journal, _, _ = _prepared_dispatch()
    response = (
        b'{ "run_url": "https://api.github.com/repos/John-MiracleWorker/Kestrel/'
        b'actions/runs/1101", "workflow_run_id": 1101, "html_url": '
        b'"https://github.com/John-MiracleWorker/Kestrel/actions/runs/1101" }'
    )

    result = subject.classify_dispatch_transport(
        journal=journal,
        http_status=200,
        response_headers=b'[["content-type","application/json"]]',
        response_body=response,
        response_observed_at=datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
        locally_proven_prewrite_failure=False,
    )

    assert result["classification"] == "response_details_received"
    assert result["returned_run"] == {
        "id": 1101,
        "run_url": ("https://api.github.com/repos/John-MiracleWorker/Kestrel/actions/runs/1101"),
        "html_url": ("https://github.com/John-MiracleWorker/Kestrel/actions/runs/1101"),
    }


@pytest.mark.parametrize("mutation", ["extra-secret", "redirected-endpoint"])
def test_dispatch_journal_rejects_injected_or_redirected_authority(mutation: str) -> None:
    """Catch a journal becoming authority for fields not derived by preparation."""

    journal, _, _ = _prepared_dispatch()
    if mutation == "extra-secret":
        journal["Authorization"] = "Bearer must-never-be-recorded"
    else:
        journal["endpoint"] = (
            "https://api.github.com/repos/attacker/other/actions/workflows/999/dispatches"
        )

    with pytest.raises(ValueError, match="schema|field|endpoint|repository|workflow"):
        subject._validate_dispatch_journal(journal)


def test_dispatch_protocol_rejects_retired_api_and_request_member() -> None:
    journal, _, _ = _prepared_dispatch()
    retired = json.loads(_canonical(journal))
    retired["api_version"] = "2022-11-28"
    with pytest.raises(ValueError, match="schema|API version"):
        subject.classify_dispatch_transport(
            journal=retired,
            http_status=200,
            response_headers=b"{}",
            response_body=_response_body(),
            response_observed_at=datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
            locally_proven_prewrite_failure=False,
        )
    request_member = json.loads(_canonical(journal))
    request_member["inputs"]["return_run_details"] = "true"
    with pytest.raises(ValueError, match="schema|input fields"):
        subject.classify_dispatch_transport(
            journal=request_member,
            http_status=204,
            response_headers=b"{}",
            response_body=b"",
            response_observed_at=datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
            locally_proven_prewrite_failure=False,
        )


def test_reconciliation_reader_policy_allows_the_exact_workflow_scoped_query() -> None:
    """Catch a credential policy that cannot execute the normative poll URI."""

    policy = json.loads((ROOT / "release-control-credential-policy.json").read_bytes())
    purpose = next(
        item for item in policy["purposes"] if item["purpose"] == "promotion_reconciliation_reader"
    )

    assert (
        "GET /repos/{repository}/actions/workflows/{workflow_id}/runs"
        in purpose["endpoint_allowlist"]
    )


def test_canonicalize_cli_rejects_an_existing_output_even_when_bytes_match(
    tmp_path: Path,
) -> None:
    """Catch CLI handlers silently accepting a non-empty EMPTY_PATH output."""

    source = tmp_path / "source.json"
    output = tmp_path / "output.json"
    source.write_bytes(b'{"value":1}')
    output.write_bytes(b'{"value":1}')

    assert subject.main(["canonicalize", str(source), "--output", str(output)]) == 1
    assert output.read_bytes() == b'{"value":1}'


def _containment_inputs(
    *,
    apps: list[dict[str, object]] | None = None,
    status: int = 401,
    token_fingerprint: str = DISPATCH_TOKEN_FINGERPRINT,
) -> tuple[bytes, bytes, bytes]:
    installed = _canonical(
        {
            "schema": "kestrel.installed_apps_snapshot.v1",
            "apps": [] if apps is None else apps,
            "captured_at": "2026-08-13T20:00:02Z",
            "complete": True,
        }
    )
    uninstall = _canonical(
        {
            "schema": "kestrel.dispatcher_uninstall_observation.v1",
            "app_id": 909,
            "installation_id": 1001,
            "uninstalled_at": "2026-08-13T20:00:01Z",
            "complete": True,
        }
    )
    probe = _canonical(
        {
            "schema": "kestrel.dispatcher_token_probe.v1",
            "endpoint": "GET /installation/repositories",
            "http_status": status,
            "observed_at": "2026-08-13T20:00:02Z",
            "response_sha256": "sha256:" + "5" * 64,
            "token_fingerprint": token_fingerprint,
            "complete": True,
        }
    )
    return installed, uninstall, probe


def _send_boundary(
    journal: dict[str, object],
    *,
    started_at: str = "2026-08-13T20:00:00Z",
    token_fingerprint: str = DISPATCH_TOKEN_FINGERPRINT,
) -> dict[str, object]:
    return {
        "schema": "kestrel.dispatch_send_boundary.v1",
        "state": "sending",
        "transaction_nonce": journal["transaction_nonce"],
        "journal_digest": _sha256(_canonical(journal)),
        "request_digest": journal["canonical_request_sha256"],
        "started_at": started_at,
        "token_fingerprint": token_fingerprint,
        "pre_send_writer_inventory_digest": _sha256(_repository_writer_inventory("pre_send")),
        "transport_policy": {
            "maximum_wire_transmissions": 1,
            "redirects": False,
            "retries": False,
            "auth_replay": False,
            "proxies": False,
            "failover": False,
        },
        "validation_status": "validated",
    }


def _containment() -> dict[str, object]:
    journal, _, _ = _prepared_dispatch()
    installed, uninstall, probe = _containment_inputs()
    return subject.create_dispatch_containment(
        journal=journal,
        dispatch=_transport(),
        send_boundary=_send_boundary(journal),
        installed_apps_snapshot=installed,
        uninstall_observation=uninstall,
        token_probe_observation=probe,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 2, tzinfo=UTC),
        **_signed_writer_inventory_arguments("post_containment", nonce_run_ids=[1101]),
    )


def test_dispatch_containment_requires_absent_app_and_invalid_exact_token() -> None:
    containment = _containment()
    assert containment["installation_id"] == 1001
    assert containment["validated"] is True
    assert containment["token_probe"]["http_status"] == 401  # type: ignore[index]
    assert (
        containment["pre_send_writer_inventory_digest"]
        == _send_boundary(  # type: ignore[index]
            _prepared_dispatch()[0]
        )["pre_send_writer_inventory_digest"]
    )
    assert containment["post_containment_writer_inventory_digest"] == _sha256(
        _repository_writer_inventory(
            "post_containment",
            captured_at="2026-08-13T20:00:02Z",
            nonce_run_ids=[1101],
        )
    )


@pytest.mark.parametrize(
    ("apps", "status", "message"),
    [
        ([{"app_id": 909, "installation_id": 1001}], 401, "installed"),
        ([], 200, "401"),
        ([], 403, "401"),
    ],
)
def test_dispatch_containment_rejects_incomplete_containment(
    apps: list[dict[str, object]], status: int, message: str
) -> None:
    journal, _, _ = _prepared_dispatch()
    installed, uninstall, probe = _containment_inputs(apps=apps, status=status)
    with pytest.raises(ValueError, match=message):
        subject.create_dispatch_containment(
            journal=journal,
            dispatch=_transport(),
            send_boundary=_send_boundary(journal),
            installed_apps_snapshot=installed,
            uninstall_observation=uninstall,
            token_probe_observation=probe,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 2, tzinfo=UTC),
            **_signed_writer_inventory_arguments("post_containment", nonce_run_ids=[1101]),
        )


@pytest.mark.parametrize("mutation", ["token", "pre-send", "stale", "writer-order"])
def test_dispatch_containment_rejects_unbound_or_misordered_proof(
    mutation: str,
) -> None:
    journal, _, _ = _prepared_dispatch()
    installed, uninstall, probe = _containment_inputs(
        token_fingerprint=(
            "sha256:" + "e" * 64 if mutation == "token" else DISPATCH_TOKEN_FINGERPRINT
        )
    )
    boundary = _send_boundary(
        journal,
        started_at=("2026-08-13T20:00:02Z" if mutation == "pre-send" else "2026-08-13T20:00:00Z"),
    )
    now = (
        datetime(2026, 8, 13, 20, 2, 3, tzinfo=UTC)
        if mutation == "stale"
        else datetime(2026, 8, 13, 20, 0, 2, tzinfo=UTC)
    )

    with pytest.raises(ValueError, match="token|ordering|send|stale"):
        subject.create_dispatch_containment(
            journal=journal,
            dispatch=_transport(),
            send_boundary=boundary,
            installed_apps_snapshot=installed,
            uninstall_observation=uninstall,
            token_probe_observation=probe,
            _clock=lambda: now,
            **_signed_writer_inventory_arguments(
                "post_containment",
                nonce_run_ids=[1101],
                captured_at=("2026-08-13T19:59:59Z" if mutation == "writer-order" else None),
            ),
        )


def test_reconciliation_revalidates_containment_order() -> None:
    journal, _, _ = _prepared_dispatch()
    containment = _containment()
    containment["uninstalled_at"] = "2026-08-13T19:59:59Z"

    with pytest.raises(ValueError, match="containment|ordering|precedes"):
        subject.reconcile_dispatch(
            journal=journal,
            dispatch=_transport(),
            containment=containment,
            polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
            candidates=[_candidate()],
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
        )


def _dispatch_identity(run_id: int = 1101, **overrides: object) -> dict[str, object]:
    journal, _, _ = _prepared_dispatch()
    value: dict[str, object] = {
        "schema": "kestrel.dispatch_identity.v1",
        "transaction_nonce": journal["transaction_nonce"],
        "dispatch_binding": journal["dispatch_binding"],
        "dispatch_inputs_digest": _sha256(_canonical(journal["inputs"])),
        "repository": "John-MiracleWorker/Kestrel",
        "repository_id": 303,
        "workflow": "Release",
        "workflow_ref": journal["target"]["workflow_ref"],  # type: ignore[index]
        "workflow_sha": DISPATCH_SOURCE_SHA,
        "event_name": "workflow_dispatch",
        "ref": "refs/heads/main",
        "sha": DISPATCH_SOURCE_SHA,
        "run_id": run_id,
        "run_attempt": 1,
        "actor": "kestrel-release-dispatcher[bot]",
        "actor_id": 808,
        "triggering_actor": "kestrel-release-dispatcher[bot]",
        "observed_at": "2026-08-13T20:00:03Z",
        "evidence": {
            "source_bundle_digest": "sha256:" + "6" * 64,
            "canonicalization_vector_digest": CANONICALIZATION_DIGEST,
        },
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "github.com",
            "method": "github-context-allowlist",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    value.update(overrides)
    return value


def _github_context_allowlist(**overrides: object) -> dict[str, object]:
    journal, _, _ = _prepared_dispatch()
    value: dict[str, object] = {
        "schema": "kestrel.github_context_allowlist.v1",
        "event_inputs": journal["inputs"],
        "repository": "John-MiracleWorker/Kestrel",
        "repository_id": 303,
        "workflow": "Release",
        "workflow_ref": journal["target"]["workflow_ref"],  # type: ignore[index]
        "workflow_sha": DISPATCH_SOURCE_SHA,
        "event_name": "workflow_dispatch",
        "ref": "refs/heads/main",
        "ref_name": "main",
        "sha": DISPATCH_SOURCE_SHA,
        "run_id": 1101,
        "run_attempt": 1,
        "actor": "kestrel-release-dispatcher[bot]",
        "actor_id": 808,
        "triggering_actor": "kestrel-release-dispatcher[bot]",
    }
    value.update(overrides)
    return value


def test_create_dispatch_identity_uses_only_allowlisted_context_and_clock() -> None:
    context = _github_context_allowlist()
    identity = subject.create_dispatch_identity(
        github_context_allowlist=context,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 3, tzinfo=UTC),
    )
    expected = _dispatch_identity()
    expected["evidence"]["source_bundle_digest"] = subject.source_bundle_digest(  # type: ignore[index]
        {"github-context-allowlist": _canonical(context)}
    )
    assert identity == expected


def test_create_dispatch_identity_cli_exposes_no_identity_or_time_scalars(
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "github-context-allowlist.json"
    output_path = tmp_path / "dispatch-identity.json"
    context_path.write_bytes(_canonical(_github_context_allowlist()))

    help_result = subprocess.run(
        [sys.executable, str(SCRIPT), "create-dispatch-identity", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    for forbidden in (
        "--observed-at",
        "--run-id",
        "--actor",
        "--ref",
        "--sha",
        "--nonce",
        "--binding",
    ):
        assert forbidden not in help_result.stdout

    assert (
        subject.main(
            [
                "create-dispatch-identity",
                "--github-context-allowlist",
                str(context_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    identity = json.loads(output_path.read_bytes())
    assert identity["schema"] == "kestrel.dispatch_identity.v1"
    assert identity["run_id"] == 1101
    assert identity["validation_status"] == "validated"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("token", "field"),
        ("binding", "binding"),
        ("nonce", "nonce"),
        ("attempt", "attempt"),
        ("event", "event"),
        ("ref-name", "binding"),
        ("actor", "actor"),
    ],
)
def test_create_dispatch_identity_rejects_context_mutants(mutation: str, message: str) -> None:
    context = _github_context_allowlist()
    if mutation == "token":
        context["token"] = "must-never-enter-allowlist"
    elif mutation == "binding":
        context["event_inputs"]["dispatch_binding"] = "sha256:" + "0" * 64  # type: ignore[index]
    elif mutation == "nonce":
        context["event_inputs"]["transaction_nonce"] = "0" * 63  # type: ignore[index]
    elif mutation == "attempt":
        context["run_attempt"] = 2
    elif mutation == "event":
        context["event_name"] = "push"
    elif mutation == "ref-name":
        context["ref_name"] = "other"
    elif mutation == "actor":
        context["actor_id"] = 0
    else:  # pragma: no cover
        raise AssertionError(mutation)
    with pytest.raises(ValueError, match=message):
        subject.create_dispatch_identity(
            github_context_allowlist=context,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 3, tzinfo=UTC),
        )


def _candidate(run_id: int = 1101, **overrides: object) -> dict[str, object]:
    journal, _, _ = _prepared_dispatch()
    run: dict[str, object] = {
        "workflow_id": 707,
        "repository_id": 303,
        "repository_full_name": "John-MiracleWorker/Kestrel",
        "path": ".github/workflows/release.yml@main",
        "event": "workflow_dispatch",
        "display_title": (
            f"Kestrel release tx {DISPATCH_NONCE} bind {journal['dispatch_binding']}"
        ),
        "head_branch": "main",
        "head_sha": DISPATCH_SOURCE_SHA,
        "run_attempt": 1,
        "actor_login": "kestrel-release-dispatcher[bot]",
        "actor_id": 808,
        "triggering_actor_login": "kestrel-release-dispatcher[bot]",
        "triggering_actor_id": 808,
        "status": "waiting",
        "conclusion": None,
    }
    identity = _dispatch_identity(run_id)
    value: dict[str, object] = {
        "run_id": run_id,
        "list_observation_sha256": "sha256:" + "7" * 64,
        "get_run_observation_sha256": "sha256:" + "8" * 64,
        "run": run,
        "identity_artifact": {
            "artifact_id": 1201,
            "name": f"kestrel-dispatch-identity-{run_id}-1",
            "api_digest": "sha256:" + "9" * 64,
            "archive_sha256": "sha256:" + "9" * 64,
            "content_sha256": _sha256(_canonical(identity)),
            "expired": False,
            "matching_name_count": 1,
            "identity": identity,
        },
    }
    value.update(overrides)
    return value


def _poll(ordinal: int, run_ids: list[int], *, complete: bool = True) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "requested_at": f"2026-08-13T20:00:{3 + (ordinal - 1) * 5:02d}Z",
        "workflow_observation_sha256": "sha256:" + f"{ordinal:x}" * 64,
        "query": (
            "GET /repos/John-MiracleWorker/Kestrel/actions/workflows/707/"
            "runs?event=workflow_dispatch&per_page=100"
        ),
        "pages": [
            {
                "number": 1,
                "http_status": 200,
                "response_sha256": "sha256:" + f"{ordinal:x}" * 64,
                "next": None,
            }
        ],
        "complete": complete,
        "result_count": len(run_ids),
        "nonce_run_ids": run_ids,
        "binding_conflict_run_ids": [],
        "rejection_reasons": [],
    }


def _poll_at(
    ordinal: int,
    offset_seconds: int,
    run_ids: list[int],
    *,
    complete: bool = True,
    pages: list[dict[str, object]] | None = None,
    rejection_reasons: list[str] | None = None,
) -> dict[str, object]:
    poll = _poll(1, run_ids, complete=complete)
    poll["ordinal"] = ordinal
    observed = datetime(2026, 8, 13, 20, 0, 3, tzinfo=UTC) + timedelta(seconds=offset_seconds)
    poll["requested_at"] = observed.strftime("%Y-%m-%dT%H:%M:%SZ")
    poll["workflow_observation_sha256"] = "sha256:" + f"{ordinal:064x}"
    if pages is not None:
        poll["pages"] = pages
    if rejection_reasons is not None:
        poll["rejection_reasons"] = rejection_reasons
    return poll


def _transport(classification: str = "outcome_unknown") -> dict[str, object]:
    journal, _, _ = _prepared_dispatch()
    if classification == "response_details_received":
        return subject.classify_dispatch_transport(
            journal=journal,
            http_status=200,
            response_headers=b"{}",
            response_body=_response_body(),
            response_observed_at=datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
            locally_proven_prewrite_failure=False,
        )
    return subject.classify_dispatch_transport(
        journal=journal,
        http_status=None,
        response_headers=None,
        response_body=None,
        response_observed_at=None,
        locally_proven_prewrite_failure=False,
    )


def test_reconciliation_adopts_only_three_stable_exact_polls() -> None:
    journal, _, _ = _prepared_dispatch()
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport("response_details_received"),
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[_candidate()],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )

    assert reconciliation["schema"] == "kestrel.release_dispatch_reconciliation.v1"
    assert reconciliation["outcome"] == {
        "state": "run_adopted",
        "cardinality": 1,
        "adopted_run_id": 1101,
        "reason_code": "exact_singleton_attempt_1",
        "decided_at": "2026-08-13T20:00:13Z",
    }
    assert reconciliation["tombstone"] is None
    assert tombstone is None
    first_page = reconciliation["polling"]["polls"][0]["pages"][0]  # type: ignore[index]
    first_page_bytes = _canonical(first_page)
    assert reconciliation["evidence"][0]["sha256"] == _sha256(first_page_bytes)  # type: ignore[index]
    assert reconciliation["evidence"][0]["size_bytes"] == len(first_page_bytes)  # type: ignore[index]


def test_reconciliation_rejects_poll_query_outside_the_signed_transaction() -> None:
    journal, _, _ = _prepared_dispatch()
    polls = [_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])]
    for poll in polls:
        poll["query"] = (
            "GET /repos/attacker/other/actions/workflows/999/"
            "runs?event=workflow_dispatch&per_page=100"
        )

    with pytest.raises(ValueError, match="query|repository|workflow"):
        subject.reconcile_dispatch(
            journal=journal,
            dispatch=_transport(),
            containment=_containment(),
            polls=polls,
            candidates=[_candidate()],
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
        )


def test_reconciliation_rejects_returned_run_urls_outside_the_transaction() -> None:
    journal, _, _ = _prepared_dispatch()
    dispatch = _transport("response_details_received")
    dispatch["returned_run"]["run_url"] = (  # type: ignore[index]
        "https://api.github.com/repos/attacker/other/actions/runs/1101"
    )
    dispatch["returned_run"]["html_url"] = (  # type: ignore[index]
        "https://github.com/attacker/other/actions/runs/1101"
    )

    with pytest.raises(ValueError, match="returned|URL|repository"):
        subject.reconcile_dispatch(
            journal=journal,
            dispatch=dispatch,
            containment=_containment(),
            polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
            candidates=[_candidate()],
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "mutation",
    ["missing-response-time", "missing-details-body", "status-without-response-time"],
)
def test_reconciliation_rejects_transport_state_contradictions(
    mutation: str,
) -> None:
    journal, _, _ = _prepared_dispatch()
    dispatch = _transport(
        "outcome_unknown"
        if mutation == "status-without-response-time"
        else "response_details_received"
    )
    if mutation == "missing-response-time":
        dispatch["response_observed_at"] = None
    elif mutation == "missing-details-body":
        dispatch["response_body_sha256"] = None
    else:
        dispatch["http_status"] = 500

    with pytest.raises(ValueError, match="response|transport|HTTP evidence"):
        subject.reconcile_dispatch(
            journal=journal,
            dispatch=dispatch,
            containment=_containment(),
            polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
            candidates=[_candidate()],
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
        )


@pytest.mark.parametrize("scenario", ["zero", "single"])
def test_reconciliation_does_not_terminalize_zero_or_single_before_deadline(
    scenario: str,
) -> None:
    """A restart or early caller observation cannot shorten the fixed window."""

    journal, _, _ = _prepared_dispatch()
    run_ids = [] if scenario == "zero" else [1101]
    candidates = [] if scenario == "zero" else [_candidate()]

    with pytest.raises(ValueError, match="pending.*deadline"):
        subject.reconcile_dispatch(
            journal=journal,
            dispatch=_transport(),
            containment=_containment(),
            polls=[_poll_at(1, 0, run_ids)],
            candidates=candidates,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 3, tzinfo=UTC),
        )


def test_definite_nonacceptance_consumes_nonce_with_terminal_tombstone() -> None:
    journal, _, _ = _prepared_dispatch()
    dispatch = subject.classify_dispatch_transport(
        journal=journal,
        http_status=422,
        response_headers=_canonical(
            [
                ["content-type", "application/json"],
                ["x-github-request-id", "ABCD:1234:5678:9ABC"],
            ]
        ),
        response_body=b'{"message":"invalid ref"}',
        response_observed_at=datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
        locally_proven_prewrite_failure=False,
        send_started_at=datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
    )

    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=dispatch,
        containment=_containment(),
        polls=[],
        candidates=[],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 3, tzinfo=UTC),
    )

    assert reconciliation["outcome"]["state"] == "unresolved_zero"  # type: ignore[index]
    assert reconciliation["outcome"]["reason_code"] == "dispatch_not_accepted"  # type: ignore[index]
    assert tombstone is not None
    assert tombstone["prohibition"] == "never_issue_dispatch_admission"


def test_response_lost_adopts_same_exact_run_without_redispatch() -> None:
    journal, _, _ = _prepared_dispatch()
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport("outcome_unknown"),
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[_candidate()],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "run_adopted"  # type: ignore[index]
    assert reconciliation["dispatch"]["returned_run"] is None  # type: ignore[index]
    assert tombstone is None


@pytest.mark.parametrize(
    "title",
    [
        " Kestrel release tx {nonce} bind {binding}",
        "Kestrel release tx {nonce} bind {binding} ",
        "kestrel release tx {nonce} bind {binding}",
        "Kestrel release tx {nonce}\N{NO-BREAK SPACE}bind {binding}",
        "Kestrel  release tx {nonce} bind {binding}",
    ],
)
def test_title_variations_are_never_adopted(title: str) -> None:
    journal, _, _ = _prepared_dispatch()
    candidate = _candidate()
    candidate["run"]["display_title"] = title.format(  # type: ignore[index]
        nonce=DISPATCH_NONCE,
        binding=journal["dispatch_binding"],
    )
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[candidate],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "unsafe_orphan_or_tamper"  # type: ignore[index]
    assert tombstone is not None


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("waiting", None),
        ("completed", "success"),
        ("completed", "cancelled"),
        ("completed", "failure"),
        ("completed", "timed_out"),
    ],
)
def test_run_status_and_conclusion_never_hide_exact_candidate(
    status: str, conclusion: str | None
) -> None:
    journal, _, _ = _prepared_dispatch()
    candidate = _candidate()
    candidate["run"]["status"] = status  # type: ignore[index]
    candidate["run"]["conclusion"] = conclusion  # type: ignore[index]
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[candidate],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "run_adopted"  # type: ignore[index]
    assert tombstone is None


def test_exhaustive_multi_page_poll_can_adopt_exact_singleton() -> None:
    journal, _, _ = _prepared_dispatch()
    pages = [
        {
            "number": 1,
            "http_status": 200,
            "response_sha256": "sha256:" + "a" * 64,
            "next": 2,
        },
        {
            "number": 2,
            "http_status": 200,
            "response_sha256": "sha256:" + "b" * 64,
            "next": None,
        },
    ]
    polls = [_poll_at(ordinal, (ordinal - 1) * 5, [1101], pages=pages) for ordinal in range(1, 4)]
    for poll in polls:
        poll["result_count"] = 101
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=polls,
        candidates=[_candidate()],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "run_adopted"  # type: ignore[index]
    assert len(reconciliation["evidence"]) == 6
    assert tombstone is None


@pytest.mark.parametrize(
    "reason",
    ["pagination_incomplete", "pagination_inconsistent", "rate_limited"],
)
def test_incomplete_pagination_is_unavailable_never_zero(reason: str) -> None:
    journal, _, _ = _prepared_dispatch()
    poll = _poll_at(1, 0, [], complete=False, rejection_reasons=[reason])
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=[poll],
        candidates=[],
        _clock=lambda: datetime(2026, 8, 13, 20, 10, 3, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "reconciliation_unavailable"  # type: ignore[index]
    assert reconciliation["outcome"]["reason_code"] == "observation_incomplete"  # type: ignore[index]
    assert tombstone is not None


@pytest.mark.parametrize(
    ("first_visible_offset", "expected"),
    [
        (590, "run_adopted"),
        (595, "unresolved_single_unproven"),
        (600, "unresolved_single_unproven"),
    ],
)
def test_deadline_quiescence_requires_three_complete_polls(
    first_visible_offset: int, expected: str
) -> None:
    journal, _, _ = _prepared_dispatch()
    polls = [
        _poll_at(
            ordinal,
            offset,
            [1101] if offset >= first_visible_offset else [],
        )
        for ordinal, offset in enumerate(range(0, 601, 5), start=1)
    ]
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=polls,
        candidates=[_candidate()],
        _clock=lambda: datetime(2026, 8, 13, 20, 10, 3, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == expected  # type: ignore[index]
    assert (tombstone is None) is (expected == "run_adopted")


def test_disappearing_nonce_run_makes_reconciliation_unavailable() -> None:
    journal, _, _ = _prepared_dispatch()
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=[
            _poll_at(1, 0, [1101]),
            _poll_at(2, 5, []),
            _poll_at(3, 10, [1101]),
        ],
        candidates=[_candidate()],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "reconciliation_unavailable"  # type: ignore[index]
    assert "nonce_run_disappeared" in reconciliation["polling"]["polls"][1]["rejection_reasons"]  # type: ignore[index]
    assert tombstone is not None


def test_malformed_nested_identity_evidence_never_authorizes() -> None:
    journal, _, _ = _prepared_dispatch()
    candidate = _candidate()
    identity = candidate["identity_artifact"]["identity"]  # type: ignore[index]
    identity["evidence"] = "not-an-object"  # type: ignore[index]
    candidate["identity_artifact"]["content_sha256"] = _sha256(  # type: ignore[index]
        _canonical(identity)
    )
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[candidate],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "unsafe_orphan_or_tamper"  # type: ignore[index]
    assert "identity_schema_mismatch" in reconciliation["candidates"][0]["reasons"]  # type: ignore[index]
    assert tombstone is not None


def test_malformed_workflow_path_is_tombstoned_not_crashed() -> None:
    journal, _, _ = _prepared_dispatch()
    candidate = _candidate()
    candidate["run"]["path"] = ".github/workflows/release.yml"  # type: ignore[index]
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[candidate],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "unsafe_orphan_or_tamper"  # type: ignore[index]
    assert "run_workflow_path_mismatch" in reconciliation["candidates"][0]["reasons"]  # type: ignore[index]
    assert tombstone is not None


@pytest.mark.parametrize(
    ("scenario", "expected", "reason"),
    [
        ("zero", "unresolved_zero", "dispatch_not_observed"),
        ("unproven", "unresolved_single_unproven", "quiescence_not_proven"),
        ("duplicate", "duplicate_dispatch_detected", "dispatch_ambiguous"),
        ("incomplete", "reconciliation_unavailable", "observation_incomplete"),
    ],
)
def test_reconciliation_terminal_failures_always_create_tombstone(
    scenario: str,
    expected: str,
    reason: str,
) -> None:
    journal, _, _ = _prepared_dispatch()
    if scenario == "zero":
        polls = [_poll(1, []), _poll(2, []), _poll(3, [])]
        candidates: list[dict[str, object]] = []
    elif scenario == "unproven":
        polls = [_poll(1, [1101]), _poll(2, [1101])]
        candidates = [_candidate()]
    elif scenario == "duplicate":
        polls = [_poll(1, [1101, 1102])]
        candidates = [_candidate(), _candidate(1102)]
    elif scenario == "incomplete":
        polls = [_poll(1, [], complete=False)]
        candidates = []
    else:  # pragma: no cover
        raise AssertionError(scenario)
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=polls,
        candidates=candidates,
        _clock=lambda: datetime(2026, 8, 13, 20, 10, 3, tzinfo=UTC),
    )

    assert reconciliation["outcome"]["state"] == expected  # type: ignore[index]
    assert reconciliation["outcome"]["reason_code"] == reason  # type: ignore[index]
    assert tombstone is not None
    assert tombstone["schema"] == "kestrel.dispatch_tombstone.v1"
    assert tombstone["prohibition"] == "never_issue_dispatch_admission"
    assert reconciliation["tombstone"]["canonical_sha256"] == _sha256(  # type: ignore[index]
        _canonical(tombstone)
    )


def test_reconciliation_detects_binding_conflict_before_candidate_selection() -> None:
    journal, _, _ = _prepared_dispatch()
    poll = _poll(1, [1101])
    poll["binding_conflict_run_ids"] = [1102]
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=[poll],
        candidates=[_candidate()],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 3, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "nonce_binding_conflict"  # type: ignore[index]
    assert tombstone is not None


def test_returned_run_plus_later_second_nonce_run_is_duplicate_terminal() -> None:
    journal, _, _ = _prepared_dispatch()
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport("response_details_received"),
        containment=_containment(),
        polls=[
            _poll(1, [1101]),
            _poll(2, [1101]),
            _poll(3, [1101, 1102]),
        ],
        candidates=[_candidate(), _candidate(1102)],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "duplicate_dispatch_detected"  # type: ignore[index]
    assert reconciliation["outcome"]["cardinality"] == 2  # type: ignore[index]
    assert tombstone is not None


def test_unrelated_concurrent_run_is_counted_but_never_nonce_selected() -> None:
    journal, _, _ = _prepared_dispatch()
    polls = [_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])]
    for poll in polls:
        poll["result_count"] = 2
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=polls,
        candidates=[_candidate()],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "run_adopted"  # type: ignore[index]
    assert reconciliation["outcome"]["cardinality"] == 1  # type: ignore[index]
    assert tombstone is None


@pytest.mark.parametrize("mutation", ["result-ceiling", "missing-page", "rate-limit"])
def test_invalid_pagination_can_never_be_misclassified_as_zero(mutation: str) -> None:
    journal, _, _ = _prepared_dispatch()
    poll = _poll(1, [])
    if mutation == "result-ceiling":
        poll["result_count"] = 1000
    elif mutation == "missing-page":
        poll["pages"][0]["next"] = 2  # type: ignore[index]
    elif mutation == "rate-limit":
        poll["pages"][0]["http_status"] = 429  # type: ignore[index]
    with pytest.raises(ValueError, match="1,000|pagination|incomplete"):
        subject.reconcile_dispatch(
            journal=journal,
            dispatch=_transport(),
            containment=_containment(),
            polls=[poll],
            candidates=[],
            _clock=lambda: datetime(2026, 8, 13, 20, 10, 3, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("attempt", "attempt"),
        ("actor", "actor"),
        ("workflow-sha", "workflow"),
        ("artifact", "artifact"),
        ("response-id", "response"),
    ],
)
def test_reconciliation_rejects_candidate_identity_mutants(mutation: str, message: str) -> None:
    journal, _, _ = _prepared_dispatch()
    candidate = _candidate()
    dispatch = _transport("response_details_received")
    if mutation == "attempt":
        candidate["run"]["run_attempt"] = 2  # type: ignore[index]
    elif mutation == "actor":
        candidate["run"]["actor_id"] = 999  # type: ignore[index]
    elif mutation == "workflow-sha":
        candidate["identity_artifact"]["identity"]["workflow_sha"] = "b" * 40  # type: ignore[index]
    elif mutation == "artifact":
        candidate["identity_artifact"]["archive_sha256"] = "sha256:" + "0" * 64  # type: ignore[index]
    elif mutation == "response-id":
        dispatch["returned_run"] = {  # type: ignore[index]
            "id": 1102,
            "run_url": (
                "https://api.github.com/repos/John-MiracleWorker/Kestrel/actions/runs/1102"
            ),
            "html_url": ("https://github.com/John-MiracleWorker/Kestrel/actions/runs/1102"),
        }
    else:  # pragma: no cover
        raise AssertionError(mutation)

    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=dispatch,
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[candidate],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] in {  # type: ignore[index]
        "response_identity_conflict",
        "unsafe_orphan_or_tamper",
    }
    assert (
        message in " ".join(reconciliation["candidates"][0]["reasons"])
        or message in reconciliation["outcome"]["reason_code"]
    )  # type: ignore[index]
    assert tombstone is not None


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("run-repository", "run_repository_id_mismatch"),
        ("run-workflow", "run_workflow_id_mismatch"),
        ("run-path", "run_workflow_ref_suffix_mismatch"),
        ("run-event", "run_event_mismatch"),
        ("run-title", "run_display_title_mismatch"),
        ("run-branch", "run_head_branch_mismatch"),
        ("run-sha", "run_head_sha_mismatch"),
        ("run-actor-login", "run_actor_login_mismatch"),
        ("run-triggering-id", "run_triggering_actor_id_mismatch"),
        ("identity-workflow", "identity_workflow_mismatch"),
        ("identity-ref", "identity_ref_mismatch"),
        ("identity-workflow-ref", "identity_workflow_ref_mismatch"),
        ("identity-workflow-sha", "identity_workflow_sha_mismatch"),
        ("identity-nonce", "identity_transaction_nonce_mismatch"),
        ("identity-run", "identity_run_id_mismatch"),
        ("identity-attempt", "identity_run_attempt_mismatch"),
        ("artifact-expired", "identity_artifact_expired"),
        ("artifact-name", "identity_artifact_name_mismatch"),
        ("artifact-duplicate", "identity_artifact_name_cardinality_mismatch"),
        ("artifact-api-digest", "identity_artifact_archive_digest_mismatch"),
        ("artifact-content", "identity_artifact_content_digest_mismatch"),
    ],
)
def test_exact_candidate_predicate_rejects_each_identity_join_mutant(
    mutation: str, expected_reason: str
) -> None:
    journal, _, _ = _prepared_dispatch()
    candidate = _candidate()
    run = candidate["run"]  # type: ignore[assignment]
    artifact = candidate["identity_artifact"]  # type: ignore[assignment]
    identity = artifact["identity"]  # type: ignore[index,assignment]
    identity_changed = False
    if mutation == "run-repository":
        run["repository_id"] = 304  # type: ignore[index]
    elif mutation == "run-workflow":
        run["workflow_id"] = 708  # type: ignore[index]
    elif mutation == "run-path":
        run["path"] = ".github/workflows/release.yml@other"  # type: ignore[index]
    elif mutation == "run-event":
        run["event"] = "push"  # type: ignore[index]
    elif mutation == "run-title":
        run["display_title"] = f"{run['display_title']} extra"  # type: ignore[index]
    elif mutation == "run-branch":
        run["head_branch"] = "other"  # type: ignore[index]
    elif mutation == "run-sha":
        run["head_sha"] = "b" * 40  # type: ignore[index]
    elif mutation == "run-actor-login":
        run["actor_login"] = "owner"  # type: ignore[index]
    elif mutation == "run-triggering-id":
        run["triggering_actor_id"] = 999  # type: ignore[index]
    elif mutation == "identity-workflow":
        identity["workflow"] = "Other"  # type: ignore[index]
        identity_changed = True
    elif mutation == "identity-ref":
        identity["ref"] = "refs/tags/v0.6.0"  # type: ignore[index]
        identity_changed = True
    elif mutation == "identity-workflow-ref":
        identity["workflow_ref"] = (
            "John-MiracleWorker/Kestrel/.github/workflows/release.yml@refs/tags/v0.6.0"  # type: ignore[index]
        )
        identity_changed = True
    elif mutation == "identity-workflow-sha":
        identity["workflow_sha"] = "b" * 40  # type: ignore[index]
        identity_changed = True
    elif mutation == "identity-nonce":
        identity["transaction_nonce"] = "0" * 64  # type: ignore[index]
        identity_changed = True
    elif mutation == "identity-run":
        identity["run_id"] = 1102  # type: ignore[index]
        identity_changed = True
    elif mutation == "identity-attempt":
        identity["run_attempt"] = 2  # type: ignore[index]
        identity_changed = True
    elif mutation == "artifact-expired":
        artifact["expired"] = True  # type: ignore[index]
    elif mutation == "artifact-name":
        artifact["name"] = "../dispatch-identity.json"  # type: ignore[index]
    elif mutation == "artifact-duplicate":
        artifact["matching_name_count"] = 2  # type: ignore[index]
    elif mutation == "artifact-api-digest":
        artifact["api_digest"] = "sha256:" + "0" * 64  # type: ignore[index]
    elif mutation == "artifact-content":
        artifact["content_sha256"] = "sha256:" + "0" * 64  # type: ignore[index]
    else:  # pragma: no cover
        raise AssertionError(mutation)
    if identity_changed:
        artifact["content_sha256"] = _sha256(_canonical(identity))  # type: ignore[index]

    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[candidate],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    assert reconciliation["outcome"]["state"] == "unsafe_orphan_or_tamper"  # type: ignore[index]
    assert expected_reason in reconciliation["candidates"][0]["reasons"]  # type: ignore[index]
    assert tombstone is not None


def test_dispatch_admission_is_derived_only_from_adopted_reconciliation() -> None:
    journal, _, _ = _prepared_dispatch()
    reconciliation, _ = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport("response_details_received"),
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[_candidate()],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    admission = subject.create_dispatch_admission(
        reconciliation=reconciliation,
        identity_observation=_dispatch_identity(),
        **_pre_admission_writer_inventory_arguments(),
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 14, tzinfo=UTC),
    )
    assert admission["schema"] == "kestrel.dispatch_admission.v1"
    assert admission["adopted_run_id"] == 1101
    assert admission["run_attempt"] == 1
    assert admission["expected_ref"] == "refs/heads/main"
    assert admission["issued_at"] == "2026-08-13T20:00:14Z"
    assert admission["expires_at"] == "2026-08-13T20:05:14Z"


def test_dispatch_admission_exposes_no_self_certified_authority_parameters() -> None:
    parameters = inspect.signature(subject.create_dispatch_admission).parameters

    assert {
        "signing_principal",
        "signing_key_fingerprint",
    }.isdisjoint(parameters)
    assert {
        "pre_admission_writer_inventory",
        "pre_admission_writer_inventory_signature",
        "owner_signing_keys_observation",
    } <= set(parameters)


def test_dispatch_admission_rejects_non_adopted_or_tombstoned_reconciliation() -> None:
    journal, _, _ = _prepared_dispatch()
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=[_poll(1, []), _poll(2, []), _poll(3, [])],
        candidates=[],
        _clock=lambda: datetime(2026, 8, 13, 20, 10, 3, tzinfo=UTC),
    )
    assert tombstone is not None
    with pytest.raises(ValueError, match="adopted|tombstone"):
        subject.create_dispatch_admission(
            reconciliation=reconciliation,
            identity_observation=_dispatch_identity(),
            **_pre_admission_writer_inventory_arguments(),
            _clock=lambda: datetime(2026, 8, 13, 20, 10, 4, tzinfo=UTC),
        )


def test_dispatch_admission_rejects_noncanonical_reconciliation() -> None:
    reconciliation, _ = _adopted_reconciliation_and_admission()
    reconciliation["untrusted_extra_authority"] = True
    with pytest.raises(ValueError, match="schema|reconciliation"):
        subject.create_dispatch_admission(
            reconciliation=reconciliation,
            identity_observation=_dispatch_identity(),
            **_pre_admission_writer_inventory_arguments(),
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 14, tzinfo=UTC),
        )


def test_dispatch_admission_producer_rejects_future_identity_evidence() -> None:
    reconciliation, _ = _adopted_reconciliation_and_admission()
    reconciliation["candidates"][0]["identity_artifact"][  # type: ignore[index]
        "identity_observed_at"
    ] = "2026-08-13T20:01:01Z"
    with pytest.raises(ValueError, match="future"):
        subject.create_dispatch_admission_from_reconciliation(
            reconciliation=reconciliation,
            containment=reconciliation["containment"],  # type: ignore[arg-type]
            **_pre_admission_writer_inventory_arguments(),
            _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
        )


def test_dispatch_admission_producer_rejects_zero_length_window() -> None:
    reconciliation, _ = _adopted_reconciliation_and_admission()

    with pytest.raises(ValueError, match="too old"):
        subject._build_dispatch_admission(  # noqa: SLF001
            reconciliation=reconciliation,
            identity_observed_at=datetime(2026, 8, 13, 19, 45, 14, tzinfo=UTC),
            signing_principal=SIGNING_PRINCIPAL,
            signing_key_fingerprint=KNOWN_KEY_FINGERPRINT,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 14, tzinfo=UTC),
        )


def test_dispatch_records_match_committed_recursive_schemas() -> None:
    import jsonschema

    journal, intent, _ = _prepared_dispatch()
    adopted, _ = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport("response_details_received"),
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[_candidate()],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    admission = subject.create_dispatch_admission(
        reconciliation=adopted,
        identity_observation=_dispatch_identity(),
        **_pre_admission_writer_inventory_arguments(),
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 14, tzinfo=UTC),
    )
    failed, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=[_poll(1, []), _poll(2, []), _poll(3, [])],
        candidates=[],
        _clock=lambda: datetime(2026, 8, 13, 20, 10, 3, tzinfo=UTC),
    )
    assert tombstone is not None
    records = {
        "kestrel.release_dispatch_transaction.v1": journal,
        "kestrel.release_dispatch_intent.v2": intent,
        "kestrel.release_dispatch_reconciliation.v1": adopted,
        "kestrel.dispatch_admission.v1": admission,
        "kestrel.dispatch_tombstone.v1": tombstone,
    }
    for name, value in records.items():
        schema = json.loads((ROOT / "schemas" / f"{name}.schema.json").read_bytes())
        jsonschema.Draft202012Validator(schema).validate(value)
    reconciliation_schema = json.loads(
        (ROOT / "schemas" / "kestrel.release_dispatch_reconciliation.v1.schema.json").read_bytes()
    )
    jsonschema.Draft202012Validator(reconciliation_schema).validate(failed)


def test_non_adopted_reconciliation_finalizes_one_signed_tombstone(
    tmp_path: Path,
) -> None:
    journal, _, _ = _prepared_dispatch()
    reconciliation, tombstone = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport(),
        containment=_containment(),
        polls=[_poll(1, []), _poll(2, []), _poll(3, [])],
        candidates=[],
        _clock=lambda: datetime(2026, 8, 13, 20, 10, 3, tzinfo=UTC),
    )
    assert tombstone is not None
    finalized, signed_tombstone, signature = subject.finalize_dispatch_tombstone(
        reconciliation=reconciliation,
        tombstone=tombstone,
        identity_file=_known_identity_file(tmp_path),
        owner_signing_keys_observation=_owner_signing_keys_observation(
            captured_at="2026-08-13T20:10:03Z"
        ),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
        _clock=lambda: datetime(2026, 8, 13, 20, 10, 3, tzinfo=UTC),
    )
    assert signed_tombstone["validation_status"] == "validated"
    assert finalized["validation_status"] == "validated"
    assert finalized["tombstone"]["canonical_sha256"] == _sha256(  # type: ignore[index]
        _canonical(signed_tombstone)
    )
    assert finalized["tombstone"]["signature_sha256"] == _sha256(signature)  # type: ignore[index]
    assert finalized["tombstone"]["validation_status"] == "validated"  # type: ignore[index]
    assert subject.verify_owner_detached_signature(
        receipt=_canonical(signed_tombstone),
        signature=signature,
        owner_signing_keys_observation=_owner_signing_keys_observation(
            captured_at="2026-08-13T20:10:03Z"
        ),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
        _clock=lambda: datetime(2026, 8, 13, 20, 10, 3, tzinfo=UTC),
    )


def test_tombstone_finalization_rejects_adopted_or_mismatched_record(
    tmp_path: Path,
) -> None:
    journal, _, _ = _prepared_dispatch()
    adopted, _ = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport("response_details_received"),
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[_candidate()],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="non-adopted|tombstone"):
        subject.finalize_dispatch_tombstone(
            reconciliation=adopted,
            tombstone={"schema": "kestrel.dispatch_tombstone.v1"},
            identity_file=_known_identity_file(tmp_path),
            owner_signing_keys_observation=_owner_signing_keys_observation(),
            principal=SIGNING_PRINCIPAL,
            namespace=SIGNING_NAMESPACE,
        )


@pytest.mark.parametrize(
    ("current", "next_state", "allowed"),
    [
        ("prepared", "sending", True),
        ("sending", "outcome_unknown", True),
        ("outcome_unknown", "app_containment", True),
        ("app_containment", "contained", True),
        ("contained", "reconciling", True),
        ("reconciling", "run_adopted", True),
        ("run_adopted", "admission_published", True),
        ("admission_published", "admission_verified_in_run", True),
        ("admission_verified_in_run", "approval_eligible", True),
        ("sending", "sending", False),
        ("outcome_unknown", "sending", False),
        ("reconciling", "approval_eligible", False),
        ("aborted_fail_closed", "sending", False),
    ],
)
def test_dispatch_state_machine_allows_only_normative_transitions(
    current: str, next_state: str, allowed: bool
) -> None:
    if allowed:
        assert subject.transition_dispatch_state(current, next_state) == next_state
    else:
        with pytest.raises(ValueError, match="transition"):
            subject.transition_dispatch_state(current, next_state)


def _adopted_reconciliation_and_admission() -> tuple[dict[str, object], dict[str, object]]:
    journal, _, _ = _prepared_dispatch()
    reconciliation, _ = subject.reconcile_dispatch(
        journal=journal,
        dispatch=_transport("response_details_received"),
        containment=_containment(),
        polls=[_poll(1, [1101]), _poll(2, [1101]), _poll(3, [1101])],
        candidates=[_candidate()],
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 13, tzinfo=UTC),
    )
    admission = subject.create_dispatch_admission(
        reconciliation=reconciliation,
        identity_observation=_dispatch_identity(),
        **_pre_admission_writer_inventory_arguments(),
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 14, tzinfo=UTC),
    )
    return reconciliation, admission


def _recovery_scope_bundle(
    tmp_path: Path, *, verified_at: str
) -> tuple[bytes, bytes, dict[str, object]]:
    token = b"exact-recovery-reader-token-for-admission"
    context = json.loads(_scope_context())
    context["signing_key_fingerprint"] = KNOWN_KEY_FINGERPRINT
    scope = subject.create_credential_scope_authority(
        purpose="recovery_reader",
        credential_id="credential-recovery-reader",
        principal_observation=_principal(),
        grants_snapshot=_grants_snapshot("recovery_reader"),
        token_fingerprint=_sha256(token),
        controller_context=_canonical(context),
    )
    scope_bytes = _canonical(scope)
    scope_signature = subject.sign_receipt_detached(
        receipt=scope_bytes,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    probes = json.loads(_endpoint_probes(scope))
    probes["captured_at"] = verified_at
    runtime = subject.verify_runtime_credential(
        scope_authority=scope_bytes,
        scope_authority_signature=scope_signature,
        owner_signing_keys_observation=_owner_signing_keys_observation(captured_at=verified_at),
        identity_probe=_principal(),
        endpoint_probe_observations=_canonical(probes),
        token_bytes=token,
        _clock=lambda: datetime.fromisoformat(verified_at.replace("Z", "+00:00")),
    )
    return scope_bytes, scope_signature, runtime


def test_signed_dispatch_admission_verifies_at_issuance_boundary(
    tmp_path: Path,
) -> None:
    now = "2026-08-13T20:00:14Z"
    _, admission = _adopted_reconciliation_and_admission()
    admission_bytes = _canonical(admission)
    admission_signature = subject.sign_receipt_detached(
        receipt=admission_bytes,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    scope, scope_signature, runtime = _recovery_scope_bundle(tmp_path, verified_at=now)
    owner_keys = _owner_signing_keys_observation(captured_at=now)

    verification = subject.verify_dispatch_admission(
        admission=admission_bytes,
        signature=admission_signature,
        owner_signing_keys_observation=owner_keys,
        current_run_identity=_dispatch_identity(),
        recovery_scope_authority=scope,
        recovery_scope_signature=scope_signature,
        recovery_runtime_verification=_canonical(runtime),
        _clock=lambda: datetime.fromisoformat(now.replace("Z", "+00:00")),
    )
    assert set(verification) == {
        "receipt_digest",
        "signature_digest",
        "verification_digest",
    }
    assert verification["receipt_digest"] == _sha256(admission_bytes)
    assert verification["signature_digest"] == _sha256(admission_signature)
    assert str(verification["verification_digest"]).startswith("sha256:")


@pytest.mark.parametrize(
    ("now", "message"),
    [
        ("2026-08-13T20:00:13Z", "not yet"),
        ("2026-08-13T20:05:14Z", "expired"),
        ("2026-08-13T20:05:15Z", "expired"),
    ],
)
def test_dispatch_admission_rejects_outside_exact_freshness_window(
    tmp_path: Path, now: str, message: str
) -> None:
    _, admission = _adopted_reconciliation_and_admission()
    admission_bytes = _canonical(admission)
    admission_signature = subject.sign_receipt_detached(
        receipt=admission_bytes,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    scope, scope_signature, runtime = _recovery_scope_bundle(tmp_path, verified_at=now)
    with pytest.raises(ValueError, match=message):
        subject.verify_dispatch_admission(
            admission=admission_bytes,
            signature=admission_signature,
            owner_signing_keys_observation=_owner_signing_keys_observation(captured_at=now),
            current_run_identity=_dispatch_identity(),
            recovery_scope_authority=scope,
            recovery_scope_signature=scope_signature,
            recovery_runtime_verification=_canonical(runtime),
            _clock=lambda: datetime.fromisoformat(now.replace("Z", "+00:00")),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("run", "run"),
        ("signature", "signature"),
        ("overscope", "scope|grant"),
        ("runtime", "runtime"),
    ],
)
def test_dispatch_admission_rejects_identity_signature_and_reader_scope_mutants(
    tmp_path: Path, mutation: str, message: str
) -> None:
    now = "2026-08-13T20:01:00Z"
    _, admission = _adopted_reconciliation_and_admission()
    admission_bytes = _canonical(admission)
    admission_signature = subject.sign_receipt_detached(
        receipt=admission_bytes,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    identity = _dispatch_identity()
    scope, scope_signature, runtime = _recovery_scope_bundle(tmp_path, verified_at=now)
    if mutation == "run":
        identity["run_id"] = 1102
    elif mutation == "signature":
        admission_signature = admission_signature.replace(b"U1NI", b"A1NI", 1)
    elif mutation == "overscope":
        scope_value = json.loads(scope)
        scope_value["grants"].append(
            {
                "repository_full_name": "John-MiracleWorker/Kestrel-Release-Recovery",
                "repository_id": 303,
                "permission": "Actions",
                "level": "write",
            }
        )
        scope_value["grants"] = sorted(
            scope_value["grants"],
            key=lambda item: (item["repository_full_name"], item["permission"]),
        )
        scope = _canonical(scope_value)
        scope_signature = subject.sign_receipt_detached(
            receipt=scope,
            identity_file=_known_identity_file(tmp_path),
            principal=SIGNING_PRINCIPAL,
            namespace=SIGNING_NAMESPACE,
        )
    elif mutation == "runtime":
        runtime["purpose"] = "release_guard"
    else:  # pragma: no cover
        raise AssertionError(mutation)
    with pytest.raises(ValueError, match=message):
        subject.verify_dispatch_admission(
            admission=admission_bytes,
            signature=admission_signature,
            owner_signing_keys_observation=_owner_signing_keys_observation(captured_at=now),
            current_run_identity=identity,
            recovery_scope_authority=scope,
            recovery_scope_signature=scope_signature,
            recovery_runtime_verification=_canonical(runtime),
            _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
        )


@pytest.mark.parametrize("mutation", ["workflow-ref", "workflow-sha"])
def test_dispatch_admission_binds_current_workflow_identity(tmp_path: Path, mutation: str) -> None:
    now = "2026-08-13T20:01:00Z"
    _, admission = _adopted_reconciliation_and_admission()
    admission_bytes = _canonical(admission)
    admission_signature = subject.sign_receipt_detached(
        receipt=admission_bytes,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    scope, scope_signature, runtime = _recovery_scope_bundle(tmp_path, verified_at=now)
    identity = _dispatch_identity()
    if mutation == "workflow-ref":
        identity["workflow_ref"] = (
            "John-MiracleWorker/Kestrel/.github/workflows/release.yml@refs/tags/v0.6.0"
        )
    else:
        identity["workflow_sha"] = "b" * 40

    with pytest.raises(ValueError, match="workflow"):
        subject.verify_dispatch_admission(
            admission=admission_bytes,
            signature=admission_signature,
            owner_signing_keys_observation=_owner_signing_keys_observation(captured_at=now),
            current_run_identity=identity,
            recovery_scope_authority=scope,
            recovery_scope_signature=scope_signature,
            recovery_runtime_verification=_canonical(runtime),
            _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
        )


def test_dispatch_admission_clock_read_failure_is_fail_closed(tmp_path: Path) -> None:
    now = "2026-08-13T20:01:00Z"
    _, admission = _adopted_reconciliation_and_admission()
    admission_bytes = _canonical(admission)
    admission_signature = subject.sign_receipt_detached(
        receipt=admission_bytes,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    scope, scope_signature, runtime = _recovery_scope_bundle(tmp_path, verified_at=now)

    def failed_clock() -> datetime:
        raise OSError("simulated clock failure")

    with pytest.raises(OSError, match="clock failure"):
        subject.verify_dispatch_admission(
            admission=admission_bytes,
            signature=admission_signature,
            owner_signing_keys_observation=_owner_signing_keys_observation(captured_at=now),
            current_run_identity=_dispatch_identity(),
            recovery_scope_authority=scope,
            recovery_scope_signature=scope_signature,
            recovery_runtime_verification=_canonical(runtime),
            _clock=failed_clock,
        )


@pytest.mark.parametrize(
    ("vector_name", "verifier", "expected_environment"),
    [
        ("github-authority", "github", 901),
        ("pypi-authority", "pypi", 904),
        ("recovery-repository-authority", "recovery", 304),
    ],
)
def test_owner_signed_authority_vectors_verify_policy_and_freshness(
    vector_name: str,
    verifier: str,
    expected_environment: int,
) -> None:
    receipt, signature = _positive_contract_vector(vector_name)
    assert signature is not None
    owner_keys = _owner_signing_keys_observation(captured_at="2026-08-13T20:01:00Z")

    def now() -> datetime:
        return datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC)

    if verifier == "github":
        verification = subject.verify_github_authority(
            receipt=receipt,
            signature=signature,
            owner_signing_keys_observation=owner_keys,
            expected_run_id=707,
            expected_candidate_digest="sha256:" + "0" * 64,
            expected_environment_id=expected_environment,
            _clock=now,
        )
    elif verifier == "pypi":
        verification = subject.verify_pypi_authority(
            receipt=receipt,
            signature=signature,
            owner_signing_keys_observation=owner_keys,
            expected_run_id=707,
            expected_candidate_digest="sha256:" + "0" * 64,
            expected_environment_id=expected_environment,
            _clock=now,
        )
    else:
        verification = subject.verify_recovery_repository_authority(
            receipt=receipt,
            signature=signature,
            owner_signing_keys_observation=owner_keys,
            expected_repository="John-MiracleWorker/Kestrel-Release-Recovery",
            expected_repository_id=expected_environment,
            _clock=now,
        )

    assert verification["receipt_digest"] == _sha256(receipt)
    assert verification["authority"] == json.loads(receipt)
    assert verification["signing_key_fingerprint"] == KNOWN_KEY_FINGERPRINT
    assert verification["validation_status"] == "validated"


def test_github_authority_rejects_unversioned_authorize_boundary() -> None:
    receipt, _signature = _positive_contract_vector("github-authority")
    authority = json.loads(receipt)
    authority["phase"] = "authorize"
    authority["maintenance_window_acknowledgement"]["statement"] = (
        "I hold exclusive owner control of GitHub and GHCR authority through the "
        "bounded release transaction authority window."
    )

    with pytest.raises(ValueError, match="schema validation"):
        subject.validate_github_authority(authority)


@pytest.mark.parametrize(
    "mutation",
    [
        "installed-app",
        "ruleset-bypass",
        "workflow-drift",
        "missing-environment-policy",
        "run-head-drift",
        "phase-environment-mismatch",
    ],
)
def test_github_authority_policy_mutants_fail_closed(mutation: str) -> None:
    raw, _ = _positive_contract_vector("github-authority")
    value = json.loads(raw)
    if mutation == "installed-app":
        value["installed_apps"].append(
            {
                "app_slug": "unexpected-writer",
                "app_id": 1,
                "installation_id": 2,
                "account_login": "John-MiracleWorker",
                "account_id": 606,
                "repository_selection": "selected",
                "repositories": [{"full_name": "John-MiracleWorker/Kestrel", "id": 303}],
                "repository_permissions": {"actions": "write"},
                "organization_permissions": {},
                "account_permissions": {},
                "installed_at": "2026-08-13T19:00:00Z",
            }
        )
    elif mutation == "ruleset-bypass":
        value["ingress_ruleset"]["bypass_actors"] = [
            {
                "actor_type": "RepositoryRole",
                "actor_id": 5,
                "bypass_mode": "always",
            }
        ]
    elif mutation == "workflow-drift":
        value["workflow_ingress"]["candidate_blob_sha256"] = "sha256:" + "f" * 64
    elif mutation == "missing-environment-policy":
        value["environment_policies"].pop()
    elif mutation == "run-head-drift":
        value["promotion_run"]["head_sha"] = "c" * 40
    else:
        value["environment"]["name"] = "release-commit"

    with pytest.raises(ValueError, match="authority|policy|ruleset|workflow|run|phase"):
        subject.validate_github_authority(value)


@pytest.mark.parametrize(
    "mutation",
    ["second-role", "upload-token", "publisher", "execution-binding", "run-ref"],
)
def test_pypi_authority_policy_mutants_fail_closed(mutation: str) -> None:
    raw, _ = _positive_contract_vector("pypi-authority")
    value = json.loads(raw)
    if mutation == "second-role":
        value["roles"].append({"username": "attacker", "role": "Maintainer"})
    elif mutation == "upload-token":
        value["upload_tokens"].append(
            {
                "scope": "project",
                "owner_username": "John_miracleworker",
                "name_prefix": "unexpected",
                "active": True,
                "can_upload": True,
            }
        )
    elif mutation == "publisher":
        value["trusted_publishers"][0]["repository_name"] = "Other"
    elif mutation == "execution-binding":
        value["bindings"]["execution_authorization_digest"] = "sha256:" + "f" * 64
    else:
        value["promotion_run"]["ref"] = "refs/tags/v1.2.3"

    with pytest.raises(ValueError, match="PyPI|authority|publisher|binding|run"):
        subject.validate_pypi_authority(value)


@pytest.mark.parametrize(
    "mutation",
    ["second-credential", "workflow", "installed-app", "capability", "collaborator"],
)
def test_recovery_repository_authority_policy_mutants_fail_closed(
    mutation: str,
) -> None:
    raw, _ = _positive_contract_vector("recovery-repository-authority")
    value = json.loads(raw)
    if mutation == "second-credential":
        value["credentials"].append(dict(value["credentials"][0], id="second"))
    elif mutation == "workflow":
        value["workflows"].append(
            {
                "id": 1,
                "name": "writer",
                "path": ".github/workflows/write.yml",
                "state": "active",
                "permissions": {"contents": "write"},
            }
        )
    elif mutation == "installed-app":
        value["installed_apps"].append(
            {
                "app_slug": "writer",
                "app_id": 1,
                "installation_id": 2,
                "account_login": "John-MiracleWorker",
                "account_id": 606,
                "repository_selection": "selected",
                "repositories": [
                    {
                        "full_name": "John-MiracleWorker/Kestrel-Release-Recovery",
                        "id": 304,
                    }
                ],
                "repository_permissions": {"contents": "write"},
                "organization_permissions": {},
                "account_permissions": {},
                "installed_at": "2026-08-13T19:00:00Z",
            }
        )
    elif mutation == "capability":
        value["credentials"][0]["capabilities"].append("repository_write")
    else:
        value["collaborators"].append(
            {
                "login": "attacker",
                "id": 999,
                "node_id": "U_attacker",
                "type": "User",
                "role_name": "write",
                "permissions": {
                    "admin": False,
                    "maintain": False,
                    "push": True,
                    "triage": True,
                    "pull": True,
                },
            }
        )

    with pytest.raises(ValueError, match="recovery|authority|credential|writer"):
        subject.validate_recovery_repository_authority(value)


def _recovery_authority_source_bundle(
    *, now: datetime
) -> tuple[dict[str, bytes], dict[str, object]]:
    raw, _ = _positive_contract_vector("recovery-repository-authority")
    expected = json.loads(raw)
    schema = "kestrel.recovery_repository_authority.v1"
    phase = "authority"
    mode = "operational"
    owner_snapshot = {
        "schema": "kestrel.recovery_repository_authority_owner.v1",
        "repository": expected["repository"],
        "owner": expected["owner"],
        "collaborators": expected["collaborators"],
        "invitations": expected["invitations"],
        "deploy_keys": expected["deploy_keys"],
        "installed_apps": expected["installed_apps"],
        "workflows": expected["workflows"],
        "packages": expected["packages"],
        "credentials": expected["credentials"],
        "captured_at": "2026-08-13T20:00:00Z",
        "complete": True,
    }
    repository = {
        "id": expected["repository"]["id"],
        "full_name": expected["repository"]["full_name"],
        "owner": expected["owner"],
    }
    immutable = expected["immutable_releases"]
    context = {
        "schema": "kestrel.recovery_repository_authority_controller_context.v1",
        "owner": {
            "id": expected["owner"]["id"],
            "login": expected["owner"]["login"],
        },
        "acknowledgement": expected["maintenance_window_acknowledgement"],
        "captured_at": "2026-08-13T20:00:00Z",
        "complete": True,
    }
    bodies = {
        "recovery-owner-dashboard": owner_snapshot,
        "recovery-repository-rest": repository,
        "recovery-immutable-releases-rest": immutable,
        "controller-context": context,
    }
    sources = {
        name: _committed_source(
            receipt_schema=schema,
            phase=phase,
            mode=mode,
            name=name,
            body=body,
            now=now,
        )
        for name, body in bodies.items()
    }
    return sources, expected


def test_create_recovery_repository_authority_derives_exact_policy_and_freshness() -> None:
    now = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC)
    sources, expected = _recovery_authority_source_bundle(now=now)

    authority = subject.create_recovery_repository_authority(
        owner_authority_snapshot=sources["recovery-owner-dashboard"],
        repository_observation=sources["recovery-repository-rest"],
        immutable_releases_observation=sources["recovery-immutable-releases-rest"],
        controller_context=sources["controller-context"],
        _clock=lambda: now,
    )

    for field in (
        "repository",
        "owner",
        "collaborators",
        "invitations",
        "deploy_keys",
        "installed_apps",
        "workflows",
        "packages",
        "credentials",
        "immutable_releases",
        "maintenance_window_acknowledgement",
    ):
        assert authority[field] == expected[field]
    assert authority["observed_at"] == "2026-08-13T20:00:00Z"
    assert authority["expires_at"] == "2026-08-13T20:05:00Z"
    assert [item["name"] for item in authority["source_snapshots"]] == sorted(sources)
    assert subject.validate_recovery_repository_authority(authority) == authority


def test_create_recovery_repository_authority_rejects_cross_source_identity() -> None:
    now = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC)
    sources, _ = _recovery_authority_source_bundle(now=now)
    repository_envelope = json.loads(sources["recovery-repository-rest"])
    repository_body = json.loads(base64.b64decode(repository_envelope["body"], validate=True))
    repository_body["id"] = 999
    repository_envelope["body"] = base64.b64encode(_canonical(repository_body)).decode("ascii")

    with pytest.raises(ValueError, match="recovery.*identity"):
        subject.create_recovery_repository_authority(
            owner_authority_snapshot=sources["recovery-owner-dashboard"],
            repository_observation=_canonical(repository_envelope),
            immutable_releases_observation=sources["recovery-immutable-releases-rest"],
            controller_context=sources["controller-context"],
            _clock=lambda: now,
        )


def test_recovery_authority_create_cli_is_exposed() -> None:
    help_text = subject._parser().format_help()  # noqa: SLF001

    assert "create-recovery-repository-authority" in help_text


def _promotion_identity_for_authority(
    *, run_id: int, ref: str, nonce: str, repository_id: int = 303
) -> dict[str, object]:
    raw, _ = _positive_contract_vector("dispatch-identity")
    identity = json.loads(raw)
    identity.update(
        {
            "transaction_nonce": nonce,
            "repository": "John-MiracleWorker/Kestrel",
            "repository_id": repository_id,
            "workflow": "Release",
            "workflow_ref": ("John-MiracleWorker/Kestrel/.github/workflows/release.yml@" + ref),
            "workflow_sha": "a" * 40,
            "event_name": "workflow_dispatch",
            "ref": ref,
            "sha": "a" * 40,
            "run_id": run_id,
            "run_attempt": 1,
            "actor": "kestrel-release-dispatcher[bot]",
            "actor_id": 202,
            "triggering_actor": "kestrel-release-dispatcher[bot]",
        }
    )
    return identity


def _promotion_run_source_body(
    *, run_id: int, ref: str, repository_id: int = 303
) -> dict[str, object]:
    return {
        "schema": "kestrel.promotion_run_observation.v1",
        "repository_id": repository_id,
        "workflow_id": 404,
        "workflow_path": ".github/workflows/release.yml",
        "run_id": run_id,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "ref": ref,
        "head_sha": "a" * 40,
        "workflow_sha": "a" * 40,
        "actor": {"id": 202, "login": "kestrel-release-dispatcher[bot]"},
        "triggering_actor": {
            "id": 202,
            "login": "kestrel-release-dispatcher[bot]",
        },
        "captured_at": "2026-08-13T20:00:00Z",
        "complete": True,
    }


def _pypi_authority_source_bundle(
    *, now: datetime, repository_id: int = 303
) -> tuple[dict[str, bytes], bytes, dict[str, object]]:
    raw, _ = _positive_contract_vector("pypi-authority")
    expected = json.loads(raw)
    manifest = _authority_candidate_manifest(repository_id=repository_id)
    manifest_raw = _canonical(manifest)
    expected["candidate"] = {
        "artifact_set_digest": manifest["artifact_set_digest"],
        "candidate_manifest_digest": _sha256(manifest_raw),
        "candidate_run_attempt": 1,
        "candidate_run_id": 101,
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "tag": "v1.2.3",
        "version": "1.2.3",
    }
    mode = "initiate"
    schema = "kestrel.pypi_upload_authority_prerequisite.v3"
    owner_snapshot = {
        "schema": "kestrel.pypi_owner_authority.v1",
        "project": expected["project"],
        "owner": expected["owner"],
        "organization": expected["organization"],
        "roles": expected["roles"],
        "pending_role_grants": expected["pending_role_grants"],
        "trusted_publishers": expected["trusted_publishers"],
        "project_tokens": [],
        "account_tokens": [],
        "captured_at": "2026-08-13T20:00:00Z",
        "complete": True,
    }
    context = {
        "schema": "kestrel.pypi_authority_controller_context.v1",
        "owner": {
            "username": "John_miracleworker",
            "id": 606,
            "login": "John-MiracleWorker",
        },
        "main_sha": "a" * 40,
        "acknowledgement": expected["maintenance_window_acknowledgement"],
        "captured_at": "2026-08-13T20:00:00Z",
        "complete": True,
    }
    bodies = {
        "bindings": expected["bindings"],
        "controller-context": context,
        "environment-rest": expected["environment"],
        "promotion-dispatch-identity": _promotion_identity_for_authority(
            run_id=707,
            ref="refs/heads/main",
            nonce="0123456789abcdef" * 4,
            repository_id=repository_id,
        ),
        "promotion-run-rest": _promotion_run_source_body(
            run_id=707, ref="refs/heads/main", repository_id=repository_id
        ),
        "pypi-owner-dashboard": owner_snapshot,
        "pypi-project": {
            "name": "nested-memvid-agent",
            "version": "1.2.2",
            "last_serial": 1001,
        },
    }
    sources = {
        name: _committed_source(
            receipt_schema=schema,
            phase="publication",
            mode=mode,
            name=name,
            body=body,
            now=now,
        )
        for name, body in bodies.items()
    }
    return sources, manifest_raw, expected


def test_create_pypi_authority_derives_candidate_run_and_sole_owner_policy() -> None:
    now = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC)
    sources, manifest, expected = _pypi_authority_source_bundle(now=now)

    authority = subject.create_pypi_authority(
        public_project_observation=sources["pypi-project"],
        owner_authority_snapshot=sources["pypi-owner-dashboard"],
        promotion_run_observation=sources["promotion-run-rest"],
        promotion_dispatch_identity=sources["promotion-dispatch-identity"],
        candidate_manifest=manifest,
        environment_observation=sources["environment-rest"],
        controller_context=sources["controller-context"],
        bindings=sources["bindings"],
        _clock=lambda: now,
    )

    for field in (
        "project",
        "owner",
        "organization",
        "roles",
        "pending_role_grants",
        "trusted_publishers",
        "upload_tokens",
        "candidate",
        "environment",
        "bindings",
        "maintenance_window_acknowledgement",
    ):
        assert authority[field] == expected[field]
    assert authority["promotion_run"]["context_observation_digest"] == _sha256(
        sources["promotion-dispatch-identity"]
    )
    assert authority["promotion_run"]["rest_observation_digest"] == _sha256(
        sources["promotion-run-rest"]
    )
    assert authority["public_project"]["observation_digest"] == _sha256(sources["pypi-project"])
    assert subject.validate_pypi_authority(authority) == authority


def test_create_pypi_authority_rejects_identity_run_mismatch() -> None:
    now = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC)
    sources, manifest, _ = _pypi_authority_source_bundle(now=now)
    envelope = json.loads(sources["promotion-dispatch-identity"])
    body = json.loads(base64.b64decode(envelope["body"], validate=True))
    body["run_id"] = 708
    envelope["body"] = base64.b64encode(_canonical(body)).decode("ascii")

    with pytest.raises(ValueError, match="promotion run.*identity"):
        subject.create_pypi_authority(
            public_project_observation=sources["pypi-project"],
            owner_authority_snapshot=sources["pypi-owner-dashboard"],
            promotion_run_observation=sources["promotion-run-rest"],
            promotion_dispatch_identity=_canonical(envelope),
            candidate_manifest=manifest,
            environment_observation=sources["environment-rest"],
            controller_context=sources["controller-context"],
            bindings=sources["bindings"],
            _clock=lambda: now,
        )


def test_pypi_authority_create_cli_is_exposed() -> None:
    assert "create-pypi-authority" in subject._parser().format_help()  # noqa: SLF001


def _github_authority_source_bundle(
    *, now: datetime, repository_id: int = 303
) -> tuple[dict[str, bytes], bytes, dict[str, object]]:
    raw, _ = _positive_contract_vector("github-authority")
    expected = json.loads(raw)
    manifest = _authority_candidate_manifest(repository_id=repository_id)
    manifest_raw = _canonical(manifest)
    expected["candidate"] = {
        "artifact_set_digest": manifest["artifact_set_digest"],
        "candidate_manifest_digest": _sha256(manifest_raw),
        "candidate_run_attempt": 1,
        "candidate_run_id": 101,
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "tag": "v1.2.3",
        "version": "1.2.3",
    }
    intent_raw, _ = _positive_contract_vector("dispatch-intent")
    intent = json.loads(intent_raw)
    request = {"ref": intent["target"]["short_ref"], "inputs": intent["inputs"]}
    assert _sha256(_canonical(request)) == intent["request_digest"]
    workflow_bytes = b"name: Release\non: workflow_dispatch\n"
    tag_ruleset = dict(expected["tag_ruleset"])
    ingress_ruleset = dict(expected["ingress_ruleset"])
    tag_ruleset.pop("observation_digest")
    ingress_ruleset.pop("observation_digest")
    owner = expected["owner"]
    context = {
        "schema": "kestrel.github_authority_controller_context.v1",
        "phase": "admission",
        "mode": "initiate",
        "owner": {"id": owner["id"], "login": owner["login"]},
        "acknowledgement": expected["maintenance_window_acknowledgement"],
        "captured_at": "2026-08-13T20:00:00Z",
        "complete": True,
    }
    bodies = {
        "bindings": expected["bindings"],
        "candidate-workflow-contents": {
            "path": ".github/workflows/release.yml",
            "content_base64": base64.b64encode(workflow_bytes).decode("ascii"),
        },
        "controller-context": context,
        "default-branch-workflow-contents": {
            "path": ".github/workflows/release.yml",
            "content_base64": base64.b64encode(workflow_bytes).decode("ascii"),
        },
        "dispatch-intent": intent,
        "dispatch-intent-signature": {
            "receipt_digest": _sha256(intent_raw),
            "signature_digest": "sha256:" + "9" * 64,
        },
        "dispatch-request": request,
        "dispatch-response": {
            "schema": "kestrel.github_dispatch_outcome.v1",
            "transport_outcome": expected["dispatch"]["transport_outcome"],
            "response_digest": expected["dispatch"]["response_digest"],
            "reconciliation_digest": expected["dispatch"]["reconciliation_digest"],
        },
        "dispatcher-invalidation-owner": {
            "schema": "kestrel.dispatcher_invalidation_observation.v1",
            "uninstalled_at": expected["dispatch"]["uninstalled_at"],
            "token_invalidation_probe": expected["dispatch"]["token_invalidation_probe"],
            "captured_at": "2026-08-13T20:00:00Z",
            "complete": True,
        },
        "environment-policy-types-owner": {
            "schema": "kestrel.environment_policy_types_snapshot.v1",
            "policies": expected["environment_policies"],
            "captured_at": "2026-08-13T20:00:00Z",
            "complete": True,
        },
        "environment-rest": expected["environment"],
        "ghcr-package-access-owner": {
            **expected["ghcr_package"],
            "captured_at": "2026-08-13T20:00:00Z",
            "complete": True,
        },
        "ingress-ruleset-detail-rest": ingress_ruleset,
        "installed-apps-owner": {
            "schema": "kestrel.installed_apps_authority_snapshot.v1",
            "installed_apps": expected["installed_apps"],
            "captured_at": "2026-08-13T20:00:00Z",
            "complete": True,
        },
        "promotion-dispatch-identity": _promotion_identity_for_authority(
            run_id=707,
            ref="refs/heads/main",
            nonce="0123456789abcdef" * 4,
            repository_id=repository_id,
        ),
        "promotion-run-rest": _promotion_run_source_body(
            run_id=707, ref="refs/heads/main", repository_id=repository_id
        ),
        "repository-rest": {
            "id": repository_id,
            "full_name": "John-MiracleWorker/Kestrel",
            "owner": owner,
        },
        "rulesets-rest": {
            "schema": "kestrel.rulesets_observation.v1",
            "rulesets": [
                {"id": 810, "name": "kestrel-release-tags", "target": "tag"},
                {
                    "id": 811,
                    "name": "kestrel-release-transaction-main-lock",
                    "target": "branch",
                },
            ],
            "captured_at": "2026-08-13T20:00:00Z",
            "complete": True,
        },
        "tag-ruleset-detail-rest": tag_ruleset,
        "workflow-rest": {
            "id": 404,
            "path": ".github/workflows/release.yml",
            "state": "active",
            "default_branch": "main",
        },
    }
    sources = {
        name: _committed_source(
            receipt_schema="kestrel.github_release_authority.v3",
            phase="admission",
            mode="initiate",
            name=name,
            body=body,
            now=now,
        )
        for name, body in bodies.items()
    }
    return sources, manifest_raw, expected


def _create_github_authority_from_bundle(
    *, sources: dict[str, bytes], manifest: bytes, now: datetime
) -> dict[str, object]:
    return subject.create_github_authority(
        repository_observation=sources["repository-rest"],
        promotion_run_observation=sources["promotion-run-rest"],
        promotion_dispatch_identity=sources["promotion-dispatch-identity"],
        candidate_manifest=manifest,
        environment_observation=sources["environment-rest"],
        environment_policy_types_snapshot=sources["environment-policy-types-owner"],
        rulesets_observation=sources["rulesets-rest"],
        tag_ruleset_detail_observation=sources["tag-ruleset-detail-rest"],
        ingress_ruleset_detail_observation=sources["ingress-ruleset-detail-rest"],
        workflow_observation=sources["workflow-rest"],
        default_branch_workflow_contents=sources["default-branch-workflow-contents"],
        candidate_workflow_contents=sources["candidate-workflow-contents"],
        dispatch_intent=sources["dispatch-intent"],
        dispatch_intent_signature=sources["dispatch-intent-signature"],
        dispatch_request=sources["dispatch-request"],
        dispatch_outcome=sources["dispatch-response"],
        installed_apps_snapshot=sources["installed-apps-owner"],
        ghcr_package_access_snapshot=sources["ghcr-package-access-owner"],
        dispatcher_invalidation_snapshot=sources["dispatcher-invalidation-owner"],
        controller_context=sources["controller-context"],
        bindings=sources["bindings"],
        _clock=lambda: now,
    )


def test_create_github_authority_derives_cross_source_authority_graph() -> None:
    now = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC)
    sources, manifest, expected = _github_authority_source_bundle(now=now)

    authority = _create_github_authority_from_bundle(sources=sources, manifest=manifest, now=now)

    for field in (
        "repository",
        "owner",
        "candidate",
        "phase",
        "mode",
        "environment",
        "environment_policies",
        "installed_apps",
        "ghcr_package",
        "bindings",
        "maintenance_window_acknowledgement",
    ):
        assert authority[field] == expected[field]
    assert authority["promotion_run"]["context_observation_digest"] == _sha256(
        sources["promotion-dispatch-identity"]
    )
    assert authority["tag_ruleset"]["observation_digest"] == _sha256(
        sources["tag-ruleset-detail-rest"]
    )
    assert authority["workflow_ingress"]["default_branch_blob_sha256"] == _sha256(
        b"name: Release\non: workflow_dispatch\n"
    )
    assert subject.validate_github_authority(authority) == authority


def test_create_github_authority_binds_live_repository_id_not_fixture_id() -> None:
    now = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC)
    repository_id = 1_155_799_292
    sources, manifest, _ = _github_authority_source_bundle(now=now, repository_id=repository_id)

    authority = _create_github_authority_from_bundle(sources=sources, manifest=manifest, now=now)

    assert authority["repository"]["id"] == repository_id  # type: ignore[index]
    assert authority["promotion_run"]["repository_id"] == repository_id  # type: ignore[index]


def test_create_github_authority_rejects_ruleset_inventory_substitution() -> None:
    now = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC)
    sources, manifest, _ = _github_authority_source_bundle(now=now)
    envelope = json.loads(sources["rulesets-rest"])
    body = json.loads(base64.b64decode(envelope["body"], validate=True))
    body["rulesets"].pop()
    envelope["body"] = base64.b64encode(_canonical(body)).decode("ascii")

    kwargs = {
        "repository_observation": sources["repository-rest"],
        "promotion_run_observation": sources["promotion-run-rest"],
        "promotion_dispatch_identity": sources["promotion-dispatch-identity"],
        "candidate_manifest": manifest,
        "environment_observation": sources["environment-rest"],
        "environment_policy_types_snapshot": sources["environment-policy-types-owner"],
        "rulesets_observation": _canonical(envelope),
        "tag_ruleset_detail_observation": sources["tag-ruleset-detail-rest"],
        "ingress_ruleset_detail_observation": sources["ingress-ruleset-detail-rest"],
        "workflow_observation": sources["workflow-rest"],
        "default_branch_workflow_contents": sources["default-branch-workflow-contents"],
        "candidate_workflow_contents": sources["candidate-workflow-contents"],
        "dispatch_intent": sources["dispatch-intent"],
        "dispatch_intent_signature": sources["dispatch-intent-signature"],
        "dispatch_request": sources["dispatch-request"],
        "dispatch_outcome": sources["dispatch-response"],
        "installed_apps_snapshot": sources["installed-apps-owner"],
        "ghcr_package_access_snapshot": sources["ghcr-package-access-owner"],
        "dispatcher_invalidation_snapshot": sources["dispatcher-invalidation-owner"],
        "controller_context": sources["controller-context"],
        "bindings": sources["bindings"],
        "_clock": lambda: now,
    }
    with pytest.raises(ValueError, match="ruleset.*inventory"):
        subject.create_github_authority(**kwargs)


def test_github_authority_create_cli_is_exposed() -> None:
    assert "create-github-authority" in subject._parser().format_help()  # noqa: SLF001


def test_recovery_capsule_vector_passes_fail_closed_policy() -> None:
    raw, _ = _positive_contract_vector("release-recovery-capsule")
    value = json.loads(raw)

    assert subject.validate_recovery_capsule_manifest(value) == value


@pytest.mark.parametrize(
    "mutation",
    [
        "archive-mode",
        "scanner",
        "finding",
        "repository",
        "execution-authority",
        "unknown-asset",
        "unsafe-path",
    ],
)
def test_recovery_capsule_policy_mutants_fail_closed(mutation: str) -> None:
    raw, _ = _positive_contract_vector("release-recovery-capsule")
    value = json.loads(raw)
    if mutation == "archive-mode":
        value["archive_policy"]["file_mode"] = "0600"
    elif mutation == "scanner":
        value["secret_scan"]["image"] = "zricethezav/gitleaks:latest"
    elif mutation == "finding":
        value["secret_scan"]["unallowed_findings"] = 1
    elif mutation == "repository":
        value["recovery_repository"]["full_name"] = "attacker/recovery"
    elif mutation == "execution-authority":
        value["assets"].append(
            {
                "name": "release-execution-authorization.json",
                "sha256": "sha256:" + "f" * 64,
                "size_bytes": 1,
                "media_type": "application/json",
            }
        )
        value["assets"].sort(key=lambda item: item["name"])
    elif mutation == "unknown-asset":
        value["assets"].append(
            {
                "name": "extra.txt",
                "sha256": "sha256:" + "f" * 64,
                "size_bytes": 1,
                "media_type": "text/plain",
            }
        )
        value["assets"].sort(key=lambda item: item["name"])
    else:
        value["assets"][0]["name"] = "../release-authorization.json"

    with pytest.raises(ValueError, match="capsule|archive|scan|repository|execution"):
        subject.validate_recovery_capsule_manifest(value)


def test_build_recovery_capsule_manifest_derives_assets_and_bindings() -> None:
    raw, _ = _positive_contract_vector("release-recovery-capsule")
    expected = json.loads(raw)
    transaction_raw = (FIXTURE_ROOT / "server-authorization" / "initiate.json").read_bytes()
    expected["transaction_authorization_digest"] = _sha256(transaction_raw)
    asset_bytes = {"release-authorization.json": transaction_raw}

    manifest = subject.build_recovery_capsule_manifest(
        candidate=expected["candidate"],
        transaction_authorization=transaction_raw,
        admission_authority_digest=expected["admission_authority_digest"],
        source_workflows={".github/workflows/release.yml": b"name: Release\n"},
        asset_bytes=asset_bytes,
        secret_scan={
            **expected["secret_scan"],
            "scanned_bytes": len(transaction_raw),
        },
        recovery_repository=expected["recovery_repository"],
        promotion_run_id=707,
        source_records={"capsule-input": b"{}"},
    )

    assert manifest["assets"] == [
        {
            "name": "release-authorization.json",
            "sha256": _sha256(transaction_raw),
            "size_bytes": len(transaction_raw),
            "media_type": "application/json",
        }
    ]
    assert manifest["release"]["tag"] == "recovery-707-1"
    assert subject.validate_recovery_capsule_manifest(manifest) == manifest


def test_recovery_capsule_controller_clis_are_exposed() -> None:
    help_text = subject._parser().format_help()  # noqa: SLF001
    for command in (
        "create-recovery-capsule",
        "publish-recovery-capsule",
        "verify-recovery-capsule-release",
    ):
        assert command in help_text


def test_capsule_dispatch_admission_binds_exact_transaction_run(
    tmp_path: Path,
) -> None:
    transaction_raw, _ = _positive_contract_vector("server-authorization-initiate")
    transaction = json.loads(transaction_raw)
    admission_raw, _ = _positive_contract_vector("dispatch-admission")
    admission = json.loads(admission_raw)
    run = transaction["promotion_run"]
    candidate = transaction["candidate"]
    admission.update(
        {
            "transaction_nonce": run["transaction_nonce"],
            "adopted_run_id": run["run_id"],
            "run_attempt": run["run_attempt"],
            "repository_id": run["repository_id"],
            "workflow_id": run["workflow_id"],
            "workflow_path": run["workflow_path"],
            "expected_ref": run["ref"],
            "expected_head_sha": candidate["source_sha"],
        }
    )
    normalized = _canonical(admission)
    signature = subject.sign_receipt_detached(
        receipt=normalized,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )

    subject._validate_capsule_dispatch_admission_binding(  # noqa: SLF001
        transaction,
        admission,
        signature=signature,
    )

    admission["adopted_run_id"] = int(admission["adopted_run_id"]) + 1
    with pytest.raises(ValueError, match="transaction binding"):
        subject._validate_capsule_dispatch_admission_binding(  # noqa: SLF001
            transaction,
            admission,
            signature=signature,
        )


def test_recovery_capsule_archive_is_byte_deterministic(tmp_path: Path) -> None:
    capsule = tmp_path / "capsule"
    (capsule / "nested").mkdir(parents=True)
    (capsule / "z.txt").write_bytes(b"z\n")
    (capsule / "nested" / "a.json").write_bytes(b"{}")

    first = subject.deterministic_recovery_capsule_archive(capsule)
    (capsule / "z.txt").chmod(0o600)
    second = subject.deterministic_recovery_capsule_archive(capsule)

    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == [
        "nested",
        "nested/a.json",
        "z.txt",
    ]
    assert [(member.uid, member.gid, member.mtime) for member in members] == [
        (0, 0, 0),
        (0, 0, 0),
        (0, 0, 0),
    ]
    assert [member.mode for member in members] == [0o755, 0o644, 0o644]


def test_recovery_capsule_archive_rejects_symlink(tmp_path: Path) -> None:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    target = tmp_path / "outside.txt"
    target.write_bytes(b"outside")
    (capsule / "escape").symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        subject.deterministic_recovery_capsule_archive(capsule)


def _complete_recovery_capsule_assets(
    transaction_raw: bytes,
    tmp_path: Path,
) -> tuple[dict[str, bytes], bytes]:
    wheel = b"fixture wheel bytes"
    workflows = {
        ".github/workflows/release.yml": b"name: Release\n",
        ".github/workflows/release-transaction.yml": b"name: Release transaction\n",
    }
    sandbox = b"fixture bubblewrap binary\n"
    closure_assets: dict[str, bytes] = {
        **workflows,
        ".gitleaksignore": b"fixture\n",
        "evidence/normalized-source.json": b"{}",
        "recovery/bin/bwrap": sandbox,
        "recovery/requirements.txt": b"# no third-party recovery dependencies\n",
        "recovery/wheelhouse-manifest.json": _canonical(
            {
                "schema": "kestrel.recovery_wheelhouse.v1",
                "wheels": [
                    {
                        "filename": "fixture-1.0-py3-none-any.whl",
                        "sha256": _sha256(wheel),
                        "size_bytes": len(wheel),
                    }
                ],
            }
        ),
    }
    for name in subject._RECOVERY_CAPSULE_SOURCE_ASSETS:  # noqa: SLF001
        closure_assets.setdefault(
            name,
            b"# recovery fixture\n" if name.endswith(".py") else b"{}",
        )
    for name in subject._RECOVERY_CAPSULE_SCHEMA_ASSETS:  # noqa: SLF001
        closure_assets[name] = b"{}"
    python_members = [
        {"path": name, "sha256": _sha256(raw)}
        for name, raw in sorted(closure_assets.items())
        if name.endswith(".py")
    ]
    data_resources = [
        {"path": name, "sha256": _sha256(raw)}
        for name, raw in sorted(closure_assets.items())
        if not name.endswith(".py")
    ]
    closure = _canonical(
        {
            "schema": "kestrel.recovery_execution_closure.v1",
            "python_members": python_members,
            "static_imports": [],
            "dynamic_imports": [],
            "shell_helpers": [],
            "data_resources": data_resources,
            "external_executables": [
                {
                    "name": "python",
                    "path": "/capsule/venv/bin/python",
                    "sha256": "sha256:" + "3" * 64,
                    "version": "Python 3.11.14",
                },
                {
                    "name": "sandbox",
                    "path": "/capsule/recovery/bin/bwrap",
                    "sha256": _sha256(sandbox),
                    "version": "bubblewrap fixture 1.0",
                },
            ],
            "python_runtime": {
                "implementation": "CPython",
                "version": "3.11.14",
                "abi": "cp311",
            },
            "dependency_lock": {
                "requirements_path": "recovery/requirements.txt",
                "requirements_sha256": _sha256(closure_assets["recovery/requirements.txt"]),
                "wheelhouse_manifest_sha256": _sha256(
                    closure_assets["recovery/wheelhouse-manifest.json"]
                ),
            },
            "sys_path": ["/capsule"],
            "io_roots": [{"path": "/capsule", "access": "read_write"}],
            "network_policy": {
                "default_deny": True,
                "allowed_endpoints": ["https://api.github.com"],
            },
            "evidence": {
                "source_bundle_digest": _sha256(b"fixture closure sources"),
                "canonicalization_vector_digest": (
                    "sha256:7d37d1815caf0bb822d1244edcde67b2872cac94c9a6238d036198c55d196054"
                ),
            },
            "provenance": {
                "producer": "scripts/recovery_launcher.py",
                "provider": "local",
                "method": "static-execution-closure",
            },
            "confidence": 1,
            "validation_status": "validated",
        }
    )
    transaction = json.loads(transaction_raw)
    admission_raw, _ = _positive_contract_vector("dispatch-admission")
    admission_value = json.loads(admission_raw)
    run = transaction["promotion_run"]
    candidate = transaction["candidate"]
    admission_value.update(
        {
            "transaction_nonce": run["transaction_nonce"],
            "adopted_run_id": run["run_id"],
            "run_attempt": run["run_attempt"],
            "repository_id": run["repository_id"],
            "workflow_id": run["workflow_id"],
            "workflow_path": run["workflow_path"],
            "expected_ref": run["ref"],
            "expected_head_sha": candidate["source_sha"],
        }
    )
    admission = _canonical(admission_value)
    admission_signature = subject.sign_receipt_detached(
        receipt=admission,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    admission_verification = _canonical(
        {
            "receipt_digest": _sha256(admission),
            "signature_digest": _sha256(admission_signature),
            "verification_digest": _sha256(b"verified admission inputs"),
        }
    )
    recovery_authority, _ = _positive_contract_vector("recovery-repository-authority")
    recovery_signature = subject.sign_receipt_detached(
        receipt=recovery_authority,
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    assets = {
        **closure_assets,
        "candidate-archive.tar": b"candidate",
        "dispatch-admission-verification.json": admission_verification,
        "dispatch-admission.json": admission,
        "dispatch-admission.json.sig": admission_signature,
        "owner-signing-keys-observation.json": _owner_signing_keys_observation(),
        "recovery-authority.json": recovery_authority,
        "recovery-authority.json.sig": recovery_signature,
        "recovery-execution-closure.json": closure,
        "recovery-repository-observation.json": b"{}",
        "release-authorization.json": transaction_raw,
        "recovery/wheelhouse/fixture-1.0-py3-none-any.whl": wheel,
    }
    return assets, closure


def test_recovery_capsule_requires_a_bound_sandbox_asset(tmp_path: Path) -> None:
    transaction_raw = (FIXTURE_ROOT / "server-authorization" / "initiate.json").read_bytes()
    assets, closure = _complete_recovery_capsule_assets(transaction_raw, tmp_path)
    value = json.loads(closure)
    value["external_executables"] = [
        item for item in value["external_executables"] if item["name"] != "sandbox"
    ]
    assets["recovery-execution-closure.json"] = _canonical(value)

    with pytest.raises(ValueError, match="sandbox"):
        subject._validate_capsule_execution_asset_closure(assets)  # noqa: SLF001


def _write_valid_recovery_capsule_root(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    transaction_raw = (FIXTURE_ROOT / "server-authorization" / "initiate.json").read_bytes()
    transaction = json.loads(transaction_raw)
    assets, _ = _complete_recovery_capsule_assets(transaction_raw, tmp_path)
    inventory = [
        {"name": name, "sha256": _sha256(raw), "size_bytes": len(raw)}
        for name, raw in sorted(assets.items())
    ]
    manifest = subject.build_recovery_capsule_manifest(
        candidate=transaction["candidate"],
        transaction_authorization=transaction_raw,
        admission_authority_digest=_sha256(assets["dispatch-admission.json"]),
        source_workflows={
            name: assets[name]
            for name in (
                ".github/workflows/release-transaction.yml",
                ".github/workflows/release.yml",
            )
        },
        asset_bytes=assets,
        secret_scan={
            "image": subject._GITLEAKS_IMAGE,  # noqa: SLF001
            "command": "dir --redact=100 --no-banner",
            "ignore_sha256": "sha256:" + "a" * 64,
            "inventory_sha256": _sha256(_canonical(inventory)),
            "redacted_report_sha256": "sha256:" + "c" * 64,
            "scanned_file_count": len(assets),
            "scanned_bytes": sum(len(raw) for raw in assets.values()),
            "unallowed_findings": 0,
        },
        recovery_repository={
            "full_name": "John-MiracleWorker/Kestrel-Release-Recovery",
            "id": 304,
            "authority_receipt_digest": _sha256(assets["recovery-authority.json"]),
            "authority_signature_digest": _sha256(assets["recovery-authority.json.sig"]),
        },
        promotion_run_id=707,
        source_records={"asset-inventory": _canonical(inventory)},
    )
    root = tmp_path / "capsule"
    for name, raw in assets.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (root / "recovery-capsule-manifest.json").write_bytes(_canonical(manifest))
    return root, manifest


def test_recovery_capsule_rejects_surplus_asset_outside_execution_closure(
    tmp_path: Path,
) -> None:
    transaction_raw = (FIXTURE_ROOT / "server-authorization" / "initiate.json").read_bytes()
    transaction = json.loads(transaction_raw)
    assets, _ = _complete_recovery_capsule_assets(transaction_raw, tmp_path)
    assets["evidence/surplus.json"] = b"{}"
    inventory = [
        {"name": name, "sha256": _sha256(raw), "size_bytes": len(raw)}
        for name, raw in sorted(assets.items())
    ]

    with pytest.raises(ValueError, match="capsule.*closure|closure.*asset"):
        subject.build_recovery_capsule_manifest(
            candidate=transaction["candidate"],
            transaction_authorization=transaction_raw,
            admission_authority_digest=_sha256(assets["dispatch-admission.json"]),
            source_workflows={
                name: assets[name]
                for name in (
                    ".github/workflows/release-transaction.yml",
                    ".github/workflows/release.yml",
                )
            },
            asset_bytes=assets,
            secret_scan={
                "image": subject._GITLEAKS_IMAGE,  # noqa: SLF001
                "command": "dir --redact=100 --no-banner",
                "ignore_sha256": _sha256(assets[".gitleaksignore"]),
                "inventory_sha256": _sha256(_canonical(inventory)),
                "redacted_report_sha256": "sha256:" + "c" * 64,
                "scanned_file_count": len(assets),
                "scanned_bytes": sum(len(raw) for raw in assets.values()),
                "unallowed_findings": 0,
            },
            recovery_repository={
                "full_name": "John-MiracleWorker/Kestrel-Release-Recovery",
                "id": 304,
                "authority_receipt_digest": _sha256(assets["recovery-authority.json"]),
                "authority_signature_digest": _sha256(assets["recovery-authority.json.sig"]),
            },
            promotion_run_id=707,
            source_records={"asset-inventory": _canonical(inventory)},
        )


def test_recovery_capsule_collects_hash_locked_offline_dependencies(
    tmp_path: Path,
) -> None:
    wheel = b"wheel bytes"
    requirements = b"fixture==1.0 --hash=sha256:" + b"1" * 64 + b"\n"
    manifest = _canonical(
        {
            "schema": "kestrel.recovery_wheelhouse.v1",
            "wheels": [
                {
                    "filename": "fixture-1.0-py3-none-any.whl",
                    "sha256": _sha256(wheel),
                    "size_bytes": len(wheel),
                }
            ],
        }
    )
    sandbox = b"fixture bubblewrap binary\n"
    source = tmp_path / "source"
    (source / "recovery" / "wheelhouse").mkdir(parents=True)
    (source / "recovery" / "bin").mkdir()
    (source / "recovery" / "bin" / "bwrap").write_bytes(sandbox)
    (source / "recovery" / "requirements.txt").write_bytes(requirements)
    (source / "recovery" / "wheelhouse-manifest.json").write_bytes(manifest)
    (source / "recovery" / "wheelhouse" / "fixture-1.0-py3-none-any.whl").write_bytes(wheel)
    assets: dict[str, bytes] = {}

    subject._capsule_collect_recovery_dependencies(  # noqa: SLF001
        assets,
        source_root=source,
        closure={
            "dependency_lock": {
                "requirements_path": "recovery/requirements.txt",
                "requirements_sha256": _sha256(requirements),
                "wheelhouse_manifest_sha256": _sha256(manifest),
            }
        },
    )

    assert assets == {
        "recovery/bin/bwrap": sandbox,
        "recovery/requirements.txt": requirements,
        "recovery/wheelhouse-manifest.json": manifest,
        "recovery/wheelhouse/fixture-1.0-py3-none-any.whl": wheel,
    }


def test_recovery_capsule_root_verifies_exact_manifest_inventory(
    tmp_path: Path,
) -> None:
    root, expected = _write_valid_recovery_capsule_root(tmp_path)

    manifest, raw = subject.verify_recovery_capsule_root(root)

    assert manifest == expected
    assert raw == _canonical(expected)


def test_recovery_capsule_root_requires_both_frozen_workflow_digests(
    tmp_path: Path,
) -> None:
    root, manifest = _write_valid_recovery_capsule_root(tmp_path)
    manifest["source_workflow_digests"].pop(0)  # type: ignore[union-attr]
    (root / "recovery-capsule-manifest.json").write_bytes(_canonical(manifest))

    with pytest.raises(ValueError, match="workflow.*allowlist"):
        subject.verify_recovery_capsule_root(root)


def test_recovery_capsule_root_rejects_substituted_admission_verification(
    tmp_path: Path,
) -> None:
    root, manifest = _write_valid_recovery_capsule_root(tmp_path)
    path = root / "dispatch-admission-verification.json"
    verification = json.loads(path.read_bytes())
    verification["signature_digest"] = "sha256:" + "f" * 64
    raw = _canonical(verification)
    path.write_bytes(raw)
    for asset in manifest["assets"]:  # type: ignore[union-attr]
        if asset["name"] == path.name:
            asset["sha256"] = _sha256(raw)
            asset["size_bytes"] = len(raw)
    inventory = [
        {
            "name": asset["name"],
            "sha256": asset["sha256"],
            "size_bytes": asset["size_bytes"],
        }
        for asset in manifest["assets"]  # type: ignore[union-attr]
    ]
    manifest["secret_scan"]["inventory_sha256"] = _sha256(  # type: ignore[index]
        _canonical(inventory)
    )
    (root / "recovery-capsule-manifest.json").write_bytes(_canonical(manifest))

    with pytest.raises(ValueError, match="admission verification.*binding"):
        subject.verify_recovery_capsule_root(root)


def test_recovery_capsule_root_cryptographically_verifies_embedded_admission(
    tmp_path: Path,
) -> None:
    root, manifest = _write_valid_recovery_capsule_root(tmp_path)
    signature_path = root / "dispatch-admission.json.sig"
    signature = subject.sign_receipt_detached(
        receipt=_canonical({"schema": "attacker-substitution.v1"}),
        identity_file=_known_identity_file(tmp_path),
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
    )
    signature_path.write_bytes(signature)
    verification_path = root / "dispatch-admission-verification.json"
    verification = json.loads(verification_path.read_bytes())
    verification["signature_digest"] = _sha256(signature)
    verification_path.write_bytes(_canonical(verification))
    for asset in manifest["assets"]:  # type: ignore[union-attr]
        path = root / asset["name"]
        asset["sha256"] = _sha256(path.read_bytes())
        asset["size_bytes"] = path.stat().st_size
    inventory = [
        {
            "name": asset["name"],
            "sha256": asset["sha256"],
            "size_bytes": asset["size_bytes"],
        }
        for asset in manifest["assets"]  # type: ignore[union-attr]
    ]
    manifest["secret_scan"]["inventory_sha256"] = _sha256(  # type: ignore[index]
        _canonical(inventory)
    )
    (root / "recovery-capsule-manifest.json").write_bytes(_canonical(manifest))

    with pytest.raises(ValueError, match="signature|cryptographic|admission"):
        subject.verify_recovery_capsule_root(root)


@pytest.mark.parametrize("mutation", ["tamper", "extra", "empty-directory"])
def test_recovery_capsule_root_rejects_inventory_mutants(tmp_path: Path, mutation: str) -> None:
    root, _ = _write_valid_recovery_capsule_root(tmp_path)
    if mutation == "tamper":
        (root / "release-authorization.json").write_bytes(b"{}")
    elif mutation == "extra":
        (root / "extra.txt").write_bytes(b"extra")
    else:
        (root / "empty").mkdir()

    with pytest.raises(ValueError, match="capsule.*(inventory|asset|director)"):
        subject.verify_recovery_capsule_root(root)


def _recovery_capsule_release_verification_inputs(
    tmp_path: Path,
) -> tuple[Path, dict[str, Path], str]:
    root, manifest = _write_valid_recovery_capsule_root(tmp_path)
    manifest_raw = (root / "recovery-capsule-manifest.json").read_bytes()
    archive = subject.deterministic_recovery_capsule_archive(root)
    bootstrap = (root / "scripts" / "bootstrap_recovery.py").read_bytes()
    release = manifest["release"]
    assert isinstance(release, dict)
    tag = str(release["tag"])
    repository = "John-MiracleWorker/Kestrel-Release-Recovery"
    values = {
        "publication": {
            "schema": "kestrel.recovery_capsule_publication.v1",
            "repository": repository,
            "repository_id": 304,
            "tag": tag,
            "release_id": 1707,
            "manifest_digest": _sha256(manifest_raw),
            "archive_digest": _sha256(archive),
            "immutable": True,
            "validation_status": "validated",
        },
        "repository": {
            "id": 304,
            "full_name": repository,
            "private": True,
        },
        "release": {
            "id": 1707,
            "tag_name": tag,
            "name": f"Kestrel recovery capsule {tag}",
            "body": (
                f"Kestrel recovery capsule {tag}\n\n"
                f"Kestrel-Recovery-Capsule: {_sha256(manifest_raw)}"
            ),
            "draft": False,
            "prerelease": False,
            "immutable": True,
        },
        "assets": [
            {
                "id": 1800,
                "name": "recovery-bootstrap.py",
                "size": len(bootstrap),
                "digest": _sha256(bootstrap),
            },
            {
                "id": 1801,
                "name": "recovery-capsule-manifest.json",
                "size": len(manifest_raw),
                "digest": _sha256(manifest_raw),
            },
            {
                "id": 1802,
                "name": "recovery-capsule.tar",
                "size": len(archive),
                "digest": _sha256(archive),
            },
        ],
    }
    paths: dict[str, Path] = {}
    for name, value in values.items():
        path = tmp_path / f"{name}.json"
        if name == "publication":
            path.write_bytes(_canonical(value))
        else:
            path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        paths[name] = path
    pinned_gh = tmp_path / "gh"
    pinned_gh.write_bytes(b"pinned-gh-2.97.0")
    pinned_gh.chmod(0o700)
    paths["pinned_gh"] = pinned_gh
    paths["output"] = tmp_path / "capsule-verification.json"
    return root, paths, tag


def test_verify_recovery_capsule_release_accepts_external_json_and_runs_pinned_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, paths, tag = _recovery_capsule_release_verification_inputs(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        stdout = b"gh version 2.97.0 (2026-02-26)\n" if command[1:] == ["--version"] else b""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(subject.subprocess, "run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "test-token")
    _trust_test_gh_binary(paths["pinned_gh"], monkeypatch)

    assert (
        subject.main(
            [
                "verify-recovery-capsule-release",
                str(root),
                "--publication-receipt",
                str(paths["publication"]),
                "--fresh-repository-observation",
                str(paths["repository"]),
                "--fresh-release-observation",
                str(paths["release"]),
                "--fresh-assets-observation",
                str(paths["assets"]),
                "--pinned-gh",
                str(paths["pinned_gh"]),
                "--output",
                str(paths["output"]),
            ]
        )
        == 0
    )

    repository = "John-MiracleWorker/Kestrel-Release-Recovery"
    assert calls == [
        [str(paths["pinned_gh"]), "--version"],
        [str(paths["pinned_gh"]), "release", "verify", tag, "--repo", repository],
        [
            str(paths["pinned_gh"]),
            "release",
            "verify-asset",
            tag,
            "recovery-bootstrap.py",
            "--repo",
            repository,
        ],
        [
            str(paths["pinned_gh"]),
            "release",
            "verify-asset",
            tag,
            "recovery-capsule-manifest.json",
            "--repo",
            repository,
        ],
        [
            str(paths["pinned_gh"]),
            "release",
            "verify-asset",
            tag,
            "recovery-capsule.tar",
            "--repo",
            repository,
        ],
    ]
    assert json.loads(paths["output"].read_bytes())["verified"] is True


def test_verify_recovery_capsule_release_accepts_exact_offline_gh_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, paths, tag = _recovery_capsule_release_verification_inputs(tmp_path)
    platform_digest = "sha256:" + "d" * 64
    monkeypatch.setitem(
        subject.PINNED_GH_BINARY_DIGESTS,
        (subject.sys.platform, subject.platform.machine()),
        platform_digest,
    )
    result_specs = [
        ("release:verify", "release-attestation.json"),
        (
            "release:verify-asset:recovery-bootstrap.py",
            "recovery-bootstrap.py.attestation.json",
        ),
        (
            "release:verify-asset:recovery-capsule-manifest.json",
            "recovery-capsule-manifest.json.attestation.json",
        ),
        (
            "release:verify-asset:recovery-capsule.tar",
            "recovery-capsule.tar.attestation.json",
        ),
    ]
    results = []
    for operation, name in result_specs:
        raw = _canonical({"operation": operation, "verified": True})
        (tmp_path / name).write_bytes(raw)
        results.append(
            {
                "operation": operation,
                "path": name,
                "sha256": _sha256(raw),
                "size_bytes": len(raw),
            }
        )
    observation = _canonical(
        {
            "schema": "kestrel.pinned_gh_release_verification.v1",
            "pinned_gh_digest": platform_digest,
            "pinned_gh_version": subject.PINNED_GH_VERSION_LINE.decode("ascii"),
            "repository": "John-MiracleWorker/Kestrel-Release-Recovery",
            "tag": tag,
            "results": results,
            "verified": True,
            "validation_status": "validated",
        }
    )
    observation_path = tmp_path / "pinned-gh-verification.json"
    observation_path.write_bytes(observation)
    monkeypatch.setattr(
        subject.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("offline capsule verification attempted a subprocess")
        ),
    )

    assert (
        subject.main(
            [
                "verify-recovery-capsule-release",
                str(root),
                "--publication-receipt",
                str(paths["publication"]),
                "--fresh-repository-observation",
                str(paths["repository"]),
                "--fresh-release-observation",
                str(paths["release"]),
                "--fresh-assets-observation",
                str(paths["assets"]),
                "--pinned-gh-verification-observation",
                str(observation_path),
                "--output",
                str(paths["output"]),
            ]
        )
        == 0
    )

    result = json.loads(paths["output"].read_bytes())
    assert result["pinned_gh_digest"] == platform_digest
    assert result["pinned_gh_verification_observation_digest"] == _sha256(observation)


def test_verify_recovery_capsule_release_rejects_incomplete_offline_gh_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, paths, tag = _recovery_capsule_release_verification_inputs(tmp_path)
    platform_digest = "sha256:" + "d" * 64
    monkeypatch.setitem(
        subject.PINNED_GH_BINARY_DIGESTS,
        (subject.sys.platform, subject.platform.machine()),
        platform_digest,
    )
    observation_path = tmp_path / "pinned-gh-verification.json"
    release_result = _canonical({"operation": "release:verify", "verified": True})
    (tmp_path / "release-attestation.json").write_bytes(release_result)
    observation_path.write_bytes(
        _canonical(
            {
                "schema": "kestrel.pinned_gh_release_verification.v1",
                "pinned_gh_digest": platform_digest,
                "pinned_gh_version": subject.PINNED_GH_VERSION_LINE.decode("ascii"),
                "repository": "John-MiracleWorker/Kestrel-Release-Recovery",
                "tag": tag,
                "results": [
                    {
                        "operation": "release:verify",
                        "path": "release-attestation.json",
                        "sha256": _sha256(release_result),
                        "size_bytes": len(release_result),
                    }
                ],
                "verified": True,
                "validation_status": "validated",
            }
        )
    )

    assert (
        subject.main(
            [
                "verify-recovery-capsule-release",
                str(root),
                "--publication-receipt",
                str(paths["publication"]),
                "--fresh-repository-observation",
                str(paths["repository"]),
                "--fresh-release-observation",
                str(paths["release"]),
                "--fresh-assets-observation",
                str(paths["assets"]),
                "--pinned-gh-verification-observation",
                str(observation_path),
                "--output",
                str(paths["output"]),
            ]
        )
        == 1
    )
    assert not paths["output"].exists()


def test_verify_recovery_capsule_release_rejects_tampered_offline_gh_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, paths, tag = _recovery_capsule_release_verification_inputs(tmp_path)
    platform_digest = "sha256:" + "d" * 64
    monkeypatch.setitem(
        subject.PINNED_GH_BINARY_DIGESTS,
        (subject.sys.platform, subject.platform.machine()),
        platform_digest,
    )
    specs = [
        ("release:verify", "release-attestation.json"),
        (
            "release:verify-asset:recovery-bootstrap.py",
            "recovery-bootstrap.py.attestation.json",
        ),
        (
            "release:verify-asset:recovery-capsule-manifest.json",
            "recovery-capsule-manifest.json.attestation.json",
        ),
        (
            "release:verify-asset:recovery-capsule.tar",
            "recovery-capsule.tar.attestation.json",
        ),
    ]
    results = []
    for operation, name in specs:
        raw = _canonical({"operation": operation, "verified": True})
        (tmp_path / name).write_bytes(raw)
        results.append(
            {
                "operation": operation,
                "path": name,
                "sha256": _sha256(raw),
                "size_bytes": len(raw),
            }
        )
    observation_path = tmp_path / "pinned-gh-verification.json"
    observation_path.write_bytes(
        _canonical(
            {
                "schema": "kestrel.pinned_gh_release_verification.v1",
                "pinned_gh_digest": platform_digest,
                "pinned_gh_version": subject.PINNED_GH_VERSION_LINE.decode("ascii"),
                "repository": "John-MiracleWorker/Kestrel-Release-Recovery",
                "tag": tag,
                "results": results,
                "verified": True,
                "validation_status": "validated",
            }
        )
    )
    (tmp_path / "recovery-capsule.tar.attestation.json").write_bytes(b"{}")

    assert (
        subject.main(
            [
                "verify-recovery-capsule-release",
                str(root),
                "--publication-receipt",
                str(paths["publication"]),
                "--fresh-repository-observation",
                str(paths["repository"]),
                "--fresh-release-observation",
                str(paths["release"]),
                "--fresh-assets-observation",
                str(paths["assets"]),
                "--pinned-gh-verification-observation",
                str(observation_path),
                "--output",
                str(paths["output"]),
            ]
        )
        == 1
    )
    assert not paths["output"].exists()


def test_verify_recovery_capsule_release_rejects_duplicate_remote_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, paths, _ = _recovery_capsule_release_verification_inputs(tmp_path)
    assets = json.loads(paths["assets"].read_bytes())
    assets.append(dict(assets[0]))
    paths["assets"].write_text(json.dumps(assets), encoding="utf-8")
    monkeypatch.setattr(
        subject.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=b"gh version 2.97.0 (2026-02-26)\n",
            stderr=b"",
        ),
    )
    monkeypatch.setenv("GH_TOKEN", "test-token")
    _trust_test_gh_binary(paths["pinned_gh"], monkeypatch)

    assert (
        subject.main(
            [
                "verify-recovery-capsule-release",
                str(root),
                "--publication-receipt",
                str(paths["publication"]),
                "--fresh-repository-observation",
                str(paths["repository"]),
                "--fresh-release-observation",
                str(paths["release"]),
                "--fresh-assets-observation",
                str(paths["assets"]),
                "--pinned-gh",
                str(paths["pinned_gh"]),
                "--output",
                str(paths["output"]),
            ]
        )
        == 1
    )
    assert not paths["output"].exists()
