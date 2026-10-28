"""P0: platform and bot-account identity must never be conflated."""

from __future__ import annotations

import unittest

from doraymon.identity import (
    PLATFORM_QQ_ACCOUNT,
    IdentityNamespace,
    is_namespaced,
    namespace_for,
    split_namespace,
)


class IdentityNamespaceTest(unittest.TestCase):
    def test_qualify_prefixes_platform_and_bot_account(self) -> None:
        namespace = IdentityNamespace(platform="qqbot", bot_account="1904033781")

        self.assertEqual(namespace.qualify("user-a"), "qqbot:1904033781:user-a")
        self.assertTrue(namespace.matches("qqbot:1904033781:user-a"))
        self.assertFalse(namespace.matches("qqaccount::1904033781"))

    def test_qualify_is_idempotent_for_qualified_identifiers(self) -> None:
        namespace = namespace_for("qqbot", "bot-1")

        self.assertEqual(namespace.qualify("qqbot:bot-1:user-a"), "qqbot:bot-1:user-a")

    def test_private_session_key_isolates_by_user(self) -> None:
        namespace = namespace_for("qqbot", "bot-1")

        self.assertEqual(
            namespace.session_key("user-a"),
            ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a"),
        )
        self.assertNotEqual(namespace.session_key("user-a"), namespace.session_key("user-b"))

    def test_group_session_key_isolates_by_group_and_user(self) -> None:
        namespace = namespace_for("qqbot", "bot-1")

        self.assertEqual(
            namespace.session_key("user-a", "group-a"),
            ("group", "qqbot:bot-1:group-a", "qqbot:bot-1:user-a"),
        )
        self.assertNotEqual(
            namespace.session_key("user-a", "group-a"),
            namespace.session_key("user-b", "group-a"),
        )
        self.assertNotEqual(
            namespace.session_key("user-a", "group-a"),
            namespace.session_key("user-a", "group-b"),
        )

    def test_empty_user_yields_no_session(self) -> None:
        self.assertIsNone(namespace_for().session_key(""))

    def test_same_numeric_id_on_two_platforms_never_shares_a_session(self) -> None:
        """A QQ number and an official openid that look alike are different people."""
        official = namespace_for("qqbot", "bot-1")
        personal = namespace_for(PLATFORM_QQ_ACCOUNT, "bot-1")

        self.assertNotEqual(
            official.session_key("1904033781"),
            personal.session_key("1904033781"),
        )

    def test_same_platform_different_bot_account_never_shares_a_session(self) -> None:
        first = namespace_for("qqbot", "bot-1")
        second = namespace_for("qqbot", "bot-2")

        self.assertNotEqual(first.session_key("user-a"), second.session_key("user-a"))

    def test_legacy_bare_id_is_not_namespaced(self) -> None:
        """History written before namespacing must stay outside platform sessions."""
        self.assertFalse(is_namespaced("user-a"))
        self.assertEqual(split_namespace("user-a"), ("", "", "user-a"))

        official = namespace_for("qqbot", "bot-1")
        self.assertNotIn(
            "user-a",
            official.session_key("user-a"),
        )

    def test_split_namespace_round_trips_a_qualified_identifier(self) -> None:
        self.assertEqual(
            split_namespace("qqaccount:12345:67890"),
            ("qqaccount", "12345", "67890"),
        )

    def test_namespace_requires_a_platform(self) -> None:
        with self.assertRaises(ValueError):
            IdentityNamespace(platform="  ")


if __name__ == "__main__":
    unittest.main()
