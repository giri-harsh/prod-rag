"""Import every module under app/ with dummy env vars (P0-3: boot correctness)."""
import importlib
import os
import pkgutil

import pytest

DUMMY = {
    "APP_ENV": "test", "DATABASE_URL": "postgresql://u:p@localhost:5432/x", "REDIS_URL": "redis://localhost:6379/0",
    "QDRANT_URL": ":memory:", "GROQ_API_KEY": "x", "PORTKEY_API_KEY": "x", "GEMINI_API_KEY": "x",
}


def _modules():
    import app

    return [m.name for m in pkgutil.walk_packages(app.__path__, "app.")]


@pytest.mark.parametrize("name", _modules())
def test_module_imports(name, monkeypatch):
    for k, v in DUMMY.items():
        monkeypatch.setenv(k, v)
    importlib.import_module(name)
    assert os.environ["APP_ENV"] == "test"
