import os
from cryptography.fernet import Fernet

_fernet = None

def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            key = b'wWQmMvMzgqMsw_2U65BruAQZgX7hA9-6z0oOa_ZFhfA='
            if os.getenv("NODE_ENV", "development") == "production":
                raise RuntimeError("ENCRYPTION_KEY MUST be set in production")
        _fernet = Fernet(key)
    return _fernet

def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return get_fernet().encrypt(plaintext.encode()).decode()

def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    if ciphertext.startswith("provider_key:"):
        return ciphertext
    try:
        return get_fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        # If decryption fails (e.g. key changed), return empty or raw. 
        # Safest is to return empty.
        return ""
