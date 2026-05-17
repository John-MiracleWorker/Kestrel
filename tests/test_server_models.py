from nested_memvid_agent.server_models import (
    CreateRunRequest,
    SecretStoreRequest,
    SelfRememberRequest,
)


def _model_payload(model: object) -> dict[str, object]:
    model_dump = getattr(model, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(by_alias=True))
    return dict(model.dict(by_alias=True))  # type: ignore[attr-defined]


def test_server_request_models_preserve_defaults_and_aliases() -> None:
    create_run = CreateRunRequest(message="hello")
    assert create_run.autonomy_mode == "background"

    secret = SecretStoreRequest(name="api", value="secret", validate=True)
    assert secret.validate_now is True

    self_memory = SelfRememberRequest(
        title="identity",
        content="Kestrel prefers validated self memory.",
        schema="identity_summary",
        validation_status="validated",
    )
    assert self_memory.schema_ == "identity_summary"
    assert _model_payload(self_memory)["schema"] == "identity_summary"
