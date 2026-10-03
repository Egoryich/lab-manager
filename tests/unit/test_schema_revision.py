from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from lab_manager.schema import CURRENT_SCHEMA_REVISION


def test_api_and_worker_revision_matches_unique_migration_head():
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [CURRENT_SCHEMA_REVISION]
