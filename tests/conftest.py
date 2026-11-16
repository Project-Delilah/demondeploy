import pytest


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DEMON_UNIT_DIR", str(tmp_path / "units"))
    yield tmp_path
