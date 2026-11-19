import pytest

from tradedesk.config import Settings
from tradedesk.db import database as db


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # Isolate from any developer .env
    monkeypatch.chdir(tmp_path)
    return Settings(_env_file=None, tradedesk_db_path=str(tmp_path / "api.db"),
                    tradedesk_risk_limits_file=str(__import__("tradedesk.config", fromlist=["x"]).PROJECT_ROOT
                                                   / "config" / "risk_limits.toml"))
