"""
User creation and authentication.
"""
import hashlib
import hmac
import uuid
import logging

import bcrypt

from db import get_pool

logger = logging.getLogger("brain.users")


async def create_user(email: str, password: str, display_name: str = "") -> dict:
    """Create a new user with bcrypt-hashed password."""
    pool = await get_pool()
    user_id = str(uuid.uuid4())
    salt = ""  # Bcrypt handles salting internally
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    final_display_name = display_name or email.split("@")[0]
    await pool.execute(
        """INSERT INTO users (id, email, password_hash, salt, display_name, created_at)
           VALUES ($1, $2, $3, $4, $5, NOW())""",
        user_id, email, pw_hash, salt, final_display_name,
    )
    return {"id": user_id, "email": email, "displayName": final_display_name}


async def authenticate_user(email: str, password: str) -> dict:
    """Verify credentials and return user info.

    If a legacy SHA-256 hash is found and the password is valid,
    the hash is transparently upgraded to bcrypt in-place.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, password_hash, salt, display_name FROM users WHERE email = $1",
        email,
    )
    if not row:
        raise ValueError("User not found")

    is_bcrypt = row["password_hash"].startswith("$2")

    if is_bcrypt:
        valid = bcrypt.checkpw(password.encode(), row["password_hash"].encode())
    else:
        # Legacy SHA-256 — constant-time comparison to avoid timing attacks
        old_hash = hashlib.sha256((password + row["salt"]).encode()).hexdigest()
        valid = hmac.compare_digest(old_hash, row["password_hash"])

    if not valid:
        raise ValueError("Invalid password")

    # Auto-upgrade legacy SHA-256 hashes to bcrypt on successful login
    if not is_bcrypt:
        new_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        await pool.execute(
            "UPDATE users SET password_hash = $1, salt = '' WHERE id = $2",
            new_hash, row["id"],
        )
        logger.info("Upgraded legacy SHA-256 hash to bcrypt for user %s", row["id"])

    # Fetch workspace memberships
    memberships = await pool.fetch(
        """SELECT w.id, wm.role FROM workspaces w
           JOIN workspace_members wm ON w.id = wm.workspace_id
           WHERE wm.user_id = $1""",
        row["id"],
    )
    workspaces = [{"id": str(m["id"]), "role": m["role"]} for m in memberships]

    return {
        "id": str(row["id"]),
        "email": row["email"],
        "displayName": row["display_name"],
        "workspaces": workspaces,
    }
