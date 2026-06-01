# 插件开发

插件放在 `plugins/` 目录。插件只处理命令逻辑并返回文本，发送消息的工作交给 `doraymon/client.py`。

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
