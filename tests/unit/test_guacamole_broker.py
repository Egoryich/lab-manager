import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from lab_manager.config import Settings
from lab_manager.guacamole_broker import valid_gateway_signature


def test_gateway_signature_binds_timestamp_and_nonce():
    key = "ab" * 32
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    timestamp = str(int(now.timestamp()))
    nonce = "n" * 43
    signature = hmac.new(
        bytes.fromhex(key), f"{timestamp}:{nonce}".encode(), hashlib.sha256
    ).hexdigest()

    assert valid_gateway_signature(key, timestamp, nonce, signature, now)
    assert not valid_gateway_signature(key, timestamp, "x" * 43, signature, now)
    assert not valid_gateway_signature(key, timestamp, nonce, "0" * 64, now)
    assert not valid_gateway_signature(
        key, timestamp, nonce, signature, now + timedelta(seconds=31)
    )
    assert not valid_gateway_signature(key, "invalid", nonce, signature, now)


def test_enabled_gateway_requires_32_byte_secret():
    values = dict(
        _env_file=None,
        environment="test",
        database_url="postgresql+psycopg://lab:pass@localhost/lab",
        redis_url="redis://localhost:6379/0",
        public_origin="http://localhost:5173",
        encryption_key=Fernet.generate_key().decode(),
        digest_key="d" * 64,
        guacamole_broker_enabled=True,
    )
    with pytest.raises(ValueError, match="requires its secret"):
        Settings(**values)
    with pytest.raises(ValueError, match="32 random bytes"):
        Settings(**values, guacamole_broker_secret="short")
    assert Settings(**values, guacamole_broker_secret="ab" * 32).guacamole_broker_enabled
