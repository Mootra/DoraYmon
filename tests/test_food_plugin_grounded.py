"""P2a wiring: a curated catalogue must actually change what /吃什么 answers."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from doraymon.config import Settings
from doraymon.context import BotContext
from plugins import food
from services.meal_session import MEAL_STATES
from services.session_history import get_session_key
from storage.db import init_all_tables


def _catalogue_payload() -> dict:
    return {
        "shops": [
            {
                "shop_id": "s1",
                "name": "示例兰州拉面（合成）",
                "address": "示例路 1 号",
                "map_url": "https://map.example.test/shop/s1",
                "distance_meters": 320,
                "source": {"kind": "map", "reference": "map/1", "fetched_at": "2026-10-19"},
            },
            {
                "shop_id": "s2",
                "name": "示例快餐（合成，仅 POI）",
                "address": "示例路 9 号",
                "distance_meters": 900,
                "source": {"kind": "map", "reference": "map/2"},
            },
        ],
        "items": [
            {
                "item_id": "i1",
                "shop_id": "s1",
                "name": "牛肉拉面（合成）",
                "source": {"kind": "menu", "reference": "menu/1"},
                "composition": ["拉面", "牛肉"],
                "composition_source": {"kind": "menu", "reference": "menu/1"},
                "portion": "大碗，约 600 克",
                "portion_source": {"kind": "review", "reference": "review/1"},
                "flavours": [
                    {
                        "description": "清汤牛肉汤底，面条偏硬",
                        "source": {"kind": "review", "reference": "review/1"},
                    }
                ],
                "prices": [
                    {
                        "basis": "menu",
                        "amount_cny": 18,
                        "source": {"kind": "menu", "reference": "menu/1"},
                    }
                ],
            },
            {
                "item_id": "i2",
                "shop_id": "s2",
                "name": "招牌套餐（合成）",
                "source": {"kind": "map", "reference": "map/2"},
                "prices": [
                    {
                        "basis": "per_person",
                        "amount_cny": 25,
                        "source": {"kind": "map", "reference": "map/2"},
                    }
                ],
            },
        ],
    }


class GroundedFoodPluginTest(unittest.TestCase):
    def setUp(self) -> None:
        MEAL_STATES.clear()
        self._temp = tempfile.TemporaryDirectory()
        self.directory = Path(self._temp.name) / "meal_catalog"
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "meal_catalog.json").write_text(
            json.dumps(_catalogue_payload(), ensure_ascii=False), encoding="utf-8"
        )
        self._store = tempfile.TemporaryDirectory()
        self._patch = patch(
            "storage.chat_history_store.get_connection",
            side_effect=self._connection,
        )
        self._patch.start()
        init_all_tables()

    def tearDown(self) -> None:
        MEAL_STATES.clear()
        self._patch.stop()
        self._store.cleanup()
        self._temp.cleanup()

    def _connection(self, name: str):
        import sqlite3

        connection = sqlite3.connect(Path(self._store.name) / "test.db")
        connection.row_factory = sqlite3.Row
        return connection

    def _context(
        self,
        args: str,
        *,
        catalog_dir: str | None = None,
        provider: str = "",
    ) -> BotContext:
        directory = str(self.directory) if catalog_dir is None else catalog_dir
        return BotContext(
            settings=Settings(
                qqbot_appid="bot-1",
                chat_history_enabled=False,
                meal_catalog_dir=directory,
                nearby_provider=provider,
            ),
            started_at=datetime.now(),
            user_openid="user-a",
            raw_content=f"/吃什么 {args}",
            command="吃什么",
            args=args,
        )

    def test_catalogue_answers_a_location_request(self) -> None:
        with patch("plugins.food.recommend_food") as fallback:
            reply = asyncio.run(food.handle(self._context("在示例大学附近吃什么")))

        fallback.assert_not_called()
        self.assertIn("示例兰州拉面（合成）", reply)
        self.assertIn("牛肉拉面（合成）", reply)
        self.assertIn("组成：", reply)
        self.assertIn("菜单价：", reply)

    def test_reply_states_gaps_for_an_incomplete_item(self) -> None:
        with patch("plugins.food.recommend_food"):
            reply = asyncio.run(food.handle(self._context("在示例大学附近，预算 30 元")))

        self.assertIn("菜单价：", reply)
        # The per-person figure must never be printed as this item's price.
        self.assertNotIn("¥25", reply)

    def test_request_without_a_location_falls_back(self) -> None:
        with patch("plugins.food.recommend_food", return_value=_Fallback()) as fallback:
            reply = asyncio.run(food.handle(self._context("今晚吃什么")))

        fallback.assert_called_once()
        self.assertEqual(reply, "规则库回答")

    def test_empty_catalogue_does_not_invent_nearby_shops(self) -> None:
        with patch("plugins.food.recommend_food", return_value=_Fallback()) as fallback:
            reply = asyncio.run(food.handle(
                self._context("在示例大学附近吃什么", catalog_dir=str(self.directory / "missing"))
            ))

        fallback.assert_not_called()
        self.assertIn("示例大学", reply)
        self.assertIn("高德还没接通", reply)

    def test_missing_directory_does_not_invent_nearby_shops(self) -> None:
        with patch("plugins.food.recommend_food", return_value=_Fallback()) as fallback:
            reply = asyncio.run(food.handle(self._context("在示例大学附近吃什么", catalog_dir="")))

        fallback.assert_not_called()
        self.assertIn("示例大学", reply)
        self.assertIn("高德还没接通", reply)

    def test_budget_follow_up_keeps_the_location(self) -> None:
        with patch("plugins.food.recommend_food") as fallback:
            first = asyncio.run(food.handle(self._context("在示例大学附近吃什么")))
            second = asyncio.run(food.handle(self._context("预算 15 元")))

        fallback.assert_not_called()
        self.assertIn("示例兰州拉面（合成）", first)
        self.assertTrue(second)

    def test_map_details_keep_the_current_catalogue_selection(self) -> None:
        context = self._context("在示例大学附近吃什么")
        asyncio.run(food.handle(context))
        before = MEAL_STATES.get(get_session_key(context))
        reply = asyncio.run(food.handle(self._context("地图")))
        after = MEAL_STATES.get(get_session_key(context))
        self.assertIn("https://map.example.test/shop/s1", reply)
        self.assertIn("示例兰州拉面（合成）", reply)
        self.assertEqual(after.current_item_id, before.current_item_id)
        self.assertEqual(after.radius_meters, before.radius_meters)


class ProviderOnlyTest(unittest.TestCase):
    """With no catalogue but a configured provider, the seam must be reached."""

    def setUp(self) -> None:
        MEAL_STATES.clear()
        self._temp = tempfile.TemporaryDirectory()
        self._store = tempfile.TemporaryDirectory()
        self._patch = patch(
            "storage.chat_history_store.get_connection", side_effect=self._connection
        )
        self._patch.start()
        init_all_tables()

    def tearDown(self) -> None:
        MEAL_STATES.clear()
        self._patch.stop()
        self._store.cleanup()
        self._temp.cleanup()

    def _connection(self, name: str):
        import sqlite3

        connection = sqlite3.connect(Path(self._store.name) / "test.db")
        connection.row_factory = sqlite3.Row
        return connection

    def _context(self, args: str, *, provider: str) -> BotContext:
        return BotContext(
            settings=Settings(
                qqbot_appid="bot-1",
                chat_history_enabled=False,
                meal_catalog_dir=str(Path(self._temp.name) / "missing"),
                nearby_provider=provider,
            ),
            started_at=datetime.now(),
            user_openid="user-a",
            raw_content=f"/吃什么 {args}",
            command="吃什么",
            args=args,
        )

    def test_unconfigured_provider_explains_missing_map_data(self) -> None:
        with patch("plugins.food.recommend_food", return_value=_Fallback()) as fallback:
            reply = asyncio.run(food.handle(self._context("在示例大学附近吃什么", provider="")))

        fallback.assert_not_called()
        self.assertIn("高德还没接通", reply)

    def test_configured_provider_without_a_key_says_so(self) -> None:
        """A named provider with no key must explain itself, not invent shops."""
        with patch("plugins.food.recommend_food") as fallback:
            reply = asyncio.run(food.handle(self._context("在示例大学附近吃什么", provider="amap")))

        fallback.assert_not_called()
        self.assertIn("高德还没接通", reply)
        # The generic message would hide the real reason.
        self.assertNotIn("没有配置地点服务", reply)

    def test_unknown_provider_name_is_reported(self) -> None:
        with patch("plugins.food.recommend_food") as fallback:
            reply = asyncio.run(food.handle(self._context("在示例大学附近吃什么", provider="baidu")))

        fallback.assert_not_called()
        self.assertIn("不认识的地点服务", reply)

    def test_named_provider_uses_the_default_place_and_reports_missing_key(self) -> None:
        with patch("plugins.food.recommend_food", return_value=_Fallback()) as fallback:
            reply = asyncio.run(food.handle(self._context("今晚吃什么", provider="amap")))

        fallback.assert_not_called()
        self.assertIn("江西师范大学瑶湖校区", reply)
        self.assertIn("高德还没接通", reply)


class _Fallback:
    """Stands in for the rule-based recommendation."""

    food_name = "规则库"
    reason = "回答"

    @property
    def text(self) -> str:
        return "规则库回答"


if __name__ == "__main__":
    unittest.main()
