"""The repo doubles as a Claude Code plugin; keep its copies in lockstep."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_plugin_manifest_matches_package_version() -> None:
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert f'version = "{manifest["version"]}"' in pyproject, (
        "plugin.json and pyproject.toml versions have drifted"
    )
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert manifest["version"] in changelog, "CHANGELOG has no entry for the manifest version"


def test_plugin_assets_mirror_the_packaged_resources() -> None:
    """install-skill serves from package resources; the plugin serves the same files
    from top-level dirs. If they drift, plugin users and pip users get different
    behavior — this fails before that ships."""
    pairs = [
        ("skills/farmteam/SKILL.md", "src/farmteam/resources/skill/SKILL.md"),
        ("agents/farmteam-watcher.md", "src/farmteam/resources/agents/farmteam-watcher.md"),
    ]
    for plugin_path, resource_path in pairs:
        plugin_text = (ROOT / plugin_path).read_text()
        resource_text = (ROOT / resource_path).read_text()
        assert plugin_text == resource_text, (
            f"{plugin_path} differs from {resource_path} — sync them"
        )


def test_setup_command_exists_and_is_frontmattered() -> None:
    command = (ROOT / "commands" / "setup.md").read_text()
    assert command.startswith("---")
    assert "description:" in command.split("---")[1]
