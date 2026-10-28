from datetime import datetime, timezone
import unittest

from doraymon.config import Settings
from services.conversation_types import ConversationContext
from storage.knowledge_store import KnowledgeSearchResult
from services.prompt_builder import build_prompt


class PromptBuilderTest(unittest.TestCase):
    def test_local_question_has_a_default_while_retaining_the_users_place(self):
        ctx = ConversationContext(scene="private", user_openid="user")
        bundle = build_prompt(ctx, "我在示例广场，附近适合散步吗？", Settings())
        system = bundle.messages[0]["content"]
        self.assertIn("江西师范大学瑶湖校区", system)
        self.assertIn("其他地点优先", system)
        self.assertEqual(bundle.messages[-1]["content"], "我在示例广场，附近适合散步吗？")

    def test_location_and_model_come_from_the_actual_configuration(self):
        ctx = ConversationContext(scene="private", user_openid="user")
        settings = Settings(default_location="示例大学北校区", deepseek_model="configured-model")
        system = build_prompt(ctx, "你是什么模型", settings).messages[0]["content"]
        self.assertIn("示例大学北校区", system)
        self.assertIn("configured-model", system)
        self.assertNotIn("江西师范大学", system)

    def test_runtime_information_matches_actual_settings_without_identifiers(self):
        ctx = ConversationContext(scene="group", user_openid="private-user-id", group_openid="private-group-id",
                                  now=datetime(2026, 9, 30, 18, 5, tzinfo=timezone.utc))
        settings = Settings(chat_history_enabled=True, chat_context_ttl_minutes=30,
                            chat_history_limit=16, command_prefix="!", rag_enabled=True)
        bundle = build_prompt(ctx, "你好", settings)
        system = bundle.messages[0]["content"]
        self.assertIn("2026-10-01 02:05", system)
        self.assertIn("场景：群聊", system)
        self.assertIn("16 条消息", system)
        self.assertIn("30 分钟", system)
        self.assertIn("!清空上下文", system)
        self.assertIn("可选知识库：已启用", system)
        self.assertNotIn(ctx.user_openid, system)
        self.assertNotIn(ctx.group_openid, system)
        self.assertEqual(bundle.messages[-1], {"role": "user", "content": "你好"})

    def test_disabled_or_unidentified_history_does_not_claim_saving(self):
        for enabled, user in ((False, "user"), (True, "")):
            ctx = ConversationContext(scene="private", user_openid=user)
            system = build_prompt(ctx, "你好", Settings(chat_history_enabled=enabled)).messages[0]["content"]
            self.assertIn("本轮不读取或保存", system)
            self.assertIn("既有记录已删除", system)

    def test_nonpositive_ttl_does_not_promise_expiry(self):
        ctx = ConversationContext(scene="private", user_openid="user")
        system = build_prompt(ctx, "你好", Settings(chat_history_enabled=True, chat_context_ttl_minutes=0)).messages[0]["content"]
        self.assertIn("不按闲置时长自动清理", system)

    def test_summary_is_optional_and_cannot_replace_latest_user_message(self):
        ctx = ConversationContext(scene="private", user_openid="user", summary="合成旧事实")
        system = build_prompt(ctx, "合成新问题", Settings()).messages[0]["content"]
        self.assertLess(system.index("本次运行信息"), system.index("合成旧事实"))
        self.assertIn("以最近对话为准", system)
        empty = build_prompt(ConversationContext(scene="private", user_openid="user"), "你好", Settings())
        self.assertNotIn("本地压缩摘要", empty.messages[0]["content"])

    def test_model_options_are_more_open_for_chat_and_lower_for_knowledge(self):
        settings = Settings(deepseek_temperature=0.7)
        normal = build_prompt(
            ConversationContext(scene="private", user_openid="user"),
            "你好",
            settings,
        )
        knowledge = KnowledgeSearchResult(
            chunk_id=1,
            document_id=1,
            source_path="setup.md",
            title="启动说明",
            heading="启动",
            content="运行启动脚本。",
            scope_type="public",
            scope_openid="",
            score=-3.0,
        )
        grounded = build_prompt(
            ConversationContext(scene="private", user_openid="user", knowledge=(knowledge,)),
            "怎么启动？",
            settings,
        )

        self.assertAlmostEqual(normal.options["temperature"], 0.8)
        self.assertEqual(normal.options["max_tokens"], 8192)
        self.assertEqual(grounded.options, {"temperature": 0.4, "max_tokens": 4096})
