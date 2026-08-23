"""Find a local model server so `join` can configure itself.

Probes the endpoints local runtimes conventionally listen on and reports what is there,
so an operator can bring a worker online without hand-writing a backend config.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

PROBE_TIMEOUT_S = 1.5


@dataclass(slots=True)
class DetectedBackend:
    runtime: str  # "ollama" | "vllm" | "lmstudio" | ...
    base_url: str  # OpenAI-compatible base, e.g. http://localhost:11434/v1
    models: list[str]

    def pick_model(self, preferred: str | None) -> str | None:
        if not self.models:
            return preferred
        if preferred:
            exact = [m for m in self.models if m == preferred]
            if exact:
                return exact[0]
            partial = [m for m in self.models if preferred in m]
            if partial:
                return partial[0]
        return self.models[0]


# host is filled in per probe so a worker can point at a model on another box.
_PROBES = [
    ("ollama", 11434, "/api/tags"),
    ("vllm", 8000, "/v1/models"),
    ("lmstudio", 1234, "/v1/models"),
    ("openai_compat", 8080, "/v1/models"),  # Tahoma's usual port
]


def _models_from(runtime: str, payload: dict) -> list[str]:
    if runtime == "ollama":
        return [m.get("name") or m.get("model") for m in payload.get("models", []) if m]
    data = payload.get("data") or []
    return [entry.get("id") for entry in data if entry.get("id")]


def probe(host: str = "localhost", timeout: float = PROBE_TIMEOUT_S) -> list[DetectedBackend]:
    """Return every model server found on `host`, in priority order."""
    found: list[DetectedBackend] = []
    with httpx.Client(timeout=timeout) as client:
        for runtime, port, path in _PROBES:
            url = f"http://{host}:{port}{path}"
            try:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                continue
            models = [m for m in _models_from(runtime, payload) if m]
            # Ollama and everything else expose an OpenAI-compatible base at /v1.
            base = f"http://{host}:{port}/v1"
            found.append(DetectedBackend(runtime=runtime, base_url=base, models=models))
    return found


def autodetect(host: str = "localhost") -> DetectedBackend | None:
    """The single best local backend, or None if nothing is listening."""
    candidates = probe(host)
    return candidates[0] if candidates else None


async def preflight(backend_config: dict) -> tuple[bool, str]:
    """Prove a backend config actually answers before a worker commits to it.

    A wrong base_url or model otherwise registers happily and only surfaces when the
    first task fails. Returns (ok, human message).
    """
    from .backends import build_backend
    from .backends.base import Turn

    backend = build_backend(backend_config)
    try:
        result = await backend.chat(
            "You are a health check.",
            [Turn(role="user", content="Reply with the single word: ok")],
            max_tokens=8,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - the whole point is to report the failure
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        await backend.close()
    text = (result.text or "").strip()
    return True, text or "(empty reply, but the endpoint answered)"
