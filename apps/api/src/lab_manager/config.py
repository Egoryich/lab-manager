import re
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
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
    node_config: str | None = None
    guacamole_json_secret: SecretStr | None = None
    guacamole_broker_enabled: bool = False

    @field_validator("guacamole_json_secret", mode="before")
    @classmethod
    def blank_guacamole_secret_is_unconfigured(cls, value):
        return None if value == "" else value

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
        if self.guacamole_json_secret is not None and not re.fullmatch(
            r"[0-9a-fA-F]{32}", self.guacamole_json_secret.get_secret_value()
        ):
            raise ValueError("guacamole_json_secret must be 16 random bytes in hex")
        return self

    @property
    def cookie_name(self) -> str:
        return "__Host-lab_session" if self.environment == "production" else "lab_session"
