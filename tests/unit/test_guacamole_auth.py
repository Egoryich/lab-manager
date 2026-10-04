import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from lab_manager.guacamole_auth import (
    GuacamoleGrantError,
    JoinConnection,
    SSHConnection,
    issue_ssh_grant,
)

SECRET = "00112233445566778899aabbccddeeff"


def decode_grant(value):
    key = bytes.fromhex(SECRET)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).decryptor()
    padded = decryptor.update(base64.b64decode(value)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    signed = unpadder.update(padded) + unpadder.finalize()
    digest, plaintext = signed[:32], signed[32:]
    assert hmac.compare_digest(digest, hmac.new(key, plaintext, hashlib.sha256).digest())
    return json.loads(plaintext)


def test_short_lived_ssh_grant_has_only_the_named_guest_connection():
    actor_id, runtime_id = uuid.uuid4(), uuid.uuid4()
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    connection = SSHConnection(
        runtime_id=runtime_id,
        address="10.70.1.2",
        subnet="10.70.1.0/30",
        username="student",
        private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n"
        "-----END OPENSSH PRIVATE KEY-----\n",
        host_key="ssh-ed25519 " + "A" * 44,
    )
    grant = issue_ssh_grant(SECRET, actor_id=actor_id, connection=connection, now=now)
    decoded = decode_grant(grant)
    assert decoded["username"] == f"u-{actor_id.hex}"
    assert decoded["expires"] == int((now + timedelta(seconds=30)).timestamp() * 1000)
    assert decoded["connections"] == {"Терминал": connection.payload()}
    assert decoded["connections"]["Терминал"]["parameters"]["hostname"] == "10.70.1.2"


def test_teacher_can_receive_interactive_join_grant():
    runtime_id = uuid.uuid4()
    grant = issue_ssh_grant(
        SECRET,
        actor_id=uuid.uuid4(),
        connection=JoinConnection(runtime_id=runtime_id),
        now=datetime.now(UTC),
    )
    assert decode_grant(grant)["connections"]["Совместный просмотр"] == {
        "join": str(runtime_id),
        "parameters": {"read-only": "false"},
    }


@pytest.mark.parametrize(
    "address,subnet",
    [
        ("192.168.0.123", "10.70.1.0/30"),
        ("127.0.0.1", "127.0.0.0/30"),
        ("10.70.1.0", "10.70.1.0/30"),
    ],
)
def test_guest_address_must_be_usable_within_its_lab_subnet(address, subnet):
    with pytest.raises(GuacamoleGrantError):
        SSHConnection(
            runtime_id=uuid.uuid4(),
            address=address,
            subnet=subnet,
            username="student",
            private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n"
            "-----END OPENSSH PRIVATE KEY-----",
            host_key="ssh-ed25519 " + "A" * 44,
        ).payload()


def test_grant_rejects_long_lifetime_and_invalid_secret():
    now = datetime.now(UTC)
    connection = JoinConnection(runtime_id=uuid.uuid4())
    with pytest.raises(GuacamoleGrantError, match="INVALID_GRANT_REQUEST"):
        issue_ssh_grant(
            SECRET,
            actor_id=uuid.uuid4(),
            connection=connection,
            now=now,
            lifetime=timedelta(minutes=10),
        )
    with pytest.raises(GuacamoleGrantError, match="INVALID_GUACAMOLE_SECRET"):
        issue_ssh_grant("short", actor_id=uuid.uuid4(), connection=connection, now=now)
