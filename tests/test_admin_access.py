"""One admin list must work for both entries.

The official entry identifies a speaker by openid and the account entry by QQ
number, and the stored identity is namespaced. An operator cannot be expected to
know which form applies, and a list that matches nobody leaves the silence switch
unusable on a live account, which is the one control that matters then.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from doraymon.config import Settings
from doraymon.context import BotContext
from services.admin_access import admin_identifiers, is_admin
from plugins import admin as admin_plugin
from plugins import silence as silence_plugin
from services.silence import SILENCE


def _context(*, user_openid: str, group_openid: str = "", platform: str = "",
             admin_openids: list[str] | None = None, args: str = "") -> BotContext:
    return BotContext(
        settings=Settings(
            command_prefix="/",
            admin_openids=admin_openids if admin_openids is not None else [],
            chat_history_enabled=False,
        ),
        started_at=datetime.now(),
        user_openid=user_openid,
        group_openid=group_openid,
        platform=platform,
        bot_account="3963059558",
        raw_content="/静默 status",
        command="静默",
        args=args,
    )


class AdminIdentifiersTest(unittest.TestCase):
    def test_a_bare_account_id_is_an_identifier(self) -> None:
        """The account entry reports a QQ number."""
        identifiers = admin_identifiers(_context(user_openid="10001", platform="qqaccount"))

        self.assertIn("10001", identifiers)

    def test_the_namespaced_identity_is_also_an_identifier(self) -> None:
        """Storage namespaces it, and an operator may paste the namespaced form."""
        identifiers = admin_identifiers(
            _context(user_openid="10001", platform="qqaccount")
        )

        self.assertTrue(
            any(":" in item and item.endswith("10001") for item in identifiers),
            identifiers,
        )

    def test_an_empty_context_yields_nothing(self) -> None:
        self.assertEqual(admin_identifiers(_context(user_openid="")), ())


class IsAdminTest(unittest.TestCase):
    def test_a_bare_id_match_works(self) -> None:
        """This is what an operator writes for the account entry."""
        context = _context(
            user_openid="10001", platform="qqaccount", admin_openids=["10001"]
        )

        self.assertTrue(is_admin(context))

    def test_a_namespaced_match_works(self) -> None:
        """And this is what an operator ends up with for the official entry."""
        context = _context(user_openid="10001", platform="qqaccount")
        namespaced = next(item for item in admin_identifiers(context) if ":" in item)

        self.assertTrue(is_admin(_context(
            user_openid="10001", platform="qqaccount", admin_openids=[namespaced]
        )))

    def test_an_unrelated_id_is_refused(self) -> None:
        context = _context(user_openid="10001", admin_openids=["10002"])

        self.assertFalse(is_admin(context))

    def test_an_empty_list_means_nobody(self) -> None:
        """A control guarding a live account must not be open by default."""
        self.assertFalse(is_admin(_context(user_openid="10001", admin_openids=[])))

    def test_angle_brackets_and_spaces_are_tolerated(self) -> None:
        """Both are easy to paste in by accident."""
        context = _context(user_openid="10001", admin_openids=[" <10001> "])

        self.assertTrue(is_admin(context))

    def test_blank_entries_do_not_grant_access(self) -> None:
        context = _context(user_openid="", admin_openids=["", "   "])

        self.assertFalse(is_admin(context))


class PluginWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        SILENCE.clear()
        self.addCleanup(SILENCE.clear)

    def test_the_silence_switch_accepts_a_bare_account_id(self) -> None:
        """The exact case that would otherwise leave a live account un-mutable."""
        reply = silence_plugin.handle(
            _context(user_openid="10001", args="on", admin_openids=["10001"])
        )

        self.assertIn("已静默", reply)
        self.assertTrue(SILENCE.silent)

    def test_a_stranger_cannot_silence_the_bot(self) -> None:
        reply = silence_plugin.handle(
            _context(user_openid="10009", args="on", admin_openids=["10001"])
        )

        self.assertIn("没有权限", reply)
        self.assertFalse(SILENCE.silent)

    def test_the_admin_plugin_uses_the_same_rule(self) -> None:
        allowed = admin_plugin.handle(
            _context(user_openid="10001", admin_openids=["10001"])
        )
        refused = admin_plugin.handle(
            _context(user_openid="10009", admin_openids=["10001"])
        )

        self.assertNotIn("没有权限", allowed)
        self.assertIn("没有权限", refused)


if __name__ == "__main__":
    unittest.main()
