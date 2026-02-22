"""
User creation and authentication.
"""
import uuid
import logging

from db import get_pool

logger = logging.getLogger("brain.users")


async def create_user(email: str, password: str, display_name: str = "") -> dict:
    """Create a new user with hashed password."""
    import bcrypt
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
    """Verify credentials and return user info."""
    import bcrypt
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, password_hash, salt, display_name FROM users WHERE email = $1",
        email,
    )
    if not row:
        raise ValueError("User not found")

    if row["password_hash"].startswith("$2"):
        valid = bcrypt.checkpw(password.encode(), row["password_hash"].encode())
    else:
        # Fallback to legacy SHA-256 for transition
        import hashlib
        old_hash = hashlib.sha256((password + row["salt"]).encode()).hexdigest()
        valid = (old_hash == row["password_hash"])

    if not valid:
        raise ValueError("Invalid password")

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
