from nested_memvid_agent.server_secret_routes import register_secret_routes


def test_secret_route_registration_is_extracted() -> None:
    assert callable(register_secret_routes)
