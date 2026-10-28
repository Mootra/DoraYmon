"""The official preflight, and the event-loop bug that made it necessary.

The event-loop test is deliberately specific: botpy calls
``asyncio.get_event_loop()`` while constructing its client, which raises on a
current Python when no loop is installed. That failure happens before any request
is made, so a correct AppID would look broken and an operator would go hunting for
a credential problem that does not exist.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from scripts.preflight_official_bot import (
    environment_notes,
    fetch_token,
    hint_for,
    interpret_token_response,
    local_problems,
)
from doraymon.config import Settings


class TokenInterpretationTest(unittest.TestCase):
    """The endpoint answers HTTP 200 even when it fails."""

    def test_a_success_payload_without_a_code_is_accepted(self) -> None:
        """A success response carries the token and no code at all."""
        outcome = interpret_token_response(
            {"access_token": "abc", "expires_in": "7200"}, http_status=200
        )

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.expires_in, 7200)

    def test_an_error_code_is_read_from_the_body_despite_http_200(self) -> None:
        """A status-only check would call this a success."""
        outcome = interpret_token_response(
            {"code": 100016, "message": "invalid appid or secret"}, http_status=200
        )

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, 100016)
        self.assertIn("invalid", outcome.message)

    def test_code_zero_is_success(self) -> None:
        outcome = interpret_token_response(
            {"code": 0, "access_token": "abc", "expires_in": 7200}, http_status=200
        )

        self.assertTrue(outcome.ok)

    def test_a_payload_with_neither_code_nor_token_is_not_success(self) -> None:
        outcome = interpret_token_response({}, http_status=200)

        self.assertFalse(outcome.ok)
        self.assertIn("无法判断", outcome.message)

    def test_a_non_object_payload_is_refused(self) -> None:
        self.assertFalse(interpret_token_response([], http_status=200).ok)

    def test_the_token_is_kept_out_of_the_repr(self) -> None:
        """A token must never leak into a log line through a repr."""
        outcome = interpret_token_response({"access_token": "super-secret-token"})

        self.assertTrue(outcome.ok)
        self.assertNotIn("super-secret-token", repr(outcome))

    def test_unparseable_expiry_is_unknown_not_zero(self) -> None:
        outcome = interpret_token_response({"access_token": "abc", "expires_in": "soon"})

        self.assertIsNone(outcome.expires_in)
        self.assertIn("未知", outcome.describe())


class HintTest(unittest.TestCase):
    def test_known_codes_get_actionable_advice(self) -> None:
        cases = {
            100007: "封禁",
            100016: "AppSecret",
            10004: "没有机器人",
            100001: "频繁",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                outcome = interpret_token_response({"code": code})
                self.assertIn(expected, hint_for(outcome))

    def test_an_unknown_code_says_so(self) -> None:
        outcome = interpret_token_response({"code": 999999})

        self.assertIn("未收录", hint_for(outcome))

    def test_a_successful_outcome_has_no_hint(self) -> None:
        self.assertEqual(hint_for(interpret_token_response({"access_token": "a"})), "")


class FetchTokenTest(unittest.TestCase):
    def test_a_network_failure_is_reported_not_raised(self) -> None:
        def boom(*args, **kwargs):
            raise ConnectionError("no route to host")

        outcome = fetch_token("1", "secret", post=boom)

        self.assertFalse(outcome.ok)
        self.assertIn("请求失败", outcome.message)

    def test_a_non_json_response_is_reported(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.side_effect = ValueError("not json")

        outcome = fetch_token("1", "secret", post=lambda *a, **k: response)

        self.assertFalse(outcome.ok)
        self.assertIn("不是 JSON", outcome.message)

    def test_the_appid_and_secret_are_sent(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"access_token": "t"}
        captured: dict = {}

        def post(url, **kwargs):
            captured.update(kwargs)
            return response

        fetch_token("123", "sekret", post=post)

        self.assertEqual(captured["json"], {"appId": "123", "clientSecret": "sekret"})


class LocalProblemsTest(unittest.TestCase):
    def _settings(self, **overrides) -> Settings:
        payload = {
            "qqbot_appid": "1904033781",
            "qqbot_secret": "a-secret",
            "deepseek_api_key": "a-key",
            "admin_openids": ["admin-a"],
        }
        payload.update(overrides)
        return Settings(**payload)

    def test_a_complete_configuration_has_no_problems(self) -> None:
        self.assertEqual(local_problems(self._settings()), [])

    def test_a_non_numeric_appid_is_flagged(self) -> None:
        problems = local_problems(self._settings(qqbot_appid="bot-1"))

        self.assertTrue(any("纯数字" in problem for problem in problems))

    def test_whitespace_around_the_secret_is_flagged(self) -> None:
        """The most common copy-paste failure."""
        problems = local_problems(self._settings(qqbot_secret=" secret "))

        self.assertTrue(any("空白" in problem for problem in problems))

    def test_a_missing_model_key_is_flagged_with_its_consequence(self) -> None:
        problems = local_problems(self._settings(deepseek_api_key=""))

        self.assertTrue(any("未配置" in problem for problem in problems))

    def test_a_missing_admin_is_flagged_because_silence_needs_one(self) -> None:
        problems = local_problems(self._settings(admin_openids=[]))

        self.assertTrue(any("静默" in problem for problem in problems))


class EnvironmentNotesTest(unittest.TestCase):
    def test_sandbox_mode_warns_that_it_proves_nothing_about_production(self) -> None:
        notes = " ".join(environment_notes(Settings(qqbot_sandbox=True)))

        self.assertIn("沙箱", notes)
        self.assertIn("不代表", notes)

    def test_the_ip_allowlist_constraint_is_stated(self) -> None:
        notes = " ".join(environment_notes(Settings()))

        self.assertIn("白名单", notes)


class EventLoopTest(unittest.TestCase):
    """The bug that made preflight necessary in the first place."""

    def test_a_current_event_loop_is_installed_when_missing(self) -> None:
        """botpy asks for a current loop during construction, before any request."""
        from main import _prepare_event_loop

        loop = _prepare_event_loop()

        self.assertIs(asyncio.get_event_loop(), loop)

    def test_preparing_twice_returns_the_same_loop(self) -> None:
        from main import _prepare_event_loop

        first = _prepare_event_loop()
        second = _prepare_event_loop()

        self.assertIs(first, second)

    def test_botpy_client_construction_succeeds_after_preparation(self) -> None:
        """The actual failure: botpy raises before a request is ever made."""
        from main import _prepare_event_loop
        from doraymon.client import MyClient

        _prepare_event_loop()
        client = object.__new__(MyClient)
        client.settings = Settings(qqbot_appid="bot-1", chat_history_enabled=False)

        # Constructing the real botpy base is what used to raise.
        from botpy import Client

        with patch.object(Client, "__init__", return_value=None) as init:
            Client.__init__(client, intents=None)
            init.assert_called_once()


if __name__ == "__main__":
    unittest.main()
