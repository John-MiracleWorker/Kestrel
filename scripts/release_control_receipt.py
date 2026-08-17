#!/usr/bin/env python3
"""Canonical, fail-closed release-control receipt primitives.

This module owns the shared JCS/I-JSON, source-envelope, freshness, signature,
create-once, and independently durable recovery-capsule policies used by the
S2 release transaction. Product publication behavior lives in
``release_promotion_transaction.py``.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import math
import os
import platform
import re
import secrets
import stat
import struct
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import NoReturn, TypeAlias, cast

import jsonschema  # type: ignore[import-untyped]
import rfc8785
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ROOT = Path(__file__).resolve().parents[1]
CANONICALIZATION_FIXTURE = (
    ROOT / "tests" / "fixtures" / "release-control" / "v3" / "canonicalization-vectors.json"
)
CANONICALIZATION_VECTOR_DIGEST = (
    "sha256:7d37d1815caf0bb822d1244edcde67b2872cac94c9a6238d036198c55d196054"
)
SOURCE_OBSERVATION_SCHEMA = "kestrel.source_observation.v1"
SOURCE_REGISTRY_SCHEMA = "kestrel.source_registry.v1"
SOURCE_REGISTRY_PATH = ROOT / "release-control-source-registry.json"
CREDENTIAL_SCOPE_SCHEMA = "kestrel.credential_scope_authority.v1"
RUNTIME_CREDENTIAL_SCHEMA = "kestrel.runtime_credential_verification.v1"
CREDENTIAL_POLICY_SCHEMA = "kestrel.release_control_credential_policy.v1"
CREDENTIAL_POLICY_PATH = ROOT / "release-control-credential-policy.json"
SCHEMA_ROOT = ROOT / "schemas"
SIGNING_NAMESPACE = "kestrel-release-control-v1"
SIGNING_PRINCIPAL = "John-MiracleWorker"
OWNER_SIGNING_KEYS_LOCATOR = "GET /users/John-MiracleWorker/ssh_signing_keys?per_page=100"
DISPATCH_TRANSACTION_SCHEMA = "kestrel.release_dispatch_transaction.v1"
DISPATCH_INTENT_SCHEMA = "kestrel.release_dispatch_intent.v2"
DISPATCH_RECONCILIATION_SCHEMA = "kestrel.release_dispatch_reconciliation.v1"
DISPATCH_IDENTITY_SCHEMA = "kestrel.dispatch_identity.v1"
DISPATCH_ADMISSION_SCHEMA = "kestrel.dispatch_admission.v1"
DISPATCH_TOMBSTONE_SCHEMA = "kestrel.dispatch_tombstone.v1"
WRITER_INVENTORY_SCHEMA = "kestrel.repository_writer_inventory.v1"
GITHUB_AUTHORITY_SCHEMA = "kestrel.github_release_authority.v3"
PYPI_AUTHORITY_SCHEMA = "kestrel.pypi_upload_authority_prerequisite.v3"
RECOVERY_AUTHORITY_SCHEMA = "kestrel.recovery_repository_authority.v1"
RECOVERY_CAPSULE_SCHEMA = "kestrel.release_recovery_capsule.v1"
DISPATCH_API_VERSION = "2026-03-10"
DISPATCH_RESPONSE_CONTRACT = "api-2026-03-10-always-run-details"
DISPATCH_WORKFLOW_PATH = ".github/workflows/release.yml"
DISPATCH_RECONCILIATION_SECONDS = 600
PINNED_GH_VERSION_LINE = b"gh version 2.97.0 (2026-02-26)"
PINNED_GH_BINARY_DIGESTS = {
    ("darwin", "arm64"): (
        "sha256:0d17dddf96bcc1dc50f3420a064d593d64016b0be16286a6c26121f2a5cb8316"
    ),
    ("linux", "x86_64"): (
        "sha256:141507c337e8b202ad398550c3b73d72f5af92e86f71665214538a81efd4c409"
    ),
}

MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_SOURCE_ENVELOPES = 4096
MAX_SOURCE_NAME_BYTES = 4096
MAX_SOURCE_BODY_BYTES = 64 * 1024 * 1024
MAX_SOURCE_ENVELOPE_BYTES = 4 * ((MAX_SOURCE_BODY_BYTES + 2) // 3) + 256 * 1024
MAX_REGISTRY_ENTRIES = 4096
MAX_REGISTRY_STRING_BYTES = 16 * 1024
CURRENT_CAPTURE_WINDOW_SECONDS = 120
RECEIPT_LIFETIME_SECONDS = 300

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CREDENTIAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
SSH_PUBLIC_KEY_RE = re.compile(
    r"^(ssh-ed25519) ([A-Za-z0-9+/]+={0,2})(?: [A-Za-z0-9@._+:/=-]{1,256})?$"
)
NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)$")

JSONScalar: TypeAlias = None | bool | int | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]

REGISTRY_ENTRY_KEYS = frozenset(
    {
        "receipt_schema",
        "phase",
        "mode",
        "name",
        "provider",
        "locator",
        "authentication_mode",
        "body_mode",
        "count_mode",
        "freshness_class",
    }
)
SOURCE_OBSERVATION_KEYS = frozenset(
    {
        "schema",
        "name",
        "provider",
        "locator",
        "authenticated_as",
        "freshness_class",
        "captured_at",
        "page_count",
        "record_count",
        "complete",
        "body_encoding",
        "body",
    }
)
PROVIDERS = frozenset({"github.com", "pypi.org", "controller", "local"})
AUTHENTICATION_MODES = frozenset(
    {"github-owner", "github-actions-run", "pypi-public", "controller-owner", "local"}
)
BODY_MODES = frozenset({"singleton-json", "singleton-bytes", "paginated-json"})
COUNT_MODES = frozenset({"one", "top-level-array", "sum-page-array", "zero"})
FRESHNESS_CLASSES = frozenset({"current", "historical"})
CREDENTIAL_PURPOSES = frozenset(
    {
        "hosted_smoke_dispatch",
        "hosted_smoke_read",
        "promotion_dispatcher",
        "promotion_reconciliation_reader",
        "recovery_reader",
        "release_guard",
    }
)
READ_ONLY_CREDENTIAL_PURPOSES = frozenset(
    {
        "hosted_smoke_read",
        "promotion_reconciliation_reader",
        "recovery_reader",
        "release_guard",
    }
)


class ReleaseControlError(ValueError):
    """A stable, fail-closed release-control validation error."""


class DispatchReconciliationPending(ReleaseControlError):
    """The durable reconciliation window is still open."""


class _ReleaseControlArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ReleaseControlError(f"release-control CLI is invalid: {message}")


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _validate_string(
    value: object,
    *,
    label: str,
    allow_empty: bool = False,
    max_bytes: int = MAX_REGISTRY_STRING_BYTES,
) -> str:
    if type(value) is not str:
        raise ReleaseControlError(f"{label} must be a string")
    if not value and not allow_empty:
        raise ReleaseControlError(f"{label} must not be empty")
    if unicodedata.normalize("NFC", value) != value:
        raise ReleaseControlError(f"{label} must be NFC-normalized")
    if any(ord(character) < 32 for character in value):
        raise ReleaseControlError(f"{label} contains a forbidden control character")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReleaseControlError(f"{label} is not valid UTF-8") from exc
    if len(encoded) > max_bytes:
        raise ReleaseControlError(f"{label} exceeds its encoded size limit")
    return value


def _validate_ijson(
    value: object,
    *,
    label: str = "JSON",
    max_string_bytes: int = MAX_REGISTRY_STRING_BYTES,
) -> JSONValue:
    if value is None or type(value) is bool:
        return cast(JSONScalar, value)
    if type(value) is int:
        integer = value
        if not -MAX_SAFE_INTEGER <= integer <= MAX_SAFE_INTEGER:
            raise ReleaseControlError(f"{label} integer is outside the I-JSON safe range")
        return integer
    if type(value) is float:
        raise ReleaseControlError(f"{label} floats are forbidden")
    if type(value) is str:
        return _validate_string(
            value,
            label=label,
            allow_empty=True,
            max_bytes=max_string_bytes,
        )
    if type(value) is list:
        return [
            _validate_ijson(
                item,
                label=f"{label}[{index}]",
                max_string_bytes=max_string_bytes,
            )
            for index, item in enumerate(cast(list[object], value))
        ]
    if type(value) is dict:
        result: JSONObject = {}
        is_source_envelope = value.get("schema") == SOURCE_OBSERVATION_SCHEMA
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ReleaseControlError(f"{label} has a non-string object key")
            checked_key = _validate_string(key, label=f"{label} key", allow_empty=True)
            child_limit = (
                4 * ((MAX_SOURCE_BODY_BYTES + 2) // 3)
                if is_source_envelope and checked_key == "body"
                else max_string_bytes
            )
            result[checked_key] = _validate_ijson(
                item,
                label=f"{label}.{checked_key}",
                max_string_bytes=child_limit,
            )
        return result
    raise ReleaseControlError(f"{label} contains an unsupported JSON type")


def _validate_external_ijson(value: object, *, label: str) -> JSONValue:
    """Validate untrusted API JSON while permitting escaped controls in values."""

    if value is None or type(value) is bool:
        return cast(JSONScalar, value)
    if type(value) is int:
        integer = value
        if not -MAX_SAFE_INTEGER <= integer <= MAX_SAFE_INTEGER:
            raise ReleaseControlError(f"{label} integer is outside the I-JSON safe range")
        return integer
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ReleaseControlError(f"{label} must be NFC-normalized")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ReleaseControlError(f"{label} is not valid UTF-8") from exc
        return value
    if type(value) is list:
        return [
            _validate_external_ijson(item, label=f"{label}[{index}]")
            for index, item in enumerate(cast(list[object], value))
        ]
    if type(value) is dict:
        result: JSONObject = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ReleaseControlError(f"{label} has a non-string object key")
            checked_key = _validate_string(key, label=f"{label} key", allow_empty=True)
            result[checked_key] = _validate_external_ijson(item, label=f"{label}.{checked_key}")
        return result
    raise ReleaseControlError(f"{label} contains an unsupported JSON type")


def canonical_json_bytes(value: object) -> bytes:
    """Return RFC 8785 bytes after enforcing Kestrel's stricter I-JSON profile."""

    checked = _validate_ijson(value)
    try:
        return rfc8785.dumps(checked)
    except (rfc8785.CanonicalizationError, UnicodeError, ValueError, TypeError) as exc:
        raise ReleaseControlError(f"canonical JSON encoding failed: {exc}") from exc


def canonical_external_json_bytes(value: object) -> bytes:
    """Encode API JSON canonically without applying receipt-string policy."""

    checked = _validate_external_ijson(value, label="external JSON")
    try:
        return rfc8785.dumps(checked)
    except (rfc8785.CanonicalizationError, UnicodeError, ValueError, TypeError) as exc:
        raise ReleaseControlError(f"external JSON encoding failed: {exc}") from exc


def _reject_constant(value: str) -> NoReturn:
    raise ReleaseControlError(f"non-I-JSON numeric constant is forbidden: {value}")


def _reject_float(value: str) -> NoReturn:
    raise ReleaseControlError(f"floats are forbidden: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ReleaseControlError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def parse_ijson_bytes(raw: bytes, *, label: str) -> JSONValue:
    """Parse one UTF-8 I-JSON document without requiring canonical wire bytes."""

    if not raw:
        raise ReleaseControlError(f"{label} is empty")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ReleaseControlError(f"{label} must not contain a UTF-8 BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseControlError(f"{label} is not UTF-8") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
        )
    except (json.JSONDecodeError, ReleaseControlError) as exc:
        raise ReleaseControlError(f"{label} is not strict JSON: {exc}") from exc
    return _validate_ijson(parsed, label=label)


def parse_external_json_bytes(raw: bytes, *, label: str) -> JSONValue:
    """Parse strict API I-JSON whose values may contain escaped controls."""

    if not raw:
        raise ReleaseControlError(f"{label} is empty")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ReleaseControlError(f"{label} must not contain a UTF-8 BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseControlError(f"{label} is not UTF-8") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
        )
    except (json.JSONDecodeError, ReleaseControlError) as exc:
        raise ReleaseControlError(f"{label} is not strict JSON: {exc}") from exc
    return _validate_external_ijson(parsed, label=label)


def strict_canonical_json(raw: bytes, *, label: str) -> JSONValue:
    """Parse one exact no-whitespace canonical I-JSON value."""

    checked = parse_ijson_bytes(raw, label=label)
    if canonical_json_bytes(checked) != raw:
        raise ReleaseControlError(f"{label} is not canonical RFC 8785 JSON")
    return checked


def _object(value: object, *, label: str) -> JSONObject:
    if type(value) is not dict:
        raise ReleaseControlError(f"{label} must be an object")
    return cast(JSONObject, value)


def _array(value: object, *, label: str) -> list[JSONValue]:
    if type(value) is not list:
        raise ReleaseControlError(f"{label} must be an array")
    return cast(list[JSONValue], value)


def _safe_integer(value: object, *, label: str, positive: bool = False) -> int:
    if type(value) is not int:
        raise ReleaseControlError(f"{label} must be an integer")
    result = value
    minimum = 1 if positive else 0
    if result < minimum or result > MAX_SAFE_INTEGER:
        raise ReleaseControlError(f"{label} is outside its accepted range")
    return result


def _format_timestamp(value: datetime, *, label: str) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ReleaseControlError(f"{label} must be an aware UTC datetime")
    normalized = value.astimezone(UTC).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: object, *, label: str) -> datetime:
    checked = _validate_string(value, label=label)
    if TIMESTAMP_RE.fullmatch(checked) is None:
        raise ReleaseControlError(f"{label} must be whole-second UTC RFC 3339 ending in Z")
    try:
        return datetime.strptime(checked, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ReleaseControlError(f"{label} is not a real UTC timestamp") from exc


def canonicalization_vector_digest() -> str:
    raw = _read_regular(
        CANONICALIZATION_FIXTURE,
        label="canonicalization vector fixture",
        max_bytes=443,
    )
    strict_canonical_json(raw, label="canonicalization vector fixture")
    if len(raw) != 443 or _sha256(raw) != CANONICALIZATION_VECTOR_DIGEST:
        raise ReleaseControlError("canonicalization vector fixture identity mismatch")
    return CANONICALIZATION_VECTOR_DIGEST


def _bounded_source_items(
    sources: Mapping[str, bytes] | Iterable[tuple[str, bytes]],
) -> list[tuple[str, bytes]]:
    iterator: Iterable[tuple[str, bytes]]
    if isinstance(sources, Mapping):
        iterator = ((name, sources[name]) for name in sources)
    else:
        iterator = sources
    copied: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for count, item in enumerate(iterator, start=1):
        if count > MAX_SOURCE_ENVELOPES:
            raise ReleaseControlError("source bundle has too many entries")
        try:
            name, raw = item
        except (TypeError, ValueError) as exc:
            raise ReleaseControlError("source bundle item must be a name/bytes pair") from exc
        checked_name = _validate_string(name, label="source bundle name")
        encoded = checked_name.encode("utf-8")
        if len(encoded) > MAX_SOURCE_NAME_BYTES:
            raise ReleaseControlError("source bundle name exceeds its size limit")
        if checked_name in seen:
            raise ReleaseControlError(f"duplicate source bundle name: {checked_name}")
        if type(raw) is not bytes:
            raise ReleaseControlError(f"source bundle {checked_name} must be exact bytes")
        copied_raw = bytes(raw)
        if len(copied_raw) > MAX_SOURCE_ENVELOPE_BYTES:
            raise ReleaseControlError(f"source bundle {checked_name} exceeds its size limit")
        seen.add(checked_name)
        copied.append((checked_name, copied_raw))
    return copied


def source_bundle_digest(
    sources: Mapping[str, bytes] | Iterable[tuple[str, bytes]],
) -> str:
    """Digest exact source envelopes with the normative length framing."""

    digest = hashlib.sha256()
    digest.update(b"Kestrel-Source-Bundle-v1\0")
    for name, raw in sorted(
        _bounded_source_items(sources), key=lambda item: item[0].encode("utf-8")
    ):
        encoded = name.encode("utf-8")
        if len(encoded) > 0xFFFFFFFF or len(raw) > 0xFFFFFFFFFFFFFFFF:
            raise ReleaseControlError("source bundle framing length is out of range")
        digest.update(struct.pack(">I", len(encoded)))
        digest.update(encoded)
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
    return "sha256:" + digest.hexdigest()


def _registry_sort_key(entry: Mapping[str, object]) -> tuple[str, str, str, str]:
    return (
        str(entry.get("receipt_schema", "")),
        "" if entry.get("phase") is None else str(entry.get("phase")),
        "" if entry.get("mode") is None else str(entry.get("mode")),
        str(entry.get("name", "")),
    )


def _validate_registry(registry: Mapping[str, object]) -> list[JSONObject]:
    if type(registry) is not dict or set(registry) != {"schema", "entries"}:
        raise ReleaseControlError("source registry fields mismatch")
    if registry.get("schema") != SOURCE_REGISTRY_SCHEMA:
        raise ReleaseControlError("source registry schema mismatch")
    raw_entries = registry.get("entries")
    if type(raw_entries) is not list:
        raise ReleaseControlError("source registry entries must be an array")
    entries = cast(list[object], raw_entries)
    if len(entries) > MAX_REGISTRY_ENTRIES:
        raise ReleaseControlError("source registry has too many entries")
    checked: list[JSONObject] = []
    identities: set[tuple[str, str, str, str]] = set()
    for raw_entry in entries:
        entry = _object(raw_entry, label="source registry entry")
        if set(entry) != REGISTRY_ENTRY_KEYS:
            raise ReleaseControlError("source registry entry fields mismatch")
        for field in ("receipt_schema", "name", "locator"):
            _validate_string(entry.get(field), label=f"source registry {field}")
        for nullable in ("phase", "mode"):
            value = entry.get(nullable)
            if value is not None:
                _validate_string(value, label=f"source registry {nullable}")
        if entry.get("provider") not in PROVIDERS:
            raise ReleaseControlError("source registry provider is invalid")
        if entry.get("authentication_mode") not in AUTHENTICATION_MODES:
            raise ReleaseControlError("source registry authentication mode is invalid")
        if entry.get("body_mode") not in BODY_MODES:
            raise ReleaseControlError("source registry body mode is invalid")
        if entry.get("count_mode") not in COUNT_MODES:
            raise ReleaseControlError("source registry count mode is invalid")
        if entry.get("freshness_class") not in FRESHNESS_CLASSES:
            raise ReleaseControlError("source registry freshness class is invalid")
        identity = _registry_sort_key(entry)
        if identity in identities:
            raise ReleaseControlError("duplicate source registry entry")
        identities.add(identity)
        checked.append(entry)
    if checked != sorted(checked, key=_registry_sort_key):
        raise ReleaseControlError("source registry entries are not sorted")
    return checked


def _identity_login(raw: bytes, *, label: str) -> str:
    observation = _object(strict_canonical_json(raw, label=label), label=label)
    return _validate_string(observation.get("login"), label=f"{label} login")


def _authenticated_as(
    authentication_mode: object, identity_observation: bytes | None
) -> str | None:
    if authentication_mode in {"github-owner", "controller-owner"}:
        if identity_observation is None:
            raise ReleaseControlError("source identity observation is required")
        return _identity_login(identity_observation, label="source identity observation")
    if authentication_mode == "github-actions-run":
        if identity_observation is None:
            raise ReleaseControlError("source identity observation is required")
        context = _object(
            strict_canonical_json(identity_observation, label="Actions identity observation"),
            label="Actions identity observation",
        )
        repository_id = _safe_integer(
            context.get("repository_id"), label="Actions repository ID", positive=True
        )
        run_id = _safe_integer(context.get("run_id"), label="Actions run ID", positive=True)
        return f"github-actions:{repository_id}:{run_id}"
    if authentication_mode in {"pypi-public", "local"}:
        if identity_observation is not None:
            raise ReleaseControlError("source identity observation is forbidden")
        return None
    raise ReleaseControlError("source authentication mode is invalid")


def _pagination_next_link(headers: object, *, label: str) -> str | None:
    values = _array(headers, label=f"{label} response headers")
    link_value: str | None = None
    for raw_header in values:
        header = _array(raw_header, label=f"{label} response header")
        if len(header) != 2:
            raise ReleaseControlError(f"{label} response header shape is invalid")
        name = _validate_string(header[0], label=f"{label} response header name")
        value = _validate_string(header[1], label=f"{label} response header value")
        if name.lower() == "link":
            if link_value is not None:
                raise ReleaseControlError(f"{label} has duplicate Link headers")
            link_value = value
    if link_value is None:
        return None
    next_links: list[str] = []
    for part in link_value.split(","):
        match = re.fullmatch(
            r'\s*<([^<>]+)>\s*;\s*rel="([A-Za-z0-9 -]+)"\s*',
            part,
        )
        if match is None:
            raise ReleaseControlError(f"{label} Link header is invalid")
        if "next" in match.group(2).split():
            next_links.append(match.group(1))
    if len(next_links) > 1:
        raise ReleaseControlError(f"{label} has multiple next Link targets")
    return next_links[0] if next_links else None


def _paginated_bodies(parsed: JSONValue, *, locator: object) -> list[JSONValue]:
    wrapper = _object(parsed, label="paginated source wrapper")
    if set(wrapper) != {"pages"}:
        raise ReleaseControlError("paginated source wrapper fields mismatch")
    raw_pages = _array(wrapper.get("pages"), label="paginated source pages")
    if not raw_pages or len(raw_pages) > MAX_SOURCE_ENVELOPES:
        raise ReleaseControlError("paginated source page cardinality is invalid")
    expected_url = _validate_string(locator, label="paginated source locator")
    seen_urls: set[str] = set()
    bodies: list[JSONValue] = []
    for number, raw_page in enumerate(raw_pages, start=1):
        page = _object(raw_page, label="paginated source page")
        _require_exact_fields(
            page,
            frozenset({"number", "request_url", "response_headers", "body"}),
            label="paginated source page",
        )
        if page.get("number") != number:
            raise ReleaseControlError("paginated source pages are out of order")
        request_url = _validate_string(
            page.get("request_url"), label="paginated source request URL"
        )
        if request_url != expected_url:
            raise ReleaseControlError("paginated source request URL does not follow the Link chain")
        if request_url in seen_urls:
            raise ReleaseControlError("paginated source Link chain loops")
        seen_urls.add(request_url)
        body = _array(page.get("body"), label="paginated source page body")
        bodies.append(cast(JSONValue, body))
        next_url = _pagination_next_link(
            page.get("response_headers"), label=f"paginated source page {number}"
        )
        is_final = number == len(raw_pages)
        if is_final:
            if next_url is not None:
                raise ReleaseControlError("paginated source final page has an unconsumed next Link")
        else:
            if next_url is None:
                raise ReleaseControlError("paginated source terminated before all supplied pages")
            if next_url in seen_urls:
                raise ReleaseControlError("paginated source Link chain loops")
            expected_url = next_url
    return bodies


def _body_counts(
    raw: bytes,
    *,
    body_mode: object,
    count_mode: object,
    locator: object,
) -> tuple[int, int]:
    if len(raw) > MAX_SOURCE_BODY_BYTES:
        raise ReleaseControlError("source raw input exceeds its size limit")
    parsed: JSONValue | None = None
    pages: list[JSONValue] | None = None
    if body_mode in {"singleton-json", "paginated-json"}:
        parsed = parse_external_json_bytes(raw, label="source raw input")
    if body_mode == "paginated-json":
        if parsed is None:  # pragma: no cover - assigned for paginated JSON above
            raise ReleaseControlError("paginated source body is missing")
        pages = _paginated_bodies(parsed, locator=locator)
        page_count = len(pages)
    elif body_mode in {"singleton-json", "singleton-bytes"}:
        page_count = 1
    else:
        raise ReleaseControlError("source body mode is invalid")
    if count_mode == "one":
        record_count = 1
    elif count_mode == "zero":
        record_count = 0
    elif count_mode == "top-level-array":
        record_count = len(_array(parsed, label="source top-level array"))
    elif count_mode == "sum-page-array":
        if pages is None:
            raise ReleaseControlError("sum-page-array requires paginated source body")
        record_count = sum(len(_array(page, label="paginated source page")) for page in pages)
    else:
        raise ReleaseControlError("source count mode is invalid")
    _safe_integer(page_count, label="source page count")
    _safe_integer(record_count, label="source record count")
    return page_count, record_count


def capture_source(
    *,
    registry: Mapping[str, object],
    receipt_schema: str,
    phase: str | None,
    mode: str | None,
    name: str,
    raw_input: bytes,
    identity_observation: bytes | None = None,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    """Wrap exact input bytes using metadata selected only from the registry."""

    checked_schema = _validate_string(receipt_schema, label="receipt schema")
    checked_name = _validate_string(name, label="source name")
    entries = _validate_registry(registry)
    matches = [
        entry
        for entry in entries
        if entry.get("receipt_schema") == checked_schema
        and entry.get("phase") == phase
        and entry.get("mode") == mode
        and entry.get("name") == checked_name
    ]
    if len(matches) != 1:
        raise ReleaseControlError("source registry lookup must select exactly one entry")
    entry = matches[0]
    page_count, record_count = _body_counts(
        raw_input,
        body_mode=entry.get("body_mode"),
        count_mode=entry.get("count_mode"),
        locator=entry.get("locator"),
    )
    authenticated_as = _authenticated_as(entry.get("authentication_mode"), identity_observation)
    captured_at = _format_timestamp(_clock(), label="source capture clock")
    envelope: JSONObject = {
        "schema": SOURCE_OBSERVATION_SCHEMA,
        "name": checked_name,
        "provider": cast(str, entry["provider"]),
        "locator": cast(str, entry["locator"]),
        "authenticated_as": authenticated_as,
        "freshness_class": cast(str, entry["freshness_class"]),
        "captured_at": captured_at,
        "page_count": page_count,
        "record_count": record_count,
        "complete": True,
        "body_encoding": "base64",
        "body": base64.b64encode(bytes(raw_input)).decode("ascii"),
    }
    _validate_source_envelope(envelope)
    return envelope


def _validate_source_envelope(value: Mapping[str, object]) -> JSONObject:
    if type(value) is not dict or set(value) != SOURCE_OBSERVATION_KEYS:
        raise ReleaseControlError("source observation fields mismatch")
    if value.get("schema") != SOURCE_OBSERVATION_SCHEMA:
        raise ReleaseControlError("source observation schema mismatch")
    for field in ("name", "locator"):
        _validate_string(value.get(field), label=f"source observation {field}")
    if value.get("provider") not in PROVIDERS:
        raise ReleaseControlError("source observation provider is invalid")
    authenticated_as = value.get("authenticated_as")
    if authenticated_as is not None:
        _validate_string(authenticated_as, label="source observation authenticated_as")
    if value.get("freshness_class") not in FRESHNESS_CLASSES:
        raise ReleaseControlError("source observation freshness class is invalid")
    parse_timestamp(value.get("captured_at"), label="source observation captured_at")
    _safe_integer(value.get("page_count"), label="source observation page_count")
    _safe_integer(value.get("record_count"), label="source observation record_count")
    if value.get("complete") is not True:
        raise ReleaseControlError("source observation must be complete")
    if value.get("body_encoding") != "base64":
        raise ReleaseControlError("source observation body encoding mismatch")
    encoded = value.get("body")
    if type(encoded) is not str:
        raise ReleaseControlError("source observation body must be a string")
    maximum_encoded_bytes = 4 * ((MAX_SOURCE_BODY_BYTES + 2) // 3)
    if len(encoded) > maximum_encoded_bytes:
        raise ReleaseControlError("source observation body exceeds its encoded size limit")
    try:
        encoded_bytes = encoded.encode("ascii")
        raw = base64.b64decode(encoded_bytes, validate=True)
    except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
        raise ReleaseControlError("source observation body is not canonical base64") from exc
    if base64.b64encode(raw).decode("ascii") != encoded:
        raise ReleaseControlError("source observation body is not canonical base64")
    if len(raw) > MAX_SOURCE_BODY_BYTES:
        raise ReleaseControlError("source observation body exceeds its size limit")
    return cast(JSONObject, value)


def source_snapshot(raw: bytes) -> JSONObject:
    envelope = _validate_source_envelope(
        _object(strict_canonical_json(raw, label="source observation"), label="source observation")
    )
    return {
        "name": envelope["name"],
        "provider": envelope["provider"],
        "locator": envelope["locator"],
        "authenticated_as": envelope["authenticated_as"],
        "freshness_class": envelope["freshness_class"],
        "captured_at": envelope["captured_at"],
        "page_count": envelope["page_count"],
        "record_count": envelope["record_count"],
        "sha256": _sha256(raw),
        "size_bytes": len(raw),
        "complete": True,
    }


def source_observation_body(raw: bytes, *, expected_name: str | None = None) -> bytes:
    """Return exact bytes carried by a canonical source observation envelope."""

    envelope = _validate_source_envelope(
        _object(
            strict_canonical_json(raw, label="source observation"),
            label="source observation",
        )
    )
    if expected_name is not None and envelope.get("name") != expected_name:
        raise ReleaseControlError("source observation name mismatch")
    encoded = cast(str, envelope["body"])
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:  # pragma: no cover - envelope validated above
        raise ReleaseControlError("source observation body is invalid") from exc
    return body


def source_observation_body_for_contract(
    raw: bytes,
    *,
    registry: Mapping[str, object],
    receipt_schema: str,
    phase: str | None,
    mode: str | None,
    name: str,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bytes:
    """Validate one source envelope against its exact committed registry tuple."""

    envelope = _validate_source_envelope(
        _object(
            strict_canonical_json(raw, label=f"{name} source observation"),
            label=f"{name} source observation",
        )
    )
    entries = _validate_registry(registry)
    matches = [
        entry
        for entry in entries
        if entry.get("receipt_schema") == receipt_schema
        and entry.get("phase") == phase
        and entry.get("mode") == mode
        and entry.get("name") == name
    ]
    if len(matches) != 1:
        raise ReleaseControlError("source contract registry lookup must select exactly one entry")
    entry = matches[0]
    for field in ("name", "provider", "locator", "freshness_class"):
        if envelope.get(field) != entry.get(field):
            raise ReleaseControlError(f"source observation {field} registry mismatch")

    authentication_mode = entry.get("authentication_mode")
    authenticated_as = envelope.get("authenticated_as")
    if authentication_mode in {"github-owner", "controller-owner"}:
        if authenticated_as != SIGNING_PRINCIPAL:
            raise ReleaseControlError("source observation owner authentication registry mismatch")
    elif authentication_mode == "github-actions-run":
        if (
            type(authenticated_as) is not str
            or re.fullmatch(r"github-actions:[1-9][0-9]*:[1-9][0-9]*", authenticated_as) is None
        ):
            raise ReleaseControlError("source observation Actions authentication registry mismatch")
    elif authentication_mode in {"local", "pypi-public"}:
        if authenticated_as is not None:
            raise ReleaseControlError("source observation authentication registry mismatch")
    else:  # pragma: no cover - the registry validator rejects this first
        raise ReleaseControlError("source observation authentication mode is invalid")

    body = source_observation_body(raw, expected_name=name)
    page_count, record_count = _body_counts(
        body,
        body_mode=entry.get("body_mode"),
        count_mode=entry.get("count_mode"),
        locator=entry.get("locator"),
    )
    if envelope.get("page_count") != page_count or envelope.get("record_count") != record_count:
        raise ReleaseControlError("source observation count registry mismatch")

    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ReleaseControlError("source verification clock must be aware UTC")
    now = now.astimezone(UTC).replace(microsecond=0)
    captured = parse_timestamp(envelope.get("captured_at"), label="source observation captured_at")
    if captured > now:
        raise ReleaseControlError("source observation capture is in the future")
    if (
        entry.get("freshness_class") == "current"
        and (now - captured).total_seconds() > CURRENT_CAPTURE_WINDOW_SECONDS
    ):
        raise ReleaseControlError("current source observation is stale")
    return body


def validate_receipt_freshness(
    sources: Sequence[Mapping[str, object]],
    *,
    acknowledgement: Mapping[str, object],
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[str, str]:
    """Derive observed/expiry time from exact current source captures."""

    current: list[datetime] = []
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ReleaseControlError("receipt verification clock must be aware UTC")
    now = now.astimezone(UTC)
    for raw_source in sources:
        source = _validate_source_envelope(raw_source)
        captured = parse_timestamp(source["captured_at"], label="source captured_at")
        if source["freshness_class"] == "current":
            if captured > now:
                raise ReleaseControlError("current source capture is in the future")
            current.append(captured)
    if not current:
        raise ReleaseControlError("receipt requires at least one current source")
    earliest = min(current)
    latest = max(current)
    if (latest - earliest).total_seconds() > CURRENT_CAPTURE_WINDOW_SECONDS:
        raise ReleaseControlError("current source capture window exceeds 120 seconds")
    observed = latest
    expires = observed + timedelta(seconds=RECEIPT_LIFETIME_SECONDS)
    begins = parse_timestamp(acknowledgement.get("begins_at"), label="acknowledgement begins_at")
    acknowledgement_expires = parse_timestamp(
        acknowledgement.get("expires_at"), label="acknowledgement expires_at"
    )
    if begins > earliest or acknowledgement_expires < expires:
        raise ReleaseControlError("acknowledgement does not cover the receipt freshness window")
    return (
        _format_timestamp(observed, label="observed_at"),
        _format_timestamp(expires, label="expires_at"),
    )


def verify_receipt_time(
    *,
    observed_at: object,
    expires_at: object,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bool:
    observed = parse_timestamp(observed_at, label="receipt observed_at")
    expires = parse_timestamp(expires_at, label="receipt expires_at")
    if expires != observed + timedelta(seconds=RECEIPT_LIFETIME_SECONDS):
        raise ReleaseControlError("receipt expiry interval mismatch")
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ReleaseControlError("receipt verification clock must be aware UTC")
    now = now.astimezone(UTC)
    if now < observed:
        raise ReleaseControlError("receipt is not yet observable")
    if now >= expires:
        raise ReleaseControlError("receipt is expired")
    return True


def _read_regular(path: Path, *, label: str, max_bytes: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseControlError(f"{label} cannot be inspected: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ReleaseControlError(f"{label} must be a regular file")
    if info.st_size > max_bytes:
        raise ReleaseControlError(f"{label} exceeds its size limit")
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) != info.st_size:
        raise ReleaseControlError(f"{label} changed while it was read")
    return raw


def _flush_windows_directory(path: Path) -> None:
    """Flush one Windows directory handle using the Win32 durability API."""

    import ctypes
    from ctypes import wintypes

    get_last_error = cast(
        Callable[[], int],
        ctypes.get_last_error,  # type: ignore[attr-defined]
    )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [wintypes.HANDLE]
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        0x40000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(get_last_error(), "cannot open directory for flush", path)
    try:
        if not flush_file_buffers(handle):
            raise OSError(get_last_error(), "cannot flush directory metadata", path)
    finally:
        close_handle(handle)


def _fsync_directory(path: Path, *, _platform_name: str | None = None) -> None:
    platform_name = os.name if _platform_name is None else _platform_name
    if platform_name == "nt":
        _flush_windows_directory(path)
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically install a staged file without replacement on supported OSes."""

    import ctypes
    import errno

    if os.name == "nt":
        from ctypes import wintypes

        get_last_error = cast(
            Callable[[], int],
            ctypes.get_last_error,  # type: ignore[attr-defined]
        )
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        move_file = kernel32.MoveFileExW
        move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move_file.restype = wintypes.BOOL
        if not move_file(str(source), str(target), 0x00000008):
            error = get_last_error()
            if error in {80, 183}:
                raise FileExistsError(error, "target already exists", target)
            raise OSError(error, "atomic no-replace move failed", target)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    encoded_source = os.fsencode(source)
    encoded_target = os.fsencode(target)
    if sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError as exc:
            raise ReleaseControlError(
                "atomic no-replace installation is unavailable on macOS"
            ) from exc
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(encoded_source, encoded_target, 0x00000004)
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise ReleaseControlError(
                "atomic no-replace installation is unavailable on Linux"
            ) from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, encoded_source, -100, encoded_target, 1)
    else:
        raise ReleaseControlError("atomic no-replace installation is unsupported on this platform")
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, "target already exists", target)
    raise OSError(error, "atomic no-replace installation failed", target)


def write_once(path: Path, raw: bytes) -> bool:
    """Create exact bytes once; exact replay is a no-op and drift fails."""

    if type(raw) is not bytes or not raw:
        raise ReleaseControlError("receipt output must be nonempty exact bytes")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ReleaseControlError("receipt output parent must be a real directory")
    if path.exists() or path.is_symlink():
        if (
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_size == len(raw)
            and _read_regular(path, label="existing receipt", max_bytes=len(raw)) == raw
        ):
            return False
        raise ReleaseControlError(f"receipt output conflict: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _rename_noreplace(temporary, path)
        except FileExistsError:
            if (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_size == len(raw)
                and _read_regular(path, label="existing receipt", max_bytes=len(raw)) == raw
            ):
                return False
            raise ReleaseControlError(f"receipt output conflict: {path}") from None
        _fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _load_canonical_file(path: Path, *, label: str, max_bytes: int) -> JSONValue:
    return strict_canonical_json(
        _read_regular(path, label=label, max_bytes=max_bytes),
        label=label,
    )


def _schema(name: str) -> JSONObject:
    path = SCHEMA_ROOT / f"{name}.schema.json"
    raw = _read_regular(path, label=f"{name} schema", max_bytes=4 * 1024 * 1024)
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ReleaseControlError) as exc:
        raise ReleaseControlError(f"{name} schema is not strict JSON: {exc}") from exc
    value = _object(_validate_ijson(parsed, label=f"{name} schema"), label=f"{name} schema")
    try:
        jsonschema.Draft202012Validator.check_schema(value)
    except jsonschema.SchemaError as exc:
        raise ReleaseControlError(f"{name} schema is invalid: {exc.message}") from exc
    return value


def _validate_schema(name: str, value: object, *, label: str) -> None:
    errors = sorted(
        jsonschema.Draft202012Validator(_schema(name)).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        raise ReleaseControlError(f"{label} fails schema validation: {errors[0].message}")


def _require_exact_fields(
    value: Mapping[str, object], expected: frozenset[str], *, label: str
) -> None:
    if type(value) is not dict or set(value) != expected:
        raise ReleaseControlError(f"{label} fields mismatch")


def _digest(value: object, *, label: str) -> str:
    checked = _validate_string(value, label=label)
    if DIGEST_RE.fullmatch(checked) is None:
        raise ReleaseControlError(f"{label} fingerprint or digest is invalid")
    return checked


def _ssh_string(raw: bytes, offset: int, *, label: str) -> tuple[bytes, int]:
    if offset < 0 or len(raw) - offset < 4:
        raise ReleaseControlError(f"{label} SSH string length is truncated")
    length = struct.unpack(">I", raw[offset : offset + 4])[0]
    start = offset + 4
    end = start + length
    if end < start or end > len(raw):
        raise ReleaseControlError(f"{label} SSH string is truncated")
    return raw[start:end], end


def _encode_ssh_string(raw: bytes) -> bytes:
    if len(raw) > 0xFFFFFFFF:
        raise ReleaseControlError("OpenSSH string is too large")
    return struct.pack(">I", len(raw)) + raw


def _decode_ssh_public_key_blob(public_key: str, *, label: str) -> bytes:
    match = SSH_PUBLIC_KEY_RE.fullmatch(public_key)
    if match is None:
        raise ReleaseControlError(f"{label} must be one exact ssh-ed25519 public key")
    try:
        blob = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReleaseControlError(f"{label} has invalid base64") from exc
    algorithm, offset = _ssh_string(blob, 0, label=label)
    key, offset = _ssh_string(blob, offset, label=label)
    if algorithm != b"ssh-ed25519" or len(key) != 32 or offset != len(blob):
        raise ReleaseControlError(f"{label} has an invalid Ed25519 key blob")
    if base64.b64encode(blob).decode("ascii") != match.group(2):
        raise ReleaseControlError(f"{label} base64 is not canonical")
    return blob


def ssh_public_key_fingerprint(public_key: str) -> str:
    """Return Kestrel's lowercase-hex fingerprint for an OpenSSH public key."""

    checked = _validate_string(public_key, label="OpenSSH public key")
    return _sha256(_decode_ssh_public_key_blob(checked, label="OpenSSH public key"))


def _decode_ssh_signature_parts(signature: bytes) -> tuple[bytes, str, bytes, bytes]:
    if type(signature) is not bytes or not signature:
        raise ReleaseControlError("OpenSSH detached signature is empty")
    try:
        text = signature.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ReleaseControlError("OpenSSH detached signature is not ASCII") from exc
    lines = text.splitlines()
    if (
        len(lines) < 3
        or lines[0] != "-----BEGIN SSH SIGNATURE-----"
        or lines[-1] != "-----END SSH SIGNATURE-----"
        or not signature.endswith(b"\n")
    ):
        raise ReleaseControlError("OpenSSH detached signature armor is invalid")
    body_lines = lines[1:-1]
    if any(not line or len(line) > 76 for line in body_lines):
        raise ReleaseControlError("OpenSSH detached signature armor is invalid")
    try:
        decoded = base64.b64decode("".join(body_lines), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReleaseControlError("OpenSSH detached signature base64 is invalid") from exc
    if not decoded.startswith(b"SSHSIG") or len(decoded) < 10:
        raise ReleaseControlError("OpenSSH detached signature magic is invalid")
    version = struct.unpack(">I", decoded[6:10])[0]
    if version != 1:
        raise ReleaseControlError("OpenSSH detached signature version is invalid")
    public_key_blob, offset = _ssh_string(decoded, 10, label="OpenSSH signature key")
    namespace_raw, offset = _ssh_string(decoded, offset, label="OpenSSH signature namespace")
    reserved, offset = _ssh_string(decoded, offset, label="OpenSSH signature reserved field")
    hash_algorithm, offset = _ssh_string(decoded, offset, label="OpenSSH signature hash algorithm")
    raw_signature, offset = _ssh_string(decoded, offset, label="OpenSSH signature value")
    if offset != len(decoded) or reserved or hash_algorithm != b"sha512":
        raise ReleaseControlError("OpenSSH detached signature fields are invalid")
    key_algorithm, key_offset = _ssh_string(
        public_key_blob, 0, label="OpenSSH signature public key"
    )
    key_value, key_offset = _ssh_string(
        public_key_blob, key_offset, label="OpenSSH signature public key"
    )
    signature_algorithm, signature_offset = _ssh_string(
        raw_signature, 0, label="OpenSSH signature algorithm"
    )
    signature_value, signature_offset = _ssh_string(
        raw_signature, signature_offset, label="OpenSSH signature bytes"
    )
    if (
        key_algorithm != b"ssh-ed25519"
        or len(key_value) != 32
        or key_offset != len(public_key_blob)
        or signature_algorithm != b"ssh-ed25519"
        or len(signature_value) != 64
        or signature_offset != len(raw_signature)
    ):
        raise ReleaseControlError("OpenSSH detached signature algorithm is invalid")
    try:
        namespace = namespace_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseControlError("OpenSSH detached signature namespace is invalid") from exc
    _validate_string(namespace, label="OpenSSH signature namespace")
    return public_key_blob, namespace, key_value, signature_value


def _decode_ssh_signature(signature: bytes) -> tuple[bytes, str]:
    public_key_blob, namespace, _, _ = _decode_ssh_signature_parts(signature)
    return public_key_blob, namespace


def _ssh_signature_payload(*, receipt: bytes, namespace: str) -> bytes:
    return b"".join(
        (
            b"SSHSIG",
            _encode_ssh_string(namespace.encode("utf-8")),
            _encode_ssh_string(b""),
            _encode_ssh_string(b"sha512"),
            _encode_ssh_string(hashlib.sha512(receipt).digest()),
        )
    )


def _armor_ssh_signature(raw: bytes) -> bytes:
    encoded = base64.b64encode(raw)
    lines = [encoded[index : index + 70] for index in range(0, len(encoded), 70)]
    return b"\n".join(
        [b"-----BEGIN SSH SIGNATURE-----", *lines, b"-----END SSH SIGNATURE-----", b""]
    )


def signature_public_key_fingerprint(signature: bytes) -> str:
    public_key_blob, _ = _decode_ssh_signature(signature)
    return _sha256(public_key_blob)


def _validate_signature_inputs(
    *, receipt: bytes, namespace: str, principal: str | None = None
) -> JSONObject:
    if namespace != SIGNING_NAMESPACE:
        raise ReleaseControlError("OpenSSH signature namespace mismatch")
    if principal is not None and principal != SIGNING_PRINCIPAL:
        raise ReleaseControlError("OpenSSH signing principal mismatch")
    return _object(strict_canonical_json(receipt, label="signed receipt"), label="signed receipt")


def _write_secure_temporary(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def sign_receipt_detached(
    *,
    receipt: bytes,
    identity_file: Path,
    principal: str,
    namespace: str,
) -> bytes:
    """Produce one deterministic OpenSSH detached signature over canonical bytes."""

    value = _validate_signature_inputs(receipt=receipt, namespace=namespace, principal=principal)
    identity = _read_regular(identity_file, label="OpenSSH signing identity", max_bytes=1024 * 1024)
    try:
        loaded_key = serialization.load_ssh_private_key(identity, password=None)
    except (TypeError, ValueError, UnsupportedAlgorithm) as exc:
        raise ReleaseControlError("OpenSSH signing identity is invalid") from exc
    if not isinstance(loaded_key, Ed25519PrivateKey):
        raise ReleaseControlError("OpenSSH signing identity must be Ed25519")
    public_key_value = loaded_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    public_key_blob = _encode_ssh_string(b"ssh-ed25519") + _encode_ssh_string(public_key_value)
    fingerprint = _sha256(public_key_blob)
    declared_fingerprint = value.get("signing_key_fingerprint")
    if declared_fingerprint is not None and declared_fingerprint != fingerprint:
        raise ReleaseControlError("OpenSSH signing identity fingerprint mismatch")
    raw_signature = loaded_key.sign(_ssh_signature_payload(receipt=receipt, namespace=namespace))
    signature_blob = _encode_ssh_string(b"ssh-ed25519") + _encode_ssh_string(raw_signature)
    signature = _armor_ssh_signature(
        b"".join(
            (
                b"SSHSIG",
                struct.pack(">I", 1),
                _encode_ssh_string(public_key_blob),
                _encode_ssh_string(namespace.encode("utf-8")),
                _encode_ssh_string(b""),
                _encode_ssh_string(b"sha512"),
                _encode_ssh_string(signature_blob),
            )
        )
    )
    public_key_blob, embedded_namespace = _decode_ssh_signature(signature)
    if embedded_namespace != namespace:
        raise ReleaseControlError("OpenSSH detached signature namespace mismatch")
    if _sha256(public_key_blob) != fingerprint:
        raise ReleaseControlError("OpenSSH detached signature fingerprint mismatch")
    verify_detached_signature(
        receipt=receipt,
        signature=signature,
        expected_fingerprint=fingerprint,
        namespace=namespace,
    )
    return signature


def verify_detached_signature(
    *,
    receipt: bytes,
    signature: bytes,
    expected_fingerprint: str,
    namespace: str,
) -> bool:
    """Verify signature integrity and exact embedded signer fingerprint."""

    _validate_signature_inputs(receipt=receipt, namespace=namespace)
    checked_fingerprint = _digest(expected_fingerprint, label="OpenSSH expected fingerprint")
    public_key_blob, embedded_namespace, public_key_value, signature_value = (
        _decode_ssh_signature_parts(signature)
    )
    if embedded_namespace != namespace:
        raise ReleaseControlError("OpenSSH signature namespace mismatch")
    if _sha256(public_key_blob) != checked_fingerprint:
        raise ReleaseControlError("OpenSSH signature fingerprint mismatch")
    try:
        Ed25519PublicKey.from_public_bytes(public_key_value).verify(
            signature_value,
            _ssh_signature_payload(receipt=receipt, namespace=namespace),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ReleaseControlError("OpenSSH detached signature verification failed") from exc
    return True


def _normalized_owner_signing_keys(value: object, *, label: str) -> list[JSONObject]:
    raw_items = _array(value, label=label)
    flattened: list[JSONObject] = []
    for raw_item in raw_items:
        if type(raw_item) is list:
            flattened.extend(_normalized_owner_signing_keys(raw_item, label=label))
            continue
        item = _object(raw_item, label="owner signing key")
        required = frozenset({"id", "key", "title"})
        if not required.issubset(item):
            raise ReleaseControlError("owner signing key fields mismatch")
        key_id = _safe_integer(item.get("id"), label="owner signing key ID", positive=True)
        public_key = _validate_string(item.get("key"), label="owner signing public key")
        title = _validate_string(item.get("title"), label="owner signing key title")
        ssh_public_key_fingerprint(public_key)
        flattened.append({"id": key_id, "key": public_key, "title": title})
    flattened.sort(key=lambda item: (cast(int, item["id"]), cast(str, item["key"])))
    return flattened


def _owner_signing_keys_from_contract(
    raw: bytes,
    *,
    _clock: Callable[[], datetime],
) -> list[JSONObject]:
    registry = _object(
        _load_canonical_file(
            SOURCE_REGISTRY_PATH,
            label="release-control source registry",
            max_bytes=4 * 1024 * 1024,
        ),
        label="release-control source registry",
    )
    body = source_observation_body_for_contract(
        raw,
        registry=registry,
        receipt_schema=SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="owner-signing-keys-observation",
        _clock=_clock,
    )
    pages = _paginated_bodies(
        parse_external_json_bytes(body, label="owner signing keys observation body"),
        locator=OWNER_SIGNING_KEYS_LOCATOR,
    )
    return _normalized_owner_signing_keys(pages, label="owner signing keys")


def _fetch_owner_signing_keys_from_github(principal: str) -> list[JSONObject]:
    """Independently refetch the owner's current GitHub signing-key registry."""

    if principal != SIGNING_PRINCIPAL:
        raise ReleaseControlError("owner signing keys principal mismatch")
    gh = _pinned_gh()
    _verify_pinned_gh(gh)
    raw = _run_pinned_gh_verification(
        gh,
        (
            "api",
            "--hostname",
            "github.com",
            "--paginate",
            "--slurp",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            f"/users/{principal}/ssh_signing_keys?per_page=100",
        ),
    )
    return _normalized_owner_signing_keys(
        parse_external_json_bytes(raw, label="independent GitHub owner signing keys"),
        label="independent GitHub owner signing keys",
    )


def owner_signing_key(
    *,
    owner_signing_keys_observation: bytes,
    principal: str,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[str, str]:
    """Return the sole fresh registered owner key and its canonical fingerprint."""

    if principal != SIGNING_PRINCIPAL:
        raise ReleaseControlError("owner signing keys principal mismatch")
    keys = _owner_signing_keys_from_contract(
        owner_signing_keys_observation,
        _clock=_clock,
    )
    if len(keys) != 1:
        raise ReleaseControlError("owner signing keys must contain exactly one key")
    independently_fetched = _fetch_owner_signing_keys_from_github(principal)
    if independently_fetched != keys:
        raise ReleaseControlError(
            "owner signing keys do not match the independent GitHub observation"
        )
    key = keys[0]
    public_key = _validate_string(key.get("key"), label="owner signing public key")
    return public_key, ssh_public_key_fingerprint(public_key)


def _offline_owner_signing_key(
    owner_signing_keys_observation: bytes,
    *,
    expected_fingerprint: str | None,
) -> tuple[str, str]:
    """Resolve the capsule-pinned owner key without network or freshness authority."""

    body = source_observation_body(
        owner_signing_keys_observation,
        expected_name="owner-signing-keys-observation",
    )
    pages = _paginated_bodies(
        parse_external_json_bytes(body, label="offline owner signing keys observation"),
        locator=OWNER_SIGNING_KEYS_LOCATOR,
    )
    keys = _normalized_owner_signing_keys(pages, label="offline owner signing keys")
    if len(keys) != 1:
        raise ReleaseControlError("offline owner signing key must be an exact singleton")
    public_key = _validate_string(keys[0].get("key"), label="offline owner signing key")
    fingerprint = ssh_public_key_fingerprint(public_key)
    if expected_fingerprint is not None and fingerprint != _digest(
        expected_fingerprint, label="externally pinned owner signing key fingerprint"
    ):
        raise ReleaseControlError("offline owner signing key fingerprint mismatch")
    return public_key, fingerprint


def verify_owner_detached_signature(
    *,
    receipt: bytes,
    signature: bytes,
    owner_signing_keys_observation: bytes,
    principal: str,
    namespace: str,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bool:
    """Verify against the one fresh public key registered to the exact owner."""

    _validate_signature_inputs(receipt=receipt, namespace=namespace, principal=principal)
    embedded_public_key_blob, embedded_namespace = _decode_ssh_signature(signature)
    if embedded_namespace != namespace:
        raise ReleaseControlError("OpenSSH signature namespace mismatch")
    public_key, expected_fingerprint = owner_signing_key(
        owner_signing_keys_observation=owner_signing_keys_observation,
        principal=principal,
        _clock=_clock,
    )
    verify_detached_signature(
        receipt=receipt,
        signature=signature,
        expected_fingerprint=expected_fingerprint,
        namespace=namespace,
    )
    if embedded_public_key_blob != _decode_ssh_public_key_blob(
        public_key, label="owner signing public key"
    ):
        raise ReleaseControlError("owner OpenSSH detached signature key mismatch")
    return True


def verify_owner_detached_signature_against_current_registration(
    *,
    receipt: bytes,
    signature: bytes,
    principal: str,
    namespace: str,
) -> bool:
    """Verify a durable receipt against the owner's independently refetched key."""

    _validate_signature_inputs(receipt=receipt, namespace=namespace, principal=principal)
    keys = _fetch_owner_signing_keys_from_github(principal)
    if len(keys) != 1:
        raise ReleaseControlError("current owner signing keys must contain exactly one key")
    public_key = _validate_string(keys[0].get("key"), label="current owner signing public key")
    fingerprint = ssh_public_key_fingerprint(public_key)
    verify_detached_signature(
        receipt=receipt,
        signature=signature,
        expected_fingerprint=fingerprint,
        namespace=namespace,
    )
    embedded_public_key_blob, embedded_namespace = _decode_ssh_signature(signature)
    if embedded_namespace != namespace:
        raise ReleaseControlError("OpenSSH signature namespace mismatch")
    if embedded_public_key_blob != _decode_ssh_public_key_blob(
        public_key, label="current owner signing public key"
    ):
        raise ReleaseControlError("current owner OpenSSH detached signature key mismatch")
    return True


def verify_repository_writer_inventory(
    *,
    inventory: bytes,
    signature: bytes,
    owner_signing_keys_observation: bytes,
    phase: str,
    expected_run_id: int | None,
    journal: Mapping[str, object] | None = None,
    transaction: Mapping[str, object] | None = None,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    """Verify one fresh, owner-signed, phase-specific GitHub writer inventory."""

    if phase not in {"pre_send", "post_containment", "pre_admission"}:
        raise ReleaseControlError("repository writer inventory phase is invalid")
    if (phase == "pre_admission") != (expected_run_id is not None):
        raise ReleaseControlError(
            "pre-admission writer inventory requires exactly one expected nonce run"
        )
    if expected_run_id is not None:
        _safe_integer(expected_run_id, label="expected nonce run ID", positive=True)

    if (journal is None) == (transaction is None):
        raise ReleaseControlError(
            "repository writer inventory requires exactly one dispatch authority record"
        )
    if journal is not None:
        authority = _validate_dispatch_journal(journal)
    elif transaction is not None:
        authority = _validate_dispatch_transaction_projection(transaction)
    else:  # pragma: no cover - excluded by the exact-one guard above
        raise ReleaseControlError("dispatch authority record is missing")
    checked = _object(
        strict_canonical_json(inventory, label="repository writer inventory"),
        label="repository writer inventory",
    )
    _validate_schema(WRITER_INVENTORY_SCHEMA, checked, label="repository writer inventory")
    if checked.get("schema") != WRITER_INVENTORY_SCHEMA:
        raise ReleaseControlError("repository writer inventory schema mismatch")
    if checked.get("phase") != phase:
        raise ReleaseControlError("repository writer inventory phase mismatch")

    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ReleaseControlError("repository writer inventory clock must be aware UTC")
    now = now.astimezone(UTC).replace(microsecond=0)
    verify_owner_detached_signature(
        receipt=inventory,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
        _clock=lambda: now,
    )
    captured = parse_timestamp(
        checked.get("captured_at"), label="repository writer inventory captured_at"
    )
    if captured > now:
        raise ReleaseControlError("repository writer inventory is in the future")
    if (now - captured).total_seconds() > CURRENT_CAPTURE_WINDOW_SECONDS:
        raise ReleaseControlError("repository writer inventory is stale")
    if checked.get("complete") is not True:
        raise ReleaseControlError("repository writer inventory is incomplete")

    repository = _object(checked.get("repository"), label="writer inventory repository")
    journal_repository = _object(authority.get("repository"), label="dispatch authority repository")
    if repository != journal_repository:
        raise ReleaseControlError("repository writer inventory repository mismatch")

    owner = _object(checked.get("owner"), label="repository owner")
    if owner.get("login") != SIGNING_PRINCIPAL or owner.get("type") != "User":
        raise ReleaseControlError("repository writer inventory owner authority mismatch")
    _safe_integer(owner.get("id"), label="repository owner ID", positive=True)

    repository_writers = _array(checked.get("repository_writers"), label="repository writers")
    expected_writer: JSONObject = {
        "login": owner["login"],
        "id": owner["id"],
        "type": "User",
        "role_name": "admin",
    }
    if repository_writers != [expected_writer]:
        raise ReleaseControlError(
            "repository writer authority must contain only the owner administrator"
        )
    if _array(checked.get("invitations"), label="repository invitations"):
        raise ReleaseControlError("repository invitation authority must be empty")
    if _array(checked.get("write_deploy_keys"), label="write deploy keys"):
        raise ReleaseControlError("repository write deploy-key authority must be empty")
    if _array(checked.get("mutation_capable_runs"), label="mutation-capable runs"):
        raise ReleaseControlError("mutation-capable run authority must be empty")

    actor = _object(authority.get("actor"), label="dispatch authority actor")
    installed_apps = _array(checked.get("installed_apps"), label="installed Apps")
    actions_writers = _array(
        checked.get("actions_write_principals"), label="Actions writer principals"
    )
    expected_app: JSONObject = {
        "app_id": actor["app_id"],
        "installation_id": actor["installation_id"],
        "bot_login": actor["login"],
        "bot_id": actor["id"],
        "permissions": {"actions": "write", "metadata": "read"},
    }
    expected_actions_writer: JSONObject = {
        "kind": "GitHubApp",
        "login": actor["login"],
        "id": actor["id"],
        "app_id": actor["app_id"],
        "installation_id": actor["installation_id"],
    }
    if phase == "pre_send":
        if installed_apps != [expected_app] or actions_writers != [expected_actions_writer]:
            raise ReleaseControlError(
                "pre-send App writer authority must be exactly the dispatcher"
            )
    elif installed_apps or actions_writers:
        raise ReleaseControlError("post-send App and Actions writer authority must be empty")

    nonce_run_ids = _array(checked.get("nonce_run_ids"), label="nonce run IDs")
    if phase == "pre_send" and nonce_run_ids:
        raise ReleaseControlError("pre-send nonce run inventory must be empty")
    if phase == "post_containment" and len(nonce_run_ids) > 1:
        raise ReleaseControlError("post-containment nonce run inventory is ambiguous")
    if phase == "pre_admission" and nonce_run_ids != [expected_run_id]:
        raise ReleaseControlError(
            "pre-admission nonce run inventory must contain only the admitted run"
        )

    evidence = _object(checked.get("evidence"), label="writer inventory evidence")
    if evidence.get("canonicalization_vector_digest") != canonicalization_vector_digest():
        raise ReleaseControlError("writer inventory canonicalization evidence mismatch")
    return checked


def _git_sha(value: object, *, label: str) -> str:
    checked = _validate_string(value, label=label)
    if GIT_SHA_RE.fullmatch(checked) is None:
        raise ReleaseControlError(f"{label} must be one full lowercase Git SHA")
    return checked


def _nonce(value: object, *, label: str = "dispatch transaction nonce") -> str:
    checked = _validate_string(value, label=label)
    if NONCE_RE.fullmatch(checked) is None:
        raise ReleaseControlError(f"{label} must contain exactly 32 random bytes as hex")
    return checked


def _copy_json_object(value: Mapping[str, object], *, label: str) -> JSONObject:
    return _object(strict_canonical_json(canonical_json_bytes(value), label=label), label=label)


def _dispatch_repository(value: Mapping[str, object]) -> JSONObject:
    repository = _copy_json_object(value, label="dispatch repository")
    _require_exact_fields(repository, frozenset({"full_name", "id"}), label="dispatch repository")
    full_name = _validate_string(repository.get("full_name"), label="dispatch repository full name")
    if full_name.count("/") != 1:
        raise ReleaseControlError("dispatch repository full name is invalid")
    _safe_integer(repository.get("id"), label="dispatch repository ID", positive=True)
    return repository


def _dispatch_workflow(value: Mapping[str, object]) -> JSONObject:
    workflow = _copy_json_object(value, label="dispatch workflow")
    _require_exact_fields(
        workflow,
        frozenset(
            {
                "id",
                "path",
                "state",
                "default_branch_sha",
                "observation_digest",
            }
        ),
        label="dispatch workflow",
    )
    _safe_integer(workflow.get("id"), label="dispatch workflow ID", positive=True)
    if workflow.get("path") != DISPATCH_WORKFLOW_PATH:
        raise ReleaseControlError("dispatch workflow path mismatch")
    if workflow.get("state") != "active":
        raise ReleaseControlError("dispatch workflow must be active")
    _git_sha(workflow.get("default_branch_sha"), label="dispatch default branch SHA")
    _digest(workflow.get("observation_digest"), label="dispatch workflow observation digest")
    return workflow


def _dispatch_target(
    value: Mapping[str, object], *, repository: JSONObject, workflow: JSONObject
) -> JSONObject:
    target = _copy_json_object(value, label="dispatch target")
    _require_exact_fields(
        target,
        frozenset(
            {
                "mode",
                "short_ref",
                "full_ref",
                "head_sha",
                "workflow_ref",
                "workflow_sha",
            }
        ),
        label="dispatch target",
    )
    mode = target.get("mode")
    if mode not in {"initiate", "recover_committed"}:
        raise ReleaseControlError("dispatch target mode is invalid")
    short_ref = _validate_string(target.get("short_ref"), label="dispatch short ref")
    full_ref = _validate_string(target.get("full_ref"), label="dispatch full ref")
    expected_full_ref = "refs/heads/main" if mode == "initiate" else f"refs/tags/{short_ref}"
    if full_ref != expected_full_ref:
        raise ReleaseControlError("dispatch target ref binding mismatch")
    if mode == "initiate" and short_ref != "main":
        raise ReleaseControlError("initiate dispatch ref must be main")
    head_sha = _git_sha(target.get("head_sha"), label="dispatch candidate head SHA")
    workflow_sha = _git_sha(target.get("workflow_sha"), label="dispatch candidate workflow SHA")
    if head_sha != workflow_sha:
        raise ReleaseControlError("dispatch candidate head/workflow SHA mismatch")
    if mode == "initiate" and head_sha != workflow.get("default_branch_sha"):
        raise ReleaseControlError("dispatch candidate does not equal active main")
    expected_workflow_ref = f"{repository['full_name']}/{DISPATCH_WORKFLOW_PATH}@{full_ref}"
    if target.get("workflow_ref") != expected_workflow_ref:
        raise ReleaseControlError("dispatch workflow ref binding mismatch")
    return target


def _dispatch_actor(value: Mapping[str, object]) -> JSONObject:
    actor = _copy_json_object(value, label="dispatch actor")
    _require_exact_fields(
        actor,
        frozenset({"login", "id", "app_id", "installation_id"}),
        label="dispatch actor",
    )
    login = _validate_string(actor.get("login"), label="dispatch actor login")
    if not login.endswith("[bot]"):
        raise ReleaseControlError("dispatch actor must be the App bot")
    for field in ("id", "app_id", "installation_id"):
        _safe_integer(actor.get(field), label=f"dispatch actor {field}", positive=True)
    return actor


def _dispatch_base_inputs(value: Mapping[str, object]) -> JSONObject:
    inputs = _copy_json_object(value, label="dispatch inputs")
    _require_exact_fields(
        inputs,
        frozenset({"candidate_run_id", "candidate_manifest_digest", "mode"}),
        label="dispatch inputs",
    )
    candidate_run_id = _validate_string(
        inputs.get("candidate_run_id"), label="dispatch candidate run ID"
    )
    if (
        re.fullmatch(r"[1-9][0-9]*", candidate_run_id) is None
        or int(candidate_run_id) > MAX_SAFE_INTEGER
    ):
        raise ReleaseControlError("dispatch candidate run ID is invalid")
    _digest(inputs.get("candidate_manifest_digest"), label="candidate-manifest digest")
    if inputs.get("mode") not in {"initiate", "recover_committed"}:
        raise ReleaseControlError("dispatch input mode is invalid")
    return inputs


def dispatch_binding(*, short_ref: str, inputs_without_binding: Mapping[str, object]) -> str:
    checked_ref = _validate_string(short_ref, label="dispatch binding ref")
    inputs = _copy_json_object(inputs_without_binding, label="dispatch binding inputs")
    if "dispatch_binding" in inputs:
        raise ReleaseControlError("dispatch binding input cannot self-reference")
    return _sha256(canonical_json_bytes({"ref": checked_ref, "inputs": inputs}))


def prepare_dispatch_records(
    *,
    repository: Mapping[str, object],
    workflow: Mapping[str, object],
    target: Mapping[str, object],
    actor: Mapping[str, object],
    inputs: Mapping[str, object],
    _nonce_source: Callable[[int], bytes] = secrets.token_bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    _monotonic: Callable[[], float] = time.monotonic,
) -> tuple[JSONObject, JSONObject, JSONObject]:
    """Prepare immutable journal, intent, and exact one-wire request records."""

    checked_repository = _dispatch_repository(repository)
    checked_workflow = _dispatch_workflow(workflow)
    checked_target = _dispatch_target(
        target, repository=checked_repository, workflow=checked_workflow
    )
    checked_actor = _dispatch_actor(actor)
    base_inputs = _dispatch_base_inputs(inputs)
    if base_inputs.get("mode") != checked_target.get("mode"):
        raise ReleaseControlError("dispatch target/input mode mismatch")
    raw_nonce = _nonce_source(32)
    if type(raw_nonce) is not bytes or len(raw_nonce) != 32:
        raise ReleaseControlError("dispatch nonce source must return exactly 32 bytes")
    transaction_nonce = raw_nonce.hex()
    _nonce(transaction_nonce)
    complete_without_binding: JSONObject = {
        "candidate_run_id": base_inputs["candidate_run_id"],
        "candidate_manifest_digest": base_inputs["candidate_manifest_digest"],
        "mode": base_inputs["mode"],
        "transaction_nonce": transaction_nonce,
    }
    binding = dispatch_binding(
        short_ref=cast(str, checked_target["short_ref"]),
        inputs_without_binding=complete_without_binding,
    )
    complete_inputs: JSONObject = {
        **complete_without_binding,
        "dispatch_binding": binding,
    }
    complete_inputs = cast(
        JSONObject,
        dict(sorted(complete_inputs.items(), key=lambda item: item[0].encode("utf-8"))),
    )
    request: JSONObject = {
        "ref": checked_target["short_ref"],
        "inputs": complete_inputs,
    }
    request_digest = _sha256(canonical_json_bytes(request))
    now = _clock()
    prepared_at = _format_timestamp(now, label="dispatch preparation clock")
    monotonic_start = _monotonic()
    if (
        type(monotonic_start) not in {int, float}
        or not math.isfinite(monotonic_start)
        or monotonic_start < 0
    ):
        raise ReleaseControlError("dispatch monotonic clock is invalid")
    monotonic_seconds = int(monotonic_start)
    repository_name = cast(str, checked_repository["full_name"])
    endpoint = (
        f"https://api.github.com/repos/{repository_name}/actions/workflows/"
        f"{checked_workflow['id']}/dispatches"
    )
    expected_title = f"Kestrel release tx {transaction_nonce} bind {binding}"
    shared_evidence: JSONObject = {
        "source_bundle_digest": source_bundle_digest(
            {
                "actor": canonical_json_bytes(checked_actor),
                "inputs": canonical_json_bytes(base_inputs),
                "repository": canonical_json_bytes(checked_repository),
                "target": canonical_json_bytes(checked_target),
                "workflow": canonical_json_bytes(checked_workflow),
            }
        ),
        "canonicalization_vector_digest": canonicalization_vector_digest(),
    }
    journal: JSONObject = {
        "schema": DISPATCH_TRANSACTION_SCHEMA,
        "state": "prepared",
        "transaction_nonce": transaction_nonce,
        "logical_dispatch_ordinal": 1,
        "repository": checked_repository,
        "workflow": checked_workflow,
        "api_version": DISPATCH_API_VERSION,
        "endpoint": endpoint,
        "method": "POST",
        "accept": "application/vnd.github+json",
        "content_type": "application/json",
        "target": checked_target,
        "actor": checked_actor,
        "inputs": complete_inputs,
        "dispatch_binding": binding,
        "expected_display_title": expected_title,
        "canonical_request_sha256": request_digest,
        "prepared_at": prepared_at,
        "send_started_at": None,
        "monotonic_started_seconds": monotonic_seconds,
        "monotonic_deadline_seconds": monotonic_seconds + DISPATCH_RECONCILIATION_SECONDS,
        "response_contract": DISPATCH_RESPONSE_CONTRACT,
        "transport_policy": {
            "maximum_wire_transmissions": 1,
            "redirects": False,
            "retries": False,
            "auth_replay": False,
            "proxies": False,
            "failover": False,
        },
        "evidence": shared_evidence,
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "controller",
            "method": "one-wire-dispatch-preparation",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    intent: JSONObject = {
        "schema": DISPATCH_INTENT_SCHEMA,
        "transaction_nonce": transaction_nonce,
        "dispatch_binding": binding,
        "repository": checked_repository,
        "workflow": checked_workflow,
        "target": checked_target,
        "actor": checked_actor,
        "inputs": complete_inputs,
        "expected_display_title": expected_title,
        "transaction_digest": _sha256(canonical_json_bytes(journal)),
        "request_digest": request_digest,
        "issued_at": prepared_at,
        "evidence": shared_evidence,
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "controller",
            "method": "owner-dispatch-intent",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    _validate_schema(DISPATCH_TRANSACTION_SCHEMA, journal, label="dispatch journal")
    _validate_schema(DISPATCH_INTENT_SCHEMA, intent, label="dispatch intent")
    return journal, intent, request


def create_dispatch_identity(
    *,
    github_context_allowlist: Mapping[str, object],
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    """Build the pre-environment identity receipt from an exact context allowlist."""

    context = _copy_json_object(github_context_allowlist, label="GitHub context allowlist")
    _require_exact_fields(
        context,
        frozenset(
            {
                "schema",
                "event_inputs",
                "repository",
                "repository_id",
                "workflow",
                "workflow_ref",
                "workflow_sha",
                "event_name",
                "ref",
                "ref_name",
                "sha",
                "run_id",
                "run_attempt",
                "actor",
                "actor_id",
                "triggering_actor",
            }
        ),
        label="GitHub context allowlist",
    )
    if context.get("schema") != "kestrel.github_context_allowlist.v1":
        raise ReleaseControlError("GitHub context allowlist schema mismatch")
    inputs = _object(context.get("event_inputs"), label="dispatch event inputs")
    _require_exact_fields(
        inputs,
        frozenset(
            {
                "candidate_run_id",
                "candidate_manifest_digest",
                "dispatch_binding",
                "mode",
                "transaction_nonce",
            }
        ),
        label="dispatch event inputs",
    )
    _dispatch_base_inputs(
        {
            "candidate_run_id": inputs["candidate_run_id"],
            "candidate_manifest_digest": inputs["candidate_manifest_digest"],
            "mode": inputs["mode"],
        }
    )
    transaction_nonce = _nonce(inputs.get("transaction_nonce"), label="dispatch identity nonce")
    recorded_binding = _digest(inputs.get("dispatch_binding"), label="dispatch identity binding")
    inputs_without_binding = dict(inputs)
    inputs_without_binding.pop("dispatch_binding")
    ref_name = _validate_string(context.get("ref_name"), label="dispatch ref name")
    expected_binding = dispatch_binding(
        short_ref=ref_name, inputs_without_binding=inputs_without_binding
    )
    if recorded_binding != expected_binding:
        raise ReleaseControlError("dispatch identity binding mismatch")
    repository = _validate_string(context.get("repository"), label="dispatch identity repository")
    if repository.count("/") != 1:
        raise ReleaseControlError("dispatch identity repository is invalid")
    repository_id = _safe_integer(
        context.get("repository_id"), label="dispatch identity repository ID", positive=True
    )
    workflow = _validate_string(context.get("workflow"), label="dispatch identity workflow")
    workflow_ref = _validate_string(
        context.get("workflow_ref"), label="dispatch identity workflow ref"
    )
    workflow_sha = _git_sha(context.get("workflow_sha"), label="dispatch identity workflow SHA")
    if context.get("event_name") != "workflow_dispatch":
        raise ReleaseControlError("dispatch identity event must be workflow_dispatch")
    full_ref = _validate_string(context.get("ref"), label="dispatch identity ref")
    if full_ref not in {"refs/heads/main", f"refs/tags/{ref_name}"}:
        raise ReleaseControlError("dispatch identity ref/ref-name mismatch")
    expected_mode = "initiate" if full_ref == "refs/heads/main" else "recover_committed"
    if inputs.get("mode") != expected_mode:
        raise ReleaseControlError("dispatch identity mode/ref mismatch")
    expected_workflow_ref = f"{repository}/{DISPATCH_WORKFLOW_PATH}@{full_ref}"
    if workflow_ref != expected_workflow_ref:
        raise ReleaseControlError("dispatch identity workflow ref mismatch")
    sha = _git_sha(context.get("sha"), label="dispatch identity SHA")
    if sha != workflow_sha:
        raise ReleaseControlError("dispatch identity SHA/workflow SHA mismatch")
    run_id = _safe_integer(context.get("run_id"), label="dispatch identity run ID", positive=True)
    if context.get("run_attempt") != 1:
        raise ReleaseControlError("dispatch identity run attempt must be one")
    actor = _validate_string(context.get("actor"), label="dispatch identity actor")
    if not actor.endswith("[bot]"):
        raise ReleaseControlError("dispatch identity actor must be an App bot")
    actor_id = _safe_integer(
        context.get("actor_id"), label="dispatch identity actor ID", positive=True
    )
    triggering_actor = _validate_string(
        context.get("triggering_actor"), label="dispatch identity triggering actor"
    )
    observed_at = _format_timestamp(_clock(), label="dispatch identity clock")
    context_bytes = canonical_json_bytes(context)
    identity: JSONObject = {
        "schema": DISPATCH_IDENTITY_SCHEMA,
        "transaction_nonce": transaction_nonce,
        "dispatch_binding": recorded_binding,
        "dispatch_inputs_digest": _sha256(canonical_json_bytes(inputs)),
        "repository": repository,
        "repository_id": repository_id,
        "workflow": workflow,
        "workflow_ref": workflow_ref,
        "workflow_sha": workflow_sha,
        "event_name": "workflow_dispatch",
        "ref": full_ref,
        "sha": sha,
        "run_id": run_id,
        "run_attempt": 1,
        "actor": actor,
        "actor_id": actor_id,
        "triggering_actor": triggering_actor,
        "observed_at": observed_at,
        "evidence": {
            "source_bundle_digest": source_bundle_digest(
                {"github-context-allowlist": context_bytes}
            ),
            "canonicalization_vector_digest": canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "github.com",
            "method": "github-context-allowlist",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    _validate_schema(DISPATCH_IDENTITY_SCHEMA, identity, label="dispatch identity")
    return identity


def _validate_dispatch_journal(value: Mapping[str, object]) -> JSONObject:
    journal = _copy_json_object(value, label="dispatch journal")
    _validate_schema(DISPATCH_TRANSACTION_SCHEMA, journal, label="dispatch journal")
    if journal.get("schema") != DISPATCH_TRANSACTION_SCHEMA:
        raise ReleaseControlError("dispatch journal schema mismatch")
    if journal.get("state") != "prepared":
        raise ReleaseControlError("dispatch journal is not in prepared state")
    transaction_nonce = _nonce(journal.get("transaction_nonce"))
    if journal.get("logical_dispatch_ordinal") != 1:
        raise ReleaseControlError("dispatch logical ordinal must be one")
    repository = _object(journal.get("repository"), label="dispatch journal repository")
    workflow = _object(journal.get("workflow"), label="dispatch journal workflow")
    target = _object(journal.get("target"), label="dispatch journal target")
    actor = _object(journal.get("actor"), label="dispatch journal actor")
    _dispatch_repository(repository)
    _dispatch_workflow(workflow)
    _dispatch_target(target, repository=repository, workflow=workflow)
    _dispatch_actor(actor)
    expected_endpoint = (
        f"https://api.github.com/repos/{repository['full_name']}/actions/workflows/"
        f"{workflow['id']}/dispatches"
    )
    if journal.get("endpoint") != expected_endpoint:
        raise ReleaseControlError("dispatch journal endpoint identity mismatch")
    if journal.get("api_version") != DISPATCH_API_VERSION:
        raise ReleaseControlError("dispatch API version mismatch")
    if journal.get("method") != "POST":
        raise ReleaseControlError("dispatch method mismatch")
    if journal.get("response_contract") != DISPATCH_RESPONSE_CONTRACT:
        raise ReleaseControlError("dispatch response contract mismatch")
    if (
        journal.get("accept") != "application/vnd.github+json"
        or journal.get("content_type") != "application/json"
    ):
        raise ReleaseControlError("dispatch journal content negotiation mismatch")
    inputs = _object(journal.get("inputs"), label="dispatch journal inputs")
    if set(inputs) != {
        "candidate_run_id",
        "candidate_manifest_digest",
        "dispatch_binding",
        "mode",
        "transaction_nonce",
    }:
        raise ReleaseControlError("dispatch journal input fields mismatch")
    if inputs.get("transaction_nonce") != transaction_nonce:
        raise ReleaseControlError("dispatch journal nonce/input mismatch")
    if inputs.get("mode") != target.get("mode"):
        raise ReleaseControlError("dispatch journal mode/input mismatch")
    without_binding = dict(inputs)
    recorded_binding = without_binding.pop("dispatch_binding", None)
    expected_binding = dispatch_binding(
        short_ref=cast(str, target["short_ref"]),
        inputs_without_binding=without_binding,
    )
    if recorded_binding != expected_binding or journal.get("dispatch_binding") != expected_binding:
        raise ReleaseControlError("dispatch journal binding mismatch")
    expected_title = f"Kestrel release tx {transaction_nonce} bind {expected_binding}"
    if journal.get("expected_display_title") != expected_title:
        raise ReleaseControlError("dispatch journal title mismatch")
    request = {"ref": target["short_ref"], "inputs": inputs}
    if journal.get("canonical_request_sha256") != _sha256(canonical_json_bytes(request)):
        raise ReleaseControlError("dispatch journal request digest mismatch")
    parse_timestamp(journal.get("prepared_at"), label="dispatch prepared_at")
    started = _safe_integer(
        journal.get("monotonic_started_seconds"),
        label="dispatch monotonic start",
    )
    deadline = _safe_integer(
        journal.get("monotonic_deadline_seconds"),
        label="dispatch monotonic deadline",
    )
    if deadline != started + DISPATCH_RECONCILIATION_SECONDS:
        raise ReleaseControlError("dispatch monotonic deadline is not fixed")
    if journal.get("transport_policy") != {
        "maximum_wire_transmissions": 1,
        "redirects": False,
        "retries": False,
        "auth_replay": False,
        "proxies": False,
        "failover": False,
    }:
        raise ReleaseControlError("dispatch transport policy mismatch")
    return journal


def _validate_dispatch_send_boundary(
    value: Mapping[str, object], *, journal: JSONObject
) -> JSONObject:
    boundary = _copy_json_object(value, label="dispatch send boundary")
    _require_exact_fields(
        boundary,
        frozenset(
            {
                "schema",
                "state",
                "transaction_nonce",
                "journal_digest",
                "request_digest",
                "started_at",
                "token_fingerprint",
                "pre_send_writer_inventory_digest",
                "transport_policy",
                "validation_status",
            }
        ),
        label="dispatch send boundary",
    )
    if (
        boundary.get("schema") != "kestrel.dispatch_send_boundary.v1"
        or boundary.get("state") != "sending"
        or boundary.get("transaction_nonce") != journal.get("transaction_nonce")
        or boundary.get("journal_digest") != _sha256(canonical_json_bytes(journal))
        or boundary.get("request_digest") != journal.get("canonical_request_sha256")
        or boundary.get("transport_policy") != journal.get("transport_policy")
        or boundary.get("validation_status") != "validated"
    ):
        raise ReleaseControlError("dispatch send boundary binding mismatch")
    _digest(
        boundary.get("token_fingerprint"),
        label="dispatch send boundary token fingerprint",
    )
    _digest(
        boundary.get("pre_send_writer_inventory_digest"),
        label="pre-send writer inventory digest",
    )
    started_at = parse_timestamp(
        boundary.get("started_at"), label="dispatch send boundary started_at"
    )
    prepared_at = parse_timestamp(journal.get("prepared_at"), label="dispatch prepared_at")
    if started_at < prepared_at:
        raise ReleaseControlError("dispatch send boundary precedes preparation")
    return boundary


def _validate_dispatch_intent(value: Mapping[str, object]) -> JSONObject:
    intent = _copy_json_object(value, label="dispatch intent")
    _validate_schema(DISPATCH_INTENT_SCHEMA, intent, label="dispatch intent")
    repository = _object(intent.get("repository"), label="dispatch intent repository")
    workflow = _object(intent.get("workflow"), label="dispatch intent workflow")
    target = _object(intent.get("target"), label="dispatch intent target")
    actor = _object(intent.get("actor"), label="dispatch intent actor")
    _dispatch_repository(repository)
    _dispatch_workflow(workflow)
    _dispatch_target(target, repository=repository, workflow=workflow)
    _dispatch_actor(actor)
    transaction_nonce = _nonce(intent.get("transaction_nonce"))
    inputs = _object(intent.get("inputs"), label="dispatch intent inputs")
    if set(inputs) != {
        "candidate_run_id",
        "candidate_manifest_digest",
        "dispatch_binding",
        "mode",
        "transaction_nonce",
    }:
        raise ReleaseControlError("dispatch intent input fields mismatch")
    if inputs.get("transaction_nonce") != transaction_nonce:
        raise ReleaseControlError("dispatch intent nonce/input mismatch")
    if inputs.get("mode") != target.get("mode"):
        raise ReleaseControlError("dispatch intent mode/input mismatch")
    without_binding = dict(inputs)
    recorded_binding = without_binding.pop("dispatch_binding", None)
    expected_binding = dispatch_binding(
        short_ref=cast(str, target["short_ref"]),
        inputs_without_binding=without_binding,
    )
    if recorded_binding != expected_binding or intent.get("dispatch_binding") != expected_binding:
        raise ReleaseControlError("dispatch intent binding mismatch")
    expected_title = f"Kestrel release tx {transaction_nonce} bind {expected_binding}"
    if intent.get("expected_display_title") != expected_title:
        raise ReleaseControlError("dispatch intent title mismatch")
    request = {"ref": target["short_ref"], "inputs": inputs}
    if intent.get("request_digest") != _sha256(canonical_json_bytes(request)):
        raise ReleaseControlError("dispatch intent request digest mismatch")
    _digest(intent.get("transaction_digest"), label="dispatch intent transaction digest")
    parse_timestamp(intent.get("issued_at"), label="dispatch intent issued_at")
    return intent


def _documented_dispatch_nonacceptance(
    *,
    http_status: int,
    response_headers: bytes | None,
    response_body: bytes | None,
) -> bool:
    if (
        http_status not in {400, 401, 403, 404, 409, 422}
        or response_headers is None
        or response_body is None
        or not response_body
    ):
        return False
    try:
        raw_headers = _array(
            strict_canonical_json(response_headers, label="dispatch response headers"),
            label="dispatch response headers",
        )
        headers: dict[str, str] = {}
        for raw_header in raw_headers:
            header = _array(raw_header, label="dispatch response header")
            if len(header) != 2:
                return False
            name = _validate_string(header[0], label="dispatch response header name")
            value = _validate_string(header[1], label="dispatch response header value")
            if name != name.lower() or name in headers:
                return False
            headers[name] = value
        content_type = headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type not in {
            "application/json",
            "application/vnd.github+json",
        }:
            return False
        _validate_string(
            headers.get("x-github-request-id"),
            label="dispatch GitHub request ID",
        )
        body = _object(
            parse_external_json_bytes(response_body, label="dispatch nonacceptance response body"),
            label="dispatch nonacceptance response body",
        )
        _validate_string(body.get("message"), label="dispatch nonacceptance message")
    except ReleaseControlError:
        return False
    return True


def classify_dispatch_transport(
    *,
    journal: Mapping[str, object],
    http_status: int | None,
    response_headers: bytes | None,
    response_body: bytes | None,
    response_observed_at: datetime | None,
    locally_proven_prewrite_failure: bool,
    send_started_at: datetime | None = None,
) -> JSONObject:
    """Classify one possible POST result without ever granting retry authority."""

    checked = _validate_dispatch_journal(journal)
    if type(locally_proven_prewrite_failure) is not bool:
        raise ReleaseControlError("dispatch prewrite proof must be boolean")
    classification = "outcome_unknown"
    returned_run: JSONObject | None = None
    if locally_proven_prewrite_failure:
        if http_status is not None or response_headers is not None or response_body is not None:
            raise ReleaseControlError("dispatch prewrite proof conflicts with response evidence")
        classification = "not_accepted"
    elif http_status is not None:
        if type(http_status) is not int or not 100 <= http_status <= 599:
            raise ReleaseControlError("dispatch HTTP status is invalid")
        if http_status == 200 and response_body is not None:
            try:
                body = _object(
                    parse_external_json_bytes(response_body, label="dispatch response body"),
                    label="dispatch response body",
                )
                _require_exact_fields(
                    body,
                    frozenset({"workflow_run_id", "run_url", "html_url"}),
                    label="dispatch response body",
                )
                run_id = _safe_integer(
                    body.get("workflow_run_id"),
                    label="dispatch returned run ID",
                    positive=True,
                )
                repository = _object(checked["repository"], label="dispatch journal repository")
                repository_name = str(repository["full_name"])
                expected_run_url = (
                    f"https://api.github.com/repos/{repository_name}/actions/runs/{run_id}"
                )
                expected_html_url = f"https://github.com/{repository_name}/actions/runs/{run_id}"
                if (
                    body.get("run_url") != expected_run_url
                    or body.get("html_url") != expected_html_url
                ):
                    raise ReleaseControlError("dispatch returned run URL identity mismatch")
                returned_run = {
                    "id": run_id,
                    "run_url": expected_run_url,
                    "html_url": expected_html_url,
                }
                classification = "response_details_received"
            except ReleaseControlError:
                classification = "outcome_unknown"
                returned_run = None
        elif _documented_dispatch_nonacceptance(
            http_status=http_status,
            response_headers=response_headers,
            response_body=response_body,
        ):
            classification = "not_accepted"
    if response_observed_at is not None:
        observed_at: str | None = _format_timestamp(
            response_observed_at, label="dispatch response clock"
        )
    else:
        observed_at = None
    if http_status is not None and observed_at is None:
        raise ReleaseControlError("dispatch response time is required with a response")
    started_at = (
        cast(str, checked["prepared_at"])
        if send_started_at is None
        else _format_timestamp(send_started_at, label="dispatch send clock")
    )
    return {
        "api_version": DISPATCH_API_VERSION,
        "endpoint": checked["endpoint"],
        "method": "POST",
        "classification": classification,
        "send_started_at": started_at,
        "response_observed_at": observed_at,
        "http_status": http_status,
        "response_headers_sha256": (
            None if response_headers is None else _sha256(response_headers)
        ),
        "response_body_sha256": None if response_body is None else _sha256(response_body),
        "returned_run": returned_run,
    }


def create_dispatch_containment(
    *,
    journal: Mapping[str, object],
    dispatch: Mapping[str, object],
    send_boundary: Mapping[str, object],
    installed_apps_snapshot: bytes,
    uninstall_observation: bytes,
    token_probe_observation: bytes,
    post_containment_writer_inventory: bytes,
    post_containment_writer_inventory_signature: bytes,
    owner_signing_keys_observation: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    """Prove the dispatcher installation is gone and its exact token is invalid."""

    checked = _validate_dispatch_journal(journal)
    checked_dispatch = _validate_dispatch_transport_record(dispatch, journal=checked)
    boundary = _validate_dispatch_send_boundary(send_boundary, journal=checked)
    token_fingerprint = _digest(
        boundary.get("token_fingerprint"),
        label="dispatch send boundary token fingerprint",
    )
    pre_send_writer_inventory_digest = _digest(
        boundary.get("pre_send_writer_inventory_digest"),
        label="pre-send writer inventory digest",
    )
    send_started_at = parse_timestamp(
        boundary.get("started_at"), label="dispatch send boundary started_at"
    )
    if checked_dispatch.get("send_started_at") != boundary.get("started_at"):
        raise ReleaseControlError("dispatch send boundary time mismatch")
    prepared_at = parse_timestamp(checked.get("prepared_at"), label="dispatch prepared_at")
    actor = _object(checked["actor"], label="dispatch journal actor")
    apps = _object(
        strict_canonical_json(installed_apps_snapshot, label="installed Apps snapshot"),
        label="installed Apps snapshot",
    )
    _require_exact_fields(
        apps,
        frozenset({"schema", "apps", "captured_at", "complete"}),
        label="installed Apps snapshot",
    )
    if (
        apps.get("schema") != "kestrel.installed_apps_snapshot.v1"
        or apps.get("complete") is not True
    ):
        raise ReleaseControlError("installed Apps snapshot is incomplete")
    captured_at = parse_timestamp(apps.get("captured_at"), label="installed Apps captured_at")
    raw_apps = _array(apps.get("apps"), label="installed Apps")
    if raw_apps:
        raise ReleaseControlError("dispatcher App remains installed")

    uninstall = _object(
        strict_canonical_json(uninstall_observation, label="dispatcher uninstall observation"),
        label="dispatcher uninstall observation",
    )
    _require_exact_fields(
        uninstall,
        frozenset({"schema", "app_id", "installation_id", "uninstalled_at", "complete"}),
        label="dispatcher uninstall observation",
    )
    if (
        uninstall.get("schema") != "kestrel.dispatcher_uninstall_observation.v1"
        or uninstall.get("complete") is not True
        or uninstall.get("app_id") != actor.get("app_id")
        or uninstall.get("installation_id") != actor.get("installation_id")
    ):
        raise ReleaseControlError("dispatcher uninstall identity mismatch")
    uninstalled_at = parse_timestamp(
        uninstall.get("uninstalled_at"), label="dispatcher uninstalled_at"
    )

    probe = _object(
        strict_canonical_json(token_probe_observation, label="dispatcher token probe"),
        label="dispatcher token probe",
    )
    _require_exact_fields(
        probe,
        frozenset(
            {
                "schema",
                "endpoint",
                "http_status",
                "observed_at",
                "response_sha256",
                "token_fingerprint",
                "complete",
            }
        ),
        label="dispatcher token probe",
    )
    if (
        probe.get("schema") != "kestrel.dispatcher_token_probe.v1"
        or probe.get("complete") is not True
    ):
        raise ReleaseControlError("dispatcher token probe is incomplete")
    if probe.get("endpoint") != "GET /installation/repositories":
        raise ReleaseControlError("dispatcher token probe endpoint mismatch")
    if probe.get("http_status") != 401:
        raise ReleaseControlError("dispatcher exact token probe must return 401")
    _digest(probe.get("response_sha256"), label="dispatcher token probe response digest")
    if (
        _digest(
            probe.get("token_fingerprint"),
            label="dispatcher token probe token fingerprint",
        )
        != token_fingerprint
    ):
        raise ReleaseControlError("dispatcher token probe token binding mismatch")
    probe_at = parse_timestamp(probe.get("observed_at"), label="dispatcher token probe time")
    response_observed_at = checked_dispatch.get("response_observed_at")
    response_at = (
        send_started_at
        if response_observed_at is None
        else parse_timestamp(response_observed_at, label="dispatch response_observed_at")
    )
    now = parse_timestamp(
        _format_timestamp(_clock(), label="dispatch containment clock"),
        label="dispatch containment clock",
    )
    post_writer_inventory = verify_repository_writer_inventory(
        inventory=post_containment_writer_inventory,
        signature=post_containment_writer_inventory_signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        journal=checked,
        phase="post_containment",
        expected_run_id=None,
        _clock=lambda: now,
    )
    post_writer_captured_at = parse_timestamp(
        post_writer_inventory.get("captured_at"),
        label="post-containment writer inventory captured_at",
    )
    if post_writer_captured_at < captured_at:
        raise ReleaseControlError("post-containment writer inventory ordering mismatch")
    if not (
        prepared_at
        <= send_started_at
        <= response_at
        <= uninstalled_at
        <= probe_at
        <= captured_at
        <= now
    ):
        raise ReleaseControlError("dispatcher containment observation ordering mismatch")
    if (now - captured_at).total_seconds() > CURRENT_CAPTURE_WINDOW_SECONDS:
        raise ReleaseControlError("dispatcher containment evidence is stale")
    return {
        "installation_id": actor["installation_id"],
        "uninstalled_at": uninstall["uninstalled_at"],
        "installed_apps_snapshot_sha256": _sha256(installed_apps_snapshot),
        "pre_send_writer_inventory_digest": pre_send_writer_inventory_digest,
        "post_containment_writer_inventory_digest": _sha256(post_containment_writer_inventory),
        "token_probe": {
            "endpoint": probe["endpoint"],
            "http_status": probe["http_status"],
            "observed_at": probe["observed_at"],
            "response_sha256": _sha256(token_probe_observation),
        },
        "validated": True,
    }


def _dispatch_transaction_projection(journal: JSONObject) -> JSONObject:
    repository = _object(journal["repository"], label="dispatch journal repository")
    workflow = _object(journal["workflow"], label="dispatch journal workflow")
    target = _object(journal["target"], label="dispatch journal target")
    actor = _object(journal["actor"], label="dispatch journal actor")
    return {
        "transaction_nonce": journal["transaction_nonce"],
        "dispatch_binding": journal["dispatch_binding"],
        "logical_dispatch_ordinal": journal["logical_dispatch_ordinal"],
        "repository": repository,
        "workflow": {
            "id": workflow["id"],
            "path": workflow["path"],
            "default_branch_sha": workflow["default_branch_sha"],
        },
        "target": target,
        "actor": actor,
        "request_sha256": journal["canonical_request_sha256"],
    }


def _validate_dispatch_transaction_projection(
    value: Mapping[str, object],
) -> JSONObject:
    transaction = _copy_json_object(value, label="dispatch reconciliation transaction")
    _require_exact_fields(
        transaction,
        frozenset(
            {
                "transaction_nonce",
                "dispatch_binding",
                "logical_dispatch_ordinal",
                "repository",
                "workflow",
                "target",
                "actor",
                "request_sha256",
            }
        ),
        label="dispatch reconciliation transaction",
    )
    _nonce(transaction.get("transaction_nonce"))
    _digest(transaction.get("dispatch_binding"), label="dispatch binding")
    if transaction.get("logical_dispatch_ordinal") != 1:
        raise ReleaseControlError("dispatch logical ordinal must be one")
    repository = _dispatch_repository(
        _object(transaction.get("repository"), label="dispatch repository")
    )
    workflow = _copy_json_object(
        _object(transaction.get("workflow"), label="dispatch workflow"),
        label="dispatch workflow",
    )
    _require_exact_fields(
        workflow,
        frozenset({"id", "path", "default_branch_sha"}),
        label="dispatch workflow",
    )
    _safe_integer(workflow.get("id"), label="dispatch workflow ID", positive=True)
    if workflow.get("path") != DISPATCH_WORKFLOW_PATH:
        raise ReleaseControlError("dispatch workflow path mismatch")
    _git_sha(workflow.get("default_branch_sha"), label="dispatch workflow SHA")
    _dispatch_target(
        _object(transaction.get("target"), label="dispatch target"),
        repository=repository,
        workflow=workflow,
    )
    _dispatch_actor(_object(transaction.get("actor"), label="dispatch actor"))
    _digest(transaction.get("request_sha256"), label="dispatch request digest")
    return transaction


def _validate_dispatch_transport_record(
    value: Mapping[str, object], *, journal: JSONObject
) -> JSONObject:
    dispatch = _copy_json_object(value, label="dispatch transport record")
    _require_exact_fields(
        dispatch,
        frozenset(
            {
                "api_version",
                "endpoint",
                "method",
                "classification",
                "send_started_at",
                "response_observed_at",
                "http_status",
                "response_headers_sha256",
                "response_body_sha256",
                "returned_run",
            }
        ),
        label="dispatch transport record",
    )
    if (
        dispatch.get("api_version") != DISPATCH_API_VERSION
        or dispatch.get("endpoint") != journal.get("endpoint")
        or dispatch.get("method") != "POST"
    ):
        raise ReleaseControlError("dispatch transport binding mismatch")
    classification = dispatch.get("classification")
    if classification not in {
        "response_details_received",
        "not_accepted",
        "outcome_unknown",
    }:
        raise ReleaseControlError("dispatch transport classification is invalid")
    parse_timestamp(dispatch.get("send_started_at"), label="dispatch send_started_at")
    response_time = dispatch.get("response_observed_at")
    if response_time is not None:
        parse_timestamp(response_time, label="dispatch response_observed_at")
    status = dispatch.get("http_status")
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ReleaseControlError("dispatch transport HTTP status is invalid")
    if (status is None) != (response_time is None):
        raise ReleaseControlError("dispatch transport HTTP status/response time mismatch")
    for field in ("response_headers_sha256", "response_body_sha256"):
        if dispatch.get(field) is not None:
            _digest(dispatch.get(field), label=f"dispatch {field}")
    returned = dispatch.get("returned_run")
    if classification == "response_details_received":
        returned_run = _object(returned, label="dispatch returned run")
        _require_exact_fields(
            returned_run,
            frozenset({"id", "run_url", "html_url"}),
            label="dispatch returned run",
        )
        run_id = _safe_integer(
            returned_run.get("id"), label="dispatch returned run ID", positive=True
        )
        if (
            status != 200
            or response_time is None
            or dispatch.get("response_headers_sha256") is None
            or dispatch.get("response_body_sha256") is None
        ):
            raise ReleaseControlError("dispatch returned details lack exact HTTP evidence")
        repository = _object(journal.get("repository"), label="dispatch returned run repository")
        repository_name = repository["full_name"]
        if returned_run != {
            "id": run_id,
            "run_url": (f"https://api.github.com/repos/{repository_name}/actions/runs/{run_id}"),
            "html_url": (f"https://github.com/{repository_name}/actions/runs/{run_id}"),
        }:
            raise ReleaseControlError("dispatch returned run URL identity mismatch")
    elif returned is not None:
        raise ReleaseControlError("dispatch returned run is forbidden for this classification")
    return dispatch


def _validate_dispatch_containment_record(
    value: Mapping[str, object], *, journal: JSONObject
) -> JSONObject:
    containment = _copy_json_object(value, label="dispatch containment")
    _require_exact_fields(
        containment,
        frozenset(
            {
                "installation_id",
                "uninstalled_at",
                "installed_apps_snapshot_sha256",
                "pre_send_writer_inventory_digest",
                "post_containment_writer_inventory_digest",
                "token_probe",
                "validated",
            }
        ),
        label="dispatch containment",
    )
    actor = _object(journal["actor"], label="dispatch journal actor")
    if containment.get("installation_id") != actor.get("installation_id"):
        raise ReleaseControlError("dispatch containment installation mismatch")
    parse_timestamp(containment.get("uninstalled_at"), label="dispatch uninstalled_at")
    _digest(
        containment.get("installed_apps_snapshot_sha256"),
        label="installed Apps snapshot digest",
    )
    _digest(
        containment.get("pre_send_writer_inventory_digest"),
        label="pre-send writer inventory digest",
    )
    _digest(
        containment.get("post_containment_writer_inventory_digest"),
        label="post-containment writer inventory digest",
    )
    probe = _object(containment.get("token_probe"), label="dispatch token probe")
    _require_exact_fields(
        probe,
        frozenset({"endpoint", "http_status", "observed_at", "response_sha256"}),
        label="dispatch token probe",
    )
    if (
        probe.get("endpoint") != "GET /installation/repositories"
        or probe.get("http_status") != 401
        or containment.get("validated") is not True
    ):
        raise ReleaseControlError("dispatch containment is not validated")
    parse_timestamp(probe.get("observed_at"), label="dispatch token probe time")
    _digest(probe.get("response_sha256"), label="dispatch token probe digest")
    return containment


def _validate_poll(
    value: Mapping[str, object], *, expected_ordinal: int
) -> tuple[JSONObject, datetime, set[int], set[int]]:
    poll = _copy_json_object(value, label="dispatch reconciliation poll")
    _require_exact_fields(
        poll,
        frozenset(
            {
                "ordinal",
                "requested_at",
                "workflow_observation_sha256",
                "query",
                "pages",
                "complete",
                "result_count",
                "nonce_run_ids",
                "binding_conflict_run_ids",
                "rejection_reasons",
            }
        ),
        label="dispatch reconciliation poll",
    )
    if poll.get("ordinal") != expected_ordinal:
        raise ReleaseControlError("dispatch poll ordinal is not consecutive")
    requested_at = parse_timestamp(poll.get("requested_at"), label="dispatch poll requested_at")
    _digest(
        poll.get("workflow_observation_sha256"),
        label="dispatch poll workflow observation digest",
    )
    query = _validate_string(poll.get("query"), label="dispatch poll query")
    if not query.endswith("?event=workflow_dispatch&per_page=100"):
        raise ReleaseControlError("dispatch poll query is not the exhaustive frozen query")
    pages = _array(poll.get("pages"), label="dispatch poll pages")
    if not pages or len(pages) > 100:
        raise ReleaseControlError("dispatch poll page cardinality is invalid")
    for index, raw_page in enumerate(pages, start=1):
        page = _object(raw_page, label="dispatch poll page")
        _require_exact_fields(
            page,
            frozenset({"number", "http_status", "response_sha256", "next"}),
            label="dispatch poll page",
        )
        if page.get("number") != index or page.get("http_status") != 200:
            raise ReleaseControlError("dispatch poll page is incomplete")
        _digest(page.get("response_sha256"), label="dispatch poll page digest")
        expected_next = None if index == len(pages) else index + 1
        if page.get("next") != expected_next:
            raise ReleaseControlError("dispatch poll pagination chain is incomplete")
    if type(poll.get("complete")) is not bool:
        raise ReleaseControlError("dispatch poll completeness is invalid")
    result_count = _safe_integer(poll.get("result_count"), label="dispatch poll result count")
    if result_count >= 1000:
        raise ReleaseControlError("dispatch poll reached the 1,000-result ceiling")

    def ids(field: str) -> tuple[list[JSONValue], set[int]]:
        raw_ids = _array(poll.get(field), label=f"dispatch poll {field}")
        checked_ids = [
            _safe_integer(item, label=f"dispatch poll {field} item", positive=True)
            for item in raw_ids
        ]
        if checked_ids != sorted(checked_ids) or len(set(checked_ids)) != len(checked_ids):
            raise ReleaseControlError(f"dispatch poll {field} is not sorted unique")
        return cast(list[JSONValue], checked_ids), set(checked_ids)

    nonce_values, nonce_ids = ids("nonce_run_ids")
    conflict_values, conflict_ids = ids("binding_conflict_run_ids")
    if nonce_ids & conflict_ids:
        raise ReleaseControlError("dispatch poll run cannot be exact and conflicting")
    reasons = _array(poll.get("rejection_reasons"), label="dispatch poll rejection reasons")
    checked_reasons = [
        _validate_string(reason, label="dispatch poll rejection reason") for reason in reasons
    ]
    if checked_reasons != sorted(set(checked_reasons)):
        raise ReleaseControlError("dispatch poll rejection reasons are not sorted unique")
    sanitized: JSONObject = {
        **poll,
        "nonce_run_ids": nonce_values,
        "binding_conflict_run_ids": conflict_values,
        "rejection_reasons": cast(list[JSONValue], checked_reasons),
    }
    return sanitized, requested_at, nonce_ids, conflict_ids


def _candidate_predicate(
    value: Mapping[str, object], *, journal: JSONObject
) -> tuple[JSONObject, list[str]]:
    candidate = _copy_json_object(value, label="dispatch candidate")
    _require_exact_fields(
        candidate,
        frozenset(
            {
                "run_id",
                "list_observation_sha256",
                "get_run_observation_sha256",
                "run",
                "identity_artifact",
            }
        ),
        label="dispatch candidate",
    )
    run_id = _safe_integer(candidate.get("run_id"), label="candidate run ID", positive=True)
    _digest(candidate.get("list_observation_sha256"), label="candidate list digest")
    _digest(candidate.get("get_run_observation_sha256"), label="candidate GET-run digest")
    run = _object(candidate.get("run"), label="candidate REST run")
    _require_exact_fields(
        run,
        frozenset(
            {
                "workflow_id",
                "repository_id",
                "repository_full_name",
                "path",
                "event",
                "display_title",
                "head_branch",
                "head_sha",
                "run_attempt",
                "actor_login",
                "actor_id",
                "triggering_actor_login",
                "triggering_actor_id",
                "status",
                "conclusion",
            }
        ),
        label="candidate REST run",
    )
    artifact = _object(candidate.get("identity_artifact"), label="candidate identity artifact")
    _require_exact_fields(
        artifact,
        frozenset(
            {
                "artifact_id",
                "name",
                "api_digest",
                "archive_sha256",
                "content_sha256",
                "expired",
                "matching_name_count",
                "identity",
            }
        ),
        label="candidate identity artifact",
    )
    identity = _object(artifact.get("identity"), label="dispatch identity")
    reasons: list[str] = []

    repository = _object(journal["repository"], label="dispatch journal repository")
    workflow = _object(journal["workflow"], label="dispatch journal workflow")
    target = _object(journal["target"], label="dispatch journal target")
    actor = _object(journal["actor"], label="dispatch journal actor")
    expected_run = {
        "workflow_id": workflow["id"],
        "repository_id": repository["id"],
        "repository_full_name": repository["full_name"],
        "event": "workflow_dispatch",
        "display_title": journal["expected_display_title"],
        "head_branch": target["short_ref"],
        "head_sha": target["head_sha"],
        "run_attempt": 1,
        "actor_login": actor["login"],
        "actor_id": actor["id"],
        "triggering_actor_login": actor["login"],
        "triggering_actor_id": actor["id"],
    }
    for field, expected in expected_run.items():
        if run.get(field) != expected:
            reasons.append(f"run_{field}_mismatch")
    if run_id != identity.get("run_id"):
        reasons.append("identity_run_id_mismatch")
    path = run.get("path")
    path_parts = path.rsplit("@", 1) if type(path) is str else []
    if len(path_parts) != 2 or path_parts[0] != workflow.get("path"):
        reasons.append("run_workflow_path_mismatch")
    elif path_parts[1] != target.get("short_ref"):
        reasons.append("run_workflow_ref_suffix_mismatch")
    if run.get("run_attempt") != 1:
        reasons.append("run_attempt_not_one")

    try:
        _require_exact_fields(
            identity,
            frozenset(
                {
                    "schema",
                    "transaction_nonce",
                    "dispatch_binding",
                    "dispatch_inputs_digest",
                    "repository",
                    "repository_id",
                    "workflow",
                    "workflow_ref",
                    "workflow_sha",
                    "event_name",
                    "ref",
                    "sha",
                    "run_id",
                    "run_attempt",
                    "actor",
                    "actor_id",
                    "triggering_actor",
                    "observed_at",
                    "evidence",
                    "provenance",
                    "confidence",
                    "validation_status",
                }
            ),
            label="dispatch identity",
        )
    except ReleaseControlError:
        reasons.append("identity_fields_mismatch")
    try:
        _validate_schema(
            DISPATCH_IDENTITY_SCHEMA,
            identity,
            label="candidate dispatch identity",
        )
    except ReleaseControlError:
        reasons.append("identity_schema_mismatch")
    expected_identity = {
        "schema": DISPATCH_IDENTITY_SCHEMA,
        "transaction_nonce": journal["transaction_nonce"],
        "dispatch_binding": journal["dispatch_binding"],
        "dispatch_inputs_digest": _sha256(
            canonical_json_bytes(_object(journal["inputs"], label="dispatch inputs"))
        ),
        "repository": repository["full_name"],
        "repository_id": repository["id"],
        "workflow": "Release",
        "workflow_ref": target["workflow_ref"],
        "workflow_sha": target["workflow_sha"],
        "event_name": "workflow_dispatch",
        "ref": target["full_ref"],
        "sha": target["head_sha"],
        "run_id": run_id,
        "run_attempt": 1,
        "actor": actor["login"],
        "actor_id": actor["id"],
        "triggering_actor": actor["login"],
        "confidence": 1,
        "validation_status": "validated",
    }
    for field, expected in expected_identity.items():
        if identity.get(field) != expected:
            reasons.append(f"identity_{field}_mismatch")
    identity_observed_at: str | None = None
    try:
        parse_timestamp(identity.get("observed_at"), label="dispatch identity observed_at")
        identity_observed_at = cast(str, identity.get("observed_at"))
    except ReleaseControlError:
        reasons.append("identity_observed_at_invalid")
    if artifact.get("name") != f"kestrel-dispatch-identity-{run_id}-1":
        reasons.append("identity_artifact_name_mismatch")
    if artifact.get("matching_name_count") != 1:
        reasons.append("identity_artifact_name_cardinality_mismatch")
    if artifact.get("expired") is not False:
        reasons.append("identity_artifact_expired")
    try:
        api_digest = _digest(artifact.get("api_digest"), label="identity artifact API digest")
        archive_digest = _digest(
            artifact.get("archive_sha256"), label="identity artifact archive digest"
        )
        content_digest = _digest(
            artifact.get("content_sha256"), label="identity artifact content digest"
        )
        if api_digest != archive_digest:
            reasons.append("identity_artifact_archive_digest_mismatch")
        if content_digest != _sha256(canonical_json_bytes(identity)):
            reasons.append("identity_artifact_content_digest_mismatch")
    except ReleaseControlError:
        reasons.append("identity_artifact_digest_invalid")
    sanitized_artifact: JSONObject = {
        key: artifact[key]
        for key in (
            "artifact_id",
            "name",
            "api_digest",
            "archive_sha256",
            "content_sha256",
            "expired",
        )
    }
    sanitized_artifact["identity_observed_at"] = identity_observed_at
    sanitized: JSONObject = {
        "run_id": run_id,
        "list_observation_sha256": candidate["list_observation_sha256"],
        "get_run_observation_sha256": candidate["get_run_observation_sha256"],
        "run": run,
        "identity_artifact": sanitized_artifact,
        "predicate": "accepted" if not reasons else "rejected",
        "reasons": cast(list[JSONValue], sorted(set(reasons))),
    }
    return sanitized, sorted(set(reasons))


def _dispatch_tombstone(
    *, journal: JSONObject, known_run_ids: list[int], created_at: str, reason_code: str
) -> JSONObject:
    transaction = _dispatch_transaction_projection(journal)
    return {
        "schema": DISPATCH_TOMBSTONE_SCHEMA,
        "transaction_nonce": journal["transaction_nonce"],
        "dispatch_binding": journal["dispatch_binding"],
        "known_run_ids": cast(list[JSONValue], known_run_ids),
        "prohibition": "never_issue_dispatch_admission",
        "reason_code": reason_code,
        "created_at": created_at,
        "channel_locator": (
            "github-release://John-MiracleWorker/Kestrel-Release-Recovery/"
            f"dispatch-tombstone-{journal['transaction_nonce']}"
        ),
        "evidence": {
            "source_bundle_digest": source_bundle_digest(
                {"dispatch-transaction": canonical_json_bytes(transaction)}
            ),
            "canonicalization_vector_digest": canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "controller",
            "method": "dispatch-terminal-tombstone",
        },
        "confidence": 1,
        "validation_status": "pending_signature",
    }


def reconstruct_dispatch_tombstone(
    *, reconciliation: Mapping[str, object], reason_code: str
) -> JSONObject:
    """Reconstruct the exact pending tombstone committed by reconciliation."""

    checked = _copy_json_object(reconciliation, label="dispatch reconciliation")
    _validate_schema(
        DISPATCH_RECONCILIATION_SCHEMA,
        checked,
        label="dispatch reconciliation",
    )
    if checked.get("validation_status") != "pending_tombstone_signature":
        raise ReleaseControlError("dispatch reconciliation has no pending tombstone")
    outcome = _object(checked.get("outcome"), label="dispatch reconciliation outcome")
    checked_reason = _validate_string(reason_code, label="dispatch tombstone reason")
    if outcome.get("state") == "run_adopted" or outcome.get("reason_code") != checked_reason:
        raise ReleaseControlError("dispatch tombstone reason mismatch")
    reference = _object(checked.get("tombstone"), label="dispatch reconciliation tombstone")
    transaction = _object(checked.get("transaction"), label="dispatch reconciliation transaction")
    tombstone: JSONObject = {
        "schema": DISPATCH_TOMBSTONE_SCHEMA,
        "transaction_nonce": reference["transaction_nonce"],
        "dispatch_binding": reference["dispatch_binding"],
        "known_run_ids": reference["known_run_ids"],
        "prohibition": reference["prohibition"],
        "reason_code": checked_reason,
        "created_at": reference["created_at"],
        "channel_locator": reference["channel_locator"],
        "evidence": {
            "source_bundle_digest": source_bundle_digest(
                {"dispatch-transaction": canonical_json_bytes(transaction)}
            ),
            "canonicalization_vector_digest": canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "controller",
            "method": "dispatch-terminal-tombstone",
        },
        "confidence": 1,
        "validation_status": "pending_signature",
    }
    _validate_schema(DISPATCH_TOMBSTONE_SCHEMA, tombstone, label="dispatch tombstone")
    if reference.get("canonical_sha256") != _sha256(canonical_json_bytes(tombstone)):
        raise ReleaseControlError("dispatch tombstone reconstruction digest mismatch")
    return tombstone


def reconcile_dispatch(
    *,
    journal: Mapping[str, object],
    dispatch: Mapping[str, object],
    containment: Mapping[str, object],
    polls: Sequence[Mapping[str, object]],
    candidates: Sequence[Mapping[str, object]],
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[JSONObject, JSONObject | None]:
    """Classify exact zero/one/many outcomes after containment; never POST."""

    checked_journal = _validate_dispatch_journal(journal)
    checked_dispatch = _validate_dispatch_transport_record(dispatch, journal=checked_journal)
    checked_containment = _validate_dispatch_containment_record(
        containment, journal=checked_journal
    )
    send_started_at = parse_timestamp(
        checked_dispatch.get("send_started_at"),
        label="dispatch reconciliation send_started_at",
    )
    response_value = checked_dispatch.get("response_observed_at")
    response_observed_at = (
        send_started_at
        if response_value is None
        else parse_timestamp(
            response_value,
            label="dispatch reconciliation response_observed_at",
        )
    )
    uninstalled_at = parse_timestamp(
        checked_containment.get("uninstalled_at"),
        label="dispatch reconciliation uninstalled_at",
    )
    containment_probe = _object(
        checked_containment.get("token_probe"),
        label="dispatch reconciliation token probe",
    )
    probe_observed_at = parse_timestamp(
        containment_probe.get("observed_at"),
        label="dispatch reconciliation token probe observed_at",
    )
    if not (send_started_at <= response_observed_at <= uninstalled_at <= probe_observed_at):
        raise ReleaseControlError("dispatch reconciliation containment ordering mismatch")
    definitely_not_accepted = checked_dispatch.get("classification") == "not_accepted"
    if definitely_not_accepted and (polls or candidates):
        raise ReleaseControlError(
            "definitely rejected dispatch cannot have reconciliation observations"
        )
    if (not definitely_not_accepted and not polls) or len(polls) > 121:
        raise ReleaseControlError("dispatch reconciliation poll cardinality is invalid")
    sanitized_polls: list[JSONObject] = []
    poll_times: list[datetime] = []
    cumulative_nonce_ids: set[int] = set()
    cumulative_conflict_ids: set[int] = set()
    prior_nonce_ids: set[int] = set()
    observation_complete = True
    repository = _object(
        checked_journal.get("repository"), label="dispatch reconciliation repository"
    )
    workflow = _object(checked_journal.get("workflow"), label="dispatch reconciliation workflow")
    expected_query = (
        f"GET /repos/{repository['full_name']}/actions/workflows/{workflow['id']}/"
        "runs?event=workflow_dispatch&per_page=100"
    )
    for ordinal, raw_poll in enumerate(polls, start=1):
        poll, requested_at, nonce_ids, conflict_ids = _validate_poll(
            raw_poll, expected_ordinal=ordinal
        )
        if poll.get("query") != expected_query:
            raise ReleaseControlError("dispatch reconciliation poll query binding mismatch")
        if not prior_nonce_ids <= nonce_ids:
            observation_complete = False
            poll["complete"] = False
            poll_reasons = _array(poll["rejection_reasons"], label="poll rejection reasons")
            poll["rejection_reasons"] = cast(
                list[JSONValue],
                sorted({*map(str, poll_reasons), "nonce_run_disappeared"}),
            )
        prior_nonce_ids = set(nonce_ids)
        cumulative_nonce_ids.update(nonce_ids)
        cumulative_conflict_ids.update(conflict_ids)
        if poll.get("complete") is not True:
            observation_complete = False
        sanitized_polls.append(poll)
        poll_times.append(requested_at)

    candidate_by_id: dict[int, JSONObject] = {}
    candidate_reasons: dict[int, list[str]] = {}
    for raw_candidate in candidates:
        candidate, reasons = _candidate_predicate(raw_candidate, journal=checked_journal)
        run_id = cast(int, candidate["run_id"])
        if run_id in candidate_by_id:
            raise ReleaseControlError("duplicate dispatch candidate observation")
        candidate_by_id[run_id] = candidate
        candidate_reasons[run_id] = reasons
    known_ids = sorted(cumulative_nonce_ids | cumulative_conflict_ids)
    now = parse_timestamp(
        _format_timestamp(_clock(), label="dispatch reconciliation clock"),
        label="dispatch reconciliation clock",
    )
    state: str
    reason_code: str
    adopted_run_id: int | None = None
    if definitely_not_accepted:
        state = "unresolved_zero"
        reason_code = "dispatch_not_accepted"
    elif cumulative_conflict_ids:
        state = "nonce_binding_conflict"
        reason_code = "dispatch_conflict"
    elif len(cumulative_nonce_ids) > 1:
        state = "duplicate_dispatch_detected"
        reason_code = "dispatch_ambiguous"
    elif not observation_complete:
        state = "reconciliation_unavailable"
        reason_code = "observation_incomplete"
    elif not cumulative_nonce_ids:
        state = "unresolved_zero"
        reason_code = "dispatch_not_observed"
    else:
        singleton = next(iter(cumulative_nonce_ids))
        returned = checked_dispatch.get("returned_run")
        if returned is not None and _object(returned, label="returned run").get("id") != singleton:
            state = "response_identity_conflict"
            reason_code = "response_run_id_mismatch"
        elif singleton not in candidate_by_id:
            state = "unresolved_single_unproven"
            reason_code = "dispatch_identity_missing"
        elif candidate_reasons[singleton]:
            state = "unsafe_orphan_or_tamper"
            reason_code = candidate_reasons[singleton][0]
        else:
            stable = len(sanitized_polls) >= 3
            if stable:
                last_three = sanitized_polls[-3:]
                stable = all(
                    set(cast(list[int], poll["nonce_run_ids"])) == {singleton}
                    and not cast(list[int], poll["binding_conflict_run_ids"])
                    and poll["complete"] is True
                    for poll in last_three
                )
                stable = stable and all(
                    (poll_times[index] - poll_times[index - 1]).total_seconds() == 5
                    for index in range(len(poll_times) - 2, len(poll_times))
                )
            if stable:
                state = "run_adopted"
                reason_code = "exact_singleton_attempt_1"
                adopted_run_id = singleton
            else:
                state = "unresolved_single_unproven"
                reason_code = "quiescence_not_proven"

    if definitely_not_accepted:
        token_probe = _object(
            checked_containment.get("token_probe"),
            label="dispatch containment token probe",
        )
        started_at = parse_timestamp(
            token_probe.get("observed_at"),
            label="dispatch containment completion time",
        )
        deadline_at = started_at
        schedule: list[int] = []
    else:
        started_at = poll_times[0]
        deadline_at = started_at + timedelta(seconds=DISPATCH_RECONCILIATION_SECONDS)
        schedule = [int((instant - started_at).total_seconds()) for instant in poll_times]
        if now < poll_times[-1]:
            raise ReleaseControlError("dispatch reconciliation evidence is in the future")
    if any(offset < 0 or offset > DISPATCH_RECONCILIATION_SECONDS for offset in schedule):
        raise ReleaseControlError("dispatch reconciliation poll is outside the fixed deadline")
    if (
        state in {"unresolved_zero", "unresolved_single_unproven"}
        and not definitely_not_accepted
        and now < deadline_at
    ):
        raise DispatchReconciliationPending(
            "dispatch reconciliation remains pending until the fixed deadline"
        )
    if definitely_not_accepted:
        decided = started_at
    elif (
        state
        in {
            "unresolved_zero",
            "unresolved_single_unproven",
            "reconciliation_unavailable",
        }
        and now >= deadline_at
    ):
        decided = deadline_at
    else:
        decided = poll_times[-1]
    decided_at = _format_timestamp(decided, label="dispatch reconciliation decision")
    sanitized_candidates = [candidate_by_id[key] for key in sorted(candidate_by_id)]
    outcome: JSONObject = {
        "state": state,
        "cardinality": len(cumulative_nonce_ids) + len(cumulative_conflict_ids),
        "adopted_run_id": adopted_run_id,
        "reason_code": reason_code,
        "decided_at": decided_at,
    }
    tombstone: JSONObject | None = None
    tombstone_reference: JSONObject | None = None
    if state != "run_adopted":
        tombstone = _dispatch_tombstone(
            journal=checked_journal,
            known_run_ids=known_ids,
            created_at=decided_at,
            reason_code=reason_code,
        )
        tombstone_reference = {
            "transaction_nonce": checked_journal["transaction_nonce"],
            "dispatch_binding": checked_journal["dispatch_binding"],
            "known_run_ids": cast(list[JSONValue], known_ids),
            "prohibition": "never_issue_dispatch_admission",
            "created_at": decided_at,
            "channel_locator": tombstone["channel_locator"],
            "canonical_sha256": _sha256(canonical_json_bytes(tombstone)),
            "signature_sha256": None,
            "validation_status": "pending_signature",
        }
    evidence: list[JSONValue] = []
    for poll in sanitized_polls:
        for page in _array(poll["pages"], label="dispatch poll pages"):
            page_object = _object(page, label="dispatch poll page")
            page_bytes = canonical_json_bytes(page_object)
            evidence.append(
                {
                    "name": f"poll-{cast(int, poll['ordinal']):03d}-page-{cast(int, page_object['number']):03d}",
                    "provider": "github.com",
                    "endpoint": poll["query"],
                    "sha256": _sha256(page_bytes),
                    "size_bytes": len(page_bytes),
                    "media_type": "application/json",
                    "captured_at": poll["requested_at"],
                }
            )
    if definitely_not_accepted:
        dispatch_bytes = canonical_json_bytes(checked_dispatch)
        evidence.append(
            {
                "name": "dispatch-transport",
                "provider": "github.com",
                "endpoint": checked_dispatch["endpoint"],
                "sha256": _sha256(dispatch_bytes),
                "size_bytes": len(dispatch_bytes),
                "media_type": "application/json",
                "captured_at": (
                    checked_dispatch["response_observed_at"] or checked_dispatch["send_started_at"]
                ),
            }
        )
    reconciliation: JSONObject = {
        "schema": DISPATCH_RECONCILIATION_SCHEMA,
        "transaction": _dispatch_transaction_projection(checked_journal),
        "dispatch": checked_dispatch,
        "containment": checked_containment,
        "polling": {
            "started_at": _format_timestamp(started_at, label="reconciliation start"),
            "deadline_at": _format_timestamp(deadline_at, label="reconciliation deadline"),
            "schedule_seconds": cast(list[JSONValue], schedule),
            "polls": cast(list[JSONValue], sanitized_polls),
            "complete": observation_complete,
        },
        "candidates": cast(list[JSONValue], sanitized_candidates),
        "outcome": outcome,
        "tombstone": tombstone_reference,
        "evidence": evidence,
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "version": "1",
            "source_sha": _git_sha(
                _object(checked_journal["target"], label="dispatch target")["head_sha"],
                label="dispatch source SHA",
            ),
        },
        "validation_status": (
            "validated" if state == "run_adopted" else "pending_tombstone_signature"
        ),
    }
    _validate_schema(
        DISPATCH_RECONCILIATION_SCHEMA,
        reconciliation,
        label="dispatch reconciliation",
    )
    if tombstone is not None:
        _validate_schema(
            DISPATCH_TOMBSTONE_SCHEMA,
            tombstone,
            label="dispatch tombstone",
        )
    return reconciliation, tombstone


def _verify_pre_admission_writer_authority(
    *,
    reconciliation: JSONObject,
    adopted_run_id: int,
    pre_admission_writer_inventory: bytes,
    pre_admission_writer_inventory_signature: bytes,
    owner_signing_keys_observation: bytes,
    minimum_writer_inventory_captured_at: datetime,
    now: datetime,
) -> str:
    writer_inventory = verify_repository_writer_inventory(
        inventory=pre_admission_writer_inventory,
        signature=pre_admission_writer_inventory_signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        transaction=_object(
            reconciliation.get("transaction"),
            label="dispatch reconciliation transaction",
        ),
        phase="pre_admission",
        expected_run_id=adopted_run_id,
        _clock=lambda: now,
    )
    minimum_captured_at = parse_timestamp(
        _format_timestamp(
            minimum_writer_inventory_captured_at,
            label="minimum writer inventory capture time",
        ),
        label="minimum writer inventory capture time",
    )
    writer_captured_at = parse_timestamp(
        writer_inventory.get("captured_at"),
        label="pre-admission writer inventory captured_at",
    )
    if writer_captured_at < minimum_captured_at:
        raise ReleaseControlError(
            "pre-admission writer inventory predates the final workflow refresh"
        )
    _, fingerprint = owner_signing_key(
        owner_signing_keys_observation=owner_signing_keys_observation,
        principal=SIGNING_PRINCIPAL,
        _clock=lambda: now,
    )
    return fingerprint


def create_dispatch_admission(
    *,
    reconciliation: Mapping[str, object],
    identity_observation: Mapping[str, object],
    pre_admission_writer_inventory: bytes,
    pre_admission_writer_inventory_signature: bytes,
    owner_signing_keys_observation: bytes,
    minimum_writer_inventory_captured_at: datetime,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    """Derive a short-lived pre-environment admission from exact adoption only."""

    checked = _copy_json_object(reconciliation, label="dispatch reconciliation")
    _validate_schema(
        DISPATCH_RECONCILIATION_SCHEMA,
        checked,
        label="dispatch reconciliation",
    )
    if checked.get("schema") != DISPATCH_RECONCILIATION_SCHEMA:
        raise ReleaseControlError("dispatch reconciliation schema mismatch")
    outcome = _object(checked.get("outcome"), label="dispatch reconciliation outcome")
    if outcome.get("state") != "run_adopted" or outcome.get("adopted_run_id") is None:
        raise ReleaseControlError("dispatch admission requires an adopted run")
    if checked.get("tombstone") is not None:
        raise ReleaseControlError("dispatch admission is forbidden by a tombstone")
    containment = _object(checked.get("containment"), label="dispatch reconciliation containment")
    if containment.get("validated") is not True:
        raise ReleaseControlError("dispatch admission requires validated containment")
    adopted_run_id = _safe_integer(
        outcome.get("adopted_run_id"), label="adopted run ID", positive=True
    )
    candidates = _array(checked.get("candidates"), label="dispatch candidates")
    matches = [
        _object(candidate, label="dispatch candidate")
        for candidate in candidates
        if _object(candidate, label="dispatch candidate").get("run_id") == adopted_run_id
    ]
    if len(matches) != 1 or matches[0].get("predicate") != "accepted":
        raise ReleaseControlError("dispatch admission adopted candidate is not exact")
    identity = _copy_json_object(identity_observation, label="adopted dispatch identity")
    _validate_schema(
        DISPATCH_IDENTITY_SCHEMA,
        identity,
        label="adopted dispatch identity",
    )
    if (
        identity.get("schema") != DISPATCH_IDENTITY_SCHEMA
        or identity.get("run_id") != adopted_run_id
        or _sha256(canonical_json_bytes(identity))
        != _object(
            matches[0].get("identity_artifact"),
            label="adopted identity artifact",
        ).get("content_sha256")
    ):
        raise ReleaseControlError("dispatch admission identity observation mismatch")
    identity_observed_at = parse_timestamp(
        identity.get("observed_at"), label="adopted identity observed_at"
    )
    now = _clock()
    fingerprint = _verify_pre_admission_writer_authority(
        reconciliation=checked,
        adopted_run_id=adopted_run_id,
        pre_admission_writer_inventory=pre_admission_writer_inventory,
        pre_admission_writer_inventory_signature=(pre_admission_writer_inventory_signature),
        owner_signing_keys_observation=owner_signing_keys_observation,
        minimum_writer_inventory_captured_at=minimum_writer_inventory_captured_at,
        now=now,
    )
    return _build_dispatch_admission(
        reconciliation=checked,
        identity_observed_at=identity_observed_at,
        signing_principal=SIGNING_PRINCIPAL,
        signing_key_fingerprint=fingerprint,
        _clock=lambda: now,
    )


def _build_dispatch_admission(
    *,
    reconciliation: JSONObject,
    identity_observed_at: datetime,
    signing_principal: str,
    signing_key_fingerprint: str,
    _clock: Callable[[], datetime],
) -> JSONObject:
    outcome = _object(reconciliation.get("outcome"), label="dispatch reconciliation outcome")
    adopted_run_id = _safe_integer(
        outcome.get("adopted_run_id"), label="adopted run ID", positive=True
    )
    containment = _object(
        reconciliation.get("containment"), label="dispatch reconciliation containment"
    )
    transaction = _object(
        reconciliation.get("transaction"), label="dispatch reconciliation transaction"
    )
    repository = _object(transaction.get("repository"), label="dispatch repository")
    workflow = _object(transaction.get("workflow"), label="dispatch workflow")
    target = _object(transaction.get("target"), label="dispatch target")
    principal = _validate_string(signing_principal, label="admission signing principal")
    if principal != SIGNING_PRINCIPAL:
        raise ReleaseControlError("dispatch admission signing principal mismatch")
    fingerprint = _digest(signing_key_fingerprint, label="admission signing key fingerprint")
    issued = _clock()
    issued_at = _format_timestamp(issued, label="dispatch admission issuance clock")
    issued_datetime = parse_timestamp(issued_at, label="dispatch admission issued_at")
    if identity_observed_at > issued_datetime:
        raise ReleaseControlError("dispatch admission identity evidence is in the future")
    expires = min(
        identity_observed_at + timedelta(seconds=900),
        issued_datetime + timedelta(seconds=300),
    )
    if expires <= issued_datetime:
        raise ReleaseControlError("dispatch admission identity evidence is too old")
    admission: JSONObject = {
        "schema": DISPATCH_ADMISSION_SCHEMA,
        "transaction_nonce": transaction["transaction_nonce"],
        "dispatch_binding": transaction["dispatch_binding"],
        "reconciliation_digest": _sha256(canonical_json_bytes(reconciliation)),
        "adopted_run_id": adopted_run_id,
        "run_attempt": 1,
        "containment_digest": _sha256(canonical_json_bytes(containment)),
        "repository": repository["full_name"],
        "repository_id": repository["id"],
        "workflow_id": workflow["id"],
        "workflow_path": workflow["path"],
        "expected_ref": target["full_ref"],
        "expected_head_sha": target["head_sha"],
        "issued_at": issued_at,
        "expires_at": _format_timestamp(expires, label="dispatch admission expires_at"),
        "signing_principal": principal,
        "signing_key_fingerprint": fingerprint,
    }
    _validate_schema(DISPATCH_ADMISSION_SCHEMA, admission, label="dispatch admission")
    return admission


def create_dispatch_admission_from_reconciliation(
    *,
    reconciliation: Mapping[str, object],
    containment: Mapping[str, object],
    owner_signing_keys_observation: bytes,
    pre_admission_writer_inventory: bytes,
    pre_admission_writer_inventory_signature: bytes,
    minimum_writer_inventory_captured_at: datetime,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    """Create admission using only adopted receipt metadata and fresh owner trust."""

    checked = _copy_json_object(reconciliation, label="dispatch reconciliation")
    _validate_schema(
        DISPATCH_RECONCILIATION_SCHEMA,
        checked,
        label="dispatch reconciliation",
    )
    outcome = _object(checked.get("outcome"), label="dispatch reconciliation outcome")
    if outcome.get("state") != "run_adopted" or checked.get("tombstone") is not None:
        raise ReleaseControlError("dispatch admission requires untombstoned adoption")
    checked_containment = _copy_json_object(containment, label="dispatch containment")
    embedded_containment = _object(
        checked.get("containment"), label="dispatch reconciliation containment"
    )
    if checked_containment != embedded_containment:
        raise ReleaseControlError("dispatch admission containment binding mismatch")
    adopted_run_id = _safe_integer(
        outcome.get("adopted_run_id"), label="adopted run ID", positive=True
    )
    candidates = _array(checked.get("candidates"), label="dispatch candidates")
    matches = [
        _object(candidate, label="dispatch candidate")
        for candidate in candidates
        if _object(candidate, label="dispatch candidate").get("run_id") == adopted_run_id
    ]
    if len(matches) != 1 or matches[0].get("predicate") != "accepted":
        raise ReleaseControlError("dispatch admission adopted candidate is not exact")
    artifact = _object(matches[0].get("identity_artifact"), label="adopted identity artifact")
    identity_observed_at = parse_timestamp(
        artifact.get("identity_observed_at"),
        label="adopted identity observed_at",
    )
    now = _clock()
    fingerprint = _verify_pre_admission_writer_authority(
        reconciliation=checked,
        adopted_run_id=adopted_run_id,
        pre_admission_writer_inventory=pre_admission_writer_inventory,
        pre_admission_writer_inventory_signature=(pre_admission_writer_inventory_signature),
        owner_signing_keys_observation=owner_signing_keys_observation,
        minimum_writer_inventory_captured_at=minimum_writer_inventory_captured_at,
        now=now,
    )
    return _build_dispatch_admission(
        reconciliation=checked,
        identity_observed_at=identity_observed_at,
        signing_principal=SIGNING_PRINCIPAL,
        signing_key_fingerprint=fingerprint,
        _clock=lambda: now,
    )


DISPATCH_STATE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "prepared": frozenset({"sending"}),
    "sending": frozenset({"response_details_received", "not_accepted", "outcome_unknown"}),
    "response_details_received": frozenset({"app_containment"}),
    "not_accepted": frozenset({"app_containment"}),
    "outcome_unknown": frozenset({"app_containment"}),
    "app_containment": frozenset({"contained", "containment_failed"}),
    "contained": frozenset({"reconciling", "aborted_fail_closed"}),
    "containment_failed": frozenset({"aborted_fail_closed"}),
    "reconciling": frozenset(
        {
            "run_adopted",
            "duplicate_dispatch_detected",
            "nonce_binding_conflict",
            "unresolved_zero",
            "unresolved_single_unproven",
            "reconciliation_unavailable",
            "response_identity_conflict",
            "unsafe_orphan_or_tamper",
        }
    ),
    "duplicate_dispatch_detected": frozenset({"aborted_fail_closed"}),
    "nonce_binding_conflict": frozenset({"aborted_fail_closed"}),
    "unresolved_zero": frozenset({"aborted_fail_closed"}),
    "unresolved_single_unproven": frozenset({"aborted_fail_closed"}),
    "reconciliation_unavailable": frozenset({"aborted_fail_closed"}),
    "response_identity_conflict": frozenset({"aborted_fail_closed"}),
    "unsafe_orphan_or_tamper": frozenset({"aborted_fail_closed"}),
    "run_adopted": frozenset({"admission_published"}),
    "admission_published": frozenset({"admission_verified_in_run"}),
    "admission_verified_in_run": frozenset({"approval_eligible"}),
    "approval_eligible": frozenset(),
    "aborted_fail_closed": frozenset(),
}


def transition_dispatch_state(current: str, next_state: str) -> str:
    checked_current = _validate_string(current, label="current dispatch state")
    checked_next = _validate_string(next_state, label="next dispatch state")
    if checked_next not in DISPATCH_STATE_TRANSITIONS.get(checked_current, frozenset()):
        raise ReleaseControlError(
            f"dispatch state transition {checked_current}->{checked_next} is forbidden"
        )
    return checked_next


def finalize_dispatch_tombstone(
    *,
    reconciliation: Mapping[str, object],
    tombstone: Mapping[str, object],
    identity_file: Path,
    owner_signing_keys_observation: bytes,
    principal: str,
    namespace: str,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[JSONObject, JSONObject, bytes]:
    """Bind, owner-sign, and validate the mandatory terminal tombstone."""

    checked_reconciliation = _copy_json_object(reconciliation, label="dispatch reconciliation")
    if checked_reconciliation.get("schema") != DISPATCH_RECONCILIATION_SCHEMA:
        raise ReleaseControlError("dispatch reconciliation schema mismatch")
    outcome = _object(
        checked_reconciliation.get("outcome"),
        label="dispatch reconciliation outcome",
    )
    if outcome.get("state") == "run_adopted":
        raise ReleaseControlError("tombstone finalization requires a non-adopted outcome")
    reference = _object(
        checked_reconciliation.get("tombstone"),
        label="dispatch reconciliation tombstone",
    )
    checked_tombstone = _copy_json_object(tombstone, label="dispatch tombstone")
    if checked_tombstone.get("schema") != DISPATCH_TOMBSTONE_SCHEMA:
        raise ReleaseControlError("dispatch tombstone schema mismatch")
    if checked_tombstone.get("validation_status") != "pending_signature":
        raise ReleaseControlError("dispatch tombstone is not pending signature")
    for field in (
        "transaction_nonce",
        "dispatch_binding",
        "known_run_ids",
        "prohibition",
        "created_at",
        "channel_locator",
    ):
        if checked_tombstone.get(field) != reference.get(field):
            raise ReleaseControlError(f"dispatch tombstone {field} binding mismatch")
    if reference.get("canonical_sha256") != _sha256(canonical_json_bytes(checked_tombstone)):
        raise ReleaseControlError("dispatch tombstone draft digest mismatch")
    if (
        reference.get("signature_sha256") is not None
        or reference.get("validation_status") != "pending_signature"
    ):
        raise ReleaseControlError("dispatch tombstone reference was already finalized")
    finalized_tombstone = dict(checked_tombstone)
    finalized_tombstone["validation_status"] = "validated"
    _validate_schema(
        DISPATCH_TOMBSTONE_SCHEMA,
        finalized_tombstone,
        label="finalized dispatch tombstone",
    )
    finalized_bytes = canonical_json_bytes(finalized_tombstone)
    signature = sign_receipt_detached(
        receipt=finalized_bytes,
        identity_file=identity_file,
        principal=principal,
        namespace=namespace,
    )
    verify_owner_detached_signature(
        receipt=finalized_bytes,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        principal=principal,
        namespace=namespace,
        _clock=_clock,
    )
    finalized_reference = dict(reference)
    finalized_reference.update(
        {
            "canonical_sha256": _sha256(finalized_bytes),
            "signature_sha256": _sha256(signature),
            "validation_status": "validated",
        }
    )
    finalized_reconciliation = dict(checked_reconciliation)
    finalized_reconciliation["tombstone"] = finalized_reference
    finalized_reconciliation["validation_status"] = "validated"
    _validate_schema(
        DISPATCH_RECONCILIATION_SCHEMA,
        finalized_reconciliation,
        label="finalized dispatch reconciliation",
    )
    return finalized_reconciliation, finalized_tombstone, signature


def _validate_recovery_reader_scope(scope: JSONObject, *, now: datetime) -> None:
    _validate_schema(CREDENTIAL_SCOPE_SCHEMA, scope, label="recovery reader scope authority")
    if scope.get("purpose") != "recovery_reader":
        raise ReleaseControlError("recovery reader scope purpose mismatch")
    if scope.get("revoked") is not False:
        raise ReleaseControlError("recovery reader scope is revoked")
    issued = parse_timestamp(scope.get("issued_at"), label="recovery reader issued_at")
    expires = parse_timestamp(scope.get("expires_at"), label="recovery reader expires_at")
    if now < issued:
        raise ReleaseControlError("recovery reader scope is not yet valid")
    if now >= expires:
        raise ReleaseControlError("recovery reader scope is expired")
    _, policies = _credential_policy()
    policy = policies["recovery_reader"]
    repositories = _array(scope.get("repositories"), label="recovery reader repositories")
    repository_names = [
        _object(repository, label="recovery reader repository").get("full_name")
        for repository in repositories
    ]
    if repository_names != policy.get("repositories") or repository_names != [
        "John-MiracleWorker/Kestrel-Release-Recovery"
    ]:
        raise ReleaseControlError("recovery reader scope repository mismatch")
    repository_ids = {
        str(_object(repository, label="recovery reader repository")["full_name"]): _object(
            repository, label="recovery reader repository"
        )["id"]
        for repository in repositories
    }
    expected_grants = [
        {
            "repository_full_name": grant["repository_full_name"],
            "repository_id": repository_ids[str(grant["repository_full_name"])],
            "permission": grant["permission"],
            "level": grant["level"],
        }
        for grant in cast(list[JSONObject], policy["grants"])
    ]
    if scope.get("grants") != expected_grants:
        raise ReleaseControlError("recovery reader scope grant mismatch or over-scope")
    if scope.get("endpoint_allowlist") != policy.get("endpoint_allowlist"):
        raise ReleaseControlError("recovery reader scope endpoint mismatch")


def verify_dispatch_admission(
    *,
    admission: bytes,
    signature: bytes,
    owner_signing_keys_observation: bytes,
    current_run_identity: Mapping[str, object],
    recovery_scope_authority: bytes,
    recovery_scope_signature: bytes,
    recovery_runtime_verification: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    """Verify admission, current run, owner trust, and recovery-reader scope."""

    value = _object(
        strict_canonical_json(admission, label="dispatch admission"),
        label="dispatch admission",
    )
    _validate_schema(DISPATCH_ADMISSION_SCHEMA, value, label="dispatch admission")
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ReleaseControlError("dispatch admission verification clock must be aware UTC")
    now = now.astimezone(UTC).replace(microsecond=0)
    issued = parse_timestamp(value.get("issued_at"), label="dispatch admission issued_at")
    expires = parse_timestamp(value.get("expires_at"), label="dispatch admission expires_at")
    if expires > issued + timedelta(seconds=300):
        raise ReleaseControlError("dispatch admission lifetime exceeds 300 seconds")
    if now < issued:
        raise ReleaseControlError("dispatch admission is not yet valid")
    if now >= expires:
        raise ReleaseControlError("dispatch admission is expired")
    principal = _validate_string(
        value.get("signing_principal"), label="dispatch admission signing principal"
    )
    fingerprint = _digest(
        value.get("signing_key_fingerprint"),
        label="dispatch admission signing key fingerprint",
    )
    if signature_public_key_fingerprint(signature) != fingerprint:
        raise ReleaseControlError("dispatch admission signature fingerprint mismatch")
    verify_owner_detached_signature(
        receipt=admission,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        principal=principal,
        namespace=SIGNING_NAMESPACE,
        _clock=lambda: now,
    )

    identity = _copy_json_object(current_run_identity, label="current dispatch run identity")
    _validate_schema(
        DISPATCH_IDENTITY_SCHEMA,
        identity,
        label="current dispatch run identity",
    )
    identity_bindings = {
        "transaction_nonce": value["transaction_nonce"],
        "dispatch_binding": value["dispatch_binding"],
        "repository": value["repository"],
        "repository_id": value["repository_id"],
        "run_id": value["adopted_run_id"],
        "run_attempt": value["run_attempt"],
        "ref": value["expected_ref"],
        "sha": value["expected_head_sha"],
    }
    for field, expected in identity_bindings.items():
        if identity.get(field) != expected:
            raise ReleaseControlError(f"dispatch admission current run {field} mismatch")
    expected_workflow_ref = (
        f"{value['repository']}/{value['workflow_path']}@{value['expected_ref']}"
    )
    if identity.get("workflow_ref") != expected_workflow_ref:
        raise ReleaseControlError("dispatch admission current run workflow ref mismatch")
    if identity.get("workflow_sha") != value.get("expected_head_sha"):
        raise ReleaseControlError("dispatch admission current run workflow SHA mismatch")

    scope = _object(
        strict_canonical_json(recovery_scope_authority, label="recovery reader scope authority"),
        label="recovery reader scope authority",
    )
    scope_fingerprint = _digest(
        scope.get("signing_key_fingerprint"),
        label="recovery reader signing key fingerprint",
    )
    if signature_public_key_fingerprint(recovery_scope_signature) != scope_fingerprint:
        raise ReleaseControlError("recovery reader scope signature fingerprint mismatch")
    verify_owner_detached_signature(
        receipt=recovery_scope_authority,
        signature=recovery_scope_signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        principal=principal,
        namespace=SIGNING_NAMESPACE,
        _clock=lambda: now,
    )
    _validate_recovery_reader_scope(scope, now=now)

    runtime = _object(
        strict_canonical_json(
            recovery_runtime_verification,
            label="recovery reader runtime verification",
        ),
        label="recovery reader runtime verification",
    )
    _validate_schema(
        RUNTIME_CREDENTIAL_SCHEMA,
        runtime,
        label="recovery reader runtime verification",
    )
    if (
        runtime.get("credential_id") != scope.get("credential_id")
        or runtime.get("purpose") != "recovery_reader"
        or runtime.get("token_fingerprint") != scope.get("token_fingerprint")
        or runtime.get("scope_authority_digest") != _sha256(recovery_scope_authority)
    ):
        raise ReleaseControlError("recovery reader runtime verification binding mismatch")
    verified_at = parse_timestamp(
        runtime.get("verified_at"), label="recovery reader runtime verified_at"
    )
    if verified_at > now:
        raise ReleaseControlError("recovery reader runtime verification is in the future")
    if (now - verified_at).total_seconds() > CURRENT_CAPTURE_WINDOW_SECONDS:
        raise ReleaseControlError("recovery reader runtime verification is stale")
    return {
        "receipt_digest": _sha256(admission),
        "signature_digest": _sha256(signature),
        "verification_digest": source_bundle_digest(
            {
                "admission": admission,
                "admission-signature": signature,
                "current-run-identity": canonical_json_bytes(identity),
                "owner-signing-keys-observation": owner_signing_keys_observation,
                "recovery-runtime-verification": recovery_runtime_verification,
                "recovery-scope-authority": recovery_scope_authority,
                "recovery-scope-signature": recovery_scope_signature,
            }
        ),
    }


def _validate_dispatch_admission_verification(
    value: Mapping[str, object],
    *,
    admission: bytes,
    signature: bytes,
) -> JSONObject:
    verification = _copy_json_object(value, label="dispatch admission verification")
    _require_exact_fields(
        verification,
        frozenset({"receipt_digest", "signature_digest", "verification_digest"}),
        label="dispatch admission verification",
    )
    if _digest(
        verification.get("receipt_digest"),
        label="dispatch admission verification receipt digest",
    ) != _sha256(admission) or _digest(
        verification.get("signature_digest"),
        label="dispatch admission verification signature digest",
    ) != _sha256(signature):
        raise ReleaseControlError("dispatch admission verification binding mismatch")
    _digest(
        verification.get("verification_digest"),
        label="dispatch admission verification evidence digest",
    )
    return verification


def _validate_capsule_dispatch_admission_binding(
    transaction: Mapping[str, object],
    admission: Mapping[str, object],
    *,
    signature: bytes,
) -> None:
    checked_transaction = _copy_json_object(transaction, label="capsule transaction authorization")
    _validate_schema(
        "kestrel.release_server_authorization.v3",
        checked_transaction,
        label="capsule transaction authorization",
    )
    checked_admission = _copy_json_object(admission, label="capsule dispatch admission")
    _validate_schema(
        DISPATCH_ADMISSION_SCHEMA,
        checked_admission,
        label="capsule dispatch admission",
    )
    if (
        checked_transaction.get("authorization_kind") != "transaction"
        or checked_transaction.get("mode") != "initiate"
    ):
        raise ReleaseControlError(
            "capsule dispatch admission requires initiate transaction authority"
        )
    run = _object(
        checked_transaction.get("promotion_run"),
        label="capsule transaction promotion run",
    )
    candidate = _object(checked_transaction.get("candidate"), label="capsule transaction candidate")
    expected = {
        "transaction_nonce": run.get("transaction_nonce"),
        "adopted_run_id": run.get("run_id"),
        "run_attempt": run.get("run_attempt"),
        "repository": "John-MiracleWorker/Kestrel",
        "repository_id": run.get("repository_id"),
        "workflow_id": run.get("workflow_id"),
        "workflow_path": run.get("workflow_path"),
        "expected_ref": run.get("ref"),
        "expected_head_sha": candidate.get("source_sha"),
    }
    if any(checked_admission.get(field) != value for field, value in expected.items()):
        raise ReleaseControlError("capsule dispatch admission transaction binding mismatch")
    if (
        run.get("head_sha") != candidate.get("source_sha")
        or run.get("workflow_sha") != candidate.get("source_sha")
        or checked_admission.get("signing_principal") != SIGNING_PRINCIPAL
        or checked_admission.get("signing_key_fingerprint")
        != signature_public_key_fingerprint(signature)
    ):
        raise ReleaseControlError("capsule dispatch admission run or signer binding mismatch")
    issued = parse_timestamp(
        checked_admission.get("issued_at"),
        label="capsule dispatch admission issued_at",
    )
    expires = parse_timestamp(
        checked_admission.get("expires_at"),
        label="capsule dispatch admission expires_at",
    )
    if expires <= issued or expires > issued + timedelta(seconds=300):
        raise ReleaseControlError("capsule dispatch admission lifetime is invalid")


def _credential_policy() -> tuple[JSONObject, dict[str, JSONObject]]:
    policy = _object(
        _load_canonical_file(
            CREDENTIAL_POLICY_PATH,
            label="credential policy",
            max_bytes=1024 * 1024,
        ),
        label="credential policy",
    )
    _require_exact_fields(policy, frozenset({"schema", "purposes"}), label="credential policy")
    if policy.get("schema") != CREDENTIAL_POLICY_SCHEMA:
        raise ReleaseControlError("credential policy schema mismatch")
    purposes = _array(policy.get("purposes"), label="credential policy purposes")
    if not purposes or len(purposes) > len(CREDENTIAL_PURPOSES):
        raise ReleaseControlError("credential policy purpose cardinality mismatch")
    indexed: dict[str, JSONObject] = {}
    ordered_names: list[str] = []
    for raw_purpose in purposes:
        purpose = _object(raw_purpose, label="credential policy purpose")
        _require_exact_fields(
            purpose,
            frozenset(
                {
                    "purpose",
                    "repositories",
                    "grants",
                    "endpoint_allowlist",
                    "read_only",
                }
            ),
            label="credential policy purpose",
        )
        name = _validate_string(purpose.get("purpose"), label="credential purpose")
        if name not in CREDENTIAL_PURPOSES:
            raise ReleaseControlError("credential policy purpose is invalid")
        if name in indexed:
            raise ReleaseControlError("duplicate credential policy purpose")
        repositories = _array(purpose.get("repositories"), label=f"{name} credential repositories")
        checked_repositories = [
            _validate_string(item, label=f"{name} credential repository") for item in repositories
        ]
        if (
            not checked_repositories
            or checked_repositories != sorted(checked_repositories)
            or len(set(checked_repositories)) != len(checked_repositories)
        ):
            raise ReleaseControlError(f"{name} credential repository policy is invalid")
        raw_grants = _array(purpose.get("grants"), label=f"{name} credential grants")
        checked_grants: list[JSONObject] = []
        grant_identities: set[tuple[str, str]] = set()
        for raw_grant in raw_grants:
            grant = _object(raw_grant, label=f"{name} credential grant")
            _require_exact_fields(
                grant,
                frozenset({"repository_full_name", "permission", "level"}),
                label=f"{name} credential grant",
            )
            repository = _validate_string(
                grant.get("repository_full_name"), label="credential grant repository"
            )
            permission = _validate_string(
                grant.get("permission"), label="credential grant permission"
            )
            level = _validate_string(grant.get("level"), label="credential grant level")
            if repository not in checked_repositories:
                raise ReleaseControlError(f"{name} credential grant repository mismatch")
            if level not in {"none", "read", "write", "admin"}:
                raise ReleaseControlError(f"{name} credential grant level is invalid")
            identity = (repository, permission)
            if identity in grant_identities:
                raise ReleaseControlError(f"duplicate {name} credential grant")
            grant_identities.add(identity)
            checked_grants.append(grant)
        if not checked_grants or checked_grants != sorted(
            checked_grants,
            key=lambda item: (str(item["repository_full_name"]), str(item["permission"])),
        ):
            raise ReleaseControlError(f"{name} credential grants are not sorted")
        endpoints = _array(purpose.get("endpoint_allowlist"), label=f"{name} credential endpoints")
        checked_endpoints = [
            _validate_string(item, label=f"{name} credential endpoint") for item in endpoints
        ]
        if (
            not checked_endpoints
            or checked_endpoints != sorted(checked_endpoints)
            or len(set(checked_endpoints)) != len(checked_endpoints)
        ):
            raise ReleaseControlError(f"{name} credential endpoint policy is invalid")
        read_only = purpose.get("read_only")
        if type(read_only) is not bool or read_only != (name in READ_ONLY_CREDENTIAL_PURPOSES):
            raise ReleaseControlError(f"{name} credential read-only policy mismatch")
        if read_only and any(grant["level"] in {"write", "admin"} for grant in checked_grants):
            raise ReleaseControlError(f"{name} read-only credential has a write grant")
        indexed[name] = purpose
        ordered_names.append(name)
    if ordered_names != sorted(CREDENTIAL_PURPOSES) or set(indexed) != CREDENTIAL_PURPOSES:
        raise ReleaseControlError("credential policy purpose set mismatch")
    return policy, indexed


def _principal(value: object, *, label: str) -> JSONObject:
    principal = _object(value, label=label)
    _require_exact_fields(principal, frozenset({"login", "id", "type"}), label=label)
    _validate_string(principal.get("login"), label=f"{label} login")
    _safe_integer(principal.get("id"), label=f"{label} ID", positive=True)
    if principal.get("type") not in {"Bot", "User"}:
        raise ReleaseControlError(f"{label} type is invalid")
    return principal


def create_credential_scope_authority(
    *,
    purpose: str,
    credential_id: str,
    principal_observation: bytes,
    grants_snapshot: bytes,
    token_fingerprint: str,
    controller_context: bytes,
) -> JSONObject:
    """Create the exact owner-controlled scope receipt without secret bytes."""

    checked_purpose = _validate_string(purpose, label="credential purpose")
    _, purposes = _credential_policy()
    if checked_purpose not in purposes:
        raise ReleaseControlError("credential purpose is not in the exact policy")
    checked_id = _validate_string(credential_id, label="credential ID")
    if CREDENTIAL_ID_RE.fullmatch(checked_id) is None:
        raise ReleaseControlError("credential ID format is invalid")
    checked_fingerprint = _digest(token_fingerprint, label="token fingerprint")

    principal = _principal(
        strict_canonical_json(principal_observation, label="credential principal observation"),
        label="credential principal",
    )
    snapshot = _object(
        strict_canonical_json(grants_snapshot, label="credential grants snapshot"),
        label="credential grants snapshot",
    )
    _require_exact_fields(
        snapshot,
        frozenset(
            {
                "schema",
                "repositories",
                "grants",
                "endpoint_allowlist",
                "captured_at",
                "complete",
            }
        ),
        label="credential grants snapshot",
    )
    if snapshot.get("schema") != "kestrel.credential_grants_snapshot.v1":
        raise ReleaseControlError("credential grants snapshot schema mismatch")
    if snapshot.get("complete") is not True:
        raise ReleaseControlError("credential grants snapshot is incomplete")
    snapshot_captured = parse_timestamp(
        snapshot.get("captured_at"), label="credential grants captured_at"
    )

    context = _object(
        strict_canonical_json(controller_context, label="credential controller context"),
        label="credential controller context",
    )
    _require_exact_fields(
        context,
        frozenset(
            {
                "schema",
                "issuer",
                "signing_key_fingerprint",
                "issued_at",
                "expires_at",
                "captured_at",
                "complete",
            }
        ),
        label="credential controller context",
    )
    if context.get("schema") != "kestrel.credential_controller_context.v1":
        raise ReleaseControlError("credential controller context schema mismatch")
    if context.get("complete") is not True:
        raise ReleaseControlError("credential controller context is incomplete")
    issuer = _validate_string(context.get("issuer"), label="credential issuer")
    signing_key_fingerprint = _digest(
        context.get("signing_key_fingerprint"), label="signing key fingerprint"
    )
    issued = parse_timestamp(context.get("issued_at"), label="credential issued_at")
    expires = parse_timestamp(context.get("expires_at"), label="credential expires_at")
    context_captured = parse_timestamp(
        context.get("captured_at"), label="credential context captured_at"
    )
    if not issued <= snapshot_captured <= expires or not issued <= context_captured < expires:
        raise ReleaseControlError("credential expiry does not cover its authority snapshots")
    if expires <= issued:
        raise ReleaseControlError("credential expiry must be after issuance")

    policy = purposes[checked_purpose]
    expected_repository_names = cast(list[JSONValue], policy["repositories"])
    raw_repositories = _array(snapshot.get("repositories"), label="credential repositories")
    repositories: list[JSONObject] = []
    repository_ids: dict[str, int] = {}
    for raw_repository in raw_repositories:
        repository = _object(raw_repository, label="credential repository")
        _require_exact_fields(
            repository, frozenset({"full_name", "id"}), label="credential repository"
        )
        full_name = _validate_string(
            repository.get("full_name"), label="credential repository name"
        )
        repository_id = _safe_integer(
            repository.get("id"), label="credential repository ID", positive=True
        )
        if full_name in repository_ids:
            raise ReleaseControlError("duplicate credential repository")
        repository_ids[full_name] = repository_id
        repositories.append(repository)
    if [repository["full_name"] for repository in repositories] != expected_repository_names:
        raise ReleaseControlError("credential repository set does not match policy")

    raw_grants = _array(snapshot.get("grants"), label="credential grants")
    grants: list[JSONObject] = []
    grant_identities: set[tuple[str, str]] = set()
    for raw_grant in raw_grants:
        grant = _object(raw_grant, label="credential grant")
        _require_exact_fields(
            grant,
            frozenset({"repository_full_name", "repository_id", "permission", "level"}),
            label="credential grant",
        )
        repository_name = _validate_string(
            grant.get("repository_full_name"), label="credential grant repository"
        )
        repository_id = _safe_integer(
            grant.get("repository_id"), label="credential grant repository ID", positive=True
        )
        permission = _validate_string(grant.get("permission"), label="credential grant permission")
        level = _validate_string(grant.get("level"), label="credential grant level")
        identity = (repository_name, permission)
        if identity in grant_identities:
            raise ReleaseControlError("duplicate credential grant")
        grant_identities.add(identity)
        if repository_ids.get(repository_name) != repository_id:
            raise ReleaseControlError("credential grant repository identity mismatch")
        if checked_purpose in READ_ONLY_CREDENTIAL_PURPOSES and level in {"write", "admin"}:
            raise ReleaseControlError("read-only credential contains a write grant")
        grants.append(grant)
    if grants != sorted(
        grants,
        key=lambda item: (str(item["repository_full_name"]), str(item["permission"])),
    ):
        raise ReleaseControlError("credential grants are not sorted")
    expected_grants = [
        {
            "repository_full_name": grant["repository_full_name"],
            "repository_id": repository_ids[str(grant["repository_full_name"])],
            "permission": grant["permission"],
            "level": grant["level"],
        }
        for grant in cast(list[JSONObject], policy["grants"])
    ]
    if grants != expected_grants:
        raise ReleaseControlError("credential grant set does not match policy")

    endpoints = [
        _validate_string(endpoint, label="credential endpoint")
        for endpoint in _array(
            snapshot.get("endpoint_allowlist"), label="credential endpoint allowlist"
        )
    ]
    if len(set(endpoints)) != len(endpoints):
        raise ReleaseControlError("duplicate credential endpoint")
    if endpoints != policy["endpoint_allowlist"]:
        raise ReleaseControlError("credential endpoint allowlist does not match policy")

    receipt: JSONObject = {
        "schema": CREDENTIAL_SCOPE_SCHEMA,
        "credential_id": checked_id,
        "purpose": checked_purpose,
        "principal": principal,
        "repositories": cast(list[JSONValue], repositories),
        "grants": cast(list[JSONValue], grants),
        "endpoint_allowlist": cast(list[JSONValue], endpoints),
        "issued_at": context["issued_at"],
        "expires_at": context["expires_at"],
        "revoked": False,
        "token_fingerprint": checked_fingerprint,
        "issuer": issuer,
        "signing_key_fingerprint": signing_key_fingerprint,
        "evidence": {
            "source_bundle_digest": source_bundle_digest(
                {
                    "controller-context": controller_context,
                    "grants-snapshot": grants_snapshot,
                    "principal-observation": principal_observation,
                }
            ),
            "canonicalization_vector_digest": canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "controller",
            "method": "owner-signed-credential-scope",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    _validate_schema(CREDENTIAL_SCOPE_SCHEMA, receipt, label="credential scope authority")
    if (
        strict_canonical_json(
            canonical_json_bytes(receipt), label="credential scope authority output"
        )
        != receipt
    ):
        raise ReleaseControlError("credential scope authority canonical replay mismatch")
    return receipt


def verify_runtime_credential(
    *,
    scope_authority: bytes,
    scope_authority_signature: bytes,
    owner_signing_keys_observation: bytes,
    identity_probe: bytes,
    endpoint_probe_observations: bytes,
    token_bytes: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    """Verify one in-memory credential against its signed-scope record."""

    scope = _object(
        strict_canonical_json(scope_authority, label="credential scope authority"),
        label="credential scope authority",
    )
    _validate_schema(CREDENTIAL_SCOPE_SCHEMA, scope, label="credential scope authority")
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ReleaseControlError("credential verification clock must be aware UTC")
    now = now.astimezone(UTC).replace(microsecond=0)
    signature_key_blob, signature_namespace = _decode_ssh_signature(scope_authority_signature)
    if signature_namespace != SIGNING_NAMESPACE:
        raise ReleaseControlError("credential scope authority signature namespace mismatch")
    signature_fingerprint = _sha256(signature_key_blob)
    if scope.get("issuer") != SIGNING_PRINCIPAL:
        raise ReleaseControlError("credential scope authority issuer is not the owner")
    if scope.get("signing_key_fingerprint") != signature_fingerprint:
        raise ReleaseControlError(
            "credential scope authority signing fingerprint does not match its signature"
        )
    verify_owner_detached_signature(
        receipt=scope_authority,
        signature=scope_authority_signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
        _clock=lambda: now,
    )
    if scope.get("revoked") is True:
        raise ReleaseControlError("credential scope authority is revoked")
    if type(token_bytes) is not bytes or not token_bytes:
        raise ReleaseControlError("credential token bytes are missing")
    token_fingerprint = _sha256(token_bytes)
    if token_fingerprint != scope.get("token_fingerprint"):
        raise ReleaseControlError("credential token fingerprint mismatch")

    issued = parse_timestamp(scope.get("issued_at"), label="credential issued_at")
    expires = parse_timestamp(scope.get("expires_at"), label="credential expires_at")
    if now < issued:
        raise ReleaseControlError("credential scope authority is not yet valid")
    if now >= expires:
        raise ReleaseControlError("credential scope authority is expired")

    identity = _principal(
        strict_canonical_json(identity_probe, label="credential identity probe"),
        label="credential identity probe",
    )
    if identity != scope.get("principal"):
        raise ReleaseControlError("credential principal does not match scope authority")

    probes = _object(
        strict_canonical_json(
            endpoint_probe_observations, label="credential endpoint probe observations"
        ),
        label="credential endpoint probe observations",
    )
    _require_exact_fields(
        probes,
        frozenset({"schema", "credential_id", "results", "captured_at", "complete"}),
        label="credential endpoint probe observations",
    )
    if probes.get("schema") != "kestrel.credential_endpoint_probes.v1":
        raise ReleaseControlError("credential endpoint probe schema mismatch")
    if probes.get("credential_id") != scope.get("credential_id"):
        raise ReleaseControlError("credential endpoint probe ID mismatch")
    if probes.get("complete") is not True:
        raise ReleaseControlError("credential endpoint probes are incomplete")
    captured = parse_timestamp(
        probes.get("captured_at"), label="credential endpoint probes captured_at"
    )
    if captured > now:
        raise ReleaseControlError("credential endpoint probes are in the future")
    if (now - captured).total_seconds() > CURRENT_CAPTURE_WINDOW_SECONDS:
        raise ReleaseControlError("credential endpoint probes are stale")

    allowed = [
        _validate_string(endpoint, label="credential allowed endpoint")
        for endpoint in _array(
            scope.get("endpoint_allowlist"), label="credential endpoint allowlist"
        )
    ]
    allowed_set = set(allowed)
    results = _array(probes.get("results"), label="credential endpoint probe results")
    checked_results: list[JSONObject] = []
    seen_endpoints: set[str] = set()
    for raw_result in results:
        result = _object(raw_result, label="credential endpoint probe result")
        _require_exact_fields(
            result,
            frozenset({"endpoint", "http_status", "response_digest"}),
            label="credential endpoint probe result",
        )
        endpoint = _validate_string(
            result.get("endpoint"), label="credential endpoint probe endpoint"
        )
        if endpoint in seen_endpoints:
            raise ReleaseControlError("duplicate credential endpoint probe")
        seen_endpoints.add(endpoint)
        status = _safe_integer(result.get("http_status"), label="credential endpoint HTTP status")
        if status < 100 or status > 599:
            raise ReleaseControlError("credential endpoint HTTP status is invalid")
        _digest(result.get("response_digest"), label="credential endpoint response digest")
        is_allowed = endpoint in allowed_set
        if is_allowed and not 200 <= status <= 299:
            raise ReleaseControlError("allowed endpoint did not succeed")
        if not is_allowed and status not in {401, 403, 404}:
            raise ReleaseControlError("forbidden endpoint did not fail closed")
        checked_results.append(
            {
                "endpoint": endpoint,
                "http_status": status,
                "response_digest": result["response_digest"],
                "allowed": is_allowed,
            }
        )
    if checked_results != sorted(checked_results, key=lambda item: str(item["endpoint"])):
        raise ReleaseControlError("credential endpoint probe results are not sorted")
    missing_allowed = allowed_set - seen_endpoints
    if missing_allowed:
        raise ReleaseControlError("allowed endpoint probe is missing")
    if not any(result["endpoint"] not in allowed_set for result in checked_results):
        raise ReleaseControlError("forbidden endpoint probe is missing")

    verification: JSONObject = {
        "schema": RUNTIME_CREDENTIAL_SCHEMA,
        "credential_id": scope["credential_id"],
        "purpose": scope["purpose"],
        "token_fingerprint": token_fingerprint,
        "verified_at": _format_timestamp(now, label="credential verified_at"),
        "endpoint_results": cast(list[JSONValue], checked_results),
        "scope_authority_digest": _sha256(scope_authority),
        "evidence": {
            "source_bundle_digest": source_bundle_digest(
                {
                    "endpoint-probe-observations": endpoint_probe_observations,
                    "identity-probe-observation": identity_probe,
                    "scope-authority": scope_authority,
                }
            ),
            "canonicalization_vector_digest": canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "controller",
            "method": "runtime-credential-verification",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    _validate_schema(
        RUNTIME_CREDENTIAL_SCHEMA,
        verification,
        label="runtime credential verification",
    )
    if token_bytes in canonical_json_bytes(verification):
        raise ReleaseControlError("credential verification secret-safety invariant failed")
    return verification


def _validate_authority_time_and_sources(authority: JSONObject, *, label: str) -> None:
    observed = parse_timestamp(authority.get("observed_at"), label=f"{label} observed_at")
    expires = parse_timestamp(authority.get("expires_at"), label=f"{label} expires_at")
    if expires - observed != timedelta(seconds=RECEIPT_LIFETIME_SECONDS):
        raise ReleaseControlError(f"{label} freshness lifetime mismatch")
    snapshots = [
        _object(item, label=f"{label} source snapshot")
        for item in _array(authority.get("source_snapshots"), label=f"{label} sources")
    ]
    names = [cast(str, snapshot.get("name")) for snapshot in snapshots]
    if names != sorted(names) or len(names) != len(set(names)):
        raise ReleaseControlError(f"{label} source snapshots are not sorted unique")
    current_times = [
        parse_timestamp(snapshot.get("captured_at"), label=f"{label} current source capture")
        for snapshot in snapshots
        if snapshot.get("freshness_class") == "current"
    ]
    if not current_times:
        raise ReleaseControlError(f"{label} has no current source evidence")
    if max(current_times) != observed:
        raise ReleaseControlError(f"{label} observed_at is not the latest current source")
    if (max(current_times) - min(current_times)).total_seconds() > CURRENT_CAPTURE_WINDOW_SECONDS:
        raise ReleaseControlError(f"{label} current source window is too wide")


def _authority_candidate_and_run(
    authority: JSONObject, *, label: str
) -> tuple[JSONObject, JSONObject]:
    candidate = _object(authority.get("candidate"), label=f"{label} candidate")
    run = _object(authority.get("promotion_run"), label=f"{label} promotion run")
    if (
        run.get("head_sha") != candidate.get("source_sha")
        or run.get("workflow_sha") != candidate.get("source_sha")
        or run.get("run_attempt") != 1
        or run.get("event") != "workflow_dispatch"
        or run.get("workflow_path") != DISPATCH_WORKFLOW_PATH
    ):
        raise ReleaseControlError(f"{label} promotion run identity mismatch")
    return candidate, run


def _validate_ruleset(
    value: object,
    *,
    label: str,
    expected_name: str,
    expected_target: str,
    expected_include: str,
) -> None:
    ruleset = _object(value, label=label)
    if (
        ruleset.get("name") != expected_name
        or ruleset.get("target") != expected_target
        or ruleset.get("enforcement") != "active"
        or ruleset.get("source_type") != "Repository"
        or ruleset.get("source") != "John-MiracleWorker/Kestrel"
        or ruleset.get("bypass_actors") != []
    ):
        raise ReleaseControlError(f"{label} identity or bypass policy mismatch")
    conditions = _object(ruleset.get("conditions"), label=f"{label} conditions")
    ref_name = _object(conditions.get("ref_name"), label=f"{label} ref-name conditions")
    if ref_name != {"include": [expected_include], "exclude": []}:
        raise ReleaseControlError(f"{label} ref policy mismatch")
    rules = _array(ruleset.get("rules"), label=f"{label} rules")
    if rules != [
        {"type": "deletion"},
        {
            "type": "update",
            "parameters": {"update_allows_fetch_and_merge": False},
        },
    ]:
        raise ReleaseControlError(f"{label} mutation rules mismatch")


def validate_github_authority(value: Mapping[str, object]) -> JSONObject:
    """Validate schema plus the exact GitHub/GHCR authority policy."""

    authority = _copy_json_object(value, label="GitHub release authority")
    _validate_schema(GITHUB_AUTHORITY_SCHEMA, authority, label="GitHub release authority")
    repository = _object(authority.get("repository"), label="GitHub authority repository")
    owner = _object(authority.get("owner"), label="GitHub authority owner")
    if (
        repository.get("full_name") != "John-MiracleWorker/Kestrel"
        or repository.get("owner_login") != "John-MiracleWorker"
        or repository.get("owner_id") != owner.get("id")
        or owner.get("login") != "John-MiracleWorker"
        or owner.get("type") != "User"
    ):
        raise ReleaseControlError("GitHub authority repository/owner mismatch")
    candidate, run = _authority_candidate_and_run(authority, label="GitHub authority")
    phase = authority.get("phase")
    mode = authority.get("mode")
    expected_ref = "refs/heads/main" if mode == "initiate" else f"refs/tags/{candidate['tag']}"
    if run.get("ref") != expected_ref:
        raise ReleaseControlError("GitHub authority mode/run ref mismatch")
    environment = _object(authority.get("environment"), label="GitHub authority environment")
    expected_environment = "release" if phase == "admission" else "release-commit"
    if environment.get("name") != expected_environment:
        raise ReleaseControlError("GitHub authority phase/environment mismatch")
    policies = [
        _object(item, label="GitHub authority environment policy")
        for item in _array(
            authority.get("environment_policies"),
            label="GitHub authority environment policies",
        )
    ]
    if len(policies) != 8:
        raise ReleaseControlError("GitHub authority environment policy cardinality mismatch")
    grouped: dict[str, list[tuple[str, str]]] = {}
    keys: list[tuple[int, str, str]] = []
    for policy in policies:
        environment_name = cast(str, policy.get("environment_name"))
        grouped.setdefault(environment_name, []).append(
            (cast(str, policy.get("type")), cast(str, policy.get("name")))
        )
        keys.append(
            (
                cast(int, policy.get("environment_id")),
                cast(str, policy.get("type")),
                cast(str, policy.get("name")),
            )
        )
    if keys != sorted(keys) or grouped != {
        "release": [("branch", "main"), ("tag", "v*")],
        "release-prepare": [("branch", "main"), ("tag", "v*")],
        "release-commit": [("branch", "main"), ("tag", "v*")],
        "pypi": [("branch", "main"), ("tag", "v*")],
    }:
        raise ReleaseControlError("GitHub authority environment policy set mismatch")
    _validate_ruleset(
        authority.get("tag_ruleset"),
        label="GitHub tag ruleset",
        expected_name="kestrel-release-tags",
        expected_target="tag",
        expected_include="refs/tags/v*",
    )
    _validate_ruleset(
        authority.get("ingress_ruleset"),
        label="GitHub ingress ruleset",
        expected_name="kestrel-release-transaction-main-lock",
        expected_target="branch",
        expected_include="refs/heads/main",
    )
    ingress = _object(authority.get("workflow_ingress"), label="GitHub workflow ingress")
    blob_digests = {
        ingress.get("default_branch_blob_sha256"),
        ingress.get("candidate_blob_sha256"),
    }
    capsule_blob = ingress.get("capsule_blob_sha256")
    if len(blob_digests) != 1 or (capsule_blob is not None and capsule_blob not in blob_digests):
        raise ReleaseControlError("GitHub authority workflow bytes drifted")
    if phase == "admission" and mode == "initiate":
        if capsule_blob is not None:
            raise ReleaseControlError("GitHub initiation admission has a capsule blob")
    elif capsule_blob is None:
        raise ReleaseControlError("GitHub authority requires capsule workflow bytes")
    dispatch = _object(authority.get("dispatch"), label="GitHub dispatch authority")
    response_digest = dispatch.get("response_digest")
    if (dispatch.get("transport_outcome") == "response_details_received") != (
        response_digest is not None
    ):
        raise ReleaseControlError("GitHub dispatch response binding mismatch")
    if authority.get("installed_apps") != []:
        raise ReleaseControlError("GitHub authority retains an installed App writer")
    bindings = _object(authority.get("bindings"), label="GitHub authority bindings")
    transaction = bindings.get("transaction_authorization_digest")
    execution = bindings.get("execution_authorization_digest")
    capsule = bindings.get("recovery_capsule_manifest_digest")
    marker = bindings.get("commit_marker_digest")
    if (phase, mode) == ("admission", "initiate"):
        valid_bindings = all(item is None for item in (transaction, execution, capsule, marker))
    elif (phase, mode) == ("admission", "recover_committed"):
        valid_bindings = (
            transaction is not None
            and execution is None
            and capsule is not None
            and marker is not None
        )
    elif (phase, mode) == ("commit", "initiate"):
        valid_bindings = (
            transaction is not None and execution is None and capsule is not None and marker is None
        )
    else:
        valid_bindings = (
            transaction is not None
            and execution is not None
            and capsule is not None
            and marker is None
        )
    if not valid_bindings:
        raise ReleaseControlError("GitHub authority phase/mode binding mismatch")
    package = _object(authority.get("ghcr_package"), label="GHCR package authority")
    if package.get("state") == "absent":
        if any(
            package.get(field) not in (None, [])
            for field in (
                "linked_repository",
                "inheritance_mode",
                "direct_roles",
                "team_roles",
                "actions_access",
                "upload_delete_principals",
            )
        ):
            raise ReleaseControlError("GHCR absent-state authority is inconsistent")
    else:
        principals = _array(package.get("upload_delete_principals"), label="GHCR upload principals")
        allowed = {
            ("user", str(owner["id"]), "John-MiracleWorker"),
            ("repository", str(repository["id"]), "John-MiracleWorker/Kestrel"),
        }
        for raw_principal in principals:
            principal = _object(raw_principal, label="GHCR upload principal")
            key = (
                cast(str, principal.get("kind")),
                str(principal.get("id")),
                cast(str, principal.get("name")),
            )
            if key not in allowed:
                raise ReleaseControlError("GHCR authority has an unexpected writer")
    _validate_authority_time_and_sources(authority, label="GitHub authority")
    provenance = _object(authority.get("provenance"), label="GitHub provenance")
    if provenance != {
        "producer": "scripts/release_control_receipt.py",
        "provider": "github.com",
        "method": "owner-authenticated-controller-snapshot",
    }:
        raise ReleaseControlError("GitHub authority provenance mismatch")
    return authority


def validate_pypi_authority(value: Mapping[str, object]) -> JSONObject:
    """Validate schema plus sole-owner/no-token/exact-publisher PyPI policy."""

    authority = _copy_json_object(value, label="PyPI authority")
    _validate_schema(PYPI_AUTHORITY_SCHEMA, authority, label="PyPI authority")
    if (
        authority.get("project")
        != {"name": "nested-memvid-agent", "normalized_name": "nested-memvid-agent"}
        or authority.get("owner") != {"username": "John_miracleworker"}
        or authority.get("organization") is not None
        or authority.get("roles") != [{"username": "John_miracleworker", "role": "Owner"}]
        or authority.get("pending_role_grants") != []
        or authority.get("upload_tokens") != []
    ):
        raise ReleaseControlError("PyPI authority owner/role/token policy mismatch")
    expected_publisher = {
        "kind": "GitHub",
        "repository_owner": "John-MiracleWorker",
        "repository_name": "Kestrel",
        "workflow_name": "release.yml",
        "environment": "pypi",
    }
    if authority.get("trusted_publishers") != [expected_publisher]:
        raise ReleaseControlError("PyPI trusted publisher authority mismatch")
    candidate, run = _authority_candidate_and_run(authority, label="PyPI authority")
    bindings = _object(authority.get("bindings"), label="PyPI authority bindings")
    execution = bindings.get("execution_authorization_digest")
    expected_ref = "refs/heads/main" if execution is None else f"refs/tags/{candidate['tag']}"
    if run.get("ref") != expected_ref:
        raise ReleaseControlError("PyPI authority run/binding mode mismatch")
    if authority.get("main_sha") != candidate.get("source_sha"):
        raise ReleaseControlError("PyPI authority historical main SHA mismatch")
    environment = _object(authority.get("environment"), label="PyPI environment")
    if environment.get("name") != "pypi":
        raise ReleaseControlError("PyPI authority environment mismatch")
    _validate_authority_time_and_sources(authority, label="PyPI authority")
    provenance = _object(authority.get("provenance"), label="PyPI provenance")
    if provenance != {
        "producer": "scripts/release_control_receipt.py",
        "provider": "pypi.org",
        "method": "owner-authenticated-controller-snapshot",
    }:
        raise ReleaseControlError("PyPI authority provenance mismatch")
    return authority


def _committed_source_registry() -> JSONObject:
    return _object(
        _load_canonical_file(
            SOURCE_REGISTRY_PATH,
            label="source registry",
            max_bytes=4 * 1024 * 1024,
        ),
        label="source registry",
    )


def _authority_source_inputs(
    sources: Mapping[str, bytes],
    *,
    receipt_schema: str,
    phase: str | None,
    mode: str | None,
    expected_names: frozenset[str],
    _clock: Callable[[], datetime],
) -> tuple[dict[str, JSONObject], list[JSONObject], list[JSONObject]]:
    if set(sources) != expected_names:
        raise ReleaseControlError("authority source set mismatch")
    registry = _committed_source_registry()
    bodies: dict[str, JSONObject] = {}
    envelopes: list[JSONObject] = []
    snapshots: list[JSONObject] = []
    for name in sorted(sources):
        raw = sources[name]
        body = source_observation_body_for_contract(
            raw,
            registry=registry,
            receipt_schema=receipt_schema,
            phase=phase,
            mode=mode,
            name=name,
            _clock=_clock,
        )
        bodies[name] = _object(
            strict_canonical_json(body, label=f"{name} authority source body"),
            label=f"{name} authority source body",
        )
        envelopes.append(
            _object(
                strict_canonical_json(raw, label=f"{name} authority source"),
                label=f"{name} authority source",
            )
        )
        snapshots.append(source_snapshot(raw))
    return bodies, envelopes, snapshots


def _candidate_from_manifest(raw: bytes) -> tuple[JSONObject, int]:
    manifest = _object(
        strict_canonical_json(raw, label="candidate manifest"),
        label="candidate manifest",
    )
    _validate_schema("kestrel.release_candidate.v1", manifest, label="candidate manifest")
    source = _object(manifest.get("source"), label="candidate source")
    repository_id = _safe_integer(
        source.get("repository_id"), label="candidate repository ID", positive=True
    )
    candidate_run = _object(manifest.get("candidate_run"), label="candidate qualification run")
    artifacts = _array(manifest.get("artifacts"), label="candidate artifacts")
    version = _validate_string(manifest.get("version"), label="candidate version")
    if (
        manifest.get("tag") != f"v{version}"
        or source.get("repository") != "John-MiracleWorker/Kestrel"
        or candidate_run.get("workflow_ref") != "refs/heads/main"
        or candidate_run.get("workflow_sha") != source.get("commit_sha")
        or candidate_run.get("run_attempt") != 1
        or manifest.get("artifact_set_digest") != _sha256(canonical_json_bytes(artifacts))
    ):
        raise ReleaseControlError("candidate manifest authority identity mismatch")
    return (
        {
            "version": version,
            "tag": manifest["tag"],
            "source_sha": source["commit_sha"],
            "source_tree": source["tree_sha"],
            "candidate_run_id": candidate_run["run_id"],
            "candidate_run_attempt": candidate_run["run_attempt"],
            "artifact_set_digest": manifest["artifact_set_digest"],
            "candidate_manifest_digest": _sha256(raw),
        },
        repository_id,
    )


def _promotion_run_from_authority_sources(
    *,
    run_observation: JSONObject,
    identity: JSONObject,
    run_observation_digest: str,
    identity_observation_digest: str,
) -> JSONObject:
    _require_exact_fields(
        run_observation,
        frozenset(
            {
                "schema",
                "repository_id",
                "workflow_id",
                "workflow_path",
                "run_id",
                "run_attempt",
                "event",
                "ref",
                "head_sha",
                "workflow_sha",
                "actor",
                "triggering_actor",
                "captured_at",
                "complete",
            }
        ),
        label="promotion run observation",
    )
    if (
        run_observation.get("schema") != "kestrel.promotion_run_observation.v1"
        or run_observation.get("complete") is not True
    ):
        raise ReleaseControlError("promotion run observation is invalid")
    _validate_schema(DISPATCH_IDENTITY_SCHEMA, identity, label="promotion dispatch identity")
    actor = _object(run_observation.get("actor"), label="promotion run actor")
    triggering_actor = _object(
        run_observation.get("triggering_actor"),
        label="promotion run triggering actor",
    )
    expected_workflow_ref = (
        f"John-MiracleWorker/Kestrel/.github/workflows/release.yml@{run_observation.get('ref')}"
    )
    if (
        identity.get("repository") != "John-MiracleWorker/Kestrel"
        or identity.get("repository_id") != run_observation.get("repository_id")
        or identity.get("workflow_ref") != expected_workflow_ref
        or identity.get("workflow_sha") != run_observation.get("workflow_sha")
        or identity.get("event_name") != run_observation.get("event")
        or identity.get("ref") != run_observation.get("ref")
        or identity.get("sha") != run_observation.get("head_sha")
        or identity.get("run_id") != run_observation.get("run_id")
        or identity.get("run_attempt") != run_observation.get("run_attempt")
        or identity.get("actor") != actor.get("login")
        or identity.get("actor_id") != actor.get("id")
        or identity.get("triggering_actor") != triggering_actor.get("login")
    ):
        raise ReleaseControlError("promotion run and dispatch identity mismatch")
    return {
        "run_id": run_observation["run_id"],
        "run_attempt": run_observation["run_attempt"],
        "workflow_id": run_observation["workflow_id"],
        "workflow_path": run_observation["workflow_path"],
        "event": run_observation["event"],
        "ref": run_observation["ref"],
        "head_sha": run_observation["head_sha"],
        "workflow_sha": run_observation["workflow_sha"],
        "repository_id": run_observation["repository_id"],
        "actor": actor,
        "triggering_actor": triggering_actor,
        "transaction_nonce": identity["transaction_nonce"],
        "rest_observation_digest": _digest(
            run_observation_digest, label="promotion run observation digest"
        ),
        "context_observation_digest": _digest(
            identity_observation_digest,
            label="promotion dispatch identity observation digest",
        ),
    }


def create_pypi_authority(
    *,
    public_project_observation: bytes,
    owner_authority_snapshot: bytes,
    promotion_run_observation: bytes,
    promotion_dispatch_identity: bytes,
    candidate_manifest: bytes,
    environment_observation: bytes,
    controller_context: bytes,
    bindings: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    """Create the exact sole-owner, token-free PyPI publication authority."""

    candidate, candidate_repository_id = _candidate_from_manifest(candidate_manifest)
    raw_sources = {
        "bindings": bindings,
        "controller-context": controller_context,
        "environment-rest": environment_observation,
        "promotion-dispatch-identity": promotion_dispatch_identity,
        "promotion-run-rest": promotion_run_observation,
        "pypi-owner-dashboard": owner_authority_snapshot,
        "pypi-project": public_project_observation,
    }
    provisional_bindings = _object(
        strict_canonical_json(
            source_observation_body(bindings, expected_name="bindings"),
            label="PyPI authority bindings",
        ),
        label="PyPI authority bindings",
    )
    mode = (
        "initiate"
        if provisional_bindings.get("execution_authorization_digest") is None
        else "recover_committed"
    )
    bodies, envelopes, snapshots = _authority_source_inputs(
        raw_sources,
        receipt_schema=PYPI_AUTHORITY_SCHEMA,
        phase="publication",
        mode=mode,
        expected_names=frozenset(raw_sources),
        _clock=_clock,
    )
    owner_snapshot = bodies["pypi-owner-dashboard"]
    _require_exact_fields(
        owner_snapshot,
        frozenset(
            {
                "schema",
                "project",
                "owner",
                "organization",
                "roles",
                "pending_role_grants",
                "trusted_publishers",
                "project_tokens",
                "account_tokens",
                "captured_at",
                "complete",
            }
        ),
        label="PyPI owner authority snapshot",
    )
    if (
        owner_snapshot.get("schema") != "kestrel.pypi_owner_authority.v1"
        or owner_snapshot.get("complete") is not True
    ):
        raise ReleaseControlError("PyPI owner authority snapshot is invalid")
    context = bodies["controller-context"]
    _require_exact_fields(
        context,
        frozenset(
            {
                "schema",
                "owner",
                "main_sha",
                "acknowledgement",
                "captured_at",
                "complete",
            }
        ),
        label="PyPI authority controller context",
    )
    if (
        context.get("schema") != "kestrel.pypi_authority_controller_context.v1"
        or context.get("complete") is not True
    ):
        raise ReleaseControlError("PyPI authority controller context is invalid")
    owner = _object(owner_snapshot.get("owner"), label="PyPI owner")
    context_owner = _object(context.get("owner"), label="PyPI controller owner")
    if (
        context_owner.get("username") != owner.get("username")
        or context_owner.get("login") != SIGNING_PRINCIPAL
    ):
        raise ReleaseControlError("PyPI owner controller identity mismatch")
    for source_name in ("pypi-owner-dashboard", "controller-context"):
        envelope = next(item for item in envelopes if item.get("name") == source_name)
        if bodies[source_name].get("captured_at") != envelope.get("captured_at"):
            raise ReleaseControlError("PyPI source body/envelope capture mismatch")
    run = _promotion_run_from_authority_sources(
        run_observation=bodies["promotion-run-rest"],
        identity=bodies["promotion-dispatch-identity"],
        run_observation_digest=_sha256(raw_sources["promotion-run-rest"]),
        identity_observation_digest=_sha256(raw_sources["promotion-dispatch-identity"]),
    )
    expected_ref = "refs/heads/main" if mode == "initiate" else f"refs/tags/{candidate['tag']}"
    if (
        run.get("repository_id") != candidate_repository_id
        or run.get("ref") != expected_ref
        or run.get("head_sha") != candidate.get("source_sha")
        or context.get("main_sha") != candidate.get("source_sha")
    ):
        raise ReleaseControlError("PyPI candidate/promotion run identity mismatch")
    environment = bodies["environment-rest"]
    _require_exact_fields(
        environment,
        frozenset({"id", "name"}),
        label="PyPI environment observation",
    )
    public = bodies["pypi-project"]
    _require_exact_fields(
        public,
        frozenset({"name", "version", "last_serial"}),
        label="public PyPI project observation",
    )
    checked_bindings = bodies["bindings"]
    _require_exact_fields(
        checked_bindings,
        frozenset(
            {
                "transaction_authorization_digest",
                "execution_authorization_digest",
                "recovery_capsule_manifest_digest",
                "commit_marker_digest",
                "immutable_release_observation_digest",
                "github_ghcr_verification_digest",
            }
        ),
        label="PyPI authority bindings",
    )
    upload_tokens = [
        *_array(owner_snapshot.get("account_tokens"), label="PyPI account tokens"),
        *_array(owner_snapshot.get("project_tokens"), label="PyPI project tokens"),
    ]
    upload_tokens = sorted(
        upload_tokens,
        key=lambda item: (
            str(_object(item, label="PyPI upload token").get("scope")),
            str(_object(item, label="PyPI upload token").get("owner_username")),
            str(_object(item, label="PyPI upload token").get("name_prefix")),
        ),
    )
    acknowledgement = _object(
        context.get("acknowledgement"), label="PyPI maintenance acknowledgement"
    )
    observed_at, expires_at = validate_receipt_freshness(
        envelopes,
        acknowledgement=acknowledgement,
        _clock=_clock,
    )
    public_snapshot = next(item for item in snapshots if item.get("name") == "pypi-project")
    authority: JSONObject = {
        "schema": PYPI_AUTHORITY_SCHEMA,
        "project": owner_snapshot["project"],
        "owner": owner,
        "organization": owner_snapshot["organization"],
        "roles": owner_snapshot["roles"],
        "pending_role_grants": owner_snapshot["pending_role_grants"],
        "trusted_publishers": owner_snapshot["trusted_publishers"],
        "upload_tokens": upload_tokens,
        "promotion_run": run,
        "candidate": candidate,
        "main_sha": context["main_sha"],
        "environment": environment,
        "bindings": checked_bindings,
        "public_project": {
            **public,
            "observation_digest": public_snapshot["sha256"],
        },
        "source_snapshots": cast(list[JSONValue], snapshots),
        "observed_at": observed_at,
        "expires_at": expires_at,
        "maintenance_window_acknowledgement": acknowledgement,
        "evidence": {
            "source_bundle_digest": source_bundle_digest(
                {**raw_sources, "candidate-manifest": candidate_manifest}
            ),
            "canonicalization_vector_digest": canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "pypi.org",
            "method": "owner-authenticated-controller-snapshot",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    validate_pypi_authority(authority)
    if (
        strict_canonical_json(canonical_json_bytes(authority), label="PyPI authority output")
        != authority
    ):
        raise ReleaseControlError("PyPI authority canonical replay mismatch")
    return authority


def _workflow_contents(value: JSONObject, *, label: str) -> tuple[str, bytes]:
    _require_exact_fields(value, frozenset({"path", "content_base64"}), label=label)
    path = _validate_string(value.get("path"), label=f"{label} path")
    encoded = _validate_string(value.get("content_base64"), label=f"{label} content")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReleaseControlError(f"{label} content is not canonical base64") from exc
    if base64.b64encode(content).decode("ascii") != encoded or not content:
        raise ReleaseControlError(f"{label} content is invalid")
    return path, content


def create_github_authority(
    *,
    repository_observation: bytes,
    promotion_run_observation: bytes,
    promotion_dispatch_identity: bytes,
    candidate_manifest: bytes,
    environment_observation: bytes,
    environment_policy_types_snapshot: bytes,
    rulesets_observation: bytes,
    tag_ruleset_detail_observation: bytes,
    ingress_ruleset_detail_observation: bytes,
    workflow_observation: bytes,
    default_branch_workflow_contents: bytes,
    candidate_workflow_contents: bytes,
    dispatch_intent: bytes,
    dispatch_intent_signature: bytes,
    dispatch_request: bytes,
    dispatch_outcome: bytes,
    installed_apps_snapshot: bytes,
    ghcr_package_access_snapshot: bytes,
    dispatcher_invalidation_snapshot: bytes,
    controller_context: bytes,
    bindings: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    """Create GitHub/GHCR authority from an exact authenticated source graph."""

    candidate, candidate_repository_id = _candidate_from_manifest(candidate_manifest)
    context_body = _object(
        strict_canonical_json(
            source_observation_body(controller_context, expected_name="controller-context"),
            label="GitHub authority controller context",
        ),
        label="GitHub authority controller context",
    )
    phase = context_body.get("phase")
    mode = context_body.get("mode")
    if phase not in {"admission", "commit"} or mode not in {
        "initiate",
        "recover_committed",
    }:
        raise ReleaseControlError("GitHub authority phase/mode is invalid")
    outcome_envelope = _validate_source_envelope(
        _object(
            strict_canonical_json(dispatch_outcome, label="dispatch outcome source"),
            label="dispatch outcome source",
        )
    )
    outcome_name = outcome_envelope.get("name")
    if outcome_name not in {"dispatch-response", "dispatch-reconciliation"}:
        raise ReleaseControlError("GitHub authority dispatch outcome source mismatch")
    raw_sources = {
        "bindings": bindings,
        "candidate-workflow-contents": candidate_workflow_contents,
        "controller-context": controller_context,
        "default-branch-workflow-contents": default_branch_workflow_contents,
        "dispatch-intent": dispatch_intent,
        "dispatch-intent-signature": dispatch_intent_signature,
        outcome_name: dispatch_outcome,
        "dispatch-request": dispatch_request,
        "dispatcher-invalidation-owner": dispatcher_invalidation_snapshot,
        "environment-policy-types-owner": environment_policy_types_snapshot,
        "environment-rest": environment_observation,
        "ghcr-package-access-owner": ghcr_package_access_snapshot,
        "ingress-ruleset-detail-rest": ingress_ruleset_detail_observation,
        "installed-apps-owner": installed_apps_snapshot,
        "promotion-dispatch-identity": promotion_dispatch_identity,
        "promotion-run-rest": promotion_run_observation,
        "repository-rest": repository_observation,
        "rulesets-rest": rulesets_observation,
        "tag-ruleset-detail-rest": tag_ruleset_detail_observation,
        "workflow-rest": workflow_observation,
    }
    bodies, envelopes, snapshots = _authority_source_inputs(
        raw_sources,
        receipt_schema=GITHUB_AUTHORITY_SCHEMA,
        phase=phase,
        mode=mode,
        expected_names=frozenset(raw_sources),
        _clock=_clock,
    )
    context = bodies["controller-context"]
    _require_exact_fields(
        context,
        frozenset(
            {
                "schema",
                "phase",
                "mode",
                "owner",
                "acknowledgement",
                "captured_at",
                "complete",
            }
        ),
        label="GitHub authority controller context",
    )
    if (
        context.get("schema") != "kestrel.github_authority_controller_context.v1"
        or context.get("complete") is not True
    ):
        raise ReleaseControlError("GitHub authority controller context is invalid")
    context_envelope = next(item for item in envelopes if item.get("name") == "controller-context")
    if context.get("captured_at") != context_envelope.get("captured_at"):
        raise ReleaseControlError("GitHub context body/envelope capture mismatch")

    repository_source = bodies["repository-rest"]
    _require_exact_fields(
        repository_source,
        frozenset({"id", "full_name", "owner"}),
        label="GitHub repository observation",
    )
    owner = _object(repository_source.get("owner"), label="GitHub repository owner")
    context_owner = _object(context.get("owner"), label="GitHub controller owner")
    if context_owner != {"id": owner.get("id"), "login": owner.get("login")}:
        raise ReleaseControlError("GitHub repository/controller owner identity mismatch")
    repository: JSONObject = {
        "id": repository_source["id"],
        "full_name": repository_source["full_name"],
        "owner_id": owner["id"],
        "owner_login": owner["login"],
    }
    run = _promotion_run_from_authority_sources(
        run_observation=bodies["promotion-run-rest"],
        identity=bodies["promotion-dispatch-identity"],
        run_observation_digest=_sha256(raw_sources["promotion-run-rest"]),
        identity_observation_digest=_sha256(raw_sources["promotion-dispatch-identity"]),
    )
    expected_ref = "refs/heads/main" if mode == "initiate" else f"refs/tags/{candidate['tag']}"
    if (
        run.get("repository_id") != candidate_repository_id
        or repository.get("id") != candidate_repository_id
        or run.get("ref") != expected_ref
        or run.get("head_sha") != candidate.get("source_sha")
    ):
        raise ReleaseControlError("GitHub candidate/promotion run identity mismatch")

    environment = bodies["environment-rest"]
    _require_exact_fields(
        environment,
        frozenset({"id", "name"}),
        label="GitHub environment observation",
    )
    policies_source = bodies["environment-policy-types-owner"]
    _require_exact_fields(
        policies_source,
        frozenset({"schema", "policies", "captured_at", "complete"}),
        label="GitHub environment policy snapshot",
    )
    if (
        policies_source.get("schema") != "kestrel.environment_policy_types_snapshot.v1"
        or policies_source.get("complete") is not True
    ):
        raise ReleaseControlError("GitHub environment policy snapshot is invalid")

    rulesets_source = bodies["rulesets-rest"]
    _require_exact_fields(
        rulesets_source,
        frozenset({"schema", "rulesets", "captured_at", "complete"}),
        label="GitHub ruleset inventory",
    )
    if (
        rulesets_source.get("schema") != "kestrel.rulesets_observation.v1"
        or rulesets_source.get("complete") is not True
    ):
        raise ReleaseControlError("GitHub ruleset inventory is invalid")
    ruleset_inventory = [
        _object(item, label="GitHub ruleset inventory item")
        for item in _array(rulesets_source.get("rulesets"), label="GitHub ruleset inventory")
    ]
    for item in ruleset_inventory:
        _require_exact_fields(
            item,
            frozenset({"id", "name", "target"}),
            label="GitHub ruleset inventory item",
        )
    tag_ruleset = dict(bodies["tag-ruleset-detail-rest"])
    ingress_ruleset = dict(bodies["ingress-ruleset-detail-rest"])
    expected_inventory = sorted(
        [
            {
                "id": tag_ruleset.get("id"),
                "name": tag_ruleset.get("name"),
                "target": tag_ruleset.get("target"),
            },
            {
                "id": ingress_ruleset.get("id"),
                "name": ingress_ruleset.get("name"),
                "target": ingress_ruleset.get("target"),
            },
        ],
        key=lambda item: cast(int, item["id"]),
    )
    if ruleset_inventory != expected_inventory:
        raise ReleaseControlError("GitHub ruleset inventory/detail mismatch")
    tag_ruleset["observation_digest"] = _sha256(raw_sources["tag-ruleset-detail-rest"])
    ingress_ruleset["observation_digest"] = _sha256(raw_sources["ingress-ruleset-detail-rest"])

    workflow = bodies["workflow-rest"]
    _require_exact_fields(
        workflow,
        frozenset({"id", "path", "state", "default_branch"}),
        label="GitHub workflow observation",
    )
    default_path, default_content = _workflow_contents(
        bodies["default-branch-workflow-contents"],
        label="default-branch workflow contents",
    )
    candidate_path, candidate_content = _workflow_contents(
        bodies["candidate-workflow-contents"],
        label="candidate workflow contents",
    )
    if (
        workflow.get("path") != DISPATCH_WORKFLOW_PATH
        or default_path != DISPATCH_WORKFLOW_PATH
        or candidate_path != DISPATCH_WORKFLOW_PATH
        or workflow.get("state") != "active"
    ):
        raise ReleaseControlError("GitHub workflow ingress identity mismatch")
    default_digest = _sha256(default_content)
    candidate_digest = _sha256(candidate_content)
    if default_digest != candidate_digest:
        raise ReleaseControlError("GitHub workflow ingress bytes drifted")

    intent_raw = source_observation_body(
        raw_sources["dispatch-intent"], expected_name="dispatch-intent"
    )
    intent = _validate_dispatch_intent(
        _object(
            strict_canonical_json(intent_raw, label="dispatch intent"),
            label="dispatch intent",
        )
    )
    request_raw = source_observation_body(
        raw_sources["dispatch-request"], expected_name="dispatch-request"
    )
    request = _object(
        strict_canonical_json(request_raw, label="dispatch request"),
        label="dispatch request",
    )
    if request != {
        "ref": _object(intent["target"], label="dispatch target")["short_ref"],
        "inputs": intent["inputs"],
    } or _sha256(request_raw) != intent.get("request_digest"):
        raise ReleaseControlError("GitHub dispatch request/intent binding mismatch")
    signature = bodies["dispatch-intent-signature"]
    _require_exact_fields(
        signature,
        frozenset({"receipt_digest", "signature_digest"}),
        label="dispatch intent signature binding",
    )
    if signature.get("receipt_digest") != _sha256(intent_raw):
        raise ReleaseControlError("GitHub dispatch intent signature binding mismatch")
    outcome = bodies[outcome_name]
    _require_exact_fields(
        outcome,
        frozenset(
            {
                "schema",
                "transport_outcome",
                "response_digest",
                "reconciliation_digest",
            }
        ),
        label="GitHub dispatch outcome",
    )
    if outcome.get("schema") != "kestrel.github_dispatch_outcome.v1":
        raise ReleaseControlError("GitHub dispatch outcome schema mismatch")
    invalidation = bodies["dispatcher-invalidation-owner"]
    _require_exact_fields(
        invalidation,
        frozenset(
            {
                "schema",
                "uninstalled_at",
                "token_invalidation_probe",
                "captured_at",
                "complete",
            }
        ),
        label="dispatcher invalidation observation",
    )
    if (
        invalidation.get("schema") != "kestrel.dispatcher_invalidation_observation.v1"
        or invalidation.get("complete") is not True
    ):
        raise ReleaseControlError("dispatcher invalidation observation is invalid")

    installed = bodies["installed-apps-owner"]
    _require_exact_fields(
        installed,
        frozenset({"schema", "installed_apps", "captured_at", "complete"}),
        label="installed App authority snapshot",
    )
    if (
        installed.get("schema") != "kestrel.installed_apps_authority_snapshot.v1"
        or installed.get("complete") is not True
    ):
        raise ReleaseControlError("installed App authority snapshot is invalid")
    ghcr_source = dict(bodies["ghcr-package-access-owner"])
    complete = ghcr_source.pop("complete", None)
    ghcr_source.pop("captured_at", None)
    if complete is not True:
        raise ReleaseControlError("GHCR package authority snapshot is incomplete")
    checked_bindings = bodies["bindings"]
    _require_exact_fields(
        checked_bindings,
        frozenset(
            {
                "transaction_authorization_digest",
                "execution_authorization_digest",
                "recovery_capsule_manifest_digest",
                "commit_marker_digest",
            }
        ),
        label="GitHub authority bindings",
    )
    acknowledgement = _object(
        context.get("acknowledgement"),
        label="GitHub maintenance acknowledgement",
    )
    observed_at, expires_at = validate_receipt_freshness(
        envelopes,
        acknowledgement=acknowledgement,
        _clock=_clock,
    )
    snapshot_by_name = {cast(str, item["name"]): item for item in snapshots}
    workflow_ingress: JSONObject = {
        "workflow_id": workflow["id"],
        "path": workflow["path"],
        "state": workflow["state"],
        "default_branch": workflow["default_branch"],
        "workflow_observation_digest": snapshot_by_name["workflow-rest"]["sha256"],
        "default_branch_contents_observation_digest": snapshot_by_name[
            "default-branch-workflow-contents"
        ]["sha256"],
        "candidate_contents_observation_digest": snapshot_by_name["candidate-workflow-contents"][
            "sha256"
        ],
        "default_branch_blob_sha256": default_digest,
        "candidate_blob_sha256": candidate_digest,
        "capsule_blob_sha256": (
            None if (phase, mode) == ("admission", "initiate") else candidate_digest
        ),
    }
    authority: JSONObject = {
        "schema": GITHUB_AUTHORITY_SCHEMA,
        "phase": phase,
        "mode": mode,
        "repository": repository,
        "owner": owner,
        "promotion_run": run,
        "candidate": candidate,
        "environment": environment,
        "environment_policies": policies_source["policies"],
        "tag_ruleset": tag_ruleset,
        "ingress_ruleset": ingress_ruleset,
        "workflow_ingress": workflow_ingress,
        "installed_apps": installed["installed_apps"],
        "ghcr_package": ghcr_source,
        "dispatch": {
            "intent_digest": _sha256(intent_raw),
            "intent_signature_digest": signature["signature_digest"],
            "request_digest": _sha256(request_raw),
            "response_digest": outcome["response_digest"],
            "reconciliation_digest": outcome["reconciliation_digest"],
            "transport_outcome": outcome["transport_outcome"],
            "uninstalled_at": invalidation["uninstalled_at"],
            "token_invalidation_probe": invalidation["token_invalidation_probe"],
        },
        "bindings": checked_bindings,
        "source_snapshots": cast(list[JSONValue], snapshots),
        "observed_at": observed_at,
        "expires_at": expires_at,
        "maintenance_window_acknowledgement": acknowledgement,
        "evidence": {
            "source_bundle_digest": source_bundle_digest(
                {**raw_sources, "candidate-manifest": candidate_manifest}
            ),
            "canonicalization_vector_digest": canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "github.com",
            "method": "owner-authenticated-controller-snapshot",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    validate_github_authority(authority)
    if (
        strict_canonical_json(canonical_json_bytes(authority), label="GitHub authority output")
        != authority
    ):
        raise ReleaseControlError("GitHub authority canonical replay mismatch")
    return authority


def create_recovery_repository_authority(
    *,
    owner_authority_snapshot: bytes,
    repository_observation: bytes,
    immutable_releases_observation: bytes,
    controller_context: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    """Create the sole-reader authority for the immutable recovery repository."""

    sources = {
        "controller-context": controller_context,
        "recovery-immutable-releases-rest": immutable_releases_observation,
        "recovery-owner-dashboard": owner_authority_snapshot,
        "recovery-repository-rest": repository_observation,
    }
    bodies, envelopes, snapshots = _authority_source_inputs(
        sources,
        receipt_schema=RECOVERY_AUTHORITY_SCHEMA,
        phase="authority",
        mode="operational",
        expected_names=frozenset(sources),
        _clock=_clock,
    )
    owner_snapshot = bodies["recovery-owner-dashboard"]
    _require_exact_fields(
        owner_snapshot,
        frozenset(
            {
                "schema",
                "repository",
                "owner",
                "collaborators",
                "invitations",
                "deploy_keys",
                "installed_apps",
                "workflows",
                "packages",
                "credentials",
                "captured_at",
                "complete",
            }
        ),
        label="recovery owner authority snapshot",
    )
    if (
        owner_snapshot.get("schema") != "kestrel.recovery_repository_authority_owner.v1"
        or owner_snapshot.get("complete") is not True
    ):
        raise ReleaseControlError("recovery owner authority snapshot is invalid")
    repository = bodies["recovery-repository-rest"]
    _require_exact_fields(
        repository,
        frozenset({"id", "full_name", "owner"}),
        label="recovery repository observation",
    )
    immutable = bodies["recovery-immutable-releases-rest"]
    _require_exact_fields(
        immutable,
        frozenset({"enabled", "enforced_by_owner", "observation_digest"}),
        label="recovery immutable Releases observation",
    )
    context = bodies["controller-context"]
    _require_exact_fields(
        context,
        frozenset({"schema", "owner", "acknowledgement", "captured_at", "complete"}),
        label="recovery authority controller context",
    )
    if (
        context.get("schema") != "kestrel.recovery_repository_authority_controller_context.v1"
        or context.get("complete") is not True
    ):
        raise ReleaseControlError("recovery authority controller context is invalid")
    snapshot_repository = _object(
        owner_snapshot.get("repository"), label="recovery owner repository"
    )
    snapshot_owner = _object(owner_snapshot.get("owner"), label="recovery owner identity")
    repository_owner = _object(repository.get("owner"), label="recovery REST owner identity")
    context_owner = _object(context.get("owner"), label="recovery controller owner identity")
    if (
        repository.get("id") != snapshot_repository.get("id")
        or repository.get("full_name") != snapshot_repository.get("full_name")
        or repository_owner != snapshot_owner
        or context_owner != {"id": snapshot_owner.get("id"), "login": snapshot_owner.get("login")}
    ):
        raise ReleaseControlError("recovery repository authority identity mismatch")
    for source_name in ("recovery-owner-dashboard", "controller-context"):
        envelope = next(item for item in envelopes if item.get("name") == source_name)
        if bodies[source_name].get("captured_at") != envelope.get("captured_at"):
            raise ReleaseControlError("recovery authority body/envelope capture identity mismatch")
    acknowledgement = _object(
        context.get("acknowledgement"),
        label="recovery authority acknowledgement",
    )
    observed_at, expires_at = validate_receipt_freshness(
        envelopes,
        acknowledgement=acknowledgement,
        _clock=_clock,
    )
    authority: JSONObject = {
        "schema": RECOVERY_AUTHORITY_SCHEMA,
        "repository": snapshot_repository,
        "owner": snapshot_owner,
        "collaborators": owner_snapshot["collaborators"],
        "invitations": owner_snapshot["invitations"],
        "deploy_keys": owner_snapshot["deploy_keys"],
        "installed_apps": owner_snapshot["installed_apps"],
        "workflows": owner_snapshot["workflows"],
        "packages": owner_snapshot["packages"],
        "credentials": owner_snapshot["credentials"],
        "immutable_releases": immutable,
        "source_snapshots": cast(list[JSONValue], snapshots),
        "observed_at": observed_at,
        "expires_at": expires_at,
        "maintenance_window_acknowledgement": acknowledgement,
        "evidence": {
            "source_bundle_digest": source_bundle_digest(sources),
            "canonicalization_vector_digest": canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "github.com",
            "method": "owner-authenticated-controller-snapshot",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    validate_recovery_repository_authority(authority)
    if (
        strict_canonical_json(canonical_json_bytes(authority), label="recovery authority output")
        != authority
    ):
        raise ReleaseControlError("recovery authority canonical replay mismatch")
    return authority


def validate_recovery_repository_authority(
    value: Mapping[str, object],
) -> JSONObject:
    """Validate the isolated immutable recovery repository's sole-reader policy."""

    authority = _copy_json_object(value, label="recovery repository authority")
    _validate_schema(RECOVERY_AUTHORITY_SCHEMA, authority, label="recovery repository authority")
    repository = _object(authority.get("repository"), label="recovery authority repository")
    owner = _object(authority.get("owner"), label="recovery authority owner")
    if (
        repository.get("full_name") != "John-MiracleWorker/Kestrel-Release-Recovery"
        or owner.get("login") != "John-MiracleWorker"
        or owner.get("type") != "User"
    ):
        raise ReleaseControlError("recovery repository authority identity mismatch")
    collaborators = _array(authority.get("collaborators"), label="recovery collaborators")
    if len(collaborators) != 1:
        raise ReleaseControlError("recovery repository has another effective writer")
    collaborator = _object(collaborators[0], label="recovery owner collaborator")
    permissions = _object(collaborator.get("permissions"), label="recovery owner permissions")
    if (
        collaborator.get("login") != owner.get("login")
        or collaborator.get("id") != owner.get("id")
        or collaborator.get("role_name") != "admin"
        or permissions.get("admin") is not True
    ):
        raise ReleaseControlError("recovery repository sole owner authority mismatch")
    for field in (
        "invitations",
        "deploy_keys",
        "installed_apps",
        "workflows",
        "packages",
    ):
        if authority.get(field) != []:
            raise ReleaseControlError(
                f"recovery repository has unexpected writer authority in {field}"
            )
    credentials = _array(authority.get("credentials"), label="recovery authority credentials")
    if len(credentials) != 1:
        raise ReleaseControlError("recovery credential authority cardinality mismatch")
    credential = _object(credentials[0], label="recovery reader credential")
    if (
        credential.get("kind") != "pat"
        or credential.get("purpose") != "recovery_reader"
        or credential.get("capabilities") != ["repository_read"]
        or credential.get("active") is not True
        or credential.get("expires_at") is None
    ):
        raise ReleaseControlError("recovery reader credential scope mismatch")
    observed = parse_timestamp(authority.get("observed_at"), label="recovery authority observed_at")
    if parse_timestamp(credential.get("expires_at"), label="recovery reader expiry") <= observed:
        raise ReleaseControlError("recovery reader credential is expired")
    expected_names = [
        "controller-context",
        "recovery-immutable-releases-rest",
        "recovery-owner-dashboard",
        "recovery-repository-rest",
    ]
    snapshots = _array(authority.get("source_snapshots"), label="recovery authority sources")
    if [
        _object(item, label="recovery authority source").get("name") for item in snapshots
    ] != expected_names:
        raise ReleaseControlError("recovery authority source set mismatch")
    _validate_authority_time_and_sources(authority, label="recovery authority")
    provenance = _object(authority.get("provenance"), label="recovery provenance")
    if provenance != {
        "producer": "scripts/release_control_receipt.py",
        "provider": "github.com",
        "method": "owner-authenticated-controller-snapshot",
    }:
        raise ReleaseControlError("recovery authority provenance mismatch")
    return authority


_RECOVERY_ARCHIVE_POLICY: JSONObject = {
    "format": "tar",
    "path_order": "lexical",
    "uid": 0,
    "gid": 0,
    "mtime": 0,
    "file_mode": "0644",
    "directory_mode": "0755",
    "max_member_bytes": 2_147_483_648,
    "max_total_bytes": 2_147_483_648,
}
_GITLEAKS_IMAGE = (
    "zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
)
_RECOVERY_BWRAP_PACKAGE_URL = (
    "https://archive.ubuntu.com/ubuntu/pool/main/b/bubblewrap/bubblewrap_0.9.0-1ubuntu0.1_amd64.deb"
)
_RECOVERY_BWRAP_PACKAGE_DIGEST = (
    "sha256:1b506492bd9c7fd0cdb4f02ac822f1d3e336b0aead5113c1239baf8db5db562a"
)
_RECOVERY_BWRAP_BINARY_DIGEST = (
    "sha256:52231e1caf55bcbc667b269f49c63599a6f7db4767ae6a039580d0ff853db712"
)
_RECOVERY_BWRAP_VERSION = "bubblewrap 0.9.0"
_RECOVERY_PYTHON_VERSION = "3.11.14"
_RECOVERY_PYTHON_ABI = "cp311"
_RECOVERY_PYTHON_PACKAGE_URL = (
    "https://github.com/actions/python-versions/releases/download/"
    "3.11.14-18393181605/python-3.11.14-linux-24.04-x64.tar.gz"
)
_RECOVERY_PYTHON_PACKAGE_DIGEST = (
    "sha256:295c25eeb4fdad1ec9526a27fbd9b476d7c79b00547d74d809b306381d0796d5"
)
_RECOVERY_PYTHON_BINARY_DIGEST = (
    "sha256:dcd2d22a91c5adb37fa3f54a3a16d2ed7616b84931eb606c0fdfbca38395dab8"
)
_RECOVERY_WHEEL_PLATFORM = "manylinux2014_x86_64"
_RECOVERY_RUNTIME_PLATFORM = "ubuntu-24.04-x86_64"
_RECOVERY_DEPENDENCY_STAGING_PROVENANCE: JSONObject = {
    "method": "checksum-pinned-recovery-dependency-staging",
    "producer": "scripts/stage_recovery_dependencies.py",
    "provider": "github.com+archive.ubuntu.com+pypi.org",
}
_RECOVERY_CAPSULE_FIXED_ASSETS = frozenset(
    {
        "candidate-archive.tar",
        "dispatch-admission-verification.json",
        "dispatch-admission.json",
        "dispatch-admission.json.sig",
        "owner-signing-keys-observation.json",
        "recovery-authority.json",
        "recovery-authority.json.sig",
        "recovery/dependency-staging-receipt.json",
        "recovery-execution-closure.json",
        "recovery-repository-observation.json",
        "release-authorization.json",
    }
)
_RECOVERY_CAPSULE_SOURCE_ASSETS = frozenset(
    {
        ".github/workflows/release-transaction.yml",
        ".github/workflows/release.yml",
        ".gitleaksignore",
        "release-control-credential-policy.json",
        "release-control-source-registry.json",
        "scripts/bootstrap_recovery.py",
        "scripts/bootstrap_recovery_tcb.sh",
        "scripts/recovery_launcher.py",
        "scripts/release_candidate_manifest.py",
        "scripts/release_control_receipt.py",
        "scripts/release_promotion_transaction.py",
    }
)
_RECOVERY_CAPSULE_WORKFLOWS = frozenset(
    {
        ".github/workflows/release-transaction.yml",
        ".github/workflows/release.yml",
    }
)
_RECOVERY_SANDBOX_ASSET = "recovery/bin/bwrap"
_RECOVERY_CAPSULE_SCHEMA_ASSETS = frozenset(
    {
        f"schemas/{name}"
        for name in (
            "kestrel.actions_artifact_observation.v1.schema.json",
            "kestrel.canonicalization_vectors.v1.schema.json",
            "kestrel.credential_scope_authority.v1.schema.json",
            "kestrel.dispatch_admission.v1.schema.json",
            "kestrel.dispatch_identity.v1.schema.json",
            "kestrel.dispatch_tombstone.v1.schema.json",
            "kestrel.github_release_authority.v3.schema.json",
            "kestrel.pypi_upload_authority_prerequisite.v3.schema.json",
            "kestrel.recovery_capsule_smoke.v1.schema.json",
            "kestrel.recovery_dependency_staging.v1.schema.json",
            "kestrel.recovery_environment.v1.schema.json",
            "kestrel.recovery_execution_closure.v1.schema.json",
            "kestrel.recovery_host_actuator_binding.v1.schema.json",
            "kestrel.recovery_python_runtime.v1.schema.json",
            "kestrel.recovery_repository_authority.v1.schema.json",
            "kestrel.recovery_runtime.v1.schema.json",
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
            "kestrel.release_shared.v1.schema.json",
            "kestrel.repository_writer_inventory.v1.schema.json",
            "kestrel.runtime_credential_verification.v1.schema.json",
            "kestrel.source_observation.v1.schema.json",
            "kestrel.source_registry.v1.schema.json",
        )
    }
)


def _capsule_asset_name_is_allowed(name: str) -> bool:
    if name in (
        _RECOVERY_CAPSULE_FIXED_ASSETS
        | _RECOVERY_CAPSULE_SOURCE_ASSETS
        | _RECOVERY_CAPSULE_SCHEMA_ASSETS
        | {
            _RECOVERY_SANDBOX_ASSET,
            "recovery/environment-manifest.json",
            "recovery/requirements.txt",
            "recovery/python-runtime-manifest.json",
            "recovery/python-runtime.tar.gz",
            "recovery/runtime-manifest.json",
            "recovery/wheelhouse-manifest.json",
        }
    ):
        return True
    path = PurePosixPath(name)
    if path.parts[0] == "evidence" and path.suffix in {".json", ".sig"}:
        return True
    return (
        len(path.parts) == 3
        and path.parts[:2] == ("recovery", "runtime")
        and re.fullmatch(r"[A-Za-z0-9._+-]+", path.name) is not None
    ) or (
        len(path.parts) == 3
        and path.parts[:2] == ("recovery", "wheelhouse")
        and path.name.endswith(".whl")
    )


def _validate_capsule_asset_name(value: object) -> str:
    name = _validate_string(value, label="recovery capsule asset name")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or name == "."
        or "\\" in name
        or path.as_posix() != name
        or any(part in {"", ".", ".."} for part in path.parts)
        or name == "recovery-capsule-manifest.json"
    ):
        raise ReleaseControlError("recovery capsule asset path is unsafe")
    return name


def validate_recovery_capsule_manifest(
    value: Mapping[str, object],
) -> JSONObject:
    """Validate deterministic archive, secret-scan, and recovery authority policy."""

    manifest = _copy_json_object(value, label="recovery capsule manifest")
    _validate_schema(RECOVERY_CAPSULE_SCHEMA, manifest, label="recovery capsule manifest")
    if manifest.get("archive_policy") != _RECOVERY_ARCHIVE_POLICY:
        raise ReleaseControlError("recovery capsule archive policy mismatch")
    scan = _object(manifest.get("secret_scan"), label="recovery capsule secret scan")
    if (
        scan.get("image") != _GITLEAKS_IMAGE
        or scan.get("command") != "dir --redact=100 --no-banner"
        or scan.get("unallowed_findings") != 0
    ):
        raise ReleaseControlError("recovery capsule secret scan policy mismatch")
    repository = _object(manifest.get("recovery_repository"), label="recovery capsule repository")
    if repository.get("full_name") != "John-MiracleWorker/Kestrel-Release-Recovery":
        raise ReleaseControlError("recovery capsule repository identity mismatch")
    assets = [
        _object(item, label="recovery capsule asset")
        for item in _array(manifest.get("assets"), label="recovery capsule assets")
    ]
    names = [_validate_capsule_asset_name(item.get("name")) for item in assets]
    if names != sorted(names) or len(names) != len(set(names)):
        raise ReleaseControlError("recovery capsule assets are not sorted unique")
    if any(not _capsule_asset_name_is_allowed(name) for name in names):
        raise ReleaseControlError("recovery capsule contains an unknown asset name")
    if "release-authorization.json" not in names:
        raise ReleaseControlError("recovery capsule lacks original transaction authorization")
    forbidden_fragments = (
        "execution-authorization",
        "token",
        "cookie",
        "credential-bytes",
        "environment-dump",
        "raw-dashboard",
        "screenshot",
    )
    if any(any(fragment in name.lower() for fragment in forbidden_fragments) for name in names):
        raise ReleaseControlError(
            "recovery capsule contains forbidden execution or secret material"
        )
    workflows = [
        _object(item, label="recovery capsule workflow digest")
        for item in _array(
            manifest.get("source_workflow_digests"),
            label="recovery capsule workflow digests",
        )
    ]
    workflow_paths = [item.get("path") for item in workflows]
    if ".github/workflows/release.yml" not in workflow_paths:
        raise ReleaseControlError("recovery capsule lacks the release ingress workflow")
    release = _object(manifest.get("release"), label="recovery capsule Release")
    if re.fullmatch(r"recovery-[1-9][0-9]*-1", str(release.get("tag"))) is None:
        raise ReleaseControlError("recovery capsule Release tag mismatch")
    if release.get("immutable") is False:
        if any(
            release.get(field) is not None for field in ("release_id", "publication_receipt_digest")
        ):
            raise ReleaseControlError("unpublished recovery capsule has publication authority")
    elif release.get("release_id") is None or release.get("publication_receipt_digest") is None:
        raise ReleaseControlError("immutable recovery capsule lacks publication evidence")
    if manifest.get("provenance") != {
        "producer": "scripts/release_control_receipt.py",
        "provider": "local",
        "method": "deterministic-recovery-capsule",
    }:
        raise ReleaseControlError("recovery capsule provenance mismatch")
    return manifest


def verify_recovery_capsule_root(
    root: Path,
    *,
    expected_owner_key_fingerprint: str | None = None,
) -> tuple[JSONObject, bytes]:
    """Verify the exact canonical manifest-to-filesystem capsule binding."""

    if not root.is_dir() or root.is_symlink():
        raise ReleaseControlError("recovery capsule root is invalid")
    manifest_path = root / "recovery-capsule-manifest.json"
    manifest_raw = _read_regular(
        manifest_path,
        label="recovery capsule manifest",
        max_bytes=MAX_SOURCE_BODY_BYTES,
    )
    manifest = validate_recovery_capsule_manifest(
        _object(
            strict_canonical_json(manifest_raw, label="recovery capsule manifest"),
            label="recovery capsule manifest",
        )
    )
    asset_records = [
        _object(item, label="recovery capsule asset")
        for item in _array(manifest.get("assets"), label="recovery capsule assets")
    ]
    expected_files = {
        _validate_capsule_asset_name(item.get("name")): item for item in asset_records
    }
    expected_file_names = {*expected_files, "recovery-capsule-manifest.json"}
    expected_directories: set[str] = set()
    for name in expected_file_names:
        parent = PurePosixPath(name).parent
        while parent.as_posix() not in {"", "."}:
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ReleaseControlError("recovery capsule inventory contains a symlink")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            actual_directories.add(relative)
        elif path.is_file():
            actual_files.add(relative)
        else:
            raise ReleaseControlError("recovery capsule inventory contains a special file")
    if actual_files != expected_file_names:
        raise ReleaseControlError("recovery capsule file inventory mismatch")
    if actual_directories != expected_directories:
        raise ReleaseControlError("recovery capsule directory inventory mismatch")

    inventory: list[JSONValue] = []
    scanned_bytes = 0
    asset_bytes: dict[str, bytes] = {}
    for name, item in sorted(expected_files.items()):
        raw = _read_regular(
            root / name,
            label=f"recovery capsule asset {name}",
            max_bytes=2_147_483_648,
        )
        if item.get("size_bytes") != len(raw) or item.get("sha256") != _sha256(raw):
            raise ReleaseControlError(f"recovery capsule asset identity mismatch: {name}")
        asset_bytes[name] = raw
        scanned_bytes += len(raw)
        if scanned_bytes > 2_147_483_648:
            raise ReleaseControlError("recovery capsule asset inventory is too large")
        inventory.append({"name": name, "sha256": _sha256(raw), "size_bytes": len(raw)})

    _validate_capsule_execution_asset_closure(asset_bytes)

    workflow_records = [
        _object(item, label="recovery capsule workflow digest")
        for item in _array(
            manifest.get("source_workflow_digests"),
            label="recovery capsule workflow digests",
        )
    ]
    if {item.get("path") for item in workflow_records} != set(_RECOVERY_CAPSULE_WORKFLOWS):
        raise ReleaseControlError("recovery capsule workflow digest allowlist mismatch")

    transaction = asset_bytes.get("release-authorization.json")
    if transaction is None or manifest.get("transaction_authorization_digest") != _sha256(
        transaction
    ):
        raise ReleaseControlError("recovery capsule transaction authorization asset mismatch")
    admission = asset_bytes.get("dispatch-admission.json")
    if admission is not None and manifest.get("admission_authority_digest") != _sha256(admission):
        raise ReleaseControlError("recovery capsule admission asset mismatch")
    admission_signature = asset_bytes.get("dispatch-admission.json.sig")
    admission_verification = asset_bytes.get("dispatch-admission-verification.json")
    if admission is None or admission_signature is None or admission_verification is None:
        raise ReleaseControlError("recovery capsule admission evidence is incomplete")
    _validate_capsule_dispatch_admission_binding(
        _object(
            strict_canonical_json(
                transaction,
                label="recovery capsule transaction authorization",
            ),
            label="recovery capsule transaction authorization",
        ),
        _object(
            strict_canonical_json(
                admission,
                label="recovery capsule dispatch admission",
            ),
            label="recovery capsule dispatch admission",
        ),
        signature=admission_signature,
    )
    _validate_dispatch_admission_verification(
        _object(
            strict_canonical_json(
                admission_verification,
                label="recovery capsule admission verification",
            ),
            label="recovery capsule admission verification",
        ),
        admission=admission,
        signature=admission_signature,
    )
    owner_keys = asset_bytes.get("owner-signing-keys-observation.json")
    if owner_keys is None:
        raise ReleaseControlError("recovery capsule owner signing key evidence is missing")
    public_key, owner_fingerprint = _offline_owner_signing_key(
        owner_keys,
        expected_fingerprint=expected_owner_key_fingerprint,
    )
    checked_admission = _object(
        strict_canonical_json(admission, label="recovery capsule dispatch admission"),
        label="recovery capsule dispatch admission",
    )
    if (
        checked_admission.get("signing_principal") != SIGNING_PRINCIPAL
        or checked_admission.get("signing_key_fingerprint") != owner_fingerprint
    ):
        raise ReleaseControlError("recovery capsule admission signer binding mismatch")
    verify_detached_signature(
        receipt=admission,
        signature=admission_signature,
        expected_fingerprint=owner_fingerprint,
        namespace=SIGNING_NAMESPACE,
    )
    embedded_admission_key, _ = _decode_ssh_signature(admission_signature)
    if embedded_admission_key != _decode_ssh_public_key_blob(
        public_key, label="offline owner signing public key"
    ):
        raise ReleaseControlError("recovery capsule admission public key mismatch")
    repository = _object(manifest.get("recovery_repository"), label="recovery capsule repository")
    for name, field in (
        ("recovery-authority.json", "authority_receipt_digest"),
        ("recovery-authority.json.sig", "authority_signature_digest"),
    ):
        authority_raw = asset_bytes.get(name)
        if authority_raw is not None and repository.get(field) != _sha256(authority_raw):
            raise ReleaseControlError("recovery capsule authority asset mismatch")
    recovery_authority = asset_bytes.get("recovery-authority.json")
    recovery_signature = asset_bytes.get("recovery-authority.json.sig")
    if recovery_authority is None or recovery_signature is None:
        raise ReleaseControlError("recovery capsule repository authority evidence is incomplete")
    authority = validate_recovery_repository_authority(
        _object(
            strict_canonical_json(
                recovery_authority,
                label="recovery capsule repository authority",
            ),
            label="recovery capsule repository authority",
        )
    )
    authority_repository = _object(
        authority.get("repository"), label="recovery capsule authority repository"
    )
    if authority_repository.get("full_name") != repository.get(
        "full_name"
    ) or authority_repository.get("id") != repository.get("id"):
        raise ReleaseControlError("recovery capsule repository authority identity mismatch")
    verify_detached_signature(
        receipt=recovery_authority,
        signature=recovery_signature,
        expected_fingerprint=owner_fingerprint,
        namespace=SIGNING_NAMESPACE,
    )
    embedded_authority_key, _ = _decode_ssh_signature(recovery_signature)
    if embedded_authority_key != _decode_ssh_public_key_blob(
        public_key, label="offline owner signing public key"
    ):
        raise ReleaseControlError("recovery capsule authority public key mismatch")
    for raw_workflow in _array(
        manifest.get("source_workflow_digests"),
        label="recovery capsule workflow digests",
    ):
        workflow = _object(raw_workflow, label="recovery capsule workflow digest")
        workflow_name = _validate_capsule_asset_name(workflow.get("path"))
        workflow_raw = asset_bytes.get(workflow_name)
        if workflow_raw is None or workflow.get("sha256") != _sha256(workflow_raw):
            raise ReleaseControlError("recovery capsule workflow asset mismatch")
    scan = _object(manifest.get("secret_scan"), label="recovery capsule secret scan")
    if (
        scan.get("inventory_sha256") != _sha256(canonical_json_bytes(inventory))
        or scan.get("scanned_file_count") != len(asset_bytes)
        or scan.get("scanned_bytes") != scanned_bytes
    ):
        raise ReleaseControlError("recovery capsule secret scan inventory mismatch")
    return manifest, manifest_raw


def _capsule_media_type(name: str) -> str:
    if name.endswith(".json"):
        return "application/json"
    if name.endswith((".tar", ".tar.gz")):
        return "application/x-tar"
    if name.endswith((".yml", ".yaml", ".txt", ".md", ".sh")):
        return "text/plain"
    if name.endswith(".py"):
        return "text/x-python"
    return "application/octet-stream"


def _validate_capsule_execution_asset_closure(
    asset_bytes: Mapping[str, bytes],
) -> JSONObject:
    """Require every executable/data/dependency asset to be closure-bound."""

    if set(_RECOVERY_CAPSULE_FIXED_ASSETS) - set(asset_bytes):
        raise ReleaseControlError("recovery capsule lacks a required fixed authority asset")
    closure_raw = asset_bytes.get("recovery-execution-closure.json")
    if closure_raw is None:
        raise ReleaseControlError("recovery capsule lacks its execution closure")
    closure = _object(
        strict_canonical_json(closure_raw, label="recovery capsule execution closure"),
        label="recovery capsule execution closure",
    )
    _validate_schema(
        "kestrel.recovery_execution_closure.v1",
        closure,
        label="recovery capsule execution closure",
    )

    member_digests: dict[str, str] = {}
    for field in ("python_members", "shell_helpers", "data_resources"):
        previous = ""
        for raw_item in _array(closure.get(field), label=f"capsule {field}"):
            item = _object(raw_item, label=f"capsule {field} item")
            name = _validate_capsule_asset_name(item.get("path"))
            if name <= previous:
                raise ReleaseControlError(f"recovery capsule {field} is not sorted unique")
            previous = name
            if name in member_digests or not _capsule_asset_name_is_allowed(name):
                raise ReleaseControlError(
                    "recovery capsule closure contains an unknown or duplicate asset"
                )
            member_digests[name] = _digest(item.get("sha256"), label=f"capsule {field} digest")

    required_members = (
        _RECOVERY_CAPSULE_SOURCE_ASSETS
        | _RECOVERY_CAPSULE_SCHEMA_ASSETS
        | {
            _RECOVERY_SANDBOX_ASSET,
            "recovery/environment-manifest.json",
            "recovery/requirements.txt",
            "recovery/python-runtime-manifest.json",
            "recovery/python-runtime.tar.gz",
            "recovery/runtime-manifest.json",
            "recovery/wheelhouse-manifest.json",
        }
    )
    if not required_members.issubset(member_digests):
        raise ReleaseControlError(
            "recovery capsule closure lacks the complete source/schema/dependency allowlist"
        )
    if not any(name.startswith("evidence/") for name in member_digests):
        raise ReleaseControlError("recovery capsule closure lacks normalized evidence")
    for name, expected_digest in member_digests.items():
        raw = asset_bytes.get(name)
        if raw is None or _sha256(raw) != expected_digest:
            raise ReleaseControlError(f"recovery capsule closure asset identity mismatch: {name}")

    sandbox_items = [
        _object(item, label="capsule sandbox executable")
        for item in _array(
            closure.get("external_executables"),
            label="capsule external executables",
        )
        if _object(item, label="capsule external executable").get("name") == "sandbox"
    ]
    sandbox_digest = member_digests.get(_RECOVERY_SANDBOX_ASSET)
    if len(sandbox_items) != 1 or sandbox_digest is None:
        raise ReleaseControlError("recovery capsule lacks its bound sandbox executable")
    sandbox = sandbox_items[0]
    sandbox_path = Path(
        _validate_string(sandbox.get("path"), label="capsule sandbox executable path")
    )
    if (
        not sandbox_path.is_absolute()
        or PurePosixPath(sandbox_path.as_posix()).parts[-3:] != ("recovery", "bin", "bwrap")
        or sandbox.get("sha256") != sandbox_digest
    ):
        raise ReleaseControlError("recovery capsule sandbox binding mismatch")

    dependency_lock = _object(closure.get("dependency_lock"), label="capsule dependency lock")
    if dependency_lock.get("requirements_path") != "recovery/requirements.txt":
        raise ReleaseControlError("recovery capsule dependency requirements path mismatch")
    if dependency_lock.get("requirements_sha256") != member_digests.get(
        "recovery/requirements.txt"
    ):
        raise ReleaseControlError("recovery capsule dependency requirements digest mismatch")
    environment_manifest_raw = asset_bytes.get("recovery/environment-manifest.json")
    if (
        environment_manifest_raw is None
        or dependency_lock.get("environment_manifest_sha256") != _sha256(environment_manifest_raw)
        or member_digests.get("recovery/environment-manifest.json")
        != _sha256(environment_manifest_raw)
    ):
        raise ReleaseControlError("recovery capsule environment manifest binding mismatch")
    environment_manifest = _object(
        strict_canonical_json(
            environment_manifest_raw,
            label="recovery capsule environment manifest",
        ),
        label="recovery capsule environment manifest",
    )
    _validate_schema(
        "kestrel.recovery_environment.v1",
        environment_manifest,
        label="recovery capsule environment manifest",
    )
    runtime_manifest_raw = asset_bytes.get("recovery/runtime-manifest.json")
    if (
        runtime_manifest_raw is None
        or dependency_lock.get("runtime_manifest_sha256") != _sha256(runtime_manifest_raw)
        or member_digests.get("recovery/runtime-manifest.json") != _sha256(runtime_manifest_raw)
    ):
        raise ReleaseControlError("recovery capsule runtime manifest binding mismatch")
    runtime_manifest = _object(
        strict_canonical_json(
            runtime_manifest_raw,
            label="recovery capsule runtime manifest",
        ),
        label="recovery capsule runtime manifest",
    )
    _validate_schema(
        "kestrel.recovery_runtime.v1",
        runtime_manifest,
        label="recovery capsule runtime manifest",
    )
    runtime_items = _array(runtime_manifest.get("files"), label="recovery capsule runtime files")
    if (
        _array(closure.get("runtime_files"), label="capsule recovery runtime files")
        != runtime_items
    ):
        raise ReleaseControlError("recovery capsule runtime closure binding mismatch")
    for raw_runtime in runtime_items:
        runtime_item = _object(raw_runtime, label="recovery capsule runtime file")
        name = _validate_capsule_asset_name(runtime_item.get("asset_path"))
        raw = asset_bytes.get(name)
        if (
            raw is None
            or member_digests.get(name) != runtime_item.get("sha256")
            or len(raw) != runtime_item.get("size_bytes")
            or _sha256(raw) != runtime_item.get("sha256")
        ):
            raise ReleaseControlError("recovery capsule runtime file binding mismatch")
    python_manifest_raw = asset_bytes.get("recovery/python-runtime-manifest.json")
    python_archive_raw = asset_bytes.get("recovery/python-runtime.tar.gz")
    if (
        python_manifest_raw is None
        or python_archive_raw is None
        or dependency_lock.get("python_runtime_manifest_sha256") != _sha256(python_manifest_raw)
        or dependency_lock.get("python_runtime_archive_sha256") != _sha256(python_archive_raw)
        or member_digests.get("recovery/python-runtime-manifest.json")
        != _sha256(python_manifest_raw)
        or member_digests.get("recovery/python-runtime.tar.gz") != _sha256(python_archive_raw)
    ):
        raise ReleaseControlError("recovery capsule Python runtime binding mismatch")
    python_manifest = _object(
        strict_canonical_json(
            python_manifest_raw,
            label="recovery capsule Python runtime manifest",
        ),
        label="recovery capsule Python runtime manifest",
    )
    _validate_schema(
        "kestrel.recovery_python_runtime.v1",
        python_manifest,
        label="recovery capsule Python runtime manifest",
    )
    if python_manifest.get("runtime_archive_sha256") != _sha256(
        python_archive_raw
    ) or python_manifest.get("runtime_archive_size_bytes") != len(python_archive_raw):
        raise ReleaseControlError("recovery capsule Python runtime archive identity mismatch")
    python_items = [
        _object(item, label="capsule Python executable")
        for item in _array(
            closure.get("external_executables"), label="capsule external executables"
        )
        if _object(item, label="capsule external executable").get("name") == "python"
    ]
    if len(python_items) != 1:
        raise ReleaseControlError("recovery capsule Python executable is ambiguous")
    python_path = PurePosixPath(
        _validate_string(python_items[0].get("path"), label="capsule Python path")
    )
    environment_root = PurePosixPath(
        _validate_string(
            environment_manifest.get("environment_root"),
            label="capsule environment root",
        )
    )
    python_runtime = _object(closure.get("python_runtime"), label="capsule Python runtime")
    if (
        not python_path.is_absolute()
        or python_path.parts[-4:] != ("recovery-runtime", "environment", "bin", "python")
        or environment_root != python_path.parent.parent
        or environment_manifest.get("site_packages_path")
        != str(environment_root / "lib" / "python3.11" / "site-packages")
        or environment_manifest.get("python_version") != python_runtime.get("version")
        or environment_manifest.get("python_abi") != python_runtime.get("abi")
        or python_items[0].get("sha256") != python_manifest.get("python_executable_sha256")
    ):
        raise ReleaseControlError("recovery capsule Python executable binding mismatch")
    wheelhouse_manifest_raw = asset_bytes.get("recovery/wheelhouse-manifest.json")
    if (
        wheelhouse_manifest_raw is None
        or dependency_lock.get("wheelhouse_manifest_sha256") != _sha256(wheelhouse_manifest_raw)
        or member_digests.get("recovery/wheelhouse-manifest.json")
        != _sha256(wheelhouse_manifest_raw)
    ):
        raise ReleaseControlError("recovery capsule wheelhouse manifest binding mismatch")
    wheelhouse_manifest = _object(
        strict_canonical_json(
            wheelhouse_manifest_raw,
            label="recovery capsule wheelhouse manifest",
        ),
        label="recovery capsule wheelhouse manifest",
    )
    _require_exact_fields(
        wheelhouse_manifest,
        frozenset({"schema", "wheels"}),
        label="recovery capsule wheelhouse manifest",
    )
    if wheelhouse_manifest.get("schema") != "kestrel.recovery_wheelhouse.v1":
        raise ReleaseControlError("recovery capsule wheelhouse schema mismatch")
    wheel_items = _array(wheelhouse_manifest.get("wheels"), label="recovery capsule wheels")
    if not wheel_items:
        raise ReleaseControlError("recovery capsule wheelhouse must not be empty")
    wheel_names: set[str] = set()
    previous_wheel = ""
    for raw_item in wheel_items:
        item = _object(raw_item, label="recovery capsule wheel")
        _require_exact_fields(
            item,
            frozenset({"filename", "sha256", "size_bytes"}),
            label="recovery capsule wheel",
        )
        filename = _validate_string(item.get("filename"), label="recovery capsule wheel filename")
        if (
            PurePosixPath(filename).name != filename
            or not filename.endswith(".whl")
            or filename <= previous_wheel
        ):
            raise ReleaseControlError(
                "recovery capsule wheelhouse names are not safe sorted wheels"
            )
        previous_wheel = filename
        name = f"recovery/wheelhouse/{filename}"
        if name in wheel_names:
            raise ReleaseControlError("recovery capsule wheel is duplicated")
        wheel_names.add(name)
        raw = asset_bytes.get(name)
        if (
            raw is None
            or item.get("size_bytes")
            != _safe_integer(
                item.get("size_bytes"),
                label="recovery capsule wheel size",
                positive=True,
            )
            or len(raw) != item.get("size_bytes")
            or _sha256(raw) != _digest(item.get("sha256"), label="recovery capsule wheel digest")
        ):
            raise ReleaseControlError("recovery capsule wheelhouse asset identity mismatch")

    expected_names = set(_RECOVERY_CAPSULE_FIXED_ASSETS) | set(member_digests) | wheel_names
    if set(asset_bytes) != expected_names:
        raise ReleaseControlError(
            "recovery capsule asset is outside the execution closure allowlist"
        )
    return closure


def build_recovery_capsule_manifest(
    *,
    candidate: Mapping[str, object],
    transaction_authorization: bytes,
    admission_authority_digest: str,
    source_workflows: Mapping[str, bytes],
    asset_bytes: Mapping[str, bytes],
    secret_scan: Mapping[str, object],
    recovery_repository: Mapping[str, object],
    promotion_run_id: int,
    source_records: Mapping[str, bytes],
) -> JSONObject:
    """Build the pre-publication manifest for a deterministic recovery capsule."""

    transaction = _object(
        strict_canonical_json(transaction_authorization, label="capsule transaction authorization"),
        label="capsule transaction authorization",
    )
    if (
        transaction.get("schema") != "kestrel.release_server_authorization.v3"
        or transaction.get("authorization_kind") != "transaction"
        or transaction.get("mode") != "initiate"
        or transaction.get("candidate") != candidate
    ):
        raise ReleaseControlError("recovery capsule transaction authorization identity mismatch")
    checked_assets: dict[str, bytes] = {}
    total_bytes = 0
    for index, (name, raw) in enumerate(asset_bytes.items(), start=1):
        if index > MAX_SOURCE_ENVELOPES:
            raise ReleaseControlError("recovery capsule has too many assets")
        checked_name = _validate_capsule_asset_name(name)
        if checked_name in checked_assets or type(raw) is not bytes:
            raise ReleaseControlError("recovery capsule asset identity is invalid")
        if len(raw) > 2_147_483_648:
            raise ReleaseControlError("recovery capsule asset exceeds its size limit")
        total_bytes += len(raw)
        if total_bytes > 2_147_483_648:
            raise ReleaseControlError("recovery capsule exceeds its total size limit")
        checked_assets[checked_name] = bytes(raw)
    if (
        "release-authorization.json" not in checked_assets
        or checked_assets["release-authorization.json"] != transaction_authorization
    ):
        raise ReleaseControlError("recovery capsule original authorization asset mismatch")
    if "recovery-execution-closure.json" in checked_assets:
        _validate_capsule_execution_asset_closure(checked_assets)
    assets: list[JSONValue] = [
        {
            "name": name,
            "sha256": _sha256(raw),
            "size_bytes": len(raw),
            "media_type": _capsule_media_type(name),
        }
        for name, raw in sorted(checked_assets.items())
    ]
    workflow_items = dict(_bounded_source_items(source_workflows))
    if "recovery-execution-closure.json" in checked_assets and set(workflow_items) != set(
        _RECOVERY_CAPSULE_WORKFLOWS
    ):
        raise ReleaseControlError("recovery capsule workflow digest allowlist mismatch")
    workflows: list[JSONValue] = [
        {"path": path, "sha256": _sha256(raw)} for path, raw in sorted(workflow_items.items())
    ]
    manifest: JSONObject = {
        "schema": RECOVERY_CAPSULE_SCHEMA,
        "candidate": _copy_json_object(candidate, label="capsule candidate"),
        "transaction_authorization_digest": _sha256(transaction_authorization),
        "admission_authority_digest": _digest(
            admission_authority_digest,
            label="capsule admission authority digest",
        ),
        "source_workflow_digests": workflows,
        "assets": assets,
        "archive_policy": dict(_RECOVERY_ARCHIVE_POLICY),
        "secret_scan": _copy_json_object(secret_scan, label="capsule secret scan"),
        "recovery_repository": _copy_json_object(
            recovery_repository, label="capsule recovery repository"
        ),
        "release": {
            "tag": f"recovery-{_safe_integer(promotion_run_id, label='promotion run ID', positive=True)}-1",
            "release_id": None,
            "immutable": False,
            "publication_receipt_digest": None,
        },
        "evidence": {
            "source_bundle_digest": source_bundle_digest(source_records),
            "canonicalization_vector_digest": canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "local",
            "method": "deterministic-recovery-capsule",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    validate_recovery_capsule_manifest(manifest)
    return manifest


def _verify_signed_authority(
    *,
    receipt: bytes,
    signature: bytes,
    owner_signing_keys_observation: bytes,
    schema: str,
    validator: Callable[[Mapping[str, object]], JSONObject],
    _clock: Callable[[], datetime],
) -> tuple[JSONObject, datetime, str]:
    value = _object(
        strict_canonical_json(receipt, label=f"{schema} receipt"),
        label=f"{schema} receipt",
    )
    authority = validator(value)
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ReleaseControlError("authority verification clock must be aware UTC")
    now = now.astimezone(UTC).replace(microsecond=0)
    observed = parse_timestamp(authority.get("observed_at"), label="authority observed_at")
    expires = parse_timestamp(authority.get("expires_at"), label="authority expires_at")
    if now < observed or now >= expires:
        raise ReleaseControlError("authority receipt is not currently fresh")
    if not verify_owner_detached_signature(
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
        _clock=lambda: now,
    ):
        raise ReleaseControlError("authority owner signature is invalid")
    fingerprint = signature_public_key_fingerprint(signature)
    return authority, now, fingerprint


def _authority_verification(
    *,
    authority: JSONObject,
    receipt: bytes,
    signature: bytes,
    owner_signing_keys_observation: bytes,
    now: datetime,
    fingerprint: str,
    authority_kind: str,
) -> JSONObject:
    return {
        "schema": f"kestrel.{authority_kind}_authority_verification.v1",
        "authority_schema": authority["schema"],
        "authority": _copy_json_object(authority, label=f"{authority_kind} verified authority"),
        "receipt_digest": _sha256(receipt),
        "signature_digest": _sha256(signature),
        "receipt_base64": base64.b64encode(receipt).decode("ascii"),
        "signature_base64": base64.b64encode(signature).decode("ascii"),
        "owner_signing_keys_observation_base64": base64.b64encode(
            owner_signing_keys_observation
        ).decode("ascii"),
        "signing_key_fingerprint": fingerprint,
        "verified_at": _format_timestamp(now, label="authority verified_at"),
        "validation_status": "validated",
    }


def verify_github_authority(
    *,
    receipt: bytes,
    signature: bytes,
    owner_signing_keys_observation: bytes,
    expected_run_id: int,
    expected_candidate_digest: str,
    expected_environment_id: int,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    authority, now, fingerprint = _verify_signed_authority(
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        schema=GITHUB_AUTHORITY_SCHEMA,
        validator=validate_github_authority,
        _clock=_clock,
    )
    candidate = _object(authority["candidate"], label="GitHub authority candidate")
    run = _object(authority["promotion_run"], label="GitHub authority run")
    environment = _object(authority["environment"], label="GitHub authority environment")
    if (
        run.get("run_id")
        != _safe_integer(expected_run_id, label="expected GitHub run ID", positive=True)
        or candidate.get("candidate_manifest_digest")
        != _digest(expected_candidate_digest, label="expected candidate digest")
        or environment.get("id")
        != _safe_integer(
            expected_environment_id,
            label="expected GitHub environment ID",
            positive=True,
        )
    ):
        raise ReleaseControlError("GitHub authority expected identity mismatch")
    return _authority_verification(
        authority=authority,
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        now=now,
        fingerprint=fingerprint,
        authority_kind="github_release",
    )


def verify_pypi_authority(
    *,
    receipt: bytes,
    signature: bytes,
    owner_signing_keys_observation: bytes,
    expected_run_id: int,
    expected_candidate_digest: str,
    expected_environment_id: int,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    authority, now, fingerprint = _verify_signed_authority(
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        schema=PYPI_AUTHORITY_SCHEMA,
        validator=validate_pypi_authority,
        _clock=_clock,
    )
    candidate = _object(authority["candidate"], label="PyPI authority candidate")
    run = _object(authority["promotion_run"], label="PyPI authority run")
    environment = _object(authority["environment"], label="PyPI authority environment")
    if (
        run.get("run_id")
        != _safe_integer(expected_run_id, label="expected PyPI run ID", positive=True)
        or candidate.get("candidate_manifest_digest")
        != _digest(expected_candidate_digest, label="expected candidate digest")
        or environment.get("id")
        != _safe_integer(
            expected_environment_id,
            label="expected PyPI environment ID",
            positive=True,
        )
    ):
        raise ReleaseControlError("PyPI authority expected identity mismatch")
    return _authority_verification(
        authority=authority,
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        now=now,
        fingerprint=fingerprint,
        authority_kind="pypi_upload",
    )


def verify_recovery_repository_authority(
    *,
    receipt: bytes,
    signature: bytes,
    owner_signing_keys_observation: bytes,
    expected_repository: str,
    expected_repository_id: int,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JSONObject:
    authority, now, fingerprint = _verify_signed_authority(
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        schema=RECOVERY_AUTHORITY_SCHEMA,
        validator=validate_recovery_repository_authority,
        _clock=_clock,
    )
    repository = _object(authority["repository"], label="recovery authority repository")
    if repository.get("full_name") != _validate_string(
        expected_repository, label="expected recovery repository"
    ) or repository.get("id") != _safe_integer(
        expected_repository_id,
        label="expected recovery repository ID",
        positive=True,
    ):
        raise ReleaseControlError("recovery authority expected repository mismatch")
    return _authority_verification(
        authority=authority,
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        now=now,
        fingerprint=fingerprint,
        authority_kind="recovery_repository",
    )


def _command_capture_source(args: argparse.Namespace) -> int:
    registry = _object(
        _load_canonical_file(
            Path(args.registry), label="source registry", max_bytes=4 * 1024 * 1024
        ),
        label="source registry",
    )
    raw_input = _read_regular(
        Path(args.raw_input), label="source raw input", max_bytes=MAX_SOURCE_BODY_BYTES
    )
    identity = (
        None
        if args.identity_observation is None
        else _read_regular(
            Path(args.identity_observation),
            label="source identity observation",
            max_bytes=4 * 1024 * 1024,
        )
    )
    envelope = capture_source(
        registry=registry,
        receipt_schema=args.receipt_schema,
        phase=None if args.phase == "null" else args.phase,
        mode=None if args.mode == "null" else args.mode,
        name=args.name,
        raw_input=raw_input,
        identity_observation=identity,
    )
    _write_cli_output(Path(args.output), canonical_json_bytes(envelope))
    return 0


def _command_canonicalize(args: argparse.Namespace) -> int:
    value = strict_canonical_json(
        _read_regular(
            Path(args.input), label="canonicalize input", max_bytes=MAX_SOURCE_BODY_BYTES
        ),
        label="canonicalize input",
    )
    _write_cli_output(Path(args.output), canonical_json_bytes(value))
    return 0


def _command_create_credential_scope_authority(args: argparse.Namespace) -> int:
    receipt = create_credential_scope_authority(
        purpose=args.purpose,
        credential_id=args.credential_id,
        principal_observation=_read_regular(
            Path(args.principal_observation),
            label="credential principal observation",
            max_bytes=1024 * 1024,
        ),
        grants_snapshot=_read_regular(
            Path(args.grants_snapshot),
            label="credential grants snapshot",
            max_bytes=4 * 1024 * 1024,
        ),
        token_fingerprint=args.token_fingerprint,
        controller_context=_read_regular(
            Path(args.controller_context),
            label="credential controller context",
            max_bytes=1024 * 1024,
        ),
    )
    _write_cli_output(Path(args.output), canonical_json_bytes(receipt))
    return 0


def _command_create_dispatch_identity(args: argparse.Namespace) -> int:
    context = _object(
        _load_canonical_file(
            Path(args.github_context_allowlist),
            label="GitHub context allowlist",
            max_bytes=4 * 1024 * 1024,
        ),
        label="GitHub context allowlist",
    )
    identity = create_dispatch_identity(github_context_allowlist=context)
    _write_cli_output(Path(args.output), canonical_json_bytes(identity))
    return 0


def _command_sign(args: argparse.Namespace) -> int:
    receipt = _read_regular(
        Path(args.receipt), label="receipt to sign", max_bytes=MAX_SOURCE_BODY_BYTES
    )
    signature = sign_receipt_detached(
        receipt=receipt,
        identity_file=Path(args.identity_file),
        principal=args.principal,
        namespace=args.namespace,
    )
    _write_cli_output(Path(args.output_signature), signature)
    return 0


def _read_secret_from_stdin() -> bytes:
    maximum = 64 * 1024
    raw = sys.stdin.buffer.read(maximum + 1)
    if not raw:
        raise ReleaseControlError("credential bytes were not provided on standard input")
    if len(raw) > maximum:
        raise ReleaseControlError("credential input exceeds its size limit")
    return raw


def _write_cli_output(path: Path, raw: bytes) -> None:
    if not write_once(path, raw):
        raise ReleaseControlError("CLI output path must be empty")


def _command_verify_runtime_credential(args: argparse.Namespace) -> int:
    signature = _read_regular(
        Path(args.scope_authority_signature),
        label="credential scope authority signature",
        max_bytes=1024 * 1024,
    )
    if not signature:
        raise ReleaseControlError("credential scope authority signature is empty")
    scope = _read_regular(
        Path(args.scope_authority),
        label="credential scope authority",
        max_bytes=4 * 1024 * 1024,
    )
    verification = verify_runtime_credential(
        scope_authority=scope,
        scope_authority_signature=signature,
        owner_signing_keys_observation=_read_regular(
            Path(args.owner_signing_keys_observation),
            label="owner signing keys observation",
            max_bytes=MAX_SOURCE_ENVELOPE_BYTES,
        ),
        identity_probe=_read_regular(
            Path(args.identity_probe_observation),
            label="credential identity probe observation",
            max_bytes=1024 * 1024,
        ),
        endpoint_probe_observations=_read_regular(
            Path(args.endpoint_probe_observations),
            label="credential endpoint probe observations",
            max_bytes=4 * 1024 * 1024,
        ),
        token_bytes=_read_secret_from_stdin(),
    )
    _write_cli_output(Path(args.output), canonical_json_bytes(verification))
    return 0


def _authority_cli_inputs(args: argparse.Namespace) -> tuple[bytes, bytes, bytes]:
    return (
        _read_regular(
            Path(args.receipt), label="authority receipt", max_bytes=MAX_SOURCE_BODY_BYTES
        ),
        _read_regular(Path(args.signature), label="authority signature", max_bytes=1024 * 1024),
        _read_regular(
            Path(args.owner_signing_keys_observation),
            label="owner signing keys observation",
            max_bytes=MAX_SOURCE_ENVELOPE_BYTES,
        ),
    )


def _command_verify_github_authority(args: argparse.Namespace) -> int:
    receipt, signature, owner_keys = _authority_cli_inputs(args)
    verification = verify_github_authority(
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_keys,
        expected_run_id=args.expected_run_id,
        expected_candidate_digest=args.expected_candidate_digest,
        expected_environment_id=args.expected_environment_id,
    )
    _write_cli_output(Path(args.output), canonical_json_bytes(verification))
    return 0


def _command_verify_pypi_authority(args: argparse.Namespace) -> int:
    receipt, signature, owner_keys = _authority_cli_inputs(args)
    verification = verify_pypi_authority(
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_keys,
        expected_run_id=args.expected_run_id,
        expected_candidate_digest=args.expected_candidate_digest,
        expected_environment_id=args.expected_environment_id,
    )
    _write_cli_output(Path(args.output), canonical_json_bytes(verification))
    return 0


def _command_verify_recovery_authority(args: argparse.Namespace) -> int:
    receipt, signature, owner_keys = _authority_cli_inputs(args)
    verification = verify_recovery_repository_authority(
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_keys,
        expected_repository=args.expected_repository,
        expected_repository_id=args.expected_repository_id,
    )
    _write_cli_output(Path(args.output), canonical_json_bytes(verification))
    return 0


def _command_create_recovery_authority(args: argparse.Namespace) -> int:
    authority = create_recovery_repository_authority(
        owner_authority_snapshot=_read_regular(
            Path(args.owner_authority_snapshot),
            label="recovery owner authority snapshot",
            max_bytes=MAX_SOURCE_ENVELOPE_BYTES,
        ),
        repository_observation=_read_regular(
            Path(args.repository_observation),
            label="recovery repository observation",
            max_bytes=MAX_SOURCE_ENVELOPE_BYTES,
        ),
        immutable_releases_observation=_read_regular(
            Path(args.immutable_releases_observation),
            label="recovery immutable Releases observation",
            max_bytes=MAX_SOURCE_ENVELOPE_BYTES,
        ),
        controller_context=_read_regular(
            Path(args.controller_context),
            label="recovery authority controller context",
            max_bytes=MAX_SOURCE_ENVELOPE_BYTES,
        ),
    )
    _write_cli_output(Path(args.output), canonical_json_bytes(authority))
    return 0


def _command_create_pypi_authority(args: argparse.Namespace) -> int:
    def read(name: str, label: str, *, source_envelope: bool = True) -> bytes:
        return _read_regular(
            Path(getattr(args, name)),
            label=label,
            max_bytes=(MAX_SOURCE_ENVELOPE_BYTES if source_envelope else MAX_SOURCE_BODY_BYTES),
        )

    authority = create_pypi_authority(
        public_project_observation=read(
            "public_project_observation", "public PyPI project observation"
        ),
        owner_authority_snapshot=read("owner_authority_snapshot", "PyPI owner authority snapshot"),
        promotion_run_observation=read("promotion_run_observation", "promotion run observation"),
        promotion_dispatch_identity=read(
            "promotion_dispatch_identity", "promotion dispatch identity"
        ),
        candidate_manifest=read("candidate_manifest", "candidate manifest", source_envelope=False),
        environment_observation=read("environment_observation", "PyPI environment observation"),
        controller_context=read("controller_context", "PyPI controller context"),
        bindings=read("bindings", "PyPI authority bindings"),
    )
    _write_cli_output(Path(args.output), canonical_json_bytes(authority))
    return 0


def _command_create_github_authority(args: argparse.Namespace) -> int:
    def read(name: str, label: str, *, source_envelope: bool = True) -> bytes:
        return _read_regular(
            Path(getattr(args, name)),
            label=label,
            max_bytes=(MAX_SOURCE_ENVELOPE_BYTES if source_envelope else MAX_SOURCE_BODY_BYTES),
        )

    authority = create_github_authority(
        repository_observation=read("repository_observation", "GitHub repository observation"),
        promotion_run_observation=read("promotion_run_observation", "promotion run observation"),
        promotion_dispatch_identity=read(
            "promotion_dispatch_identity", "promotion dispatch identity"
        ),
        candidate_manifest=read("candidate_manifest", "candidate manifest", source_envelope=False),
        environment_observation=read("environment_observation", "GitHub environment observation"),
        environment_policy_types_snapshot=read(
            "environment_policy_types_snapshot",
            "GitHub environment policy types snapshot",
        ),
        rulesets_observation=read("rulesets_observation", "ruleset inventory"),
        tag_ruleset_detail_observation=read("tag_ruleset_detail_observation", "tag ruleset detail"),
        ingress_ruleset_detail_observation=read(
            "ingress_ruleset_detail_observation", "ingress ruleset detail"
        ),
        workflow_observation=read("workflow_observation", "workflow observation"),
        default_branch_workflow_contents=read(
            "default_branch_workflow_contents", "default workflow contents"
        ),
        candidate_workflow_contents=read(
            "candidate_workflow_contents", "candidate workflow contents"
        ),
        dispatch_intent=read("dispatch_intent", "dispatch intent"),
        dispatch_intent_signature=read("dispatch_intent_signature", "dispatch intent signature"),
        dispatch_request=read("dispatch_request", "dispatch request"),
        dispatch_outcome=read("dispatch_outcome", "dispatch outcome"),
        installed_apps_snapshot=read("installed_apps_snapshot", "installed App snapshot"),
        ghcr_package_access_snapshot=read(
            "ghcr_package_access_snapshot", "GHCR package access snapshot"
        ),
        dispatcher_invalidation_snapshot=read(
            "dispatcher_invalidation_snapshot", "dispatcher invalidation snapshot"
        ),
        controller_context=read("controller_context", "GitHub controller context"),
        bindings=read("bindings", "GitHub authority bindings"),
    )
    _write_cli_output(Path(args.output), canonical_json_bytes(authority))
    return 0


def _capsule_add_asset(
    assets: dict[str, bytes], *, name: str, path: Path, max_bytes: int = 2_147_483_648
) -> None:
    checked_name = _validate_capsule_asset_name(name)
    if checked_name in assets:
        raise ReleaseControlError("duplicate recovery capsule asset")
    assets[checked_name] = _read_regular(
        path, label=f"recovery capsule asset {checked_name}", max_bytes=max_bytes
    )


def _capsule_collect_tree(assets: dict[str, bytes], *, root: Path, prefix: str) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ReleaseControlError(f"recovery capsule {prefix} root is invalid")
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ReleaseControlError("recovery capsule tree contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseControlError("recovery capsule tree contains a special file")
        relative = path.relative_to(root).as_posix()
        _capsule_add_asset(assets, name=f"{prefix}/{relative}", path=path)


def _capsule_collect_recovery_dependencies(
    assets: dict[str, bytes],
    *,
    dependency_root: Path,
    closure: JSONObject,
    expected_source_sha: str,
) -> None:
    if GIT_SHA_RE.fullmatch(expected_source_sha) is None:
        raise ReleaseControlError("recovery dependency candidate source SHA is invalid")
    lock = _object(closure.get("dependency_lock"), label="capsule dependency lock")
    if lock.get("requirements_path") != "recovery/requirements.txt":
        raise ReleaseControlError("recovery capsule dependency requirements path mismatch")
    environment_raw = assets.get("recovery/environment-manifest.json")
    if environment_raw is None or lock.get("environment_manifest_sha256") != _sha256(
        environment_raw
    ):
        raise ReleaseControlError("recovery capsule environment manifest binding mismatch")
    environment_manifest = _object(
        strict_canonical_json(
            environment_raw,
            label="recovery capsule environment manifest",
        ),
        label="recovery capsule environment manifest",
    )
    _validate_schema(
        "kestrel.recovery_environment.v1",
        environment_manifest,
        label="recovery capsule environment manifest",
    )
    _capsule_add_asset(
        assets,
        name=_RECOVERY_SANDBOX_ASSET,
        path=dependency_root / _RECOVERY_SANDBOX_ASSET,
        max_bytes=16 * 1024 * 1024,
    )
    _capsule_add_asset(
        assets,
        name="recovery/requirements.txt",
        path=dependency_root / "recovery/requirements.txt",
        max_bytes=16 * 1024 * 1024,
    )
    _capsule_add_asset(
        assets,
        name="recovery/wheelhouse-manifest.json",
        path=dependency_root / "recovery/wheelhouse-manifest.json",
        max_bytes=16 * 1024 * 1024,
    )
    _capsule_add_asset(
        assets,
        name="recovery/runtime-manifest.json",
        path=dependency_root / "recovery/runtime-manifest.json",
        max_bytes=16 * 1024 * 1024,
    )
    _capsule_add_asset(
        assets,
        name="recovery/python-runtime-manifest.json",
        path=dependency_root / "recovery/python-runtime-manifest.json",
        max_bytes=16 * 1024 * 1024,
    )
    _capsule_add_asset(
        assets,
        name="recovery/python-runtime.tar.gz",
        path=dependency_root / "recovery/python-runtime.tar.gz",
        max_bytes=512 * 1024 * 1024,
    )
    _capsule_add_asset(
        assets,
        name="recovery/dependency-staging-receipt.json",
        path=dependency_root / "recovery/dependency-staging-receipt.json",
        max_bytes=16 * 1024 * 1024,
    )
    receipt = _object(
        strict_canonical_json(
            assets["recovery/dependency-staging-receipt.json"],
            label="recovery capsule dependency staging receipt",
        ),
        label="recovery capsule dependency staging receipt",
    )
    _require_exact_fields(
        receipt,
        frozenset(
            {
                "schema",
                "inputs",
                "outputs",
                "provenance",
                "confidence",
                "validation_status",
                "receipt_digest",
            }
        ),
        label="recovery capsule dependency staging receipt",
    )
    if receipt.get("schema") != "kestrel.recovery_dependency_staging.v1":
        raise ReleaseControlError("recovery capsule dependency staging schema mismatch")
    _validate_schema(
        "kestrel.recovery_dependency_staging.v1",
        receipt,
        label="recovery capsule dependency staging receipt",
    )
    receipt_digest = _digest(
        receipt.get("receipt_digest"),
        label="recovery capsule dependency staging receipt digest",
    )
    unsigned_receipt = dict(receipt)
    del unsigned_receipt["receipt_digest"]
    if receipt_digest != _sha256(canonical_json_bytes(unsigned_receipt)):
        raise ReleaseControlError("recovery capsule dependency staging receipt digest mismatch")
    inputs = _object(receipt.get("inputs"), label="recovery dependency staging inputs")
    _require_exact_fields(
        inputs,
        frozenset(
            {
                "bubblewrap_package_url",
                "bubblewrap_package_sha256",
                "requirements_sha256",
                "python_package_url",
                "python_package_sha256",
                "python_version",
                "python_abi",
                "wheel_platform",
                "source_sha",
            }
        ),
        label="recovery dependency staging inputs",
    )
    if (
        inputs.get("bubblewrap_package_url") != _RECOVERY_BWRAP_PACKAGE_URL
        or inputs.get("bubblewrap_package_sha256") != _RECOVERY_BWRAP_PACKAGE_DIGEST
        or inputs.get("requirements_sha256") != _sha256(assets["recovery/requirements.txt"])
        or inputs.get("requirements_sha256") != lock.get("requirements_sha256")
        or inputs.get("python_package_url") != _RECOVERY_PYTHON_PACKAGE_URL
        or inputs.get("python_package_sha256") != _RECOVERY_PYTHON_PACKAGE_DIGEST
        or inputs.get("python_version") != _RECOVERY_PYTHON_VERSION
        or inputs.get("python_abi") != _RECOVERY_PYTHON_ABI
        or inputs.get("wheel_platform") != _RECOVERY_WHEEL_PLATFORM
        or inputs.get("source_sha") != expected_source_sha
    ):
        raise ReleaseControlError("recovery capsule dependency staging input binding mismatch")
    outputs = _object(receipt.get("outputs"), label="recovery dependency staging outputs")
    _require_exact_fields(
        outputs,
        frozenset(
            {
                "bubblewrap_sha256",
                "bubblewrap_version",
                "wheelhouse_manifest_sha256",
                "wheel_count",
                "runtime_manifest_sha256",
                "runtime_file_count",
                "python_runtime_manifest_sha256",
                "python_runtime_archive_sha256",
            }
        ),
        label="recovery dependency staging outputs",
    )
    if (
        outputs.get("bubblewrap_sha256") != _RECOVERY_BWRAP_BINARY_DIGEST
        or outputs.get("bubblewrap_sha256") != _sha256(assets[_RECOVERY_SANDBOX_ASSET])
        or outputs.get("bubblewrap_version") != _RECOVERY_BWRAP_VERSION
        or outputs.get("wheelhouse_manifest_sha256")
        != _sha256(assets["recovery/wheelhouse-manifest.json"])
        or outputs.get("wheelhouse_manifest_sha256") != lock.get("wheelhouse_manifest_sha256")
        or outputs.get("runtime_manifest_sha256")
        != _sha256(assets["recovery/runtime-manifest.json"])
        or outputs.get("runtime_manifest_sha256") != lock.get("runtime_manifest_sha256")
        or outputs.get("python_runtime_manifest_sha256")
        != _sha256(assets["recovery/python-runtime-manifest.json"])
        or outputs.get("python_runtime_manifest_sha256")
        != lock.get("python_runtime_manifest_sha256")
        or outputs.get("python_runtime_archive_sha256")
        != _sha256(assets["recovery/python-runtime.tar.gz"])
        or outputs.get("python_runtime_archive_sha256") != lock.get("python_runtime_archive_sha256")
    ):
        raise ReleaseControlError("recovery capsule dependency staging output binding mismatch")
    if (
        receipt.get("provenance") != _RECOVERY_DEPENDENCY_STAGING_PROVENANCE
        or receipt.get("confidence") != 1
        or receipt.get("validation_status") != "validated"
    ):
        raise ReleaseControlError("recovery capsule dependency staging evidence mismatch")
    runtime_manifest = _object(
        strict_canonical_json(
            assets["recovery/runtime-manifest.json"],
            label="recovery capsule runtime manifest",
        ),
        label="recovery capsule runtime manifest",
    )
    _validate_schema(
        "kestrel.recovery_runtime.v1",
        runtime_manifest,
        label="recovery capsule runtime manifest",
    )
    if (
        runtime_manifest.get("platform") != _RECOVERY_RUNTIME_PLATFORM
        or runtime_manifest.get("python_version") != _RECOVERY_PYTHON_VERSION
        or runtime_manifest.get("python_executable_sha256") != _RECOVERY_PYTHON_BINARY_DIGEST
    ):
        raise ReleaseControlError("recovery capsule runtime identity mismatch")
    python_runtime_manifest = _object(
        strict_canonical_json(
            assets["recovery/python-runtime-manifest.json"],
            label="recovery capsule Python runtime manifest",
        ),
        label="recovery capsule Python runtime manifest",
    )
    _validate_schema(
        "kestrel.recovery_python_runtime.v1",
        python_runtime_manifest,
        label="recovery capsule Python runtime manifest",
    )
    if (
        python_runtime_manifest.get("runtime_archive_sha256")
        != _sha256(assets["recovery/python-runtime.tar.gz"])
        or python_runtime_manifest.get("runtime_archive_size_bytes")
        != len(assets["recovery/python-runtime.tar.gz"])
        or python_runtime_manifest.get("python_executable_sha256") != _RECOVERY_PYTHON_BINARY_DIGEST
    ):
        raise ReleaseControlError("recovery capsule Python runtime identity mismatch")
    runtime_items = _array(runtime_manifest.get("files"), label="recovery capsule runtime files")
    if outputs.get("runtime_file_count") != len(runtime_items):
        raise ReleaseControlError("recovery capsule runtime file count mismatch")
    closure_runtime_items = _array(
        closure.get("runtime_files"), label="capsule recovery runtime files"
    )
    if closure_runtime_items != runtime_items:
        raise ReleaseControlError("recovery capsule runtime closure binding mismatch")
    previous_runtime = ""
    for raw_runtime in runtime_items:
        runtime_item = _object(raw_runtime, label="recovery capsule runtime file")
        _require_exact_fields(
            runtime_item,
            frozenset({"asset_path", "sandbox_path", "sha256", "size_bytes"}),
            label="recovery capsule runtime file",
        )
        asset_path = _validate_capsule_asset_name(runtime_item.get("asset_path"))
        sandbox_path = _validate_string(
            runtime_item.get("sandbox_path"), label="recovery runtime sandbox path"
        )
        size_bytes = _safe_integer(
            runtime_item.get("size_bytes"),
            label="recovery runtime file size",
            positive=True,
        )
        if sandbox_path <= previous_runtime or not sandbox_path.startswith("/"):
            raise ReleaseControlError("recovery runtime paths are not sorted absolute paths")
        previous_runtime = sandbox_path
        _capsule_add_asset(
            assets,
            name=asset_path,
            path=dependency_root / asset_path,
            max_bytes=256 * 1024 * 1024,
        )
        if len(assets[asset_path]) != size_bytes or _sha256(assets[asset_path]) != _digest(
            runtime_item.get("sha256"), label="recovery runtime file digest"
        ):
            raise ReleaseControlError("recovery capsule runtime file identity mismatch")
    manifest = _object(
        strict_canonical_json(
            assets["recovery/wheelhouse-manifest.json"],
            label="recovery capsule wheelhouse manifest",
        ),
        label="recovery capsule wheelhouse manifest",
    )
    _require_exact_fields(
        manifest,
        frozenset({"schema", "wheels"}),
        label="recovery capsule wheelhouse manifest",
    )
    if manifest.get("schema") != "kestrel.recovery_wheelhouse.v1":
        raise ReleaseControlError("recovery capsule wheelhouse schema mismatch")
    wheel_items = _array(manifest.get("wheels"), label="recovery capsule wheels")
    if not wheel_items:
        raise ReleaseControlError("recovery capsule wheelhouse must not be empty")
    if outputs.get("wheel_count") != len(wheel_items):
        raise ReleaseControlError("recovery capsule dependency wheel count mismatch")
    previous = ""
    for raw_item in wheel_items:
        item = _object(raw_item, label="recovery capsule wheel")
        _require_exact_fields(
            item,
            frozenset({"filename", "sha256", "size_bytes"}),
            label="recovery capsule wheel",
        )
        filename = _validate_string(item.get("filename"), label="recovery capsule wheel filename")
        if (
            PurePosixPath(filename).name != filename
            or not filename.endswith(".whl")
            or filename <= previous
        ):
            raise ReleaseControlError(
                "recovery capsule wheelhouse names are not safe sorted wheels"
            )
        previous = filename
        _capsule_add_asset(
            assets,
            name=f"recovery/wheelhouse/{filename}",
            path=dependency_root / "recovery" / "wheelhouse" / filename,
        )


def _run_capsule_secret_scan(*, root: Path, image: str) -> tuple[bytes, int]:
    if image != _GITLEAKS_IMAGE:
        raise ReleaseControlError("recovery capsule scanner image mismatch")
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--volume",
        f"{root.resolve()}:/scan:ro",
        image,
        "dir",
        "/scan",
        "--redact=100",
        "--no-banner",
        "--report-format=json",
    ]
    completed = subprocess.run(  # noqa: S603  # nosec B603
        command,
        check=False,
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
    )
    if completed.returncode != 0:
        raise ReleaseControlError("recovery capsule secret scan failed or found unallowed material")
    return completed.stdout, 0


def _prepare_capsule_output_root(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise ReleaseControlError("recovery capsule output root must be absent or empty")
        if stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise ReleaseControlError("recovery capsule output root must have mode 0700")
        return
    path.mkdir(mode=0o700, parents=False)


def _write_capsule_assets(root: Path, assets: Mapping[str, bytes]) -> None:
    for name, raw in sorted(assets.items()):
        path = root / name
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if not write_once(path, raw):
            raise ReleaseControlError("recovery capsule asset path already exists")
        os.chmod(path, 0o644)  # codeql[py/overly-permissive-file] — recovery capsule asset: 0o644 is the required world-readable archive contract (secrets stay 0o600)


def _command_create_recovery_capsule(
    args: argparse.Namespace,
    *,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    output_root = Path(args.output_root)
    _prepare_capsule_output_root(output_root)
    transaction = _read_regular(
        Path(args.transaction_authorization),
        label="capsule transaction authorization",
        max_bytes=MAX_SOURCE_BODY_BYTES,
    )
    transaction_value = _object(
        strict_canonical_json(transaction, label="capsule transaction authorization"),
        label="capsule transaction authorization",
    )
    candidate = _object(transaction_value.get("candidate"), label="capsule candidate")
    admission = _read_regular(
        Path(args.admission_receipt),
        label="capsule admission receipt",
        max_bytes=MAX_SOURCE_BODY_BYTES,
    )
    admission_value = _object(
        strict_canonical_json(admission, label="capsule admission receipt"),
        label="capsule admission receipt",
    )
    _validate_schema(DISPATCH_ADMISSION_SCHEMA, admission_value, label="capsule admission receipt")
    admission_signature = _read_regular(
        Path(args.admission_signature),
        label="capsule admission signature",
        max_bytes=1024 * 1024,
    )
    _validate_capsule_dispatch_admission_binding(
        transaction_value,
        admission_value,
        signature=admission_signature,
    )
    owner_keys = _read_regular(
        Path(args.owner_key_observation),
        label="capsule owner key observation",
        max_bytes=MAX_SOURCE_ENVELOPE_BYTES,
    )
    verify_owner_detached_signature(
        receipt=admission,
        signature=admission_signature,
        owner_signing_keys_observation=owner_keys,
        principal=SIGNING_PRINCIPAL,
        namespace=SIGNING_NAMESPACE,
        _clock=_clock,
    )
    admission_verification = _read_regular(
        Path(args.admission_verification),
        label="capsule admission verification",
        max_bytes=MAX_SOURCE_BODY_BYTES,
    )
    admission_verification_value = _object(
        strict_canonical_json(admission_verification, label="capsule admission verification"),
        label="capsule admission verification",
    )
    _validate_dispatch_admission_verification(
        admission_verification_value,
        admission=admission,
        signature=admission_signature,
    )
    recovery_authority = _read_regular(
        Path(args.recovery_authority_receipt),
        label="capsule recovery authority",
        max_bytes=MAX_SOURCE_BODY_BYTES,
    )
    recovery_signature = _read_regular(
        Path(args.recovery_authority_signature),
        label="capsule recovery authority signature",
        max_bytes=1024 * 1024,
    )
    recovery_repository_raw = _read_regular(
        Path(args.recovery_repository_observation),
        label="capsule recovery repository observation",
        max_bytes=MAX_SOURCE_BODY_BYTES,
    )
    recovery_repository = _object(
        strict_canonical_json(
            recovery_repository_raw,
            label="capsule recovery repository observation",
        ),
        label="capsule recovery repository observation",
    )
    repository_id = _safe_integer(
        recovery_repository.get("id"),
        label="capsule recovery repository ID",
        positive=True,
    )
    verify_recovery_repository_authority(
        receipt=recovery_authority,
        signature=recovery_signature,
        owner_signing_keys_observation=owner_keys,
        expected_repository="John-MiracleWorker/Kestrel-Release-Recovery",
        expected_repository_id=repository_id,
        _clock=_clock,
    )
    execution_closure = _read_regular(
        Path(args.execution_closure),
        label="capsule recovery execution closure",
        max_bytes=MAX_SOURCE_BODY_BYTES,
    )
    closure_value = strict_canonical_json(
        execution_closure, label="capsule recovery execution closure"
    )
    _validate_schema(
        "kestrel.recovery_execution_closure.v1",
        closure_value,
        label="capsule recovery execution closure",
    )
    assets: dict[str, bytes] = {
        "candidate-archive.tar": _read_regular(
            Path(args.candidate_archive),
            label="capsule candidate archive",
            max_bytes=2_147_483_648,
        ),
        "release-authorization.json": transaction,
        "dispatch-admission.json": admission,
        "dispatch-admission.json.sig": admission_signature,
        "dispatch-admission-verification.json": admission_verification,
        "owner-signing-keys-observation.json": owner_keys,
        "recovery-authority.json": recovery_authority,
        "recovery-authority.json.sig": recovery_signature,
        "recovery-repository-observation.json": recovery_repository_raw,
        "recovery-execution-closure.json": execution_closure,
        "recovery/environment-manifest.json": _read_regular(
            Path(args.environment_manifest),
            label="capsule recovery environment manifest",
            max_bytes=MAX_SOURCE_BODY_BYTES,
        ),
    }
    _capsule_collect_tree(assets, root=Path(args.normalized_evidence_root), prefix="evidence")
    _capsule_collect_tree(assets, root=Path(args.schema_root), prefix="schemas")
    source_root = Path(args.source_root)
    for relative in sorted(_RECOVERY_CAPSULE_SOURCE_ASSETS - {".gitleaksignore"}):
        _capsule_add_asset(assets, name=relative, path=source_root / relative)
    if args.gitleaks_ignore != ".gitleaksignore":
        raise ReleaseControlError("recovery capsule Gitleaks ignore path mismatch")
    ignore_path = source_root / args.gitleaks_ignore
    _capsule_add_asset(assets, name=args.gitleaks_ignore, path=ignore_path)
    _capsule_collect_recovery_dependencies(
        assets,
        dependency_root=Path(args.dependency_root),
        closure=_object(closure_value, label="capsule recovery execution closure"),
        expected_source_sha=_validate_string(
            candidate.get("source_sha"), label="capsule candidate source SHA"
        ),
    )
    _validate_capsule_execution_asset_closure(assets)
    _write_capsule_assets(output_root, assets)
    scan_report, finding_count = _run_capsule_secret_scan(
        root=output_root, image=args.gitleaks_image
    )
    inventory = [
        {"name": name, "sha256": _sha256(raw), "size_bytes": len(raw)}
        for name, raw in sorted(assets.items())
    ]
    secret_scan: JSONObject = {
        "image": args.gitleaks_image,
        "command": "dir --redact=100 --no-banner",
        "ignore_sha256": _sha256(assets[args.gitleaks_ignore]),
        "inventory_sha256": _sha256(canonical_json_bytes(inventory)),
        "redacted_report_sha256": _sha256(scan_report),
        "scanned_file_count": len(assets),
        "scanned_bytes": sum(len(raw) for raw in assets.values()),
        "unallowed_findings": finding_count,
    }
    workflow_assets = {
        name: assets[name]
        for name in (
            ".github/workflows/release-transaction.yml",
            ".github/workflows/release.yml",
        )
    }
    manifest = build_recovery_capsule_manifest(
        candidate=candidate,
        transaction_authorization=transaction,
        admission_authority_digest=_sha256(admission),
        source_workflows=workflow_assets,
        asset_bytes=assets,
        secret_scan=secret_scan,
        recovery_repository={
            "full_name": recovery_repository["full_name"],
            "id": repository_id,
            "authority_receipt_digest": _sha256(recovery_authority),
            "authority_signature_digest": _sha256(recovery_signature),
        },
        promotion_run_id=cast(
            int,
            _object(
                transaction_value.get("promotion_run"),
                label="capsule promotion run",
            )["run_id"],
        ),
        source_records={"asset-inventory": canonical_json_bytes(inventory)},
    )
    manifest_path = output_root / "recovery-capsule-manifest.json"
    if not write_once(manifest_path, canonical_json_bytes(manifest)):
        raise ReleaseControlError("recovery capsule manifest already exists")
    return 0


def deterministic_recovery_capsule_archive(root: Path) -> bytes:
    """Create a USTAR archive with lexical paths and fixed ownership/modes."""

    if not root.is_dir() or root.is_symlink():
        raise ReleaseControlError("recovery capsule root is invalid")
    files: list[tuple[str, Path]] = []
    directories: set[str] = set()
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ReleaseControlError("recovery capsule archive contains a symlink")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            directories.add(relative)
            continue
        if not path.is_file():
            raise ReleaseControlError("recovery capsule archive contains a special file")
        size = path.stat().st_size
        if size > 2_147_483_648:
            raise ReleaseControlError("recovery capsule archive member is too large")
        total += size
        if total > 2_147_483_648:
            raise ReleaseControlError("recovery capsule archive is too large")
        files.append((relative, path))
        parent = Path(relative).parent
        while parent.as_posix() not in {".", ""}:
            directories.add(parent.as_posix())
            parent = parent.parent
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(directories):
            info = tarfile.TarInfo(f"{name}/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = 0
            info.gid = 0
            info.mtime = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info)
        for name, path in files:
            raw = _read_regular(path, label=f"capsule archive {name}", max_bytes=2_147_483_648)
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.mtime = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(raw))
    return output.getvalue()


def _pinned_gh() -> Path:
    raw = os.environ.get("KESTREL_PINNED_GH")
    if raw is None:
        raise ReleaseControlError("KESTREL_PINNED_GH is required")
    path = Path(raw)
    if (
        not path.is_absolute()
        or not path.is_file()
        or path.is_symlink()
        or not os.access(path, os.X_OK)
    ):
        raise ReleaseControlError("pinned GitHub CLI path is invalid")
    return path


def _verify_pinned_gh(path: Path) -> str:
    if (
        not path.is_absolute()
        or not path.is_file()
        or path.is_symlink()
        or not os.access(path, os.X_OK)
    ):
        raise ReleaseControlError("pinned GitHub CLI path is invalid")
    binary_digest = _sha256(
        _read_regular(
            path,
            label="pinned GitHub CLI",
            max_bytes=256 * 1024 * 1024,
        )
    )
    platform_key = (sys.platform, platform.machine())
    expected_digest = PINNED_GH_BINARY_DIGESTS.get(platform_key)
    if expected_digest is None:
        raise ReleaseControlError("pinned GitHub CLI platform is unsupported")
    if binary_digest != expected_digest:
        raise ReleaseControlError("pinned GitHub CLI binary digest mismatch")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [str(path), "--version"],
        capture_output=True,
        check=False,
        env={"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"},
    )
    first_line = completed.stdout.splitlines()[:1]
    if completed.returncode != 0 or first_line != [PINNED_GH_VERSION_LINE]:
        raise ReleaseControlError("pinned GitHub CLI version mismatch")
    return binary_digest


def _run_pinned_gh_verification(path: Path, arguments: Sequence[str]) -> bytes:
    token = os.environ.get("GH_TOKEN")
    if token is None or not token:
        raise ReleaseControlError("GitHub verification credential is unavailable")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [str(path), *arguments],
        capture_output=True,
        check=False,
        env={
            "GH_TOKEN": token,
            "GH_PROMPT_DISABLED": "1",
            "NO_COLOR": "1",
        },
    )
    if (
        completed.returncode != 0
        or len(completed.stdout) > MAX_SOURCE_BODY_BYTES
        or len(completed.stderr) > MAX_SOURCE_BODY_BYTES
    ):
        raise ReleaseControlError("pinned GitHub CLI verification failed")
    return completed.stdout


def _verify_pinned_gh_release_observation(
    path: Path,
    *,
    repository: str,
    tag: str,
    asset_names: Sequence[str],
) -> tuple[str, str]:
    """Validate the exact result of network verification performed by the pinned CLI."""

    raw = _read_regular(
        path,
        label="pinned GitHub CLI release verification observation",
        max_bytes=MAX_SOURCE_BODY_BYTES,
    )
    value = _object(
        strict_canonical_json(
            raw,
            label="pinned GitHub CLI release verification observation",
        ),
        label="pinned GitHub CLI release verification observation",
    )
    _require_exact_fields(
        value,
        frozenset(
            {
                "schema",
                "pinned_gh_digest",
                "pinned_gh_version",
                "repository",
                "tag",
                "results",
                "verified",
                "validation_status",
            }
        ),
        label="pinned GitHub CLI release verification observation",
    )
    platform_digest = PINNED_GH_BINARY_DIGESTS.get((sys.platform, platform.machine()))
    expected_results = [
        ("release:verify", "release-attestation.json"),
        *(
            (f"release:verify-asset:{name}", f"{name}.attestation.json")
            for name in sorted(asset_names)
        ),
    ]
    result_items = _array(
        value.get("results"),
        label="pinned GitHub CLI verification results",
    )
    if len(result_items) != len(expected_results):
        raise ReleaseControlError("pinned GitHub CLI verification result inventory mismatch")
    for raw_result, (expected_operation, expected_name) in zip(
        result_items, expected_results, strict=True
    ):
        result = _object(raw_result, label="pinned GitHub CLI verification result")
        _require_exact_fields(
            result,
            frozenset({"operation", "path", "sha256", "size_bytes"}),
            label="pinned GitHub CLI verification result",
        )
        name = _validate_string(
            result.get("path"), label="pinned GitHub CLI verification result path"
        )
        size = _safe_integer(
            result.get("size_bytes"),
            label="pinned GitHub CLI verification result size",
            positive=True,
        )
        if (
            result.get("operation") != expected_operation
            or name != expected_name
            or PurePosixPath(name).name != name
        ):
            raise ReleaseControlError("pinned GitHub CLI verification result identity mismatch")
        result_raw = _read_regular(
            path.parent / name,
            label="pinned GitHub CLI verification result",
            max_bytes=MAX_SOURCE_BODY_BYTES,
        )
        if len(result_raw) != size or _sha256(result_raw) != _digest(
            result.get("sha256"),
            label="pinned GitHub CLI verification result digest",
        ):
            raise ReleaseControlError("pinned GitHub CLI verification result digest mismatch")
        parse_external_json_bytes(
            result_raw,
            label="pinned GitHub CLI verification result JSON",
        )
    if (
        platform_digest is None
        or value.get("schema") != "kestrel.pinned_gh_release_verification.v1"
        or value.get("pinned_gh_digest") != platform_digest
        or value.get("pinned_gh_version") != PINNED_GH_VERSION_LINE.decode("ascii")
        or value.get("repository") != repository
        or value.get("tag") != tag
        or value.get("verified") is not True
        or value.get("validation_status") != "validated"
    ):
        raise ReleaseControlError("pinned GitHub CLI release verification observation mismatch")
    return platform_digest, _sha256(raw)


def _load_external_file(path: Path, *, label: str) -> tuple[JSONValue, bytes]:
    raw = _read_regular(path, label=label, max_bytes=MAX_SOURCE_BODY_BYTES)
    return parse_external_json_bytes(raw, label=label), raw


def _run_gh_json(arguments: Sequence[str], *, input_value: object | None = None) -> JSONValue:
    gh = _pinned_gh()
    token = os.environ.get("GH_TOKEN")
    if token is None or not token:
        raise ReleaseControlError("GitHub publication credential is unavailable")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [str(gh), *arguments],
        input=(None if input_value is None else canonical_external_json_bytes(input_value)),
        capture_output=True,
        check=False,
        env={
            "GH_TOKEN": token,
            "GH_PROMPT_DISABLED": "1",
            "NO_COLOR": "1",
        },
    )
    if completed.returncode != 0:
        raise ReleaseControlError("pinned GitHub CLI request failed")
    return parse_external_json_bytes(completed.stdout, label="GitHub CLI response")


def _release_items(value: object) -> list[JSONObject]:
    raw_items = _array(value, label="recovery Release listing")
    items: list[JSONObject] = []
    for item in raw_items:
        if type(item) is list:
            items.extend(_release_items(item))
        else:
            items.append(_object(item, label="recovery Release"))
    return items


def _verify_remote_capsule_assets(
    value: object, *, expected: Mapping[str, tuple[int, str]]
) -> list[JSONObject]:
    assets = _array(value, label="recovery Release assets")
    normalized: list[JSONObject] = []
    seen_names: set[str] = set()
    seen_ids: set[int] = set()
    for raw_asset in assets:
        asset = _object(raw_asset, label="recovery Release asset")
        name = _validate_string(asset.get("name"), label="recovery Release asset name")
        asset_id = _safe_integer(asset.get("id"), label="recovery Release asset ID", positive=True)
        size = _safe_integer(asset.get("size"), label="recovery Release asset size", positive=True)
        digest = _digest(asset.get("digest"), label="recovery Release asset digest")
        if name in seen_names or asset_id in seen_ids:
            raise ReleaseControlError("recovery Release assets are duplicated")
        if expected.get(name) != (size, digest):
            raise ReleaseControlError("recovery Release asset identity mismatch")
        seen_names.add(name)
        seen_ids.add(asset_id)
        normalized.append({"id": asset_id, "name": name, "size_bytes": size, "sha256": digest})
    if seen_names != set(expected):
        raise ReleaseControlError("recovery Release asset inventory mismatch")
    normalized.sort(key=lambda item: cast(str, item["name"]))
    return normalized


def publish_recovery_capsule(
    *,
    capsule_root: Path,
    repository: str,
    tag: str,
    expected_repository_id: int,
    output: Path,
    mutation_guard: Callable[[], None] | None = None,
) -> int:
    """Publish one exact capsule while reauthorizing every remote mutation."""

    return _command_publish_recovery_capsule(
        argparse.Namespace(
            capsule_root=str(capsule_root),
            repository=repository,
            tag=tag,
            expected_repository_id=expected_repository_id,
            output=str(output),
        ),
        mutation_guard=mutation_guard,
    )


def _command_publish_recovery_capsule(
    args: argparse.Namespace,
    *,
    mutation_guard: Callable[[], None] | None = None,
) -> int:
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise ReleaseControlError("recovery publication output path must be empty")
    root = Path(args.capsule_root)
    manifest, manifest_raw = verify_recovery_capsule_root(root)
    pinned_gh = _pinned_gh()
    _verify_pinned_gh(pinned_gh)
    if args.repository != "John-MiracleWorker/Kestrel-Release-Recovery":
        raise ReleaseControlError("recovery publication repository mismatch")
    repository = _object(
        _run_gh_json(["api", f"repos/{args.repository}"]),
        label="recovery repository",
    )
    if (
        repository.get("id") != args.expected_repository_id
        or repository.get("full_name") != args.repository
    ):
        raise ReleaseControlError("recovery publication repository identity mismatch")
    release_manifest = _object(manifest["release"], label="capsule Release")
    if release_manifest.get("tag") != args.tag:
        raise ReleaseControlError("recovery publication tag mismatch")
    archive = deterministic_recovery_capsule_archive(root)
    bootstrap = _read_regular(
        root / "scripts" / "bootstrap_recovery.py",
        label="recovery bootstrap release asset",
        max_bytes=MAX_SOURCE_BODY_BYTES,
    )
    expected_assets = {
        "recovery-bootstrap.py": bootstrap,
        "recovery-capsule-manifest.json": manifest_raw,
        "recovery-capsule.tar": archive,
    }
    name = f"Kestrel recovery capsule {args.tag}"
    body = (
        f"Kestrel recovery capsule {args.tag}\n\nKestrel-Recovery-Capsule: {_sha256(manifest_raw)}"
    )

    def list_matching() -> list[JSONObject]:
        listed = _run_gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{args.repository}/releases?per_page=100",
            ]
        )
        return [item for item in _release_items(listed) if item.get("tag_name") == args.tag]

    expected_asset_identities = {
        asset_name: (len(raw), _sha256(raw)) for asset_name, raw in expected_assets.items()
    }

    def observe_exact(
        *, expected_release_id: int | None = None
    ) -> tuple[JSONObject, dict[str, JSONObject]] | None:
        matching = list_matching()
        if len(matching) > 1:
            raise ReleaseControlError("recovery publication Release is ambiguous")
        if not matching:
            return None
        release = matching[0]
        release_id = _safe_integer(release.get("id"), label="recovery Release ID", positive=True)
        if expected_release_id is not None and release_id != expected_release_id:
            raise ReleaseControlError("recovery publication Release ID changed")
        if (
            release.get("tag_name") != args.tag
            or release.get("name") != name
            or release.get("body") != body
            or type(release.get("draft")) is not bool
            or release.get("prerelease") is not False
            or type(release.get("immutable")) is not bool
            or release.get("draft") is release.get("immutable")
        ):
            raise ReleaseControlError("recovery publication Release identity conflicts")
        observed_assets: dict[str, JSONObject] = {}
        observed_ids: set[int] = set()
        for raw_asset in _array(release.get("assets"), label="recovery Release assets"):
            asset = _object(raw_asset, label="recovery Release asset")
            asset_name = _validate_string(asset.get("name"), label="recovery Release asset name")
            asset_id = _safe_integer(
                asset.get("id"), label="recovery Release asset ID", positive=True
            )
            if asset_name in observed_assets or asset_id in observed_ids:
                raise ReleaseControlError("recovery publication assets are duplicated")
            expected = expected_asset_identities.get(asset_name)
            if expected is None:
                raise ReleaseControlError("recovery publication has an unexpected asset")
            size = _safe_integer(
                asset.get("size"), label="recovery Release asset size", positive=True
            )
            digest = _digest(asset.get("digest"), label="recovery Release asset digest")
            if (size, digest) != expected:
                raise ReleaseControlError("recovery publication asset identity conflicts")
            observed_assets[asset_name] = asset
            observed_ids.add(asset_id)
        if release.get("draft") is False and set(observed_assets) != set(expected_assets):
            raise ReleaseControlError("published recovery publication is missing an asset")
        return release, observed_assets

    observed_release = observe_exact()
    if observed_release is None:
        if mutation_guard is not None:
            mutation_guard()
        observed_release = observe_exact()
        if observed_release is None:
            created = _object(
                _run_gh_json(
                    [
                        "api",
                        "--method",
                        "POST",
                        f"repos/{args.repository}/releases",
                        "--input",
                        "-",
                    ],
                    input_value={
                        "tag_name": args.tag,
                        "name": name,
                        "body": body,
                        "draft": True,
                        "prerelease": False,
                        "generate_release_notes": False,
                        "make_latest": "false",
                    },
                ),
                label="created recovery Release",
            )
            created_id = _safe_integer(
                created.get("id"), label="created recovery Release ID", positive=True
            )
            observed_release = observe_exact(expected_release_id=created_id)
            if observed_release is None:
                raise ReleaseControlError(
                    "recovery publication create response is not uniquely observable"
                )
    release, observed_assets = observed_release
    release_id = _safe_integer(release.get("id"), label="recovery Release ID", positive=True)
    with tempfile.TemporaryDirectory(prefix="kestrel-recovery-publish-") as temporary:
        temporary_root = Path(temporary)
        for asset_name, raw in expected_assets.items():
            observed_release = observe_exact(expected_release_id=release_id)
            if observed_release is None:
                raise ReleaseControlError("recovery publication Release disappeared")
            release, observed_assets = observed_release
            observed = observed_assets.get(asset_name)
            if observed is not None:
                continue
            if release.get("draft") is not True:
                raise ReleaseControlError("published recovery publication is missing an asset")
            path = temporary_root / asset_name
            _write_secure_temporary(path, raw, mode=0o600)
            if mutation_guard is not None:
                mutation_guard()
            observed_release = observe_exact(expected_release_id=release_id)
            if observed_release is None:
                raise ReleaseControlError("recovery publication Release disappeared")
            release, observed_assets = observed_release
            if asset_name in observed_assets:
                continue
            if release.get("draft") is not True:
                raise ReleaseControlError("published recovery publication is missing an asset")
            completed = subprocess.run(  # noqa: S603  # nosec B603
                [
                    str(pinned_gh),
                    "release",
                    "upload",
                    args.tag,
                    str(path),
                    "--repo",
                    args.repository,
                ],
                capture_output=True,
                check=False,
                env={
                    "GH_TOKEN": os.environ.get("GH_TOKEN", ""),
                    "GH_PROMPT_DISABLED": "1",
                    "NO_COLOR": "1",
                },
            )
            if completed.returncode != 0:
                raise ReleaseControlError("recovery capsule asset upload failed")
            observed_release = observe_exact(expected_release_id=release_id)
            if observed_release is None or asset_name not in observed_release[1]:
                raise ReleaseControlError("recovery capsule asset upload was not observed exactly")
    observed_release = observe_exact(expected_release_id=release_id)
    if observed_release is None:
        raise ReleaseControlError("recovery publication Release disappeared")
    release, observed_assets = observed_release
    if set(observed_assets) != set(expected_assets):
        raise ReleaseControlError("recovery publication asset inventory is incomplete")
    if release.get("draft") is True:
        if mutation_guard is not None:
            mutation_guard()
        observed_release = observe_exact(expected_release_id=release_id)
        if observed_release is None:
            raise ReleaseControlError("recovery publication Release disappeared")
        release, observed_assets = observed_release
        if set(observed_assets) != set(expected_assets):
            raise ReleaseControlError(
                "recovery publication asset inventory changed before publication"
            )
        if release.get("draft") is True:
            _run_gh_json(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"repos/{args.repository}/releases/{release_id}",
                    "--input",
                    "-",
                ],
                input_value={"draft": False, "make_latest": "false"},
            )
    observed_release = observe_exact(expected_release_id=release_id)
    if observed_release is None:
        raise ReleaseControlError("recovery capsule Release is not uniquely immutable")
    final_release, _final_assets = observed_release
    if (
        final_release.get("id") != release_id
        or final_release.get("tag_name") != args.tag
        or final_release.get("name") != name
        or final_release.get("body") != body
        or final_release.get("draft") is not False
        or final_release.get("prerelease") is not False
        or final_release.get("immutable") is not True
    ):
        raise ReleaseControlError("recovery capsule Release is not uniquely immutable")
    _verify_remote_capsule_assets(
        final_release.get("assets"),
        expected={
            asset_name: (len(raw), _sha256(raw)) for asset_name, raw in expected_assets.items()
        },
    )
    receipt: JSONObject = {
        "schema": "kestrel.recovery_capsule_publication.v1",
        "repository": args.repository,
        "repository_id": args.expected_repository_id,
        "tag": args.tag,
        "release_id": final_release["id"],
        "manifest_digest": _sha256(manifest_raw),
        "archive_digest": _sha256(archive),
        "immutable": True,
        "validation_status": "validated",
    }
    _write_cli_output(output, canonical_json_bytes(receipt))
    return 0


def _command_verify_recovery_capsule_release(args: argparse.Namespace) -> int:
    output = Path(args.output)
    root = Path(args.capsule_root)
    manifest, manifest_raw = verify_recovery_capsule_root(root)
    publication = _object(
        _load_canonical_file(
            Path(args.publication_receipt),
            label="recovery publication receipt",
            max_bytes=MAX_SOURCE_BODY_BYTES,
        ),
        label="recovery publication receipt",
    )
    _require_exact_fields(
        publication,
        frozenset(
            {
                "schema",
                "repository",
                "repository_id",
                "tag",
                "release_id",
                "manifest_digest",
                "archive_digest",
                "immutable",
                "validation_status",
            }
        ),
        label="recovery publication receipt",
    )
    if (
        publication.get("schema") != "kestrel.recovery_capsule_publication.v1"
        or publication.get("immutable") is not True
        or publication.get("validation_status") != "validated"
    ):
        raise ReleaseControlError("recovery publication receipt is invalid")
    repository_value, repository_raw = _load_external_file(
        Path(args.fresh_repository_observation),
        label="fresh recovery repository observation",
    )
    repository = _object(
        repository_value,
        label="fresh recovery repository observation",
    )
    release_value, release_raw = _load_external_file(
        Path(args.fresh_release_observation),
        label="fresh recovery Release observation",
    )
    release = _object(
        release_value,
        label="fresh recovery Release observation",
    )
    assets_value, assets_raw = _load_external_file(
        Path(args.fresh_assets_observation),
        label="fresh recovery asset observation",
    )
    archive = deterministic_recovery_capsule_archive(root)
    bootstrap = _read_regular(
        root / "scripts" / "bootstrap_recovery.py",
        label="recovery bootstrap release asset",
        max_bytes=MAX_SOURCE_BODY_BYTES,
    )
    expected_assets = {
        "recovery-bootstrap.py": (len(bootstrap), _sha256(bootstrap)),
        "recovery-capsule-manifest.json": (len(manifest_raw), _sha256(manifest_raw)),
        "recovery-capsule.tar": (len(archive), _sha256(archive)),
    }
    normalized_assets = _verify_remote_capsule_assets(assets_value, expected=expected_assets)
    manifest_release = _object(manifest["release"], label="capsule Release")
    tag = _validate_string(manifest_release.get("tag"), label="capsule Release tag")
    manifest_digest = _sha256(manifest_raw)
    expected_name = f"Kestrel recovery capsule {tag}"
    expected_body = f"Kestrel recovery capsule {tag}\n\nKestrel-Recovery-Capsule: {manifest_digest}"
    if (
        repository.get("full_name") != publication.get("repository")
        or repository.get("id") != publication.get("repository_id")
        or repository.get("private") is not True
        or release.get("id") != publication.get("release_id")
        or release.get("tag_name") != tag
        or release.get("name") != expected_name
        or release.get("body") != expected_body
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or release.get("immutable") is not True
        or publication.get("tag") != tag
        or publication.get("manifest_digest") != manifest_digest
        or publication.get("archive_digest") != _sha256(archive)
    ):
        raise ReleaseControlError("recovery capsule Release verification mismatch")
    repository_name = cast(str, repository["full_name"])
    observation_digest: str | None = None
    if args.pinned_gh is not None:
        pinned_gh = Path(args.pinned_gh)
        pinned_gh_digest = _verify_pinned_gh(pinned_gh)
        _run_pinned_gh_verification(
            pinned_gh,
            ["release", "verify", tag, "--repo", repository_name],
        )
        for asset_name in sorted(expected_assets):
            _run_pinned_gh_verification(
                pinned_gh,
                [
                    "release",
                    "verify-asset",
                    tag,
                    asset_name,
                    "--repo",
                    repository_name,
                ],
            )
    else:
        pinned_gh_digest, observation_digest = _verify_pinned_gh_release_observation(
            Path(args.pinned_gh_verification_observation),
            repository=repository_name,
            tag=tag,
            asset_names=tuple(expected_assets),
        )
    verification: JSONObject = {
        "schema": "kestrel.recovery_capsule_release_verification.v1",
        "repository": repository["full_name"],
        "repository_id": repository["id"],
        "release_id": release["id"],
        "tag": release["tag_name"],
        "capsule_manifest_digest": manifest_digest,
        "archive_digest": _sha256(archive),
        "pinned_gh_digest": pinned_gh_digest,
        "publication_receipt_digest": _sha256(canonical_json_bytes(publication)),
        "repository_observation_digest": _sha256(repository_raw),
        "release_observation_digest": _sha256(release_raw),
        "assets_observation_digest": _sha256(assets_raw),
        "assets": cast(list[JSONValue], normalized_assets),
        "verified": True,
        "validation_status": "validated",
    }
    if observation_digest is not None:
        verification["pinned_gh_verification_observation_digest"] = observation_digest
    _write_cli_output(output, canonical_json_bytes(verification))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = _ReleaseControlArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture-source")
    capture.add_argument("--registry", required=True)
    capture.add_argument("--receipt-schema", required=True)
    capture.add_argument("--phase", required=True)
    capture.add_argument("--mode", required=True)
    capture.add_argument("--name", required=True)
    capture.add_argument("--raw-input", required=True)
    capture.add_argument("--identity-observation")
    capture.add_argument("--output", required=True)
    capture.set_defaults(handler=_command_capture_source)

    canonicalize = commands.add_parser("canonicalize")
    canonicalize.add_argument("input")
    canonicalize.add_argument("--output", required=True)
    canonicalize.set_defaults(handler=_command_canonicalize)

    scope = commands.add_parser("create-credential-scope-authority")
    scope.add_argument("--purpose", required=True, choices=sorted(CREDENTIAL_PURPOSES))
    scope.add_argument("--credential-id", required=True)
    scope.add_argument("--principal-observation", required=True)
    scope.add_argument("--grants-snapshot", required=True)
    scope.add_argument("--token-fingerprint", required=True)
    scope.add_argument("--controller-context", required=True)
    scope.add_argument("--output", required=True)
    scope.set_defaults(handler=_command_create_credential_scope_authority)

    identity = commands.add_parser("create-dispatch-identity")
    identity.add_argument("--github-context-allowlist", required=True)
    identity.add_argument("--output", required=True)
    identity.set_defaults(handler=_command_create_dispatch_identity)

    sign = commands.add_parser("sign")
    sign.add_argument("receipt")
    sign.add_argument("--identity-file", required=True)
    sign.add_argument("--principal", required=True)
    sign.add_argument("--namespace", required=True)
    sign.add_argument("--output-signature", required=True)
    sign.set_defaults(handler=_command_sign)

    runtime = commands.add_parser("verify-runtime-credential")
    runtime.add_argument("--scope-authority", required=True)
    runtime.add_argument("--scope-authority-signature", required=True)
    runtime.add_argument("--owner-signing-keys-observation", required=True)
    runtime.add_argument("--identity-probe-observation", required=True)
    runtime.add_argument("--endpoint-probe-observations", required=True)
    runtime.add_argument("--output", required=True)
    runtime.set_defaults(handler=_command_verify_runtime_credential)

    create_recovery = commands.add_parser("create-recovery-repository-authority")
    create_recovery.add_argument("--owner-authority-snapshot", required=True)
    create_recovery.add_argument("--repository-observation", required=True)
    create_recovery.add_argument("--immutable-releases-observation", required=True)
    create_recovery.add_argument("--controller-context", required=True)
    create_recovery.add_argument("--output", required=True)
    create_recovery.set_defaults(handler=_command_create_recovery_authority)

    create_pypi = commands.add_parser("create-pypi-authority")
    create_pypi.add_argument("--public-project-observation", required=True)
    create_pypi.add_argument("--owner-authority-snapshot", required=True)
    create_pypi.add_argument("--promotion-run-observation", required=True)
    create_pypi.add_argument("--promotion-dispatch-identity", required=True)
    create_pypi.add_argument("--candidate-manifest", required=True)
    create_pypi.add_argument("--environment-observation", required=True)
    create_pypi.add_argument("--controller-context", required=True)
    create_pypi.add_argument("--bindings", required=True)
    create_pypi.add_argument("--output", required=True)
    create_pypi.set_defaults(handler=_command_create_pypi_authority)

    create_github = commands.add_parser("create-github-authority")
    for argument in (
        "repository-observation",
        "promotion-run-observation",
        "promotion-dispatch-identity",
        "candidate-manifest",
        "environment-observation",
        "environment-policy-types-snapshot",
        "rulesets-observation",
        "tag-ruleset-detail-observation",
        "ingress-ruleset-detail-observation",
        "workflow-observation",
        "default-branch-workflow-contents",
        "candidate-workflow-contents",
        "dispatch-intent",
        "dispatch-intent-signature",
        "dispatch-request",
        "dispatch-outcome",
        "installed-apps-snapshot",
        "ghcr-package-access-snapshot",
        "dispatcher-invalidation-snapshot",
        "controller-context",
        "bindings",
    ):
        create_github.add_argument(f"--{argument}", required=True)
    create_github.add_argument("--output", required=True)
    create_github.set_defaults(handler=_command_create_github_authority)

    github = commands.add_parser("verify-github-authority")
    github.add_argument("receipt")
    github.add_argument("--signature", required=True)
    github.add_argument("--owner-signing-keys-observation", required=True)
    github.add_argument("--expected-run-id", required=True, type=int)
    github.add_argument("--expected-candidate-digest", required=True)
    github.add_argument("--expected-environment-id", required=True, type=int)
    github.add_argument("--output", required=True)
    github.set_defaults(handler=_command_verify_github_authority)

    pypi = commands.add_parser("verify-pypi-authority")
    pypi.add_argument("receipt")
    pypi.add_argument("--signature", required=True)
    pypi.add_argument("--owner-signing-keys-observation", required=True)
    pypi.add_argument("--expected-run-id", required=True, type=int)
    pypi.add_argument("--expected-candidate-digest", required=True)
    pypi.add_argument("--expected-environment-id", required=True, type=int)
    pypi.add_argument("--output", required=True)
    pypi.set_defaults(handler=_command_verify_pypi_authority)

    recovery = commands.add_parser("verify-recovery-repository-authority")
    recovery.add_argument("receipt")
    recovery.add_argument("--signature", required=True)
    recovery.add_argument("--owner-signing-keys-observation", required=True)
    recovery.add_argument("--expected-repository", required=True)
    recovery.add_argument("--expected-repository-id", required=True, type=int)
    recovery.add_argument("--output", required=True)
    recovery.set_defaults(handler=_command_verify_recovery_authority)

    create_capsule = commands.add_parser("create-recovery-capsule")
    for argument in (
        "candidate-archive",
        "transaction-authorization",
        "admission-receipt",
        "admission-signature",
        "admission-verification",
        "owner-key-observation",
        "normalized-evidence-root",
        "schema-root",
        "source-root",
        "dependency-root",
        "gitleaks-image",
        "gitleaks-ignore",
        "recovery-authority-receipt",
        "recovery-authority-signature",
        "recovery-repository-observation",
        "execution-closure",
        "environment-manifest",
        "output-root",
    ):
        create_capsule.add_argument(f"--{argument}", required=True)
    create_capsule.set_defaults(handler=_command_create_recovery_capsule)

    publish_capsule = commands.add_parser("publish-recovery-capsule")
    publish_capsule.add_argument("capsule_root")
    publish_capsule.add_argument("--repository", required=True)
    publish_capsule.add_argument("--tag", required=True)
    publish_capsule.add_argument("--expected-repository-id", required=True, type=int)
    publish_capsule.add_argument("--output", required=True)
    publish_capsule.set_defaults(handler=_command_publish_recovery_capsule)

    verify_capsule_release = commands.add_parser("verify-recovery-capsule-release")
    verify_capsule_release.add_argument("capsule_root")
    verify_capsule_release.add_argument("--publication-receipt", required=True)
    verify_capsule_release.add_argument("--fresh-repository-observation", required=True)
    verify_capsule_release.add_argument("--fresh-release-observation", required=True)
    verify_capsule_release.add_argument("--fresh-assets-observation", required=True)
    gh_verification = verify_capsule_release.add_mutually_exclusive_group(required=True)
    gh_verification.add_argument("--pinned-gh")
    gh_verification.add_argument("--pinned-gh-verification-observation")
    verify_capsule_release.add_argument("--output", required=True)
    verify_capsule_release.set_defaults(handler=_command_verify_recovery_capsule_release)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    _clock: Callable[[], datetime] | None = None,
) -> int:
    try:
        args = _parser().parse_args(argv)
        if _clock is not None:
            if args.handler is not _command_create_recovery_capsule:
                raise ReleaseControlError(
                    "an explicit verification clock is limited to recovery capsule creation"
                )
            return cast(int, _command_create_recovery_capsule(args, _clock=_clock))
        return cast(int, args.handler(args))
    except (ReleaseControlError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
