import uuid

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from lab_manager.runtime_ssh import new_credential
from lab_manager.security import SecretCodec


def test_new_credential_is_encrypted_and_matches_public_key():
    codec = SecretCodec("digest" * 12, Fernet.generate_key().decode())
    credential = new_credential(uuid.uuid4(), codec)
    assert "BEGIN OPENSSH PRIVATE KEY" not in credential.private_key_ciphertext
    private = codec.decrypt(credential.private_key_ciphertext)
    loaded = serialization.load_ssh_private_key(private.encode(), password=None)
    public = (
        loaded.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    assert public == credential.public_key
    assert credential.host_key is None
