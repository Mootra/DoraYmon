from __future__ import annotations

from doraymon.context import BotContext
from services.food_recommend_service import recommend_food


def handle(context: BotContext) -> str:
    recommendation = recommend_food(context.args)
    return recommendation.text
