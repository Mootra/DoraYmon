from __future__ import annotations

import logging
from typing import Any

import requests

from doraymon.config import Settings


logger = logging.getLogger(__name__)


class DeepSeekService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def chat(self, prompt: str) -> str:
        if not self.settings.deepseek_api_key:
            return "DeepSeek API Key 未配置，请检查 .env。"

        url = f"{self.settings.deepseek_base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.settings.deepseek_model or "deepseek-chat",
            "messages": [
                {"role": "system", "content": "你是 DoraYmon，一个简洁、友好的 QQ Bot 助手。"},
                {"role": "user", "content": prompt},
            ],
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
            return data["choices"][0]["message"]["content"].strip() or "DeepSeek 返回了空内容。"
        except requests.RequestException as exc:
            logger.warning("DeepSeek API 请求失败：%s", exc)
            return "DeepSeek 请求失败，请稍后再试。"
        except (KeyError, IndexError, TypeError, ValueError):
            logger.exception("DeepSeek API 响应格式异常")
            return "DeepSeek 响应格式异常，请稍后再试。"


# 说明：如需使用 deepseek-reasoner，可在 .env 中设置：
# DEEPSEEK_MODEL=deepseek-reasoner
