from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAB_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "production"
    database_url: SecretStr
    redis_url: SecretStr
    encryption_key: SecretStr
    digest_key: SecretStr
    public_origin: str
    session_hours: int = 12
    password_hash_concurrency: int = Field(default=2, ge=1, le=4)

    @model_validator(mode="after")
    def validate_security(self):
        if not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("PostgreSQL with psycopg is required")
        Fernet(self.encryption_key.get_secret_value().encode())
        if len(self.digest_key.get_secret_value()) < 64:
            raise ValueError("digest_key must contain at least 64 characters")
        origin = urlsplit(self.public_origin)
        if origin.scheme not in {"http", "https"} or not origin.netloc:
            raise ValueError("public_origin must be an absolute HTTP(S) origin")
        if origin.path or origin.query or origin.fragment or origin.username:
            raise ValueError("public_origin must not contain path, credentials, or query")
        if self.environment == "production" and origin.scheme != "https":
            raise ValueError("production requires HTTPS")
        if not 1 <= self.session_hours <= 24:
            raise ValueError("session_hours must be between 1 and 24")
        return self

    @property
    def cookie_name(self) -> str:
        return "__Host-lab_session" if self.environment == "production" else "lab_session"
