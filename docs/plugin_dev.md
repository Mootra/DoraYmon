# 插件开发

插件放在 `plugins/` 目录，处理命令逻辑并返回文本。发送消息交给官方客户端或 QQ 账号/飞书会话层。

## 新增插件

1. 新建文件，例如 `plugins/echo.py`。
2. 编写处理函数：

```python
from doraymon.context import BotContext


def handle(context: BotContext) -> str:
    return context.args or "请输入内容"
```

3. 在 `doraymon/router.py` 注册：

```python
from plugins import echo

COMMANDS = {
    "echo": echo.handle,
}
```

4. 在群里发送：

```text
/echo hello
```

## 约定

- 插件不要读取 `.env` 或密钥文件。
- 插件不要执行任意系统命令。
- 数据读写放到 `storage/`。
- 外部 API 调用放到 `services/`。
- 阻塞查询使用 `services/blocking_io.py` 的受控执行器；会话状态的读取、版本核对和提交留在事件循环，不把整个插件放进线程。
- 对话插件用 `stage_reply()` 返回带 revision 的 `PendingReply`，不在生成完成时保存问答。消息出口将平台确认送达的连续内容交给 `commit_delivered_reply()`；新增出口也必须完成这一步。普通命令仍可返回纯文本。
