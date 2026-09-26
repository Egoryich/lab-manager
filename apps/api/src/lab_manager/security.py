import hashlib
import hmac
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet
from pwdlib import PasswordHash

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_HASH = PasswordHash.recommended()
# Equal-cost verification for unknown users; initialized once per process.
DUMMY_HASH = PASSWORD_HASH.hash(secrets.token_urlsafe(32))


@dataclass(frozen=True)
class SecretCodec:
    digest_key: str
    encryption_key: str

    def digest(self, purpose: str, value: str) -> str:
        return hmac.new(
            self.digest_key.encode(), f"{purpose}:{value}".encode(), hashlib.sha256
        ).hexdigest()

    def encrypt(self, value: str) -> str:
        return Fernet(self.encryption_key.encode()).encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return Fernet(self.encryption_key.encode()).decrypt(value.encode()).decode()


def new_join_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(6))
