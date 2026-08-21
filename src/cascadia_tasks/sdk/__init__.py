"""Client library for the cascadia-tasks hub — the protocol contract.

Anything embedding `AgentClient` becomes a first-class agent: it can join rooms, hold
multi-turn conversations, and claim and work tasks. Depends on httpx only.
"""

from .client import AgentClient, HubClientError

__all__ = ["AgentClient", "HubClientError"]
