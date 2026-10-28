from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from services.computer_control_guard import claimed_by_computer_bridge


class ComputerControlGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "lease.json"
        self.environment = patch.dict(os.environ, {"BOT_PC_BRIDGE_LEASE_PATH": str(self.path)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def event(self, text, user=200, message_type="private"):
        return {"message_type": message_type, "user_id": user,
                "message": [{"type": "text", "data": {"text": text}}]}

    def lease(self, **changes):
        self.path.write_text(json.dumps({
            "owner_id": "200", "active": True, "expires": time.time() + 30, **changes,
        }), encoding="utf-8")

    def test_claims_only_owner_private_chat_with_live_lease(self):
        self.lease()
        self.assertTrue(claimed_by_computer_bridge(self.event("继续刚才的任务")))
        self.assertFalse(claimed_by_computer_bridge(self.event("继续", user=300)))
        self.assertFalse(claimed_by_computer_bridge(self.event("继续", message_type="group")))
        self.lease(expires=time.time() - 1)
        self.assertFalse(claimed_by_computer_bridge(self.event("继续")))

    def test_inactive_mode_claims_entry_and_exit_but_allows_normal_chat(self):
        self.lease(active=False)
        for text in ("电脑进度", "用 Codex 修复", "用DSH", "退出电脑", "菜单", "C", "d 整理文件", "Codex"):
            self.assertTrue(claimed_by_computer_bridge(self.event(text)))
        self.assertFalse(claimed_by_computer_bridge(self.event("吃什么")))
        self.assertFalse(claimed_by_computer_bridge(self.event("C++ 怎么编译")))
        self.assertFalse(claimed_by_computer_bridge(self.event("用 Codex 修复", user=300)))

    def test_pairing_is_reserved_before_owner_binding(self):
        self.lease(owner_id=None, active=False)
        self.assertTrue(claimed_by_computer_bridge(self.event("连接电脑 1234567890abcdef")))
        self.assertFalse(claimed_by_computer_bridge(self.event("你好")))

    def test_missing_or_invalid_lease_falls_back_to_regular_bot(self):
        self.assertFalse(claimed_by_computer_bridge(self.event("你好")))
        self.path.write_text("{", encoding="utf-8")
        self.assertFalse(claimed_by_computer_bridge(self.event("你好")))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(claimed_by_computer_bridge(self.event("你好")))
