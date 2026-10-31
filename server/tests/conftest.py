import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Fresh app + empty SQLite DB per test. Davis uses its rules (no model
    download or load in tests) unless a test says otherwise."""
    monkeypatch.setenv("DAVIS_PROVIDER", os.environ.get("DAVIS_PROVIDER", "rules"))
    monkeypatch.setenv("DAVIS_LOCAL_PRELOAD", "0")
    import app.config as config
    import app.database as database
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "karlon.db")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "karlon.db")
    (tmp_path / "profile_pics").mkdir()
    monkeypatch.setattr(config, "PROFILE_PICS_DIR", tmp_path / "profile_pics")
    for mod in [m for m in list(sys.modules) if m == "app.main" or m.startswith(("app.routers", "app.davis"))]:
        del sys.modules[mod]
    main = importlib.import_module("app.main")   # runs init_db() against the temp DB
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def db(client):
    from app.database import get_db
    conn = get_db()
    yield conn
    conn.close()
