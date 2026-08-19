from __future__ import annotations

from doraymon.context import BotContext


def handle(context: BotContext) -> str:
    prefix = context.settings.command_prefix
    return "\n".join(
        [
            "DoraYmon 可用命令：",
            "",
            "💬 聊天",
            f"{prefix}chat 你好 - 调用 DeepSeek 聊天",
            f"{prefix}清空上下文 - 清空当前会话的短期上下文",
            f"{prefix}上下文状态 - 查看当前会话的上下文状态",
            "私聊普通文本、群聊 @ 普通文本也会进入 AI 聊天",
            "",
            "📚 知识库",
            f"{prefix}知识问 如何部署 - 基于本地资料回答并附来源",
            f"{prefix}知识来源 如何部署 - 只查看检索到的来源",
            f"{prefix}知识库状态 - 查看文档、分块和索引状态",
            "",
            "🍜 生活",
            f"{prefix}天气 南昌 - 天气功能占位",
            f"{prefix}今日运势 - 运势功能占位",
            f"{prefix}签到 - 每日签到",
            f"{prefix}我的签到 - 查看签到统计",
            f"{prefix}吃什么 今天很累 - 推荐一款适合当前状态的食物",
            "私聊也可以直接问：今晚吃什么",
            f"{prefix}记住口味 我喜欢辣 - 保存明确提交的口味",
            f"{prefix}我的口味 - 查看已保存的口味",
            f"{prefix}忘记口味 我喜欢辣 - 删除一条口味",
            "",
            "🧰 工具",
            f"{prefix}help - 查看命令列表",
            f"{prefix}ping - 测试机器人是否在线",
            f"{prefix}status - 查看运行状态",
            f"{prefix}todo - 待办功能占位",
            "",
            "🔐 管理",
            f"{prefix}重建知识库 - 从本地资料重建索引（仅管理员）",
            f"{prefix}admin status - 管理员状态命令",
        ]
    )
