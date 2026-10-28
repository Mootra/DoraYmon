from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from pathlib import Path

import requests

from doraymon.config import Settings
from services.model_usage import MODEL_USAGE

logger = logging.getLogger(__name__)
DEFAULT_SYSTEM_PROMPT = (
    Path(__file__).resolve().parent.parent / "resources/persona/default.md"
).read_text(encoding="utf-8").strip()
VALID_MESSAGE_ROLES = {"system", "user", "assistant"}


def is_model_error(raw: str) -> bool:
    """Recognize the legacy transport error protocol in one place."""
    return str(raw or "").startswith("DeepSeek ")


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

    def chat_messages(
        self, messages: Sequence[dict[str, str]], *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> str:
        if not self.settings.deepseek_api_key:
            return "DeepSeek API Key 未配置，请检查 .env。"

        # Check the daily budget before spending anything, and say so when it is
        # exhausted: a bot that goes quiet without explaining is indistinguishable
        # from a broken one.
        decision = MODEL_USAGE.check()
        if not decision.allowed:
            logger.warning("模型请求被每日额度拒绝：%s", decision.describe())
            return decision.describe()

        normalized_messages = _normalize_messages(messages)
        if not normalized_messages:
            return "DeepSeek 请求格式错误，请检查消息内容。"

        url = f"{self.settings.deepseek_base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model or self.settings.deepseek_model or "deepseek-v4-flash",
            "messages": normalized_messages,
            "temperature": self.settings.deepseek_temperature if temperature is None else temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            # Recorded even when the provider reports no usage: the request still
            # happened, and losing that fact would make the daily picture wrong in
            # the direction that matters (fewer requests than were really made).
            usage = MODEL_USAGE.token_usage_from(data)
            MODEL_USAGE.record(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                model=str(payload.get("model", "")),
            )
            choice = data["choices"][0]
            content = choice["message"]["content"].strip()
            if choice.get("finish_reason") == "length":
                if not content:
                    return "DeepSeek 本次生成达到长度上限，未能给出正文，请缩小问题范围后重试。"
                return content + "\n\n[生成达到长度上限，回答未完成]"
            return content or "DeepSeek 返回了空内容。"
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
