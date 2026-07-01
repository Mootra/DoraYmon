from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import requests

from doraymon.config import Settings

logger = logging.getLogger(__name__)
DEFAULT_SYSTEM_PROMPT = "你是 DoraYmon，一个简洁、友好的 QQ Bot 助手。"
VALID_MESSAGE_ROLES = {"system", "user", "assistant"}


class DeepSeekService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def chat(self, prompt: str) -> str:
        return self.chat_messages(
            [
                {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )

    def chat_messages(self, messages: Sequence[dict[str, str]]) -> str:
        if not self.settings.deepseek_api_key:
            return "DeepSeek API Key 未配置，请检查 .env。"

        normalized_messages = _normalize_messages(messages)
        if not normalized_messages:
            return "DeepSeek 请求格式错误，请检查消息内容。"

        url = f"{self.settings.deepseek_base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.settings.deepseek_model or "deepseek-v4-flash",
            "messages": normalized_messages,
            "temperature": self.settings.deepseek_temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            return (
                data["choices"][0]["message"]["content"].strip()
                or "DeepSeek 返回了空内容。"
            )
        except requests.RequestException as exc:
            logger.warning("DeepSeek API 请求失败：%s", exc)
            return "DeepSeek 请求失败，请稍后再试。"
        except (KeyError, IndexError, TypeError, ValueError):
            logger.exception("DeepSeek API 响应格式异常")
            return "DeepSeek 响应格式异常，请稍后再试。"


def _normalize_messages(messages: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    if not messages:
        return []

    normalized_messages: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            return []

        role = str(message.get("role", "")).strip()
        content = str(message.get("content", "")).strip()
        if role not in VALID_MESSAGE_ROLES or not content:
            return []

        normalized_messages.append({"role": role, "content": content})

    return normalized_messages


# 说明：如需使用更强的 deepseek-v4-pro，可在 .env 中设置：
# DEEPSEEK_MODEL=deepseek-v4-pro
