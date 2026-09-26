import pytest
from cryptography.fernet import Fernet
from lab_manager.config import Settings
from lab_manager.schemas import GroupJoin, Register
from lab_manager.security import ALPHABET, SecretCodec, new_join_code
from pydantic import ValidationError


def test_domain_separated_keyed_digests_and_encryption():
    codec = SecretCodec("a" * 64, Fernet.generate_key().decode())
    assert codec.digest("join", "ABC234") != codec.digest("session", "ABC234")
    assert codec.encrypt("ABC234") != "ABC234"
    assert codec.decrypt(codec.encrypt("ABC234")) == "ABC234"
    other = SecretCodec("b" * 64, codec.encryption_key)
    assert other.digest("join", "ABC234") != codec.digest("join", "ABC234")


def test_code_and_identity_validation():
    for _ in range(100):
        code = new_join_code()
        assert len(code) == 6 and set(code) <= set(ALPHABET)
    assert GroupJoin(code="abc234", confirm=True).code == "ABC234"
    with pytest.raises(ValidationError):
        Register(
            username="admin", display_name="Admin", password="long-enough-password", roles=["ADMIN"]
        )
    with pytest.raises(ValidationError):
        Register(username="name", display_name="  ", password="long-enough-password")


@pytest.mark.parametrize(
    "override",
    [
        {"database_url": "sqlite:///lab.db"},
        {"public_origin": "http://example.com"},
        {"public_origin": "https://example.com/path"},
        {"digest_key": "short"},
        {"encryption_key": "invalid"},
    ],
)
def test_production_rejects_unsafe_settings(override):
    values = dict(
        environment="production",
        database_url="postgresql+psycopg://x:x@localhost/lab",
        redis_url="redis://localhost/0",
        public_origin="https://example.com",
        digest_key="a" * 64,
        encryption_key=Fernet.generate_key().decode(),
    )
    with pytest.raises((ValueError, ValidationError)):
        Settings(_env_file=None, **(values | override))
