"""Track detached hub/agent processes so one terminal can start and stop the fleet.

Best-effort process supervision: enough for `up --detach` / `join --detach` / `down`
on a workstation, not a replacement for the launchd and systemd units under
examples/deploy/ for a real deployment.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .settings import home


def _run_dir() -> Path:
    path = home() / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(slots=True)
class Proc:
    label: str
    pid: int
    kind: str  # "hub" | "agent"
    log: str
    command: list[str]


def _record_path(label: str) -> Path:
    safe = label.replace("/", "_").replace(":", "_")
    return _run_dir() / f"{safe}.json"


def spawn(label: str, kind: str, command: list[str]) -> Proc:
    """Start a detached child that outlives this process, logging to a file."""
    existing = get(label)
    if existing and is_alive(existing.pid):
        raise RuntimeError(f"'{label}' is already running (pid {existing.pid})")

    log_path = _run_dir() / f"{label.replace('/', '_').replace(':', '_')}.log"
    log_file = open(log_path, "ab")  # noqa: SIM115 - handed to the child, closed on exit
    kwargs: dict = {"stdout": log_file, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **kwargs)  # noqa: S603 - command is built by us
    log_file.close()
    proc = Proc(label=label, pid=process.pid, kind=kind, log=str(log_path), command=command)
    _record_path(label).write_text(json.dumps(asdict(proc), indent=2))
    return proc


def is_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def get(label: str) -> Proc | None:
    path = _record_path(label)
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return Proc(**raw)


def list_all() -> list[Proc]:
    return [Proc(**json.loads(p.read_text())) for p in sorted(_run_dir().glob("*.json"))]


def stop(label: str) -> bool:
    proc = get(label)
    if proc is None:
        return False
    if is_alive(proc.pid):
        try:
            if os.name == "nt":
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                os.kill(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    _record_path(proc.label).unlink(missing_ok=True)
    return True


def stop_all() -> list[str]:
    return [proc.label for proc in list_all() if stop(proc.label)]


def cli_executable() -> list[str]:
    """How to re-invoke this CLI for a detached child."""
    return [sys.executable, "-m", "farmteam"]
