"""The real account entry, started as a real process.

Everything else drives objects in-process. This starts the actual command an
operator types, ``python -m scripts.run_account``, as a subprocess with a real
environment, and talks to it over a real socket. That is the only way to cover
what in-process tests cannot: configuration loading, the enabled guard, the
separate runtime data directory, logging setup, and the fact that the wiring in
the script connects the session to the transport at all.

The reply assertion stays loose on purpose. The question here is whether the
process comes up, connects, obeys the allowlist and answers, not what the model
would say.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

from tests.onebot_server import (
    OneBotTestServer,
    group_message_event,
    private_message_event,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
STARTUP_TIMEOUT = 30.0
REPLY_TIMEOUT = 60.0

ALLOWED_GROUP = 30003
ALLOWED_FRIEND = 10001
OTHER_GROUP = 40004


class AccountEntryProcessTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._temp.name)
        self.server = OneBotTestServer()
        await self.server.start()
        self.process: asyncio.subprocess.Process | None = None
        self._stderr: list[str] = []

    async def asyncTearDown(self) -> None:
        await self._terminate()
        await self.server.stop()
        self._temp.cleanup()

    # -- process control ----------------------------------------------------

    def _environment(self, **overrides) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                # Keep the run out of the repository's own data and logs.
                "DATA_DIR": str(self.runtime / "data"),
                "LOG_DIR": str(self.runtime / "logs"),
                "BOT_QQ_ACCOUNT_ENABLED": "true",
                "BOT_QQ_ACCOUNT_ACCESS_MODE": "allowlist",
                "BOT_QQ_ACCOUNT_SELF_ID": "20002",
                "BOT_QQ_ACCOUNT_WS_URL": self.server.url,
                "BOT_QQ_ACCOUNT_GROUPS": str(ALLOWED_GROUP),
                "BOT_QQ_ACCOUNT_FRIENDS": str(ALLOWED_FRIEND),
                "BOT_QQ_ACCOUNT_GROUP_COOLDOWN_SECONDS": "0",
                "BOT_QQ_ACCOUNT_GROUP_MAX_REPLIES": "8",
                "BOT_QQ_ACCOUNT_GROUP_RATE_WINDOW_SECONDS": "60",
                "QQBOT_APPID": "bot-1",
            }
        )
        env.update(overrides)
        return env

    async def _start(self, **env_overrides) -> None:
        self.process = await asyncio.create_subprocess_exec(
            str(VENV_PYTHON),
            "-u",
            "-m",
            "scripts.run_account",
            cwd=str(PROJECT_ROOT),
            env=self._environment(**env_overrides),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.ensure_future(self._drain(self.process))

    async def _drain(self, process: asyncio.subprocess.Process) -> None:
        """Keep the pipes empty so the child can never block on output."""
        if process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            self._stderr.append(line.decode("utf-8", "replace").rstrip())

    async def _terminate(self) -> None:
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None

    def _log_text(self) -> str:
        return "\n".join(self._stderr)

    # -- cases --------------------------------------------------------------

    async def test_the_entry_refuses_to_start_when_not_enabled(self) -> None:
        """The guard must hold before anything connects anywhere."""
        self.process = await asyncio.create_subprocess_exec(
            str(VENV_PYTHON),
            "-u",
            "-m",
            "scripts.run_account",
            cwd=str(PROJECT_ROOT),
            env=self._environment(BOT_QQ_ACCOUNT_ENABLED="false"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.ensure_future(self._drain(self.process))
        code = await asyncio.wait_for(self.process.wait(), timeout=STARTUP_TIMEOUT)
        self.process = None

        self.assertEqual(code, 1)
        self.assertIn("未开启", self._log_text())
        self.assertEqual(self.server.connections, 0)

    async def test_the_entry_refuses_an_empty_allowlist(self) -> None:
        """Default-deny: no allowlist means no account entry at all."""
        await self._start(BOT_QQ_ACCOUNT_GROUPS="", BOT_QQ_ACCOUNT_FRIENDS="")
        code = await asyncio.wait_for(self.process.wait(), timeout=STARTUP_TIMEOUT)
        self.process = None

        self.assertEqual(code, 1)
        self.assertIn("允许列表", self._log_text())

    async def test_the_entry_connects_and_answers_an_allowed_friend(self) -> None:
        await self._start()
        self.assertTrue(
            await self.server.wait_for_connection(timeout=STARTUP_TIMEOUT),
            f"the entry never connected; log:\n{self._log_text()}",
        )

        await self.server.push_event(
            private_message_event(user_id=ALLOWED_FRIEND, message_id=9001, message="你好")
        )
        action = await self.server.wait_for_action("send_private_msg", timeout=REPLY_TIMEOUT)

        self.assertIsNotNone(
            action,
            f"no reply; actions={self.server.action_names()}\nlog:\n{self._log_text()}",
        )
        self.assertEqual(action.params.get("user_id"), ALLOWED_FRIEND)
        # The test environment has no model key, so the reply must be the honest
        # "not configured" message rather than an invented answer. Asserting this
        # keeps the test from passing if the entry ever fabricates a reply.
        self.assertIn("未配置", str(action.params.get("message")))

    async def test_the_entry_logs_its_own_startup_configuration(self) -> None:
        """The log must show the allowlist and throttle an operator needs to see."""
        await self._start()
        self.assertTrue(await self.server.wait_for_connection(timeout=STARTUP_TIMEOUT))
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            if "QQ 账号入口启动" in self._log_text():
                break
            await asyncio.sleep(0.1)

        log = self._log_text()
        self.assertIn("QQ 账号入口使用独立数据目录", log)
        self.assertIn("QQ 账号入口启动", log)
        self.assertIn("允许群=1", log)
        self.assertIn("允许好友=1", log)

    async def test_the_entry_does_not_answer_a_group_outside_the_allowlist(self) -> None:
        await self._start()
        self.assertTrue(await self.server.wait_for_connection(timeout=STARTUP_TIMEOUT))

        await self.server.push_event(
            group_message_event(group_id=OTHER_GROUP, user_id=ALLOWED_FRIEND, message_id=9002)
        )
        await asyncio.sleep(3.0)

        self.assertEqual(
            self.server.actions,
            [],
            f"a non-allowlisted group was answered: {self.server.action_names()}",
        )

    async def test_the_entry_uses_its_own_runtime_data_directory(self) -> None:
        """It must not share a database with the official bot entry."""
        await self._start()
        self.assertTrue(await self.server.wait_for_connection(timeout=STARTUP_TIMEOUT))

        account_dir = self.runtime / "data" / "qq_account"
        self.assertTrue(
            account_dir.exists(), f"expected {account_dir} to be created"
        )
        self.assertTrue(any(account_dir.glob("*.db")) or any(account_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
