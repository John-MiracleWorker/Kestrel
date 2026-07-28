from __future__ import annotations

import base64
import json
import math
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_REQUEST_SCHEMA = "kestrel.provider_http_request.v1"
_RESPONSE_SCHEMA = "kestrel.provider_http_response.v1"
_READ_CHUNK_BYTES = 16 * 1024


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _validated_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("provider transport URL must be a string")
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("provider transport URL is malformed") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("provider transport URL must use HTTP or HTTPS")
    if not parsed.netloc or not hostname:
        raise ValueError("provider transport URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("provider transport URL must not embed credentials")
    return candidate


def _positive_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _positive_timeout(payload: dict[str, object]) -> float:
    value = payload.get("timeout_seconds")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("timeout_seconds must be a positive finite number")
    return float(value)


def _headers(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError("provider transport headers must be a list")
    headers: dict[str, str] = {}
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise ValueError("provider transport header entries must be string pairs")
        name, header_value = item
        if "\r" in name or "\n" in name or "\r" in header_value or "\n" in header_value:
            raise ValueError("provider transport headers must not contain newlines")
        headers[name] = header_value
    return headers


def _optional_body(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("provider transport body must be base64 text")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("provider transport body is not valid base64") from exc


def _read_bounded(response: Any, *, max_bytes: int) -> bytes:
    reader = getattr(response, "read1", None)
    if not callable(reader):
        response_fp = getattr(response, "fp", None)
        reader = getattr(response_fp, "read1", None)
    if not callable(reader):
        reader = getattr(response, "read", None)
    if not callable(reader):
        raise ValueError("provider response is not readable")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = reader(min(_READ_CHUNK_BYTES, max_bytes + 1 - total))
        if not isinstance(chunk, bytes):
            raise ValueError("provider response reader returned non-bytes content")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("provider response exceeded the byte limit")


def _encoded_body(body: bytes) -> str:
    return base64.b64encode(body).decode("ascii")


def _response(kind: str, **payload: object) -> dict[str, object]:
    return {
        "schema": _RESPONSE_SCHEMA,
        "kind": kind,
        **payload,
    }


def _exchange(payload: dict[str, object]) -> dict[str, object]:
    if payload.get("schema") != _REQUEST_SCHEMA:
        raise ValueError("unsupported provider transport request schema")
    url = _validated_url(payload.get("url"))
    method = payload.get("method")
    if method not in {"GET", "POST"}:
        raise ValueError("provider transport method must be GET or POST")
    timeout_seconds = _positive_timeout(payload)
    max_bytes = _positive_int(payload, "max_bytes")
    error_max_bytes = _positive_int(payload, "error_max_bytes")
    request = Request(
        url,
        data=_optional_body(payload.get("body_base64")),
        headers=_headers(payload.get("headers")),
        method=method,
    )
    opener = build_opener(_RejectRedirectHandler())
    try:
        response = opener.open(request, timeout=timeout_seconds)  # nosec B310
        try:
            body = _read_bounded(response, max_bytes=max_bytes)
        finally:
            response.close()
        return _response("ok", body_base64=_encoded_body(body))
    except HTTPError as exc:
        try:
            detail = _read_bounded(exc, max_bytes=error_max_bytes)
        except Exception:  # noqa: BLE001 - status diagnostics are best effort
            detail = b"response detail unavailable"
        finally:
            exc.close()
        return _response(
            "http_error",
            status_code=int(exc.code),
            body_base64=_encoded_body(detail),
        )
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            return _response("timeout")
        return _response("url_error", detail=str(reason))
    except TimeoutError:
        return _response("timeout")
    except ValueError as exc:
        return _response("value_error", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - isolated boundary returns typed failure
        return _response(
            "transport_error",
            error_type=type(exc).__name__,
            detail=str(exc),
        )


def main() -> int:
    try:
        raw_request = sys.stdin.buffer.read()
        parsed = json.loads(raw_request.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("provider transport request must be a JSON object")
        response = _exchange(parsed)
    except Exception as exc:  # noqa: BLE001 - never print request or credentials
        response = _response(
            "protocol_error",
            error_type=type(exc).__name__,
            detail="isolated provider transport rejected its request",
        )
    encoded = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
