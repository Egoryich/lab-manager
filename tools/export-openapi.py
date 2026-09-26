"""Generate the public contract without connecting to infrastructure."""

import json
from pathlib import Path

from cryptography.fernet import Fernet
from lab_manager.config import Settings
from lab_manager.main import create_app

app = create_app(
    Settings(
        _env_file=None,
        environment="test",
        public_origin="http://localhost:5173",
        database_url="postgresql+psycopg://unused:unused@localhost/unused",
        redis_url="redis://localhost/0",
        encryption_key=Fernet.generate_key().decode(),
        digest_key="export-only-not-used-for-runtime-" * 3,
    )
)
Path("packages/shared-types/openapi.json").write_text(
    json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
