import pytest


@pytest.fixture(autouse=True)
def raven_home(tmp_path, monkeypatch):
    """Keep settings and certificates out of the real config directory."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "raven-home"))
    return tmp_path / "raven-home"
