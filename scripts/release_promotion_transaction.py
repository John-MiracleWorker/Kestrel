#!/usr/bin/env python3
"""Fail-closed release-promotion transport and remote-state transaction logic.

Canonical receipt, schema, and signature policy remains in
``release_control_receipt.py``. This module owns the stateful boundary where a
prepared dispatch can cause at most one wire transmission.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import http.client
import importlib.metadata
import io
import math
import os
import platform
import re
import secrets
import ssl
import subprocess  # nosec B404
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from urllib.parse import quote, urlsplit

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from scripts import release_candidate_manifest as candidates  # noqa: E402
from scripts import release_control_receipt as receipts  # noqa: E402

MAX_TRANSPORT_RESPONSE_BYTES = 1024 * 1024
MAX_DISPATCH_TOKEN_BYTES = 4096
DISPATCH_STATE_ROOT = Path.home() / ".kestrel" / "release-control" / "dispatches"
_WORKFLOW_TOOL_ARCHIVE_DIGESTS = {
    (
        "linux",
        "x86_64",
    ): "sha256:a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112",
    (
        "darwin",
        "arm64",
    ): "sha256:a58b8fd77b417a38f47a0b54d1370c59b0fcdb324ccc9ca002b0998f7c4c999e",
}


def _workflow_tools_archive_digest(*, system: str | None = None, machine: str | None = None) -> str:
    """Return the pinned GitHub CLI archive digest for an allowed platform."""

    platform_key = (
        sys.platform if system is None else system,
        platform.machine() if machine is None else machine,
    )
    try:
        return _WORKFLOW_TOOL_ARCHIVE_DIGESTS[platform_key]
    except KeyError as exc:
        raise receipts.ReleaseControlError(
            "workflow tool bootstrap platform is unsupported"
        ) from exc


@dataclass(frozen=True)
class OneWirePolicy:
    """Transport settings that prohibit replay of the dispatch body."""

    maximum_transmissions: int = 1
    redirects: bool = False
    retries: bool = False
    auth_replay: bool = False
    proxies: bool = False
    failover: bool = False


@dataclass(frozen=True)
class DispatchExchange:
    """Exact result of the sole transport invocation."""

    http_status: int | None
    response_headers: bytes | None
    response_body: bytes | None
    request_may_have_reached_peer: bool


@dataclass(frozen=True)
class TerminalReleaseAsset:
    """Normalized immutable-channel asset identity."""

    asset_id: int
    name: str
    size_bytes: int
    digest: str
    media_type: str


@dataclass(frozen=True)
class TerminalRelease:
    """Normalized recovery-channel Release state used at mutation boundaries."""

    release_id: int
    tag_name: str
    name: str
    body: str
    draft: bool
    prerelease: bool
    immutable: bool
    html_url: str
    assets: tuple[TerminalReleaseAsset, ...]


@dataclass(frozen=True)
class TerminalReleaseListing:
    """One exhaustive recovery-channel Release listing."""

    releases: tuple[TerminalRelease, ...]
    complete: bool


@dataclass(frozen=True)
class TerminalPublicationClaim:
    """One remotely atomic per-nonce admission/tombstone winner."""

    transaction_nonce: str
    kind: str
    record_digest: str
    ref_name: str
    tag_object_sha: str
    target_commit_sha: str


class TerminalReleaseAPI(Protocol):
    """Mutation surface for one immutable dispatch terminal Release."""

    def list_releases(self, repository: str) -> TerminalReleaseListing: ...

    def claim_terminal_kind(
        self,
        repository: str,
        *,
        transaction_nonce: str,
        kind: str,
        record_digest: str,
    ) -> TerminalPublicationClaim: ...

    def create_draft(
        self,
        repository: str,
        *,
        tag_name: str,
        name: str,
        body: str,
    ) -> int: ...

    def upload_asset(
        self,
        repository: str,
        *,
        release_id: int,
        name: str,
        media_type: str,
        content: bytes,
    ) -> None: ...

    def publish_immutable(self, repository: str, *, release_id: int) -> None: ...


CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class GitHubTerminalReleaseAPI:
    """Pinned-GitHub-CLI adapter for the immutable dispatch terminal channel."""

    def __init__(
        self,
        *,
        pinned_gh: Path,
        token: bytes,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        if (
            not pinned_gh.is_absolute()
            or not pinned_gh.is_file()
            or pinned_gh.is_symlink()
            or not os.access(pinned_gh, os.X_OK)
        ):
            raise receipts.ReleaseControlError("dispatch terminal GitHub CLI path is invalid")
        if (
            type(token) is not bytes
            or not token
            or len(token) > MAX_DISPATCH_TOKEN_BYTES
            or any(byte < 0x21 or byte > 0x7E for byte in token)
        ):
            raise receipts.ReleaseControlError("dispatch terminal credential bytes are invalid")
        receipts._verify_pinned_gh(pinned_gh)  # noqa: SLF001
        self._gh = pinned_gh
        self._token = token.decode("ascii")
        self._token_fingerprint = receipts._sha256(token)  # noqa: SLF001
        self._runner = runner

    @property
    def token_fingerprint(self) -> str:
        """Return the only credential-derived value safe for evidence."""

        return self._token_fingerprint

    def _run(self, command: list[str], *, body: bytes | None = None) -> bytes:
        completed = self._runner(  # noqa: S603  # nosec B603
            command,
            input=body,
            capture_output=True,
            check=False,
            timeout=30,
            env={
                "GH_TOKEN": self._token,
                "GH_PROMPT_DISABLED": "1",
                "NO_COLOR": "1",
            },
        )
        if completed.returncode != 0:
            raise receipts.ReleaseControlError("dispatch terminal GitHub CLI request failed")
        if len(completed.stdout) > receipts.MAX_SOURCE_BODY_BYTES:
            raise receipts.ReleaseControlError("dispatch terminal GitHub CLI response is too large")
        return completed.stdout

    def _api(
        self,
        *,
        method: str,
        endpoint: str,
        body: bytes | None = None,
        paginate: bool = False,
    ) -> receipts.JSONValue:
        command = [
            str(self._gh),
            "api",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {receipts.DISPATCH_API_VERSION}",
        ]
        if paginate:
            command.extend(("--paginate", "--slurp"))
        if body is not None:
            command.extend(("--input", "-"))
        command.append(endpoint)
        raw = self._run(command, body=body)
        return receipts.parse_external_json_bytes(raw, label="dispatch terminal GitHub response")

    @staticmethod
    def _repository(repository: str) -> str:
        if repository != "John-MiracleWorker/Kestrel-Release-Recovery":
            raise receipts.ReleaseControlError("dispatch terminal repository identity mismatch")
        return repository

    @staticmethod
    def _external_string(value: object, *, label: str, allow_empty: bool = False) -> str:
        if type(value) is not str or (not value and not allow_empty):
            raise receipts.ReleaseControlError(f"{label} is invalid")
        return value

    @classmethod
    def _flatten_releases(cls, value: object) -> list[receipts.JSONObject]:
        values = receipts._array(value, label="dispatch terminal Releases")  # noqa: SLF001
        releases: list[receipts.JSONObject] = []
        for item in values:
            if type(item) is list:
                releases.extend(cls._flatten_releases(item))
            else:
                releases.append(
                    receipts._object(item, label="dispatch terminal Release")  # noqa: SLF001
                )
        return releases

    @classmethod
    def _normalize_release(cls, value: receipts.JSONObject) -> TerminalRelease:
        assets: list[TerminalReleaseAsset] = []
        for raw_asset in receipts._array(  # noqa: SLF001
            value.get("assets"), label="dispatch terminal Release assets"
        ):
            asset = receipts._object(  # noqa: SLF001
                raw_asset, label="dispatch terminal Release asset"
            )
            assets.append(
                TerminalReleaseAsset(
                    asset_id=receipts._safe_integer(  # noqa: SLF001
                        asset.get("id"),
                        label="dispatch terminal Release asset ID",
                        positive=True,
                    ),
                    name=receipts._validate_string(  # noqa: SLF001
                        asset.get("name"),
                        label="dispatch terminal Release asset name",
                    ),
                    size_bytes=receipts._safe_integer(  # noqa: SLF001
                        asset.get("size"),
                        label="dispatch terminal Release asset size",
                        positive=True,
                    ),
                    digest=receipts._digest(  # noqa: SLF001
                        asset.get("digest"),
                        label="dispatch terminal Release asset digest",
                    ),
                    media_type=receipts._validate_string(  # noqa: SLF001
                        asset.get("content_type"),
                        label="dispatch terminal Release asset media type",
                    ),
                )
            )
        for field in ("draft", "prerelease", "immutable"):
            if type(value.get(field)) is not bool:
                raise receipts.ReleaseControlError(f"dispatch terminal Release {field} is invalid")
        return TerminalRelease(
            release_id=receipts._safe_integer(  # noqa: SLF001
                value.get("id"), label="dispatch terminal Release ID", positive=True
            ),
            tag_name=receipts._validate_string(  # noqa: SLF001
                value.get("tag_name"), label="dispatch terminal Release tag"
            ),
            name=receipts._validate_string(  # noqa: SLF001
                value.get("name"), label="dispatch terminal Release name"
            ),
            body=cls._external_string(
                value.get("body"),
                label="dispatch terminal Release body",
                allow_empty=True,
            ),
            draft=cast(bool, value["draft"]),
            prerelease=cast(bool, value["prerelease"]),
            immutable=cast(bool, value["immutable"]),
            html_url=receipts._validate_string(  # noqa: SLF001
                value.get("html_url"), label="dispatch terminal Release URL"
            ),
            assets=tuple(sorted(assets, key=lambda item: item.name)),
        )

    def list_releases(self, repository: str) -> TerminalReleaseListing:
        checked = self._repository(repository)
        value = self._api(
            method="GET",
            endpoint=f"repos/{checked}/releases?per_page=100",
            paginate=True,
        )
        releases = [self._normalize_release(item) for item in self._flatten_releases(value)]
        releases.sort(key=lambda item: item.release_id)
        return TerminalReleaseListing(tuple(releases), complete=True)

    @staticmethod
    def _git_object(value: object, *, label: str) -> tuple[str, str]:
        checked = receipts._object(value, label=label)  # noqa: SLF001
        object_value = receipts._object(  # noqa: SLF001
            checked.get("object"), label=f"{label} object"
        )
        object_type = receipts._validate_string(  # noqa: SLF001
            object_value.get("type"), label=f"{label} object type"
        )
        object_sha = receipts._git_sha(  # noqa: SLF001
            object_value.get("sha"), label=f"{label} object SHA"
        )
        return object_type, object_sha

    def claim_terminal_kind(
        self,
        repository: str,
        *,
        transaction_nonce: str,
        kind: str,
        record_digest: str,
    ) -> TerminalPublicationClaim:
        """Atomically select one terminal kind using a shared Git tag ref."""

        checked_repository = self._repository(repository)
        nonce = receipts._nonce(transaction_nonce)  # noqa: SLF001
        if kind not in _TERMINAL_RECORD_SCHEMAS:
            raise receipts.ReleaseControlError("dispatch terminal claim kind is invalid")
        digest = receipts._digest(  # noqa: SLF001
            record_digest, label="dispatch terminal claim record digest"
        )
        claim_record: receipts.JSONObject = {
            "schema": "kestrel.dispatch_terminal_remote_claim.v1",
            "transaction_nonce": nonce,
            "kind": kind,
            "record_digest": digest,
        }
        claim_message = receipts.canonical_json_bytes(claim_record).decode("ascii")
        claim_tag = f"dispatch-terminal-claim-{nonce}"
        claim_ref = f"refs/tags/{claim_tag}"
        head = receipts._object(  # noqa: SLF001
            self._api(
                method="GET",
                endpoint=f"repos/{checked_repository}/git/ref/heads/main",
            ),
            label="dispatch terminal recovery main ref",
        )
        if head.get("ref") != "refs/heads/main":
            raise receipts.ReleaseControlError("dispatch terminal recovery main ref mismatch")
        head_type, target_commit = self._git_object(
            head, label="dispatch terminal recovery main ref"
        )
        if head_type != "commit":
            raise receipts.ReleaseControlError(
                "dispatch terminal recovery main ref is not a commit"
            )
        tag_response = receipts._object(  # noqa: SLF001
            self._api(
                method="POST",
                endpoint=f"repos/{checked_repository}/git/tags",
                body=receipts.canonical_external_json_bytes(
                    {
                        "tag": claim_tag,
                        "message": claim_message,
                        "object": target_commit,
                        "type": "commit",
                    }
                ),
            ),
            label="dispatch terminal claim tag object",
        )
        tag_object_sha = receipts._git_sha(  # noqa: SLF001
            tag_response.get("sha"), label="dispatch terminal claim tag object SHA"
        )
        created_ref = True
        try:
            self._api(
                method="POST",
                endpoint=f"repos/{checked_repository}/git/refs",
                body=receipts.canonical_external_json_bytes(
                    {"ref": claim_ref, "sha": tag_object_sha}
                ),
            )
        except receipts.ReleaseControlError:
            created_ref = False

        selected_ref = receipts._object(  # noqa: SLF001
            self._api(
                method="GET",
                endpoint=f"repos/{checked_repository}/git/ref/tags/{claim_tag}",
            ),
            label="dispatch terminal selected claim ref",
        )
        if selected_ref.get("ref") != claim_ref:
            raise receipts.ReleaseControlError("dispatch terminal selected claim ref mismatch")
        selected_type, selected_tag_sha = self._git_object(
            selected_ref, label="dispatch terminal selected claim ref"
        )
        if selected_type != "tag":
            raise receipts.ReleaseControlError(
                "dispatch terminal selected claim ref is not annotated"
            )
        selected_tag = receipts._object(  # noqa: SLF001
            self._api(
                method="GET",
                endpoint=f"repos/{checked_repository}/git/tags/{selected_tag_sha}",
            ),
            label="dispatch terminal selected claim tag",
        )
        selected_target_type, selected_target_sha = self._git_object(
            selected_tag, label="dispatch terminal selected claim tag"
        )
        if (
            selected_tag.get("tag") != claim_tag
            or selected_tag.get("message") != claim_message
            or selected_target_type != "commit"
            or (created_ref and selected_target_sha != target_commit)
        ):
            raise receipts.ReleaseControlError(
                "dispatch terminal admission/tombstone remote claim conflict"
            )
        return TerminalPublicationClaim(
            transaction_nonce=nonce,
            kind=kind,
            record_digest=digest,
            ref_name=claim_ref,
            tag_object_sha=selected_tag_sha,
            target_commit_sha=selected_target_sha,
        )

    def create_draft(
        self,
        repository: str,
        *,
        tag_name: str,
        name: str,
        body: str,
    ) -> int:
        checked = self._repository(repository)
        request = {
            "tag_name": receipts._validate_string(  # noqa: SLF001
                tag_name, label="dispatch terminal tag"
            ),
            "name": receipts._validate_string(  # noqa: SLF001
                name, label="dispatch terminal name"
            ),
            "body": self._external_string(body, label="dispatch terminal body"),
            "draft": True,
            "prerelease": False,
            "generate_release_notes": False,
            "make_latest": "false",
        }
        response = receipts._object(  # noqa: SLF001
            self._api(
                method="POST",
                endpoint=f"repos/{checked}/releases",
                body=receipts.canonical_external_json_bytes(request),
            ),
            label="dispatch terminal created Release",
        )
        return receipts._safe_integer(  # noqa: SLF001
            response.get("id"), label="dispatch terminal created Release ID", positive=True
        )

    def upload_asset(
        self,
        repository: str,
        *,
        release_id: int,
        name: str,
        media_type: str,
        content: bytes,
    ) -> None:
        checked = self._repository(repository)
        checked_id = receipts._safe_integer(  # noqa: SLF001
            release_id, label="dispatch terminal Release ID", positive=True
        )
        checked_name = receipts._validate_string(  # noqa: SLF001
            name, label="dispatch terminal asset name"
        )
        receipts._validate_string(  # noqa: SLF001
            media_type, label="dispatch terminal asset media type"
        )
        if type(content) is not bytes or not content:
            raise receipts.ReleaseControlError("dispatch terminal asset content is invalid")
        endpoint = (
            "https://uploads.github.com/repos/"
            f"{checked}/releases/{checked_id}/assets?name={quote(checked_name, safe='')}"
        )
        command = [
            str(self._gh),
            "api",
            "--method",
            "POST",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {receipts.DISPATCH_API_VERSION}",
            "-H",
            f"Content-Type: {media_type}",
            "--input",
            "-",
            endpoint,
        ]
        raw = self._run(command, body=content)
        receipts.parse_external_json_bytes(raw, label="dispatch terminal upload response")

    def publish_immutable(self, repository: str, *, release_id: int) -> None:
        checked = self._repository(repository)
        checked_id = receipts._safe_integer(  # noqa: SLF001
            release_id, label="dispatch terminal Release ID", positive=True
        )
        self._api(
            method="PATCH",
            endpoint=f"repos/{checked}/releases/{checked_id}",
            body=receipts.canonical_external_json_bytes({"draft": False, "make_latest": "false"}),
        )


def _terminal_release_api_from_environment() -> GitHubTerminalReleaseAPI:
    gh_text = os.environ.get("KESTREL_PINNED_GH")
    token_text = os.environ.get("GH_TOKEN")
    if gh_text is None:
        raise receipts.ReleaseControlError("KESTREL_PINNED_GH is required for terminal publication")
    if token_text is None:
        raise receipts.ReleaseControlError("terminal publication credential is unavailable")
    try:
        token = token_text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise receipts.ReleaseControlError(
            "terminal publication credential bytes are invalid"
        ) from exc
    return GitHubTerminalReleaseAPI(pinned_gh=Path(gh_text), token=token)


class OneWireTransport(Protocol):
    def __call__(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        policy: OneWirePolicy,
    ) -> DispatchExchange: ...


class HTTPResponseLike(Protocol):
    status: int

    def getheaders(self) -> list[tuple[str, str]]: ...

    def read(self, amt: int | None = None) -> bytes: ...


class HTTPSConnectionLike(Protocol):
    def connect(self) -> None: ...

    def putrequest(
        self,
        method: str,
        url: str,
        skip_host: bool = False,
        skip_accept_encoding: bool = False,
    ) -> None: ...

    def putheader(self, header: str, *values: str) -> None: ...

    def endheaders(
        self, message_body: bytes | None = None, *, encode_chunked: bool = False
    ) -> None: ...

    def send(self, data: bytes) -> None: ...

    def getresponse(self) -> HTTPResponseLike: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[str, ssl.SSLContext, float], HTTPSConnectionLike]


class DispatchTransportError(RuntimeError):
    """A transport failure with an explicit possible-write boundary."""

    def __init__(self, message: str, *, request_may_have_reached_peer: bool) -> None:
        super().__init__(message)
        self.request_may_have_reached_peer = request_may_have_reached_peer


def _default_connection(host: str, context: ssl.SSLContext, timeout: float) -> HTTPSConnectionLike:
    connection = http.client.HTTPSConnection(
        host,
        port=443,
        timeout=timeout,
        context=context,
    )
    return cast(HTTPSConnectionLike, connection)


class PinnedGitHubTransport:
    """Direct HTTPS transport with no redirect, proxy, retry, or auth replay layer."""

    def __init__(
        self,
        *,
        token: bytes,
        timeout_seconds: float = 30.0,
        connection_factory: ConnectionFactory = _default_connection,
    ) -> None:
        if (
            type(token) is not bytes
            or not token
            or len(token) > MAX_DISPATCH_TOKEN_BYTES
            or any(byte < 0x21 or byte > 0x7E for byte in token)
        ):
            raise receipts.ReleaseControlError("dispatch credential bytes are invalid")
        if type(timeout_seconds) is not float or not 0.0 < timeout_seconds <= 60.0:
            raise receipts.ReleaseControlError("dispatch transport timeout is invalid")
        self._token_fingerprint = receipts._sha256(token)  # noqa: SLF001
        self._token = token.decode("ascii")
        self._timeout_seconds = timeout_seconds
        self._connection_factory = connection_factory

    @property
    def token_fingerprint(self) -> str:
        """Return the non-secret fingerprint bound into the send boundary."""

        return self._token_fingerprint

    def __call__(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        policy: OneWirePolicy,
    ) -> DispatchExchange:
        if policy != OneWirePolicy():
            raise receipts.ReleaseControlError("dispatch transport policy mismatch")
        if headers != {
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
        }:
            raise receipts.ReleaseControlError("dispatch transport headers mismatch")
        if type(body) is not bytes or not body or len(body) > 1024 * 1024:
            raise receipts.ReleaseControlError("dispatch request body is invalid")
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
        except ValueError as exc:
            raise receipts.ReleaseControlError("dispatch endpoint is invalid") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.github.com"
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/repos/")
            or not parsed.path.endswith("/dispatches")
        ):
            raise receipts.ReleaseControlError("dispatch endpoint is not the pinned origin")

        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        connection = self._connection_factory("api.github.com", context, self._timeout_seconds)
        request_may_have_reached_peer = False
        try:
            connection.connect()
            connection.putrequest(
                "POST",
                parsed.path,
                skip_host=False,
                skip_accept_encoding=True,
            )
            wire_headers = {
                **headers,
                "Authorization": f"Bearer {self._token}",
                "Content-Length": str(len(body)),
                "User-Agent": "kestrel-release-control/1",
            }
            for name, value in sorted(wire_headers.items()):
                connection.putheader(name, value)
            request_may_have_reached_peer = True
            connection.endheaders()
            connection.send(body)
            response = connection.getresponse()
            response_body = response.read(MAX_TRANSPORT_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_TRANSPORT_RESPONSE_BYTES:
                raise DispatchTransportError(
                    "dispatch response exceeded its size limit",
                    request_may_have_reached_peer=True,
                )
            response_headers = receipts.canonical_json_bytes(
                [
                    [name.lower(), value]
                    for name, value in sorted(
                        response.getheaders(),
                        key=lambda item: (item[0].lower(), item[1]),
                    )
                ]
            )
            if type(response.status) is not int:
                raise DispatchTransportError(
                    "dispatch response status was invalid",
                    request_may_have_reached_peer=True,
                )
            return DispatchExchange(
                http_status=response.status,
                response_headers=response_headers,
                response_body=response_body,
                request_may_have_reached_peer=True,
            )
        except DispatchTransportError:
            raise
        except Exception as exc:
            raise DispatchTransportError(
                "dispatch transport failed",
                request_may_have_reached_peer=request_may_have_reached_peer,
            ) from exc
        finally:
            with suppress(Exception):
                connection.close()


def _load_canonical_object(path: Path, *, label: str) -> receipts.JSONObject:
    raw = receipts._read_regular(  # noqa: SLF001
        path, label=label, max_bytes=receipts.MAX_SOURCE_BODY_BYTES
    )
    value = receipts.strict_canonical_json(raw, label=label)
    if type(value) is not dict:
        raise receipts.ReleaseControlError(f"{label} must be an object")
    return value


def _canonical_object(raw: bytes, *, label: str) -> receipts.JSONObject:
    return receipts._object(  # noqa: SLF001
        receipts.strict_canonical_json(raw, label=label),
        label=label,
    )


def verify_owner_signed_dispatch_intent(
    *,
    intent: bytes,
    signature: bytes,
    owner_signing_keys_observation: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> receipts.JSONObject:
    """Verify an intent against the fresh registered owner signing key."""

    checked = _canonical_object(intent, label="signed dispatch intent")
    receipts._validate_dispatch_intent(checked)  # noqa: SLF001
    now = _clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise receipts.ReleaseControlError("dispatch intent verification clock must be aware UTC")
    receipts.verify_owner_detached_signature(
        receipt=intent,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
        _clock=lambda: now,
    )
    return checked


def prepare_dispatch_from_observations(
    *,
    repository_observation: bytes,
    workflow_observation: bytes,
    default_branch_workflow_contents: bytes,
    candidate_workflow_contents: bytes,
    candidate_manifest: bytes,
    mode: str,
    dispatcher_observation: bytes,
    prior_intents_observation: bytes,
    _nonce_source: Callable[[int], bytes] = secrets.token_bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    _monotonic: Callable[[], float] = time.monotonic,
) -> tuple[receipts.JSONObject, receipts.JSONObject, receipts.JSONObject]:
    """Derive the exact dispatch records from immutable observation bytes."""

    if mode not in {"initiate", "recover_committed"}:
        raise receipts.ReleaseControlError("dispatch mode is invalid")
    repository_source = _canonical_object(
        repository_observation, label="dispatch repository observation"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        repository_source,
        frozenset({"id", "full_name", "default_branch", "default_branch_sha"}),
        label="dispatch repository observation",
    )
    if repository_source.get("default_branch") != "main":
        raise receipts.ReleaseControlError("dispatch repository default branch mismatch")
    repository = {
        "full_name": repository_source["full_name"],
        "id": repository_source["id"],
    }
    receipts._dispatch_repository(repository)  # noqa: SLF001
    default_branch_sha = receipts._git_sha(  # noqa: SLF001
        repository_source.get("default_branch_sha"),
        label="dispatch repository default branch SHA",
    )

    workflow_source = _canonical_object(workflow_observation, label="dispatch workflow observation")
    receipts._require_exact_fields(  # noqa: SLF001
        workflow_source,
        frozenset({"id", "path", "state"}),
        label="dispatch workflow observation",
    )
    workflow = {
        "id": workflow_source["id"],
        "path": workflow_source["path"],
        "state": workflow_source["state"],
        "default_branch_sha": default_branch_sha,
        "observation_digest": receipts._sha256(workflow_observation),  # noqa: SLF001
    }
    receipts._dispatch_workflow(workflow)  # noqa: SLF001
    if (
        not default_branch_workflow_contents
        or default_branch_workflow_contents != candidate_workflow_contents
    ):
        raise receipts.ReleaseControlError(
            "dispatch ingress workflow bytes differ between default and candidate"
        )

    manifest = _canonical_object(candidate_manifest, label="candidate manifest")
    receipts._validate_schema(  # noqa: SLF001
        "kestrel.release_candidate.v1",
        manifest,
        label="candidate manifest",
    )
    source = receipts._object(  # noqa: SLF001
        manifest.get("source"), label="candidate source"
    )
    candidate_run = receipts._object(  # noqa: SLF001
        manifest.get("candidate_run"), label="candidate run"
    )
    if (
        source.get("repository") != repository["full_name"]
        or source.get("repository_id") != repository["id"]
    ):
        raise receipts.ReleaseControlError("candidate repository identity mismatch")
    candidate_sha = receipts._git_sha(  # noqa: SLF001
        source.get("commit_sha"), label="candidate commit SHA"
    )
    if candidate_run.get("workflow_sha") != candidate_sha:
        raise receipts.ReleaseControlError("candidate workflow SHA mismatch")
    if mode == "initiate" and default_branch_sha != candidate_sha:
        raise receipts.ReleaseControlError("initiate dispatch candidate is not current main")
    short_ref = "main" if mode == "initiate" else manifest["tag"]
    full_ref = f"refs/heads/{short_ref}" if mode == "initiate" else f"refs/tags/{short_ref}"
    target = {
        "mode": mode,
        "short_ref": short_ref,
        "full_ref": full_ref,
        "head_sha": candidate_sha,
        "workflow_ref": (f"{repository['full_name']}/{workflow['path']}@{full_ref}"),
        "workflow_sha": candidate_sha,
    }

    dispatcher = _canonical_object(dispatcher_observation, label="dispatcher observation")
    receipts._require_exact_fields(  # noqa: SLF001
        dispatcher,
        frozenset(
            {
                "schema",
                "repository",
                "repository_id",
                "bot_login",
                "bot_id",
                "app_id",
                "installation_id",
                "permissions",
                "complete",
            }
        ),
        label="dispatcher observation",
    )
    if (
        dispatcher.get("schema") != "kestrel.dispatcher_observation.v1"
        or dispatcher.get("repository") != repository["full_name"]
        or dispatcher.get("repository_id") != repository["id"]
        or dispatcher.get("permissions") != {"actions": "write", "metadata": "read"}
        or dispatcher.get("complete") is not True
    ):
        raise receipts.ReleaseControlError("dispatcher observation mismatch")
    actor = {
        "login": dispatcher["bot_login"],
        "id": dispatcher["bot_id"],
        "app_id": dispatcher["app_id"],
        "installation_id": dispatcher["installation_id"],
    }

    prior = _canonical_object(prior_intents_observation, label="prior dispatch intents observation")
    receipts._require_exact_fields(  # noqa: SLF001
        prior,
        frozenset({"schema", "transaction_nonces", "complete"}),
        label="prior dispatch intents observation",
    )
    if (
        prior.get("schema") != "kestrel.prior_dispatch_intents.v1"
        or prior.get("complete") is not True
    ):
        raise receipts.ReleaseControlError("prior dispatch intents are incomplete")
    prior_values = receipts._array(  # noqa: SLF001
        prior.get("transaction_nonces"), label="prior transaction nonces"
    )
    if len(prior_values) > 4096:
        raise receipts.ReleaseControlError("prior transaction nonce inventory is too large")
    prior_nonces = [receipts._nonce(value) for value in prior_values]  # noqa: SLF001
    if prior_nonces != sorted(set(prior_nonces)):
        raise receipts.ReleaseControlError("prior transaction nonce inventory is not sorted unique")

    journal, intent, request = receipts.prepare_dispatch_records(
        repository=repository,
        workflow=workflow,
        target=target,
        actor=actor,
        inputs={
            "candidate_run_id": str(candidate_run["run_id"]),
            "candidate_manifest_digest": receipts._sha256(candidate_manifest),  # noqa: SLF001
            "mode": mode,
        },
        _nonce_source=_nonce_source,
        _clock=_clock,
        _monotonic=_monotonic,
    )
    if journal["transaction_nonce"] in prior_nonces:
        raise receipts.ReleaseControlError("dispatch transaction nonce was already used")
    return journal, intent, request


def _journal_bound_to_signed_intent(
    *,
    journal: receipts.JSONObject,
    intent: receipts.JSONObject,
    request: receipts.JSONObject,
) -> receipts.JSONObject:
    checked_journal = receipts._validate_dispatch_journal(journal)  # noqa: SLF001
    checked_intent = receipts._validate_dispatch_intent(intent)  # noqa: SLF001
    target = receipts._object(  # noqa: SLF001
        checked_journal["target"], label="dispatch journal target"
    )
    expected_request = {
        "ref": target["short_ref"],
        "inputs": checked_journal["inputs"],
    }
    if request != expected_request:
        raise receipts.ReleaseControlError("dispatch request does not match journal")
    request_digest = receipts._sha256(receipts.canonical_json_bytes(request))  # noqa: SLF001
    journal_digest = receipts._sha256(  # noqa: SLF001
        receipts.canonical_json_bytes(checked_journal)
    )
    if checked_intent.get("transaction_digest") != journal_digest:
        raise receipts.ReleaseControlError("dispatch journal digest mismatch")
    if request_digest != checked_intent.get(
        "request_digest"
    ) or request_digest != checked_journal.get("canonical_request_sha256"):
        raise receipts.ReleaseControlError("dispatch request digest mismatch")
    for field in (
        "transaction_nonce",
        "dispatch_binding",
        "repository",
        "workflow",
        "target",
        "actor",
        "inputs",
        "expected_display_title",
        "evidence",
    ):
        if checked_intent.get(field) != checked_journal.get(field):
            raise receipts.ReleaseControlError(f"dispatch intent/journal {field} binding mismatch")
    if checked_intent.get("issued_at") != checked_journal.get("prepared_at"):
        raise receipts.ReleaseControlError(
            "dispatch intent/journal preparation time binding mismatch"
        )
    return checked_journal


RAW_WORKFLOW_RUNS_SCHEMA = "kestrel.dispatch_workflow_runs_raw_observation.v1"
RAW_IDENTITY_ARTIFACTS_SCHEMA = "kestrel.dispatch_identity_artifact_raw_observation.v1"
DISPATCH_TITLE_RE = re.compile(r"^Kestrel release tx ([0-9a-f]{64}) bind (sha256:[0-9a-f]{64})$")


def _decode_observation_bytes(
    value: object, *, label: str, maximum: int = receipts.MAX_SOURCE_BODY_BYTES
) -> bytes:
    encoded = receipts._validate_string(value, label=label)  # noqa: SLF001
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise receipts.ReleaseControlError(f"{label} is not canonical base64") from exc
    if base64.b64encode(raw).decode("ascii") != encoded or len(raw) > maximum:
        raise receipts.ReleaseControlError(f"{label} is not canonical bounded base64")
    return raw


def _raw_response_headers(value: object, *, label: str) -> dict[str, str]:
    headers: list[tuple[str, str]] = []
    for raw_pair in receipts._array(value, label=label):  # noqa: SLF001
        pair = receipts._array(raw_pair, label=f"{label} pair")  # noqa: SLF001
        if len(pair) != 2:
            raise receipts.ReleaseControlError(f"{label} pair fields mismatch")
        name = receipts._validate_string(pair[0], label=f"{label} name")  # noqa: SLF001
        value_text = receipts._validate_string(  # noqa: SLF001
            pair[1], label=f"{label} value", allow_empty=True
        )
        if name != name.lower() or not re.fullmatch(r"[a-z0-9-]+", name):
            raise receipts.ReleaseControlError(f"{label} name is not normalized")
        headers.append((name, value_text))
    if headers != sorted(headers) or len({name for name, _ in headers}) != len(headers):
        raise receipts.ReleaseControlError(f"{label} is not sorted unique")
    return dict(headers)


def _next_link_request(headers: dict[str, str]) -> str | None:
    link = headers.get("link")
    if link is None:
        return None
    next_urls: list[str] = []
    for member in link.split(","):
        parts = [part.strip() for part in member.split(";")]
        if len(parts) < 2 or not parts[0].startswith("<") or not parts[0].endswith(">"):
            raise receipts.ReleaseControlError("GitHub Link response header is malformed")
        if any(part == 'rel="next"' for part in parts[1:]):
            next_urls.append(parts[0][1:-1])
    if len(next_urls) > 1:
        raise receipts.ReleaseControlError("GitHub Link response has multiple next links")
    if not next_urls:
        return None
    parsed = urlsplit(next_urls[0])
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.github.com"
        or not parsed.path.startswith("/repos/John-MiracleWorker/Kestrel/")
        or not parsed.query
        or parsed.fragment
    ):
        raise receipts.ReleaseControlError("GitHub next-page link leaves the pinned origin")
    return f"GET {parsed.path}?{parsed.query}"


def _parse_raw_page(
    value: object, *, expected_number: int, label: str
) -> tuple[receipts.JSONObject, bytes, dict[str, str], str, int]:
    page = receipts._object(value, label=label)  # noqa: SLF001
    receipts._require_exact_fields(  # noqa: SLF001
        page,
        frozenset(
            {
                "number",
                "request_url",
                "http_status",
                "response_headers",
                "response_body",
            }
        ),
        label=label,
    )
    if page.get("number") != expected_number:
        raise receipts.ReleaseControlError(f"{label} number is not consecutive")
    request_url = receipts._validate_string(  # noqa: SLF001
        page.get("request_url"), label=f"{label} request URL"
    )
    status = receipts._safe_integer(  # noqa: SLF001
        page.get("http_status"), label=f"{label} HTTP status"
    )
    if not 100 <= status <= 599:
        raise receipts.ReleaseControlError(f"{label} HTTP status is invalid")
    headers = _raw_response_headers(page.get("response_headers"), label=f"{label} response headers")
    body = _decode_observation_bytes(page.get("response_body"), label=f"{label} response body")
    return page, body, headers, request_url, status


def _parse_paginated_items(
    pages_value: object,
    *,
    base_query: str,
    items_field: str,
    label: str,
) -> tuple[list[receipts.JSONObject], list[receipts.JSONObject], bool, list[str]]:
    raw_pages = receipts._array(pages_value, label=f"{label} pages")  # noqa: SLF001
    if not raw_pages or len(raw_pages) > 100:
        raise receipts.ReleaseControlError(f"{label} page cardinality is invalid")
    items: list[receipts.JSONObject] = []
    pages: list[receipts.JSONObject] = []
    complete = True
    reasons: set[str] = set()
    expected_request = base_query
    total_count: int | None = None
    for index, raw_page in enumerate(raw_pages, start=1):
        _, body, headers, request_url, status = _parse_raw_page(
            raw_page, expected_number=index, label=f"{label} page"
        )
        if request_url != expected_request:
            complete = False
            reasons.add("pagination_request_mismatch")
        next_request = _next_link_request(headers)
        if index < len(raw_pages):
            next_page = receipts._object(  # noqa: SLF001
                raw_pages[index], label=f"{label} next page"
            )
            expected_request = receipts._validate_string(  # noqa: SLF001
                next_page.get("request_url"), label=f"{label} next request URL"
            )
            if next_request != expected_request:
                complete = False
                reasons.add("pagination_link_mismatch")
        elif next_request is not None:
            complete = False
            reasons.add("pagination_page_missing")
        digest = receipts._sha256(body)  # noqa: SLF001
        pages.append(
            {
                "number": index,
                "http_status": status,
                "response_sha256": digest,
                "next": None if index == len(raw_pages) else index + 1,
            }
        )
        if status != 200:
            complete = False
            reasons.add("http_status_not_200")
            continue
        response = receipts._object(  # noqa: SLF001
            receipts.parse_external_json_bytes(body, label=f"{label} response body"),
            label=f"{label} response body",
        )
        if "total_count" not in response or items_field not in response:
            raise receipts.ReleaseControlError(f"{label} response fields are incomplete")
        page_total = receipts._safe_integer(  # noqa: SLF001
            response.get("total_count"), label=f"{label} total count"
        )
        if total_count is None:
            total_count = page_total
        elif total_count != page_total:
            complete = False
            reasons.add("pagination_total_count_changed")
        for raw_item in receipts._array(  # noqa: SLF001
            response.get(items_field), label=f"{label} response items"
        ):
            item = receipts._object(raw_item, label=f"{label} response item")  # noqa: SLF001
            enriched = dict(item)
            enriched["_observation_sha256"] = digest
            items.append(enriched)
    if total_count is None or total_count != len(items):
        complete = False
        reasons.add("pagination_total_count_mismatch")
    if total_count is not None and total_count >= 1000:
        complete = False
        reasons.add("pagination_result_ceiling")
    return items, pages, complete, sorted(reasons)


def _normalize_rest_run(value: receipts.JSONObject, *, label: str) -> receipts.JSONObject:
    required = {
        "id",
        "workflow_id",
        "repository",
        "path",
        "event",
        "display_title",
        "head_branch",
        "head_sha",
        "run_attempt",
        "actor",
        "triggering_actor",
        "status",
        "conclusion",
    }
    if not required <= set(value):
        raise receipts.ReleaseControlError(f"{label} required fields are incomplete")
    run_id = receipts._safe_integer(value.get("id"), label=f"{label} ID", positive=True)  # noqa: SLF001
    repository = receipts._object(value.get("repository"), label=f"{label} repository")  # noqa: SLF001
    actor = receipts._object(value.get("actor"), label=f"{label} actor")  # noqa: SLF001
    triggering_actor = receipts._object(  # noqa: SLF001
        value.get("triggering_actor"), label=f"{label} triggering actor"
    )
    conclusion = value.get("conclusion")
    if conclusion is not None:
        receipts._validate_string(conclusion, label=f"{label} conclusion")  # noqa: SLF001
    return {
        "workflow_id": receipts._safe_integer(  # noqa: SLF001
            value.get("workflow_id"), label=f"{label} workflow ID", positive=True
        ),
        "repository_id": receipts._safe_integer(  # noqa: SLF001
            repository.get("id"), label=f"{label} repository ID", positive=True
        ),
        "repository_full_name": receipts._validate_string(  # noqa: SLF001
            repository.get("full_name"), label=f"{label} repository name"
        ),
        "path": receipts._validate_string(value.get("path"), label=f"{label} path"),  # noqa: SLF001
        "event": receipts._validate_string(value.get("event"), label=f"{label} event"),  # noqa: SLF001
        "display_title": receipts._validate_string(  # noqa: SLF001
            value.get("display_title"), label=f"{label} display title"
        ),
        "head_branch": receipts._validate_string(  # noqa: SLF001
            value.get("head_branch"), label=f"{label} head branch"
        ),
        "head_sha": receipts._git_sha(value.get("head_sha"), label=f"{label} head SHA"),  # noqa: SLF001
        "run_attempt": receipts._safe_integer(  # noqa: SLF001
            value.get("run_attempt"), label=f"{label} run attempt", positive=True
        ),
        "actor_login": receipts._validate_string(  # noqa: SLF001
            actor.get("login"), label=f"{label} actor login"
        ),
        "actor_id": receipts._safe_integer(  # noqa: SLF001
            actor.get("id"), label=f"{label} actor ID", positive=True
        ),
        "triggering_actor_login": receipts._validate_string(  # noqa: SLF001
            triggering_actor.get("login"), label=f"{label} triggering actor login"
        ),
        "triggering_actor_id": receipts._safe_integer(  # noqa: SLF001
            triggering_actor.get("id"), label=f"{label} triggering actor ID", positive=True
        ),
        "status": receipts._validate_string(  # noqa: SLF001
            value.get("status"), label=f"{label} status"
        ),
        "conclusion": conclusion,
        "_run_id": run_id,
    }


def _identity_from_archive(raw: bytes) -> receipts.JSONObject | None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) != 1:
                return None
            member = members[0]
            file_type = (member.external_attr >> 16) & 0o170000
            if (
                member.filename != "dispatch-identity.json"
                or member.is_dir()
                or file_type == 0o120000
                or member.file_size > 4 * 1024 * 1024
                or member.flag_bits & 0x1
            ):
                return None
            identity_raw = archive.read(member)
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError):
        return None
    try:
        identity = _canonical_object(identity_raw, label="dispatch identity artifact")
        receipts._validate_schema(  # noqa: SLF001
            receipts.DISPATCH_IDENTITY_SCHEMA,
            identity,
            label="dispatch identity artifact",
        )
    except receipts.ReleaseControlError:
        return None
    return identity


def _parse_raw_direct_runs(
    raw: object,
    *,
    journal: receipts.JSONObject,
    expected_ids: set[int],
) -> tuple[dict[int, tuple[receipts.JSONObject, str]], bool]:
    repository = receipts._object(  # noqa: SLF001
        journal["repository"], label="dispatch repository"
    )
    direct_runs: dict[int, tuple[receipts.JSONObject, str]] = {}
    complete = True
    previous_id = 0
    for raw_direct in receipts._array(raw, label="direct run observations"):  # noqa: SLF001
        direct = receipts._object(  # noqa: SLF001
            raw_direct, label="direct run observation"
        )
        receipts._require_exact_fields(  # noqa: SLF001
            direct,
            frozenset(
                {
                    "run_id",
                    "request_url",
                    "http_status",
                    "response_headers",
                    "response_body",
                }
            ),
            label="direct run observation",
        )
        run_id = receipts._safe_integer(  # noqa: SLF001
            direct.get("run_id"), label="direct run ID", positive=True
        )
        if run_id <= previous_id or run_id not in expected_ids:
            raise receipts.ReleaseControlError(
                "direct run observations are not sorted observed nonce IDs"
            )
        previous_id = run_id
        _, body, _, request_url, status = _parse_raw_page(
            {
                "number": 1,
                "request_url": direct.get("request_url"),
                "http_status": direct.get("http_status"),
                "response_headers": direct.get("response_headers"),
                "response_body": direct.get("response_body"),
            },
            expected_number=1,
            label="direct run observation",
        )
        expected_url = f"GET /repos/{repository['full_name']}/actions/runs/{run_id}"
        if request_url != expected_url or status != 200:
            complete = False
            continue
        raw_run = receipts._object(  # noqa: SLF001
            receipts.parse_external_json_bytes(body, label="direct GET-run response"),
            label="direct GET-run response",
        )
        normalized = _normalize_rest_run(raw_run, label="direct workflow run")
        response_run_id = cast(int, normalized.pop("_run_id"))
        if response_run_id != run_id:
            raise receipts.ReleaseControlError("direct GET-run response ID mismatch")
        direct_runs[run_id] = (normalized, receipts._sha256(body))  # noqa: SLF001
    return direct_runs, complete and set(direct_runs) == expected_ids


def _parse_raw_identity_artifact_runs(
    raw: object,
    *,
    journal: receipts.JSONObject,
    expected_ids: set[int],
) -> tuple[dict[int, receipts.JSONObject], bool]:
    repository = receipts._object(journal["repository"], label="dispatch repository")  # noqa: SLF001
    repository_name = str(repository["full_name"])
    by_run: dict[int, receipts.JSONObject] = {}
    previous_run_id = 0
    complete = True
    for raw_run in receipts._array(raw, label="artifact runs"):  # noqa: SLF001
        run = receipts._object(raw_run, label="artifact run observation")  # noqa: SLF001
        receipts._require_exact_fields(  # noqa: SLF001
            run,
            frozenset({"run_id", "pages", "downloads"}),
            label="artifact run observation",
        )
        run_id = receipts._safe_integer(  # noqa: SLF001
            run.get("run_id"), label="artifact run ID", positive=True
        )
        if run_id <= previous_run_id or run_id not in expected_ids:
            raise receipts.ReleaseControlError(
                "identity artifact run observations are not sorted observed nonce IDs"
            )
        previous_run_id = run_id
        base_query = f"GET /repos/{repository_name}/actions/runs/{run_id}/artifacts?per_page=100"
        artifacts, _, pages_complete, _ = _parse_paginated_items(
            run.get("pages"),
            base_query=base_query,
            items_field="artifacts",
            label="identity artifact list",
        )
        complete = complete and pages_complete
        expected_name = f"kestrel-dispatch-identity-{run_id}-1"
        matching = [item for item in artifacts if item.get("name") == expected_name]
        selected_pool = matching if matching else artifacts
        if not selected_pool:
            complete = False
            continue
        selected_pool.sort(
            key=lambda item: receipts._safe_integer(  # noqa: SLF001
                item.get("id"), label="identity artifact ID", positive=True
            )
        )
        selected = selected_pool[0]
        artifact_id = receipts._safe_integer(  # noqa: SLF001
            selected.get("id"), label="identity artifact ID", positive=True
        )
        artifact_name = receipts._validate_string(  # noqa: SLF001
            selected.get("name"), label="identity artifact name"
        )
        if type(selected.get("expired")) is not bool:
            raise receipts.ReleaseControlError("identity artifact expiry is invalid")
        api_digest = receipts._digest(  # noqa: SLF001
            selected.get("digest"), label="identity artifact API digest"
        )
        downloads: dict[int, receipts.JSONObject] = {}
        for raw_download in receipts._array(  # noqa: SLF001
            run.get("downloads"), label="identity artifact downloads"
        ):
            download = receipts._object(  # noqa: SLF001
                raw_download, label="identity artifact download"
            )
            receipts._require_exact_fields(  # noqa: SLF001
                download,
                frozenset(
                    {
                        "artifact_id",
                        "request_url",
                        "http_status",
                        "response_headers",
                        "response_body",
                    }
                ),
                label="identity artifact download",
            )
            download_id = receipts._safe_integer(  # noqa: SLF001
                download.get("artifact_id"), label="download artifact ID", positive=True
            )
            if download_id in downloads:
                raise receipts.ReleaseControlError("duplicate identity artifact download")
            downloads[download_id] = download
        selected_download = downloads.get(artifact_id)
        if selected_download is None:
            complete = False
            continue
        expected_url = f"GET /repos/{repository_name}/actions/artifacts/{artifact_id}/zip"
        if (
            selected_download.get("request_url") != expected_url
            or selected_download.get("http_status") != 200
        ):
            complete = False
            continue
        _raw_response_headers(
            selected_download.get("response_headers"),
            label="identity artifact download response headers",
        )
        archive = _decode_observation_bytes(
            selected_download.get("response_body"), label="identity artifact archive"
        )
        identity = _identity_from_archive(archive)
        if identity is None:
            complete = False
            continue
        identity_bytes = receipts.canonical_json_bytes(identity)
        by_run[run_id] = {
            "artifact_id": artifact_id,
            "name": artifact_name,
            "api_digest": api_digest,
            "archive_sha256": receipts._sha256(archive),  # noqa: SLF001
            "content_sha256": receipts._sha256(identity_bytes),  # noqa: SLF001
            "expired": selected["expired"],
            "matching_name_count": len(matching),
            "identity": identity,
        }
    return by_run, complete and set(by_run) == expected_ids


def _join_reconciliation_observations(
    *,
    journal: receipts.JSONObject,
    workflow_runs_observation: bytes,
    identity_artifact_observations: bytes,
) -> tuple[list[receipts.JSONObject], list[receipts.JSONObject]]:
    """Derive reconciliation predicates only from captured raw server bytes."""

    workflow_runs = _canonical_object(
        workflow_runs_observation,
        label="raw workflow runs reconciliation observation",
    )
    receipts._require_exact_fields(  # noqa: SLF001
        workflow_runs,
        frozenset({"schema", "polls", "complete"}),
        label="raw workflow runs reconciliation observation",
    )
    if workflow_runs.get("schema") != RAW_WORKFLOW_RUNS_SCHEMA:
        raise receipts.ReleaseControlError("raw workflow runs observation schema mismatch")
    if type(workflow_runs.get("complete")) is not bool:
        raise receipts.ReleaseControlError("raw workflow runs completeness is invalid")
    identity_observations = _canonical_object(
        identity_artifact_observations,
        label="raw identity artifact observations",
    )
    receipts._require_exact_fields(  # noqa: SLF001
        identity_observations,
        frozenset({"schema", "polls", "complete"}),
        label="raw identity artifact observations",
    )
    if identity_observations.get("schema") != RAW_IDENTITY_ARTIFACTS_SCHEMA:
        raise receipts.ReleaseControlError("raw identity artifact observation schema mismatch")
    if type(identity_observations.get("complete")) is not bool:
        raise receipts.ReleaseControlError("raw identity artifact completeness is invalid")
    raw_workflow_polls = receipts._array(  # noqa: SLF001
        workflow_runs.get("polls"), label="raw dispatch polls"
    )
    raw_artifact_polls = receipts._array(  # noqa: SLF001
        identity_observations.get("polls"), label="raw identity artifact polls"
    )
    artifact_polls: list[receipts.JSONObject] = []
    for ordinal, raw_artifact_poll in enumerate(raw_artifact_polls, start=1):
        artifact_poll = receipts._object(  # noqa: SLF001
            raw_artifact_poll, label="raw identity artifact poll"
        )
        receipts._require_exact_fields(  # noqa: SLF001
            artifact_poll,
            frozenset({"ordinal", "runs", "complete"}),
            label="raw identity artifact poll",
        )
        if artifact_poll.get("ordinal") != ordinal:
            raise receipts.ReleaseControlError(
                "raw identity artifact poll ordinals are not consecutive"
            )
        if type(artifact_poll.get("complete")) is not bool:
            raise receipts.ReleaseControlError("raw identity artifact poll completeness is invalid")
        artifact_polls.append(artifact_poll)
    artifact_poll_cardinality_ok = len(artifact_polls) == len(raw_workflow_polls)
    repository = receipts._object(journal["repository"], label="dispatch repository")  # noqa: SLF001
    workflow = receipts._object(journal["workflow"], label="dispatch workflow")  # noqa: SLF001
    base_query = (
        f"GET /repos/{repository['full_name']}/actions/workflows/{workflow['id']}/"
        "runs?event=workflow_dispatch&per_page=100"
    )
    expected_nonce = str(journal["transaction_nonce"])
    expected_binding = str(journal["dispatch_binding"])
    polls: list[receipts.JSONObject] = []
    candidate_ids: set[int] = set()
    conflict_ids: set[int] = set()
    list_digests: dict[int, set[str]] = {}
    latest_candidates: dict[int, receipts.JSONObject] = {}
    poll_snapshots: list[dict[int, receipts.JSONObject]] = []
    for ordinal, raw_poll in enumerate(
        raw_workflow_polls,
        start=1,
    ):
        poll = receipts._object(raw_poll, label="raw dispatch poll")  # noqa: SLF001
        receipts._require_exact_fields(  # noqa: SLF001
            poll,
            frozenset({"requested_at", "workflow", "pages", "direct_runs"}),
            label="raw dispatch poll",
        )
        requested_at = receipts._validate_string(  # noqa: SLF001
            poll.get("requested_at"), label="raw dispatch poll requested_at"
        )
        receipts.parse_timestamp(requested_at, label="raw dispatch poll requested_at")
        workflow_raw = receipts._object(  # noqa: SLF001
            poll.get("workflow"), label="raw dispatch workflow observation"
        )
        _, workflow_body, _, workflow_url, workflow_status = _parse_raw_page(
            {"number": 1, **workflow_raw},
            expected_number=1,
            label="raw dispatch workflow observation",
        )
        workflow_value = receipts._object(  # noqa: SLF001
            receipts.parse_external_json_bytes(
                workflow_body, label="raw dispatch workflow response"
            ),
            label="raw dispatch workflow response",
        )
        expected_workflow_url = (
            f"GET /repos/{repository['full_name']}/actions/workflows/{workflow['id']}"
        )
        workflow_ok = (
            workflow_url == expected_workflow_url
            and workflow_status == 200
            and workflow_value.get("id") == workflow.get("id")
            and workflow_value.get("path") == workflow.get("path")
            and workflow_value.get("state") == "active"
        )
        runs, pages, complete, reasons = _parse_paginated_items(
            poll.get("pages"),
            base_query=base_query,
            items_field="workflow_runs",
            label="workflow runs",
        )
        if not workflow_ok:
            reasons = sorted({*reasons, "workflow_inactive_or_mismatched"})
            complete = False
        nonce_ids: set[int] = set()
        binding_conflicts: set[int] = set()
        seen_ids: set[int] = set()
        for raw_run in runs:
            normalized = _normalize_rest_run(raw_run, label="listed workflow run")
            run_id = cast(int, normalized.pop("_run_id"))
            if run_id in seen_ids:
                complete = False
                reasons = sorted({*reasons, "duplicate_run_in_poll"})
            seen_ids.add(run_id)
            list_digests.setdefault(run_id, set()).add(cast(str, raw_run["_observation_sha256"]))
            title = cast(str, normalized["display_title"])
            match = DISPATCH_TITLE_RE.fullmatch(title)
            if match is None or match.group(1) != expected_nonce:
                continue
            if match.group(2) == expected_binding:
                nonce_ids.add(run_id)
            else:
                binding_conflicts.add(run_id)
        candidate_ids.update(nonce_ids)
        conflict_ids.update(binding_conflicts)
        known_ids = candidate_ids | conflict_ids
        direct_runs, direct_complete = _parse_raw_direct_runs(
            poll.get("direct_runs"),
            journal=journal,
            expected_ids=known_ids,
        )
        if not direct_complete:
            complete = False
            reasons = sorted({*reasons, "direct_run_observation_incomplete"})
        if ordinal <= len(artifact_polls):
            artifact_poll = artifact_polls[ordinal - 1]
            artifacts_by_run, artifacts_complete = _parse_raw_identity_artifact_runs(
                artifact_poll.get("runs"),
                journal=journal,
                expected_ids=known_ids,
            )
            artifacts_complete = artifacts_complete and artifact_poll.get("complete") is True
        else:
            artifacts_by_run = {}
            artifacts_complete = False
        if not artifacts_complete:
            complete = False
            reasons = sorted({*reasons, "identity_artifact_observation_incomplete"})
        if not artifact_poll_cardinality_ok:
            complete = False
            reasons = sorted({*reasons, "identity_artifact_poll_cardinality_mismatch"})
        snapshots: dict[int, receipts.JSONObject] = {}
        for run_id in sorted(known_ids):
            direct_entry = direct_runs.get(run_id)
            artifact = artifacts_by_run.get(run_id)
            if direct_entry is None or artifact is None:
                continue
            run, get_digest = direct_entry
            snapshot: receipts.JSONObject = {
                "get_run_observation_sha256": get_digest,
                "run": run,
                "identity_artifact": artifact,
            }
            snapshots[run_id] = snapshot
            if run_id in candidate_ids:
                latest_candidates[run_id] = snapshot
        poll_snapshots.append(snapshots)
        poll_record: receipts.JSONObject = {
            "ordinal": ordinal,
            "requested_at": requested_at,
            "workflow_observation_sha256": receipts._sha256(  # noqa: SLF001
                workflow_body
            ),
            "query": base_query,
            "pages": cast(list[receipts.JSONValue], pages),
            "complete": (
                complete
                and workflow_runs.get("complete") is True
                and identity_observations.get("complete") is True
            ),
            "result_count": len(runs),
            "nonce_run_ids": cast(list[receipts.JSONValue], sorted(nonce_ids)),
            "binding_conflict_run_ids": cast(list[receipts.JSONValue], sorted(binding_conflicts)),
            "rejection_reasons": cast(list[receipts.JSONValue], reasons),
        }
        polls.append(poll_record)
    if not polls:
        raise receipts.ReleaseControlError("raw workflow runs observation has no polls")

    if len(polls) >= 3:
        final_poll_indexes = range(len(polls) - 3, len(polls))
        final_polls = [polls[index] for index in final_poll_indexes]
        nonce_sets = [set(cast(list[int], poll["nonce_run_ids"])) for poll in final_polls]
        conflict_sets = [
            set(cast(list[int], poll["binding_conflict_run_ids"])) for poll in final_polls
        ]
        if (
            len(nonce_sets[0]) == 1
            and all(ids == nonce_sets[0] for ids in nonce_sets)
            and all(not ids for ids in conflict_sets)
        ):
            singleton = next(iter(nonce_sets[0]))
            stable_snapshots = [
                poll_snapshots[index].get(singleton) for index in final_poll_indexes
            ]
            if all(snapshot is not None for snapshot in stable_snapshots):
                snapshot_digests = {
                    receipts._sha256(  # noqa: SLF001
                        receipts.canonical_json_bytes(cast(receipts.JSONObject, snapshot))
                    )
                    for snapshot in stable_snapshots
                }
                if len(snapshot_digests) != 1:
                    for index in final_poll_indexes:
                        polls[index]["complete"] = False
                        current_reasons = cast(
                            list[receipts.JSONValue],
                            polls[index]["rejection_reasons"],
                        )
                        polls[index]["rejection_reasons"] = cast(
                            list[receipts.JSONValue],
                            sorted(
                                {
                                    *map(str, current_reasons),
                                    "candidate_observation_changed",
                                }
                            ),
                        )
    candidates: list[receipts.JSONObject] = []
    for run_id in sorted(candidate_ids):
        latest = latest_candidates.get(run_id)
        if latest is None:
            continue
        list_digest = receipts._sha256(  # noqa: SLF001
            receipts.canonical_json_bytes(sorted(list_digests.get(run_id, set())))
        )
        candidates.append(
            {
                "run_id": run_id,
                "list_observation_sha256": list_digest,
                **latest,
            }
        )
    return polls, candidates


def _send_boundary_path(journal_path: Path) -> Path:
    return journal_path.with_name(f"{journal_path.stem}.send-boundary.json")


def _nonce_send_boundary_path(journal: receipts.JSONObject) -> Path:
    root = _dispatch_state_root()
    return root / f"{journal['transaction_nonce']}.send-boundary.json"


def _load_or_recover_send_boundary(
    *, journal_path: Path, journal: receipts.JSONObject
) -> receipts.JSONObject:
    """Recover the path-local copy from the authoritative create-once nonce record."""

    nonce_path = _nonce_send_boundary_path(journal)
    nonce_raw = receipts._read_regular(  # noqa: SLF001
        nonce_path,
        label="nonce dispatch send boundary",
        max_bytes=4 * 1024 * 1024,
    )
    nonce_boundary = _canonical_object(nonce_raw, label="nonce dispatch send boundary")
    local_path = _send_boundary_path(journal_path)
    if not local_path.exists() and not local_path.is_symlink():
        if not receipts.write_once(local_path, nonce_raw):
            raise receipts.ReleaseControlError("local dispatch send boundary recovery conflicted")
    local_boundary = _load_canonical_object(local_path, label="local dispatch send boundary")
    if local_boundary != nonce_boundary:
        raise receipts.ReleaseControlError("dispatch send boundary copies mismatch")
    return local_boundary


def _dispatch_state_root() -> Path:
    root = DISPATCH_STATE_ROOT
    if root.exists() or root.is_symlink():
        if not root.is_dir() or root.is_symlink():
            raise receipts.ReleaseControlError("dispatch state root is invalid")
    else:
        root.mkdir(parents=True, mode=0o700)
    return root


def _persist_or_load_terminal_record(
    *,
    kind: str,
    record: Mapping[str, object],
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bytes:
    """Persist exact pre-claim bytes so a retry cannot change the remote claim digest."""

    if kind not in _TERMINAL_RECORD_SCHEMAS:
        raise receipts.ReleaseControlError("dispatch terminal pending kind is invalid")
    checked = receipts._copy_json_object(  # noqa: SLF001
        record, label=f"pending dispatch {kind}"
    )
    schema_name = _TERMINAL_RECORD_SCHEMAS[kind]
    receipts._validate_schema(  # noqa: SLF001
        schema_name,
        checked,
        label=f"pending dispatch {kind}",
    )
    if checked.get("schema") != schema_name:
        raise receipts.ReleaseControlError("dispatch terminal pending schema mismatch")
    nonce = receipts._nonce(checked.get("transaction_nonce"))  # noqa: SLF001
    proposed = receipts.canonical_json_bytes(checked)
    path = _dispatch_state_root() / f"{nonce}.terminal-{kind}.pending.json"
    if path.exists() or path.is_symlink():
        selected = receipts._read_regular(  # noqa: SLF001
            path,
            label=f"pending dispatch {kind}",
            max_bytes=4 * 1024 * 1024,
        )
    else:
        try:
            receipts.write_once(path, proposed)
            selected = proposed
        except receipts.ReleaseControlError:
            if not path.exists() or path.is_symlink():
                raise
            selected = receipts._read_regular(  # noqa: SLF001
                path,
                label=f"pending dispatch {kind}",
                max_bytes=4 * 1024 * 1024,
            )
    persisted = _canonical_object(selected, label=f"pending dispatch {kind}")
    receipts._validate_schema(  # noqa: SLF001
        schema_name,
        persisted,
        label=f"pending dispatch {kind}",
    )
    varying_fields = {"issued_at", "expires_at"} if kind == "admission" else set()
    if {key: value for key, value in checked.items() if key not in varying_fields} != {
        key: value for key, value in persisted.items() if key not in varying_fields
    }:
        raise receipts.ReleaseControlError("dispatch terminal pending record binding mismatch")
    if kind == "admission":
        now = _clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise receipts.ReleaseControlError("dispatch terminal pending clock must be aware UTC")
        now = now.astimezone(UTC).replace(microsecond=0)
        issued = receipts.parse_timestamp(
            persisted.get("issued_at"), label="pending dispatch admission issued_at"
        )
        expires = receipts.parse_timestamp(
            persisted.get("expires_at"), label="pending dispatch admission expires_at"
        )
        if now < issued or now >= expires:
            raise receipts.ReleaseControlError("pending dispatch admission is not currently valid")
    return selected


def _reconciliation_checkpoint_directory(journal: receipts.JSONObject) -> Path:
    path = _dispatch_state_root() / f"{journal['transaction_nonce']}.reconciliation"
    if path.exists() or path.is_symlink():
        if not path.is_dir() or path.is_symlink():
            raise receipts.ReleaseControlError("dispatch reconciliation checkpoint path is invalid")
    else:
        path.mkdir(mode=0o700)
    return path


def _persist_reconciliation_checkpoint(
    *,
    journal: Mapping[str, object],
    containment: Mapping[str, object],
    polls: Sequence[Mapping[str, object]],
    candidates: Sequence[Mapping[str, object]],
    terminal: Mapping[str, object] | None,
) -> None:
    """Append validated reconciliation evidence without resetting its deadline."""

    checked_journal = receipts._validate_dispatch_journal(journal)  # noqa: SLF001
    checked_containment = receipts._validate_dispatch_containment_record(  # noqa: SLF001
        containment,
        journal=checked_journal,
    )
    checked_polls: list[receipts.JSONObject] = []
    for ordinal, poll in enumerate(polls, start=1):
        checked_poll, _, _, _ = receipts._validate_poll(  # noqa: SLF001
            poll,
            expected_ordinal=ordinal,
        )
        checked_polls.append(checked_poll)
    checked_candidates: list[receipts.JSONObject] = []
    for candidate in candidates:
        raw_candidate = receipts._copy_json_object(  # noqa: SLF001
            candidate,
            label="dispatch reconciliation checkpoint candidate",
        )
        receipts._candidate_predicate(  # noqa: SLF001
            raw_candidate,
            journal=checked_journal,
        )
        checked_candidates.append(raw_candidate)
    checked_candidates.sort(key=lambda item: cast(int, item["run_id"]))
    if len({item["run_id"] for item in checked_candidates}) != len(checked_candidates):
        raise receipts.ReleaseControlError(
            "dispatch reconciliation checkpoint candidates are duplicated"
        )

    if checked_polls:
        started = receipts.parse_timestamp(
            checked_polls[0].get("requested_at"),
            label="dispatch reconciliation checkpoint start",
        )
        deadline = started + timedelta(seconds=receipts.DISPATCH_RECONCILIATION_SECONDS)
    else:
        token_probe = receipts._object(  # noqa: SLF001
            checked_containment.get("token_probe"),
            label="dispatch containment token probe",
        )
        started = receipts.parse_timestamp(
            token_probe.get("observed_at"),
            label="dispatch reconciliation checkpoint start",
        )
        deadline = started
    metadata: receipts.JSONObject = {
        "schema": "kestrel.dispatch_reconciliation_checkpoint.v1",
        "transaction_nonce": checked_journal["transaction_nonce"],
        "journal_digest": receipts._sha256(  # noqa: SLF001
            receipts.canonical_json_bytes(checked_journal)
        ),
        "containment_digest": receipts._sha256(  # noqa: SLF001
            receipts.canonical_json_bytes(checked_containment)
        ),
        "started_at": receipts._format_timestamp(  # noqa: SLF001
            started, label="dispatch reconciliation checkpoint start"
        ),
        "deadline_at": receipts._format_timestamp(  # noqa: SLF001
            deadline, label="dispatch reconciliation checkpoint deadline"
        ),
        "validation_status": "validated",
    }
    directory = _reconciliation_checkpoint_directory(checked_journal)
    actual_names = {path.name for path in directory.iterdir()}
    fixed_names = {
        "checkpoint.json",
        "terminal.json",
        *{f"poll-{index:03d}.json" for index in range(1, 122)},
    }
    candidate_name_pattern = re.compile(r"candidate-([1-9][0-9]*)-([0-9a-f]{64})[.]json")
    if any(
        name not in fixed_names and candidate_name_pattern.fullmatch(name) is None
        for name in actual_names
    ):
        raise receipts.ReleaseControlError(
            "dispatch reconciliation checkpoint contains unknown state"
        )

    metadata_path = directory / "checkpoint.json"
    metadata_raw = receipts.canonical_json_bytes(metadata)
    if metadata_path.exists() or metadata_path.is_symlink():
        if (
            _load_canonical_object(metadata_path, label="dispatch reconciliation checkpoint")
            != metadata
        ):
            raise receipts.ReleaseControlError("dispatch reconciliation checkpoint deadline reset")
    elif not receipts.write_once(metadata_path, metadata_raw):
        raise receipts.ReleaseControlError("dispatch reconciliation checkpoint creation raced")

    existing_poll_count = len([name for name in actual_names if name.startswith("poll-")])
    if len(checked_polls) < existing_poll_count:
        raise receipts.ReleaseControlError(
            "dispatch reconciliation checkpoint history was truncated"
        )
    for ordinal, poll in enumerate(checked_polls, start=1):
        path = directory / f"poll-{ordinal:03d}.json"
        raw = receipts.canonical_json_bytes(poll)
        if path.exists() or path.is_symlink():
            if (
                _load_canonical_object(path, label="dispatch reconciliation checkpoint poll")
                != poll
            ):
                raise receipts.ReleaseControlError(
                    "dispatch reconciliation checkpoint poll history changed"
                )
        elif not receipts.write_once(path, raw):
            raise receipts.ReleaseControlError(
                "dispatch reconciliation checkpoint poll creation raced"
            )

    existing_candidate_names = sorted(
        name for name in actual_names if name.startswith("candidate-")
    )
    if len(existing_candidate_names) > 512:
        raise receipts.ReleaseControlError(
            "dispatch reconciliation checkpoint candidate history is too large"
        )
    for name in existing_candidate_names:
        match = candidate_name_pattern.fullmatch(name)
        if match is None:  # pragma: no cover - rejected by the inventory check above
            raise receipts.ReleaseControlError(
                "dispatch reconciliation checkpoint candidate name is invalid"
            )
        persisted = _load_canonical_object(
            directory / name,
            label="dispatch reconciliation checkpoint candidate",
        )
        receipts._candidate_predicate(  # noqa: SLF001
            persisted,
            journal=checked_journal,
        )
        persisted_raw = receipts.canonical_json_bytes(persisted)
        if (
            cast(int, persisted["run_id"]) != int(match.group(1))
            or receipts._sha256(persisted_raw).removeprefix("sha256:") != match.group(2)  # noqa: SLF001
        ):
            raise receipts.ReleaseControlError(
                "dispatch reconciliation checkpoint candidate identity changed"
            )
    for candidate in checked_candidates:
        raw = receipts.canonical_json_bytes(candidate)
        digest = receipts._sha256(raw).removeprefix("sha256:")  # noqa: SLF001
        path = directory / f"candidate-{cast(int, candidate['run_id'])}-{digest}.json"
        if path.exists() or path.is_symlink():
            if (
                _load_canonical_object(path, label="dispatch reconciliation checkpoint candidate")
                != candidate
            ):
                raise receipts.ReleaseControlError(
                    "dispatch reconciliation checkpoint candidate history changed"
                )
        elif not receipts.write_once(path, raw):
            raise receipts.ReleaseControlError(
                "dispatch reconciliation checkpoint candidate creation raced"
            )

    terminal_path = directory / "terminal.json"
    if terminal is None:
        if terminal_path.exists() or terminal_path.is_symlink():
            raise receipts.ReleaseControlError(
                "dispatch reconciliation checkpoint is already terminal"
            )
        return
    checked_terminal = receipts._copy_json_object(  # noqa: SLF001
        terminal,
        label="dispatch reconciliation terminal record",
    )
    receipts._validate_schema(  # noqa: SLF001
        receipts.DISPATCH_RECONCILIATION_SCHEMA,
        checked_terminal,
        label="dispatch reconciliation terminal record",
    )
    terminal_raw = receipts.canonical_json_bytes(checked_terminal)
    if terminal_path.exists() or terminal_path.is_symlink():
        if (
            _load_canonical_object(
                terminal_path, label="dispatch reconciliation terminal checkpoint"
            )
            != checked_terminal
        ):
            raise receipts.ReleaseControlError(
                "dispatch reconciliation terminal checkpoint changed"
            )
    elif not receipts.write_once(terminal_path, terminal_raw):
        raise receipts.ReleaseControlError(
            "dispatch reconciliation terminal checkpoint creation raced"
        )


def _claim_dispatch_terminal_publication(
    *,
    remote_claim: TerminalPublicationClaim,
) -> receipts.JSONObject:
    """Persist the already-selected remote nonce claim before signing."""

    nonce = receipts._nonce(remote_claim.transaction_nonce)  # noqa: SLF001
    kind = remote_claim.kind
    if kind not in {"admission", "tombstone"}:
        raise receipts.ReleaseControlError("dispatch terminal publication kind is invalid")
    record_digest = receipts._digest(  # noqa: SLF001
        remote_claim.record_digest,
        label="dispatch terminal canonical digest",
    )
    expected_ref = f"refs/tags/dispatch-terminal-claim-{nonce}"
    if remote_claim.ref_name != expected_ref:
        raise receipts.ReleaseControlError("dispatch terminal remote claim ref mismatch")
    record: receipts.JSONObject = {
        "schema": "kestrel.dispatch_terminal_publication_claim.v2",
        "transaction_nonce": nonce,
        "kind": kind,
        "canonical_digest": record_digest,
        "remote_claim_ref": expected_ref,
        "remote_tag_object_sha": receipts._git_sha(  # noqa: SLF001
            remote_claim.tag_object_sha,
            label="dispatch terminal remote claim tag SHA",
        ),
        "remote_target_commit_sha": receipts._git_sha(  # noqa: SLF001
            remote_claim.target_commit_sha,
            label="dispatch terminal remote claim target SHA",
        ),
        "channel_locator": (
            f"github-release://John-MiracleWorker/Kestrel-Release-Recovery/dispatch-{kind}-{nonce}"
        ),
        "validation_status": "validated",
    }
    path = _dispatch_state_root() / f"{nonce}.terminal-publication.json"
    if path.exists() or path.is_symlink():
        existing = _load_canonical_object(path, label="dispatch terminal publication claim")
        if existing != record:
            raise receipts.ReleaseControlError(
                "dispatch terminal admission/tombstone publication conflict"
            )
        return record
    if not receipts.write_once(path, receipts.canonical_json_bytes(record)):
        raise receipts.ReleaseControlError("dispatch terminal publication claim creation raced")
    return record


RECOVERY_CHANNEL_REPOSITORY = "John-MiracleWorker/Kestrel-Release-Recovery"
SERVER_AUTHORIZATION_SCHEMA = "kestrel.release_server_authorization.v3"
RELEASE_RECONCILIATION_SCHEMA = "kestrel.release_reconciliation.v2"
RELEASE_PREREQUISITES_SCHEMA = "kestrel.release_prerequisites.v2"
_TERMINAL_RECORD_SCHEMAS = {
    "admission": receipts.DISPATCH_ADMISSION_SCHEMA,
    "tombstone": receipts.DISPATCH_TOMBSTONE_SCHEMA,
}

_RELEASE_STAGE_POLICY: dict[str, tuple[int, tuple[str, ...]]] = {
    "kestrel.release_preparation_outcome.v2": (
        1,
        (
            "create_github_release_draft",
            "publish_ghcr_digests",
            "upload_github_release_assets",
        ),
    ),
    "kestrel.release_commit_outcome.v2": (
        2,
        (
            "attest_github_assets",
            "attest_oci_index_repository",
            "create_tag",
            "publish_github_release_draft",
        ),
    ),
    "kestrel.release_github_ghcr_verification.v2": (3, ()),
    "kestrel.release_pypi_outcome.v2": (
        4,
        ("publish_pypi_missing_files",),
    ),
}

_RELEASE_STAGE_CHAIN = (
    (
        "release-commit-outcome.json",
        "kestrel.release_commit_outcome.v2",
    ),
    (
        "release-github-ghcr-verification.json",
        "kestrel.release_github_ghcr_verification.v2",
    ),
    (
        "release-preparation-outcome.json",
        "kestrel.release_preparation_outcome.v2",
    ),
    (
        "release-pypi-outcome.json",
        "kestrel.release_pypi_outcome.v2",
    ),
)

_RELEASE_STAGE_SEQUENCE = (
    (
        "release-preparation-outcome.json",
        "kestrel.release_preparation_outcome.v2",
    ),
    (
        "release-commit-outcome.json",
        "kestrel.release_commit_outcome.v2",
    ),
    (
        "release-github-ghcr-verification.json",
        "kestrel.release_github_ghcr_verification.v2",
    ),
    (
        "release-pypi-outcome.json",
        "kestrel.release_pypi_outcome.v2",
    ),
)


def validate_server_authorization(
    value: Mapping[str, object],
    *,
    expected_original_transaction_digest: str | None,
    expected_owner_login: str = "John-MiracleWorker",
    expected_owner_user_id: int = 58918509,
) -> receipts.JSONObject:
    """Validate transaction/execution roles and every mode-aware binding."""

    authorization = receipts._copy_json_object(  # noqa: SLF001
        value, label="release server authorization"
    )
    receipts._validate_schema(  # noqa: SLF001
        SERVER_AUTHORIZATION_SCHEMA,
        authorization,
        label="release server authorization",
    )
    kind = authorization.get("authorization_kind")
    mode = authorization.get("mode")
    if (kind, mode) not in {
        ("transaction", "initiate"),
        ("execution", "recover_committed"),
    }:
        raise receipts.ReleaseControlError(
            "release server authorization kind/mode combination is forbidden"
        )
    candidate = receipts._object(  # noqa: SLF001
        authorization.get("candidate"), label="server authorization candidate"
    )
    run = receipts._object(  # noqa: SLF001
        authorization.get("promotion_run"),
        label="server authorization promotion run",
    )
    expected_ref = "refs/heads/main" if mode == "initiate" else f"refs/tags/{candidate['tag']}"
    if (
        run.get("ref") != expected_ref
        or run.get("head_sha") != candidate.get("source_sha")
        or run.get("workflow_sha") != candidate.get("source_sha")
        or run.get("run_attempt") != 1
        or run.get("event") != "workflow_dispatch"
        or run.get("workflow_path") != ".github/workflows/release.yml"
    ):
        raise receipts.ReleaseControlError(
            "release server authorization promotion run binding mismatch"
        )
    environment = receipts._object(  # noqa: SLF001
        authorization.get("environment"), label="server authorization environment"
    )
    if environment.get("name") != "release":
        raise receipts.ReleaseControlError("release server authorization environment mismatch")
    history = receipts._object(  # noqa: SLF001
        authorization.get("approval_history"),
        label="server authorization approval history",
    )
    records = receipts._array(  # noqa: SLF001
        history.get("records"), label="server authorization approval records"
    )
    if len(records) != 1:
        raise receipts.ReleaseControlError(
            "release server authorization approval cardinality mismatch"
        )
    approval = receipts._object(  # noqa: SLF001
        records[0], label="server authorization approval"
    )
    approval_environment = receipts._object(  # noqa: SLF001
        approval.get("environment"), label="server authorization approval environment"
    )
    reviewer = receipts._object(  # noqa: SLF001
        approval.get("reviewer"), label="server authorization reviewer"
    )
    if (
        approval_environment.get("name") != environment.get("name")
        or approval_environment.get("id") != environment.get("id")
        or approval.get("state") != "approved"
        or reviewer.get("login") != expected_owner_login
        or reviewer.get("id") != expected_owner_user_id
        or reviewer.get("type") != "User"
    ):
        raise receipts.ReleaseControlError(
            "release server authorization approval identity mismatch"
        )
    bindings = receipts._object(  # noqa: SLF001
        authorization.get("bindings"), label="server authorization bindings"
    )
    transaction_digest = bindings.get("transaction_authorization_digest")
    capsule_digest = bindings.get("recovery_capsule_manifest_digest")
    marker_digest = bindings.get("commit_marker_digest")
    if mode == "initiate":
        if expected_original_transaction_digest is not None or any(
            item is not None for item in (transaction_digest, capsule_digest, marker_digest)
        ):
            raise receipts.ReleaseControlError(
                "initiate server authorization has recovery bindings"
            )
    else:
        if expected_original_transaction_digest is None:
            raise receipts.ReleaseControlError(
                "recovery server authorization lacks the expected original binding"
            )
        expected_digest = receipts._digest(  # noqa: SLF001
            expected_original_transaction_digest,
            label="expected original transaction authorization digest",
        )
        if transaction_digest != expected_digest or capsule_digest is None or marker_digest is None:
            raise receipts.ReleaseControlError("recovery server authorization binding mismatch")
    receipts.parse_timestamp(
        authorization.get("authorized_at"),
        label="release server authorization time",
    )
    if authorization.get("provenance") != {
        "producer": "scripts/release_promotion_transaction.py",
        "provider": "github.com",
        "method": "server-observation-after-protected-environment",
    }:
        raise receipts.ReleaseControlError("release server authorization provenance mismatch")
    return authorization


def build_server_authorization(
    *,
    candidate: Mapping[str, object],
    promotion_run: Mapping[str, object],
    environment: Mapping[str, object],
    approval_history: Mapping[str, object],
    admission_authority: Mapping[str, object],
    repository_state: Mapping[str, object],
    mode: str,
    transaction_authorization: bytes | None,
    recovery_capsule_manifest_digest: str | None,
    commit_marker_digest: str | None,
    source_records: Mapping[str, bytes],
    expected_owner_login: str = "John-MiracleWorker",
    expected_owner_user_id: int = 58918509,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> receipts.JSONObject:
    """Build the mode-discriminated server authority after environment approval."""

    if mode not in {"initiate", "recover_committed"}:
        raise receipts.ReleaseControlError("server authorization mode is invalid")
    checked_candidate = receipts._copy_json_object(  # noqa: SLF001
        candidate, label="server authorization candidate"
    )
    checked_run = receipts._copy_json_object(  # noqa: SLF001
        promotion_run, label="server authorization promotion run"
    )
    checked_environment = receipts._copy_json_object(  # noqa: SLF001
        environment, label="server authorization environment"
    )
    checked_approval = receipts._copy_json_object(  # noqa: SLF001
        approval_history, label="server authorization approval history"
    )
    checked_admission = receipts._copy_json_object(  # noqa: SLF001
        admission_authority, label="server authorization admission authority"
    )
    checked_repository = receipts._copy_json_object(  # noqa: SLF001
        repository_state, label="server authorization repository state"
    )
    original_digest: str | None
    if mode == "initiate":
        if (
            transaction_authorization is not None
            or recovery_capsule_manifest_digest is not None
            or commit_marker_digest is not None
        ):
            raise receipts.ReleaseControlError(
                "initiate server authorization cannot carry recovery inputs"
            )
        original_digest = None
        bindings: receipts.JSONObject = {
            "transaction_authorization_digest": None,
            "recovery_capsule_manifest_digest": None,
            "commit_marker_digest": None,
        }
        authorization_kind = "transaction"
    else:
        if transaction_authorization is None:
            raise receipts.ReleaseControlError(
                "recovery lacks the original transaction authorization"
            )
        original = _canonical_object(
            transaction_authorization,
            label="original transaction authorization",
        )
        try:
            validate_server_authorization(
                original,
                expected_original_transaction_digest=None,
                expected_owner_login=expected_owner_login,
                expected_owner_user_id=expected_owner_user_id,
            )
        except receipts.ReleaseControlError as exc:
            raise receipts.ReleaseControlError(
                "original transaction authorization is invalid"
            ) from exc
        if (
            original.get("authorization_kind") != "transaction"
            or original.get("mode") != "initiate"
            or original.get("candidate") != checked_candidate
        ):
            raise receipts.ReleaseControlError(
                "original transaction authorization identity mismatch"
            )
        original_digest = receipts._sha256(transaction_authorization)  # noqa: SLF001
        bindings = {
            "transaction_authorization_digest": original_digest,
            "recovery_capsule_manifest_digest": receipts._digest(  # noqa: SLF001
                recovery_capsule_manifest_digest,
                label="recovery capsule manifest digest",
            ),
            "commit_marker_digest": receipts._digest(  # noqa: SLF001
                commit_marker_digest,
                label="commit marker digest",
            ),
        }
        authorization_kind = "execution"
    now = _clock()
    authorized_at = receipts._format_timestamp(  # noqa: SLF001
        now, label="server authorization clock"
    )
    authority: receipts.JSONObject = {
        "schema": SERVER_AUTHORIZATION_SCHEMA,
        "authorization_kind": authorization_kind,
        "mode": mode,
        "candidate": checked_candidate,
        "promotion_run": checked_run,
        "environment": checked_environment,
        "approval_history": checked_approval,
        "admission_authority": checked_admission,
        "repository_state": checked_repository,
        "bindings": bindings,
        "authorized_at": authorized_at,
        "evidence": {
            "source_bundle_digest": receipts.source_bundle_digest(source_records),
            "canonicalization_vector_digest": receipts.canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_promotion_transaction.py",
            "provider": "github.com",
            "method": "server-observation-after-protected-environment",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    validate_server_authorization(
        authority,
        expected_original_transaction_digest=original_digest,
        expected_owner_login=expected_owner_login,
        expected_owner_user_id=expected_owner_user_id,
    )
    if (
        receipts.strict_canonical_json(
            receipts.canonical_json_bytes(authority),
            label="server authorization output",
        )
        != authority
    ):
        raise receipts.ReleaseControlError("server authorization canonical replay mismatch")
    return authority


def validate_release_stage_record(
    value: Mapping[str, object],
) -> receipts.JSONObject:
    """Validate one ordered, fail-closed release-promotion stage record."""

    record = receipts._copy_json_object(value, label="release stage record")  # noqa: SLF001
    schema = record.get("schema")
    if type(schema) is not str or schema not in _RELEASE_STAGE_POLICY:
        raise receipts.ReleaseControlError("release stage schema is unsupported")
    receipts._validate_schema(  # noqa: SLF001
        schema,
        record,
        label="release stage record",
    )
    expected_stage, expected_operations = _RELEASE_STAGE_POLICY[schema]
    if record.get("stage") != expected_stage:
        raise receipts.ReleaseControlError("release stage ordinal mismatch")

    previous = record.get("previous_record_digest")
    if (expected_stage == 1 and previous is not None) or (expected_stage != 1 and previous is None):
        raise receipts.ReleaseControlError("release stage previous-record binding mismatch")
    if schema == "kestrel.release_commit_outcome.v2":
        receipts._digest(  # noqa: SLF001
            record.get("commit_authority_digest"),
            label="release stage commit authority digest",
        )

    if schema == "kestrel.release_github_ghcr_verification.v2":
        results = receipts._array(  # noqa: SLF001
            record.get("verification_results"),
            label="release stage verification results",
        )
        checks = [
            (
                receipts._object(  # noqa: SLF001
                    result, label="release stage verification result"
                ).get("check"),
                receipts._object(  # noqa: SLF001
                    result, label="release stage verification result"
                ).get("subject_digest"),
            )
            for result in results
        ]
        if checks != sorted(checks) or len(checks) != len(set(checks)):
            raise receipts.ReleaseControlError(
                "release stage verification results are not uniquely sorted"
            )
        if record.get("completed") is True and any(
            receipts._object(  # noqa: SLF001
                result, label="release stage verification result"
            ).get("result")
            != "passed"
            for result in results
        ):
            raise receipts.ReleaseControlError(
                "completed release stage has a failed or pending verification"
            )
        expected_provenance = {
            "producer": "scripts/release_promotion_transaction.py",
            "provider": "github.com",
            "method": "read-only-surface-verification",
        }
    else:
        operations = receipts._array(  # noqa: SLF001
            record.get("attempted_operations"),
            label="release stage operations",
        )
        operation_records = [
            receipts._object(operation, label="release stage operation")  # noqa: SLF001
            for operation in operations
        ]
        operation_names = tuple(
            cast(str, operation.get("operation")) for operation in operation_records
        )
        if operation_names != expected_operations:
            raise receipts.ReleaseControlError("release stage operation set or ordering mismatch")
        completed = record.get("completed") is True
        uncertain = record.get("uncertain") is True
        pending = record.get("pending") is True
        if completed and (uncertain or pending):
            raise receipts.ReleaseControlError(
                "completed release stage cannot be uncertain or pending"
            )
        if uncertain and pending:
            raise receipts.ReleaseControlError("release stage cannot be both uncertain and pending")
        if completed and any(
            operation.get("outcome") not in {"created", "existing_exact"}
            or operation.get("response_observation_digest") is None
            for operation in operation_records
        ):
            raise receipts.ReleaseControlError(
                "completed release stage has an unproven operation outcome"
            )
        expected_provenance = {
            "producer": "scripts/release_promotion_transaction.py",
            "provider": "github.com",
            "method": "deterministic-stage-record",
        }

    if record.get("provenance") != expected_provenance:
        raise receipts.ReleaseControlError("release stage provenance mismatch")
    return record


def _require_completed_stage_binding(
    record: Mapping[str, object],
    *,
    candidate: Mapping[str, object],
    transaction_authorization_digest: str,
    execution_authorization_digest: str | None,
    recovery_capsule_digest: str,
    label: str,
) -> None:
    if (
        record.get("candidate") != candidate
        or record.get("transaction_authorization_digest") != transaction_authorization_digest
        or record.get("execution_authorization_digest") != execution_authorization_digest
        or record.get("recovery_capsule_digest") != recovery_capsule_digest
        or record.get("completed") is not True
    ):
        raise receipts.ReleaseControlError(f"{label} authority binding mismatch")


def _require_pypi_authority_binding(
    authority: Mapping[str, object],
    *,
    candidate: Mapping[str, object],
    transaction_authorization_digest: str,
    execution_authorization_digest: str | None,
    recovery_capsule_digest: str,
    github_ghcr_verification_digest: str,
) -> None:
    checked = receipts.validate_pypi_authority(authority)
    bindings = receipts._object(  # noqa: SLF001
        checked.get("bindings"), label="PyPI authority bindings"
    )
    if (
        checked.get("candidate") != candidate
        or bindings.get("transaction_authorization_digest") != transaction_authorization_digest
        or bindings.get("execution_authorization_digest") != execution_authorization_digest
        or bindings.get("recovery_capsule_manifest_digest") != recovery_capsule_digest
        or bindings.get("github_ghcr_verification_digest") != github_ghcr_verification_digest
    ):
        raise receipts.ReleaseControlError("PyPI authority transaction binding mismatch")


def _verified_authority_from_record(
    verification: Mapping[str, object],
    *,
    verification_schema: str,
    authority_schema: str,
    label: str,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> receipts.JSONObject:
    checked = receipts._copy_json_object(verification, label=label)  # noqa: SLF001
    receipts._require_exact_fields(  # noqa: SLF001
        checked,
        frozenset(
            {
                "schema",
                "authority_schema",
                "authority",
                "receipt_digest",
                "signature_digest",
                "receipt_base64",
                "signature_base64",
                "owner_signing_keys_observation_base64",
                "signing_key_fingerprint",
                "verified_at",
                "validation_status",
            }
        ),
        label=label,
    )
    if (
        checked.get("schema") != verification_schema
        or checked.get("authority_schema") != authority_schema
        or checked.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError(f"{label} is invalid")

    def decode(field: str) -> bytes:
        encoded = receipts._validate_string(  # noqa: SLF001
            checked.get(field), label=f"{label} {field}"
        )
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise receipts.ReleaseControlError(f"{label} {field} is invalid base64") from exc
        if base64.b64encode(raw).decode("ascii") != encoded:
            raise receipts.ReleaseControlError(f"{label} {field} is not canonical base64")
        return raw

    receipt = decode("receipt_base64")
    signature = decode("signature_base64")
    owner_keys = decode("owner_signing_keys_observation_base64")
    authority = receipts._object(  # noqa: SLF001
        receipts.strict_canonical_json(receipt, label=f"{label} signed receipt"),
        label=f"{label} authority",
    )
    if checked.get("authority") != authority:
        raise receipts.ReleaseControlError(f"{label} embedded authority differs from receipt bytes")
    validators: dict[str, Callable[[Mapping[str, object]], receipts.JSONObject]] = {
        receipts.GITHUB_AUTHORITY_SCHEMA: receipts.validate_github_authority,
        receipts.PYPI_AUTHORITY_SCHEMA: receipts.validate_pypi_authority,
        receipts.RECOVERY_AUTHORITY_SCHEMA: receipts.validate_recovery_repository_authority,
    }
    validator = validators.get(authority_schema)
    if validator is None:
        raise receipts.ReleaseControlError(f"{label} authority schema is unsupported")
    authority = validator(authority)
    receipt_digest = receipts._digest(  # noqa: SLF001
        checked.get("receipt_digest"), label=f"{label} receipt digest"
    )
    if receipt_digest != receipts._sha256(receipt):  # noqa: SLF001
        raise receipts.ReleaseControlError(f"{label} receipt binding mismatch")
    signature_digest = receipts._digest(  # noqa: SLF001
        checked.get("signature_digest"), label=f"{label} signature digest"
    )
    if signature_digest != receipts._sha256(signature):  # noqa: SLF001
        raise receipts.ReleaseControlError(f"{label} signature bytes do not match its digest")
    signing_fingerprint = receipts._digest(  # noqa: SLF001
        checked.get("signing_key_fingerprint"),
        label=f"{label} signing key fingerprint",
    )
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError(f"{label} clock must be aware UTC")
    now = now.astimezone(UTC).replace(microsecond=0)
    receipts.verify_owner_detached_signature(
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_keys,
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
        _clock=lambda: now,
    )
    if receipts.signature_public_key_fingerprint(signature) != signing_fingerprint:
        raise receipts.ReleaseControlError(f"{label} signing key fingerprint mismatch")
    verified_at = receipts.parse_timestamp(
        checked.get("verified_at"), label=f"{label} verification time"
    )
    observed_at = receipts.parse_timestamp(
        authority.get("observed_at"), label=f"{label} authority observed_at"
    )
    expires_at = receipts.parse_timestamp(
        authority.get("expires_at"), label=f"{label} authority expires_at"
    )
    if verified_at < observed_at or verified_at >= expires_at:
        raise receipts.ReleaseControlError(
            f"{label} verification occurred outside authority lifetime"
        )
    if now < observed_at or now >= expires_at:
        raise receipts.ReleaseControlError(f"{label} authority is not currently fresh")
    return authority


def _require_current_authority(
    authority: Mapping[str, object],
    *,
    label: str,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError(f"{label} clock must be aware UTC")
    now = now.astimezone(UTC).replace(microsecond=0)
    observed_at = receipts.parse_timestamp(
        authority.get("observed_at"), label=f"{label} observed_at"
    )
    expires_at = receipts.parse_timestamp(authority.get("expires_at"), label=f"{label} expires_at")
    if now < observed_at or now >= expires_at:
        raise receipts.ReleaseControlError(f"{label} is not currently fresh")


def _require_github_authority_binding(
    authority: Mapping[str, object],
    *,
    candidate: Mapping[str, object],
    phase: str,
    transaction_authorization_digest: str | None,
    execution_authorization_digest: str | None,
    recovery_capsule_digest: str | None,
    commit_marker_digest: str | None,
) -> None:
    checked = receipts.validate_github_authority(authority)
    bindings = receipts._object(  # noqa: SLF001
        checked.get("bindings"), label="GitHub authority bindings"
    )
    if (
        checked.get("candidate") != candidate
        or checked.get("phase") != phase
        or bindings.get("transaction_authorization_digest") != transaction_authorization_digest
        or bindings.get("execution_authorization_digest") != execution_authorization_digest
        or bindings.get("recovery_capsule_manifest_digest") != recovery_capsule_digest
        or bindings.get("commit_marker_digest") != commit_marker_digest
    ):
        raise receipts.ReleaseControlError("GitHub authority transaction binding mismatch")


def _require_cumulative_owner_approvals(
    history: Mapping[str, object], *, expected_environments: Sequence[str]
) -> None:
    checked = receipts._copy_json_object(  # noqa: SLF001
        history, label="release approval history"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        checked,
        frozenset({"records", "complete_response_digest"}),
        label="release approval history",
    )
    receipts._digest(  # noqa: SLF001
        checked.get("complete_response_digest"),
        label="release approval history response digest",
    )
    records = [
        receipts._object(item, label="release approval record")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            checked.get("records"), label="release approval records"
        )
    ]
    if len(records) != len(expected_environments):
        raise receipts.ReleaseControlError("release approval history cardinality mismatch")
    sort_keys: list[tuple[int, str]] = []
    observed_names: list[str] = []
    for record in records:
        receipts._require_exact_fields(  # noqa: SLF001
            record,
            frozenset({"environment", "reviewer", "state", "observed_record_digest"}),
            label="release approval record",
        )
        environment = receipts._object(  # noqa: SLF001
            record.get("environment"), label="release approval environment"
        )
        reviewer = receipts._object(  # noqa: SLF001
            record.get("reviewer"), label="release approval reviewer"
        )
        receipts._require_exact_fields(  # noqa: SLF001
            environment,
            frozenset({"name", "id"}),
            label="release approval environment",
        )
        receipts._require_exact_fields(  # noqa: SLF001
            reviewer,
            frozenset({"login", "id", "type"}),
            label="release approval reviewer",
        )
        environment_id = receipts._safe_integer(  # noqa: SLF001
            environment.get("id"), label="release approval environment ID", positive=True
        )
        digest = receipts._digest(  # noqa: SLF001
            record.get("observed_record_digest"),
            label="release approval record digest",
        )
        if (
            reviewer
            != {
                "login": "John-MiracleWorker",
                "id": 58918509,
                "type": "User",
            }
            or record.get("state") != "approved"
        ):
            raise receipts.ReleaseControlError(
                "release approval history reviewer identity mismatch"
            )
        observed_names.append(
            receipts._validate_string(  # noqa: SLF001
                environment.get("name"), label="release approval environment name"
            )
        )
        sort_keys.append((environment_id, digest))
    if tuple(observed_names) != tuple(expected_environments):
        raise receipts.ReleaseControlError("release approval environment sequence mismatch")
    if sort_keys != sorted(sort_keys) or len(sort_keys) != len(set(sort_keys)):
        raise receipts.ReleaseControlError("release approval history is not sorted unique")


def build_annotated_tag_message(
    *,
    candidate: Mapping[str, object],
    transaction_authorization_digest: str,
    recovery_capsule_digest: str,
) -> str:
    """Return the exact monotonic commit-marker message."""

    checked_candidate = receipts._copy_json_object(  # noqa: SLF001
        candidate, label="annotated tag candidate"
    )
    tag = receipts._validate_string(  # noqa: SLF001
        checked_candidate.get("tag"), label="annotated tag"
    )
    candidate_digest = receipts._digest(  # noqa: SLF001
        checked_candidate.get("candidate_manifest_digest"),
        label="annotated tag candidate manifest digest",
    )
    artifact_digest = receipts._digest(  # noqa: SLF001
        checked_candidate.get("artifact_set_digest"),
        label="annotated tag artifact set digest",
    )
    transaction_digest = receipts._digest(  # noqa: SLF001
        transaction_authorization_digest,
        label="annotated tag transaction authorization digest",
    )
    capsule_digest = receipts._digest(  # noqa: SLF001
        recovery_capsule_digest, label="annotated tag recovery capsule digest"
    )
    return "\n".join(
        (
            f"Kestrel release {tag}",
            "",
            f"Kestrel-Release-Candidate: {candidate_digest}",
            f"Kestrel-Artifact-Set: {artifact_digest}",
            f"Kestrel-Transaction-Authorization: {transaction_digest}",
            f"Kestrel-Recovery-Capsule: {capsule_digest}",
        )
    )


def _require_committed_recovery_marker(
    *,
    observation: Mapping[str, object],
    candidate: Mapping[str, object],
    transaction_authorization_digest: str,
    recovery_capsule_digest: str,
) -> None:
    checked = receipts._object(  # noqa: SLF001
        receipts.parse_external_json_bytes(
            receipts.canonical_external_json_bytes(observation),
            label="recovery commit marker observation",
        ),
        label="recovery commit marker observation",
    )
    receipts._require_exact_fields(  # noqa: SLF001
        checked,
        frozenset({"ref", "tag", "release"}),
        label="recovery commit marker observation",
    )
    checked_candidate = receipts._copy_json_object(  # noqa: SLF001
        candidate, label="recovery commit marker candidate"
    )
    tag_name = receipts._validate_string(  # noqa: SLF001
        checked_candidate.get("tag"), label="recovery commit marker tag"
    )
    source_sha = receipts._git_sha(  # noqa: SLF001
        checked_candidate.get("source_sha"), label="recovery commit marker source"
    )
    ref = receipts._object(checked.get("ref"), label="recovery tag ref")  # noqa: SLF001
    ref_object = receipts._object(  # noqa: SLF001
        ref.get("object"), label="recovery tag ref object"
    )
    tag = receipts._object(checked.get("tag"), label="recovery annotated tag")  # noqa: SLF001
    tag_object = receipts._object(  # noqa: SLF001
        tag.get("object"), label="recovery annotated tag object"
    )
    release = receipts._object(  # noqa: SLF001
        checked.get("release"), label="recovery product Release"
    )
    expected_message = build_annotated_tag_message(
        candidate=checked_candidate,
        transaction_authorization_digest=transaction_authorization_digest,
        recovery_capsule_digest=recovery_capsule_digest,
    )
    draft_state = release.get("draft") is True and release.get("immutable") is False
    immutable_state = release.get("draft") is False and release.get("immutable") is True
    if (
        ref.get("ref") != f"refs/tags/{tag_name}"
        or ref_object.get("type") != "tag"
        or ref_object.get("sha") != tag.get("sha")
        or tag.get("tag") != tag_name
        or tag.get("message") != expected_message
        or tag_object != {"type": "commit", "sha": source_sha}
        or release.get("tag_name") != tag_name
        or release.get("prerelease") is not False
        or not (draft_state or immutable_state)
    ):
        raise receipts.ReleaseControlError(
            "recovery commit marker, peel, or Release state mismatch"
        )


def build_release_stage_record(
    *,
    schema: str,
    candidate: Mapping[str, object],
    transaction_authorization_digest: str,
    execution_authorization_digest: str | None,
    recovery_capsule_digest: str,
    previous_record_digest: str | None,
    observations_before: Sequence[object] | None,
    observations_after: Sequence[object] | None,
    attempted_operations: Sequence[object] | None,
    fresh_observations: Sequence[object] | None,
    verification_results: Sequence[object] | None,
    commit_authority_digest: str | None,
    completed: bool,
    uncertain: bool | None,
    pending: bool | None,
    source_records: Mapping[str, bytes],
) -> receipts.JSONObject:
    """Construct one canonical stage record from already captured observations."""

    if schema not in _RELEASE_STAGE_POLICY:
        raise receipts.ReleaseControlError("release stage schema is unsupported")
    stage, _operations = _RELEASE_STAGE_POLICY[schema]
    record: receipts.JSONObject = {
        "schema": schema,
        "stage": stage,
        "candidate": receipts._copy_json_object(  # noqa: SLF001
            candidate, label="release stage candidate"
        ),
        "transaction_authorization_digest": receipts._digest(  # noqa: SLF001
            transaction_authorization_digest,
            label="release stage transaction authorization digest",
        ),
        "execution_authorization_digest": (
            None
            if execution_authorization_digest is None
            else receipts._digest(  # noqa: SLF001
                execution_authorization_digest,
                label="release stage execution authorization digest",
            )
        ),
        "recovery_capsule_digest": receipts._digest(  # noqa: SLF001
            recovery_capsule_digest, label="release stage recovery capsule digest"
        ),
        "previous_record_digest": (
            None
            if previous_record_digest is None
            else receipts._digest(  # noqa: SLF001
                previous_record_digest, label="release stage previous record digest"
            )
        ),
        "completed": completed,
        "evidence": {
            "source_bundle_digest": receipts.source_bundle_digest(source_records),
            "canonicalization_vector_digest": receipts.canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_promotion_transaction.py",
            "provider": "github.com",
            "method": (
                "read-only-surface-verification" if stage == 3 else "deterministic-stage-record"
            ),
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    if type(completed) is not bool:
        raise receipts.ReleaseControlError("release stage completion must be boolean")
    if stage == 3:
        if any(
            value is not None
            for value in (
                observations_before,
                observations_after,
                attempted_operations,
                uncertain,
                pending,
                commit_authority_digest,
            )
        ):
            raise receipts.ReleaseControlError(
                "verification release stage received mutation-stage fields"
            )
        if fresh_observations is None or verification_results is None:
            raise receipts.ReleaseControlError(
                "verification release stage lacks fresh verification evidence"
            )
        record["fresh_observations"] = cast(list[receipts.JSONValue], list(fresh_observations))
        record["verification_results"] = cast(list[receipts.JSONValue], list(verification_results))
    else:
        if (
            any(
                value is None
                for value in (
                    observations_before,
                    observations_after,
                    attempted_operations,
                    uncertain,
                    pending,
                )
            )
            or fresh_observations is not None
            or verification_results is not None
        ):
            raise receipts.ReleaseControlError("mutation release stage fields are incomplete")
        record["observations_before"] = cast(
            list[receipts.JSONValue], list(cast(Sequence[object], observations_before))
        )
        record["attempted_operations"] = cast(
            list[receipts.JSONValue], list(cast(Sequence[object], attempted_operations))
        )
        record["observations_after"] = cast(
            list[receipts.JSONValue], list(cast(Sequence[object], observations_after))
        )
        record["uncertain"] = uncertain
        record["pending"] = pending
        if stage == 2:
            record["commit_authority_digest"] = receipts._digest(  # noqa: SLF001
                commit_authority_digest,
                label="release commit authority digest",
            )
        elif commit_authority_digest is not None:
            raise receipts.ReleaseControlError("non-commit release stage has commit authority")
    validate_release_stage_record(record)
    return record


def build_release_stage_plan(
    *,
    stage: int,
    candidate: Mapping[str, object],
    transaction_authorization_digest: str,
    execution_authorization_digest: str | None,
    recovery_capsule_digest: str,
    previous_record_digest: str | None,
    commit_authority_digest: str | None,
    state_observation: Mapping[str, object],
    state_observation_raw: bytes,
    operation_requests: Mapping[str, Mapping[str, object]] | None = None,
) -> receipts.JSONObject:
    """Plan only fixed, idempotent operations from one complete remote snapshot."""

    schemas = {
        policy_stage: schema
        for schema, (policy_stage, _operations) in _RELEASE_STAGE_POLICY.items()
        if policy_stage in {1, 2}
    }
    if stage not in schemas:
        raise receipts.ReleaseControlError("release plan stage is invalid")
    checked_state = receipts._copy_json_object(  # noqa: SLF001
        state_observation, label="release stage state observation"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        checked_state,
        frozenset({"schema", "stage", "operations", "complete"}),
        label="release stage state observation",
    )
    if (
        checked_state.get("schema") != "kestrel.release_stage_state.v1"
        or checked_state.get("stage") != stage
        or checked_state.get("complete") is not True
    ):
        raise receipts.ReleaseControlError("release stage state observation is incomplete")
    state_operations = [
        receipts._object(item, label="release stage state operation")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            checked_state.get("operations"), label="release stage state operations"
        )
    ]
    for item in state_operations:
        receipts._require_exact_fields(  # noqa: SLF001
            item,
            frozenset({"operation", "state"}),
            label="release stage state operation",
        )
    _schema, (_policy_stage, expected_operations) = next(
        item for item in _RELEASE_STAGE_POLICY.items() if item[1][0] == stage
    )
    if tuple(item.get("operation") for item in state_operations) != expected_operations:
        raise receipts.ReleaseControlError("release stage state operation set mismatch")
    checked_requests: dict[str, receipts.JSONObject] = {}
    if operation_requests is not None:
        if set(operation_requests) != set(expected_operations):
            raise receipts.ReleaseControlError(
                "release stage request inventory does not match fixed operations"
            )
        checked_requests = {
            name: receipts._copy_json_object(  # noqa: SLF001
                operation_requests[name],
                label=f"release stage {name} request",
            )
            for name in expected_operations
        }
    states = [item.get("state") for item in state_operations]
    if any(state not in {"missing", "existing_exact"} for state in states):
        raise receipts.ReleaseControlError("release stage remote state conflicts")
    checked_candidate = receipts._copy_json_object(  # noqa: SLF001
        candidate, label="release stage plan candidate"
    )
    transaction_digest = receipts._digest(  # noqa: SLF001
        transaction_authorization_digest,
        label="release stage plan transaction authorization",
    )
    capsule_digest = receipts._digest(  # noqa: SLF001
        recovery_capsule_digest, label="release stage plan recovery capsule"
    )
    operations: list[receipts.JSONValue] = []
    for item in state_operations:
        operation = cast(str, item["operation"])
        request = {
            "candidate": checked_candidate,
            "operation": operation,
            "request": checked_requests.get(operation, {}),
            "transaction_authorization_digest": transaction_digest,
            "recovery_capsule_digest": capsule_digest,
        }
        operations.append(
            {
                "operation": operation,
                "action": ("create" if item.get("state") == "missing" else "no_op"),
                "request_digest": receipts._sha256(  # noqa: SLF001
                    receipts.canonical_json_bytes(request)
                ),
            }
        )
    plan: receipts.JSONObject = {
        "schema": f"kestrel.release_stage_{stage}_plan.v1",
        "stage": stage,
        "candidate": checked_candidate,
        "transaction_authorization_digest": transaction_digest,
        "execution_authorization_digest": (
            None
            if execution_authorization_digest is None
            else receipts._digest(  # noqa: SLF001
                execution_authorization_digest,
                label="release stage plan execution authorization",
            )
        ),
        "recovery_capsule_digest": capsule_digest,
        "previous_record_digest": previous_record_digest,
        "commit_authority_digest": commit_authority_digest,
        "operations": operations,
        "state_observation_digest": receipts._sha256(state_observation_raw),  # noqa: SLF001
        "provenance": {
            "producer": "scripts/release_promotion_transaction.py",
            "provider": "github.com",
            "method": "complete-state-idempotent-plan",
        },
        "validation_status": "validated",
    }
    if (
        stage == 1 and (previous_record_digest is not None or commit_authority_digest is not None)
    ) or (stage == 2 and (previous_record_digest is None or commit_authority_digest is None)):
        raise receipts.ReleaseControlError("release stage plan chain binding mismatch")
    receipts.canonical_json_bytes(plan)
    return plan


def validate_release_reconciliation(
    value: Mapping[str, object],
) -> receipts.JSONObject:
    """Validate final reconciliation and its exact lock-release proof chain."""

    record = receipts._copy_json_object(  # noqa: SLF001
        value, label="release reconciliation"
    )
    receipts._validate_schema(  # noqa: SLF001
        RELEASE_RECONCILIATION_SCHEMA,
        record,
        label="release reconciliation",
    )
    chain = receipts._array(  # noqa: SLF001
        record.get("stage_chain"), label="release reconciliation stage chain"
    )
    chain_records = [
        receipts._object(item, label="release reconciliation stage")  # noqa: SLF001
        for item in chain
    ]
    actual_chain = tuple(
        (cast(str, item.get("filename")), cast(str, item.get("schema"))) for item in chain_records
    )
    if actual_chain != tuple(sorted(actual_chain)):
        raise receipts.ReleaseControlError("release reconciliation stage chain is not sorted")
    if any(item not in _RELEASE_STAGE_CHAIN for item in actual_chain):
        raise receipts.ReleaseControlError(
            "release reconciliation stage chain contains an unknown stage"
        )
    chronological_prefixes = {
        frozenset(_RELEASE_STAGE_SEQUENCE[:length])
        for length in range(len(_RELEASE_STAGE_SEQUENCE) + 1)
    }
    if frozenset(actual_chain) not in chronological_prefixes:
        raise receipts.ReleaseControlError(
            "release reconciliation stage chain is not a chronological prefix"
        )

    candidate_value = record.get("candidate")
    candidate = (
        receipts._object(  # noqa: SLF001
            candidate_value, label="release reconciliation candidate"
        )
        if candidate_value is not None
        else None
    )
    run = receipts._object(  # noqa: SLF001
        record.get("run"), label="release reconciliation run"
    )
    dispatch_inputs = receipts._object(  # noqa: SLF001
        record.get("dispatch_inputs"),
        label="release reconciliation dispatch inputs",
    )
    if dispatch_inputs.get("transaction_nonce") != run.get("transaction_nonce"):
        raise receipts.ReleaseControlError("release reconciliation run/dispatch binding mismatch")
    if candidate is not None and (
        dispatch_inputs.get("candidate_manifest_digest")
        != candidate.get("candidate_manifest_digest")
        or dispatch_inputs.get("candidate_run_id") != str(candidate.get("candidate_run_id"))
        or run.get("head_sha") != candidate.get("source_sha")
        or run.get("workflow_sha") != candidate.get("source_sha")
    ):
        raise receipts.ReleaseControlError("release reconciliation candidate binding mismatch")

    mode = dispatch_inputs.get("mode")
    transaction_digest = record.get("transaction_authorization_digest")
    execution_digest = record.get("execution_authorization_digest")
    capsule_digest = record.get("recovery_capsule_digest")
    if execution_digest is not None and mode != "recover_committed":
        raise receipts.ReleaseControlError("release reconciliation execution mode mismatch")
    if candidate is None:
        if (
            any(
                value is not None
                for value in (transaction_digest, execution_digest, capsule_digest)
            )
            or actual_chain
        ):
            raise receipts.ReleaseControlError(
                "release reconciliation has authority without a candidate"
            )
    else:
        expected_ref = "refs/heads/main" if mode == "initiate" else f"refs/tags/{candidate['tag']}"
        if run.get("ref") != expected_ref:
            raise receipts.ReleaseControlError("release reconciliation run mode mismatch")
        if capsule_digest is not None and transaction_digest is None:
            raise receipts.ReleaseControlError(
                "release reconciliation capsule lacks transaction authority"
            )
        if actual_chain and (transaction_digest is None or capsule_digest is None):
            raise receipts.ReleaseControlError("release reconciliation stage chain lacks authority")

    completed = record.get("completed") is True
    uncertain = record.get("uncertain") is True
    pending = record.get("pending") is True
    failure = record.get("failure_code")
    lock_release = record.get("lock_release_permitted") is True
    if completed and mode == "recover_committed" and execution_digest is None:
        raise receipts.ReleaseControlError(
            "completed recovery reconciliation lacks execution authorization"
        )
    complete_proof = (
        completed
        and not uncertain
        and not pending
        and failure is None
        and candidate is not None
        and transaction_digest is not None
        and capsule_digest is not None
        and (mode == "initiate" or execution_digest is not None)
        and actual_chain == _RELEASE_STAGE_CHAIN
        and record.get("next_action") == "none"
    )
    if lock_release != complete_proof:
        raise receipts.ReleaseControlError(
            "release reconciliation lock-release proof is incomplete"
        )
    if completed and (uncertain or pending or failure is not None):
        raise receipts.ReleaseControlError("completed release reconciliation has unresolved state")
    if record.get("provenance") != {
        "producer": "scripts/release_promotion_transaction.py",
        "provider": "github.com",
        "method": "final-release-reconciliation",
    }:
        raise receipts.ReleaseControlError("release reconciliation provenance mismatch")
    return record


def validate_release_prerequisites(
    value: Mapping[str, object],
) -> receipts.JSONObject:
    """Validate hosted-smoke versus operational prerequisite authority."""

    record = receipts._copy_json_object(  # noqa: SLF001
        value, label="release prerequisites"
    )
    receipts._validate_schema(  # noqa: SLF001
        RELEASE_PREREQUISITES_SCHEMA,
        record,
        label="release prerequisites",
    )
    repository = receipts._object(  # noqa: SLF001
        record.get("repository"), label="prerequisite repository"
    )
    if repository.get("full_name") != "John-MiracleWorker/Kestrel":
        raise receipts.ReleaseControlError("release prerequisite repository mismatch")
    writers = [
        receipts._object(item, label="prerequisite repository writer")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            record.get("repository_writers"), label="prerequisite writers"
        )
    ]
    if (
        len(writers) != 1
        or writers[0].get("login") != "John-MiracleWorker"
        or writers[0].get("type") != "User"
        or writers[0].get("role_name") != "admin"
    ):
        raise receipts.ReleaseControlError(
            "release prerequisite repository writer authority mismatch"
        )
    workflows = receipts._array(  # noqa: SLF001
        record.get("workflow_inventory"), label="prerequisite workflows"
    )
    if (
        len(workflows) != 1
        or receipts._object(  # noqa: SLF001
            workflows[0], label="prerequisite workflow"
        ).get("path")
        != ".github/workflows/release.yml"
    ):
        raise receipts.ReleaseControlError("release prerequisite workflow mismatch")
    workflow = receipts._object(  # noqa: SLF001
        workflows[0], label="prerequisite workflow"
    )
    main = receipts._object(record.get("main_branch"), label="prerequisite main")  # noqa: SLF001
    default = receipts._object(  # noqa: SLF001
        record.get("default_branch"), label="prerequisite default branch"
    )
    if main.get("name") != "main" or default.get("name") != "main":
        raise receipts.ReleaseControlError("release prerequisite main branch mismatch")
    environments = [
        receipts._object(item, label="prerequisite environment")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            record.get("environments"), label="prerequisite environments"
        )
    ]
    environment_keys = [
        (cast(int, item.get("id")), cast(str, item.get("name"))) for item in environments
    ]
    if (
        environment_keys != sorted(environment_keys)
        or len({item[0] for item in environment_keys}) != len(environment_keys)
        or {item.get("name") for item in environments}
        != {"release", "release-prepare", "release-commit", "pypi"}
        or any(
            item.get("reviewer_login") != "John-MiracleWorker"
            or item.get("prevent_self_review") is not True
            for item in environments
        )
    ):
        raise receipts.ReleaseControlError("release prerequisite environment authority mismatch")
    recovery = receipts._object(  # noqa: SLF001
        record.get("recovery_repository"), label="prerequisite recovery repository"
    )
    ingress = receipts._object(  # noqa: SLF001
        record.get("ingress_observation"), label="prerequisite ingress"
    )
    immutable = receipts._object(  # noqa: SLF001
        record.get("immutable_releases"), label="prerequisite immutable Releases"
    )
    mode = record.get("mode")
    blockers = record.get("operational_blockers")
    if mode == "hosted-smoke":
        expected_blockers = [
            "environment_policy_types_unverified",
            "github_authority_unprovisioned",
            "pypi_authority_unprovisioned",
            "recovery_authority_unprovisioned",
        ]
        if (
            blockers != expected_blockers
            or record.get("validation_status") != "validated_for_hosted_smoke"
            or recovery.get("authority_digest") is not None
            or recovery.get("immutable_releases") is not False
            or (
                workflow.get("state") == "unverified"
                and (
                    workflow.get("id") is not None
                    or ingress.get("ruleset_id") is not None
                    or ingress.get("active") is not False
                    or ingress.get("workflow_byte_equal") is not False
                )
            )
            or workflow.get("state") not in {"active", "unverified"}
            or (workflow.get("state") == "active" and type(workflow.get("id")) is not int)
        ):
            raise receipts.ReleaseControlError(
                "hosted-smoke release prerequisite blocker policy mismatch"
            )
    else:
        if (
            blockers != []
            or record.get("validation_status") != "validated_operational"
            or recovery.get("authority_digest") is None
            or recovery.get("immutable_releases") is not True
            or immutable.get("enabled") is not True
            or immutable.get("observation_digest") is None
            or ingress.get("active") is not True
            or ingress.get("workflow_byte_equal") is not True
            or workflow.get("state") != "active"
            or type(workflow.get("id")) is not int
            or type(ingress.get("ruleset_id")) is not int
        ):
            raise receipts.ReleaseControlError(
                "operational release prerequisite authority is incomplete"
            )
    if record.get("provenance") != {
        "producer": "scripts/release_promotion_transaction.py",
        "provider": "github.com",
        "method": "complete-prerequisite-inspection",
    }:
        raise receipts.ReleaseControlError("release prerequisite provenance mismatch")
    return record


def _terminal_release_value(release: TerminalRelease) -> receipts.JSONObject:
    return {
        "release_id": release.release_id,
        "tag_name": release.tag_name,
        "name": release.name,
        "body": release.body,
        "draft": release.draft,
        "prerelease": release.prerelease,
        "immutable": release.immutable,
        "html_url": release.html_url,
        "assets": [
            {
                "asset_id": asset.asset_id,
                "name": asset.name,
                "size_bytes": asset.size_bytes,
                "digest": asset.digest,
                "media_type": asset.media_type,
            }
            for asset in release.assets
        ],
    }


def _terminal_stage_path(journal_path: Path, stage: str) -> Path:
    return journal_path.with_name(f"{journal_path.stem}.{stage}.json")


def _persist_terminal_stage(*, journal_path: Path, stage: str, release: TerminalRelease) -> None:
    path = _terminal_stage_path(journal_path, stage)
    if path.exists() or path.is_symlink():
        existing = _load_canonical_object(path, label="dispatch terminal publication stage")
        receipts._require_exact_fields(  # noqa: SLF001
            existing,
            frozenset(
                {
                    "schema",
                    "stage",
                    "release_id",
                    "release_digest",
                    "validation_status",
                }
            ),
            label="dispatch terminal publication stage",
        )
        if (
            existing.get("schema") != "kestrel.dispatch_terminal_publication_stage.v1"
            or existing.get("stage") != stage
            or existing.get("release_id") != release.release_id
            or existing.get("validation_status") != "validated"
        ):
            raise receipts.ReleaseControlError("dispatch terminal publication stage conflicts")
        receipts._digest(  # noqa: SLF001
            existing.get("release_digest"),
            label="dispatch terminal publication stage Release digest",
        )
        return
    value: receipts.JSONObject = {
        "schema": "kestrel.dispatch_terminal_publication_stage.v1",
        "stage": stage,
        "release_id": release.release_id,
        "release_digest": receipts._sha256(  # noqa: SLF001
            receipts.canonical_json_bytes(_terminal_release_value(release))
        ),
        "validation_status": "validated",
    }
    receipts.write_once(
        path,
        receipts.canonical_json_bytes(value),
    )


def _inspect_terminal_releases(
    *,
    listing: TerminalReleaseListing,
    target_tag: str,
    opposite_tag: str,
    expected_name: str,
    expected_body: str,
    expected_assets: Mapping[str, tuple[bytes, str]],
) -> TerminalRelease | None:
    if listing.complete is not True:
        raise receipts.ReleaseControlError("dispatch terminal Release listing is incomplete")
    release_ids: set[int] = set()
    for release in listing.releases:
        if type(release.release_id) is not int or release.release_id <= 0:
            raise receipts.ReleaseControlError("dispatch terminal Release ID is invalid")
        if release.release_id in release_ids:
            raise receipts.ReleaseControlError(
                "dispatch terminal Release listing has duplicate IDs"
            )
        release_ids.add(release.release_id)
    opposite = [release for release in listing.releases if release.tag_name == opposite_tag]
    if opposite:
        raise receipts.ReleaseControlError(
            "opposite dispatch terminal admission/tombstone Release already exists"
        )
    matches = [release for release in listing.releases if release.tag_name == target_tag]
    if len(matches) > 1:
        raise receipts.ReleaseControlError("dispatch terminal Release tag is ambiguous")
    if not matches:
        return None
    release = matches[0]
    if (
        release.name != expected_name
        or release.body != expected_body
        or release.prerelease is not False
        or not release.html_url
        or (release.draft is release.immutable)
    ):
        raise receipts.ReleaseControlError(
            "dispatch terminal Release identity or immutable state conflicts"
        )
    asset_names = [asset.name for asset in release.assets]
    if asset_names != sorted(asset_names) or len(asset_names) != len(set(asset_names)):
        raise receipts.ReleaseControlError(
            "dispatch terminal Release assets are unsorted or duplicated"
        )
    if not set(asset_names).issubset(expected_assets):
        raise receipts.ReleaseControlError("dispatch terminal Release contains an unexpected asset")
    asset_ids: set[int] = set()
    for asset in release.assets:
        if type(asset.asset_id) is not int or asset.asset_id <= 0:
            raise receipts.ReleaseControlError("dispatch terminal Release asset ID is invalid")
        if asset.asset_id in asset_ids:
            raise receipts.ReleaseControlError("dispatch terminal Release has duplicate asset IDs")
        asset_ids.add(asset.asset_id)
        content, media_type = expected_assets[asset.name]
        if (
            asset.size_bytes != len(content)
            or asset.digest != receipts._sha256(content)  # noqa: SLF001
            or asset.media_type != media_type
        ):
            raise receipts.ReleaseControlError("dispatch terminal Release asset identity conflicts")
    if release.immutable and set(asset_names) != set(expected_assets):
        raise receipts.ReleaseControlError(
            "immutable dispatch terminal Release asset set is incomplete"
        )
    return release


def publish_dispatch_terminal_release(
    *,
    kind: str,
    record: bytes,
    signature: bytes,
    expected_signing_key_fingerprint: str,
    claim: TerminalPublicationClaim,
    journal_path: Path,
    api: TerminalReleaseAPI,
) -> receipts.JSONObject:
    """Create or crash-resume one exact immutable nonce terminal Release."""

    if kind not in _TERMINAL_RECORD_SCHEMAS:
        raise receipts.ReleaseControlError("dispatch terminal publication kind is invalid")
    value = receipts.strict_canonical_json(record, label="dispatch terminal record")
    checked = receipts._object(value, label="dispatch terminal record")  # noqa: SLF001
    receipts._validate_schema(  # noqa: SLF001
        _TERMINAL_RECORD_SCHEMAS[kind],
        checked,
        label="dispatch terminal record",
    )
    if checked.get("schema") != _TERMINAL_RECORD_SCHEMAS[kind]:
        raise receipts.ReleaseControlError("dispatch terminal record schema mismatch")
    if not receipts.verify_detached_signature(
        receipt=record,
        signature=signature,
        expected_fingerprint=expected_signing_key_fingerprint,
        namespace=receipts.SIGNING_NAMESPACE,
    ):
        raise receipts.ReleaseControlError("dispatch terminal signature is invalid")
    if (
        kind == "admission"
        and checked.get("signing_key_fingerprint") != expected_signing_key_fingerprint
    ):
        raise receipts.ReleaseControlError("dispatch admission signing fingerprint mismatch")
    nonce = receipts._nonce(checked.get("transaction_nonce"))  # noqa: SLF001
    record_digest = receipts._sha256(record)  # noqa: SLF001
    if (
        claim.transaction_nonce != nonce
        or claim.kind != kind
        or claim.record_digest != record_digest
        or claim.ref_name != f"refs/tags/dispatch-terminal-claim-{nonce}"
    ):
        raise receipts.ReleaseControlError(
            "dispatch terminal publication does not match its remote atomic claim"
        )
    receipts._git_sha(  # noqa: SLF001
        claim.tag_object_sha, label="dispatch terminal claim tag object SHA"
    )
    receipts._git_sha(  # noqa: SLF001
        claim.target_commit_sha, label="dispatch terminal claim target SHA"
    )
    schema_name = _TERMINAL_RECORD_SCHEMAS[kind]
    tag_name = f"dispatch-{kind}-{nonce}"
    opposite_kind = "tombstone" if kind == "admission" else "admission"
    opposite_tag = f"dispatch-{opposite_kind}-{nonce}"
    release_name = tag_name
    release_body = f"Kestrel dispatch {kind} {nonce}"
    expected_assets = {
        f"{schema_name}.json": (record, "application/json"),
        f"{schema_name}.json.sig": (signature, "application/octet-stream"),
    }
    journal: receipts.JSONObject = {
        "schema": "kestrel.dispatch_terminal_publication_journal.v1",
        "repository": RECOVERY_CHANNEL_REPOSITORY,
        "kind": kind,
        "transaction_nonce": nonce,
        "tag_name": tag_name,
        "name": release_name,
        "body": release_body,
        "remote_claim": {
            "ref_name": claim.ref_name,
            "tag_object_sha": claim.tag_object_sha,
            "target_commit_sha": claim.target_commit_sha,
            "record_digest": claim.record_digest,
        },
        "record_schema": schema_name,
        "assets": [
            {
                "name": name,
                "sha256": receipts._sha256(content),  # noqa: SLF001
                "size_bytes": len(content),
                "media_type": media_type,
            }
            for name, (content, media_type) in sorted(expected_assets.items())
        ],
        "validation_status": "validated",
    }
    journal_raw = receipts.canonical_json_bytes(journal)
    if journal_path.exists() or journal_path.is_symlink():
        if (
            _load_canonical_object(journal_path, label="dispatch terminal publication journal")
            != journal
        ):
            raise receipts.ReleaseControlError("dispatch terminal publication journal conflicts")
    elif not receipts.write_once(journal_path, journal_raw):
        raise receipts.ReleaseControlError("dispatch terminal publication journal creation raced")

    def observe() -> TerminalRelease | None:
        return _inspect_terminal_releases(
            listing=api.list_releases(RECOVERY_CHANNEL_REPOSITORY),
            target_tag=tag_name,
            opposite_tag=opposite_tag,
            expected_name=release_name,
            expected_body=release_body,
            expected_assets=expected_assets,
        )

    release = observe()
    if release is None:
        created_release_id = api.create_draft(
            RECOVERY_CHANNEL_REPOSITORY,
            tag_name=tag_name,
            name=release_name,
            body=release_body,
        )
        release = observe()
        if release is None or not release.draft or release.release_id != created_release_id:
            raise receipts.ReleaseControlError(
                "dispatch terminal draft creation was not observed at its returned Release ID"
            )
    _persist_terminal_stage(journal_path=journal_path, stage="created", release=release)

    for ordinal, (asset_name, (content, media_type)) in enumerate(
        sorted(expected_assets.items()), start=1
    ):
        release = observe()
        if release is None:
            raise receipts.ReleaseControlError(
                "dispatch terminal Release disappeared during publication"
            )
        present = {asset.name for asset in release.assets}
        if asset_name not in present:
            if not release.draft:
                raise receipts.ReleaseControlError(
                    "published dispatch terminal Release is missing an asset"
                )
            api.upload_asset(
                RECOVERY_CHANNEL_REPOSITORY,
                release_id=release.release_id,
                name=asset_name,
                media_type=media_type,
                content=content,
            )
            release = observe()
            if release is None or asset_name not in {asset.name for asset in release.assets}:
                raise receipts.ReleaseControlError(
                    "dispatch terminal asset upload was not observed exactly"
                )
        _persist_terminal_stage(
            journal_path=journal_path,
            stage=f"asset-{ordinal}",
            release=release,
        )

    release = observe()
    if release is None:
        raise receipts.ReleaseControlError(
            "dispatch terminal Release disappeared before publication"
        )
    if release.draft:
        api.publish_immutable(
            RECOVERY_CHANNEL_REPOSITORY,
            release_id=release.release_id,
        )
        release = observe()
    if release is None or release.draft or not release.immutable:
        raise receipts.ReleaseControlError(
            "dispatch terminal Release is not immutable after publication"
        )
    if {asset.name for asset in release.assets} != set(expected_assets):
        raise receipts.ReleaseControlError("dispatch terminal immutable asset set changed")
    _persist_terminal_stage(journal_path=journal_path, stage="published", release=release)
    return {
        "schema": "kestrel.dispatch_terminal_publication_receipt.v1",
        "repository": RECOVERY_CHANNEL_REPOSITORY,
        "release_id": release.release_id,
        "tag_name": release.tag_name,
        "html_url": release.html_url,
        "immutable": True,
        "asset_names": cast(list[receipts.JSONValue], sorted(expected_assets)),
        "record_digest": record_digest,
        "signature_digest": receipts._sha256(signature),  # noqa: SLF001
        "validation_status": "validated",
    }


def _validate_exact_request(
    *, journal: receipts.JSONObject, request_path: Path
) -> tuple[receipts.JSONObject, bytes]:
    raw = receipts._read_regular(  # noqa: SLF001
        request_path,
        label="dispatch request",
        max_bytes=1024 * 1024,
    )
    value = receipts.strict_canonical_json(raw, label="dispatch request")
    if type(value) is not dict:
        raise receipts.ReleaseControlError("dispatch request must be an object")
    request = value
    if set(request) != {"ref", "inputs"}:
        raise receipts.ReleaseControlError("dispatch request fields mismatch")
    target = journal.get("target")
    inputs = journal.get("inputs")
    if type(target) is not dict or type(inputs) is not dict:
        raise receipts.ReleaseControlError("dispatch journal request bindings are invalid")
    if request.get("ref") != target.get("short_ref") or request.get("inputs") != inputs:
        raise receipts.ReleaseControlError("dispatch request does not match its journal")
    if receipts._sha256(raw) != journal.get("canonical_request_sha256"):  # noqa: SLF001
        raise receipts.ReleaseControlError("dispatch request digest mismatch")
    return request, raw


def _ensure_unsent_outputs(*, boundary_path: Path, response_output: Path) -> None:
    if boundary_path.exists() or boundary_path.is_symlink():
        raise receipts.ReleaseControlError("dispatch was already attempted")
    if response_output.exists() or response_output.is_symlink():
        raise receipts.ReleaseControlError("dispatch response output already exists")
    for path, label in (
        (boundary_path, "dispatch send boundary"),
        (response_output, "dispatch response output"),
    ):
        if not path.parent.is_dir() or path.parent.is_symlink():
            raise receipts.ReleaseControlError(f"{label} parent is invalid")


def send_dispatch_once(
    *,
    journal_path: Path,
    request_path: Path,
    response_output: Path,
    transport: OneWireTransport,
    credential_fingerprint: str,
    writer_inventory: bytes,
    writer_inventory_signature: bytes,
    owner_signing_keys_observation: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    _monotonic: Callable[[], float] = lambda: time.monotonic(),
) -> receipts.JSONObject:
    """Persist the send boundary, invoke transport once, and never retry it."""

    journal = receipts._validate_dispatch_journal(  # noqa: SLF001
        _load_canonical_object(journal_path, label="dispatch journal")
    )
    monotonic_now = _monotonic()
    if (
        type(monotonic_now) not in {int, float}
        or not math.isfinite(monotonic_now)
        or monotonic_now < 0
    ):
        raise receipts.ReleaseControlError("dispatch monotonic clock is invalid")
    monotonic_started = cast(int, journal["monotonic_started_seconds"])
    monotonic_deadline = cast(int, journal["monotonic_deadline_seconds"])
    if not monotonic_started <= monotonic_now <= monotonic_deadline:
        raise receipts.ReleaseControlError("dispatch journal monotonic send deadline expired")
    _, request_raw = _validate_exact_request(journal=journal, request_path=request_path)
    token_fingerprint = receipts._digest(  # noqa: SLF001
        credential_fingerprint,
        label="dispatch credential fingerprint",
    )
    boundary_path = _send_boundary_path(journal_path)
    nonce_boundary_path = _nonce_send_boundary_path(journal)
    if nonce_boundary_path.exists() or nonce_boundary_path.is_symlink():
        raise receipts.ReleaseControlError("dispatch was already attempted")
    _ensure_unsent_outputs(boundary_path=boundary_path, response_output=response_output)
    started = _clock()
    started_at = receipts._format_timestamp(  # noqa: SLF001
        started, label="dispatch send clock"
    )
    started_datetime = receipts.parse_timestamp(started_at, label="dispatch send clock")
    prepared_datetime = receipts.parse_timestamp(
        journal.get("prepared_at"), label="dispatch prepared_at"
    )
    if not (
        prepared_datetime
        <= started_datetime
        <= prepared_datetime + timedelta(seconds=receipts.DISPATCH_RECONCILIATION_SECONDS)
    ):
        raise receipts.ReleaseControlError("dispatch journal wall-clock send deadline expired")
    receipts.verify_repository_writer_inventory(
        inventory=writer_inventory,
        signature=writer_inventory_signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        journal=journal,
        phase="pre_send",
        expected_run_id=None,
        _clock=lambda: started,
    )
    boundary: receipts.JSONObject = {
        "schema": "kestrel.dispatch_send_boundary.v1",
        "state": "sending",
        "transaction_nonce": journal["transaction_nonce"],
        "journal_digest": receipts._sha256(  # noqa: SLF001
            receipts.canonical_json_bytes(journal)
        ),
        "request_digest": receipts._sha256(request_raw),  # noqa: SLF001
        "started_at": started_at,
        "token_fingerprint": token_fingerprint,
        "pre_send_writer_inventory_digest": receipts._sha256(  # noqa: SLF001
            writer_inventory
        ),
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
    created = receipts.write_once(nonce_boundary_path, receipts.canonical_json_bytes(boundary))
    if not created:
        raise receipts.ReleaseControlError("dispatch was already attempted")
    created = receipts.write_once(boundary_path, receipts.canonical_json_bytes(boundary))
    if not created:
        raise receipts.ReleaseControlError("dispatch was already attempted")

    policy = OneWirePolicy()
    headers = {
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    try:
        exchange = transport(cast(str, journal["endpoint"]), headers, request_raw, policy)
    except DispatchTransportError as exc:
        result = receipts.classify_dispatch_transport(
            journal=journal,
            http_status=None,
            response_headers=None,
            response_body=None,
            response_observed_at=None,
            locally_proven_prewrite_failure=not exc.request_may_have_reached_peer,
            send_started_at=started,
        )
    else:
        if type(exchange) is not DispatchExchange:
            raise receipts.ReleaseControlError("dispatch transport returned an invalid exchange")
        if exchange.http_status is not None and not exchange.request_may_have_reached_peer:
            raise receipts.ReleaseControlError(
                "dispatch transport response contradicts its possible-write state"
            )
        observed = _clock() if exchange.http_status is not None else None
        result = receipts.classify_dispatch_transport(
            journal=journal,
            http_status=exchange.http_status,
            response_headers=exchange.response_headers,
            response_body=exchange.response_body,
            response_observed_at=observed,
            locally_proven_prewrite_failure=(
                exchange.http_status is None and not exchange.request_may_have_reached_peer
            ),
            send_started_at=started,
        )
    receipts.write_once(response_output, receipts.canonical_json_bytes(result))
    return result


def _read_observation_or_record(path: Path, *, label: str) -> bytes:
    raw = receipts._read_regular(  # noqa: SLF001
        path,
        label=label,
        max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
    )
    value = receipts.parse_external_json_bytes(raw, label=label)
    if type(value) is dict and value.get("schema") == receipts.SOURCE_OBSERVATION_SCHEMA:
        receipts.strict_canonical_json(raw, label=label)
        return receipts.source_observation_body(raw)
    if (
        type(value) is dict
        and type(value.get("schema")) is str
        and cast(str, value["schema"]).startswith("kestrel.")
    ):
        receipts.strict_canonical_json(raw, label=label)
    return raw


def _source_registry() -> receipts.JSONObject:
    return receipts._object(  # noqa: SLF001
        receipts._load_canonical_file(  # noqa: SLF001
            receipts.SOURCE_REGISTRY_PATH,
            label="release-control source registry",
            max_bytes=4 * 1024 * 1024,
        ),
        label="release-control source registry",
    )


def _contract_source_body(
    raw: bytes,
    *,
    label: str,
    receipt_schema: str,
    phase: str | None,
    mode: str | None,
    name: str,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bytes:
    registry = _source_registry()
    body = receipts.source_observation_body_for_contract(
        raw,
        registry=registry,
        receipt_schema=receipt_schema,
        phase=phase,
        mode=mode,
        name=name,
        _clock=_clock,
    )
    matching_entries = [
        entry
        for entry in receipts._array(  # noqa: SLF001
            registry.get("entries"), label="release-control source registry entries"
        )
        if type(entry) is dict
        and entry.get("receipt_schema") == receipt_schema
        and entry.get("phase") == phase
        and entry.get("mode") == mode
        and entry.get("name") == name
    ]
    if len(matching_entries) != 1:
        raise receipts.ReleaseControlError(
            "source contract registry lookup must select exactly one entry"
        )
    if matching_entries[0].get("body_mode") == "paginated-json":
        parsed = receipts._object(  # noqa: SLF001
            receipts.parse_external_json_bytes(body, label=label),
            label=f"{label} pagination wrapper",
        )
        pages = receipts._array(parsed.get("pages"), label=f"{label} pages")  # noqa: SLF001
        bodies = [
            receipts._object(page, label=f"{label} page").get("body")  # noqa: SLF001
            for page in pages
        ]
        return receipts.canonical_external_json_bytes(bodies)
    return body


def _read_contract_source(
    path: Path,
    *,
    label: str,
    receipt_schema: str,
    phase: str | None,
    mode: str | None,
    name: str,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bytes:
    raw = receipts._read_regular(  # noqa: SLF001
        path,
        label=label,
        max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
    )
    return _contract_source_body(
        raw,
        label=label,
        receipt_schema=receipt_schema,
        phase=phase,
        mode=mode,
        name=name,
        _clock=_clock,
    )


def _read_dispatch_token() -> bytes:
    raw = sys.stdin.buffer.read(MAX_DISPATCH_TOKEN_BYTES + 1)
    if not raw or len(raw) > MAX_DISPATCH_TOKEN_BYTES:
        raise receipts.ReleaseControlError(
            "dispatch credential was not provided within its size limit"
        )
    return raw


def _prepare_from_args(
    args: argparse.Namespace,
) -> tuple[receipts.JSONObject, receipts.JSONObject, receipts.JSONObject]:
    mode = cast(str, args.mode)
    return prepare_dispatch_from_observations(
        repository_observation=_read_contract_source(
            Path(args.repository_observation),
            label="dispatch repository observation",
            receipt_schema=receipts.DISPATCH_INTENT_SCHEMA,
            phase="prepare",
            mode=mode,
            name="repository-rest",
        ),
        workflow_observation=_read_contract_source(
            Path(args.workflow_observation),
            label="dispatch workflow observation",
            receipt_schema=receipts.DISPATCH_INTENT_SCHEMA,
            phase="prepare",
            mode=mode,
            name="workflow-rest",
        ),
        default_branch_workflow_contents=_read_contract_source(
            Path(args.default_branch_workflow_contents),
            label="default-branch workflow contents",
            receipt_schema=receipts.DISPATCH_INTENT_SCHEMA,
            phase="prepare",
            mode=mode,
            name="default-branch-workflow-contents",
        ),
        candidate_workflow_contents=_read_contract_source(
            Path(args.candidate_workflow_contents),
            label="candidate workflow contents",
            receipt_schema=receipts.DISPATCH_INTENT_SCHEMA,
            phase="prepare",
            mode=mode,
            name="candidate-workflow-contents",
        ),
        candidate_manifest=_read_observation_or_record(
            Path(args.candidate_manifest),
            label="candidate manifest",
        ),
        mode=mode,
        dispatcher_observation=_read_contract_source(
            Path(args.dispatcher_observation),
            label="dispatcher observation",
            receipt_schema=receipts.DISPATCH_INTENT_SCHEMA,
            phase="prepare",
            mode=mode,
            name="dispatcher-observation",
        ),
        prior_intents_observation=_read_contract_source(
            Path(args.prior_intents_observation),
            label="prior dispatch intents observation",
            receipt_schema=receipts.DISPATCH_INTENT_SCHEMA,
            phase="prepare",
            mode=mode,
            name="prior-intents-observation",
        ),
    )


def _require_new_outputs(paths: Sequence[Path]) -> None:
    if len(set(paths)) != len(paths):
        raise receipts.ReleaseControlError("dispatch output paths must be distinct")
    for path in paths:
        if path.exists() or path.is_symlink():
            raise receipts.ReleaseControlError(f"dispatch output already exists: {path}")
        if not path.parent.is_dir() or path.parent.is_symlink():
            raise receipts.ReleaseControlError("dispatch output parent is invalid")


def _write_prepared_records(
    *,
    journal: receipts.JSONObject,
    intent: receipts.JSONObject,
    request: receipts.JSONObject,
    journal_output: Path,
    intent_output: Path,
    request_output: Path,
) -> None:
    outputs = (journal_output, intent_output, request_output)
    raws = (
        receipts.canonical_json_bytes(journal),
        receipts.canonical_json_bytes(intent),
        receipts.canonical_json_bytes(request),
    )
    if len(set(outputs)) != len(outputs):
        raise receipts.ReleaseControlError("dispatch output paths must be distinct")
    for path, raw in zip(outputs, raws, strict=True):
        if not path.parent.is_dir() or path.parent.is_symlink():
            raise receipts.ReleaseControlError("dispatch output parent is invalid")
        if path.exists() or path.is_symlink():
            if (
                not path.is_file()
                or path.is_symlink()
                or receipts._read_regular(  # noqa: SLF001
                    path,
                    label="existing prepared dispatch record",
                    max_bytes=len(raw),
                )
                != raw
            ):
                raise receipts.ReleaseControlError(f"prepared dispatch output conflict: {path}")
    for path, raw in zip(outputs, raws, strict=True):
        receipts.write_once(path, raw)


def _preparation_stage_paths(journal_output: Path) -> tuple[Path, Path]:
    return (
        journal_output.with_name(f".{journal_output.name}.preparation-stage.json"),
        journal_output.with_name(f".{journal_output.name}.preparation-complete.json"),
    )


def _validated_preparation_stage(
    value: Mapping[str, object],
) -> tuple[receipts.JSONObject, receipts.JSONObject, receipts.JSONObject]:
    stage = receipts._copy_json_object(  # noqa: SLF001
        value, label="dispatch preparation stage"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        stage,
        frozenset({"schema", "journal", "intent", "request", "validation_status"}),
        label="dispatch preparation stage",
    )
    if (
        stage.get("schema") != "kestrel.dispatch_preparation_stage.v1"
        or stage.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("dispatch preparation stage is invalid")
    journal = receipts._validate_dispatch_journal(  # noqa: SLF001
        receipts._object(stage.get("journal"), label="staged dispatch journal")  # noqa: SLF001
    )
    intent = receipts._validate_dispatch_intent(  # noqa: SLF001
        receipts._object(stage.get("intent"), label="staged dispatch intent")  # noqa: SLF001
    )
    request = receipts._object(  # noqa: SLF001
        stage.get("request"), label="staged dispatch request"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        request,
        frozenset({"ref", "inputs"}),
        label="staged dispatch request",
    )
    target = receipts._object(  # noqa: SLF001
        journal.get("target"), label="staged dispatch target"
    )
    if request != {"ref": target["short_ref"], "inputs": journal["inputs"]}:
        raise receipts.ReleaseControlError("dispatch preparation stage request binding mismatch")
    journal_bytes = receipts.canonical_json_bytes(journal)
    request_bytes = receipts.canonical_json_bytes(request)
    if (
        intent.get("transaction_digest") != receipts._sha256(journal_bytes)  # noqa: SLF001
        or intent.get("request_digest") != receipts._sha256(request_bytes)  # noqa: SLF001
        or journal.get("canonical_request_sha256") != receipts._sha256(request_bytes)  # noqa: SLF001
    ):
        raise receipts.ReleaseControlError("dispatch preparation stage digest binding mismatch")
    for field in (
        "transaction_nonce",
        "dispatch_binding",
        "repository",
        "workflow",
        "target",
        "actor",
        "inputs",
        "expected_display_title",
        "evidence",
    ):
        if intent.get(field) != journal.get(field):
            raise receipts.ReleaseControlError(
                f"dispatch preparation stage {field} binding mismatch"
            )
    return journal, intent, request


def _load_or_create_preparation_stage(
    *,
    args: argparse.Namespace,
    journal_output: Path,
    outputs: Sequence[Path],
) -> tuple[
    receipts.JSONObject,
    receipts.JSONObject,
    receipts.JSONObject,
    Path,
    Path,
]:
    stage_path, completion_path = _preparation_stage_paths(journal_output)
    if completion_path.exists() or completion_path.is_symlink():
        raise receipts.ReleaseControlError("dispatch preparation is already complete")
    if stage_path.exists() or stage_path.is_symlink():
        loaded_stage = _load_canonical_object(stage_path, label="dispatch preparation stage")
        journal, intent, request = _validated_preparation_stage(loaded_stage)
        return journal, intent, request, stage_path, completion_path
    if any(path.exists() or path.is_symlink() for path in outputs):
        raise receipts.ReleaseControlError("partial dispatch preparation lacks its durable stage")
    journal, intent, request = _prepare_from_args(args)
    new_stage: receipts.JSONObject = {
        "schema": "kestrel.dispatch_preparation_stage.v1",
        "journal": journal,
        "intent": intent,
        "request": request,
        "validation_status": "validated",
    }
    _validated_preparation_stage(new_stage)
    if not receipts.write_once(stage_path, receipts.canonical_json_bytes(new_stage)):
        raise receipts.ReleaseControlError("dispatch preparation stage already exists")
    return journal, intent, request, stage_path, completion_path


def _complete_preparation_stage(*, stage_path: Path, completion_path: Path) -> None:
    stage_raw = receipts._read_regular(  # noqa: SLF001
        stage_path,
        label="dispatch preparation stage",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    completion: receipts.JSONObject = {
        "schema": "kestrel.dispatch_preparation_complete.v1",
        "stage_digest": receipts._sha256(stage_raw),  # noqa: SLF001
        "validation_status": "validated",
    }
    if not receipts.write_once(completion_path, receipts.canonical_json_bytes(completion)):
        raise receipts.ReleaseControlError("dispatch preparation is already complete")


def _command_prepare_dispatch(args: argparse.Namespace) -> int:
    journal_output = Path(args.journal_output)
    intent_output = Path(args.intent_output)
    request_output = Path(args.request_output)
    outputs = (journal_output, intent_output, request_output)
    journal, intent, request, stage_path, completion_path = _load_or_create_preparation_stage(
        args=args,
        journal_output=journal_output,
        outputs=outputs,
    )
    _write_prepared_records(
        journal=journal,
        intent=intent,
        request=request,
        journal_output=journal_output,
        intent_output=intent_output,
        request_output=request_output,
    )
    _complete_preparation_stage(stage_path=stage_path, completion_path=completion_path)
    return 0


def _command_create_dispatch_intent(args: argparse.Namespace) -> int:
    intent_output = Path(args.output)
    journal_output = intent_output.with_name("dispatch-transaction.json")
    request_output = intent_output.with_name("dispatch-request.json")
    outputs = (journal_output, intent_output, request_output)
    journal, intent, request, stage_path, completion_path = _load_or_create_preparation_stage(
        args=args,
        journal_output=journal_output,
        outputs=outputs,
    )
    _write_prepared_records(
        journal=journal,
        intent=intent,
        request=request,
        journal_output=journal_output,
        intent_output=intent_output,
        request_output=request_output,
    )
    _complete_preparation_stage(stage_path=stage_path, completion_path=completion_path)
    return 0


def _command_send_dispatch(args: argparse.Namespace) -> int:
    transport = PinnedGitHubTransport(token=_read_dispatch_token())
    send_dispatch_once(
        journal_path=Path(args.journal),
        request_path=Path(args.request),
        response_output=Path(args.response_output),
        transport=transport,
        credential_fingerprint=transport.token_fingerprint,
        writer_inventory=_read_observation_or_record(
            Path(args.writer_inventory), label="pre-send repository writer inventory"
        ),
        writer_inventory_signature=receipts._read_regular(  # noqa: SLF001
            Path(args.writer_inventory_signature),
            label="pre-send repository writer inventory signature",
            max_bytes=1024 * 1024,
        ),
        owner_signing_keys_observation=receipts._read_regular(  # noqa: SLF001
            Path(args.owner_key_observation),
            label="owner signing keys observation",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        ),
    )
    return 0


def _command_contain_dispatch(args: argparse.Namespace) -> int:
    journal_path = Path(args.journal)
    journal = receipts._validate_dispatch_journal(  # noqa: SLF001
        _load_canonical_object(journal_path, label="dispatch journal")
    )
    local_boundary = _load_or_recover_send_boundary(
        journal_path=journal_path,
        journal=journal,
    )
    if args.response is not None:
        dispatch = _load_canonical_object(Path(args.response), label="dispatch response")
        receipts._validate_dispatch_transport_record(  # noqa: SLF001
            dispatch,
            journal=journal,
        )
    else:
        dispatch = receipts.classify_dispatch_transport(
            journal=journal,
            http_status=None,
            response_headers=None,
            response_body=None,
            response_observed_at=None,
            locally_proven_prewrite_failure=False,
            send_started_at=receipts.parse_timestamp(
                local_boundary.get("started_at"),
                label="dispatch send boundary started_at",
            ),
        )
    uninstall_bundle_raw = _read_observation_or_record(
        Path(args.uninstall_observation),
        label="dispatcher uninstall observation",
    )
    uninstall_bundle = receipts._object(  # noqa: SLF001
        receipts.strict_canonical_json(
            uninstall_bundle_raw,
            label="dispatcher uninstall observation",
        ),
        label="dispatcher uninstall observation",
    )
    receipts._require_exact_fields(  # noqa: SLF001
        uninstall_bundle,
        frozenset({"schema", "installed_apps_snapshot", "uninstall"}),
        label="dispatcher uninstall observation",
    )
    if uninstall_bundle.get("schema") != "kestrel.dispatcher_uninstall_bundle.v1":
        raise receipts.ReleaseControlError("dispatcher uninstall schema mismatch")
    installed_apps_snapshot = receipts._object(  # noqa: SLF001
        uninstall_bundle.get("installed_apps_snapshot"),
        label="post-uninstall installed Apps snapshot",
    )
    uninstall = receipts._object(  # noqa: SLF001
        uninstall_bundle.get("uninstall"),
        label="dispatcher uninstall result",
    )
    containment = receipts.create_dispatch_containment(
        journal=journal,
        dispatch=dispatch,
        send_boundary=local_boundary,
        installed_apps_snapshot=receipts.canonical_json_bytes(installed_apps_snapshot),
        uninstall_observation=receipts.canonical_json_bytes(uninstall),
        token_probe_observation=_read_observation_or_record(
            Path(args.token_probe_observation),
            label="dispatcher token probe observation",
        ),
        post_containment_writer_inventory=_read_observation_or_record(
            Path(args.writer_inventory),
            label="post-containment repository writer inventory",
        ),
        post_containment_writer_inventory_signature=receipts._read_regular(  # noqa: SLF001
            Path(args.writer_inventory_signature),
            label="post-containment repository writer inventory signature",
            max_bytes=1024 * 1024,
        ),
        owner_signing_keys_observation=receipts._read_regular(  # noqa: SLF001
            Path(args.owner_key_observation),
            label="owner signing keys observation",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        ),
    )
    receipts.write_once(Path(args.output), receipts.canonical_json_bytes(containment))
    return 0


def _command_reconcile_dispatch(args: argparse.Namespace) -> int:
    output = Path(args.output)
    _require_new_outputs((output,))
    intent_raw = _read_observation_or_record(Path(args.intent), label="signed dispatch intent")
    signature = receipts._read_regular(  # noqa: SLF001
        Path(args.intent_signature),
        label="dispatch intent signature",
        max_bytes=1024 * 1024,
    )
    intent = verify_owner_signed_dispatch_intent(
        intent=intent_raw,
        signature=signature,
        owner_signing_keys_observation=receipts._read_regular(  # noqa: SLF001
            Path(args.owner_key_observation),
            label="owner signing keys observation",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        ),
    )
    request = _canonical_object(
        _read_observation_or_record(Path(args.request), label="dispatch request"),
        label="dispatch request",
    )
    journal = _journal_bound_to_signed_intent(
        journal=_canonical_object(
            _read_observation_or_record(Path(args.journal), label="dispatch journal"),
            label="dispatch journal",
        ),
        intent=intent,
        request=request,
    )
    send_boundary = receipts._validate_dispatch_send_boundary(  # noqa: SLF001
        _load_or_recover_send_boundary(
            journal_path=Path(args.journal),
            journal=journal,
        ),
        journal=journal,
    )
    if args.response is None:
        dispatch: receipts.JSONObject = {
            "api_version": receipts.DISPATCH_API_VERSION,
            "endpoint": journal["endpoint"],
            "method": "POST",
            "classification": "outcome_unknown",
            "send_started_at": send_boundary["started_at"],
            "response_observed_at": None,
            "http_status": None,
            "response_headers_sha256": None,
            "response_body_sha256": None,
            "returned_run": None,
        }
    else:
        dispatch = _canonical_object(
            _read_observation_or_record(Path(args.response), label="dispatch response"),
            label="dispatch response",
        )
    containment = _canonical_object(
        _read_observation_or_record(Path(args.containment), label="dispatch containment"),
        label="dispatch containment",
    )
    if dispatch.get("send_started_at") != send_boundary.get("started_at"):
        raise receipts.ReleaseControlError(
            "dispatch response does not match the durable send boundary"
        )
    if containment.get("pre_send_writer_inventory_digest") != send_boundary.get(
        "pre_send_writer_inventory_digest"
    ):
        raise receipts.ReleaseControlError(
            "dispatch containment does not match the durable send boundary"
        )
    polls, candidates = _join_reconciliation_observations(
        journal=journal,
        workflow_runs_observation=_read_contract_source(
            Path(args.workflow_runs_observation),
            label="workflow runs reconciliation observation",
            receipt_schema=receipts.DISPATCH_RECONCILIATION_SCHEMA,
            phase="reconcile",
            mode=None,
            name="workflow-runs-observation",
        ),
        identity_artifact_observations=_read_contract_source(
            Path(args.identity_artifact_observations),
            label="dispatch identity artifact observations",
            receipt_schema=receipts.DISPATCH_RECONCILIATION_SCHEMA,
            phase="reconcile",
            mode=None,
            name="identity-artifact-observations",
        ),
    )
    try:
        reconciliation, _ = receipts.reconcile_dispatch(
            journal=journal,
            dispatch=dispatch,
            containment=containment,
            polls=polls,
            candidates=candidates,
        )
    except receipts.DispatchReconciliationPending:
        _persist_reconciliation_checkpoint(
            journal=journal,
            containment=containment,
            polls=polls,
            candidates=candidates,
            terminal=None,
        )
        raise
    _persist_reconciliation_checkpoint(
        journal=journal,
        containment=containment,
        polls=polls,
        candidates=candidates,
        terminal=reconciliation,
    )
    receipts.write_once(output, receipts.canonical_json_bytes(reconciliation))
    return 0


def _validate_final_admission_refresh(
    *,
    reconciliation: receipts.JSONObject,
    workflow_runs_observation: bytes,
    identity_artifact_observations: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> datetime:
    """Require one fresh complete query that still proves the adopted singleton."""

    receipts._validate_schema(  # noqa: SLF001
        receipts.DISPATCH_RECONCILIATION_SCHEMA,
        reconciliation,
        label="dispatch reconciliation",
    )
    outcome = receipts._object(  # noqa: SLF001
        reconciliation.get("outcome"), label="dispatch reconciliation outcome"
    )
    adopted_run_id = receipts._safe_integer(  # noqa: SLF001
        outcome.get("adopted_run_id"), label="adopted run ID", positive=True
    )
    if outcome.get("state") != "run_adopted":
        raise receipts.ReleaseControlError("final dispatch refresh requires an adopted run")
    transaction = receipts._object(  # noqa: SLF001
        reconciliation.get("transaction"),
        label="dispatch reconciliation transaction",
    )
    polls, candidates = _join_reconciliation_observations(
        journal=transaction,
        workflow_runs_observation=workflow_runs_observation,
        identity_artifact_observations=identity_artifact_observations,
    )
    if len(polls) != 1:
        raise receipts.ReleaseControlError(
            "final dispatch refresh must contain exactly one complete poll"
        )
    poll = polls[0]
    if (
        poll.get("complete") is not True
        or poll.get("nonce_run_ids") != [adopted_run_id]
        or poll.get("binding_conflict_run_ids") != []
    ):
        raise receipts.ReleaseControlError(
            "final dispatch refresh no longer proves the adopted singleton"
        )
    if len(candidates) != 1 or candidates[0].get("run_id") != adopted_run_id:
        raise receipts.ReleaseControlError("final dispatch refresh candidate cardinality mismatch")
    prior_candidates = receipts._array(  # noqa: SLF001
        reconciliation.get("candidates"), label="dispatch reconciliation candidates"
    )
    prior_matches = [
        receipts._object(candidate, label="dispatch reconciliation candidate")  # noqa: SLF001
        for candidate in prior_candidates
        if receipts._object(  # noqa: SLF001
            candidate, label="dispatch reconciliation candidate"
        ).get("run_id")
        == adopted_run_id
    ]
    if len(prior_matches) != 1:
        raise receipts.ReleaseControlError(
            "final dispatch refresh prior candidate cardinality mismatch"
        )
    current = candidates[0]
    prior = prior_matches[0]
    if current.get("run") != prior.get("run"):
        raise receipts.ReleaseControlError("final dispatch refresh run identity changed")
    current_artifact = receipts._object(  # noqa: SLF001
        current.get("identity_artifact"),
        label="final dispatch identity artifact",
    )
    identity = receipts._object(  # noqa: SLF001
        current_artifact.get("identity"),
        label="final dispatch identity",
    )
    if current_artifact.get("matching_name_count") != 1:
        raise receipts.ReleaseControlError("final dispatch identity artifact is ambiguous")
    refreshed_artifact = {
        key: current_artifact[key]
        for key in (
            "artifact_id",
            "name",
            "api_digest",
            "archive_sha256",
            "content_sha256",
            "expired",
        )
    }
    refreshed_artifact["identity_observed_at"] = identity.get("observed_at")
    if refreshed_artifact != prior.get("identity_artifact"):
        raise receipts.ReleaseControlError("final dispatch identity artifact changed")
    polling = receipts._object(  # noqa: SLF001
        reconciliation.get("polling"), label="dispatch reconciliation polling"
    )
    prior_polls = receipts._array(  # noqa: SLF001
        polling.get("polls"), label="dispatch reconciliation polls"
    )
    if not prior_polls:
        raise receipts.ReleaseControlError("final dispatch refresh lacks prior polling evidence")
    prior_requested = receipts.parse_timestamp(
        receipts._object(  # noqa: SLF001
            prior_polls[-1], label="dispatch reconciliation final poll"
        ).get("requested_at"),
        label="dispatch reconciliation final poll time",
    )
    refreshed_at = receipts.parse_timestamp(
        poll.get("requested_at"), label="final dispatch refresh time"
    )
    now = receipts.parse_timestamp(
        receipts._format_timestamp(  # noqa: SLF001
            _clock(), label="final dispatch refresh clock"
        ),
        label="final dispatch refresh clock",
    )
    if refreshed_at <= prior_requested:
        raise receipts.ReleaseControlError(
            "final dispatch refresh did not occur after reconciliation"
        )
    if refreshed_at > now:
        raise receipts.ReleaseControlError("final dispatch refresh evidence is in the future")
    if (now - refreshed_at).total_seconds() > receipts.CURRENT_CAPTURE_WINDOW_SECONDS:
        raise receipts.ReleaseControlError("final dispatch refresh evidence is stale")
    return refreshed_at


def _command_publish_dispatch_admission(args: argparse.Namespace) -> int:
    output = Path(args.output)
    signature_output = output.with_name(f"{output.name}.sig")
    publication_output = output.with_name(f"{output.stem}.terminal-publication-receipt.json")
    publication_journal = output.with_name(f"{output.stem}.terminal-publication.json")
    _require_new_outputs((output, signature_output, publication_output))
    reconciliation = _canonical_object(
        _read_observation_or_record(Path(args.reconciliation), label="dispatch reconciliation"),
        label="dispatch reconciliation",
    )
    containment = _canonical_object(
        _read_observation_or_record(Path(args.containment), label="dispatch containment"),
        label="dispatch containment",
    )
    refreshed_at = _validate_final_admission_refresh(
        reconciliation=reconciliation,
        workflow_runs_observation=_read_contract_source(
            Path(args.final_workflow_runs_observation),
            label="final workflow runs observation",
            receipt_schema=receipts.DISPATCH_RECONCILIATION_SCHEMA,
            phase="reconcile",
            mode=None,
            name="workflow-runs-observation",
        ),
        identity_artifact_observations=_read_contract_source(
            Path(args.final_identity_artifact_observations),
            label="final identity artifact observations",
            receipt_schema=receipts.DISPATCH_RECONCILIATION_SCHEMA,
            phase="reconcile",
            mode=None,
            name="identity-artifact-observations",
        ),
    )
    owner_keys = receipts._read_regular(  # noqa: SLF001
        Path(args.owner_key_observation),
        label="owner signing keys observation",
        max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
    )
    admission = receipts.create_dispatch_admission_from_reconciliation(
        reconciliation=reconciliation,
        containment=containment,
        owner_signing_keys_observation=owner_keys,
        pre_admission_writer_inventory=_read_observation_or_record(
            Path(args.writer_inventory),
            label="pre-admission repository writer inventory",
        ),
        pre_admission_writer_inventory_signature=receipts._read_regular(  # noqa: SLF001
            Path(args.writer_inventory_signature),
            label="pre-admission repository writer inventory signature",
            max_bytes=1024 * 1024,
        ),
        minimum_writer_inventory_captured_at=refreshed_at,
    )
    admission_bytes = _persist_or_load_terminal_record(
        kind="admission",
        record=admission,
    )
    admission = _canonical_object(admission_bytes, label="pending dispatch admission")
    terminal_api = _terminal_release_api_from_environment()
    remote_claim = terminal_api.claim_terminal_kind(
        RECOVERY_CHANNEL_REPOSITORY,
        transaction_nonce=cast(str, admission["transaction_nonce"]),
        kind="admission",
        record_digest=receipts._sha256(admission_bytes),  # noqa: SLF001
    )
    _claim_dispatch_terminal_publication(remote_claim=remote_claim)
    signature = receipts.sign_receipt_detached(
        receipt=admission_bytes,
        identity_file=Path(args.identity_file),
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    receipts.verify_owner_detached_signature(
        receipt=admission_bytes,
        signature=signature,
        owner_signing_keys_observation=owner_keys,
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    publication = publish_dispatch_terminal_release(
        kind="admission",
        record=admission_bytes,
        signature=signature,
        expected_signing_key_fingerprint=(receipts.signature_public_key_fingerprint(signature)),
        claim=remote_claim,
        journal_path=publication_journal,
        api=terminal_api,
    )
    if not receipts.write_once(output, admission_bytes):
        raise receipts.ReleaseControlError("dispatch admission output creation raced")
    if not receipts.write_once(signature_output, signature):
        raise receipts.ReleaseControlError("dispatch admission signature creation raced")
    if not receipts.write_once(publication_output, receipts.canonical_json_bytes(publication)):
        raise receipts.ReleaseControlError("dispatch admission publication receipt creation raced")
    return 0


def _command_publish_dispatch_tombstone(args: argparse.Namespace) -> int:
    output = Path(args.output)
    signature_output = output.with_name(f"{output.name}.sig")
    reconciliation_output = output.with_name(f"{output.stem}.reconciliation.json")
    publication_output = output.with_name(f"{output.stem}.terminal-publication-receipt.json")
    publication_journal = output.with_name(f"{output.stem}.terminal-publication.json")
    _require_new_outputs((output, signature_output, reconciliation_output, publication_output))
    reconciliation = _canonical_object(
        _read_observation_or_record(Path(args.reconciliation), label="dispatch reconciliation"),
        label="dispatch reconciliation",
    )
    tombstone = receipts.reconstruct_dispatch_tombstone(
        reconciliation=reconciliation,
        reason_code=cast(str, args.reason),
    )
    preview_tombstone = dict(tombstone)
    preview_tombstone["validation_status"] = "validated"
    receipts._validate_schema(  # noqa: SLF001
        receipts.DISPATCH_TOMBSTONE_SCHEMA,
        preview_tombstone,
        label="dispatch tombstone claim preview",
    )
    preview_bytes = receipts.canonical_json_bytes(preview_tombstone)
    terminal_api = _terminal_release_api_from_environment()
    remote_claim = terminal_api.claim_terminal_kind(
        RECOVERY_CHANNEL_REPOSITORY,
        transaction_nonce=cast(str, preview_tombstone["transaction_nonce"]),
        kind="tombstone",
        record_digest=receipts._sha256(preview_bytes),  # noqa: SLF001
    )
    _claim_dispatch_terminal_publication(remote_claim=remote_claim)
    finalized, signed_tombstone, signature = receipts.finalize_dispatch_tombstone(
        reconciliation=reconciliation,
        tombstone=tombstone,
        identity_file=Path(args.identity_file),
        owner_signing_keys_observation=receipts._read_regular(  # noqa: SLF001
            Path(args.owner_key_observation),
            label="owner signing keys observation",
            max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
        ),
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    tombstone_bytes = receipts.canonical_json_bytes(signed_tombstone)
    if tombstone_bytes != preview_bytes:
        raise receipts.ReleaseControlError(
            "dispatch tombstone changed after its remote atomic claim"
        )
    publication = publish_dispatch_terminal_release(
        kind="tombstone",
        record=tombstone_bytes,
        signature=signature,
        expected_signing_key_fingerprint=(receipts.signature_public_key_fingerprint(signature)),
        claim=remote_claim,
        journal_path=publication_journal,
        api=terminal_api,
    )
    if not receipts.write_once(output, tombstone_bytes):
        raise receipts.ReleaseControlError("dispatch tombstone output creation raced")
    if not receipts.write_once(signature_output, signature):
        raise receipts.ReleaseControlError("dispatch tombstone signature creation raced")
    if not receipts.write_once(
        reconciliation_output,
        receipts.canonical_json_bytes(finalized),
    ):
        raise receipts.ReleaseControlError("dispatch tombstone reconciliation creation raced")
    if not receipts.write_once(publication_output, receipts.canonical_json_bytes(publication)):
        raise receipts.ReleaseControlError("dispatch tombstone publication receipt creation raced")
    return 0


def _authorization_file(
    path: Path,
    *,
    label: str,
    source_name: str | None = None,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[bytes, bytes, receipts.JSONObject]:
    raw = receipts._read_regular(  # noqa: SLF001
        path,
        label=label,
        max_bytes=(
            receipts.MAX_SOURCE_ENVELOPE_BYTES
            if source_name is not None
            else receipts.MAX_SOURCE_BODY_BYTES
        ),
    )
    if source_name is not None:
        body = _read_contract_source(
            path,
            label=label,
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name=source_name,
            _clock=_clock,
        )
        parsed = receipts.parse_external_json_bytes(body, label=label)
    else:
        parsed = receipts.strict_canonical_json(raw, label=label)
        if (
            type(parsed) is not dict
            or type(parsed.get("schema")) is not str
            or not cast(str, parsed["schema"]).startswith("kestrel.")
            or parsed.get("schema") == receipts.SOURCE_OBSERVATION_SCHEMA
        ):
            raise receipts.ReleaseControlError(
                f"{label} must be a canonical Kestrel authority record"
            )
        body = raw
    if type(parsed) is list:
        return raw, body, {"items": parsed}
    return raw, body, receipts._object(parsed, label=label)  # noqa: SLF001


def _require_repository_identity(
    repository: Mapping[str, object],
    *,
    expected_repository: str,
    expected_repository_id: int | None,
    expected_owner_login: str,
    expected_owner_user_id: int,
    label: str,
) -> int:
    checked = receipts._copy_json_object(repository, label=label)  # noqa: SLF001
    owner = receipts._object(  # noqa: SLF001
        checked.get("owner"), label=f"{label} owner"
    )
    repository_id = receipts._safe_integer(  # noqa: SLF001
        checked.get("id"), label=f"{label} ID", positive=True
    )
    if (
        checked.get("full_name") != expected_repository
        or (expected_repository_id is not None and repository_id != expected_repository_id)
        or owner.get("login") != expected_owner_login
        or owner.get("id") != expected_owner_user_id
        or owner.get("type") != "User"
    ):
        raise receipts.ReleaseControlError(f"{label} repository identity mismatch")
    return repository_id


def _authorization_promotion_run(
    *,
    run_observation: receipts.JSONObject,
    run_observation_raw: bytes,
    identity: receipts.JSONObject,
    identity_raw: bytes,
) -> receipts.JSONObject:
    if run_observation.get("schema") != "kestrel.promotion_run_observation.v1":
        raise receipts.ReleaseControlError("authorization requires the promotion REST observation")
    return receipts._promotion_run_from_authority_sources(  # noqa: SLF001
        run_observation=run_observation,
        identity=identity,
        run_observation_digest=receipts._sha256(run_observation_raw),  # noqa: SLF001
        identity_observation_digest=receipts._sha256(identity_raw),  # noqa: SLF001
    )


def _validate_recovery_capsule_verification_claim(
    value: Mapping[str, object],
) -> receipts.JSONObject:
    checked = receipts._copy_json_object(  # noqa: SLF001
        value, label="signed recovery capsule verification claim"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        checked,
        frozenset(
            {
                "schema",
                "capsule_manifest_digest",
                "candidate_manifest_digest",
                "transaction_authorization_digest",
                "execution_closure_digest",
                "repository",
                "release",
                "assets",
                "owner_signing_keys_observation_digest",
                "signing_principal",
                "signing_key_fingerprint",
                "verified_at",
                "evidence",
                "provenance",
                "verified",
                "confidence",
                "validation_status",
            }
        ),
        label="signed recovery capsule verification claim",
    )
    if (
        checked.get("schema") != "kestrel.recovery_capsule_verification_claim.v1"
        or checked.get("signing_principal") != receipts.SIGNING_PRINCIPAL
        or checked.get("verified") is not True
        or checked.get("confidence") != 1
        or checked.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("recovery capsule verification claim is invalid")
    for field in (
        "capsule_manifest_digest",
        "candidate_manifest_digest",
        "transaction_authorization_digest",
        "execution_closure_digest",
        "owner_signing_keys_observation_digest",
        "signing_key_fingerprint",
    ):
        receipts._digest(checked.get(field), label=f"recovery capsule verification {field}")  # noqa: SLF001
    receipts.parse_timestamp(checked.get("verified_at"), label="recovery capsule verification time")

    repository = receipts._object(  # noqa: SLF001
        checked.get("repository"), label="recovery capsule verification repository"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        repository,
        frozenset({"full_name", "id", "private"}),
        label="recovery capsule verification repository",
    )
    if (
        repository.get("full_name") != RECOVERY_CHANNEL_REPOSITORY
        or repository.get("private") is not True
    ):
        raise receipts.ReleaseControlError(
            "recovery capsule verification repository identity mismatch"
        )
    receipts._safe_integer(  # noqa: SLF001
        repository.get("id"), label="recovery capsule verification repository ID", positive=True
    )

    release = receipts._object(  # noqa: SLF001
        checked.get("release"), label="recovery capsule verification Release"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        release,
        frozenset({"id", "tag", "immutable"}),
        label="recovery capsule verification Release",
    )
    tag = receipts._validate_string(  # noqa: SLF001
        release.get("tag"), label="recovery capsule verification Release tag"
    )
    if re.fullmatch(r"recovery-[1-9][0-9]*-1", tag) is None or release.get("immutable") is not True:
        raise receipts.ReleaseControlError(
            "recovery capsule verification Release identity mismatch"
        )
    receipts._safe_integer(  # noqa: SLF001
        release.get("id"), label="recovery capsule verification Release ID", positive=True
    )

    expected_asset_names = [
        "recovery-capsule-manifest.json",
        "recovery-capsule.tar",
    ]
    assets = receipts._array(  # noqa: SLF001
        checked.get("assets"), label="recovery capsule verification assets"
    )
    if len(assets) != len(expected_asset_names):
        raise receipts.ReleaseControlError("recovery capsule verification asset inventory mismatch")
    asset_names: list[str] = []
    asset_ids: set[int] = set()
    manifest_asset_digest: str | None = None
    for raw_asset in assets:
        asset = receipts._object(  # noqa: SLF001
            raw_asset, label="recovery capsule verification asset"
        )
        receipts._require_exact_fields(  # noqa: SLF001
            asset,
            frozenset({"id", "name", "size_bytes", "sha256"}),
            label="recovery capsule verification asset",
        )
        asset_id = receipts._safe_integer(  # noqa: SLF001
            asset.get("id"), label="recovery capsule verification asset ID", positive=True
        )
        name = receipts._validate_string(  # noqa: SLF001
            asset.get("name"), label="recovery capsule verification asset name"
        )
        receipts._safe_integer(  # noqa: SLF001
            asset.get("size_bytes"),
            label="recovery capsule verification asset size",
            positive=True,
        )
        digest = receipts._digest(  # noqa: SLF001
            asset.get("sha256"), label="recovery capsule verification asset digest"
        )
        if asset_id in asset_ids:
            raise receipts.ReleaseControlError(
                "recovery capsule verification asset IDs are duplicated"
            )
        asset_ids.add(asset_id)
        asset_names.append(name)
        if name == "recovery-capsule-manifest.json":
            manifest_asset_digest = digest
    if asset_names != expected_asset_names:
        raise receipts.ReleaseControlError(
            "recovery capsule verification assets are not exact and sorted"
        )
    if manifest_asset_digest != checked.get("capsule_manifest_digest"):
        raise receipts.ReleaseControlError(
            "recovery capsule verification manifest asset binding mismatch"
        )

    evidence = receipts._object(  # noqa: SLF001
        checked.get("evidence"), label="recovery capsule verification evidence"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        evidence,
        frozenset({"source_bundle_digest", "canonicalization_vector_digest"}),
        label="recovery capsule verification evidence",
    )
    receipts._digest(  # noqa: SLF001
        evidence.get("source_bundle_digest"),
        label="recovery capsule verification source bundle digest",
    )
    if evidence.get("canonicalization_vector_digest") != receipts.canonicalization_vector_digest():
        raise receipts.ReleaseControlError(
            "recovery capsule verification canonicalization binding mismatch"
        )
    provenance = receipts._object(  # noqa: SLF001
        checked.get("provenance"), label="recovery capsule verification provenance"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        provenance,
        frozenset({"producer", "provider", "method"}),
        label="recovery capsule verification provenance",
    )
    if provenance != {
        "producer": "scripts/release_promotion_transaction.py",
        "provider": "github.com",
        "method": "immutable-recovery-capsule-verification",
    }:
        raise receipts.ReleaseControlError("recovery capsule verification provenance mismatch")
    return checked


def _authorization_capsule_digest(
    *,
    verification: Mapping[str, object],
    candidate_manifest_digest: object,
    transaction_authorization: bytes,
) -> str:
    checked = receipts._copy_json_object(  # noqa: SLF001
        verification, label="signed recovery capsule verification"
    )
    expected_fields = frozenset(
        {
            "schema",
            "verification",
            "receipt_digest",
            "signature_digest",
            "receipt_base64",
            "signature_base64",
            "validation_status",
        }
    )
    if set(checked) != expected_fields:
        raise receipts.ReleaseControlError("signed recovery capsule verification fields mismatch")
    if (
        checked.get("schema") != "kestrel.recovery_capsule_verification.v1"
        or checked.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("signed recovery capsule verification is invalid")
    claim = _validate_recovery_capsule_verification_claim(
        receipts._object(  # noqa: SLF001
            checked.get("verification"), label="signed recovery capsule verification claim"
        )
    )
    receipt = _decode_observation_bytes(
        checked.get("receipt_base64"), label="recovery capsule verification receipt"
    )
    signature = _decode_observation_bytes(
        checked.get("signature_base64"),
        label="recovery capsule verification signature",
        maximum=1024 * 1024,
    )
    receipt_value = _canonical_object(receipt, label="signed recovery capsule verification receipt")
    if receipt_value != claim:
        raise receipts.ReleaseControlError(
            "signed recovery capsule verification receipt bytes mismatch"
        )
    if receipts._digest(  # noqa: SLF001
        checked.get("receipt_digest"), label="recovery capsule verification receipt digest"
    ) != receipts._sha256(receipt):  # noqa: SLF001
        raise receipts.ReleaseControlError(
            "signed recovery capsule verification receipt digest mismatch"
        )
    if receipts._digest(  # noqa: SLF001
        checked.get("signature_digest"),
        label="recovery capsule verification signature digest",
    ) != receipts._sha256(signature):  # noqa: SLF001
        raise receipts.ReleaseControlError(
            "signed recovery capsule verification signature digest mismatch"
        )
    signing_fingerprint = receipts._digest(  # noqa: SLF001
        claim.get("signing_key_fingerprint"),
        label="recovery capsule verification signing key fingerprint",
    )
    if (
        claim.get("signing_principal") != receipts.SIGNING_PRINCIPAL
        or receipts.signature_public_key_fingerprint(signature) != signing_fingerprint
    ):
        raise receipts.ReleaseControlError(
            "signed recovery capsule verification owner binding mismatch"
        )
    receipts._digest(  # noqa: SLF001
        claim.get("owner_signing_keys_observation_digest"),
        label="recovery capsule verification owner key observation digest",
    )
    receipts.parse_timestamp(claim.get("verified_at"), label="recovery capsule verification time")
    receipts.verify_owner_detached_signature_against_current_registration(
        receipt=receipt,
        signature=signature,
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    capsule_digest = receipts._digest(  # noqa: SLF001
        claim.get("capsule_manifest_digest"),
        label="recovery authorization capsule manifest digest",
    )
    if (
        claim.get("schema") != "kestrel.recovery_capsule_verification_claim.v1"
        or claim.get("candidate_manifest_digest") != candidate_manifest_digest
        or claim.get("transaction_authorization_digest")
        != receipts._sha256(transaction_authorization)  # noqa: SLF001
        or claim.get("verified") is not True
        or claim.get("confidence") != 1
        or claim.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError(
            "recovery authorization capsule verification binding mismatch"
        )
    return capsule_digest


def _require_release_dispatch_binding(
    *,
    run: Mapping[str, object],
    identity: Mapping[str, object],
    intent: Mapping[str, object],
    dispatch_reconciliation: Mapping[str, object],
) -> None:
    checked_run = receipts._copy_json_object(  # noqa: SLF001
        run, label="release reconciliation promotion run"
    )
    checked_identity = receipts._copy_json_object(  # noqa: SLF001
        identity, label="release reconciliation dispatch identity"
    )
    checked_intent = receipts._validate_dispatch_intent(intent)  # noqa: SLF001
    checked_dispatch = receipts._copy_json_object(  # noqa: SLF001
        dispatch_reconciliation, label="release dispatch reconciliation"
    )
    receipts._validate_schema(  # noqa: SLF001
        receipts.DISPATCH_RECONCILIATION_SCHEMA,
        checked_dispatch,
        label="release dispatch reconciliation",
    )
    transaction = receipts._object(  # noqa: SLF001
        checked_dispatch.get("transaction"),
        label="release dispatch reconciliation transaction",
    )
    outcome = receipts._object(  # noqa: SLF001
        checked_dispatch.get("outcome"),
        label="release dispatch reconciliation outcome",
    )
    intent_repository = receipts._object(  # noqa: SLF001
        checked_intent.get("repository"), label="release dispatch repository"
    )
    intent_workflow = receipts._object(  # noqa: SLF001
        checked_intent.get("workflow"), label="release dispatch workflow"
    )
    intent_target = receipts._object(  # noqa: SLF001
        checked_intent.get("target"), label="release dispatch target"
    )
    intent_inputs = receipts._object(  # noqa: SLF001
        checked_intent.get("inputs"), label="release dispatch inputs"
    )
    transaction_repository = receipts._object(  # noqa: SLF001
        transaction.get("repository"),
        label="release reconciliation transaction repository",
    )
    transaction_workflow = receipts._object(  # noqa: SLF001
        transaction.get("workflow"),
        label="release reconciliation transaction workflow",
    )
    transaction_target = receipts._object(  # noqa: SLF001
        transaction.get("target"),
        label="release reconciliation transaction target",
    )
    if (
        checked_identity.get("transaction_nonce") != checked_intent.get("transaction_nonce")
        or checked_identity.get("dispatch_binding") != checked_intent.get("dispatch_binding")
        or checked_identity.get("dispatch_inputs_digest")
        != receipts._sha256(receipts.canonical_json_bytes(intent_inputs))  # noqa: SLF001
        or checked_run.get("transaction_nonce") != checked_intent.get("transaction_nonce")
        or checked_run.get("run_id") != outcome.get("adopted_run_id")
        or outcome.get("state") != "run_adopted"
        or checked_dispatch.get("tombstone") is not None
        or transaction.get("transaction_nonce") != checked_intent.get("transaction_nonce")
        or transaction.get("dispatch_binding") != checked_intent.get("dispatch_binding")
        or transaction.get("request_sha256") != checked_intent.get("request_digest")
        or transaction_repository != intent_repository
        or transaction_workflow.get("id") != intent_workflow.get("id")
        or transaction_workflow.get("path") != intent_workflow.get("path")
        or transaction_target != intent_target
        or checked_run.get("repository_id") != intent_repository.get("id")
        or checked_run.get("workflow_id") != intent_workflow.get("id")
        or checked_run.get("workflow_path") != intent_workflow.get("path")
        or checked_run.get("ref") != intent_target.get("full_ref")
        or checked_run.get("head_sha") != intent_target.get("head_sha")
        or checked_run.get("workflow_sha") != intent_target.get("workflow_sha")
    ):
        raise receipts.ReleaseControlError(
            "release reconciliation dispatch authority binding mismatch"
        )


_AUTHORIZATION_EXTERNAL_SOURCE_NAMES = {
    "repository_observation": "repository-observation",
    "repository_collaborators_observation": "repository-collaborators-observation",
    "repository_invitations_observation": "repository-invitations-observation",
    "deploy_keys_observation": "deploy-keys-observation",
    "actions_workflow_permissions_observation": "actions-workflow-permissions-observation",
    "owner_signing_keys_observation": "owner-signing-keys-observation",
    "active_runs_observation": "active-runs-observation",
    "main_branch_observation": "main-branch-observation",
    "immutable_releases_observation": "immutable-releases-observation",
    "rulesets_observation": "rulesets-observation",
    "tag_ruleset_detail_observation": "tag-ruleset-detail-observation",
    "ingress_ruleset_detail_observation": "ingress-ruleset-detail-observation",
    "workflow_observation": "workflow-observation",
    "release_environment_observation": "environment-release-observation",
    "release_deployment_policies_observation": "environment-release-policies-observation",
    "promotion_run_observation": "promotion-run-observation",
    "approval_history_observation": "approval-history-observation",
    "commit_marker_observation": "tag-observation",
}


def _command_authorize(args: argparse.Namespace) -> int:
    output = Path(args.output)
    _require_new_outputs((output,))
    manifest_raw = receipts._read_regular(  # noqa: SLF001
        Path(args.manifest),
        label="candidate manifest",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    manifest = _canonical_object(manifest_raw, label="candidate manifest")
    try:
        verified = candidates.verify_candidate_bundle(
            manifest,
            bundle_root=Path(args.bundle_root),
            source_root=Path(args.workflow_source_root),
        )
    except ValueError as exc:
        raise receipts.ReleaseControlError(f"candidate bundle verification failed: {exc}") from exc
    candidate_run = receipts._object(  # noqa: SLF001
        verified.get("candidate_run"), label="candidate qualification run"
    )
    candidate: receipts.JSONObject = {
        "candidate_manifest_digest": cast(
            receipts.JSONValue, verified["candidate_manifest_digest"]
        ),
        "artifact_set_digest": cast(receipts.JSONValue, verified["artifact_set_digest"]),
        "version": cast(receipts.JSONValue, verified["version"]),
        "tag": cast(receipts.JSONValue, verified["tag"]),
        "source_sha": cast(receipts.JSONValue, verified["source_sha"]),
        "source_tree": cast(receipts.JSONValue, verified["source_tree"]),
        "candidate_run_id": candidate_run["run_id"],
        "candidate_run_attempt": candidate_run["run_attempt"],
    }
    manifest_source = receipts._object(  # noqa: SLF001
        manifest.get("source"), label="authorization candidate source"
    )
    candidate_repository_id = receipts._safe_integer(  # noqa: SLF001
        manifest_source.get("repository_id"),
        label="authorization candidate repository ID",
        positive=True,
    )

    source_records: dict[str, bytes] = {"candidate-manifest": manifest_raw}

    def read(name: str, label: str) -> tuple[bytes, receipts.JSONObject]:
        raw, _body, value = _authorization_file(
            Path(getattr(args, name)),
            label=label,
            source_name=_AUTHORIZATION_EXTERNAL_SOURCE_NAMES.get(name),
        )
        source_records[name.replace("_", "-")] = raw
        return raw, value

    repository_raw, repository = read(
        "repository_observation", "authorization repository observation"
    )
    _require_repository_identity(
        repository,
        expected_repository="John-MiracleWorker/Kestrel",
        expected_repository_id=candidate_repository_id,
        expected_owner_login="John-MiracleWorker",
        expected_owner_user_id=58918509,
        label="authorization",
    )
    collaborators_raw, collaborators = read(
        "repository_collaborators_observation",
        "authorization repository collaborators",
    )
    invitations_raw, invitations = read(
        "repository_invitations_observation",
        "authorization repository invitations",
    )
    deploy_keys_raw, deploy_keys = read("deploy_keys_observation", "authorization deploy keys")

    def collection(value: receipts.JSONObject, key: str) -> list[receipts.JSONValue]:
        candidate_value: object = value.get(key, value.get("items"))
        if candidate_value is None:
            return _api_items(value, label=f"authorization {key}")
        return _api_items(candidate_value, label=f"authorization {key}")

    writers = collection(collaborators, "collaborators")
    if len(writers) != 1:
        raise receipts.ReleaseControlError("authorization writer cardinality mismatch")
    writer = receipts._object(writers[0], label="authorization repository writer")  # noqa: SLF001
    if (
        writer.get("login") != "John-MiracleWorker"
        or writer.get("id") != 58918509
        or writer.get("type") != "User"
        or writer.get("role_name") != "admin"
    ):
        raise receipts.ReleaseControlError("authorization repository writer mismatch")
    if collection(invitations, "invitations") or collection(deploy_keys, "deploy_keys"):
        raise receipts.ReleaseControlError("authorization repository has another writer")

    actions_raw, actions = read(
        "actions_workflow_permissions_observation",
        "authorization Actions workflow permissions",
    )
    if (
        actions.get("default_workflow_permissions") != "read"
        or actions.get("can_approve_pull_request_reviews") is not False
    ):
        raise receipts.ReleaseControlError(
            "authorization Actions workflow permissions are too broad"
        )
    owner_keys_raw, _owner_keys = read(
        "owner_signing_keys_observation", "authorization owner signing keys"
    )
    _owner_public_key, owner_key_fingerprint = receipts.owner_signing_key(
        owner_signing_keys_observation=owner_keys_raw,
        principal=receipts.SIGNING_PRINCIPAL,
    )
    _active_runs_raw, active_runs = read("active_runs_observation", "authorization active runs")
    main_raw, main = read("main_branch_observation", "authorization main branch")
    main_commit = main.get("commit")
    main_sha = (
        receipts._object(  # noqa: SLF001
            main_commit, label="authorization main branch commit"
        ).get("sha")
        if main_commit is not None
        else main.get("sha", main.get("commit_sha"))
    )
    if args.mode == "initiate" and main_sha != candidate["source_sha"]:
        raise receipts.ReleaseControlError(
            "initiate authorization candidate is not locked current main"
        )
    immutable_raw, immutable = read(
        "immutable_releases_observation", "authorization immutable Releases"
    )
    immutable_enabled = immutable.get("enabled", immutable.get("immutable_releases_enabled"))
    if immutable_enabled is not True:
        raise receipts.ReleaseControlError("authorization immutable Releases are disabled")
    _rulesets_raw, rulesets = read("rulesets_observation", "authorization rulesets")
    tag_raw, tag_ruleset = read("tag_ruleset_detail_observation", "authorization tag ruleset")
    ingress_raw, ingress = read(
        "ingress_ruleset_detail_observation", "authorization ingress ruleset"
    )
    receipts._validate_ruleset(  # noqa: SLF001
        tag_ruleset,
        label="authorization tag ruleset",
        expected_name="kestrel-release-tags",
        expected_target="tag",
        expected_include="refs/tags/v*",
    )
    receipts._validate_ruleset(  # noqa: SLF001
        ingress,
        label="authorization ingress ruleset",
        expected_name="kestrel-release-transaction-main-lock",
        expected_target="branch",
        expected_include="refs/heads/main",
    )
    ruleset_items = [
        receipts._object(item, label="authorization ruleset inventory item")  # noqa: SLF001
        for item in collection(rulesets, "rulesets")
    ]
    for detail in (tag_ruleset, ingress):
        matches = [
            item
            for item in ruleset_items
            if item.get("id") == detail.get("id")
            and item.get("name") == detail.get("name")
            and item.get("target") == detail.get("target")
        ]
        if len(matches) != 1:
            raise receipts.ReleaseControlError("authorization ruleset inventory/detail mismatch")
    workflow_raw, workflow = read("workflow_observation", "authorization workflow observation")
    if (
        workflow.get("id") != args.expected_workflow_id
        or workflow.get("path") != args.expected_workflow_path
        or workflow.get("state") != "active"
        or workflow.get("default_branch") != "main"
    ):
        raise receipts.ReleaseControlError("authorization workflow identity mismatch")

    default_source_raw = receipts._read_regular(  # noqa: SLF001
        Path(args.default_branch_workflow_contents),
        label="authorization default workflow contents",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    default_raw = _read_contract_source(
        Path(args.default_branch_workflow_contents),
        label="authorization default workflow contents",
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="default-branch-workflow-contents",
    )
    candidate_workflow_source_raw = receipts._read_regular(  # noqa: SLF001
        Path(args.candidate_workflow_contents),
        label="authorization candidate workflow contents",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    candidate_workflow_raw = _read_contract_source(
        Path(args.candidate_workflow_contents),
        label="authorization candidate workflow contents",
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="candidate-workflow-contents",
    )
    source_records["default-branch-workflow-contents"] = default_source_raw
    source_records["candidate-workflow-contents"] = candidate_workflow_source_raw
    workflow_source_raw = receipts._read_regular(  # noqa: SLF001
        Path(args.workflow_source_root) / args.expected_workflow_path,
        label="authorization workflow source",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    source_records["workflow-source"] = workflow_source_raw
    if (
        not default_raw
        or default_raw != candidate_workflow_raw
        or candidate_workflow_raw != workflow_source_raw
    ):
        raise receipts.ReleaseControlError("authorization ingress workflow bytes drifted")

    environment_raw, environment_source = read(
        "release_environment_observation", "authorization release environment"
    )
    policies_raw, policies = read(
        "release_deployment_policies_observation",
        "authorization release deployment policies",
    )
    environment, observed_release_policies = _environment_gate_from_observations(
        environment=environment_source,
        policies=policies,
        policies_digest=receipts._sha256(policies_raw),  # noqa: SLF001
        expected_name="release",
        expected_owner_login="John-MiracleWorker",
        expected_owner_user_id=58918509,
    )
    run_raw, run_source = read("promotion_run_observation", "authorization promotion run")
    identity_raw, identity = read(
        "promotion_dispatch_identity", "authorization promotion dispatch identity"
    )
    promotion_run = _authorization_promotion_run(
        run_observation=run_source,
        run_observation_raw=run_raw,
        identity=identity,
        identity_raw=identity_raw,
    )
    if (
        promotion_run.get("repository_id") != candidate_repository_id
        or promotion_run.get("run_id") != args.expected_run_id
        or promotion_run.get("run_attempt") != args.expected_run_attempt
        or promotion_run.get("workflow_id") != args.expected_workflow_id
        or promotion_run.get("workflow_path") != args.expected_workflow_path
    ):
        raise receipts.ReleaseControlError("authorization promotion run mismatch")
    active_run_items = [
        receipts._object(item, label="authorization active run")  # noqa: SLF001
        for item in collection(active_runs, "workflow_runs")
    ]
    if (
        len(active_run_items) != 1
        or active_run_items[0].get("id") != args.expected_run_id
        or active_run_items[0].get("run_attempt") != 1
    ):
        raise receipts.ReleaseControlError("authorization active release run cardinality mismatch")
    approval_raw, approval_history = read(
        "approval_history_observation", "authorization approval history"
    )
    _require_cumulative_owner_approvals(approval_history, expected_environments=("release",))
    admission_raw, admission_source = read(
        "github_admission_authority_verification",
        "authorization admission authority verification",
    )
    github_admission_authority = _verified_authority_from_record(
        admission_source,
        verification_schema="kestrel.github_release_authority_verification.v1",
        authority_schema=receipts.GITHUB_AUTHORITY_SCHEMA,
        label="authorization admission authority verification",
    )
    _require_current_authority(
        github_admission_authority, label="authorization admission authority"
    )
    if admission_source.get("signing_key_fingerprint") != owner_key_fingerprint:
        raise receipts.ReleaseControlError(
            "authorization admission signing key is not the current owner key"
        )
    _require_operational_environment_policy_join(
        github_authority=github_admission_authority,
        environments={"release": environment},
        observed_policies={"release": observed_release_policies},
        require_complete=False,
    )
    admission_authority = {
        "receipt_digest": admission_source["receipt_digest"],
        "signature_digest": admission_source["signature_digest"],
        "verification_digest": receipts._sha256(  # noqa: SLF001
            receipts.canonical_json_bytes(admission_source)
        ),
    }
    intent_raw, intent_body, intent = _authorization_file(
        Path(args.dispatch_intent), label="authorization dispatch intent"
    )
    source_records["dispatch-intent"] = intent_raw
    receipts._validate_dispatch_intent(intent)  # noqa: SLF001
    reconciliation_raw, reconciliation_body, reconciliation = _authorization_file(
        Path(args.dispatch_reconciliation),
        label="authorization dispatch reconciliation",
    )
    source_records["dispatch-reconciliation"] = reconciliation_raw
    receipts._validate_schema(  # noqa: SLF001
        receipts.DISPATCH_RECONCILIATION_SCHEMA,
        reconciliation,
        label="authorization dispatch reconciliation",
    )
    outcome = receipts._object(  # noqa: SLF001
        reconciliation.get("outcome"), label="authorization dispatch outcome"
    )
    if (
        outcome.get("state") != "run_adopted"
        or outcome.get("adopted_run_id") != args.expected_run_id
    ):
        raise receipts.ReleaseControlError(
            "authorization dispatch reconciliation is not the admitted singleton"
        )
    _require_release_dispatch_binding(
        run=promotion_run,
        identity=identity,
        intent=intent,
        dispatch_reconciliation=reconciliation,
    )
    authority_dispatch = receipts._object(  # noqa: SLF001
        github_admission_authority.get("dispatch"),
        label="authorization admission dispatch authority",
    )
    reconciliation_dispatch = receipts._object(  # noqa: SLF001
        reconciliation.get("dispatch"), label="authorization reconciliation dispatch"
    )
    reconciliation_containment = receipts._object(  # noqa: SLF001
        reconciliation.get("containment"),
        label="authorization reconciliation containment",
    )
    token_probe = receipts._object(  # noqa: SLF001
        reconciliation_containment.get("token_probe"),
        label="authorization reconciliation token probe",
    )
    expected_token_probe = {
        "endpoint": token_probe.get("endpoint"),
        "http_status": token_probe.get("http_status"),
        "observed_at": token_probe.get("observed_at"),
        "response_digest": token_probe.get("response_sha256"),
    }
    if (
        authority_dispatch.get("intent_digest") != receipts._sha256(intent_body)  # noqa: SLF001
        or authority_dispatch.get("request_digest") != intent.get("request_digest")
        or authority_dispatch.get("reconciliation_digest") != receipts._sha256(reconciliation_body)  # noqa: SLF001
        or authority_dispatch.get("transport_outcome")
        != reconciliation_dispatch.get("classification")
        or authority_dispatch.get("uninstalled_at")
        != reconciliation_containment.get("uninstalled_at")
        or authority_dispatch.get("token_invalidation_probe") != expected_token_probe
    ):
        raise receipts.ReleaseControlError(
            "authorization admission/dispatch evidence binding mismatch"
        )

    repository_state = {
        "repository_writers_observation_digest": receipts.source_bundle_digest(
            {
                "collaborators": collaborators_raw,
                "deploy-keys": deploy_keys_raw,
                "invitations": invitations_raw,
                "repository": repository_raw,
            }
        ),
        "actions_authority_digest": receipts._sha256(actions_raw),  # noqa: SLF001
        "immutable_releases_observation_digest": receipts._sha256(immutable_raw),  # noqa: SLF001
        "tag_ruleset_observation_digest": receipts._sha256(tag_raw),  # noqa: SLF001
        "ingress_observation_digest": receipts.source_bundle_digest(
            {
                "candidate-workflow": candidate_workflow_source_raw,
                "default-workflow": default_source_raw,
                "ingress-ruleset": ingress_raw,
                "workflow": workflow_raw,
            }
        ),
    }
    transaction_raw: bytes | None = None
    capsule_digest: str | None = None
    marker_digest: str | None = None
    optional_values = (
        args.commit_marker_observation,
        args.transaction_authorization,
        args.recovery_capsule_verification,
    )
    if args.mode == "initiate":
        if any(value is not None for value in optional_values):
            raise receipts.ReleaseControlError("initiate authorization forbids recovery inputs")
    else:
        if any(value is None for value in optional_values):
            raise receipts.ReleaseControlError(
                "recovery authorization requires transaction, capsule, and marker"
            )
        transaction_raw = receipts._read_regular(  # noqa: SLF001
            Path(args.transaction_authorization),
            label="original transaction authorization",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        source_records["original-transaction-authorization"] = transaction_raw
        capsule_source_raw, capsule_verification = read(
            "recovery_capsule_verification", "recovery capsule verification"
        )
        capsule_digest = _authorization_capsule_digest(
            verification=capsule_verification,
            candidate_manifest_digest=candidate.get("candidate_manifest_digest"),
            transaction_authorization=transaction_raw,
        )
        marker_raw, marker = read("commit_marker_observation", "release commit marker")
        _require_committed_recovery_marker(
            observation=marker,
            candidate=candidate,
            transaction_authorization_digest=receipts._sha256(transaction_raw),  # noqa: SLF001
            recovery_capsule_digest=capsule_digest,
        )
        marker_digest = receipts._sha256(marker_raw)  # noqa: SLF001
        source_records["recovery-capsule-verification"] = capsule_source_raw
    _require_github_authority_binding(
        github_admission_authority,
        candidate=candidate,
        phase="admission",
        transaction_authorization_digest=(
            None if transaction_raw is None else receipts._sha256(transaction_raw)  # noqa: SLF001
        ),
        execution_authorization_digest=None,
        recovery_capsule_digest=capsule_digest,
        commit_marker_digest=marker_digest,
    )
    authority_run = receipts._object(  # noqa: SLF001
        github_admission_authority.get("promotion_run"),
        label="authorization verified admission run",
    )
    authority_environment = receipts._object(  # noqa: SLF001
        github_admission_authority.get("environment"),
        label="authorization verified admission environment",
    )
    if (
        github_admission_authority.get("mode") != args.mode
        or authority_run != promotion_run
        or authority_environment.get("id") != environment.get("id")
        or authority_environment.get("name") != environment.get("name")
    ):
        raise receipts.ReleaseControlError(
            "authorization admission authority current-run binding mismatch"
        )
    source_records["approval-history"] = approval_raw
    source_records["admission-authority"] = admission_raw
    source_records["dispatch-reconciliation"] = reconciliation_raw
    authority = build_server_authorization(
        candidate=candidate,
        promotion_run=promotion_run,
        environment=environment,
        approval_history=approval_history,
        admission_authority=admission_authority,
        repository_state=repository_state,
        mode=args.mode,
        transaction_authorization=transaction_raw,
        recovery_capsule_manifest_digest=capsule_digest,
        commit_marker_digest=marker_digest,
        source_records=source_records,
    )
    if not receipts.write_once(output, receipts.canonical_json_bytes(authority)):
        raise receipts.ReleaseControlError("authorization output path must be empty")
    return 0


def _api_items(value: object, *, label: str) -> list[receipts.JSONValue]:
    collection_keys = (
        "items",
        "repositories",
        "collaborators",
        "invitations",
        "keys",
        "branch_policies",
        "deploy_keys",
        "rulesets",
        "workflow_runs",
    )
    if type(value) is dict:
        for key in collection_keys:
            if key in value:
                return _api_items(value[key], label=label)
        raise receipts.ReleaseControlError(f"{label} is not an API collection")
    items = receipts._array(value, label=label)  # noqa: SLF001
    result: list[receipts.JSONValue] = []
    for item in items:
        if type(item) is list or (
            type(item) is dict and any(key in item for key in collection_keys)
        ):
            result.extend(_api_items(item, label=label))
        else:
            result.append(item)
    return result


def _environment_gate_from_observations(
    *,
    environment: Mapping[str, object],
    policies: object,
    policies_digest: str,
    expected_name: str,
    expected_owner_login: str,
    expected_owner_user_id: int,
) -> tuple[receipts.JSONObject, tuple[tuple[int, str], ...]]:
    checked_environment = receipts._copy_json_object(  # noqa: SLF001
        environment, label=f"{expected_name} environment"
    )
    environment_id = receipts._safe_integer(  # noqa: SLF001
        checked_environment.get("id"),
        label=f"{expected_name} environment ID",
        positive=True,
    )
    deployment_policy = receipts._object(  # noqa: SLF001
        checked_environment.get("deployment_branch_policy"),
        label=f"{expected_name} deployment branch policy",
    )
    if (
        checked_environment.get("name") != expected_name
        or deployment_policy.get("protected_branches") is not False
        or deployment_policy.get("custom_branch_policies") is not True
    ):
        raise receipts.ReleaseControlError(
            f"{expected_name} environment deployment policy mismatch"
        )
    protection_rules = receipts._array(  # noqa: SLF001
        checked_environment.get("protection_rules"),
        label=f"{expected_name} environment protection rules",
    )
    reviewer_rules = [
        receipts._object(item, label=f"{expected_name} reviewer rule")  # noqa: SLF001
        for item in protection_rules
        if receipts._object(  # noqa: SLF001
            item, label=f"{expected_name} environment rule"
        ).get("type")
        == "required_reviewers"
    ]
    if len(reviewer_rules) != 1:
        raise receipts.ReleaseControlError(f"{expected_name} environment reviewer policy mismatch")
    reviewer_values = receipts._array(  # noqa: SLF001
        reviewer_rules[0].get("reviewers"),
        label=f"{expected_name} environment reviewers",
    )
    if len(reviewer_values) != 1:
        raise receipts.ReleaseControlError(
            f"{expected_name} environment reviewer cardinality mismatch"
        )
    reviewer_entry = receipts._object(  # noqa: SLF001
        reviewer_values[0], label=f"{expected_name} reviewer entry"
    )
    reviewer = receipts._object(  # noqa: SLF001
        reviewer_entry.get("reviewer"), label=f"{expected_name} reviewer"
    )
    if (
        reviewer_entry.get("type") != "User"
        or reviewer.get("login") != expected_owner_login
        or reviewer.get("id") != expected_owner_user_id
        or reviewer.get("type") != "User"
        or reviewer_rules[0].get("prevent_self_review") is not True
    ):
        raise receipts.ReleaseControlError(
            f"{expected_name} environment owner review policy mismatch"
        )

    policy_items = [
        receipts._object(item, label=f"{expected_name} deployment policy")  # noqa: SLF001
        for item in _api_items(policies, label=f"{expected_name} deployment policies")
    ]
    normalized_policies = tuple(
        sorted(
            (
                receipts._safe_integer(  # noqa: SLF001
                    item.get("id"),
                    label=f"{expected_name} deployment policy ID",
                    positive=True,
                ),
                receipts._validate_string(  # noqa: SLF001
                    item.get("name"),
                    label=f"{expected_name} deployment policy name",
                ),
            )
            for item in policy_items
        )
    )
    if (
        len(normalized_policies) != 2
        or len({item[0] for item in normalized_policies}) != 2
        or {item[1] for item in normalized_policies} != {"main", "v*"}
    ):
        raise receipts.ReleaseControlError(
            f"{expected_name} environment deployment policy set mismatch"
        )
    gate: receipts.JSONObject = {
        "id": environment_id,
        "name": expected_name,
        "reviewer_login": expected_owner_login,
        "prevent_self_review": True,
        "policies_digest": receipts._digest(  # noqa: SLF001
            policies_digest, label=f"{expected_name} policies digest"
        ),
    }
    return gate, normalized_policies


def _require_operational_environment_policy_join(
    *,
    github_authority: Mapping[str, object],
    environments: Mapping[str, Mapping[str, object]],
    observed_policies: Mapping[str, tuple[tuple[int, str], ...]],
    require_complete: bool = True,
) -> None:
    authority_policies = [
        receipts._object(item, label="operational environment policy")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            github_authority.get("environment_policies"),
            label="operational environment policies",
        )
    ]
    authority_policy_ids: list[int] = []
    for name, environment in environments.items():
        environment_id = environment.get("id")
        matching = [
            item
            for item in authority_policies
            if item.get("environment_name") == name and item.get("environment_id") == environment_id
        ]
        authority_pairs = tuple(
            sorted(
                (
                    cast(int, item["policy_id"]),
                    cast(str, item["name"]),
                )
                for item in matching
            )
        )
        authority_types = {(cast(str, item["type"]), cast(str, item["name"])) for item in matching}
        if authority_pairs != observed_policies.get(name) or authority_types != {
            ("branch", "main"),
            ("tag", "v*"),
        }:
            raise receipts.ReleaseControlError("operational environment policy authority mismatch")
        authority_policy_ids.extend(cast(int, item["policy_id"]) for item in matching)
    expected_count = 8 if require_complete else 2 * len(environments)
    if (
        len(authority_policy_ids) != expected_count
        or len(set(authority_policy_ids)) != expected_count
    ):
        raise receipts.ReleaseControlError(
            "operational environment policy authority cardinality mismatch"
        )


def _command_inspect_prerequisites(args: argparse.Namespace) -> int:
    output = Path(args.output)
    _require_new_outputs((output,))
    sources: dict[str, bytes] = {}

    def read_value(
        path_text: str,
        name: str,
        *,
        contract_name: str | None = None,
        canonical_record: bool = False,
    ) -> object:
        raw = receipts._read_regular(  # noqa: SLF001
            Path(path_text), label=name, max_bytes=receipts.MAX_SOURCE_BODY_BYTES
        )
        sources[name] = raw
        if canonical_record:
            value = receipts.strict_canonical_json(raw, label=name)
            if (
                type(value) is not dict
                or type(value.get("schema")) is not str
                or not cast(str, value["schema"]).startswith("kestrel.")
                or value.get("schema") == receipts.SOURCE_OBSERVATION_SCHEMA
            ):
                raise receipts.ReleaseControlError(
                    f"{name} must be a canonical Kestrel authority record"
                )
            return value
        body = _read_contract_source(
            Path(path_text),
            label=name,
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name=name if contract_name is None else contract_name,
        )
        return receipts.parse_external_json_bytes(body, label=name)

    repository = receipts._object(  # noqa: SLF001
        read_value(args.repository_observation, "repository-observation"),
        label="prerequisite repository observation",
    )
    repository_id = _require_repository_identity(
        repository,
        expected_repository=args.expected_repository,
        expected_repository_id=None,
        expected_owner_login=args.expected_owner_login,
        expected_owner_user_id=args.expected_owner_user_id,
        label="prerequisite",
    )

    collaborators_value = read_value(
        args.repository_collaborators_observation,
        "repository-collaborators-observation",
    )
    collaborators = [
        receipts._object(item, label="prerequisite collaborator")  # noqa: SLF001
        for item in _api_items(collaborators_value, label="prerequisite collaborators")
    ]
    if len(collaborators) != 1:
        raise receipts.ReleaseControlError("prerequisite repository writer cardinality mismatch")
    collaborator = collaborators[0]
    if (
        collaborator.get("login") != args.expected_owner_login
        or collaborator.get("id") != args.expected_owner_user_id
        or collaborator.get("type") != "User"
        or collaborator.get("role_name") != "admin"
    ):
        raise receipts.ReleaseControlError("prerequisite repository writer mismatch")
    repository_writers: list[receipts.JSONValue] = [
        {
            "login": collaborator["login"],
            "id": collaborator["id"],
            "type": collaborator["type"],
            "role_name": collaborator["role_name"],
        }
    ]
    for path_text, name in (
        (args.repository_invitations_observation, "repository-invitations-observation"),
        (args.deploy_keys_observation, "deploy-keys-observation"),
    ):
        if _api_items(read_value(path_text, name), label=name):
            raise receipts.ReleaseControlError(
                "prerequisite repository has additional writer authority"
            )

    actions = receipts._object(  # noqa: SLF001
        read_value(
            args.actions_workflow_permissions_observation,
            "actions-workflow-permissions-observation",
        ),
        label="prerequisite Actions workflow permissions",
    )
    if (
        actions.get("default_workflow_permissions") != "read"
        or actions.get("can_approve_pull_request_reviews") is not False
    ):
        raise receipts.ReleaseControlError(
            "prerequisite Actions workflow permissions are too broad"
        )

    key_items = [
        receipts._object(item, label="prerequisite owner signing key")  # noqa: SLF001
        for item in _api_items(
            read_value(
                args.owner_signing_keys_observation,
                "owner-signing-keys-observation",
            ),
            label="prerequisite owner signing keys",
        )
    ]
    if len(key_items) != 1:
        raise receipts.ReleaseControlError("prerequisite owner signing key cardinality mismatch")
    _owner_public_key, fingerprint = receipts.owner_signing_key(
        owner_signing_keys_observation=sources["owner-signing-keys-observation"],
        principal=args.expected_owner_login,
    )
    controller_signing_key = {
        "owner_login": args.expected_owner_login,
        "fingerprint": fingerprint,
        "observation_digest": receipts._sha256(  # noqa: SLF001
            sources["owner-signing-keys-observation"]
        ),
    }

    main_observation = receipts._object(  # noqa: SLF001
        read_value(args.main_branch_observation, "main-branch-observation"),
        label="prerequisite main branch",
    )
    commit = main_observation.get("commit")
    main_sha = (
        receipts._object(commit, label="prerequisite main commit").get("sha")  # noqa: SLF001
        if commit is not None
        else main_observation.get("sha")
    )
    main_branch: receipts.JSONObject = {
        "name": "main",
        "sha": receipts._git_sha(main_sha, label="prerequisite main SHA"),  # noqa: SLF001
        "observation_digest": receipts._sha256(  # noqa: SLF001
            sources["main-branch-observation"]
        ),
    }
    immutable_source = receipts._object(  # noqa: SLF001
        read_value(
            args.immutable_releases_observation,
            "immutable-releases-observation",
        ),
        label="prerequisite immutable Releases observation",
    )
    immutable_enabled = immutable_source.get(
        "enabled", immutable_source.get("immutable_releases_enabled")
    )
    if type(immutable_enabled) is not bool:
        raise receipts.ReleaseControlError("prerequisite immutable Releases setting is missing")
    immutable_releases: receipts.JSONObject = {
        "enabled": immutable_enabled,
        "observation_digest": receipts._sha256(  # noqa: SLF001
            sources["immutable-releases-observation"]
        ),
    }

    rulesets_value = read_value(args.rulesets_observation, "rulesets-observation")
    rulesets = [
        receipts._object(item, label="prerequisite ruleset")  # noqa: SLF001
        for item in _api_items(rulesets_value, label="prerequisite rulesets")
    ]
    tag_candidates = [
        item
        for item in rulesets
        if item.get("name") == "kestrel-release-tags" and item.get("enforcement") == "active"
    ]
    if len(tag_candidates) != 1:
        raise receipts.ReleaseControlError("prerequisite tag ruleset mismatch")
    tag_detail = receipts._object(  # noqa: SLF001
        read_value(
            args.tag_ruleset_detail_observation,
            "tag-ruleset-detail-observation",
        ),
        label="prerequisite tag ruleset detail",
    )
    if (
        tag_detail.get("id") != tag_candidates[0].get("id")
        or tag_detail.get("bypass_actors") != []
        or tag_detail.get("target") != "tag"
        or tag_detail.get("enforcement") != "active"
    ):
        raise receipts.ReleaseControlError("prerequisite tag ruleset is unsafe")

    optional_ingress = (
        args.ingress_ruleset_detail_observation,
        args.workflow_observation,
        args.default_branch_workflow_contents,
        args.candidate_workflow_contents,
    )
    if any(value is not None for value in optional_ingress) and not all(
        value is not None for value in optional_ingress
    ):
        raise receipts.ReleaseControlError(
            "prerequisite ingress observations are an incomplete set"
        )
    if all(value is not None for value in optional_ingress):
        ingress_detail = receipts._object(  # noqa: SLF001
            read_value(
                args.ingress_ruleset_detail_observation,
                "ingress-ruleset-detail-observation",
            ),
            label="prerequisite ingress ruleset detail",
        )
        workflow = receipts._object(  # noqa: SLF001
            read_value(args.workflow_observation, "workflow-observation"),
            label="prerequisite workflow",
        )
        default_source = receipts._read_regular(  # noqa: SLF001
            Path(args.default_branch_workflow_contents),
            label="default-branch-workflow-contents",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        default_bytes = _read_contract_source(
            Path(args.default_branch_workflow_contents),
            label="default-branch-workflow-contents",
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name="default-branch-workflow-contents",
        )
        candidate_source = receipts._read_regular(  # noqa: SLF001
            Path(args.candidate_workflow_contents),
            label="candidate-workflow-contents",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        candidate_bytes = _read_contract_source(
            Path(args.candidate_workflow_contents),
            label="candidate-workflow-contents",
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name="candidate-workflow-contents",
        )
        sources["default-branch-workflow-contents"] = default_source
        sources["candidate-workflow-contents"] = candidate_source
        ingress_active = (
            ingress_detail.get("name") == "kestrel-release-transaction-main-lock"
            and ingress_detail.get("target") == "branch"
            and ingress_detail.get("enforcement") == "active"
            and ingress_detail.get("bypass_actors") == []
        )
        workflow_equal = bool(default_bytes) and default_bytes == candidate_bytes
        ingress_observation: receipts.JSONObject = {
            "ruleset_id": receipts._safe_integer(  # noqa: SLF001
                ingress_detail.get("id"),
                label="prerequisite ingress ruleset ID",
                positive=True,
            ),
            "active": ingress_active,
            "workflow_byte_equal": workflow_equal,
            "observation_digest": receipts.source_bundle_digest(
                {
                    "candidate-workflow": candidate_source,
                    "default-workflow": default_source,
                    "ingress-ruleset": sources["ingress-ruleset-detail-observation"],
                }
            ),
        }
        workflow_inventory: list[receipts.JSONValue] = [
            {
                "id": workflow["id"],
                "path": workflow["path"],
                "state": workflow["state"],
                "observation_digest": receipts._sha256(  # noqa: SLF001
                    sources["workflow-observation"]
                ),
            }
        ]
        default_branch: receipts.JSONObject = {
            "name": "main",
            "workflow_sha256": receipts._sha256(default_bytes),  # noqa: SLF001
            "observation_digest": receipts._sha256(  # noqa: SLF001
                sources["default-branch-workflow-contents"]
            ),
        }
    else:
        ingress_observation = {
            "ruleset_id": None,
            "active": False,
            "workflow_byte_equal": False,
            "observation_digest": receipts._sha256(b"unverified"),  # noqa: SLF001
        }
        workflow_path = Path(args.workflow_source_root) / ".github/workflows/release.yml"
        workflow_bytes = receipts._read_regular(  # noqa: SLF001
            workflow_path,
            label="candidate release workflow",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        workflow_inventory = [
            {
                "id": None,
                "path": ".github/workflows/release.yml",
                "state": "unverified",
                "observation_digest": receipts._sha256(workflow_bytes),  # noqa: SLF001
            }
        ]
        default_branch = {
            "name": "main",
            "workflow_sha256": receipts._sha256(workflow_bytes),  # noqa: SLF001
            "observation_digest": receipts._sha256(workflow_bytes),  # noqa: SLF001
        }

    environment_paths: dict[str, str] = {}
    policy_paths: dict[str, str] = {}
    for raw_value in args.environment_observation:
        name, separator, path = raw_value.partition("=")
        if not separator or name in environment_paths:
            raise receipts.ReleaseControlError(
                "prerequisite environment observation mapping is invalid"
            )
        environment_paths[name] = path
    for raw_value in args.environment_policies_observation:
        name, separator, path = raw_value.partition("=")
        if not separator or name in policy_paths:
            raise receipts.ReleaseControlError("prerequisite environment policy mapping is invalid")
        policy_paths[name] = path
    expected_environment_names = {
        "release",
        "release-prepare",
        "release-commit",
        "pypi",
    }
    if (
        set(environment_paths) != expected_environment_names
        or set(policy_paths) != expected_environment_names
    ):
        raise receipts.ReleaseControlError(
            "prerequisite environment mapping must contain the exact four gates"
        )
    environments: list[receipts.JSONObject] = []
    observed_environment_policies: dict[str, tuple[tuple[int, str], ...]] = {}
    for name in expected_environment_names:
        environment = receipts._object(  # noqa: SLF001
            read_value(
                environment_paths[name],
                f"{name}-environment-observation",
                contract_name=f"environment-{name}-observation",
            ),
            label=f"{name} environment",
        )
        policy_source_name = f"{name}-environment-policies-observation"
        policy_value = read_value(
            policy_paths[name],
            policy_source_name,
            contract_name=f"environment-{name}-policies-observation",
        )
        gate, normalized_policies = _environment_gate_from_observations(
            environment=environment,
            policies=policy_value,
            policies_digest=receipts._sha256(sources[policy_source_name]),  # noqa: SLF001
            expected_name=name,
            expected_owner_login=args.expected_owner_login,
            expected_owner_user_id=args.expected_owner_user_id,
        )
        environments.append(gate)
        observed_environment_policies[name] = normalized_policies
    environments.sort(key=lambda item: (cast(int, item["id"]), str(item["name"])))
    if len({item["id"] for item in environments}) != len(environments):
        raise receipts.ReleaseControlError("prerequisite environment IDs are not unique")

    recovery_repository_source = receipts._object(  # noqa: SLF001
        read_value(
            args.recovery_repository_observation,
            "recovery-repository-observation",
        ),
        label="prerequisite recovery repository",
    )
    if recovery_repository_source.get("full_name") != "John-MiracleWorker/Kestrel-Release-Recovery":
        raise receipts.ReleaseControlError("prerequisite recovery repository identity mismatch")
    recovery_id = receipts._safe_integer(  # noqa: SLF001
        recovery_repository_source.get("id"),
        label="prerequisite recovery repository ID",
        positive=True,
    )
    verification_paths = (
        args.recovery_authority_verification,
        args.github_authority_verification,
        args.pypi_authority_verification,
    )
    if args.mode == "hosted-smoke":
        if any(value is not None for value in verification_paths) or (
            args.recovery_immutable_releases_observation is not None
        ):
            raise receipts.ReleaseControlError(
                "hosted-smoke prerequisites cannot mix operational authority inputs"
            )
        blockers = [
            "environment_policy_types_unverified",
            "github_authority_unprovisioned",
            "pypi_authority_unprovisioned",
            "recovery_authority_unprovisioned",
        ]
        recovery_repository = {
            "repository": recovery_repository_source["full_name"],
            "repository_id": recovery_id,
            "authority_digest": None,
            "immutable_releases": False,
        }
        status = "validated_for_hosted_smoke"
    else:
        if any(value is None for value in verification_paths) or (
            args.recovery_immutable_releases_observation is None
        ):
            raise receipts.ReleaseControlError(
                "operational prerequisites require every signed authority"
            )
        recovery_verification = receipts._object(  # noqa: SLF001
            read_value(
                args.recovery_authority_verification,
                "recovery-authority-verification",
                canonical_record=True,
            ),
            label="recovery authority verification",
        )
        github_verification = receipts._object(  # noqa: SLF001
            read_value(
                args.github_authority_verification,
                "github-authority-verification",
                canonical_record=True,
            ),
            label="GitHub authority verification",
        )
        pypi_verification = receipts._object(  # noqa: SLF001
            read_value(
                args.pypi_authority_verification,
                "pypi-authority-verification",
                canonical_record=True,
            ),
            label="PyPI authority verification",
        )
        recovery_authority = receipts.validate_recovery_repository_authority(
            _verified_authority_from_record(
                recovery_verification,
                verification_schema=("kestrel.recovery_repository_authority_verification.v1"),
                authority_schema=receipts.RECOVERY_AUTHORITY_SCHEMA,
                label="recovery authority verification",
            )
        )
        github_authority = receipts.validate_github_authority(
            _verified_authority_from_record(
                github_verification,
                verification_schema=("kestrel.github_release_authority_verification.v1"),
                authority_schema=receipts.GITHUB_AUTHORITY_SCHEMA,
                label="GitHub authority verification",
            )
        )
        pypi_authority = receipts.validate_pypi_authority(
            _verified_authority_from_record(
                pypi_verification,
                verification_schema=("kestrel.pypi_upload_authority_verification.v1"),
                authority_schema=receipts.PYPI_AUTHORITY_SCHEMA,
                label="PyPI authority verification",
            )
        )
        if (
            receipts._object(  # noqa: SLF001
                recovery_authority.get("repository"),
                label="verified recovery repository",
            ).get("id")
            != recovery_id
            or receipts._object(  # noqa: SLF001
                github_authority.get("repository"),
                label="verified prerequisite repository",
            ).get("id")
            != repository_id
            or github_authority.get("candidate") != pypi_authority.get("candidate")
            or github_authority.get("promotion_run") != pypi_authority.get("promotion_run")
        ):
            raise receipts.ReleaseControlError(
                "operational prerequisite authority identities disagree"
            )
        _require_operational_environment_policy_join(
            github_authority=github_authority,
            environments={cast(str, item["name"]): item for item in environments},
            observed_policies=observed_environment_policies,
        )
        recovery_immutable = receipts._object(  # noqa: SLF001
            read_value(
                args.recovery_immutable_releases_observation,
                "recovery-immutable-releases-observation",
            ),
            label="recovery immutable Releases",
        )
        recovery_enabled = recovery_immutable.get(
            "enabled", recovery_immutable.get("immutable_releases_enabled")
        )
        if recovery_enabled is not True:
            raise receipts.ReleaseControlError(
                "operational recovery immutable Releases are disabled"
            )
        blockers = []
        recovery_repository = {
            "repository": recovery_repository_source["full_name"],
            "repository_id": recovery_id,
            "authority_digest": recovery_verification["receipt_digest"],
            "immutable_releases": True,
        }
        status = "validated_operational"
    bootstrap_path = Path(args.workflow_source_root) / "scripts/bootstrap_workflow_tools.sh"
    bootstrap_raw = receipts._read_regular(  # noqa: SLF001
        bootstrap_path,
        label="workflow tool bootstrap",
        max_bytes=4 * 1024 * 1024,
    )
    sources["workflow-tool-bootstrap"] = bootstrap_raw
    prerequisites: receipts.JSONObject = {
        "schema": RELEASE_PREREQUISITES_SCHEMA,
        "mode": args.mode,
        "repository": {
            "full_name": repository["full_name"],
            "id": repository_id,
        },
        "repository_writers": repository_writers,
        "controller_signing_key": controller_signing_key,
        "workflow_inventory": workflow_inventory,
        "main_branch": main_branch,
        "default_branch": default_branch,
        "ingress_observation": ingress_observation,
        "immutable_releases": immutable_releases,
        "environments": cast(list[receipts.JSONValue], environments),
        "recovery_repository": recovery_repository,
        "tool_bootstrap": [
            {
                "name": "gh",
                "version": "2.97.0",
                "sha256": _workflow_tools_archive_digest(),
            }
        ],
        "operational_blockers": cast(list[receipts.JSONValue], blockers),
        "evidence": {
            "source_bundle_digest": receipts.source_bundle_digest(sources),
            "canonicalization_vector_digest": receipts.canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_promotion_transaction.py",
            "provider": "github.com",
            "method": "complete-prerequisite-inspection",
        },
        "confidence": 1,
        "validation_status": status,
    }
    validate_release_prerequisites(prerequisites)
    if not receipts.write_once(output, receipts.canonical_json_bytes(prerequisites)):
        raise receipts.ReleaseControlError("prerequisite output path must be empty")
    return 0


def _external_object(raw: bytes, *, label: str) -> receipts.JSONObject:
    return receipts._object(  # noqa: SLF001
        receipts.parse_external_json_bytes(raw, label=label), label=label
    )


def _external_observation_body(path: Path, *, label: str) -> bytes:
    raw = receipts._read_regular(  # noqa: SLF001
        path, label=label, max_bytes=receipts.MAX_SOURCE_BODY_BYTES
    )
    value = receipts.parse_external_json_bytes(raw, label=label)
    if type(value) is dict and value.get("schema") == receipts.SOURCE_OBSERVATION_SCHEMA:
        return receipts.source_observation_body(raw)
    return raw


def verify_recovery_capsule(
    *,
    capsule_manifest: bytes,
    capsule_root: Path,
    recovery_repository_observation: bytes,
    recovery_release_observation: bytes,
    recovery_assets_observation: bytes,
    execution_closure: bytes,
    expected_candidate_digest: str,
    expected_transaction_authorization_digest: str,
    remote_source_records: Mapping[str, bytes] | None = None,
) -> receipts.JSONObject:
    """Bind one exact local capsule to its immutable recovery Release."""

    manifest, root_manifest_raw = receipts.verify_recovery_capsule_root(capsule_root)
    if capsule_manifest != root_manifest_raw:
        raise receipts.ReleaseControlError("recovery capsule manifest file and root disagree")
    candidate = receipts._object(  # noqa: SLF001
        manifest.get("candidate"), label="recovery capsule candidate"
    )
    candidate_digest = receipts._digest(  # noqa: SLF001
        expected_candidate_digest, label="expected recovery candidate digest"
    )
    transaction_digest = receipts._digest(  # noqa: SLF001
        expected_transaction_authorization_digest,
        label="expected transaction authorization digest",
    )
    if (
        candidate.get("candidate_manifest_digest") != candidate_digest
        or manifest.get("transaction_authorization_digest") != transaction_digest
    ):
        raise receipts.ReleaseControlError(
            "recovery capsule candidate or transaction binding mismatch"
        )

    closure = _canonical_object(execution_closure, label="recovery execution closure")
    receipts._validate_schema(  # noqa: SLF001
        "kestrel.recovery_execution_closure.v1",
        closure,
        label="recovery execution closure",
    )
    capsule_closure = receipts._read_regular(  # noqa: SLF001
        capsule_root / "recovery-execution-closure.json",
        label="capsule recovery execution closure",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    if capsule_closure != execution_closure:
        raise receipts.ReleaseControlError("recovery capsule execution closure binding mismatch")

    repository = _external_object(
        recovery_repository_observation,
        label="recovery capsule repository observation",
    )
    manifest_repository = receipts._object(  # noqa: SLF001
        manifest.get("recovery_repository"), label="recovery capsule repository"
    )
    repository_id = receipts._safe_integer(  # noqa: SLF001
        repository.get("id"), label="recovery capsule repository ID", positive=True
    )
    if (
        repository.get("full_name") != manifest_repository.get("full_name")
        or repository_id != manifest_repository.get("id")
        or repository.get("private") is not True
    ):
        raise receipts.ReleaseControlError("recovery capsule recovery repository mismatch")

    release = _external_object(
        recovery_release_observation, label="recovery capsule Release observation"
    )
    release_id = receipts._safe_integer(  # noqa: SLF001
        release.get("id"), label="recovery capsule Release ID", positive=True
    )
    manifest_release = receipts._object(  # noqa: SLF001
        manifest.get("release"), label="recovery capsule Release"
    )
    tag = cast(str, manifest_release["tag"])
    manifest_digest = receipts._sha256(capsule_manifest)  # noqa: SLF001
    expected_name = f"Kestrel recovery capsule {tag}"
    expected_body = f"Kestrel recovery capsule {tag}\n\nKestrel-Recovery-Capsule: {manifest_digest}"
    if (
        release.get("tag_name") != tag
        or release.get("name") != expected_name
        or release.get("body") != expected_body
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or release.get("immutable") is not True
    ):
        raise receipts.ReleaseControlError("recovery capsule immutable Release identity mismatch")

    archive = receipts.deterministic_recovery_capsule_archive(capsule_root)
    expected_assets = {
        "recovery-capsule-manifest.json": (
            len(capsule_manifest),
            manifest_digest,
        ),
        "recovery-capsule.tar": (len(archive), receipts._sha256(archive)),  # noqa: SLF001
    }
    assets_value = receipts.parse_external_json_bytes(
        recovery_assets_observation, label="recovery capsule asset observation"
    )
    raw_assets = _api_items(assets_value, label="recovery capsule assets")
    normalized_assets: list[receipts.JSONObject] = []
    seen_names: set[str] = set()
    seen_ids: set[int] = set()
    for raw_asset in raw_assets:
        asset = receipts._object(raw_asset, label="recovery capsule asset")  # noqa: SLF001
        name = receipts._validate_string(  # noqa: SLF001
            asset.get("name"), label="recovery capsule asset name"
        )
        asset_id = receipts._safe_integer(  # noqa: SLF001
            asset.get("id"), label="recovery capsule asset ID", positive=True
        )
        if name in seen_names or asset_id in seen_ids:
            raise receipts.ReleaseControlError("recovery capsule Release assets are duplicated")
        seen_names.add(name)
        seen_ids.add(asset_id)
        size = receipts._safe_integer(  # noqa: SLF001
            asset.get("size"), label="recovery capsule asset size", positive=True
        )
        digest = receipts._digest(  # noqa: SLF001
            asset.get("digest"), label="recovery capsule asset digest"
        )
        if expected_assets.get(name) != (size, digest):
            raise receipts.ReleaseControlError("recovery capsule Release asset identity mismatch")
        normalized_assets.append(
            {"id": asset_id, "name": name, "size_bytes": size, "sha256": digest}
        )
    normalized_assets.sort(key=lambda item: cast(str, item["name"]))
    if seen_names != set(expected_assets):
        raise receipts.ReleaseControlError("recovery capsule Release asset inventory mismatch")

    sources: dict[str, bytes] = {
        "capsule-manifest": capsule_manifest,
        "execution-closure": execution_closure,
        "recovery-repository": recovery_repository_observation,
        "recovery-release": recovery_release_observation,
        "recovery-release-assets": recovery_assets_observation,
    }
    if remote_source_records is not None:
        expected_remote_names = {
            "recovery-repository",
            "recovery-release",
            "recovery-release-assets",
        }
        if set(remote_source_records) != expected_remote_names:
            raise receipts.ReleaseControlError(
                "recovery capsule remote source record inventory mismatch"
            )
        sources.update(
            {name: bytes(raw) for name, raw in remote_source_records.items() if type(raw) is bytes}
        )
        if set(sources) != {
            "capsule-manifest",
            "execution-closure",
            *expected_remote_names,
        }:
            raise receipts.ReleaseControlError(
                "recovery capsule remote source records must be exact bytes"
            )
    return {
        "schema": "kestrel.recovery_capsule_verification_claim.v1",
        "capsule_manifest_digest": manifest_digest,
        "candidate_manifest_digest": candidate_digest,
        "transaction_authorization_digest": transaction_digest,
        "execution_closure_digest": receipts._sha256(execution_closure),  # noqa: SLF001
        "repository": {
            "full_name": repository["full_name"],
            "id": repository_id,
            "private": True,
        },
        "release": {"id": release_id, "tag": tag, "immutable": True},
        "assets": cast(list[receipts.JSONValue], normalized_assets),
        "evidence": {
            "source_bundle_digest": receipts.source_bundle_digest(sources),
            "canonicalization_vector_digest": receipts.canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_promotion_transaction.py",
            "provider": "github.com",
            "method": "immutable-recovery-capsule-verification",
        },
        "verified": True,
        "confidence": 1,
        "validation_status": "validated",
    }


def sign_recovery_capsule_verification(
    *,
    verification_claim: Mapping[str, object],
    identity_file: Path,
    owner_signing_keys_observation: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> receipts.JSONObject:
    """Bind a successful capsule verification to the current owner's key."""

    claim = receipts._copy_json_object(  # noqa: SLF001
        verification_claim, label="recovery capsule verification claim"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        claim,
        frozenset(
            {
                "schema",
                "capsule_manifest_digest",
                "candidate_manifest_digest",
                "transaction_authorization_digest",
                "execution_closure_digest",
                "repository",
                "release",
                "assets",
                "evidence",
                "provenance",
                "verified",
                "confidence",
                "validation_status",
            }
        ),
        label="recovery capsule verification claim",
    )
    if (
        claim.get("schema") != "kestrel.recovery_capsule_verification_claim.v1"
        or claim.get("verified") is not True
        or claim.get("confidence") != 1
        or claim.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("recovery capsule verification claim is invalid")
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError("recovery capsule verification clock must be aware UTC")
    now = now.astimezone(UTC).replace(microsecond=0)
    _public_key, fingerprint = receipts.owner_signing_key(
        owner_signing_keys_observation=owner_signing_keys_observation,
        principal=receipts.SIGNING_PRINCIPAL,
        _clock=lambda: now,
    )
    claim.update(
        {
            "owner_signing_keys_observation_digest": receipts._sha256(  # noqa: SLF001
                owner_signing_keys_observation
            ),
            "signing_principal": receipts.SIGNING_PRINCIPAL,
            "signing_key_fingerprint": fingerprint,
            "verified_at": receipts._format_timestamp(  # noqa: SLF001
                now, label="recovery capsule verified_at"
            ),
        }
    )
    receipt = receipts.canonical_json_bytes(claim)
    signature = receipts.sign_receipt_detached(
        receipt=receipt,
        identity_file=identity_file,
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    receipts.verify_owner_detached_signature_against_current_registration(
        receipt=receipt,
        signature=signature,
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    return {
        "schema": "kestrel.recovery_capsule_verification.v1",
        "verification": claim,
        "receipt_digest": receipts._sha256(receipt),  # noqa: SLF001
        "signature_digest": receipts._sha256(signature),  # noqa: SLF001
        "receipt_base64": base64.b64encode(receipt).decode("ascii"),
        "signature_base64": base64.b64encode(signature).decode("ascii"),
        "validation_status": "validated",
    }


def _command_verify_recovery_capsule(args: argparse.Namespace) -> int:
    output = Path(args.output)
    _require_new_outputs((output,))
    manifest = receipts._read_regular(  # noqa: SLF001
        Path(args.capsule_manifest),
        label="recovery capsule manifest",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    remote_sources: dict[str, bytes] = {}

    def read_remote_source(argument: str, *, label: str, name: str) -> bytes:
        path = Path(getattr(args, argument))
        raw = receipts._read_regular(  # noqa: SLF001
            path, label=label, max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES
        )
        remote_sources[
            {
                "recovery-repository-observation": "recovery-repository",
                "recovery-release-observation": "recovery-release",
                "recovery-assets-observation": "recovery-release-assets",
            }[name]
        ] = raw
        return _contract_source_body(
            raw,
            label=label,
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name=name,
        )

    verification_claim = verify_recovery_capsule(
        capsule_manifest=manifest,
        capsule_root=Path(args.capsule_root),
        recovery_repository_observation=read_remote_source(
            "recovery_repository_observation",
            label="recovery repository observation",
            name="recovery-repository-observation",
        ),
        recovery_release_observation=read_remote_source(
            "recovery_release_observation",
            label="recovery Release observation",
            name="recovery-release-observation",
        ),
        recovery_assets_observation=read_remote_source(
            "recovery_assets_observation",
            label="recovery asset observation",
            name="recovery-assets-observation",
        ),
        execution_closure=receipts._read_regular(  # noqa: SLF001
            Path(args.execution_closure),
            label="recovery execution closure",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        ),
        expected_candidate_digest=args.expected_candidate_digest,
        expected_transaction_authorization_digest=(args.expected_transaction_authorization_digest),
        remote_source_records=remote_sources,
    )
    verification = sign_recovery_capsule_verification(
        verification_claim=verification_claim,
        identity_file=Path(args.identity_file),
        owner_signing_keys_observation=receipts._read_regular(  # noqa: SLF001
            Path(args.owner_key_observation),
            label="recovery capsule owner signing keys observation",
            max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
        ),
    )
    if not receipts.write_once(output, receipts.canonical_json_bytes(verification)):
        raise receipts.ReleaseControlError(
            "recovery capsule verification output path must be empty"
        )
    return 0


def _stage_candidate_and_authority(
    *,
    manifest_path: Path,
    bundle_root: Path | None,
    transaction_authorization_path: Path,
    execution_authorization_path: Path | None,
    recovery_capsule_verification_path: Path,
) -> tuple[
    receipts.JSONObject,
    str,
    str | None,
    str,
    dict[str, bytes],
]:
    manifest_raw = receipts._read_regular(  # noqa: SLF001
        manifest_path,
        label="stage candidate manifest",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    candidate, candidate_repository_id = receipts._candidate_from_manifest(  # noqa: SLF001
        manifest_raw
    )
    if bundle_root is not None:
        bundled_manifest = receipts._read_regular(  # noqa: SLF001
            bundle_root / "candidate-manifest.json",
            label="bundled candidate manifest",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        if bundled_manifest != manifest_raw:
            raise receipts.ReleaseControlError("stage bundle candidate manifest bytes mismatch")
        try:
            verified_bundle = candidates.verify_candidate_bundle(
                _canonical_object(manifest_raw, label="stage candidate manifest"),
                bundle_root=bundle_root,
                source_root=SCRIPT_ROOT,
            )
        except ValueError as exc:
            raise receipts.ReleaseControlError(
                f"stage candidate bundle verification failed: {exc}"
            ) from exc
        if (
            verified_bundle.get("candidate_manifest_digest")
            != candidate.get("candidate_manifest_digest")
            or verified_bundle.get("artifact_set_digest") != candidate.get("artifact_set_digest")
            or verified_bundle.get("source_sha") != candidate.get("source_sha")
            or verified_bundle.get("source_tree") != candidate.get("source_tree")
            or verified_bundle.get("tag") != candidate.get("tag")
            or verified_bundle.get("version") != candidate.get("version")
        ):
            raise receipts.ReleaseControlError("stage candidate bundle identity mismatch")
    transaction_raw = receipts._read_regular(  # noqa: SLF001
        transaction_authorization_path,
        label="transaction authorization",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    transaction = _canonical_object(transaction_raw, label="transaction authorization")
    validate_server_authorization(transaction, expected_original_transaction_digest=None)
    transaction_run = receipts._object(  # noqa: SLF001
        transaction.get("promotion_run"), label="stage transaction promotion run"
    )
    if (
        transaction.get("authorization_kind") != "transaction"
        or transaction.get("mode") != "initiate"
        or transaction.get("candidate") != candidate
        or transaction_run.get("repository_id") != candidate_repository_id
    ):
        raise receipts.ReleaseControlError("stage transaction authorization identity mismatch")
    transaction_digest = receipts._sha256(transaction_raw)  # noqa: SLF001
    execution_digest: str | None = None
    source_records = {
        "candidate-manifest": manifest_raw,
        "transaction-authorization": transaction_raw,
    }
    if execution_authorization_path is not None:
        execution_raw = receipts._read_regular(  # noqa: SLF001
            execution_authorization_path,
            label="execution authorization",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        execution = _canonical_object(execution_raw, label="execution authorization")
        validate_server_authorization(
            execution,
            expected_original_transaction_digest=transaction_digest,
        )
        execution_run = receipts._object(  # noqa: SLF001
            execution.get("promotion_run"), label="stage execution promotion run"
        )
        if (
            execution.get("authorization_kind") != "execution"
            or execution.get("mode") != "recover_committed"
            or execution.get("candidate") != candidate
            or execution_run.get("repository_id") != candidate_repository_id
        ):
            raise receipts.ReleaseControlError("stage execution authorization identity mismatch")
        execution_digest = receipts._sha256(execution_raw)  # noqa: SLF001
        source_records["execution-authorization"] = execution_raw
    capsule_raw = receipts._read_regular(  # noqa: SLF001
        recovery_capsule_verification_path,
        label="recovery capsule verification",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    capsule = _canonical_object(capsule_raw, label="recovery capsule verification")
    capsule_digest = _authorization_capsule_digest(
        verification=capsule,
        candidate_manifest_digest=candidate.get("candidate_manifest_digest"),
        transaction_authorization=transaction_raw,
    )
    source_records["recovery-capsule-verification"] = capsule_raw
    return (
        candidate,
        transaction_digest,
        execution_digest,
        capsule_digest,
        source_records,
    )


def _candidate_product_release_contract(
    manifest: Mapping[str, object],
    *,
    transaction_authorization_digest: str,
    recovery_capsule_digest: str,
) -> receipts.JSONObject:
    """Derive the only permitted product Release request from candidate bytes."""

    checked = receipts._copy_json_object(  # noqa: SLF001
        manifest, label="product Release candidate manifest"
    )
    tag = receipts._validate_string(  # noqa: SLF001
        checked.get("tag"), label="product Release tag"
    )
    source = receipts._object(checked.get("source"), label="product Release source")  # noqa: SLF001
    source_sha = receipts._git_sha(  # noqa: SLF001
        source.get("commit_sha"), label="product Release source SHA"
    )
    transaction_digest = receipts._digest(  # noqa: SLF001
        transaction_authorization_digest,
        label="product Release transaction authorization",
    )
    capsule_digest = receipts._digest(  # noqa: SLF001
        recovery_capsule_digest, label="product Release recovery capsule"
    )
    candidate_digest = candidates.candidate_manifest_digest(checked)
    artifact_set_digest = receipts._digest(  # noqa: SLF001
        checked.get("artifact_set_digest"), label="product Release artifact set"
    )
    assets: list[receipts.JSONObject] = []
    names: set[str] = set()
    for raw_item in receipts._array(  # noqa: SLF001
        checked.get("artifacts"), label="product Release candidate artifacts"
    ):
        item = receipts._object(raw_item, label="product Release candidate artifact")  # noqa: SLF001
        path = receipts._validate_string(  # noqa: SLF001
            item.get("path"), label="product Release artifact path"
        )
        parts = PurePosixPath(path).parts
        if not parts or parts[0] != "release":
            continue
        if len(parts) != 2 or not parts[1] or parts[1] in names:
            raise receipts.ReleaseControlError(
                "product Release assets must have unique direct release/ basenames"
            )
        names.add(parts[1])
        assets.append(
            {
                "name": parts[1],
                "path": path,
                "sha256": receipts._digest(  # noqa: SLF001
                    item.get("sha256"), label="product Release asset digest"
                ),
                "size_bytes": receipts._safe_integer(  # noqa: SLF001
                    item.get("size_bytes"),
                    label="product Release asset size",
                    positive=True,
                ),
                "media_type": receipts._validate_string(  # noqa: SLF001
                    item.get("media_type"), label="product Release asset media type"
                ),
            }
        )
    assets.sort(key=lambda item: cast(str, item["name"]))
    if not assets:
        raise receipts.ReleaseControlError("candidate has no direct product Release assets")
    body = "\n".join(
        (
            tag,
            "",
            f"Kestrel-Release-Candidate: {candidate_digest}",
            f"Kestrel-Artifact-Set: {artifact_set_digest}",
            f"Kestrel-Source-SHA: {source_sha}",
            f"Kestrel-Transaction-Authorization: {transaction_digest}",
            f"Kestrel-Recovery-Capsule: {capsule_digest}",
        )
    )
    persisted: receipts.JSONObject = {
        "tag_name": tag,
        "target_commitish": source_sha,
        "name": f"Kestrel {tag}",
        "body": body,
        "prerelease": False,
    }
    create_request: receipts.JSONObject = {
        **persisted,
        "draft": True,
        "generate_release_notes": False,
        "make_latest": False,
    }
    return {
        "create_request": create_request,
        "persisted": persisted,
        "assets": cast(list[receipts.JSONValue], assets),
    }


def _classify_product_release_listing(
    value: object, *, contract: Mapping[str, object]
) -> receipts.JSONObject:
    """Classify one exhaustive paginated GitHub Release listing."""

    pages = receipts._array(value, label="complete product Release pagination")  # noqa: SLF001
    if not pages:
        raise receipts.ReleaseControlError("complete product Release pagination is empty")
    releases: list[receipts.JSONObject] = []
    release_ids: set[int] = set()
    for raw_page in pages:
        page = receipts._array(  # noqa: SLF001
            raw_page, label="complete product Release pagination page"
        )
        for raw_release in page:
            release = receipts._object(raw_release, label="product Release")  # noqa: SLF001
            release_id = receipts._safe_integer(  # noqa: SLF001
                release.get("id"), label="product Release ID", positive=True
            )
            if release_id in release_ids:
                raise receipts.ReleaseControlError(
                    "product Release pagination repeats a Release ID"
                )
            release_ids.add(release_id)
            releases.append(release)
    persisted = receipts._object(  # noqa: SLF001
        contract.get("persisted"), label="product Release persisted contract"
    )
    tag = persisted.get("tag_name")
    matches = [release for release in releases if release.get("tag_name") == tag]
    if len(matches) > 1:
        raise receipts.ReleaseControlError("product Release listing has multiple matching Releases")
    if not matches:
        return {"release": "missing", "assets": "missing", "release_id": None}
    release = matches[0]
    for key, expected in persisted.items():
        if release.get(key) != expected:
            raise receipts.ReleaseControlError(f"product Release persisted field conflicts: {key}")
    draft = release.get("draft")
    immutable = release.get("immutable")
    if draft is True and immutable is False:
        release_state = "draft_exact"
    elif draft is False and immutable is True:
        release_state = "immutable_exact"
    else:
        raise receipts.ReleaseControlError(
            "product Release is neither an exact draft nor immutable publication"
        )
    expected_assets = {
        cast(str, item["name"]): item
        for raw_item in receipts._array(  # noqa: SLF001
            contract.get("assets"), label="product Release asset contract"
        )
        for item in [receipts._object(raw_item, label="product Release asset contract")]  # noqa: SLF001
    }
    observed_assets: dict[str, receipts.JSONObject] = {}
    asset_ids: set[int] = set()
    for raw_asset in receipts._array(  # noqa: SLF001
        release.get("assets"), label="product Release assets"
    ):
        asset = receipts._object(raw_asset, label="product Release asset")  # noqa: SLF001
        asset_id = receipts._safe_integer(  # noqa: SLF001
            asset.get("id"), label="product Release asset ID", positive=True
        )
        name = receipts._validate_string(  # noqa: SLF001
            asset.get("name"), label="product Release asset name"
        )
        if asset_id in asset_ids or name in observed_assets:
            raise receipts.ReleaseControlError("product Release asset inventory is duplicated")
        if name not in expected_assets:
            raise receipts.ReleaseControlError("product Release contains an unexpected asset")
        expected = expected_assets[name]
        if asset.get("size") != expected.get("size_bytes") or asset.get("digest") != expected.get(
            "sha256"
        ):
            raise receipts.ReleaseControlError(
                "product Release asset identity conflicts with candidate bytes"
            )
        asset_ids.add(asset_id)
        observed_assets[name] = asset
    asset_state = "existing_exact" if set(observed_assets) == set(expected_assets) else "missing"
    return {
        "release": release_state,
        "assets": asset_state,
        "release_id": receipts._safe_integer(  # noqa: SLF001
            release.get("id"), label="product Release ID", positive=True
        ),
    }


def _classify_ghcr_digest_observation(value: object, *, expected_digests: Sequence[str]) -> str:
    """Derive GHCR state from exact digest-query status and tag observations."""

    observation = receipts._object(value, label="GHCR digest observation")  # noqa: SLF001
    receipts._require_exact_fields(  # noqa: SLF001
        observation,
        frozenset({"repository", "objects"}),
        label="GHCR digest observation",
    )
    if observation.get("repository") != candidates.OCI_REPOSITORY:
        raise receipts.ReleaseControlError("GHCR repository identity mismatch")
    expected = sorted(
        receipts._digest(item, label="expected GHCR object digest")  # noqa: SLF001
        for item in expected_digests
    )
    if not expected or len(expected) != len(set(expected)):
        raise receipts.ReleaseControlError("expected GHCR object inventory is empty or duplicated")
    objects = [
        receipts._object(item, label="GHCR digest query")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            observation.get("objects"), label="GHCR digest queries"
        )
    ]
    observed_digests: list[str] = []
    statuses: list[int] = []
    for item in objects:
        receipts._require_exact_fields(  # noqa: SLF001
            item,
            frozenset({"digest", "http_status", "tags"}),
            label="GHCR digest query",
        )
        digest = receipts._digest(item.get("digest"), label="GHCR object digest")  # noqa: SLF001
        status = item.get("http_status")
        if isinstance(status, bool) or status not in {200, 404}:
            raise receipts.ReleaseControlError("GHCR object query status is neither 200 nor 404")
        tags = receipts._array(item.get("tags"), label="GHCR object tags")  # noqa: SLF001
        if tags:
            raise receipts.ReleaseControlError(
                "digest-addressed GHCR object unexpectedly has a tag"
            )
        observed_digests.append(digest)
        statuses.append(status)
    if observed_digests != expected:
        raise receipts.ReleaseControlError(
            "GHCR digest query inventory is incomplete, duplicated, or unsorted"
        )
    return "existing_exact" if all(status == 200 for status in statuses) else "missing"


def _expected_oci_object_digests(bundle_root: Path) -> tuple[str, ...]:
    """Return every candidate OCI object digest that promotion must observe."""

    descriptor_raw = receipts._read_regular(  # noqa: SLF001
        bundle_root / "containers/oci-descriptor.json",
        label="candidate OCI descriptor",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    descriptor = receipts._object(  # noqa: SLF001
        receipts.strict_canonical_json(descriptor_raw, label="candidate OCI descriptor"),
        label="candidate OCI descriptor",
    )
    if descriptor.get("repository") != candidates.OCI_REPOSITORY:
        raise receipts.ReleaseControlError("candidate OCI repository mismatch")
    digests = {
        receipts._digest(  # noqa: SLF001
            descriptor.get("index_digest"), label="candidate OCI index digest"
        )
    }
    blobs = bundle_root / "containers/oci-layout/blobs/sha256"
    if not blobs.is_dir() or blobs.is_symlink():
        raise receipts.ReleaseControlError("candidate OCI blob inventory is not a real directory")
    for path in blobs.iterdir():
        if (
            path.is_symlink()
            or not path.is_file()
            or re.fullmatch(r"[0-9a-f]{64}", path.name) is None
        ):
            raise receipts.ReleaseControlError(
                "candidate OCI blob inventory contains an invalid entry"
            )
        digests.add(f"sha256:{path.name}")
    if len(digests) < 2:
        raise receipts.ReleaseControlError("candidate OCI object inventory is empty")
    return tuple(sorted(digests))


def _expected_oci_object_digests_from_manifest(
    manifest: Mapping[str, object],
) -> tuple[str, ...]:
    """Derive the final GHCR inventory solely from candidate-manifest identities."""

    digests: set[str] = set()
    for raw_subject in receipts._array(  # noqa: SLF001
        manifest.get("attestation_subjects"), label="final OCI attestation subjects"
    ):
        subject = receipts._object(raw_subject, label="final OCI attestation subject")  # noqa: SLF001
        if subject.get("kind") == "oci_index":
            if subject.get("name") != candidates.OCI_REPOSITORY:
                raise receipts.ReleaseControlError("final OCI repository identity mismatch")
            digests.add(
                receipts._digest(  # noqa: SLF001
                    subject.get("digest"), label="final OCI index digest"
                )
            )
    for raw_artifact in receipts._array(  # noqa: SLF001
        manifest.get("artifacts"), label="final candidate artifacts"
    ):
        artifact = receipts._object(raw_artifact, label="final candidate artifact")  # noqa: SLF001
        path = receipts._validate_string(  # noqa: SLF001
            artifact.get("path"), label="final candidate artifact path"
        )
        if path.startswith("containers/oci-layout/blobs/sha256/"):
            filename = PurePosixPath(path).name
            digest = receipts._digest(  # noqa: SLF001
                artifact.get("sha256"), label="final OCI blob digest"
            )
            if digest != f"sha256:{filename}":
                raise receipts.ReleaseControlError(
                    "final OCI blob path does not match its candidate digest"
                )
            digests.add(digest)
    if not digests:
        raise receipts.ReleaseControlError("final candidate has no OCI object inventory")
    return tuple(sorted(digests))


def _classify_commit_tag_observation(
    value: object,
    *,
    candidate: Mapping[str, object],
    transaction_authorization_digest: str,
    recovery_capsule_digest: str,
) -> str:
    """Derive commit-marker state from a tag ref plus annotated-tag peel."""

    observation = receipts._object(value, label="commit tag observation")  # noqa: SLF001
    receipts._require_exact_fields(  # noqa: SLF001
        observation,
        frozenset({"http_status", "ref", "tag"}),
        label="commit tag observation",
    )
    status = observation.get("http_status")
    if status == 404:
        if observation.get("ref") is not None or observation.get("tag") is not None:
            raise receipts.ReleaseControlError("missing commit tag observation contains tag data")
        return "missing"
    if isinstance(status, bool) or status != 200:
        raise receipts.ReleaseControlError("commit tag observation status is neither 200 nor 404")
    checked_candidate = receipts._copy_json_object(  # noqa: SLF001
        candidate, label="commit tag candidate"
    )
    tag_name = receipts._validate_string(  # noqa: SLF001
        checked_candidate.get("tag"), label="commit tag name"
    )
    source_sha = receipts._git_sha(  # noqa: SLF001
        checked_candidate.get("source_sha"), label="commit tag source SHA"
    )
    ref = receipts._object(observation.get("ref"), label="commit tag ref")  # noqa: SLF001
    ref_object = receipts._object(ref.get("object"), label="commit tag ref object")  # noqa: SLF001
    tag = receipts._object(observation.get("tag"), label="annotated tag object")  # noqa: SLF001
    tag_object = receipts._object(tag.get("object"), label="annotated tag target")  # noqa: SLF001
    tag_sha = receipts._git_sha(tag.get("sha"), label="annotated tag SHA")  # noqa: SLF001
    expected_message = build_annotated_tag_message(
        candidate=checked_candidate,
        transaction_authorization_digest=transaction_authorization_digest,
        recovery_capsule_digest=recovery_capsule_digest,
    )
    if (
        ref.get("ref") != f"refs/tags/{tag_name}"
        or ref_object != {"type": "tag", "sha": tag_sha}
        or tag.get("tag") != tag_name
        or tag.get("message") != expected_message
        or tag_object != {"type": "commit", "sha": source_sha}
    ):
        raise receipts.ReleaseControlError("commit marker is not the exact annotated tag and peel")
    return "existing_exact"


def _classify_promotion_attestation_observation(
    value: object, *, manifest: Mapping[str, object]
) -> dict[str, str]:
    """Derive attestation state from subject-bound GitHub CLI verification JSON."""

    observation = receipts._object(value, label="promotion attestation observation")  # noqa: SLF001
    receipts._require_exact_fields(  # noqa: SLF001
        observation,
        frozenset({"subjects"}),
        label="promotion attestation observation",
    )
    expected_subjects = [
        receipts._object(item, label="candidate attestation subject")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            manifest.get("attestation_subjects"),
            label="candidate attestation subjects",
        )
    ]
    observed_subjects = [
        receipts._object(item, label="promotion attestation subject")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            observation.get("subjects"), label="promotion attestation subjects"
        )
    ]
    expected_identities = [
        (item.get("kind"), item.get("name"), item.get("digest")) for item in expected_subjects
    ]
    observed_identities: list[tuple[object, object, object]] = []
    states = {"file": "existing_exact", "oci_index": "existing_exact"}
    seen_kinds: set[str] = set()
    for item in observed_subjects:
        receipts._require_exact_fields(  # noqa: SLF001
            item,
            frozenset({"kind", "name", "digest", "verification"}),
            label="promotion attestation subject",
        )
        kind = receipts._validate_string(  # noqa: SLF001
            item.get("kind"), label="promotion attestation subject kind"
        )
        if kind not in states:
            raise receipts.ReleaseControlError("promotion attestation subject kind is invalid")
        seen_kinds.add(kind)
        observed_identities.append((item.get("kind"), item.get("name"), item.get("digest")))
        verification = item.get("verification")
        if verification is None:
            states[kind] = "missing"
            continue
        name = receipts._validate_string(  # noqa: SLF001
            item.get("name"), label="promotion attestation subject name"
        )
        digest = receipts._digest(  # noqa: SLF001
            item.get("digest"), label="promotion attestation subject digest"
        )
        _verify_attestation_output(
            receipts.canonical_external_json_bytes(verification),
            expected_name=name,
            expected_digest=digest,
        )
    if observed_identities != expected_identities or seen_kinds != set(states):
        raise receipts.ReleaseControlError(
            "promotion attestation subject inventory does not match the candidate"
        )
    return states


def _candidate_pypi_files(
    manifest: Mapping[str, object],
) -> dict[str, receipts.JSONObject]:
    """Derive the exact wheel and sdist identities from the candidate manifest."""

    version = receipts._validate_string(  # noqa: SLF001
        manifest.get("version"), label="PyPI candidate version"
    )
    wheel_pattern = re.compile(rf"^nested_memvid_agent-{re.escape(version)}-[A-Za-z0-9_.-]+\.whl$")
    sdist_name = f"nested_memvid_agent-{version}.tar.gz"
    files: dict[str, receipts.JSONObject] = {}
    wheel_count = 0
    sdist_count = 0
    for raw_item in receipts._array(  # noqa: SLF001
        manifest.get("artifacts"), label="PyPI candidate artifacts"
    ):
        item = receipts._object(raw_item, label="PyPI candidate artifact")  # noqa: SLF001
        path = receipts._validate_string(  # noqa: SLF001
            item.get("path"), label="PyPI candidate artifact path"
        )
        parts = PurePosixPath(path).parts
        if len(parts) != 2 or parts[0] != "release":
            continue
        filename = parts[1]
        if wheel_pattern.fullmatch(filename) is not None:
            wheel_count += 1
        elif filename == sdist_name:
            sdist_count += 1
        else:
            continue
        if filename in files:
            raise receipts.ReleaseControlError("PyPI candidate distribution filename is duplicated")
        files[filename] = {
            "path": path,
            "sha256": receipts._digest(  # noqa: SLF001
                item.get("sha256"), label="PyPI candidate distribution digest"
            ),
            "size_bytes": receipts._safe_integer(  # noqa: SLF001
                item.get("size_bytes"),
                label="PyPI candidate distribution size",
                positive=True,
            ),
        }
    if wheel_count != 1 or sdist_count != 1 or len(files) != 2:
        raise receipts.ReleaseControlError(
            "PyPI candidate must contain exactly one wheel and one sdist"
        )
    return dict(sorted(files.items()))


def _classify_pypi_project_observation(
    value: object,
    *,
    version: str,
    expected_files: Mapping[str, Mapping[str, object]],
) -> receipts.JSONObject:
    """Classify one public PyPI project JSON response against candidate files."""

    project = receipts._object(value, label="PyPI project observation")  # noqa: SLF001
    info = receipts._object(project.get("info"), label="PyPI project info")  # noqa: SLF001
    if info.get("name") != "nested-memvid-agent":
        raise receipts.ReleaseControlError("PyPI project identity mismatch")
    serial = receipts._safe_integer(  # noqa: SLF001
        project.get("last_serial"), label="PyPI project serial", positive=True
    )
    releases = receipts._object(  # noqa: SLF001
        project.get("releases"), label="PyPI project releases"
    )
    raw_files = releases.get(version, [])
    observed: dict[str, receipts.JSONObject] = {}
    for raw_item in receipts._array(raw_files, label="PyPI version files"):  # noqa: SLF001
        item = receipts._object(raw_item, label="PyPI version file")  # noqa: SLF001
        filename = receipts._validate_string(  # noqa: SLF001
            item.get("filename"), label="PyPI version filename"
        )
        if filename in observed:
            raise receipts.ReleaseControlError("PyPI version file inventory is duplicated")
        if filename not in expected_files:
            raise receipts.ReleaseControlError("PyPI version contains a foreign candidate filename")
        expected = expected_files[filename]
        digests = receipts._object(item.get("digests"), label="PyPI file digests")  # noqa: SLF001
        observed_sha = digests.get("sha256")
        expected_sha = receipts._digest(  # noqa: SLF001
            expected.get("sha256"), label="expected PyPI file digest"
        ).removeprefix("sha256:")
        if observed_sha != expected_sha:
            raise receipts.ReleaseControlError("PyPI file hash conflicts with the candidate")
        if item.get("size") != expected.get("size_bytes"):
            raise receipts.ReleaseControlError("PyPI file size conflicts with the candidate")
        if item.get("yanked") is not False:
            raise receipts.ReleaseControlError("PyPI candidate file is yanked")
        url = receipts._validate_string(  # noqa: SLF001
            item.get("url"), label="PyPI candidate file URL"
        )
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "files.pythonhosted.org"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or not parsed.path.endswith(f"/{quote(filename)}")
            or parsed.query
            or parsed.fragment
        ):
            raise receipts.ReleaseControlError("PyPI candidate file URL is invalid")
        observed[filename] = item
    present = sorted(observed)
    missing = sorted(set(expected_files) - set(observed))
    return {
        "present": cast(list[receipts.JSONValue], present),
        "missing": cast(list[receipts.JSONValue], missing),
        "last_serial": serial,
    }


def _verify_pypi_integrity_provenance(
    value: object, *, filename: str, expected_digest: str
) -> receipts.JSONObject:
    """Strictly parse one PyPI Integrity v1 provenance and publisher identity."""

    provenance = receipts._object(value, label="PyPI Integrity provenance")  # noqa: SLF001
    receipts._require_exact_fields(  # noqa: SLF001
        provenance,
        frozenset({"version", "attestation_bundles"}),
        label="PyPI Integrity provenance",
    )
    if provenance.get("version") != 1:
        raise receipts.ReleaseControlError("PyPI Integrity provenance version mismatch")
    expected_hex = receipts._digest(  # noqa: SLF001
        expected_digest, label="PyPI Integrity expected digest"
    ).removeprefix("sha256:")
    bundles = [
        receipts._object(item, label="PyPI attestation bundle")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            provenance.get("attestation_bundles"),
            label="PyPI attestation bundles",
        )
    ]
    if not bundles:
        raise receipts.ReleaseControlError("PyPI Integrity provenance has no bundles")
    publish_count = 0
    publish_bundle_count = 0
    publisher_result: receipts.JSONObject | None = None
    for bundle in bundles:
        receipts._require_exact_fields(  # noqa: SLF001
            bundle,
            frozenset({"publisher", "attestations"}),
            label="PyPI attestation bundle",
        )
        publisher = receipts._object(  # noqa: SLF001
            bundle.get("publisher"), label="PyPI attestation publisher"
        )
        attestations = [
            receipts._object(item, label="PyPI attestation")  # noqa: SLF001
            for item in receipts._array(  # noqa: SLF001
                bundle.get("attestations"), label="PyPI attestations"
            )
        ]
        if not attestations:
            raise receipts.ReleaseControlError("PyPI attestation bundle is empty")
        bundle_has_publish = False
        predicate_types: set[str] = set()
        for attestation in attestations:
            receipts._require_exact_fields(  # noqa: SLF001
                attestation,
                frozenset({"version", "envelope", "verification_material"}),
                label="PyPI attestation",
            )
            if attestation.get("version") != 1:
                raise receipts.ReleaseControlError("PyPI attestation version mismatch")
            receipts._object(  # noqa: SLF001
                attestation.get("verification_material"),
                label="PyPI attestation verification material",
            )
            envelope = receipts._object(  # noqa: SLF001
                attestation.get("envelope"), label="PyPI attestation envelope"
            )
            receipts._require_exact_fields(  # noqa: SLF001
                envelope,
                frozenset({"signature", "statement"}),
                label="PyPI attestation envelope",
            )
            signature = _decode_observation_bytes(
                envelope.get("signature"), label="PyPI attestation signature"
            )
            if not signature:
                raise receipts.ReleaseControlError("PyPI attestation signature is empty")
            statement_raw = _decode_observation_bytes(
                envelope.get("statement"), label="PyPI attestation statement"
            )
            statement = receipts._object(  # noqa: SLF001
                receipts.parse_external_json_bytes(
                    statement_raw, label="PyPI attestation statement"
                ),
                label="PyPI attestation statement",
            )
            receipts._require_exact_fields(  # noqa: SLF001
                statement,
                frozenset({"_type", "subject", "predicateType", "predicate"}),
                label="PyPI attestation statement",
            )
            if statement.get("_type") != "https://in-toto.io/Statement/v1":
                raise receipts.ReleaseControlError("PyPI attestation statement type mismatch")
            predicate_type = receipts._validate_string(  # noqa: SLF001
                statement.get("predicateType"),
                label="PyPI attestation predicate type",
            )
            if predicate_type in predicate_types:
                raise receipts.ReleaseControlError("PyPI attestation predicate type is duplicated")
            predicate_types.add(predicate_type)
            subjects = receipts._array(  # noqa: SLF001
                statement.get("subject"), label="PyPI attestation subjects"
            )
            if len(subjects) != 1:
                raise receipts.ReleaseControlError("PyPI attestation subject is not a singleton")
            subject = receipts._object(subjects[0], label="PyPI attestation subject")  # noqa: SLF001
            receipts._require_exact_fields(  # noqa: SLF001
                subject,
                frozenset({"name", "digest"}),
                label="PyPI attestation subject",
            )
            digest = receipts._object(  # noqa: SLF001
                subject.get("digest"), label="PyPI attestation subject digest"
            )
            if (
                subject.get("name") != filename
                or set(digest) != {"sha256"}
                or digest.get("sha256") != expected_hex
            ):
                raise receipts.ReleaseControlError(
                    "PyPI attestation subject conflicts with candidate bytes"
                )
            if predicate_type == "https://docs.pypi.org/attestations/publish/v1":
                if statement.get("predicate") is not None:
                    raise receipts.ReleaseControlError(
                        "PyPI publish attestation predicate must be null"
                    )
                publish_count += 1
                bundle_has_publish = True
        if bundle_has_publish:
            publish_bundle_count += 1
            allowed_publisher_fields = {
                "kind",
                "repository",
                "workflow",
                "environment",
                "claims",
            }
            if (
                not set(publisher) <= allowed_publisher_fields
                or publisher.get("claims") is not None
            ):
                raise receipts.ReleaseControlError("PyPI publish publisher fields are invalid")
            expected_publisher: receipts.JSONObject = {
                "kind": "GitHub",
                "repository": "John-MiracleWorker/Kestrel",
                "workflow": "release.yml",
                "environment": "pypi",
            }
            if any(publisher.get(key) != expected for key, expected in expected_publisher.items()):
                raise receipts.ReleaseControlError("PyPI publish publisher identity mismatch")
            publisher_result = expected_publisher
    if publish_count != 1 or publish_bundle_count != 1 or publisher_result is None:
        raise receipts.ReleaseControlError(
            "PyPI provenance must contain exactly one publish identity"
        )
    return publisher_result


def _run_pypi_attestations_verifier(
    *,
    distribution_path: Path,
    provenance: bytes,
) -> bytes:
    """Execute the lock-pinned verifier in isolated offline mode."""

    try:
        version = importlib.metadata.version("pypi-attestations")
    except importlib.metadata.PackageNotFoundError as exc:
        raise receipts.ReleaseControlError("pypi-attestations verifier is unavailable") from exc
    if version != "0.0.30":
        raise receipts.ReleaseControlError("pypi-attestations verifier version mismatch")
    if (
        not distribution_path.is_absolute()
        or not distribution_path.is_file()
        or distribution_path.is_symlink()
    ):
        raise receipts.ReleaseControlError("PyPI verifier distribution path is invalid")
    with tempfile.TemporaryDirectory(prefix="kestrel-pypi-provenance-") as temporary_root:
        provenance_path = Path(temporary_root) / "provenance.json"
        provenance_path.write_bytes(provenance)
        completed = subprocess.run(  # noqa: S603  # nosec B603
            [
                sys.executable,
                "-I",
                "-m",
                "pypi_attestations",
                "verify",
                "pypi",
                "--offline",
                "--repository",
                "https://github.com/John-MiracleWorker/Kestrel",
                "--provenance-file",
                str(provenance_path),
                str(distribution_path),
            ],
            capture_output=True,
            check=False,
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONNOUSERSITE": "1",
            },
            timeout=60,
        )
    if (
        completed.returncode != 0
        or len(completed.stdout) > receipts.MAX_SOURCE_BODY_BYTES
        or len(completed.stderr) > receipts.MAX_SOURCE_BODY_BYTES
    ):
        raise receipts.ReleaseControlError(
            "pypi-attestations rejected the exact distribution provenance"
        )
    return bytes(completed.stdout)


def _validate_pypi_provenance_evidence(
    *,
    integrity_observations: object,
    provenance_verifications: object,
    expected_files: Mapping[str, Mapping[str, object]],
    persisted_filenames: Sequence[str],
    distribution_root: Path,
) -> list[receipts.JSONObject]:
    """Join raw Integrity provenance to pinned verifier results for every file."""

    integrity = receipts._object(  # noqa: SLF001
        integrity_observations, label="PyPI Integrity observations"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        integrity,
        frozenset({"schema", "files"}),
        label="PyPI Integrity observations",
    )
    if integrity.get("schema") != "pypi.integrity_observations.v1":
        raise receipts.ReleaseControlError("PyPI Integrity observation schema mismatch")
    raw_integrity_files = [
        receipts._object(item, label="PyPI Integrity file")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            integrity.get("files"), label="PyPI Integrity files"
        )
    ]
    verifications = receipts._object(  # noqa: SLF001
        provenance_verifications, label="PyPI provenance verifications"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        verifications,
        frozenset({"schema", "tool", "files"}),
        label="PyPI provenance verifications",
    )
    if verifications.get("schema") != "pypi.provenance_verifications.v1":
        raise receipts.ReleaseControlError("PyPI provenance verification schema mismatch")
    tool = receipts._object(  # noqa: SLF001
        verifications.get("tool"), label="PyPI provenance verifier"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        tool,
        frozenset({"name", "version"}),
        label="PyPI provenance verifier",
    )
    if tool != {"name": "pypi-attestations", "version": "0.0.30"}:
        raise receipts.ReleaseControlError("PyPI provenance verifier version mismatch")
    raw_verification_files = [
        receipts._object(item, label="PyPI provenance verification file")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            verifications.get("files"), label="PyPI provenance verification files"
        )
    ]
    expected_names = sorted(persisted_filenames)
    integrity_names = [item.get("filename") for item in raw_integrity_files]
    verification_names = [item.get("filename") for item in raw_verification_files]
    if integrity_names != expected_names or verification_names != expected_names:
        raise receipts.ReleaseControlError(
            "PyPI provenance evidence does not cover every persisted file exactly once"
        )
    results: list[receipts.JSONObject] = []
    for integrity_item, verification_item in zip(
        raw_integrity_files, raw_verification_files, strict=True
    ):
        receipts._require_exact_fields(  # noqa: SLF001
            integrity_item,
            frozenset({"filename", "provenance"}),
            label="PyPI Integrity file",
        )
        receipts._require_exact_fields(  # noqa: SLF001
            verification_item,
            frozenset(
                {
                    "filename",
                    "distribution_sha256",
                    "provenance_sha256",
                    "exit_code",
                }
            ),
            label="PyPI provenance verification file",
        )
        filename = cast(str, integrity_item["filename"])
        expected = expected_files.get(filename)
        if expected is None:
            raise receipts.ReleaseControlError("PyPI provenance names a foreign distribution")
        expected_digest = receipts._digest(  # noqa: SLF001
            expected.get("sha256"), label="PyPI expected distribution digest"
        )
        relative_path = PurePosixPath(
            receipts._validate_string(  # noqa: SLF001
                expected.get("path"), label="PyPI expected distribution path"
            )
        )
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            raise receipts.ReleaseControlError("PyPI expected distribution path is unsafe")
        root = distribution_root.resolve(strict=True)
        distribution_path = root.joinpath(*relative_path.parts)
        resolved_distribution = distribution_path.resolve(strict=True)
        if (
            root not in resolved_distribution.parents
            or not distribution_path.is_file()
            or distribution_path.is_symlink()
        ):
            raise receipts.ReleaseControlError("PyPI distribution is outside the candidate root")
        distribution_raw = receipts._read_regular(  # noqa: SLF001
            distribution_path,
            label=f"PyPI candidate distribution {filename}",
            max_bytes=2_147_483_648,
        )
        if (
            len(distribution_raw) != expected.get("size_bytes")
            or receipts._sha256(distribution_raw) != expected_digest  # noqa: SLF001
        ):
            raise receipts.ReleaseControlError("PyPI candidate distribution bytes mismatch")
        provenance = integrity_item.get("provenance")
        provenance_raw = receipts.canonical_external_json_bytes(provenance)
        if (
            verification_item.get("filename") != filename
            or verification_item.get("distribution_sha256") != expected_digest
            or verification_item.get("provenance_sha256") != receipts._sha256(provenance_raw)  # noqa: SLF001
            or verification_item.get("exit_code") != 0
        ):
            raise receipts.ReleaseControlError(
                "PyPI provenance verifier result does not bind exact file bytes"
            )
        _run_pypi_attestations_verifier(
            distribution_path=resolved_distribution,
            provenance=provenance_raw,
        )
        publisher = _verify_pypi_integrity_provenance(
            provenance, filename=filename, expected_digest=expected_digest
        )
        results.append(
            {
                "filename": filename,
                "sha256": expected_digest,
                "publisher": publisher,
                "provenance_sha256": receipts._sha256(provenance_raw),  # noqa: SLF001
                "verifier": "pypi-attestations 0.0.30",
            }
        )
    return results


def _command_tag_message(args: argparse.Namespace) -> int:
    candidate, transaction, execution, capsule, _sources = _stage_candidate_and_authority(
        manifest_path=Path(args.manifest),
        bundle_root=Path(args.bundle_root),
        transaction_authorization_path=Path(args.transaction_authorization),
        execution_authorization_path=None,
        recovery_capsule_verification_path=Path(args.recovery_capsule_verification),
    )
    if execution is not None:
        raise receipts.ReleaseControlError(
            "annotated tag message cannot bind recovery execution authority"
        )
    sys.stdout.write(
        build_annotated_tag_message(
            candidate=candidate,
            transaction_authorization_digest=transaction,
            recovery_capsule_digest=capsule,
        )
        + "\n"
    )
    return 0


def _reconciliation_stage_chain(
    *,
    stage_root: Path,
    candidate: Mapping[str, object],
    transaction_authorization_digest: str,
    execution_authorization_digest: str | None,
    recovery_capsule_digest: str,
    require_complete: bool,
    source_records: dict[str, bytes],
    stage_statuses: list[tuple[bool, bool, bool]] | None = None,
) -> list[receipts.JSONObject]:
    if not stage_root.is_dir() or stage_root.is_symlink():
        raise receipts.ReleaseControlError("stage records path must be a real directory")
    chain: list[receipts.JSONObject] = []
    previous_raw: bytes | None = None
    missing_predecessor = False
    for filename, schema in _RELEASE_STAGE_SEQUENCE:
        path = stage_root / filename
        if path.is_symlink():
            raise receipts.ReleaseControlError("stage record is not a regular file")
        if not path.exists():
            missing_predecessor = True
            continue
        if not path.is_file():
            raise receipts.ReleaseControlError("stage record is not a regular file")
        if missing_predecessor:
            raise receipts.ReleaseControlError(
                "reconciliation stage chain is not a chronological prefix"
            )
        raw = receipts._read_regular(  # noqa: SLF001
            path,
            label=filename,
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        stage = _canonical_object(raw, label=filename)
        validate_release_stage_record(stage)
        expected_previous = (
            None if previous_raw is None else receipts._sha256(previous_raw)  # noqa: SLF001
        )
        if (
            stage.get("schema") != schema
            or stage.get("candidate") != candidate
            or stage.get("transaction_authorization_digest") != transaction_authorization_digest
            or stage.get("execution_authorization_digest") != execution_authorization_digest
            or stage.get("recovery_capsule_digest") != recovery_capsule_digest
            or stage.get("previous_record_digest") != expected_previous
            or (require_complete and stage.get("completed") is not True)
            or (previous_raw is not None and chain[-1].get("completed") is not True)
        ):
            raise receipts.ReleaseControlError(
                "reconciliation stage chain authority or completion mismatch"
            )
        chain.append(
            {
                "filename": filename,
                "schema": schema,
                "canonical_sha256": receipts._sha256(raw),  # noqa: SLF001
                "artifact": None,
                "completed": stage["completed"],
            }
        )
        if stage_statuses is not None:
            stage_statuses.append(
                (
                    stage.get("completed") is True,
                    stage.get("uncertain") is True,
                    stage.get("pending") is True,
                )
            )
        source_records[filename] = raw
        previous_raw = raw
    if require_complete and len(chain) != len(_RELEASE_STAGE_SEQUENCE):
        raise receipts.ReleaseControlError(
            "completed reconciliation omits a successful stage producer"
        )
    public_chain: list[receipts.JSONObject] = []
    for item in chain:
        public_chain.append(
            {
                "filename": item["filename"],
                "schema": item["schema"],
                "canonical_sha256": item["canonical_sha256"],
                "artifact": item["artifact"],
            }
        )
    public_chain.sort(key=lambda item: cast(str, item["filename"]))
    return public_chain


def _derive_final_release_summary(
    *,
    stage_statuses: Sequence[tuple[bool, bool, bool]],
    full_chain: bool,
    remote_complete: bool,
    failure_code: str | None,
    next_action: str,
) -> tuple[bool, bool, bool]:
    """Derive terminal summary flags from validated stage and failure evidence."""

    stage_complete = (
        full_chain
        and len(stage_statuses) == len(_RELEASE_STAGE_SEQUENCE)
        and all(completed for completed, _uncertain, _pending in stage_statuses)
    )
    stage_uncertain = any(uncertain for _completed, uncertain, _pending in stage_statuses)
    stage_pending = any(pending for _completed, _uncertain, pending in stage_statuses)
    normalized_failure = "" if failure_code is None else failure_code.lower()
    failure_uncertain = any(
        token in normalized_failure for token in ("unknown", "uncertain", "ambiguous", "conflict")
    )
    uncertain = stage_uncertain or failure_uncertain
    completed = (
        stage_complete
        and remote_complete
        and not stage_uncertain
        and not stage_pending
        and failure_code is None
        and next_action == "none"
    )
    pending = not completed and not uncertain and (stage_pending or next_action != "none")
    if not completed and not uncertain and not pending and failure_code is None:
        raise receipts.ReleaseControlError(
            "incomplete release reconciliation lacks a failure or next action"
        )
    return completed, uncertain, pending


def _validate_final_lock_proof(value: object, *, expected_workflow_digest: str) -> None:
    """Require the active no-bypass main lock and byte-identical ingress."""

    proof = receipts._object(value, label="final release lock proof")  # noqa: SLF001
    receipts._require_exact_fields(  # noqa: SLF001
        proof,
        frozenset(
            {
                "main_lock",
                "workflow",
                "default_branch_workflow_sha256",
                "capsule_workflow_sha256",
            }
        ),
        label="final release lock proof",
    )
    receipts._validate_ruleset(  # noqa: SLF001
        proof.get("main_lock"),
        label="final release main lock",
        expected_name="kestrel-release-transaction-main-lock",
        expected_target="branch",
        expected_include="refs/heads/main",
    )
    workflow = receipts._object(  # noqa: SLF001
        proof.get("workflow"), label="final release ingress workflow"
    )
    receipts._require_exact_fields(  # noqa: SLF001
        workflow,
        frozenset({"id", "path", "state", "default_branch"}),
        label="final release ingress workflow",
    )
    receipts._safe_integer(  # noqa: SLF001
        workflow.get("id"), label="final release ingress workflow ID", positive=True
    )
    expected_digest = receipts._digest(  # noqa: SLF001
        expected_workflow_digest, label="final release expected workflow digest"
    )
    if (
        workflow.get("path") != ".github/workflows/release.yml"
        or workflow.get("state") != "active"
        or workflow.get("default_branch") != "main"
        or proof.get("default_branch_workflow_sha256") != expected_digest
        or proof.get("capsule_workflow_sha256") != expected_digest
    ):
        raise receipts.ReleaseControlError("final release lock or ingress byte proof mismatch")


def _validate_final_lock_sources(
    *,
    main_lock: object,
    workflow: object,
    default_branch_workflow: bytes,
    expected_workflow: bytes,
) -> None:
    """Validate the final lock directly from fresh registered source bodies."""

    receipts._validate_ruleset(  # noqa: SLF001
        main_lock,
        label="final release main lock",
        expected_name="kestrel-release-transaction-main-lock",
        expected_target="branch",
        expected_include="refs/heads/main",
    )
    checked_workflow = receipts._object(  # noqa: SLF001
        workflow, label="final release ingress workflow"
    )
    receipts._safe_integer(  # noqa: SLF001
        checked_workflow.get("id"), label="final release ingress workflow ID", positive=True
    )
    if (
        checked_workflow.get("path") != ".github/workflows/release.yml"
        or checked_workflow.get("state") != "active"
        or checked_workflow.get("default_branch") != "main"
        or not default_branch_workflow
        or default_branch_workflow != expected_workflow
    ):
        raise receipts.ReleaseControlError("final release lock or ingress byte source mismatch")


def _command_plan_preparation(args: argparse.Namespace) -> int:
    output = Path(args.output)
    _require_new_outputs((output,))
    candidate, transaction, execution, capsule, sources = _stage_candidate_and_authority(
        manifest_path=Path(args.manifest),
        bundle_root=Path(args.bundle_root),
        transaction_authorization_path=Path(args.transaction_authorization),
        execution_authorization_path=(
            None if args.execution_authorization is None else Path(args.execution_authorization)
        ),
        recovery_capsule_verification_path=Path(args.recovery_capsule_verification),
    )
    manifest = _canonical_object(
        sources["candidate-manifest"], label="preparation candidate manifest"
    )
    release_contract = _candidate_product_release_contract(
        manifest,
        transaction_authorization_digest=transaction,
        recovery_capsule_digest=capsule,
    )
    release_raw = _read_observation_or_record(
        Path(args.release_list_observation),
        label="preparation complete product Release listing",
    )
    release_state = _classify_product_release_listing(
        receipts.parse_external_json_bytes(
            release_raw, label="preparation complete product Release listing"
        ),
        contract=release_contract,
    )
    ghcr_raw = _read_observation_or_record(
        Path(args.ghcr_observation), label="preparation GHCR observation"
    )
    expected_oci_digests = _expected_oci_object_digests(Path(args.bundle_root))
    ghcr_state = _classify_ghcr_digest_observation(
        receipts.parse_external_json_bytes(ghcr_raw, label="preparation GHCR observation"),
        expected_digests=expected_oci_digests,
    )
    state: receipts.JSONObject = {
        "schema": "kestrel.release_stage_state.v1",
        "stage": 1,
        "operations": [
            {
                "operation": "create_github_release_draft",
                "state": ("missing" if release_state["release"] == "missing" else "existing_exact"),
            },
            {"operation": "publish_ghcr_digests", "state": ghcr_state},
            {
                "operation": "upload_github_release_assets",
                "state": release_state["assets"],
            },
        ],
        "complete": True,
    }
    create_request = receipts._object(  # noqa: SLF001
        release_contract.get("create_request"),
        label="preparation product Release create request",
    )
    asset_request: receipts.JSONObject = {
        "tag_name": create_request["tag_name"],
        "release_id": release_state["release_id"],
        "assets": release_contract["assets"],
    }
    ghcr_request: receipts.JSONObject = {
        "repository": candidates.OCI_REPOSITORY,
        "digests": list(expected_oci_digests),
    }
    plan = build_release_stage_plan(
        stage=1,
        candidate=candidate,
        transaction_authorization_digest=transaction,
        execution_authorization_digest=execution,
        recovery_capsule_digest=capsule,
        previous_record_digest=None,
        commit_authority_digest=None,
        state_observation=state,
        state_observation_raw=receipts.canonical_json_bytes(
            {
                "release_list_digest": receipts._sha256(release_raw),  # noqa: SLF001
                "ghcr_digest": receipts._sha256(ghcr_raw),  # noqa: SLF001
            }
        ),
        operation_requests={
            "create_github_release_draft": create_request,
            "publish_ghcr_digests": ghcr_request,
            "upload_github_release_assets": asset_request,
        },
    )
    if not receipts.write_once(output, receipts.canonical_json_bytes(plan)):
        raise receipts.ReleaseControlError("preparation plan output path must be empty")
    return 0


def _command_plan_commit(args: argparse.Namespace) -> int:
    output = Path(args.output)
    _require_new_outputs((output,))
    candidate, transaction, execution, capsule, sources = _stage_candidate_and_authority(
        manifest_path=Path(args.manifest),
        bundle_root=Path(args.bundle_root),
        transaction_authorization_path=Path(args.transaction_authorization),
        execution_authorization_path=(
            None if args.execution_authorization is None else Path(args.execution_authorization)
        ),
        recovery_capsule_verification_path=Path(args.recovery_capsule_verification),
    )
    preparation_raw = receipts._read_regular(  # noqa: SLF001
        Path(args.preparation_outcome),
        label="preparation outcome",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    preparation = _canonical_object(preparation_raw, label="preparation outcome")
    validate_release_stage_record(preparation)
    if preparation.get("schema") != "kestrel.release_preparation_outcome.v2":
        raise receipts.ReleaseControlError("commit plan preparation outcome mismatch")
    _require_completed_stage_binding(
        preparation,
        candidate=candidate,
        transaction_authorization_digest=transaction,
        execution_authorization_digest=execution,
        recovery_capsule_digest=capsule,
        label="commit plan preparation outcome",
    )
    commit_authority_raw = receipts._read_regular(  # noqa: SLF001
        Path(args.commit_authority_verification),
        label="commit authority verification",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    commit_authority = _canonical_object(
        commit_authority_raw, label="commit authority verification"
    )
    if (
        commit_authority.get("schema") != "kestrel.github_release_authority_verification.v1"
        or commit_authority.get("authority_schema") != receipts.GITHUB_AUTHORITY_SCHEMA
        or commit_authority.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("commit authority verification is invalid")
    verified_commit_authority = _verified_authority_from_record(
        commit_authority,
        verification_schema=("kestrel.github_release_authority_verification.v1"),
        authority_schema=receipts.GITHUB_AUTHORITY_SCHEMA,
        label="commit authority verification",
    )
    _require_current_authority(verified_commit_authority, label="commit GitHub authority")
    _require_github_authority_binding(
        verified_commit_authority,
        candidate=candidate,
        phase="commit",
        transaction_authorization_digest=transaction,
        execution_authorization_digest=execution,
        recovery_capsule_digest=capsule,
        commit_marker_digest=None,
    )
    commit_authority_digest = receipts._digest(  # noqa: SLF001
        commit_authority.get("receipt_digest"),
        label="commit authority receipt digest",
    )
    manifest = _canonical_object(sources["candidate-manifest"], label="commit candidate manifest")
    release_contract = _candidate_product_release_contract(
        manifest,
        transaction_authorization_digest=transaction,
        recovery_capsule_digest=capsule,
    )
    tag_raw = _read_observation_or_record(
        Path(args.tag_observation), label="commit tag observation"
    )
    tag_state = _classify_commit_tag_observation(
        receipts.parse_external_json_bytes(tag_raw, label="commit tag observation"),
        candidate=candidate,
        transaction_authorization_digest=transaction,
        recovery_capsule_digest=capsule,
    )
    release_raw = _read_observation_or_record(
        Path(args.release_list_observation),
        label="commit complete product Release listing",
    )
    release_state = _classify_product_release_listing(
        receipts.parse_external_json_bytes(
            release_raw, label="commit complete product Release listing"
        ),
        contract=release_contract,
    )
    if release_state["release"] == "missing" or release_state["assets"] != "existing_exact":
        raise receipts.ReleaseControlError(
            "commit requires one exact product Release with every candidate asset"
        )
    ghcr_raw = _read_observation_or_record(
        Path(args.ghcr_observation), label="commit GHCR observation"
    )
    expected_oci_digests = _expected_oci_object_digests(Path(args.bundle_root))
    ghcr_state = _classify_ghcr_digest_observation(
        receipts.parse_external_json_bytes(ghcr_raw, label="commit GHCR observation"),
        expected_digests=expected_oci_digests,
    )
    if ghcr_state != "existing_exact":
        raise receipts.ReleaseControlError("commit requires every candidate OCI object by digest")
    attestations_raw = _read_observation_or_record(
        Path(args.attestation_observations),
        label="commit promotion attestation observations",
    )
    attestation_state = _classify_promotion_attestation_observation(
        receipts.parse_external_json_bytes(
            attestations_raw, label="commit promotion attestation observations"
        ),
        manifest=manifest,
    )
    if tag_state == "missing" and (
        release_state["release"] == "immutable_exact"
        or any(state == "existing_exact" for state in attestation_state.values())
    ):
        raise receipts.ReleaseControlError(
            "committed Release or attestation exists without the exact tag marker"
        )
    if release_state["release"] == "draft_exact" and any(
        state == "existing_exact" for state in attestation_state.values()
    ):
        raise receipts.ReleaseControlError(
            "promotion attestation exists before immutable Release publication"
        )
    state: receipts.JSONObject = {
        "schema": "kestrel.release_stage_state.v1",
        "stage": 2,
        "operations": [
            {
                "operation": "attest_github_assets",
                "state": attestation_state["file"],
            },
            {
                "operation": "attest_oci_index_repository",
                "state": attestation_state["oci_index"],
            },
            {"operation": "create_tag", "state": tag_state},
            {
                "operation": "publish_github_release_draft",
                "state": (
                    "missing" if release_state["release"] == "draft_exact" else "existing_exact"
                ),
            },
        ],
        "complete": True,
    }
    subject_requests: dict[str, receipts.JSONObject] = {
        "file": {"predicate_type": _RELEASE_PROMOTION_PREDICATE_TYPE, "subjects": []},
        "oci_index": {
            "predicate_type": _RELEASE_PROMOTION_PREDICATE_TYPE,
            "subjects": [],
        },
    }
    for raw_subject in receipts._array(  # noqa: SLF001
        manifest.get("attestation_subjects"), label="commit attestation subjects"
    ):
        subject = receipts._object(raw_subject, label="commit attestation subject")  # noqa: SLF001
        kind = cast(str, subject["kind"])
        cast(list[receipts.JSONValue], subject_requests[kind]["subjects"]).append(subject)
    create_tag_request: receipts.JSONObject = {
        "tag": candidate["tag"],
        "message": build_annotated_tag_message(
            candidate=candidate,
            transaction_authorization_digest=transaction,
            recovery_capsule_digest=capsule,
        ),
        "object": candidate["source_sha"],
        "type": "commit",
        "ref": f"refs/tags/{candidate['tag']}",
    }
    publish_request: receipts.JSONObject = {
        "release_id": release_state["release_id"],
        "tag_name": candidate["tag"],
        "patch": {"draft": False, "make_latest": False},
    }
    plan = build_release_stage_plan(
        stage=2,
        candidate=candidate,
        transaction_authorization_digest=transaction,
        execution_authorization_digest=execution,
        recovery_capsule_digest=capsule,
        previous_record_digest=receipts._sha256(preparation_raw),  # noqa: SLF001
        commit_authority_digest=commit_authority_digest,
        state_observation=state,
        state_observation_raw=receipts.canonical_json_bytes(
            {
                "tag_digest": receipts._sha256(tag_raw),  # noqa: SLF001
                "release_list_digest": receipts._sha256(release_raw),  # noqa: SLF001
                "ghcr_digest": receipts._sha256(ghcr_raw),  # noqa: SLF001
                "attestations_digest": receipts._sha256(attestations_raw),  # noqa: SLF001
            }
        ),
        operation_requests={
            "attest_github_assets": subject_requests["file"],
            "attest_oci_index_repository": subject_requests["oci_index"],
            "create_tag": create_tag_request,
            "publish_github_release_draft": publish_request,
        },
    )
    if not receipts.write_once(output, receipts.canonical_json_bytes(plan)):
        raise receipts.ReleaseControlError("commit plan output path must be empty")
    return 0


def _validated_stage_plan(value: receipts.JSONObject, *, stage: int) -> receipts.JSONObject:
    receipts._require_exact_fields(  # noqa: SLF001
        value,
        frozenset(
            {
                "schema",
                "stage",
                "candidate",
                "transaction_authorization_digest",
                "execution_authorization_digest",
                "recovery_capsule_digest",
                "previous_record_digest",
                "commit_authority_digest",
                "operations",
                "state_observation_digest",
                "provenance",
                "validation_status",
            }
        ),
        label="release stage plan",
    )
    if (
        value.get("schema") != f"kestrel.release_stage_{stage}_plan.v1"
        or value.get("stage") != stage
        or value.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("release stage plan identity mismatch")
    expected_operations = next(
        operations
        for policy_stage, operations in _RELEASE_STAGE_POLICY.values()
        if policy_stage == stage
    )
    operations = [
        receipts._object(item, label="release stage planned operation")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            value.get("operations"), label="release stage planned operations"
        )
    ]
    for operation in operations:
        receipts._require_exact_fields(  # noqa: SLF001
            operation,
            frozenset({"operation", "action", "request_digest"}),
            label="release stage planned operation",
        )
        if operation.get("action") not in {"create", "no_op"}:
            raise receipts.ReleaseControlError("release stage plan action is invalid")
        receipts._digest(  # noqa: SLF001
            operation.get("request_digest"),
            label="release stage plan request digest",
        )
    if tuple(item.get("operation") for item in operations) != expected_operations:
        raise receipts.ReleaseControlError("release stage plan operations mismatch")
    return value


def _stage_execution_observation(path: Path, *, stage: int) -> tuple[bytes, receipts.JSONObject]:
    raw = _read_observation_or_record(path, label="release stage execution observation")
    value = _canonical_object(raw, label="release stage execution observation")
    fields = {
        "schema",
        "stage",
        "observations",
        "operation_outcomes",
        "completed",
        "uncertain",
        "pending",
    }
    if stage == 4:
        fields.add("project")
    receipts._require_exact_fields(  # noqa: SLF001
        value,
        frozenset(fields),
        label="release stage execution observation",
    )
    if value.get("schema") != "kestrel.release_stage_execution.v1" or value.get("stage") != stage:
        raise receipts.ReleaseControlError("release stage execution observation mismatch")
    return raw, value


def _command_record_mutation_stage(args: argparse.Namespace, *, stage: int) -> int:
    output = Path(args.output)
    _require_new_outputs((output,))
    plan_raw = receipts._read_regular(  # noqa: SLF001
        Path(args.plan),
        label="release stage plan",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    plan = _validated_stage_plan(
        _canonical_object(plan_raw, label="release stage plan"), stage=stage
    )
    pre_raw = _read_observation_or_record(
        Path(args.pre_observations), label="release pre-observations"
    )
    pre = _canonical_object(pre_raw, label="release pre-observations")
    receipts._require_exact_fields(  # noqa: SLF001
        pre,
        frozenset({"schema", "stage", "observations"}),
        label="release pre-observations",
    )
    if pre.get("schema") != "kestrel.release_stage_observations.v1" or pre.get("stage") != stage:
        raise receipts.ReleaseControlError("release pre-observations mismatch")
    post_raw, post = _stage_execution_observation(Path(args.post_observations), stage=stage)
    planned_operations = [
        receipts._object(item, label="release stage planned operation")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            plan.get("operations"), label="release stage planned operations"
        )
    ]
    operation_outcomes = [
        receipts._object(item, label="release stage operation outcome")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            post.get("operation_outcomes"),
            label="release stage operation outcomes",
        )
    ]
    if len(operation_outcomes) != len(planned_operations):
        raise receipts.ReleaseControlError("release stage plan/outcome cardinality mismatch")
    for planned, outcome in zip(planned_operations, operation_outcomes, strict=True):
        if (
            outcome.get("operation") != planned.get("operation")
            or outcome.get("request_digest") != planned.get("request_digest")
            or (planned.get("action") == "no_op" and outcome.get("outcome") != "existing_exact")
            or (outcome.get("outcome") == "created" and planned.get("action") != "create")
        ):
            raise receipts.ReleaseControlError("release stage execution does not match its plan")
    all_success = all(
        item.get("outcome") in {"created", "existing_exact"} for item in operation_outcomes
    )
    has_unknown = any(item.get("outcome") == "unknown" for item in operation_outcomes)
    has_not_attempted = any(item.get("outcome") == "not_attempted" for item in operation_outcomes)
    if (
        post.get("completed") is not all_success
        or post.get("uncertain") is not has_unknown
        or (has_not_attempted and post.get("pending") is not True)
    ):
        raise receipts.ReleaseControlError(
            "release stage execution summary is not derived from its outcomes"
        )
    schema = (
        "kestrel.release_preparation_outcome.v2"
        if stage == 1
        else "kestrel.release_commit_outcome.v2"
    )
    record = build_release_stage_record(
        schema=schema,
        candidate=receipts._object(plan["candidate"], label="stage plan candidate"),  # noqa: SLF001
        transaction_authorization_digest=cast(str, plan["transaction_authorization_digest"]),
        execution_authorization_digest=cast(str | None, plan["execution_authorization_digest"]),
        recovery_capsule_digest=cast(str, plan["recovery_capsule_digest"]),
        previous_record_digest=cast(str | None, plan["previous_record_digest"]),
        observations_before=receipts._array(  # noqa: SLF001
            pre.get("observations"), label="release pre-observations"
        ),
        observations_after=receipts._array(  # noqa: SLF001
            post.get("observations"), label="release post-observations"
        ),
        attempted_operations=operation_outcomes,
        fresh_observations=None,
        verification_results=None,
        commit_authority_digest=cast(str | None, plan["commit_authority_digest"]),
        completed=cast(bool, post["completed"]),
        uncertain=cast(bool, post["uncertain"]),
        pending=cast(bool, post["pending"]),
        source_records={
            "plan": plan_raw,
            "pre-observations": pre_raw,
            "post-observations": post_raw,
        },
    )
    if not receipts.write_once(output, receipts.canonical_json_bytes(record)):
        raise receipts.ReleaseControlError("stage record output path must be empty")
    return 0


def _command_record_preparation(args: argparse.Namespace) -> int:
    return _command_record_mutation_stage(args, stage=1)


def _command_record_commit(args: argparse.Namespace) -> int:
    return _command_record_mutation_stage(args, stage=2)


_RELEASE_PROMOTION_PREDICATE_TYPE = "https://kestrel.dev/attestations/release-promotion/v1"


def _require_nonempty_gh_json(raw: bytes, *, label: str) -> receipts.JSONValue:
    if not raw:
        raise receipts.ReleaseControlError(f"{label} produced no verification output")
    value = receipts.parse_external_json_bytes(raw, label=label)
    if value in ({}, []):
        raise receipts.ReleaseControlError(f"{label} verification output is empty")
    return value


def _verify_attestation_output(raw: bytes, *, expected_name: str, expected_digest: str) -> None:
    value = _require_nonempty_gh_json(raw, label="GitHub attestation verification")
    entries = receipts._array(value, label="GitHub attestation verification")  # noqa: SLF001
    if len(entries) != 1:
        raise receipts.ReleaseControlError(
            "GitHub custom attestation verification is not a singleton"
        )
    entry = receipts._object(entries[0], label="GitHub attestation entry")  # noqa: SLF001
    result = receipts._object(  # noqa: SLF001
        entry.get("verificationResult"), label="GitHub attestation result"
    )
    statement = receipts._object(  # noqa: SLF001
        result.get("statement"), label="GitHub attestation statement"
    )
    if statement.get("predicateType") != _RELEASE_PROMOTION_PREDICATE_TYPE:
        raise receipts.ReleaseControlError("GitHub attestation predicate type mismatch")
    subjects = receipts._array(  # noqa: SLF001
        statement.get("subject"), label="GitHub attestation subjects"
    )
    if len(subjects) != 1:
        raise receipts.ReleaseControlError("GitHub attestation subject is not a singleton")
    subject = receipts._object(subjects[0], label="GitHub attestation subject")  # noqa: SLF001
    digest = receipts._object(  # noqa: SLF001
        subject.get("digest"), label="GitHub attestation subject digest"
    )
    expected_hex = expected_digest.removeprefix("sha256:")
    if set(digest) != {"sha256"} or (subject.get("name"), digest.get("sha256")) != (
        expected_name,
        expected_hex,
    ):
        raise receipts.ReleaseControlError("GitHub attestation subject identity mismatch")


def _run_github_surface_verifications(
    *,
    candidate: Mapping[str, object],
    bundle_root: Path,
    pinned_gh: Path,
    source_ref: str,
) -> tuple[list[receipts.JSONObject], dict[str, bytes]]:
    """Run the complete read-only GitHub verification set with one pinned CLI."""

    source = receipts._object(candidate.get("source"), label="verification source")  # noqa: SLF001
    repository = receipts._validate_string(  # noqa: SLF001
        source.get("repository"), label="verification repository"
    )
    if repository != "John-MiracleWorker/Kestrel":
        raise receipts.ReleaseControlError("verification repository mismatch")
    source_sha = receipts._validate_string(  # noqa: SLF001
        source.get("commit_sha"), label="verification source SHA"
    )
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise receipts.ReleaseControlError("verification source SHA is invalid")
    tag = receipts._validate_string(  # noqa: SLF001
        candidate.get("tag"), label="verification tag"
    )
    if source_ref not in {"refs/heads/main", f"refs/tags/{tag}"}:
        raise receipts.ReleaseControlError("verification source ref mismatch")

    release_artifacts: list[tuple[str, Path, str, int]] = []
    artifact_digests: dict[str, str] = {}
    for raw_item in receipts._array(  # noqa: SLF001
        candidate.get("artifacts"), label="verification artifacts"
    ):
        item = receipts._object(raw_item, label="verification artifact")  # noqa: SLF001
        name = receipts._validate_string(  # noqa: SLF001
            item.get("path"), label="verification artifact path"
        )
        digest = receipts._digest(  # noqa: SLF001
            item.get("sha256"), label="verification artifact digest"
        )
        artifact_digests[name] = digest
        if not name.startswith("release/"):
            continue
        size = receipts._safe_integer(  # noqa: SLF001
            item.get("size_bytes"), label="verification artifact size", positive=True
        )
        release_artifacts.append((name, bundle_root / name, digest, size))
    if not release_artifacts:
        raise receipts.ReleaseControlError("candidate has no GitHub Release assets")

    attestation_targets: list[tuple[str, str, str]] = []
    for raw_item in receipts._array(  # noqa: SLF001
        candidate.get("attestation_subjects"), label="verification attestation subjects"
    ):
        item = receipts._object(  # noqa: SLF001
            raw_item, label="verification attestation subject"
        )
        kind = receipts._validate_string(  # noqa: SLF001
            item.get("kind"), label="verification attestation kind"
        )
        name = receipts._validate_string(  # noqa: SLF001
            item.get("name"), label="verification attestation name"
        )
        digest = receipts._digest(  # noqa: SLF001
            item.get("digest"), label="verification attestation digest"
        )
        if kind == "file":
            if artifact_digests.get(name) != digest:
                raise receipts.ReleaseControlError(
                    "file attestation subject is outside the candidate artifacts"
                )
            target = str((bundle_root / name).resolve())
        elif kind == "oci_index":
            if name != candidates.OCI_REPOSITORY:
                raise receipts.ReleaseControlError(
                    "attestation subject is not the candidate OCI repository"
                )
            target = f"oci://{name}@{digest}"
        else:
            raise receipts.ReleaseControlError("verification attestation subject kind is invalid")
        attestation_targets.append((name, target, digest))

    checked_release_artifacts: list[tuple[str, Path, str]] = []
    for name, path, digest, size in release_artifacts:
        raw = receipts._read_regular(  # noqa: SLF001
            path,
            label=f"GitHub Release asset {name}",
            max_bytes=2_147_483_648,
        )
        if len(raw) != size or receipts._sha256(raw) != digest:  # noqa: SLF001
            raise receipts.ReleaseControlError("GitHub Release asset differs from the candidate")
        checked_release_artifacts.append((name, path.resolve(), digest))

    receipts._verify_pinned_gh(pinned_gh)  # noqa: SLF001
    evidence: dict[str, bytes] = {}
    results: list[receipts.JSONObject] = []

    def record(*, key: str, check: str, subject_digest: str, raw: bytes) -> None:
        evidence[key] = raw
        results.append(
            {
                "check": check,
                "subject_digest": subject_digest,
                "result": "passed",
                "observation_digest": receipts._sha256(raw),  # noqa: SLF001
            }
        )

    release_raw = receipts._run_pinned_gh_verification(  # noqa: SLF001
        pinned_gh,
        ["release", "verify", tag, "--repo", repository, "--format", "json"],
    )
    _require_nonempty_gh_json(release_raw, label="GitHub Release verification")
    record(
        key="github-release-verification",
        check="github-release",
        subject_digest=candidates.candidate_manifest_digest(candidate),
        raw=release_raw,
    )
    for index, (_name, path, digest) in enumerate(checked_release_artifacts, start=1):
        raw = receipts._run_pinned_gh_verification(  # noqa: SLF001
            pinned_gh,
            [
                "release",
                "verify-asset",
                tag,
                str(path),
                "--repo",
                repository,
                "--format",
                "json",
            ],
        )
        _require_nonempty_gh_json(raw, label="GitHub Release asset verification")
        record(
            key=f"github-release-asset-{index:03d}",
            check=f"github-release-asset-{index:03d}",
            subject_digest=digest,
            raw=raw,
        )

    signer_workflow = f"{repository}/.github/workflows/release-transaction.yml"
    common_arguments = [
        "--repo",
        repository,
        "--signer-workflow",
        signer_workflow,
        "--signer-digest",
        source_sha,
        "--source-digest",
        source_sha,
        "--source-ref",
        source_ref,
        "--predicate-type",
        _RELEASE_PROMOTION_PREDICATE_TYPE,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    for index, (name, target, digest) in enumerate(attestation_targets, start=1):
        raw = receipts._run_pinned_gh_verification(  # noqa: SLF001
            pinned_gh,
            ["attestation", "verify", target, *common_arguments],
        )
        _verify_attestation_output(raw, expected_name=name, expected_digest=digest)
        record(
            key=f"repository-attestation-{index:03d}",
            check=f"repository-attestation-{index:03d}",
            subject_digest=digest,
            raw=raw,
        )
    results.sort(key=lambda item: cast(str, item["check"]))
    return results, evidence


def _command_verify_github_ghcr(args: argparse.Namespace) -> int:
    output = Path(args.output)
    _require_new_outputs((output,))
    candidate, transaction, execution, capsule, sources = _stage_candidate_and_authority(
        manifest_path=Path(args.manifest),
        bundle_root=Path(args.bundle_root),
        transaction_authorization_path=Path(args.transaction_authorization),
        execution_authorization_path=(
            None if args.execution_authorization is None else Path(args.execution_authorization)
        ),
        recovery_capsule_verification_path=Path(args.recovery_capsule_verification),
    )
    prior_records = []
    for name in ("preparation_outcome", "commit_outcome"):
        raw = receipts._read_regular(  # noqa: SLF001
            Path(getattr(args, name)),
            label=name.replace("_", " "),
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        record = _canonical_object(raw, label=name.replace("_", " "))
        validate_release_stage_record(record)
        _require_completed_stage_binding(
            record,
            candidate=candidate,
            transaction_authorization_digest=transaction,
            execution_authorization_digest=execution,
            recovery_capsule_digest=capsule,
            label="GitHub/GHCR prior stage",
        )
        sources[name.replace("_", "-")] = raw
        prior_records.append((raw, record))
    if (
        prior_records[0][1].get("schema") != "kestrel.release_preparation_outcome.v2"
        or prior_records[1][1].get("schema") != "kestrel.release_commit_outcome.v2"
        or prior_records[1][1].get("previous_record_digest")
        != receipts._sha256(prior_records[0][0])  # noqa: SLF001
    ):
        raise receipts.ReleaseControlError("GitHub/GHCR verification stage chain mismatch")
    fresh_raw = _external_observation_body(
        Path(args.fresh_observations), label="GitHub/GHCR fresh observations"
    )
    fresh = _external_object(fresh_raw, label="GitHub/GHCR fresh observations")
    receipts._require_exact_fields(  # noqa: SLF001
        fresh,
        frozenset(
            {
                "schema",
                "observations",
                "verification_results",
                "completed",
                "release_list",
                "ghcr",
                "tag",
                "attestations",
            }
        ),
        label="GitHub/GHCR fresh observations",
    )
    if fresh.get("schema") != "github.release_surface_observation.v1":
        raise receipts.ReleaseControlError("GitHub/GHCR fresh observation schema mismatch")
    pinned_gh = Path(args.pinned_gh)
    sources["fresh-observations"] = fresh_raw
    sources["pinned-gh"] = receipts._read_regular(  # noqa: SLF001
        pinned_gh,
        label="pinned GitHub CLI",
        max_bytes=256 * 1024 * 1024,
    )
    candidate_manifest = _canonical_object(
        sources["candidate-manifest"], label="GitHub/GHCR candidate manifest"
    )
    release_contract = _candidate_product_release_contract(
        candidate_manifest,
        transaction_authorization_digest=transaction,
        recovery_capsule_digest=capsule,
    )
    release_state = _classify_product_release_listing(
        fresh.get("release_list"), contract=release_contract
    )
    expected_oci_digests = _expected_oci_object_digests(Path(args.bundle_root))
    ghcr_state = _classify_ghcr_digest_observation(
        fresh.get("ghcr"), expected_digests=expected_oci_digests
    )
    tag_state = _classify_commit_tag_observation(
        fresh.get("tag"),
        candidate=candidate,
        transaction_authorization_digest=transaction,
        recovery_capsule_digest=capsule,
    )
    attestation_state = _classify_promotion_attestation_observation(
        fresh.get("attestations"), manifest=candidate_manifest
    )
    remote_complete = (
        release_state["release"] == "immutable_exact"
        and release_state["assets"] == "existing_exact"
        and ghcr_state == "existing_exact"
        and tag_state == "existing_exact"
        and all(state == "existing_exact" for state in attestation_state.values())
    )
    if fresh.get("completed") is not remote_complete or not remote_complete:
        raise receipts.ReleaseControlError("GitHub/GHCR fresh observation is not derived-complete")
    verification_results, command_evidence = _run_github_surface_verifications(
        candidate=candidate_manifest,
        bundle_root=Path(args.bundle_root),
        pinned_gh=pinned_gh,
        source_ref=("refs/heads/main" if execution is None else f"refs/tags/{candidate['tag']}"),
    )
    supplied_results = receipts._array(  # noqa: SLF001
        fresh.get("verification_results"),
        label="GitHub/GHCR supplied verification results",
    )
    if supplied_results and supplied_results != verification_results:
        raise receipts.ReleaseControlError(
            "GitHub/GHCR supplied results disagree with controller verification"
        )
    sources.update(command_evidence)
    record = build_release_stage_record(
        schema="kestrel.release_github_ghcr_verification.v2",
        candidate=candidate,
        transaction_authorization_digest=transaction,
        execution_authorization_digest=execution,
        recovery_capsule_digest=capsule,
        previous_record_digest=receipts._sha256(prior_records[1][0]),  # noqa: SLF001
        observations_before=None,
        observations_after=None,
        attempted_operations=None,
        fresh_observations=receipts._array(  # noqa: SLF001
            fresh.get("observations"), label="GitHub/GHCR observations"
        ),
        verification_results=verification_results,
        commit_authority_digest=None,
        completed=remote_complete,
        uncertain=None,
        pending=None,
        source_records=sources,
    )
    if not receipts.write_once(output, receipts.canonical_json_bytes(record)):
        raise receipts.ReleaseControlError("verification output path must be empty")
    return 0


def _command_record_pypi(args: argparse.Namespace) -> int:
    output = Path(args.output)
    _require_new_outputs((output,))
    candidate, transaction, execution, capsule, sources = _stage_candidate_and_authority(
        manifest_path=Path(args.manifest),
        bundle_root=None,
        transaction_authorization_path=Path(args.transaction_authorization),
        execution_authorization_path=(
            None if args.execution_authorization is None else Path(args.execution_authorization)
        ),
        recovery_capsule_verification_path=Path(args.recovery_capsule_verification),
    )
    prior: list[tuple[bytes, receipts.JSONObject]] = []
    for name in ("commit_outcome", "github_ghcr_verification"):
        raw = receipts._read_regular(  # noqa: SLF001
            Path(getattr(args, name)),
            label=name.replace("_", " "),
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        value = _canonical_object(raw, label=name.replace("_", " "))
        validate_release_stage_record(value)
        _require_completed_stage_binding(
            value,
            candidate=candidate,
            transaction_authorization_digest=transaction,
            execution_authorization_digest=execution,
            recovery_capsule_digest=capsule,
            label="PyPI prior stage",
        )
        prior.append((raw, value))
        sources[name.replace("_", "-")] = raw
    if (
        prior[0][1].get("schema") != "kestrel.release_commit_outcome.v2"
        or prior[1][1].get("schema") != "kestrel.release_github_ghcr_verification.v2"
        or prior[1][1].get("previous_record_digest") != receipts._sha256(prior[0][0])  # noqa: SLF001
    ):
        raise receipts.ReleaseControlError("PyPI release stage chain mismatch")
    authority_raw = receipts._read_regular(  # noqa: SLF001
        Path(args.pypi_authority_verification),
        label="PyPI authority verification",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    authority = _canonical_object(authority_raw, label="PyPI authority verification")
    if (
        authority.get("schema") != "kestrel.pypi_upload_authority_verification.v1"
        or authority.get("authority_schema") != receipts.PYPI_AUTHORITY_SCHEMA
        or authority.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("PyPI authority verification is invalid")
    verified_authority = _verified_authority_from_record(
        authority,
        verification_schema="kestrel.pypi_upload_authority_verification.v1",
        authority_schema=receipts.PYPI_AUTHORITY_SCHEMA,
        label="PyPI authority verification",
    )
    _require_current_authority(verified_authority, label="PyPI authority")
    _require_pypi_authority_binding(
        verified_authority,
        candidate=candidate,
        transaction_authorization_digest=transaction,
        execution_authorization_digest=execution,
        recovery_capsule_digest=capsule,
        github_ghcr_verification_digest=receipts._sha256(prior[1][0]),  # noqa: SLF001
    )
    sources["pypi-authority-verification"] = authority_raw
    pre_raw = _read_observation_or_record(Path(args.pre_observation), label="PyPI pre-observation")
    pre = _canonical_object(pre_raw, label="PyPI pre-observation")
    receipts._require_exact_fields(  # noqa: SLF001
        pre,
        frozenset({"schema", "stage", "observations", "project"}),
        label="PyPI pre-observation",
    )
    if pre.get("schema") != "kestrel.release_stage_observations.v1" or pre.get("stage") != 4:
        raise receipts.ReleaseControlError("PyPI pre-observation mismatch")
    post_raw, post = _stage_execution_observation(Path(args.post_observation), stage=4)
    manifest = _canonical_object(sources["candidate-manifest"], label="PyPI candidate manifest")
    expected_files = _candidate_pypi_files(manifest)
    version = receipts._validate_string(  # noqa: SLF001
        manifest.get("version"), label="PyPI candidate version"
    )
    pre_state = _classify_pypi_project_observation(
        pre.get("project"), version=version, expected_files=expected_files
    )
    post_state = _classify_pypi_project_observation(
        post.get("project"), version=version, expected_files=expected_files
    )
    pre_present = [cast(str, item) for item in pre_state["present"]]  # type: ignore[union-attr]
    pre_missing = [cast(str, item) for item in pre_state["missing"]]  # type: ignore[union-attr]
    post_present = [cast(str, item) for item in post_state["present"]]  # type: ignore[union-attr]
    post_missing = [cast(str, item) for item in post_state["missing"]]  # type: ignore[union-attr]
    if not set(pre_present) <= set(post_present):
        raise receipts.ReleaseControlError("PyPI candidate file disappeared during publication")
    created_filenames = sorted(set(post_present) - set(pre_present))
    pre_serial = cast(int, pre_state["last_serial"])
    post_serial = cast(int, post_state["last_serial"])
    if post_serial < pre_serial or (
        (bool(created_filenames) and post_serial == pre_serial)
        or (not created_filenames and post_serial != pre_serial)
    ):
        raise receipts.ReleaseControlError(
            "PyPI project serial changed without the observed candidate file transition"
        )
    sources["pypi-pre-observation"] = pre_raw
    sources["pypi-post-observation"] = post_raw
    integrity_raw = _read_observation_or_record(
        Path(args.integrity_observations), label="PyPI Integrity observations"
    )
    provenance_raw = _read_observation_or_record(
        Path(args.provenance_verifications),
        label="PyPI provenance verifications",
    )
    _validate_pypi_provenance_evidence(
        integrity_observations=receipts.parse_external_json_bytes(
            integrity_raw, label="PyPI Integrity observations"
        ),
        provenance_verifications=receipts.parse_external_json_bytes(
            provenance_raw, label="PyPI provenance verifications"
        ),
        expected_files=expected_files,
        persisted_filenames=post_present,
        distribution_root=Path(args.manifest).resolve(strict=True).parent,
    )
    sources["integrity-observations"] = integrity_raw
    sources["provenance-verifications"] = provenance_raw
    operation_outcomes = [
        receipts._object(item, label="PyPI publication outcome")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            post.get("operation_outcomes"), label="PyPI operation outcomes"
        )
    ]
    if len(operation_outcomes) != 1:
        raise receipts.ReleaseControlError(
            "PyPI publication must have exactly one operation outcome"
        )
    operation = operation_outcomes[0]
    request_files: list[receipts.JSONObject] = []
    for filename in pre_missing:
        expected = expected_files[filename]
        request_files.append(
            {
                "filename": filename,
                "path": expected["path"],
                "sha256": expected["sha256"],
                "size_bytes": expected["size_bytes"],
            }
        )
    expected_request_digest = receipts._sha256(  # noqa: SLF001
        receipts.canonical_json_bytes(
            {
                "candidate": candidate,
                "transaction_authorization_digest": transaction,
                "execution_authorization_digest": execution,
                "recovery_capsule_digest": capsule,
                "operation": "publish_pypi_missing_files",
                "files": request_files,
            }
        )
    )
    outcome = operation.get("outcome")
    if (
        operation.get("operation") != "publish_pypi_missing_files"
        or operation.get("request_digest") != expected_request_digest
        or (not pre_missing and outcome != "existing_exact")
        or (pre_missing and not post_missing and outcome != "created")
    ):
        raise receipts.ReleaseControlError(
            "PyPI publication outcome does not match the missing-only request"
        )
    completed = not post_missing and outcome in {"created", "existing_exact"}
    uncertain = outcome == "unknown"
    pending = bool(post_missing) and not uncertain
    if (
        post.get("completed") is not completed
        or post.get("uncertain") is not uncertain
        or post.get("pending") is not pending
    ):
        raise receipts.ReleaseControlError(
            "PyPI publication summary is not derived from public post-state"
        )
    approval_raw = _read_observation_or_record(
        Path(args.approval_history_observation),
        label="PyPI cumulative approval history",
    )
    approval_history = _canonical_object(approval_raw, label="PyPI cumulative approval history")
    _require_cumulative_owner_approvals(
        approval_history,
        expected_environments=(
            "release",
            "release-prepare",
            "release-commit",
            "pypi",
        ),
    )
    sources["approval-history-observation"] = approval_raw
    record = build_release_stage_record(
        schema="kestrel.release_pypi_outcome.v2",
        candidate=candidate,
        transaction_authorization_digest=transaction,
        execution_authorization_digest=execution,
        recovery_capsule_digest=capsule,
        previous_record_digest=receipts._sha256(prior[1][0]),  # noqa: SLF001
        observations_before=receipts._array(  # noqa: SLF001
            pre.get("observations"), label="PyPI pre-observations"
        ),
        observations_after=receipts._array(  # noqa: SLF001
            post.get("observations"), label="PyPI post-observations"
        ),
        attempted_operations=receipts._array(  # noqa: SLF001
            post.get("operation_outcomes"), label="PyPI operation outcomes"
        ),
        fresh_observations=None,
        verification_results=None,
        commit_authority_digest=None,
        completed=completed,
        uncertain=uncertain,
        pending=pending,
        source_records=sources,
    )
    if not receipts.write_once(output, receipts.canonical_json_bytes(record)):
        raise receipts.ReleaseControlError("PyPI record output path must be empty")
    return 0


def _command_reconcile_release(args: argparse.Namespace) -> int:
    output = Path(args.output)
    _require_new_outputs((output,))
    source_records: dict[str, bytes] = {}

    def read_record(path_text: str, name: str) -> tuple[bytes, receipts.JSONObject]:
        raw = receipts._read_regular(  # noqa: SLF001
            Path(path_text), label=name, max_bytes=receipts.MAX_SOURCE_BODY_BYTES
        )
        source_records[name] = raw
        value = _canonical_object(raw, label=name)
        if value.get("schema") == receipts.SOURCE_OBSERVATION_SCHEMA:
            raise receipts.ReleaseControlError(f"{name} must be a canonical Kestrel record")
        return raw, value

    run_raw = receipts._read_regular(  # noqa: SLF001
        Path(args.run_observation),
        label="run-observation",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    run_body = _contract_source_body(
        run_raw,
        label="run-observation",
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="promotion-run-observation",
    )
    run_observation = receipts._object(  # noqa: SLF001
        receipts.parse_external_json_bytes(run_body, label="run-observation"),
        label="run-observation",
    )
    source_records["run-observation"] = run_raw
    identity_raw, identity = read_record(args.dispatch_identity, "dispatch-identity")
    receipts._validate_schema(  # noqa: SLF001
        receipts.DISPATCH_IDENTITY_SCHEMA, identity, label="reconciliation dispatch identity"
    )
    run = _authorization_promotion_run(
        run_observation=run_observation,
        run_observation_raw=run_raw,
        identity=identity,
        identity_raw=identity_raw,
    )
    intent_raw, intent = read_record(args.dispatch_intent, "dispatch-intent")
    receipts._validate_dispatch_intent(intent)  # noqa: SLF001
    dispatch_raw, dispatch_reconciliation = read_record(
        args.dispatch_reconciliation, "dispatch-reconciliation"
    )
    receipts._validate_schema(  # noqa: SLF001
        receipts.DISPATCH_RECONCILIATION_SCHEMA,
        dispatch_reconciliation,
        label="dispatch reconciliation",
    )
    _require_release_dispatch_binding(
        run=run,
        identity=identity,
        intent=intent,
        dispatch_reconciliation=dispatch_reconciliation,
    )
    fresh_raw, fresh = read_record(args.fresh_observations, "fresh-observations")
    receipts._require_exact_fields(  # noqa: SLF001
        fresh,
        frozenset(
            {
                "schema",
                "sources",
            }
        ),
        label="final release observations",
    )
    if fresh.get("schema") != "kestrel.release_final_observations.v2":
        raise receipts.ReleaseControlError("final release observation schema mismatch")
    requested_failure = None if args.failure_code == "none" else args.failure_code
    candidate: receipts.JSONObject | None = None
    manifest_value: receipts.JSONObject | None = None
    candidate_repository_id: int | None = None
    transaction_digest: str | None = None
    transaction_raw: bytes | None = None
    execution_digest: str | None = None
    capsule_digest: str | None = None
    if args.manifest is not None:
        manifest_raw = receipts._read_regular(  # noqa: SLF001
            Path(args.manifest),
            label="reconciliation candidate manifest",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        candidate, candidate_repository_id = receipts._candidate_from_manifest(  # noqa: SLF001
            manifest_raw
        )
        manifest_value = _canonical_object(manifest_raw, label="reconciliation candidate manifest")
        source_records["manifest"] = manifest_raw
    if args.transaction_authorization is not None:
        transaction_raw = receipts._read_regular(  # noqa: SLF001
            Path(args.transaction_authorization),
            label="reconciliation transaction authorization",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        transaction = _canonical_object(
            transaction_raw, label="reconciliation transaction authorization"
        )
        validate_server_authorization(transaction, expected_original_transaction_digest=None)
        transaction_digest = receipts._sha256(transaction_raw)  # noqa: SLF001
        source_records["transaction-authorization"] = transaction_raw
        if candidate is not None:
            transaction_run = receipts._object(  # noqa: SLF001
                transaction.get("promotion_run"),
                label="reconciliation transaction promotion run",
            )
            if (
                transaction.get("candidate") != candidate
                or transaction_run.get("repository_id") != candidate_repository_id
            ):
                raise receipts.ReleaseControlError("reconciliation transaction candidate mismatch")
    if args.execution_authorization is not None:
        if transaction_digest is None:
            raise receipts.ReleaseControlError(
                "reconciliation execution authorization lacks transaction"
            )
        execution_raw = receipts._read_regular(  # noqa: SLF001
            Path(args.execution_authorization),
            label="reconciliation execution authorization",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        execution = _canonical_object(execution_raw, label="reconciliation execution authorization")
        validate_server_authorization(
            execution,
            expected_original_transaction_digest=transaction_digest,
        )
        execution_digest = receipts._sha256(execution_raw)  # noqa: SLF001
        source_records["execution-authorization"] = execution_raw
        if candidate is not None:
            execution_run = receipts._object(  # noqa: SLF001
                execution.get("promotion_run"),
                label="reconciliation execution promotion run",
            )
            if (
                execution.get("candidate") != candidate
                or execution_run.get("repository_id") != candidate_repository_id
            ):
                raise receipts.ReleaseControlError("reconciliation execution candidate mismatch")
    if args.recovery_capsule_verification is not None:
        if candidate is None or transaction_raw is None:
            raise receipts.ReleaseControlError(
                "reconciliation capsule verification lacks candidate transaction"
            )
        capsule_raw, capsule = read_record(
            args.recovery_capsule_verification, "recovery-capsule-verification"
        )
        capsule_digest = _authorization_capsule_digest(
            verification=capsule,
            candidate_manifest_digest=candidate.get("candidate_manifest_digest"),
            transaction_authorization=transaction_raw,
        )
        source_records["recovery-capsule-verification"] = capsule_raw
    if (
        any(
            value is not None
            for value in (candidate, transaction_digest, execution_digest, capsule_digest)
        )
        and candidate is None
    ):
        raise receipts.ReleaseControlError(
            "reconciliation authority inputs lack candidate manifest"
        )
    stage_chain: list[receipts.JSONObject] = []
    stage_statuses: list[tuple[bool, bool, bool]] = []
    if args.stage_records is not None:
        if candidate is None or transaction_digest is None or capsule_digest is None:
            raise receipts.ReleaseControlError(
                "reconciliation stage records lack complete authority inputs"
            )
        stage_chain = _reconciliation_stage_chain(
            stage_root=Path(args.stage_records),
            candidate=candidate,
            transaction_authorization_digest=transaction_digest,
            execution_authorization_digest=execution_digest,
            recovery_capsule_digest=capsule_digest,
            require_complete=False,
            source_records=source_records,
            stage_statuses=stage_statuses,
        )
    full_chain = (
        tuple((cast(str, item["filename"]), cast(str, item["schema"])) for item in stage_chain)
        == _RELEASE_STAGE_CHAIN
    )
    expected_base_sources = {
        "default-branch-workflow-contents",
        "ingress-ruleset-detail-observation",
        "workflow-observation",
    }
    can_classify_products = all(
        value is not None
        for value in (candidate, manifest_value, transaction_digest, capsule_digest)
    )
    expected_product_sources = {
        "final-attestation-observation",
        "final-ghcr-observation",
        "final-pypi-integrity-observations",
        "final-pypi-project-observation",
        "final-pypi-provenance-verifications",
        "final-release-list-observation",
        "tag-observation",
    }
    expected_source_names = expected_base_sources | (
        expected_product_sources if can_classify_products else set()
    )
    source_values = [
        receipts._object(item, label="final release source observation")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            fresh.get("sources"), label="final release source observations"
        )
    ]
    observed_source_names = [
        receipts._validate_string(  # noqa: SLF001
            item.get("name"), label="final release source observation name"
        )
        for item in source_values
    ]
    if (
        observed_source_names != sorted(observed_source_names)
        or len(observed_source_names) != len(set(observed_source_names))
        or set(observed_source_names) != expected_source_names
    ):
        raise receipts.ReleaseControlError(
            "final release source observation set is incomplete, duplicated, or unsorted"
        )
    source_bodies: dict[str, bytes] = {}
    fresh_snapshots: list[receipts.JSONObject] = []
    for source_name, source_value in zip(observed_source_names, source_values, strict=True):
        source_raw = receipts.canonical_json_bytes(source_value)
        source_bodies[source_name] = _contract_source_body(
            source_raw,
            label=f"final release {source_name}",
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name=source_name,
        )
        source_records[f"final-source-{source_name}"] = source_raw
        fresh_snapshots.append(receipts.source_snapshot(source_raw))

    workflow_source = receipts._read_regular(  # noqa: SLF001
        SCRIPT_ROOT / ".github/workflows/release.yml",
        label="final release workflow source",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    source_records["final-workflow-source"] = workflow_source
    _validate_final_lock_sources(
        main_lock=receipts.parse_external_json_bytes(
            source_bodies["ingress-ruleset-detail-observation"],
            label="final release main lock",
        ),
        workflow=receipts.parse_external_json_bytes(
            source_bodies["workflow-observation"],
            label="final release workflow",
        ),
        default_branch_workflow=source_bodies["default-branch-workflow-contents"],
        expected_workflow=workflow_source,
    )

    remote_complete = False
    if can_classify_products:
        if (
            candidate is None
            or manifest_value is None
            or transaction_digest is None
            or capsule_digest is None
        ):
            raise receipts.ReleaseControlError(
                "final product classification authority inputs are incomplete"
            )
        release_contract = _candidate_product_release_contract(
            manifest_value,
            transaction_authorization_digest=transaction_digest,
            recovery_capsule_digest=capsule_digest,
        )
        release_state = _classify_product_release_listing(
            receipts.parse_external_json_bytes(
                source_bodies["final-release-list-observation"],
                label="final product Release listing",
            ),
            contract=release_contract,
        )
        ghcr_state = _classify_ghcr_digest_observation(
            receipts.parse_external_json_bytes(
                source_bodies["final-ghcr-observation"],
                label="final GHCR observation",
            ),
            expected_digests=_expected_oci_object_digests_from_manifest(manifest_value),
        )
        tag_state = _classify_commit_tag_observation(
            receipts.parse_external_json_bytes(
                source_bodies["tag-observation"],
                label="final commit tag observation",
            ),
            candidate=candidate,
            transaction_authorization_digest=transaction_digest,
            recovery_capsule_digest=capsule_digest,
        )
        attestation_states = _classify_promotion_attestation_observation(
            receipts.parse_external_json_bytes(
                source_bodies["final-attestation-observation"],
                label="final promotion attestation observation",
            ),
            manifest=manifest_value,
        )
        expected_pypi_files = _candidate_pypi_files(manifest_value)
        pypi_state = _classify_pypi_project_observation(
            receipts.parse_external_json_bytes(
                source_bodies["final-pypi-project-observation"],
                label="final PyPI project observation",
            ),
            version=cast(str, manifest_value["version"]),
            expected_files=expected_pypi_files,
        )
        pypi_files_complete = pypi_state.get("missing") == []
        if pypi_files_complete:
            _validate_pypi_provenance_evidence(
                integrity_observations=receipts.parse_external_json_bytes(
                    source_bodies["final-pypi-integrity-observations"],
                    label="final PyPI Integrity observations",
                ),
                provenance_verifications=receipts.parse_external_json_bytes(
                    source_bodies["final-pypi-provenance-verifications"],
                    label="final PyPI provenance verifications",
                ),
                expected_files=expected_pypi_files,
                persisted_filenames=cast(list[str], pypi_state["present"]),
                distribution_root=Path(args.manifest).resolve(strict=True).parent,
            )
        remote_complete = (
            release_state.get("release") == "immutable_exact"
            and release_state.get("assets") == "existing_exact"
            and ghcr_state == "existing_exact"
            and tag_state == "existing_exact"
            and all(state == "existing_exact" for state in attestation_states.values())
            and pypi_files_complete
        )
        if remote_complete and not full_chain:
            raise receipts.ReleaseControlError(
                "final remote success omits a successful stage producer"
            )

    normalized_failure = "" if requested_failure is None else requested_failure.lower()
    failure_uncertain = any(
        token in normalized_failure for token in ("unknown", "uncertain", "ambiguous", "conflict")
    )
    next_action = (
        "none"
        if full_chain and remote_complete and requested_failure is None
        else "reconcile"
        if failure_uncertain
        else "resume"
    )
    completed, uncertain, pending = _derive_final_release_summary(
        stage_statuses=stage_statuses,
        full_chain=full_chain,
        remote_complete=remote_complete,
        failure_code=requested_failure,
        next_action=next_action,
    )
    dispatch_inputs_source = receipts._object(  # noqa: SLF001
        intent.get("inputs"), label="reconciliation dispatch inputs"
    )
    dispatch_inputs = {
        "candidate_run_id": str(dispatch_inputs_source["candidate_run_id"]),
        "candidate_manifest_digest": dispatch_inputs_source["candidate_manifest_digest"],
        "mode": dispatch_inputs_source["mode"],
        "transaction_nonce": dispatch_inputs_source["transaction_nonce"],
        "dispatch_binding": dispatch_inputs_source["dispatch_binding"],
    }
    mode = dispatch_inputs["mode"]
    if execution_digest is not None and mode != "recover_committed":
        raise receipts.ReleaseControlError(
            "initiate reconciliation cannot carry execution authorization"
        )
    if candidate is not None:
        expected_ref = "refs/heads/main" if mode == "initiate" else f"refs/tags/{candidate['tag']}"
        if (
            dispatch_inputs["candidate_manifest_digest"]
            != candidate.get("candidate_manifest_digest")
            or dispatch_inputs["candidate_run_id"] != str(candidate.get("candidate_run_id"))
            or run.get("ref") != expected_ref
            or (completed and mode == "recover_committed" and execution_digest is None)
        ):
            raise receipts.ReleaseControlError(
                "release reconciliation candidate or mode binding mismatch"
            )
    if stage_chain and mode == "recover_committed" and execution_digest is None:
        raise receipts.ReleaseControlError("recovery stage chain lacks execution authorization")
    lock_release = (
        completed
        and not uncertain
        and not pending
        and requested_failure is None
        and candidate is not None
        and transaction_digest is not None
        and capsule_digest is not None
        and full_chain
        and next_action == "none"
        and remote_complete
    )
    record: receipts.JSONObject = {
        "schema": RELEASE_RECONCILIATION_SCHEMA,
        "run": run,
        "dispatch_inputs": dispatch_inputs,
        "candidate": candidate,
        "transaction_authorization_digest": transaction_digest,
        "execution_authorization_digest": execution_digest,
        "recovery_capsule_digest": capsule_digest,
        "stage_chain": cast(list[receipts.JSONValue], stage_chain),
        "fresh_observations": cast(list[receipts.JSONValue], fresh_snapshots),
        "completed": completed,
        "uncertain": uncertain,
        "pending": pending,
        "failure_code": requested_failure,
        "next_action": next_action,
        "lock_release_permitted": lock_release,
        "evidence": {
            "source_bundle_digest": receipts.source_bundle_digest(source_records),
            "canonicalization_vector_digest": receipts.canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_promotion_transaction.py",
            "provider": "github.com",
            "method": "final-release-reconciliation",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    validate_release_reconciliation(record)
    if not receipts.write_once(output, receipts.canonical_json_bytes(record)):
        raise receipts.ReleaseControlError("reconciliation output path must be empty")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = receipts._ReleaseControlArgumentParser(description=__doc__)  # noqa: SLF001
    commands = parser.add_subparsers(dest="command", required=True)

    def add_preparation_inputs(command: argparse.ArgumentParser) -> None:
        command.add_argument("--repository-observation", required=True)
        command.add_argument("--workflow-observation", required=True)
        command.add_argument("--default-branch-workflow-contents", required=True)
        command.add_argument("--candidate-workflow-contents", required=True)
        command.add_argument("--candidate-manifest", required=True)
        command.add_argument(
            "--mode",
            required=True,
            choices=("initiate", "recover_committed"),
        )
        command.add_argument("--dispatcher-observation", required=True)
        command.add_argument("--prior-intents-observation", required=True)

    create_intent = commands.add_parser("create-dispatch-intent")
    add_preparation_inputs(create_intent)
    create_intent.add_argument("--output", required=True)
    create_intent.set_defaults(handler=_command_create_dispatch_intent)

    prepare = commands.add_parser("prepare-dispatch")
    add_preparation_inputs(prepare)
    prepare.add_argument("--journal-output", required=True)
    prepare.add_argument("--intent-output", required=True)
    prepare.add_argument("--request-output", required=True)
    prepare.set_defaults(handler=_command_prepare_dispatch)

    send = commands.add_parser("send-dispatch")
    send.add_argument("--journal", required=True)
    send.add_argument("--request", required=True)
    send.add_argument("--response-output", required=True)
    send.add_argument("--writer-inventory", required=True)
    send.add_argument("--writer-inventory-signature", required=True)
    send.add_argument("--owner-key-observation", required=True)
    send.set_defaults(handler=_command_send_dispatch)

    contain = commands.add_parser("contain-dispatch")
    contain.add_argument("--journal", required=True)
    contain.add_argument("--response")
    contain.add_argument("--uninstall-observation", required=True)
    contain.add_argument("--token-probe-observation", required=True)
    contain.add_argument("--writer-inventory", required=True)
    contain.add_argument("--writer-inventory-signature", required=True)
    contain.add_argument("--owner-key-observation", required=True)
    contain.add_argument("--output", required=True)
    contain.set_defaults(handler=_command_contain_dispatch)

    reconcile = commands.add_parser("reconcile-dispatch")
    reconcile.add_argument("--journal", required=True)
    reconcile.add_argument("--intent", required=True)
    reconcile.add_argument("--intent-signature", required=True)
    reconcile.add_argument("--owner-key-observation", required=True)
    reconcile.add_argument("--request", required=True)
    reconcile.add_argument("--response")
    reconcile.add_argument("--containment", required=True)
    reconcile.add_argument("--workflow-runs-observation", required=True)
    reconcile.add_argument("--identity-artifact-observations", required=True)
    reconcile.add_argument("--output", required=True)
    reconcile.set_defaults(handler=_command_reconcile_dispatch)

    admission = commands.add_parser("publish-dispatch-admission")
    admission.add_argument("--reconciliation", required=True)
    admission.add_argument("--containment", required=True)
    admission.add_argument("--owner-key-observation", required=True)
    admission.add_argument("--writer-inventory", required=True)
    admission.add_argument("--writer-inventory-signature", required=True)
    admission.add_argument("--identity-file", required=True)
    admission.add_argument("--final-workflow-runs-observation", required=True)
    admission.add_argument("--final-identity-artifact-observations", required=True)
    admission.add_argument("--output", required=True)
    admission.set_defaults(handler=_command_publish_dispatch_admission)

    tombstone = commands.add_parser("publish-dispatch-tombstone")
    tombstone.add_argument("--reconciliation", required=True)
    tombstone.add_argument("--reason", required=True)
    tombstone.add_argument("--identity-file", required=True)
    tombstone.add_argument("--owner-key-observation", required=True)
    tombstone.add_argument("--output", required=True)
    tombstone.set_defaults(handler=_command_publish_dispatch_tombstone)

    authorize = commands.add_parser("authorize")
    authorize.add_argument("manifest")
    for argument in (
        "bundle-root",
        "repository-observation",
        "repository-collaborators-observation",
        "repository-invitations-observation",
        "deploy-keys-observation",
        "actions-workflow-permissions-observation",
        "owner-signing-keys-observation",
        "active-runs-observation",
        "workflow-source-root",
        "main-branch-observation",
        "immutable-releases-observation",
        "rulesets-observation",
        "tag-ruleset-detail-observation",
        "ingress-ruleset-detail-observation",
        "workflow-observation",
        "default-branch-workflow-contents",
        "candidate-workflow-contents",
        "release-environment-observation",
        "release-deployment-policies-observation",
        "promotion-run-observation",
        "promotion-dispatch-identity",
        "approval-history-observation",
        "github-admission-authority-verification",
        "dispatch-intent",
        "dispatch-reconciliation",
    ):
        authorize.add_argument(f"--{argument}", required=True)
    authorize.add_argument("--expected-run-id", required=True, type=int)
    authorize.add_argument("--expected-run-attempt", required=True, type=int)
    authorize.add_argument("--expected-workflow-id", required=True, type=int)
    authorize.add_argument("--expected-workflow-path", required=True)
    authorize.add_argument("--mode", required=True, choices=("initiate", "recover_committed"))
    authorize.add_argument("--commit-marker-observation")
    authorize.add_argument("--transaction-authorization")
    authorize.add_argument("--recovery-capsule-verification")
    authorize.add_argument("--output", required=True)
    authorize.set_defaults(handler=_command_authorize)

    prerequisites = commands.add_parser("inspect-prerequisites")
    prerequisites.add_argument("--mode", required=True, choices=("hosted-smoke", "operational"))
    for argument in (
        "repository-observation",
        "repository-collaborators-observation",
        "repository-invitations-observation",
        "deploy-keys-observation",
        "actions-workflow-permissions-observation",
        "owner-signing-keys-observation",
        "workflow-source-root",
        "main-branch-observation",
        "immutable-releases-observation",
        "rulesets-observation",
        "tag-ruleset-detail-observation",
        "recovery-repository-observation",
        "expected-repository",
        "expected-owner-login",
    ):
        prerequisites.add_argument(f"--{argument}", required=True)
    prerequisites.add_argument("--expected-owner-user-id", required=True, type=int)
    prerequisites.add_argument("--ingress-ruleset-detail-observation")
    prerequisites.add_argument("--workflow-observation")
    prerequisites.add_argument("--default-branch-workflow-contents")
    prerequisites.add_argument("--candidate-workflow-contents")
    prerequisites.add_argument("--environment-observation", required=True, action="append")
    prerequisites.add_argument("--environment-policies-observation", required=True, action="append")
    prerequisites.add_argument("--recovery-immutable-releases-observation")
    prerequisites.add_argument("--recovery-authority-verification")
    prerequisites.add_argument("--github-authority-verification")
    prerequisites.add_argument("--pypi-authority-verification")
    prerequisites.add_argument("--output", required=True)
    prerequisites.set_defaults(handler=_command_inspect_prerequisites)

    verify_capsule = commands.add_parser("verify-recovery-capsule")
    for argument in (
        "capsule-manifest",
        "capsule-root",
        "recovery-repository-observation",
        "recovery-release-observation",
        "recovery-assets-observation",
        "execution-closure",
        "expected-candidate-digest",
        "expected-transaction-authorization-digest",
        "identity-file",
        "owner-key-observation",
        "output",
    ):
        verify_capsule.add_argument(f"--{argument}", required=True)
    verify_capsule.set_defaults(handler=_command_verify_recovery_capsule)

    def add_stage_authority_inputs(command: argparse.ArgumentParser, *, bundle_root: bool) -> None:
        command.add_argument("manifest")
        if bundle_root:
            command.add_argument("--bundle-root", required=True)
        command.add_argument("--transaction-authorization", required=True)
        command.add_argument("--execution-authorization")
        command.add_argument("--recovery-capsule-verification", required=True)

    tag_message = commands.add_parser("tag-message")
    tag_message.add_argument("manifest")
    tag_message.add_argument("--bundle-root", required=True)
    tag_message.add_argument("--transaction-authorization", required=True)
    tag_message.add_argument("--recovery-capsule-verification", required=True)
    tag_message.set_defaults(handler=_command_tag_message)

    plan_preparation = commands.add_parser("plan-preparation")
    add_stage_authority_inputs(plan_preparation, bundle_root=True)
    plan_preparation.add_argument("--release-list-observation", required=True)
    plan_preparation.add_argument("--ghcr-observation", required=True)
    plan_preparation.add_argument("--output", required=True)
    plan_preparation.set_defaults(handler=_command_plan_preparation)

    record_preparation = commands.add_parser("record-preparation")
    record_preparation.add_argument("plan")
    record_preparation.add_argument("--pre-observations", required=True)
    record_preparation.add_argument("--post-observations", required=True)
    record_preparation.add_argument("--output", required=True)
    record_preparation.set_defaults(handler=_command_record_preparation)

    plan_commit = commands.add_parser("plan-commit")
    add_stage_authority_inputs(plan_commit, bundle_root=True)
    for argument in (
        "preparation-outcome",
        "commit-authority-verification",
        "tag-observation",
        "release-list-observation",
        "ghcr-observation",
        "attestation-observations",
    ):
        plan_commit.add_argument(f"--{argument}", required=True)
    plan_commit.add_argument("--output", required=True)
    plan_commit.set_defaults(handler=_command_plan_commit)

    record_commit = commands.add_parser("record-commit")
    record_commit.add_argument("plan")
    record_commit.add_argument("--pre-observations", required=True)
    record_commit.add_argument("--post-observations", required=True)
    record_commit.add_argument("--output", required=True)
    record_commit.set_defaults(handler=_command_record_commit)

    verify_surfaces = commands.add_parser("verify-github-ghcr")
    add_stage_authority_inputs(verify_surfaces, bundle_root=True)
    verify_surfaces.add_argument("--preparation-outcome", required=True)
    verify_surfaces.add_argument("--commit-outcome", required=True)
    verify_surfaces.add_argument("--fresh-observations", required=True)
    verify_surfaces.add_argument("--pinned-gh", required=True)
    verify_surfaces.add_argument("--output", required=True)
    verify_surfaces.set_defaults(handler=_command_verify_github_ghcr)

    record_pypi = commands.add_parser("record-pypi")
    add_stage_authority_inputs(record_pypi, bundle_root=False)
    for argument in (
        "commit-outcome",
        "github-ghcr-verification",
        "pypi-authority-verification",
        "pre-observation",
        "post-observation",
        "integrity-observations",
        "provenance-verifications",
        "approval-history-observation",
    ):
        record_pypi.add_argument(f"--{argument}", required=True)
    record_pypi.add_argument("--output", required=True)
    record_pypi.set_defaults(handler=_command_record_pypi)

    final_reconcile = commands.add_parser("reconcile")
    for argument in (
        "run-observation",
        "dispatch-identity",
        "dispatch-intent",
        "dispatch-reconciliation",
        "fresh-observations",
        "failure-code",
        "output",
    ):
        final_reconcile.add_argument(f"--{argument}", required=True)
    final_reconcile.add_argument("--manifest")
    final_reconcile.add_argument("--transaction-authorization")
    final_reconcile.add_argument("--execution-authorization")
    final_reconcile.add_argument("--recovery-capsule-verification")
    final_reconcile.add_argument("--stage-records")
    final_reconcile.set_defaults(handler=_command_reconcile_release)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        return cast(int, args.handler(args))
    except (receipts.ReleaseControlError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
