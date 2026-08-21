from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest

from cascadia_tasks.hub import HubConfig, Store
from cascadia_tasks.hub.events import EventBus


@pytest.fixture
def store() -> Iterator[Store]:
    """Synchronous store for state-machine tests."""
    instance = Store(":memory:", EventBus())
    yield instance
    instance.close()


@pytest.fixture
async def async_store() -> AsyncIterator[Store]:
    """Store with its event bus bound to the running loop, for delivery tests."""
    instance = Store(":memory:", EventBus())
    instance.bus.bind(asyncio.get_running_loop())
    yield instance
    instance.close()


@pytest.fixture
def config() -> HubConfig:
    return HubConfig(db_path=":memory:", sweep_interval_s=3600.0)


def register(store: Store, name: str, **kwargs) -> str:
    """Register a worker and return its token."""
    _, token = store.register_agent(name=name, **kwargs)
    return token
