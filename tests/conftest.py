import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Every test gets its own ledger/output/log dirs and never touches real providers."""
    from app.service.settings import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "events_csv", str(tmp_path / "events.csv"))
    monkeypatch.setattr(s, "db_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(s, "output_dir", str(tmp_path / "out"))
    monkeypatch.setattr(s, "log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(s, "llm_force_provider", "")
    monkeypatch.setattr(s, "mcp_mode", "inprocess")
    yield
