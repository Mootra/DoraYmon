"""Nearby messages reach the real provider pipeline with synthetic HTTP data."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from doraymon.config import Settings
from doraymon.context import BotContext
from doraymon.router import COMMANDS, route_incoming_message
from services.meal_session import MEAL_STATES
from services.nearby_lookup import reset_lookup_cache
from services.session_history import clear_history, get_session_key


class NearbyFoodRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        MEAL_STATES.clear()
        # Both the lookup and the HTTP session are reused process-wide now, so
        # each test starts from an empty cache: otherwise a previous test's
        # cached places serve this test and the call-order assertions below would
        # be measuring the wrong turn.
        reset_lookup_cache()
        self.calls: list[tuple[str, dict]] = []
        self.session = Mock()
        self.session.get = Mock(side_effect=self._http_get)
        self.http = patch(
            "services.amap_http.shared_session", return_value=self.session
        ).start()

    def tearDown(self) -> None:
        patch.stopall()
        MEAL_STATES.clear()
        reset_lookup_cache()

    def _context(self, text: str, *, user: str = "user-a", configured: bool = True) -> BotContext:
        return BotContext(
            settings=Settings(
                chat_history_enabled=False,
                meal_catalog_dir="",
                nearby_provider="amap" if configured else "",
                nearby_provider_key="synthetic-test-key" if configured else "",
            ),
            started_at=datetime.now(),
            platform="qqaccount",
            bot_account="bot-test",
            user_openid=user,
            raw_content=text,
        )

    def _route(self, text: str, **kwargs) -> str:
        return asyncio.run(route_incoming_message(self._context(text, **kwargs), fallback_command="chat"))

    def _http_get(self, url: str, *, timeout: float):
        parsed = urlparse(url)
        params = {name: values[0] for name, values in parse_qs(parsed.query).items()}
        self.calls.append((parsed.path, params))
        if parsed.path.endswith("/geocode/geo"):
            coordinate = "115.0,28.0" if "北校区" in params["address"] else "116.0,29.0"
            payload = {
                "status": "1",
                "geocodes": [{"formatted_address": params["address"] + "（合成校门）", "location": coordinate}],
            }
        else:
            self.assertTrue(parsed.path.endswith("/place/around"))
            payload = {
                "status": "1",
                "pois": [
                    {
                        "id": f"synthetic-{index}",
                        "name": f"合成餐馆{index}",
                        "address": f"合成路{index}号",
                        "location": params["location"],
                        "distance": str(index * 100),
                    }
                    for index in range(1, 11)
                ],
            }
        return SimpleNamespace(status_code=200, json=lambda: payload)

    def test_nearby_question_uses_the_default_campus_through_amap(self) -> None:
        with patch("plugins.food.recommend_food") as fallback:
            reply = self._route("附近有什么吃的")

        fallback.assert_not_called()
        self.assertEqual(self.calls[0][1]["address"], "江西师范大学瑶湖校区")
        self.assertEqual(self.calls[1][1]["location"], "116.000000,29.000000")
        self.assertIn("江西师范大学瑶湖校区附近", reply)
        self.assertIn("距离起点：江西师范大学瑶湖校区（合成校门）", reply)
        self.assertIn("合成餐馆1", reply)
        self.assertIn("先看看合成餐馆1（直线距离约100米） 合成路1号", reply)
        self.assertEqual(reply.count("合成餐馆"), 1)
        self.assertNotIn("https://", reply)
        self.assertIn("现价", reply)

    def test_explicit_place_overrides_default_and_survives_follow_ups(self) -> None:
        self._route("附近有什么吃的")
        reply = self._route("示例大学北校区附近吃什么")
        closer = self._route("再近点")

        self.assertNotIn("江西师范大学瑶湖校区", reply)
        self.assertIn("示例大学北校区附近", reply)
        self.assertIn("示例大学北校区附近", closer)
        self.assertEqual(self.calls[-2][1]["address"], "示例大学北校区")
        self.assertEqual(self.calls[-1][1]["radius"], "1000")
        self.assertEqual(self.calls[-1][1]["location"], "115.000000,28.000000")

    def test_a_bare_landmark_can_continue_the_meal_conversation(self) -> None:
        self._route("附近有什么吃的")
        reply = self._route("示例大学北校区")
        self.assertIn("示例大学北校区附近", reply)
        self.assertEqual(self.calls[-2][1]["address"], "示例大学北校区")

    def test_another_shop_continues_from_the_current_choice(self) -> None:
        self._route("附近有什么吃的")
        reply = self._route("换一家")
        self.assertIn("合成餐馆2", reply)
        self.assertNotIn("合成餐馆1（", reply)

    def test_places_are_isolated_by_user(self) -> None:
        self._route("示例大学北校区附近吃什么")
        reply = self._route("附近有什么吃的", user="user-b")
        self.assertIn("江西师范大学瑶湖校区附近", reply)
        self.assertNotIn("示例大学北校区", reply)

    def test_unconfigured_map_does_not_fall_back_to_sample_shops_or_http(self) -> None:
        with patch("plugins.food.recommend_food") as fallback:
            reply = self._route("附近有什么吃的", configured=False)
        self.http.assert_not_called()
        fallback.assert_not_called()
        self.assertIn("江西师范大学瑶湖校区", reply)
        self.assertIn("高德还没接通", reply)

    def test_ordinary_chat_still_reaches_chat_after_a_meal_query(self) -> None:
        self._route("附近有什么吃的")
        with patch.dict(COMMANDS, {"chat": Mock(return_value="聊天回复")}):
            reply = self._route("换一种说法")
        self.assertEqual(reply, "聊天回复")
        self.assertEqual(len(self.calls), 2)

    def test_plain_phrasings_reach_the_map_instead_of_ordinary_chat(self) -> None:
        """The phrasings that really appeared in chat must not fall through.

        "附近有什么" and "江西师范大学附近" both used to reach ordinary chat,
        where the honest answer is "我查不到" — which reads as a broken feature.
        """
        for text in (
            "附近有什么",
            "附近有啥好吃的",
            "附近吃点啥",
            "江西师范大学附近",
            "师大附近有推荐的店吗",
            "推荐个附近的",
        ):
            with self.subTest(text=text):
                reset_lookup_cache()
                self.calls.clear()
                with patch("plugins.food.recommend_food") as fallback:
                    reply = self._route(text)
                fallback.assert_not_called()
                self.assertEqual(self.calls[0][0], "/v3/geocode/geo")
                self.assertIn("合成餐馆1", reply)

    def test_a_nearby_turn_is_visible_in_the_log(self) -> None:
        """A reply alone cannot prove the map was queried; the log has to say so."""
        with self.assertLogs("plugins.food", level="INFO") as captured:
            self._route("附近有什么")

        self.assertTrue(any("附近查询" in line for line in captured.output))
        self.assertTrue(any("高德返回=5家" in line for line in captured.output))
        self.assertTrue(any("采用=1家" in line for line in captured.output))

    def test_plain_meal_request_uses_the_configured_default_place(self) -> None:
        with patch("plugins.food.recommend_food") as fallback:
            reply = self._route("吃什么")
        fallback.assert_not_called()
        self.assertEqual(self.calls[0][1]["address"], "江西师范大学瑶湖校区")
        self.assertIn("合成餐馆1", reply)

    def test_map_request_refers_to_the_selected_shop_and_uses_cached_facts(self) -> None:
        self._route("吃什么")
        calls = len(self.calls)
        reply = self._route("发地图")
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(reply.count("https://uri.amap.com/marker?"), 1)
        self.assertIn("合成餐馆1", reply)
        self.assertIn("合成路1号", reply)

    def test_numbered_map_request_matches_the_last_list(self) -> None:
        self._route("吃什么")
        listed = self._route("多给几个")
        self.assertEqual(listed.count("合成餐馆"), 3)
        reply = self._route("第2家地图")
        self.assertIn("合成餐馆2", reply)
        self.assertNotIn("合成餐馆1", reply)
        self.assertEqual(reply.count("https://"), 1)
        self.assertIn("没有这家", self._route("第5家地图"))

    def test_map_details_follow_the_replacement_shop(self) -> None:
        self._route("吃什么")
        self._route("换一家")
        reply = self._route("地图")
        self.assertIn("合成餐馆2", reply)
        self.assertEqual(reply.count("https://"), 1)

    def test_budget_survives_replacement_and_location_changes_without_a_price_claim(self) -> None:
        self._route("吃什么，15元以内")
        replaced = self._route("换一个")
        moved = self._route("示例大学北校区附近吃什么")
        state = MEAL_STATES.get(get_session_key(self._context("")))
        self.assertEqual(state.budget_cny, 15)
        self.assertEqual(state.start.text, "示例大学北校区")
        for reply in (replaced, moved):
            self.assertIn("预算按15元保留", reply)
            self.assertIn("不能确认够不够", reply)

    def test_closer_request_reports_when_the_choice_did_not_get_closer(self) -> None:
        self._route("吃什么")
        reply = self._route("再近点")
        self.assertIn("没找到比上次更近", reply)
        self.assertIn("直线距离约100米", reply)

    def test_non_food_destinations_do_not_change_an_active_meal_location(self) -> None:
        self._route("示例大学北校区附近吃什么")
        calls = len(self.calls)
        for text in ("推荐附近的图书馆", "附近推荐的超市", "示例大学附近有什么图书馆", "还有哪些图书馆"):
            with self.subTest(text=text), patch.dict(COMMANDS, {"chat": Mock(return_value="聊天回复")}):
                self.assertEqual(self._route(text), "聊天回复")
        self.assertEqual(len(self.calls), calls)
        state = MEAL_STATES.get(get_session_key(self._context("")))
        self.assertEqual(state.start.text, "示例大学北校区")

    def test_party_size_is_retained_as_an_unverified_requirement(self) -> None:
        self._route("吃什么")
        reply = self._route("我们3个人")
        state = MEAL_STATES.get(get_session_key(self._context("")))
        self.assertEqual(state.party_size, 3)
        self.assertIn("3人用餐的份量尚未核实", reply)

    def test_a_cheaper_request_does_not_treat_map_cost_as_menu_price(self) -> None:
        self._route("吃什么")
        self.assertIn("没法比较哪家更便宜", self._route("便宜点"))

    def test_an_unrelated_nearby_question_still_goes_to_chat(self) -> None:
        for text in ("附近有什么图书馆", "学校附近租房贵吗", "附近有超市吗"):
            with self.subTest(text=text):
                self.calls.clear()
                with patch.dict(COMMANDS, {"chat": Mock(return_value="聊天回复")}):
                    reply = self._route(text)
                self.assertEqual(reply, "聊天回复")
                self.assertEqual(self.calls, [])

    def test_a_repeated_question_is_served_from_the_cache(self) -> None:
        """The lookup survives the turn now; a repeat must not re-fetch."""
        self._route("附近有什么")
        calls_after_first = len(self.calls)

        reply = self._route("附近有什么")

        self.assertEqual(len(self.calls), calls_after_first)
        self.assertIn("合成餐馆1", reply)

    def test_clearing_context_restores_the_default_location(self) -> None:
        self._route("示例大学北校区附近吃什么")
        context = self._context("/清空上下文")
        with (
            patch("services.session_history.init_chat_history_table"),
            patch("services.session_history.clear_messages", return_value=2),
            patch("services.session_history.clear_summary"),
        ):
            clear_history(context)
        self.assertIsNone(MEAL_STATES.get(get_session_key(context)))
        self.assertIn("江西师范大学瑶湖校区附近", self._route("附近有什么吃的"))


if __name__ == "__main__":
    unittest.main()
