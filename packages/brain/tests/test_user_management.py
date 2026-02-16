"""
Tests for Brain service user management functions.
These test the standalone functions (create_user, authenticate_user)
with a mocked asyncpg pool.
"""

import hashlib
import secrets
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


# ── Helpers ───────────────────────────────────────────────────────────

def _hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((password + salt).encode()).hexdigest()


# ── Test create_user ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_user_returns_user_dict():
    """create_user should return a dict with id, email, and displayName."""
    mock_pool = AsyncMock()
    mock_pool.execute = AsyncMock()

    with patch("server.get_pool", return_value=mock_pool), \
         patch("server.uuid.uuid4", return_value=uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")):

        from server import create_user
        result = await create_user("alice@example.com", "hunter2", "Alice")

        assert result["email"] == "alice@example.com"
        assert result["id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert result["displayName"] == "Alice"
        mock_pool.execute.assert_called_once()


@pytest.mark.asyncio
async def test_create_user_uses_email_prefix_as_default_name():
    """When display_name is empty, create_user should default to email prefix."""
    mock_pool = AsyncMock()
    mock_pool.execute = AsyncMock()

    with patch("server.get_pool", return_value=mock_pool):
        from server import create_user
        result = await create_user("bob@work.co", "secret", "")

        # The default display name should be the email prefix
        call_args = mock_pool.execute.call_args[0]
        # 5th positional arg to execute is display_name
        assert call_args[4] == "bob"


# ── Test authenticate_user ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_authenticate_user_success():
    """Valid credentials should return user info with workspace memberships."""
    salt = "testsalt1234"
    pw_hash = _hash_pw("correct-password", salt)

    user_row = {
        "id": "user-42",
        "email": "alice@example.com",
        "password_hash": pw_hash,
        "salt": salt,
        "display_name": "Alice",
    }
    membership_rows = [
        {"id": "ws-1", "role": "owner"},
        {"id": "ws-2", "role": "member"},
    ]

    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value=user_row)
    mock_pool.fetch = AsyncMock(return_value=membership_rows)

    with patch("server.get_pool", return_value=mock_pool):
        from server import authenticate_user
        result = await authenticate_user("alice@example.com", "correct-password")

    assert result["id"] == "user-42"
    assert result["email"] == "alice@example.com"
    assert len(result["workspaces"]) == 2
    assert result["workspaces"][0]["role"] == "owner"


@pytest.mark.asyncio
async def test_authenticate_user_wrong_password():
    """Wrong password should raise ValueError."""
    salt = "somesalt"
    pw_hash = _hash_pw("correct-password", salt)

    user_row = {
        "id": "user-1",
        "email": "test@test.com",
        "password_hash": pw_hash,
        "salt": salt,
        "display_name": "Test",
    }

    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value=user_row)

    with patch("server.get_pool", return_value=mock_pool):
        from server import authenticate_user
        with pytest.raises(ValueError, match="Invalid password"):
            await authenticate_user("test@test.com", "wrong-password")


@pytest.mark.asyncio
async def test_authenticate_user_not_found():
    """Non-existent email should raise ValueError."""
    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value=None)

    with patch("server.get_pool", return_value=mock_pool):
        from server import authenticate_user
        with pytest.raises(ValueError, match="User not found"):
            await authenticate_user("nobody@example.com", "any")
