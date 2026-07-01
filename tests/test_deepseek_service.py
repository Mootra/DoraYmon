from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from doraymon.config import Settings
from services.deepseek_service import DeepSeekService


class DeepSeekServiceTest(unittest.TestCase):
    def _settings(self, api_key: str = "sk-test-secret") -> Settings:
        return Settings(
            deepseek_api_key=api_key,
            deepseek_base_url="https://example.test",
            deepseek_model="deepseek-test-model",
            deepseek_temperature=0.2,
        )

    def _mock_response(self, content: str = "模型回复") -> Mock:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": content,
                    }
                }
            ]
        }
        return response

    @patch("services.deepseek_service.requests.post")
    def test_chat_prompt_still_works(self, post_mock) -> None:
        post_mock.return_value = self._mock_response("你好")
        service = DeepSeekService(self._settings())

        reply = service.chat("你好")

        self.assertEqual(reply, "你好")
        post_mock.assert_called_once()

    @patch("services.deepseek_service.requests.post")
    def test_chat_prompt_payload_contains_user_message(self, post_mock) -> None:
        post_mock.return_value = self._mock_response()
        service = DeepSeekService(self._settings())

        service.chat("旧入口消息")

        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "deepseek-test-model")
        self.assertEqual(payload["temperature"], 0.2)
        self.assertIn(
            {"role": "user", "content": "旧入口消息"},
            payload["messages"],
        )

    @patch("services.deepseek_service.requests.post")
    def test_chat_messages_preserves_message_order(self, post_mock) -> None:
        post_mock.return_value = self._mock_response()
        service = DeepSeekService(self._settings())
        messages = [
            {"role": "system", "content": "你是一个 QQ Bot 助手"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，有什么可以帮你？"},
            {"role": "user", "content": "我刚才说了什么？"},
        ]

        service.chat_messages(messages)

        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["messages"], messages)

    @patch("services.deepseek_service.requests.post")
    def test_missing_api_key_does_not_post(self, post_mock) -> None:
        service = DeepSeekService(self._settings(api_key=""))

        reply = service.chat_messages([{"role": "user", "content": "你好"}])

        self.assertIn("API Key 未配置", reply)
        post_mock.assert_not_called()

    @patch("services.deepseek_service.requests.post")
    def test_api_error_does_not_leak_secret(self, post_mock) -> None:
        fake_secret = "sk-test-secret-never-leak"
        post_mock.side_effect = requests.RequestException("network unavailable")
        service = DeepSeekService(self._settings(api_key=fake_secret))

        reply = service.chat_messages([{"role": "user", "content": "你好"}])

        self.assertEqual(reply, "DeepSeek 请求失败，请稍后再试。")
        self.assertNotIn(fake_secret, reply)
        self.assertNotIn("Authorization", reply)

    @patch("services.deepseek_service.requests.post")
    def test_mock_response_content_is_extracted(self, post_mock) -> None:
        post_mock.return_value = self._mock_response("  已提取回复  ")
        service = DeepSeekService(self._settings())

        reply = service.chat_messages([{"role": "user", "content": "你好"}])

        self.assertEqual(reply, "已提取回复")

    @patch("services.deepseek_service.requests.post")
    def test_empty_messages_returns_controlled_error_without_post(self, post_mock) -> None:
        service = DeepSeekService(self._settings())

        reply = service.chat_messages([])

        self.assertEqual(reply, "DeepSeek 请求格式错误，请检查消息内容。")
        post_mock.assert_not_called()

    @patch("services.deepseek_service.requests.post")
    def test_invalid_role_returns_controlled_error_without_post(self, post_mock) -> None:
        service = DeepSeekService(self._settings())

        reply = service.chat_messages([{"role": "tool", "content": "不能传这个角色"}])

        self.assertEqual(reply, "DeepSeek 请求格式错误，请检查消息内容。")
        post_mock.assert_not_called()

    def test_test_file_does_not_read_real_env_or_api_key(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")

        forbidden_patterns = (
            "load_" + "settings()",
            'open("' + '.env"',
            "QQBOT_" + "SECRET=",
            "DEEPSEEK_" + "API_KEY=",
        )
        for pattern in forbidden_patterns:
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, source)


if __name__ == "__main__":
    unittest.main()
