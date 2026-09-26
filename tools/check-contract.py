"""Fail CI if generated OpenAPI no longer describes the executable API."""

import json
from pathlib import Path

from lab_manager.main import create_app

actual = create_app().openapi()
expected = json.loads(Path("packages/shared-types/openapi.json").read_text(encoding="utf-8"))
if actual != expected:
    raise SystemExit("OpenAPI drift: run python tools/export-openapi.py && npm run generate")
print("OpenAPI matches the executable API.")
