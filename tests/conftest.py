from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from doraymon.app import Application
from doraymon.config import Settings
from doraymon.models import Incoming, Place
from doraymon.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database=tmp_path / "test.sqlite3",
        merge_gap=0,
        allowed_users={"alice", "bob"},
        allowed_groups={"g1", "g2"},
        model_key="synthetic",
        amap_key="synthetic",
        admin_users={"alice"},
    )


@pytest.fixture
async def store(settings):
    result = Store(
        settings.database, max_turns=3, clock=lambda: datetime(2026, 10, 24, 4, tzinfo=UTC)
    )
    await result.open()
    yield result
    await result.close()


@pytest.fixture
def places():
    return (
        Place(
            id="a", name="合成甲店", address="测试路1号", distance=100, longitude=115, latitude=28
        ),
        Place(
            id="b", name="合成乙店", address="测试路2号", distance=150, longitude=115, latitude=28
        ),
    )


@pytest.fixture
def clients(places):
    return SimpleNamespace(
        complete=AsyncMock(return_value="合成回答"), nearby=AsyncMock(return_value=places)
    )


@pytest.fixture
async def app(settings, store, clients):
    result = Application(settings, store, clients)
    yield result
    await result.close()


@pytest.fixture
def event():
    def make(text="你好", **kwargs):
        fields = {"bot_id": "bot", "user_id": "alice", "message_id": "m1", "text": text}
        return Incoming(**(fields | kwargs))

    return make
