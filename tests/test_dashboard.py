"""The web dashboard: a read-only, curated, un-authenticated fleet snapshot at / and
/overview (same LAN-only boundary as /healthz), plus the served HTML page."""

from __future__ import annotations

from fastapi.testclient import TestClient

from yeschef.hub.api import HubConfig, create_app
from yeschef.hub.store import Store


def _client(tmp_path) -> tuple[TestClient, Store]:
    store = Store(str(tmp_path / "hub.db"))
    # tokens configured → agent/REST reads are gated, but the dashboard must still work
    app = create_app(store, HubConfig(db_path=str(tmp_path / "hub.db"), admin_token="secret"))
    return TestClient(app), store


def test_dashboard_page_served_at_root(tmp_path) -> None:
    client, _ = _client(tmp_path)
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<title>yeschef</title>" in r.text
    assert "/overview" in r.text  # the page polls the overview endpoint


def test_overview_is_open_and_curated(tmp_path) -> None:
    """Un-authenticated even when a token is set (LAN boundary), and it never leaks
    task specs or result bodies."""
    client, store = _client(tmp_path)
    store.register_agent("forge", node="miner", backend="ollama/qwen3.8:27b", tags=["coder"])
    t = store.submit_task(
        title="build page",
        spec="SECRET-SPEC-CONTENT " * 20,
        created_by="claude:local",
        assignee="forge",
    )
    store.claim_task(t.id, "forge")
    store.update_progress(t.id, "forge", 40.0, "working")

    # a REST read IS gated (control): 401 without the token
    assert client.get("/api/v1/agents").status_code == 401
    # the overview is NOT gated
    r = client.get("/overview")
    assert r.status_code == 200
    body = r.json()
    assert "SECRET-SPEC-CONTENT" not in r.text  # spec never exposed
    w = next(x for x in body["workers"] if x["name"] == "forge")
    assert w["backend"] == "ollama/qwen3.8:27b" and w["active_tasks"] == 1
    row = next(x for x in body["tasks"] if x["title"] == "build page")
    assert row["state"] == "working" and row["assignee"] == "forge"
    assert "spec" not in row and "result" not in row


def test_overview_handles_empty_fleet(tmp_path) -> None:
    client, _ = _client(tmp_path)
    body = client.get("/overview").json()
    assert body["workers"] == [] and body["tasks"] == []
    assert "lifetime" in body


def test_dashboard_html_ships_in_the_package() -> None:
    from pathlib import Path

    import yeschef.hub

    assert (Path(yeschef.hub.__file__).with_name("dashboard.html")).exists()
