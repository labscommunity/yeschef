"""Settings persistence and model detection — the pieces that make `up`/`join` work."""

from __future__ import annotations

import pytest

from cascadia_tasks import settings
from cascadia_tasks.agent.detect import DetectedBackend


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CASCADIA_TASKS_HOME", str(tmp_path))
    for var in (
        "CASCADIA_TASKS_HUB",
        "CASCADIA_TASKS_ADMIN_TOKEN",
        "CASCADIA_TASKS_REGISTER_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_settings_round_trip(isolated_home) -> None:
    settings.save(
        settings.Settings(
            hub_url="http://mini.local:8787",
            admin_token="a-token",
            register_token="r-token",
            advertise_url="http://mini.local:8787",
        )
    )
    loaded = settings.load()
    assert loaded.hub_url == "http://mini.local:8787"
    assert loaded.admin_token == "a-token"
    assert loaded.register_token == "r-token"


def test_missing_config_yields_defaults(isolated_home) -> None:
    loaded = settings.load()
    assert loaded.hub_url == "http://localhost:8787"
    assert loaded.admin_token is None


def test_environment_overrides_the_file(isolated_home, monkeypatch) -> None:
    settings.save(settings.Settings(hub_url="http://saved:8787", admin_token="saved"))
    monkeypatch.setenv("CASCADIA_TASKS_HUB", "http://env:9999")
    monkeypatch.setenv("CASCADIA_TASKS_ADMIN_TOKEN", "from-env")
    loaded = settings.load()
    assert loaded.hub_url == "http://env:9999"
    assert loaded.admin_token == "from-env"


def test_config_file_is_not_world_readable(isolated_home) -> None:
    path = settings.save(settings.Settings(admin_token="secret"))
    mode = path.stat().st_mode & 0o777
    assert mode & 0o077 == 0, f"tokens file is group/other-accessible: {oct(mode)}"


def test_best_lan_url_has_the_port() -> None:
    url = settings.best_lan_url(8787)
    assert url.startswith("http://")
    assert url.endswith(":8787")


# ---------------------------------------------------------------- detection


def test_pick_model_prefers_exact_then_substring_then_first() -> None:
    backend = DetectedBackend(
        runtime="ollama",
        base_url="http://localhost:11434/v1",
        models=["qwen3:8b", "llama3:8b", "qwen3:32b"],
    )
    assert backend.pick_model("llama3:8b") == "llama3:8b"  # exact
    assert backend.pick_model("qwen3:32") == "qwen3:32b"  # substring
    assert backend.pick_model(None) == "qwen3:8b"  # first
    assert backend.pick_model("nonexistent") == "qwen3:8b"  # falls back to first


def test_pick_model_without_a_list_keeps_the_preference() -> None:
    backend = DetectedBackend(runtime="vllm", base_url="http://x/v1", models=[])
    assert backend.pick_model("only-choice") == "only-choice"
    assert backend.pick_model(None) is None
