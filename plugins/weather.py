from __future__ import annotations

from doraymon.context import BotContext
from services.weather_service import get_weather_placeholder


def handle(context: BotContext) -> str:
    city = context.args.strip() or "未指定城市"
    return get_weather_placeholder(city)
