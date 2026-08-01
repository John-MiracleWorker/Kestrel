"""Bounded passive mDNS collection for an explicitly confirmed private scope.

The collector treats every callback value as untrusted public display material.
It never resolves a hostname, infers a provider, or creates target authority.
"""

from __future__ import annotations

import importlib
import ipaddress
import json
import platform
import re
import socket
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from nested_memvid_agent.lan_discovery_models import (
    KNOWN_MODEL_SERVICE_PORTS,
    MAX_ACTIVE_HOSTS,
    LanScanLimits,
    NetworkInterface,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import (
    PrivateScanScope,
    enumerate_private_interfaces,
)

ALLOWED_MODEL_SERVICE_TYPES = (
    "_ollama._tcp.local.",
    "_lmstudio._tcp.local.",
    "_openai._tcp.local.",
    "_kestrel-model._tcp.local.",
)
MAX_MDNS_METADATA_BYTES = 4096

_DISPLAY_TXT_FIELDS = frozenset(
    {"display_name", "description", "vendor", "product", "version"}
)
# This per-field cap also guarantees that reconciling all five fields (including
# worst-case JSON escaping) stays within the canonical aggregate byte bound.
_MAX_DISPLAY_VALUE_BYTES = 300
_MAX_INSTANCE_NAME_BYTES = 255
_MAX_ADDRESS_LITERAL_CHARS = 128
_MAX_TXT_KEY_BYTES = 32
_MAX_LIVE_SERVICE_RECORDS = 16
_LIVE_BROWSER_JOIN_SECONDS = 0.25
_LIVE_CLEANUP_SECONDS = 0.75
_DNS_TYPE_A = 1
_DNS_TYPE_TXT = 16
_DNS_TYPE_AAAA = 28
_DNS_TYPE_SRV = 33
_DNS_CLASS_IN = 1
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
_NUMERIC_VERSION_RE = re.compile(
    r"v?(?P<release>[0-9]+(?:\.[0-9]+)+)"
    r"(?:[-+][0-9a-z]+(?:[.-][0-9a-z]+)*)?",
    re.IGNORECASE,
)
_PROSE_DOTTED_ABBREVIATIONS = frozenset({"e.g.", "i.e."})
_CREDENTIAL_RE = re.compile(
    r"(?:\bauthorization\b|\bbearer\s|\bbasic\s|\bapi[ _-]?key\b|"
    r"\bpassword\b|\bsecret\b|\btoken\b|\bsk-[a-z0-9_-]{8,}|"
    r"\bgh[pousr]_[a-z0-9]{8,}|\bAKIA[A-Z0-9]{12,}|\bprivate key\b)",
    re.IGNORECASE,
)
_ELIGIBLE_IPV4_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)
_ELIGIBLE_IPV6_NETWORKS = (
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)


@dataclass(frozen=True)
class MdnsBinding:
    """The exact numeric adapter authority passed to an mDNS implementation."""

    ipv4_addresses: tuple[str, ...]
    ipv6_interface_index: int | None


@dataclass(frozen=True)
class CurrentInterfaceState:
    """Fresh OS-owned identity evidence used immediately before socket binding."""

    interface_index: int
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class MdnsRecord:
    """A narrow, deliberately untrusted adapter callback value."""

    service_type: object
    instance_name: object
    addresses: tuple[object, ...]
    port: object
    properties: Mapping[object, object]
    hostname: object | None = None


@dataclass(frozen=True, init=False)
class LanCandidate:
    """Immutable public display evidence for one literal private endpoint."""

    interface_id: str
    address: str
    port: int
    service_type: str
    instance_name: str
    _metadata_items: tuple[tuple[str, str], ...]
    metadata_json: str

    @classmethod
    def _from_normalized(
        cls,
        *,
        interface_id: str,
        address: str,
        port: int,
        service_type: str,
        instance_name: str,
        metadata: Mapping[str, str],
    ) -> LanCandidate:
        metadata_items = tuple(sorted(metadata.items()))
        metadata_payload = {
            "instance_name": instance_name,
            "service_type": service_type,
            "txt": dict(metadata_items),
        }
        metadata_json = json.dumps(
            metadata_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(metadata_json.encode("utf-8")) > MAX_MDNS_METADATA_BYTES:
            raise ValueError("mDNS display metadata exceeds its UTF-8 byte limit")
        candidate = object.__new__(cls)
        object.__setattr__(candidate, "interface_id", interface_id)
        object.__setattr__(candidate, "address", address)
        object.__setattr__(candidate, "port", port)
        object.__setattr__(candidate, "service_type", service_type)
        object.__setattr__(candidate, "instance_name", instance_name)
        object.__setattr__(candidate, "_metadata_items", metadata_items)
        object.__setattr__(candidate, "metadata_json", metadata_json)
        return candidate

    @property
    def metadata(self) -> Mapping[str, str]:
        """Return a read-only copy so callback-owned mappings never escape."""

        return MappingProxyType(dict(self._metadata_items))

    @property
    def provider_hint(self) -> None:
        """mDNS never grants or even suggests provider identity."""

        return None


class MdnsSession(Protocol):
    """A started browser with only bounded waiting and idempotent cleanup."""

    def wait(self, seconds: float) -> None: ...

    def close(self) -> None: ...


class MdnsAdapterFactory(Protocol):
    def __call__(
        self,
        binding: MdnsBinding,
        service_types: tuple[str, ...],
        callback: Callable[[MdnsRecord], None],
    ) -> MdnsSession: ...


InterfaceStateResolver = Callable[[str], CurrentInterfaceState]


class _CandidateState:
    def __init__(
        self,
        *,
        scope: PrivateScanScope,
        binding: MdnsBinding,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        self._scope = scope
        self._binding = binding
        self._deadline = deadline
        self._clock = clock
        self._lock = threading.Lock()
        self._closed = False
        self._candidates: dict[tuple[str, int], LanCandidate] = {}

    def admit(self, record: MdnsRecord) -> None:
        """Contain one hostile callback entirely under the admission lock."""

        with self._lock:
            if self._closed or self._clock() >= self._deadline:
                return
            try:
                normalized = _normalize_record(record, self._scope, self._binding)
                for candidate in normalized:
                    key = (candidate.address, candidate.port)
                    existing = self._candidates.get(key)
                    self._candidates[key] = (
                        candidate if existing is None else _reconcile(existing, candidate)
                    )
                self._retain_top_k()
            except Exception:
                # A malformed callback is public input, not a scan-level failure.
                return

    def close_admissions(self) -> None:
        with self._lock:
            self._closed = True

    def snapshot(self) -> tuple[LanCandidate, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._candidates.values(),
                    key=lambda candidate: _endpoint_sort_key(candidate.address, candidate.port),
                )
            )

    def _retain_top_k(self) -> None:
        if len(self._candidates) <= MAX_ACTIVE_HOSTS:
            return
        retained_keys = set(
            sorted(
                self._candidates,
                key=lambda key: _endpoint_sort_key(key[0], key[1]),
            )[:MAX_ACTIVE_HOSTS]
        )
        self._candidates = {
            key: candidate
            for key, candidate in self._candidates.items()
            if key in retained_keys
        }


def collect_mdns_candidates(
    scope: PrivateScanScope,
    *,
    adapter_factory: MdnsAdapterFactory | None = None,
    clock: Callable[[], float] = time.monotonic,
    interface_state_resolver: InterfaceStateResolver | None = None,
) -> tuple[LanCandidate, ...]:
    """Collect one fixed-window, passive set for a canonical owner-confirmed scope."""

    canonical_scope = _authenticate_scope(scope)
    binding = _binding_for_scope(
        canonical_scope,
        (
            interface_state_resolver
            if interface_state_resolver is not None
            else _resolve_current_interface_state
        ),
    )
    limits = LanScanLimits()
    started_at = clock()
    deadline = started_at + limits.mdns_window_seconds
    state = _CandidateState(
        scope=canonical_scope,
        binding=binding,
        deadline=deadline,
        clock=clock,
    )
    factory = adapter_factory if adapter_factory is not None else _live_adapter_factory
    session: MdnsSession | None = None
    try:
        session = factory(binding, ALLOWED_MODEL_SERVICE_TYPES, state.admit)
        remaining = max(0.0, deadline - clock())
        session.wait(remaining)
    finally:
        # This lock transition happens before browser cancellation/join in close().
        state.close_admissions()
        if session is not None:
            session.close()
    return state.snapshot()


def _authenticate_scope(scope: PrivateScanScope) -> PrivateScanScope:
    error = ValueError("mDNS collection requires a canonical confirmed scope")
    if not isinstance(scope, PrivateScanScope):
        raise error
    try:
        interface = scope.interface
        canonical_interface = NetworkInterface.from_addresses(
            os_identity=interface.os_identity,
            display_name=interface.display_name,
            addresses=interface.addresses,
        )
        if not all(
            _is_eligible_confirmed_interface_address(value)
            for value in canonical_interface.addresses
        ):
            raise ValueError("interface contains an ineligible address")
        canonical_scope = PrivateScanScope.from_request(canonical_interface, scope.network)
    except (AttributeError, TypeError, ValueError) as exc:
        raise error from exc
    if scope != canonical_scope:
        raise error
    return canonical_scope


def _binding_for_scope(
    scope: PrivateScanScope,
    interface_state_resolver: InterfaceStateResolver,
) -> MdnsBinding:
    try:
        current_state = interface_state_resolver(scope.interface.os_identity)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("mDNS requires authenticated current interface state") from exc

    resolved_index = getattr(current_state, "interface_index", None)
    if (
        isinstance(resolved_index, bool)
        or not isinstance(resolved_index, int)
        or resolved_index <= 0
    ):
        raise ValueError("mDNS requires a verified numeric interface index")

    current_addresses = getattr(current_state, "addresses", None)
    if type(current_addresses) is not tuple or len(current_addresses) > MAX_ACTIVE_HOSTS:
        raise ValueError("mDNS current selected interface addresses are invalid")
    try:
        current_interface = NetworkInterface.from_addresses(
            os_identity=scope.interface.os_identity,
            display_name=scope.interface.display_name,
            addresses=current_addresses,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("mDNS current selected interface addresses are invalid") from exc
    if not all(
        _is_eligible_confirmed_interface_address(value)
        for value in current_interface.addresses
    ):
        raise ValueError("mDNS current selected interface addresses are invalid")
    if current_interface.addresses != scope.interface.addresses:
        raise ValueError("mDNS current selected interface addresses changed")

    ipv4_addresses: list[str] = []
    has_ipv6 = False
    for value in current_interface.addresses:
        attached = ipaddress.ip_interface(value)
        if isinstance(attached, ipaddress.IPv4Interface):
            ipv4_addresses.append(str(attached.ip))
        else:
            has_ipv6 = True
    ipv6_interface_index = resolved_index if has_ipv6 else None

    return MdnsBinding(
        ipv4_addresses=tuple(
            sorted(ipv4_addresses, key=lambda value: int(ipaddress.IPv4Address(value)))
        ),
        ipv6_interface_index=ipv6_interface_index,
    )


def _is_eligible_confirmed_interface_address(value: str) -> bool:
    attached = ipaddress.ip_interface(value)
    address = attached.ip
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_reserved:
        return False
    if isinstance(attached, ipaddress.IPv4Interface):
        return any(attached.network.subnet_of(network) for network in _ELIGIBLE_IPV4_NETWORKS)
    return any(attached.network.subnet_of(network) for network in _ELIGIBLE_IPV6_NETWORKS)


def _resolve_verified_interface_index(os_identity: str) -> int:
    system_prefix = f"{platform.system().lower()}:"
    if not os_identity.startswith(system_prefix):
        raise ValueError("interface OS identity does not belong to this host platform")
    interface_name = os_identity[len(system_prefix) :]
    if not interface_name:
        raise ValueError("interface OS identity has no interface name")
    index = socket.if_nametoindex(interface_name)
    if index <= 0 or socket.if_indextoname(index) != interface_name:
        raise ValueError("interface index could not be authenticated")
    return index


def _resolve_current_interface_state(os_identity: str) -> CurrentInterfaceState:
    current_interface = next(
        (
            interface
            for interface in enumerate_private_interfaces()
            if interface.os_identity == os_identity
        ),
        None,
    )
    if current_interface is None:
        raise ValueError("mDNS current selected interface addresses changed")
    try:
        interface_index = _resolve_verified_interface_index(os_identity)
    except (OSError, ValueError) as exc:
        raise ValueError("mDNS requires a verified numeric interface index") from exc
    return CurrentInterfaceState(
        interface_index=interface_index,
        addresses=current_interface.addresses,
    )


def _normalize_record(
    record: MdnsRecord,
    scope: PrivateScanScope,
    binding: MdnsBinding,
) -> tuple[LanCandidate, ...]:
    if type(record.service_type) is not str or record.service_type not in (
        ALLOWED_MODEL_SERVICE_TYPES
    ):
        raise ValueError("mDNS service type is not allowed")
    service_type = record.service_type
    instance_name = _normalize_instance_name(record.instance_name, service_type)
    if isinstance(record.port, bool) or not isinstance(record.port, int):
        raise ValueError("mDNS port must be an integer")
    if record.port not in KNOWN_MODEL_SERVICE_PORTS:
        raise ValueError("mDNS port is not an approved model-service port")
    if not isinstance(record.addresses, tuple):
        raise ValueError("mDNS addresses must be an immutable tuple")
    if len(record.addresses) > MAX_ACTIVE_HOSTS:
        raise ValueError("one mDNS record contains too many addresses")
    metadata = _normalize_txt(record.properties)

    candidates: list[LanCandidate] = []
    for raw_address in record.addresses:
        try:
            address = _normalize_address(raw_address, binding)
            endpoint = ResolvedLanEndpoint.from_scope(scope, address, record.port)
            candidates.append(
                LanCandidate._from_normalized(
                    interface_id=endpoint.interface_id,
                    address=endpoint.address,
                    port=endpoint.port,
                    service_type=service_type,
                    instance_name=instance_name,
                    metadata=metadata,
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(candidates)


def _normalize_instance_name(raw_name: object, service_type: str) -> str:
    if type(raw_name) is not str:
        raise ValueError("mDNS instance name must be text")
    if len(raw_name) > _MAX_INSTANCE_NAME_BYTES + len(service_type) + 1:
        raise ValueError("mDNS instance name exceeds its character limit")
    suffix = f".{service_type}"
    if not raw_name.endswith(suffix):
        raise ValueError("mDNS instance name must match its service type")
    instance_name = raw_name[: -len(suffix)]
    _validate_display_text(instance_name, max_bytes=_MAX_INSTANCE_NAME_BYTES)
    return instance_name


def _normalize_txt(properties: Mapping[object, object]) -> dict[str, str]:
    if not isinstance(properties, Mapping):
        raise ValueError("mDNS TXT properties must be a mapping")
    if len(properties) > len(_DISPLAY_TXT_FIELDS):
        raise ValueError("mDNS TXT contains too many entries")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in properties.items():
        if type(raw_key) is not bytes or type(raw_value) is not bytes:
            raise ValueError("mDNS TXT keys and values must be raw bytes")
        if len(raw_key) > _MAX_TXT_KEY_BYTES or len(raw_value) > _MAX_DISPLAY_VALUE_BYTES:
            raise ValueError("mDNS TXT key or value exceeds its byte limit")
        try:
            key = raw_key.decode("utf-8", errors="strict")
            value = raw_value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("mDNS TXT must be strict UTF-8") from exc
        if key not in _DISPLAY_TXT_FIELDS:
            raise ValueError("mDNS TXT key is not public display metadata")
        _validate_display_text(
            value,
            max_bytes=_MAX_DISPLAY_VALUE_BYTES,
            allowed_dotted_value="llama.cpp" if key == "product" else None,
            allow_numeric_version=key == "version",
        )
        normalized[key] = value
    return normalized


def _validate_display_text(
    value: str,
    *,
    max_bytes: int,
    allowed_dotted_value: str | None = None,
    allow_numeric_version: bool = False,
) -> None:
    if not value or value != value.strip():
        raise ValueError("mDNS display text must be non-empty and canonical")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError("mDNS display text exceeds its byte limit")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("mDNS display text must use canonical NFC normalization")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("mDNS display text contains control material")
    if _contains_transport_material(
        value,
        allowed_dotted_value=allowed_dotted_value,
        allow_numeric_version=allow_numeric_version,
    ):
        raise ValueError("mDNS display text contains transport material")
    if _CREDENTIAL_RE.search(value):
        raise ValueError("mDNS display text contains credential material")


def _contains_transport_material(
    value: str,
    *,
    allowed_dotted_value: str | None,
    allow_numeric_version: bool,
) -> bool:
    if _URL_RE.search(value) or _USERINFO_RE.search(value) or _HOST_PORT_RE.search(value):
        return True
    if allow_numeric_version and _is_allowed_numeric_version(value):
        return False
    if _contains_ip_literal(value):
        return True
    if value == allowed_dotted_value:
        return False
    return any(
        match.group().casefold() not in _PROSE_DOTTED_ABBREVIATIONS
        for match in _HOSTNAME_RE.finditer(value)
    )


def _is_allowed_numeric_version(value: str) -> bool:
    match = _NUMERIC_VERSION_RE.fullmatch(value)
    if match is None:
        return False
    release = match.group("release")
    try:
        possible_address = ipaddress.IPv4Address(release)
    except ValueError:
        return True
    return possible_address.is_global and not possible_address.is_multicast


def _contains_ip_literal(value: str) -> bool:
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
    return False


def _normalize_address(raw_address: object, binding: MdnsBinding) -> str:
    if (
        type(raw_address) is not str
        or len(raw_address) > _MAX_ADDRESS_LITERAL_CHARS
        or raw_address != raw_address.strip()
    ):
        raise ValueError("mDNS address must be a canonical literal")
    literal, separator, zone = raw_address.partition("%")
    try:
        parsed = ipaddress.ip_address(literal)
    except ValueError as exc:
        raise ValueError("mDNS address must be a literal IP address") from exc

    if isinstance(parsed, ipaddress.IPv4Address):
        if separator:
            raise ValueError("IPv4 mDNS addresses cannot have a zone")
    elif parsed.is_link_local:
        expected_zone = binding.ipv6_interface_index
        if (
            not separator
            or expected_zone is None
            or not zone.isascii()
            or not zone.isdecimal()
            or int(zone) != expected_zone
            or zone != str(expected_zone)
        ):
            raise ValueError("IPv6 link-local address requires the selected numeric zone")
    elif separator:
        raise ValueError("non-link-local IPv6 addresses cannot have a zone")
    return str(parsed)


def _reconcile(first: LanCandidate, second: LanCandidate) -> LanCandidate:
    metadata: dict[str, str] = {}
    first_metadata = dict(first._metadata_items)
    second_metadata = dict(second._metadata_items)
    for key in sorted(first_metadata.keys() | second_metadata.keys()):
        values = [value for value in (first_metadata.get(key), second_metadata.get(key)) if value]
        metadata[key] = min(values)
    return LanCandidate._from_normalized(
        interface_id=first.interface_id,
        address=first.address,
        port=first.port,
        service_type=min(first.service_type, second.service_type),
        instance_name=min(first.instance_name, second.instance_name),
        metadata=metadata,
    )


def _endpoint_sort_key(address: str, port: int) -> tuple[int, int, int]:
    parsed = ipaddress.ip_address(address)
    return (parsed.version, int(parsed), port)


class _LiveListener:
    def __init__(
        self,
        callback: Callable[[MdnsRecord], None],
        binding: MdnsBinding,
    ) -> None:
        self._callback = callback
        self._binding = binding

    def add_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._emit_cached(zeroconf, service_type, name)

    def update_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._emit_cached(zeroconf, service_type, name)

    def remove_service(self, _zeroconf: Any, _service_type: str, _name: str) -> None:
        return

    def _emit_cached(self, zeroconf: Any, service_type: str, name: str) -> None:
        try:
            now_millis = _current_time_millis()
            record = _cached_mdns_record(
                zeroconf,
                service_type,
                name,
                self._binding,
                now_millis,
            )
            if record is not None:
                self._callback(record)
        except Exception:
            # Optional adapter/cache drift is isolated to one callback.
            return


class _LiveMdnsSession:
    def __init__(self, zeroconf: Any, browser: Any) -> None:
        self._zeroconf = zeroconf
        self._browser = browser
        self._wait_event = threading.Event()
        self._close_lock = threading.Lock()
        self._cleanup_done = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        self._cleanup_error: Exception | None = None

    def wait(self, seconds: float) -> None:
        bounded = min(max(0.0, seconds), LanScanLimits().mdns_window_seconds)
        self._wait_event.wait(bounded)

    def close(self) -> None:
        with self._close_lock:
            if self._cleanup_thread is None:
                self._cleanup_thread = threading.Thread(
                    target=self._cleanup,
                    name="kestrel-mdns-cleanup",
                    daemon=True,
                )
                self._cleanup_thread.start()
        if not self._cleanup_done.wait(_LIVE_CLEANUP_SECONDS):
            raise TimeoutError("live mDNS cleanup did not settle within its bounded window")
        if self._cleanup_error is not None:
            raise self._cleanup_error

    def _cleanup(self) -> None:
        try:
            _close_live_resources(self._browser, self._zeroconf)
        except Exception as exc:
            self._cleanup_error = exc
        finally:
            self._cleanup_done.set()


def _live_adapter_factory(
    binding: MdnsBinding,
    service_types: tuple[str, ...],
    callback: Callable[[MdnsRecord], None],
) -> MdnsSession:
    if service_types != ALLOWED_MODEL_SERVICE_TYPES:
        raise ValueError("live mDNS adapter requires the exact service allowlist")
    interfaces: list[str | int] = [*binding.ipv4_addresses]
    if binding.ipv6_interface_index is not None:
        interfaces.append(binding.ipv6_interface_index)
    if not interfaces:
        raise ValueError("live mDNS adapter requires an exact interface binding")

    zeroconf: Any | None = None
    browser: Any | None = None
    try:
        module = importlib.import_module("zeroconf")
        if binding.ipv4_addresses and binding.ipv6_interface_index is not None:
            ip_version = module.IPVersion.All
        elif binding.ipv4_addresses:
            ip_version = module.IPVersion.V4Only
        else:
            ip_version = module.IPVersion.V6Only
        zeroconf = module.Zeroconf(interfaces=interfaces, ip_version=ip_version)
        listener = _LiveListener(callback, binding)
        browser = module.ServiceBrowser(zeroconf, list(service_types), listener)
        return _LiveMdnsSession(zeroconf, browser)
    except Exception as creation_error:
        if browser is not None or zeroconf is not None:
            try:
                _LiveMdnsSession(zeroconf, browser).close()
            except Exception as cleanup_error:
                creation_error.add_note(f"bounded mDNS cleanup also failed: {cleanup_error}")
        raise


def _cached_mdns_record(
    zeroconf: Any,
    service_type: object,
    name: object,
    binding: MdnsBinding,
    now_millis: float,
) -> MdnsRecord | None:
    if type(service_type) is not str or service_type not in ALLOWED_MODEL_SERVICE_TYPES:
        raise ValueError("live mDNS callback service type is not allowed")
    if type(name) is not str or not name or len(name.encode("utf-8")) > (
        _MAX_INSTANCE_NAME_BYTES + len(service_type.encode("utf-8")) + 1
    ):
        raise ValueError("live mDNS callback name is invalid")

    service_records = _bounded_cache_records(
        zeroconf,
        name,
        limit=_MAX_LIVE_SERVICE_RECORDS,
    )
    service_values = [
        record
        for record in service_records
        if getattr(record, "class_", None) == _DNS_CLASS_IN
        and getattr(record, "type", None) in {_DNS_TYPE_SRV, _DNS_TYPE_TXT}
        and _cache_record_is_fresh(record, now_millis)
    ]
    srv_records = [
        record for record in service_values if getattr(record, "type", None) == _DNS_TYPE_SRV
    ]
    txt_records = [
        record for record in service_values if getattr(record, "type", None) == _DNS_TYPE_TXT
    ]
    if len(srv_records) != 1 or len(txt_records) != 1:
        return None

    srv_record = srv_records[0]
    server = getattr(srv_record, "server", None)
    if (
        type(server) is not str
        or not server
        or len(server.encode("utf-8")) > _MAX_INSTANCE_NAME_BYTES
        or any(unicodedata.category(character).startswith("C") for character in server)
    ):
        raise ValueError("live mDNS SRV target is invalid")

    properties = _parse_raw_txt(getattr(txt_records[0], "text", None))

    address_records = _bounded_cache_records(
        zeroconf,
        server,
        limit=MAX_ACTIVE_HOSTS,
    )
    addresses: list[object] = []
    for address_record in address_records:
        if (
            getattr(address_record, "class_", None) != _DNS_CLASS_IN
            or getattr(address_record, "type", None) not in {_DNS_TYPE_A, _DNS_TYPE_AAAA}
        ):
            continue
        if not _cache_record_is_fresh(address_record, now_millis):
            continue
        raw_address = getattr(address_record, "address", None)
        record_type = getattr(address_record, "type", None)
        expected_length = 4 if record_type == _DNS_TYPE_A else 16
        if type(raw_address) is not bytes or len(raw_address) != expected_length:
            addresses.append(raw_address)
            continue
        parsed = ipaddress.ip_address(raw_address)
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.is_link_local:
            scope_id = getattr(address_record, "scope_id", None)
            if isinstance(scope_id, bool) or not isinstance(scope_id, int) or scope_id <= 0:
                addresses.append(str(parsed))
            else:
                addresses.append(f"{parsed}%{scope_id}")
        else:
            addresses.append(str(parsed))

    if not addresses:
        return None

    return MdnsRecord(
        service_type=service_type,
        instance_name=name,
        addresses=tuple(addresses),
        port=getattr(srv_record, "port", None),
        properties=properties,
        # The cache target is retained only as ignored display-hostile input.
        hostname=server,
    )


def _current_time_millis() -> float:
    """Return the monotonic millisecond domain used by pinned zeroconf records."""

    return time.monotonic() * 1000


def _cache_record_is_fresh(record: Any, now_millis: float) -> bool:
    is_expired = getattr(record, "is_expired", None)
    if not callable(is_expired):
        raise ValueError("live mDNS cache record has no expiry predicate")
    expired = is_expired(now_millis)
    if type(expired) is not bool:
        raise ValueError("live mDNS cache record expiry predicate is malformed")
    return not expired


def _bounded_cache_records(
    zeroconf: Any,
    key: str,
    *,
    limit: int,
) -> tuple[Any, ...]:
    cache_owner = getattr(zeroconf, "cache", None)
    raw_cache = getattr(cache_owner, "cache", None)
    if not isinstance(raw_cache, Mapping):
        return ()
    bucket = raw_cache.get(key.lower())
    if bucket is None:
        return ()
    if not isinstance(bucket, Mapping):
        raise ValueError("live mDNS cache bucket is not a mapping")
    if len(bucket) > limit:
        raise ValueError("live mDNS cache bucket exceeds its record limit")

    records: list[Any] = []
    for cached_record in bucket.values():
        if len(records) >= limit:
            raise ValueError("live mDNS cache bucket changed beyond its record limit")
        records.append(cached_record)
    return tuple(records)


def _parse_raw_txt(raw_txt: object) -> Mapping[object, object]:
    if type(raw_txt) is not bytes or len(raw_txt) > MAX_MDNS_METADATA_BYTES:
        raise ValueError("live mDNS TXT payload exceeds its raw byte limit")

    properties: dict[object, object] = {}
    offset = 0
    while offset < len(raw_txt):
        entry_length = raw_txt[offset]
        offset += 1
        end = offset + entry_length
        if entry_length == 0 or end > len(raw_txt):
            raise ValueError("live mDNS TXT payload has invalid framing")
        entry = raw_txt[offset:end]
        offset = end
        if len(properties) >= len(_DISPLAY_TXT_FIELDS):
            raise ValueError("live mDNS TXT payload contains too many entries")
        raw_key, separator, raw_value = entry.partition(b"=")
        if (
            not separator
            or not raw_key
            or len(raw_key) > _MAX_TXT_KEY_BYTES
            or len(raw_value) > _MAX_DISPLAY_VALUE_BYTES
            or raw_key in properties
        ):
            raise ValueError("live mDNS TXT key or value is invalid")
        properties[raw_key] = raw_value
    return properties


def _close_live_resources(browser: Any | None, zeroconf: Any | None) -> None:
    first_error: Exception | None = None
    if browser is not None:
        try:
            _stop_pinned_browser(browser, zeroconf)
        except Exception as exc:
            first_error = exc
    if zeroconf is not None:
        try:
            zeroconf.close()
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _stop_pinned_browser(browser: Any, zeroconf: Any | None) -> None:
    """Request pinned zeroconf shutdown without its unbounded cancel()."""

    first_error: Exception | None = None
    try:
        queue = browser.queue
        queue.put(None)
    except Exception as exc:
        first_error = exc

    try:
        async_cancel = browser._async_cancel
        if not callable(async_cancel):
            raise TypeError("pinned zeroconf browser has no async cancel callback")
        browser_zeroconf = getattr(browser, "zc", None)
        loop_owner = browser_zeroconf if browser_zeroconf is not None else zeroconf
        if loop_owner is None:
            raise TypeError("pinned zeroconf browser has no event-loop owner")
        loop = loop_owner.loop
        is_closed = loop.is_closed
        if not callable(is_closed) or not is_closed():
            loop.call_soon_threadsafe(async_cancel)
    except Exception as exc:
        if first_error is None:
            first_error = exc

    try:
        join = browser.join
        if not callable(join):
            raise TypeError("pinned zeroconf browser has no bounded join")
        join(timeout=_LIVE_BROWSER_JOIN_SECONDS)
        is_alive = browser.is_alive
        if not callable(is_alive) or is_alive():
            raise TimeoutError("live mDNS browser did not stop within its bounded join")
    except Exception as exc:
        if first_error is None:
            first_error = exc

    if first_error is not None:
        raise first_error
