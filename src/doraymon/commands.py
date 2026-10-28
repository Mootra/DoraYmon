import aiosqlite

from .config import Settings
from .models import Incoming
from .store import Store

MAX_PREFERENCE_CHARS = 100
MAX_PREFERENCES = 20
STORAGE_ERROR = "本机记录暂时不可用，请稍后再试。"

HELP_TEXT = (
    "/chat 问题 · /吃什么 · /清空上下文 · /上下文状态\n"
    "/知识问 问题 · /知识来源 问题\n"
    "/记住口味 口味 · /我的口味 · /忘记口味 口味\n"
    "/签到 · /我的签到\n"
    "/停止 · /ping · /status · /help"
)


def _argument(event: Incoming) -> str:
    parts = event.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _remember(event: Incoming, store: Store) -> str:
    preference = _argument(event)
    if not preference:
        return "请告诉我要记住什么，例如：/记住口味 我不吃香菜"
    if len(preference) > MAX_PREFERENCE_CHARS:
        return f"口味描述请控制在 {MAX_PREFERENCE_CHARS} 个字符以内。"
    result = await store.add_food_preference(event.session, preference, limit=MAX_PREFERENCES)
    if result == "full":
        return f"最多保存 {MAX_PREFERENCES} 条口味，请先删除不需要的记录。"
    if result == "exists":
        return f"已经记住了：{preference}"
    return f"记住了：{preference}"


async def _list(event: Incoming, store: Store) -> str:
    preferences = await store.food_preferences(event.session)
    if not preferences:
        return "你还没有保存口味。可以发送：/记住口味 我喜欢辣"
    listed = "\n".join(f"{index}. {preference}" for index, preference in enumerate(preferences, 1))
    return "你保存的口味：\n" + listed


async def _forget(event: Incoming, store: Store) -> str:
    preference = _argument(event)
    if not preference:
        return "请输入要忘记的完整口味，例如：/忘记口味 我不吃香菜"
    if not await store.remove_food_preference(event.session, preference):
        return "没有找到完全相同的口味记录，请先发送 /我的口味 查看。"
    return f"已经忘记：{preference}"


async def _sign(event: Incoming, store: Store) -> str:
    result = await store.sign_today(event.session)
    if result.created:
        return f"签到成功！这是你第 {result.total_days} 次签到。"
    return f"今天已经签到过啦。累计签到 {result.total_days} 天。"


async def _my_sign(event: Incoming, store: Store) -> str:
    summary = await store.sign_summary(event.session)
    return f"你的累计签到：{summary.total_days} 天。最近签到：{summary.last_sign_date or '暂无'}。"


MAPPING = {
    "/记住口味": _remember,
    "/我的口味": _list,
    "/忘记口味": _forget,
    "/签到": _sign,
    "/我的签到": _my_sign,
}


async def local_reply(event: Incoming, store: Store, settings: Settings) -> str | None:
    command = event.text.split(maxsplit=1)[0]
    if command == "/ping":
        return "pong"
    if command == "/help":
        return HELP_TEXT
    if command == "/status":
        return "QQ 账号入口；知识库" + ("已启用。" if settings.knowledge_enabled else "未启用。")
    if command == "/上下文状态":
        return f"当前会话保留 {len(await store.history(event.session))} 个已投递轮次。"
    handler = MAPPING.get(command)
    if handler is not None:
        try:
            return await handler(event, store)
        except aiosqlite.Error:
            return STORAGE_ERROR
        except ValueError as error:
            # aiosqlite 0.22.1 uses ValueError for closed connections.
            if str(error) in {"no active connection", "Connection closed"}:
                return STORAGE_ERROR
            raise
    return None
