import json
from typing import Any, cast

from .channels import ChannelPayloadError
from .server_models import ChannelConfigRequest, ChannelIngestRequest
from .server_support import known_secret_env_names, request_headers


async def request_body(request: object) -> bytes:
    body = getattr(request, "body", None)
    if not callable(body):
        raise ValueError("request body is unavailable")
    raw = await body()
    return raw if isinstance(raw, bytes) else bytes(raw)


def parse_json_body(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("JSON body must be an object.")
    return parsed


def channel_public(channel: dict[str, Any], secret_broker: Any) -> dict[str, object]:
    settings = channel.get("settings")
    safe = dict(channel)
    safe["settings"] = dict(settings) if isinstance(settings, dict) else {}
    token_env = str(safe.get("token_env") or "")
    webhook_env = str(safe.get("webhook_url_env") or "")
    signature_env = ""
    if isinstance(settings, dict):
        signature_env = str(settings.get("signature_secret_env") or "")
    safe["env_status"] = {
        "token_env_configured": bool(token_env and secret_broker.resolve(token_env)),
        "token_env_status": secret_broker.status(token_env) if token_env else {"configured": False},
        "webhook_url_env_configured": bool(webhook_env and secret_broker.resolve(webhook_env)),
        "webhook_url_env_status": secret_broker.status(webhook_env)
        if webhook_env
        else {"configured": False},
        "signature_secret_env": signature_env or None,
        "signature_secret_env_configured": bool(
            signature_env and secret_broker.resolve(signature_env)
        ),
        "signature_secret_env_status": secret_broker.status(signature_env)
        if signature_env
        else {"configured": False},
    }
    return safe


def register_channel_routes(
    app: Any,
    *,
    http_exception: Any,
    request_type: type[Any],
    channels: Any,
    secret_broker: Any,
    mcp: Any,
) -> None:
    Request = request_type

    @app.get("/api/channels")  # type: ignore[untyped-decorator]
    def list_channels() -> list[dict[str, object]]:
        return [channel_public(channel, secret_broker) for channel in channels.list_channels()]

    @app.get("/api/channels/{channel_id}")  # type: ignore[untyped-decorator]
    def get_channel(channel_id: str) -> dict[str, object]:
        try:
            return channel_public(channels.get_channel(channel_id), secret_broker)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc

    @app.post("/api/channels")  # type: ignore[untyped-decorator]
    def upsert_channel(request: ChannelConfigRequest) -> dict[str, object]:
        channel = channels.upsert_channel(request.model_dump())
        secret_broker.register_allowed_env_names(
            known_secret_env_names([channel], mcp.list_servers())
        )
        return channel_public(channel, secret_broker)

    @app.put("/api/channels/{channel_id}")  # type: ignore[untyped-decorator]
    def update_channel(channel_id: str, request: ChannelConfigRequest) -> dict[str, object]:
        payload = request.model_dump()
        payload["id"] = channel_id
        channel = channels.upsert_channel(payload)
        secret_broker.register_allowed_env_names(
            known_secret_env_names([channel], mcp.list_servers())
        )
        return channel_public(channel, secret_broker)

    @app.delete("/api/channels/{channel_id}")  # type: ignore[untyped-decorator]
    def delete_channel(channel_id: str) -> dict[str, bool]:
        try:
            channels.delete_channel(channel_id)
            return {"ok": True}
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc

    @app.post("/api/channels/ingest")  # type: ignore[untyped-decorator]
    async def ingest_channel(http_request: Request) -> dict[str, object]:  # type: ignore[valid-type]
        try:
            raw = await request_body(http_request)
            body = parse_json_body(raw)
            request = ChannelIngestRequest(**body)
            return cast(
                dict[str, object],
                channels.handle_payload(
                    provider=request.provider,
                    channel_id=request.channel_id,
                    payload=request.payload,
                    raw_body=raw,
                    send=request.send,
                    headers=request_headers(http_request),
                ).to_public_dict(),
            )
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc
        except ChannelPayloadError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.post("/api/channels/{provider}/webhook")  # type: ignore[untyped-decorator]
    async def channel_webhook(
        provider: str,
        request: Request,  # type: ignore[valid-type]
        channel_id: str | None = None,
        send: bool | None = None,
    ) -> dict[str, object]:
        try:
            raw = await request_body(request)
            payload = parse_json_body(raw)
            return cast(
                dict[str, object],
                channels.handle_payload(
                    provider=provider,
                    channel_id=channel_id,
                    payload=payload,
                    raw_body=raw,
                    send=send,
                    headers=request_headers(request),
                    require_signature=True,
                ).to_public_dict(),
            )
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc
        except ChannelPayloadError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc
