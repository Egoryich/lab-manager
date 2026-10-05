"""Generate one SSH identity per persistent LXC runtime."""

import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from lab_manager.runtime_models import RuntimeSshCredential
from lab_manager.security import SecretCodec


def new_credential(runtime_id: uuid.UUID, codec: SecretCodec) -> RuntimeSshCredential:
    if not isinstance(runtime_id, uuid.UUID) or runtime_id.int == 0:
        raise ValueError("INVALID_RUNTIME_ID")
    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode("ascii")
    )
    return RuntimeSshCredential(
        runtime_id=runtime_id,
        public_key=public,
        private_key_ciphertext=codec.encrypt(private),
        host_key=None,
    )
