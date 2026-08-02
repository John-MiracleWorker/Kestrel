"""Cycle-safe, immutable authority for one reviewed private-LAN runtime target."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from nested_memvid_agent.lan_discovery_models import (
    ManualLanEndpoint,
    NetworkInterface,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import PrivateScanScope
from nested_memvid_agent.security_boundary import redact_text

LAN_OPENAI_RUNTIME_HARDENING_VERSION = "kestrel.lan.runtime.openai.v1"

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROVIDER_PROFILE_ID_RE = re.compile(r"lan-provider-[0-9a-f]{64}\Z")
_TARGET_ID_RE = re.compile(r"lan-target-[0-9a-f]{64}\Z")
_URL_RE = re.compile(r"(?:[a-z][a-z0-9+.-]*://|\bwww\.)", re.IGNORECASE)
_HOSTNAME_RE = re.compile(
    r"(?<![a-z0-9-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?)\.?(?![a-z0-9-])",
    re.IGNORECASE,
)
_HOST_PORT_RE = re.compile(
    r"(?<![a-z0-9-])(?:localhost|[a-z][a-z0-9-]{0,62}|"
    r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}|\[[0-9a-f:.%]+\])"
    r":[0-9]{1,5}(?![0-9])",
    re.IGNORECASE,
)
_USERINFO_RE = re.compile(
    r"(?<!\S)[^\s/@]+(?::[^\s/@]+)?@(?:\[[^\]\s]+\]|[^\s/@]+)",
    re.IGNORECASE,
)
_IPV4_MATERIAL_RE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
_IPV6_MATERIAL_RE = re.compile(
    r"(?<![0-9a-f:])(?:\[[0-9a-f:.%]+\]|"
    r"[0-9a-f][0-9a-f:.%]*:[0-9a-f:.%]*)(?![0-9a-f:])",
    re.IGNORECASE,
)
_CREDENTIAL_RE = re.compile(
    r"(?:\bauthorization\b|\bbearer\s|\bbasic\s|\bapi[ _-]?key\b|"
    r"\bpassword\b|\bsecret\b|\btoken\b|\bsk-[a-z0-9_-]{8,}|"
    r"\bgh[pousr]_[a-z0-9]{8,}|\bAKIA[A-Z0-9]{12,}|\bprivate key\b)",
    re.IGNORECASE,
)
_LOCALHOST_RE = re.compile(r"(?<![A-Za-z0-9-])localhost(?![A-Za-z0-9-])", re.IGNORECASE)
_ELIGIBLE_IPV4_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)
_ELIGIBLE_IPV6_NETWORKS = (
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)


class LanRuntimeAuthorityResolver(Protocol):
    """Resolve the current durable authority for one reviewed LAN target."""

    def __call__(self, target_id: str) -> LanRuntimeAuthority: ...


class LanRuntimeAuthorityError(ValueError):
    """Raised when a LAN runtime snapshot is not canonical authority."""


@dataclass(frozen=True, slots=True)
class LanRuntimeAuthority:
    """Internal-only, canonical snapshot authorizing one direct LAN request."""

    scope: PrivateScanScope
    endpoint: ResolvedLanEndpoint | ManualLanEndpoint
    source_address: str
    os_interface_identity: str
    interface_index: int
    provider_profile_id: str
    reviewed_target_id: str
    model_id: str
    api_shape: str
    runtime_adapter: str
    runtime_hardening_version: str
    endpoint_binding_digest: str
    endpoint_fingerprint: str
    reviewed_material_binding_digest: str
    review_digest: str
    fresh_until: str

    def __post_init__(self) -> None:
        _rebuild_authority(self)

    @property
    def fresh_until_datetime(self) -> datetime:
        return _parse_canonical_utc(self.fresh_until)


def authenticate_lan_runtime_authority(value: object) -> LanRuntimeAuthority:
    """Reconstruct every protected field and return only an exact snapshot."""

    if type(value) is not LanRuntimeAuthority:
        raise LanRuntimeAuthorityError("LAN runtime authority must use the exact snapshot type")
    _rebuild_authority(value)
    return value


def derive_lan_runtime_provider_profile_id(endpoint_binding_digest: str) -> str:
    """Derive the Task 5 managed-provider ID from its endpoint binding."""

    _require_digest(endpoint_binding_digest)
    digest = _sha256_digest(
        {
            "schema": "kestrel.lan.provider-binding.v1",
            "endpoint_binding_digest": endpoint_binding_digest,
        }
    )
    return "lan-provider-" + digest.removeprefix("sha256:")


def derive_lan_runtime_endpoint_binding_digest(
    endpoint: ResolvedLanEndpoint | ManualLanEndpoint,
) -> str:
    """Recompute the Task 4 endpoint-binding digest from its literal destination."""

    if type(endpoint) not in {ResolvedLanEndpoint, ManualLanEndpoint}:
        raise LanRuntimeAuthorityError("LAN runtime endpoint binding is invalid")
    if type(endpoint) is ManualLanEndpoint and endpoint.kind != "manual":
        raise LanRuntimeAuthorityError("LAN runtime endpoint binding is invalid")
    return _sha256_digest(
        {
            "address": endpoint.address,
            "interface_id": endpoint.interface_id,
            "port": endpoint.port,
            "schema": "kestrel.lan.endpoint-binding.v1",
        }
    )


def derive_lan_runtime_interface_binding_digest(
    *,
    os_interface_identity: str,
    source_address: str,
    interface_index: int,
    interface_id: str,
    confirmed_network: str,
    endpoint_binding_digest: str,
    endpoint_fingerprint: str,
    reviewed_material_binding_digest: str,
    review_digest: str,
    observation_source: str = "active",
    endpoint_kind: str = "automatic",
) -> str:
    """Bind one reviewed runtime source/interface tuple to its exact review."""

    if (
        type(os_interface_identity) is not str
        or not os_interface_identity
        or os_interface_identity != os_interface_identity.strip()
        or len(os_interface_identity.encode("utf-8")) > 256
        or unicodedata.normalize("NFC", os_interface_identity) != os_interface_identity
        or any(
            unicodedata.category(character).startswith("C") for character in os_interface_identity
        )
    ):
        raise LanRuntimeAuthorityError("LAN runtime interface identity is invalid")
    source = _parse_eligible_literal(source_address)
    if (
        str(source) != source_address
        or isinstance(interface_index, bool)
        or not isinstance(interface_index, int)
        or not 0 < interface_index <= 2**31 - 1
    ):
        raise LanRuntimeAuthorityError("LAN runtime interface source is invalid")
    _require_digest(interface_id)
    try:
        network = ipaddress.ip_network(confirmed_network, strict=True)
    except (TypeError, ValueError):
        raise LanRuntimeAuthorityError("LAN runtime confirmed network is invalid") from None
    if (
        observation_source not in {"active", "mdns", "manual"}
        or endpoint_kind not in {"automatic", "manual"}
        or (observation_source == "manual") != (endpoint_kind == "manual")
    ):
        raise LanRuntimeAuthorityError("LAN runtime endpoint provenance is invalid")
    manual_endpoint = endpoint_kind == "manual"
    if (
        str(network) != confirmed_network
        or source.version != network.version
        or (manual_endpoint and network.prefixlen != network.max_prefixlen)
        or (not manual_endpoint and source not in network)
    ):
        raise LanRuntimeAuthorityError("LAN runtime confirmed network is invalid")
    for digest in (
        endpoint_binding_digest,
        endpoint_fingerprint,
        reviewed_material_binding_digest,
        review_digest,
    ):
        _require_digest(digest)
    preimage = {
        "schema": "kestrel.lan.reviewed-runtime-interface-binding.v1",
        "os_interface_identity": os_interface_identity,
        "source_address": source_address,
        "interface_index": interface_index,
        "interface_id": interface_id,
        "confirmed_network": confirmed_network,
        "endpoint_binding_digest": endpoint_binding_digest,
        "endpoint_fingerprint": endpoint_fingerprint,
        "reviewed_material_binding_digest": reviewed_material_binding_digest,
        "review_digest": review_digest,
    }
    if manual_endpoint:
        preimage.update(
            {
                "observation_source": observation_source,
                "endpoint_kind": endpoint_kind,
            }
        )
    return _sha256_digest(preimage)


def derive_lan_runtime_authority_interface_binding_digest(
    authority: LanRuntimeAuthority,
) -> str:
    """Recompute the reviewed runtime-interface digest from an exact authority."""

    current = authenticate_lan_runtime_authority(authority)
    return derive_lan_runtime_interface_binding_digest(
        os_interface_identity=current.os_interface_identity,
        source_address=current.source_address,
        interface_index=current.interface_index,
        interface_id=current.scope.interface.interface_id,
        confirmed_network=current.scope.network,
        endpoint_binding_digest=current.endpoint_binding_digest,
        endpoint_fingerprint=current.endpoint_fingerprint,
        reviewed_material_binding_digest=current.reviewed_material_binding_digest,
        review_digest=current.review_digest,
        observation_source=("manual" if type(current.endpoint) is ManualLanEndpoint else "active"),
        endpoint_kind=current.endpoint.kind,
    )


def derive_lan_runtime_target_id(provider_profile_id: str, model_id: str) -> str:
    """Derive the Task 5 managed-target ID from provider and canonical model."""

    if (
        type(provider_profile_id) is not str
        or _PROVIDER_PROFILE_ID_RE.fullmatch(provider_profile_id) is None
    ):
        raise LanRuntimeAuthorityError("LAN runtime provider profile ID is invalid")
    _require_canonical_model_id(model_id)
    digest = _sha256_digest(
        {
            "schema": "kestrel.lan.model-target.v1",
            "provider_profile_id": provider_profile_id,
            "model_id": model_id,
        }
    )
    return "lan-target-" + digest.removeprefix("sha256:")


def _rebuild_authority(value: LanRuntimeAuthority) -> None:
    error = LanRuntimeAuthorityError("LAN runtime authority is not canonical")
    try:
        if type(value.scope) is not PrivateScanScope:
            raise error
        interface = value.scope.interface
        if type(interface) is not NetworkInterface:
            raise error
        if type(interface.os_identity) is not str or not interface.os_identity:
            raise error
        normalized_interface = NetworkInterface.from_addresses(
            os_identity=interface.os_identity,
            display_name=interface.os_identity,
            addresses=interface.addresses,
        )
        if interface != normalized_interface:
            raise error
        normalized_scope = PrivateScanScope.from_request(
            normalized_interface,
            value.scope.network,
        )
        if value.scope != normalized_scope:
            raise error

        if type(value.endpoint) is ManualLanEndpoint:
            normalized_endpoint: ResolvedLanEndpoint | ManualLanEndpoint = (
                ManualLanEndpoint.from_exact_scope(
                    normalized_scope,
                    value.endpoint.address,
                    value.endpoint.port,
                )
            )
        elif type(value.endpoint) is ResolvedLanEndpoint:
            normalized_endpoint = ResolvedLanEndpoint.from_scope(
                normalized_scope,
                value.endpoint.address,
                value.endpoint.port,
            )
        else:
            raise error
        if value.endpoint != normalized_endpoint:
            raise error

        source = _parse_eligible_literal(value.source_address)
        attached_sources = {
            ipaddress.ip_interface(address).ip for address in normalized_interface.addresses
        }
        if source not in attached_sources:
            raise error
        if source.version != ipaddress.ip_address(normalized_endpoint.address).version:
            raise error
        if type(normalized_endpoint) is ResolvedLanEndpoint and source not in ipaddress.ip_network(
            normalized_scope.network, strict=True
        ):
            raise error

        if (
            type(value.os_interface_identity) is not str
            or value.os_interface_identity != normalized_interface.os_identity
        ):
            raise error
        if (
            isinstance(value.interface_index, bool)
            or not isinstance(value.interface_index, int)
            or value.interface_index <= 0
        ):
            raise error
        if (
            type(value.provider_profile_id) is not str
            or _PROVIDER_PROFILE_ID_RE.fullmatch(value.provider_profile_id) is None
            or value.provider_profile_id
            != derive_lan_runtime_provider_profile_id(value.endpoint_binding_digest)
        ):
            raise error
        if (
            type(value.reviewed_target_id) is not str
            or _TARGET_ID_RE.fullmatch(value.reviewed_target_id) is None
        ):
            raise error
        _require_canonical_model_id(value.model_id)
        if value.reviewed_target_id != derive_lan_runtime_target_id(
            value.provider_profile_id,
            value.model_id,
        ):
            raise error
        if value.api_shape != "openai_compatible":
            raise error
        if value.runtime_adapter != "lan-openai-compatible":
            raise error
        if value.runtime_hardening_version != LAN_OPENAI_RUNTIME_HARDENING_VERSION:
            raise error
        if value.endpoint_binding_digest != derive_lan_runtime_endpoint_binding_digest(
            normalized_endpoint
        ):
            raise error
        for digest in (
            value.endpoint_fingerprint,
            value.reviewed_material_binding_digest,
            value.review_digest,
        ):
            _require_digest(digest)
        _parse_canonical_utc(value.fresh_until)
    except LanRuntimeAuthorityError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise error from None


def _parse_eligible_literal(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if type(value) is not str or not value or value != value.strip() or "%" in value:
        raise LanRuntimeAuthorityError("LAN runtime source must be an unzoned literal address")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        raise LanRuntimeAuthorityError("LAN runtime source must be a literal address") from None
    if str(parsed) != value:
        raise LanRuntimeAuthorityError("LAN runtime source must be canonical")
    eligible = (
        any(parsed in network for network in _ELIGIBLE_IPV4_NETWORKS)
        if isinstance(parsed, ipaddress.IPv4Address)
        else any(parsed in network for network in _ELIGIBLE_IPV6_NETWORKS)
    )
    if not eligible:
        raise LanRuntimeAuthorityError("LAN runtime source must be private")
    return parsed


def _require_canonical_model_id(value: object) -> None:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8", errors="surrogatepass")) > 512
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character).startswith("C") for character in value)
        or _LOCALHOST_RE.search(value)
        or _CREDENTIAL_RE.search(value)
        or redact_text(value) != value
    ):
        raise LanRuntimeAuthorityError("LAN runtime model ID is not canonical")
    contains_transport = _contains_transport_material(value)
    if (":" in value or contains_transport) and not _is_canonical_model_name_tag(value):
        raise LanRuntimeAuthorityError("LAN runtime model ID contains transport material")


def _is_canonical_model_name_tag(value: str) -> bool:
    if value.count(":") != 1:
        return False
    name, tag = value.split(":", 1)
    if (
        not name
        or not tag
        or name.casefold() == "localhost"
        or tag.isdecimal()
        or re.search(r"[A-Za-z]", tag) is None
    ):
        return False
    return not _contains_transport_material(name) and not _contains_transport_material(tag)


def _contains_transport_material(value: str) -> bool:
    if _URL_RE.search(value) or _USERINFO_RE.search(value) or _HOST_PORT_RE.search(value):
        return True
    for match in _IPV4_MATERIAL_RE.finditer(value):
        try:
            ipaddress.IPv4Address(match.group())
        except ValueError:
            continue
        return True
    for match in _IPV6_MATERIAL_RE.finditer(value):
        candidate = match.group().strip("[]").rstrip(".").partition("%")[0]
        try:
            ipaddress.IPv6Address(candidate)
        except ValueError:
            continue
        return True
    return _HOSTNAME_RE.search(value) is not None


def _require_digest(value: object) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise LanRuntimeAuthorityError("LAN runtime digest is invalid")


def _sha256_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _parse_canonical_utc(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z") or len(value) > 64:
        raise LanRuntimeAuthorityError("LAN runtime freshness must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise LanRuntimeAuthorityError("LAN runtime freshness is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise LanRuntimeAuthorityError("LAN runtime freshness must be UTC")
    canonical = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise LanRuntimeAuthorityError("LAN runtime freshness must be canonical UTC")
    return parsed.astimezone(UTC)
