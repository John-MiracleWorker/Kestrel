import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("brain.encryption")

_fernet = None


def get_fernet() -> Fernet:
    """Return a Fernet cipher keyed from the ENCRYPTION_KEY env var.

    In production (NODE_ENV=production) the key MUST be provided.
    In development a deterministic dev-only key is used so that local
    databases remain readable across restarts, but a warning is logged.
    """
    global _fernet
    if _fernet is None:
        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            if os.getenv("NODE_ENV", "development") == "production":
                raise RuntimeError(
                    "ENCRYPTION_KEY must be set in production. "
                    "Generate one with: python -c "
                    "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
                )
            # Dev-only fallback — NEVER used in production
            key = Fernet.generate_key().decode()
            logger.warning(
                "ENCRYPTION_KEY not set — using ephemeral key. "
                "Data encrypted in this session will NOT be decryptable after restart. "
                "Set ENCRYPTION_KEY in your .env file."
            )
        _fernet = Fernet(key if isinstance(key, bytes) else key.encode())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns empty string for empty input."""
    if not plaintext:
        return ""
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a ciphertext string.

    Raises InvalidToken on decryption failure instead of silently
    returning empty — callers must handle the error explicitly.
    """
    if not ciphertext:
        return ""
    if ciphertext.startswith("provider_key:"):
        # Legacy unencrypted value — log a warning so it gets noticed
        logger.warning(
            "Encountered unencrypted provider key (provider_key: prefix). "
            "Re-save the provider config to encrypt it."
        )
        return ciphertext
    try:
        return get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error(
            "Decryption failed — the ENCRYPTION_KEY may have changed or "
            "the ciphertext is corrupt. "
            "The stored credential will need to be re-entered."
        )
        raise
