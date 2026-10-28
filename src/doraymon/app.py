import asyncio
import logging
import re

from cachetools import TTLCache

from .clients import Clients
from .commands import local_reply
from .config import Settings
from .flow import BusyError, Conversations, Sender
from .meals import leaves_meal, recommend, wants_meal
from .models import Incoming
from .policy import ACK, STOP, Policy
from .prompt import build_messages
from .store import Store

logger = logging.getLogger(__name__)
RESET = ("换个话题", "忽略之前的对话", "重新开始")


class Application:
    def __init__(self, settings: Settings, store: Store, clients: Clients):
        self.settings, self.store, self.clients = settings, store, clients
        self.policy = Policy(settings)
        self.seen: TTLCache = TTLCache(maxsize=4096, ttl=600)
        self.meals: TTLCache = TTLCache(maxsize=256, ttl=1800)
        self.slots = asyncio.Semaphore(settings.model_concurrency)
        self.silenced = False
        self.flow = Conversations(
            self._process,
            max_sessions=settings.max_sessions,
            queue_size=settings.session_queue,
            gap=settings.merge_gap,
            window=settings.merge_window,
        )

    async def handle(self, event: Incoming, send: Sender) -> bool:
        if not event.text.strip() or not self.policy.allowed(event):
            return False
        if self.policy.bridge_claims(event):
            return False
        identity = event.session, event.message_id
        if identity in self.seen:
            return False
        self.seen[identity] = True
        self.policy.observe(event)
        command = event.text.split(maxsplit=1)[0]
        reset = event.text.startswith(RESET) and bool(self.policy.reason(event))
        closing = (
            bool(event.group_id)
            and event.session in self.flow.lanes
            and bool(ACK.fullmatch(event.text))
            and not self.policy.called(event)
        )
        control = (
            reset
            or closing
            or command in {"/清空上下文", "/停止", "/静默"}
            or bool(STOP.fullmatch(event.text))
        )
        if self.policy.addressed_to_other(event):
            return False
        continuation = (
            event.session in self.flow.lanes
            and event.session not in self.policy.muted
            and not ACK.fullmatch(event.text)
        )
        if not control and (self.silenced or not (self.policy.reason(event) or continuation)):
            return False
        try:
            return await self.flow.submit(event, send, control=self._control if control else None)
        except BusyError:
            self.seen.pop(identity, None)
            await send("当前请求较多，请稍后再试。")
            return False
        except Exception as error:
            # Network exceptions may include credential-bearing request URLs.
            logger.error("请求处理失败：%s", type(error).__name__)
            return False

    async def _control(self, event: Incoming, send: Sender) -> bool:
        if event.group_id and ACK.fullmatch(event.text):
            self.policy.active.pop(event.session, None)
            self.meals.pop(event.session, None)
            return False
        if event.text.startswith(("/清空上下文", *RESET)):
            await self.store.clear(event.session)
            self.meals.pop(event.session, None)
            self.policy.active.pop(event.session, None)
            self.policy.muted.pop(event.session, None)
            if event.text.startswith(RESET):
                return await self._process(event.model_copy(update={"addressed": True}), send)
            await send("当前会话已清空。")
        elif event.text.startswith("/静默"):
            if event.user_id not in self.settings.admin_users:
                await send("此操作需要管理员权限。")
                return False
            mode = event.text.partition(" ")[2].strip()
            if mode not in {"开", "关"}:
                await send("用法：/静默 开 或 /静默 关")
                return False
            self.silenced = mode == "开"
            if self.silenced:
                for key in tuple(self.flow.lanes):
                    if key != event.session:
                        await self.flow.cancel(key)
            await send("已静默。" if self.silenced else "已恢复回复。")
        else:
            self.policy.stop(event)
            self.meals.pop(event.session, None)
            await send("好，先不接话。")
        return True

    async def _process(self, event: Incoming, send: Sender) -> bool:
        if self.silenced:
            return False
        reason = self.policy.reason(event)
        if not reason or not await self.policy.admit(event, reason):
            return False
        command, _, args = event.text.partition(" ")
        if (text := await local_reply(event, self.store, self.settings)) is not None:
            await send(text)
            return True
        if command.startswith("/") and command not in {"/chat", "/吃什么", "/知识问", "/知识来源"}:
            await send("未知命令，发送 /help 查看可用命令。")
            return False
        request = args.strip() if command.startswith("/") else event.text.strip()
        if command == "/吃什么" and not request:
            request = "吃什么"
        if not request:
            await send("请在命令后写上问题。")
            return False
        incoming = event.model_copy(update={"text": request})
        if leaves_meal(request):
            self.meals.pop(event.session, None)
        next_meal = None
        try:
            async with self.slots:
                if command == "/吃什么" or (
                    not command.startswith("/")
                    and wants_meal(request, self.meals.get(event.session))
                ):
                    reply, next_meal = await recommend(
                        request,
                        self.meals.get(event.session),
                        self.clients,
                        self.settings,
                    )
                else:
                    strict = command == "/知识问"
                    passages = (
                        await self.store.search(request, event.scopes)
                        if self.settings.knowledge_enabled
                        else []
                    )
                    if command == "/知识来源":
                        await send(
                            "\n".join(p.source for p in passages) or "没有检索到可访问的资料。"
                        )
                        return True
                    if strict and not passages:
                        await send("没有检索到可访问的相关资料，暂时无法依据知识库回答。")
                        return True
                    prompt = build_messages(
                        incoming,
                        await self.store.history(event.session),
                        passages,
                        budget=self.settings.context_chars,
                        group_context=self.policy.context(event),
                        reply_chars=self.settings.reply_chars,
                        strict=strict,
                    )
                    if strict and not prompt.sources:
                        await send("资料超出当前上下文预算，无法依据知识库回答。")
                        return False
                    reply = await self.clients.complete(prompt.messages)
                    if prompt.sources:
                        used = [int(n) for n in re.findall(r"\[(\d+)\]", reply)]
                        cited = [source for i, source in enumerate(prompt.sources, 1) if i in used]
                        if cited:
                            reply += "\n来源：" + "；".join(cited)
        except Exception as error:
            logger.warning("外部服务失败：%s", type(error).__name__)
            recovery = {
                "model_key_missing": "模型尚未配置，请管理员检查模型设置。",
                "location_ambiguous": "这个地点有多个匹配，请补充城市或更完整的地址。",
                "location_not_found": "没有查到这个起点，请换一个更完整的地址或地标。",
            }.get(str(error), "这次查询失败，请稍后再试。")
            await send(recovery)
            return False
        if len(reply) > self.settings.reply_chars:
            marker = "\n[回复超出发送上限]"
            reply = reply[: self.settings.reply_chars - len(marker)] + marker
        for start in range(0, len(reply), 1500):
            await send(reply[start : start + 1500])
        await self.store.append(event.session, event.message_id, request, reply)
        if next_meal is not None:
            self.meals[event.session] = next_meal
        self.policy.delivered(event)
        return True

    async def close(self) -> None:
        await self.flow.close()
