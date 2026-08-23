"""Local settings shared by every ``yeschef`` command on a machine.

`up` and `join` write this file; every other command reads it, so the common case is
zero flags. Environment variables still win when set, for scripts and overrides.

Kept deliberately dependency-free (stdlib json) so the SDK stays light.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
from dataclasses import asdict, dataclass
from pathlib import Path


def env(name: str, default: str | None = None) -> str | None:
    """YESCHEF_* first; CASCADIA_TASKS_* still honored for pre-rename deployments."""
    value = os.environ.get(f"YESCHEF_{name}")
    if value is not None:
        return value
    return os.environ.get(f"CASCADIA_TASKS_{name}", default)


def home() -> Path:
    override = env("HOME")
    if override:
        return Path(override).expanduser()
    new = Path("~/.yeschef").expanduser()
    legacy = Path("~/.cascadia-tasks").expanduser()
    # A machine set up before the rename keeps working without migration.
    if not new.exists() and legacy.exists():
        return legacy
    return new


def config_path() -> Path:
    return home() / "config.json"


@dataclass(slots=True)
class Settings:
    """What a command needs to reach and act on a hub."""

    hub_url: str = "http://localhost:8787"
    admin_token: str | None = None
    register_token: str | None = None
    # Where workers should reach this hub — set by `up`, printed in the join command.
    advertise_url: str | None = None

    def merged_with_env(self) -> Settings:
        return Settings(
            hub_url=env("HUB", self.hub_url) or self.hub_url,
            admin_token=env("ADMIN_TOKEN", self.admin_token),
            register_token=env("REGISTER_TOKEN", self.register_token),
            advertise_url=self.advertise_url,
        )


def load() -> Settings:
    """File values with environment overrides layered on top."""
    path = config_path()
    if path.exists():
        raw = json.loads(path.read_text())
        base = Settings(
            hub_url=raw.get("hub_url", "http://localhost:8787"),
            admin_token=raw.get("admin_token"),
            register_token=raw.get("register_token"),
            advertise_url=raw.get("advertise_url"),
        )
    else:
        base = Settings()
    return base.merged_with_env()


def save(settings: Settings) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2) + "\n")
    # Tokens live here; keep them off other users on a shared box.
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def best_lan_url(port: int) -> str:
    """A URL other machines on the LAN can most plausibly use to reach this host.

    A guess, not a guarantee: on a Tailnet the MagicDNS name is usually better, so the
    join command that prints this also says how to override it.
    """
    host = socket.getfqdn()
    if not host or host.startswith("localhost") or "." not in host:
        try:
            host = socket.gethostbyname(socket.gethostname())
        except OSError:
            host = socket.gethostname()
    return f"http://{host}:{port}"
