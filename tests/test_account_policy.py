"""QQ normal chat and explicit closed allowlists keep separate access rules."""

from __future__ import annotations

import unittest

from doraymon.config import Settings
from doraymon.account_entry import build_policy, validate_account_settings
from services.account_policy import (
    DECISION_ALLOWED,
    DECISION_EMPTY_ALLOWLIST,
    DECISION_GROUP_MISSING,
    DECISION_USER_MISSING,
    AccountAccessPolicy,
)
from services.message_contract import ConversationKind, MessageEvent


def _group_event(*, group: str = "555", user: str = "67890") -> MessageEvent:
    return MessageEvent(
        platform="qqaccount",
        bot_account="12345",
        kind=ConversationKind.GROUP,
        message_id="m-1",
        raw_user_id=user,
        text="你好",
        raw_group_id=group,
        mentions_bot=True,
    )


def _private_event(*, user: str = "67890") -> MessageEvent:
    return MessageEvent(
        platform="qqaccount",
        bot_account="12345",
        kind=ConversationKind.PRIVATE,
        message_id="m-1",
        raw_user_id=user,
        text="你好",
    )


class EmptyAllowlistTest(unittest.TestCase):
    def test_no_allowlist_denies_everything(self) -> None:
        policy = AccountAccessPolicy()

        for event in (_group_event(), _private_event()):
            with self.subTest(event=event.kind):
                decision = policy.decide(event)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, DECISION_EMPTY_ALLOWLIST)

    def test_empty_allowlist_is_reported_as_a_startup_problem(self) -> None:
        problems = validate_account_settings(
            Settings(qq_account_access_mode="allowlist", qq_account_self_id="12345", qq_account_ws_url="ws://127.0.0.1:3001")
        )

        self.assertTrue(any("允许列表为空" in problem for problem in problems))


class GroupPolicyTest(unittest.TestCase):
    def test_listed_group_is_allowed(self) -> None:
        policy = AccountAccessPolicy(allowed_groups=["555"])

        decision = policy.decide(_group_event(group="555"))

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, DECISION_ALLOWED)

    def test_unlisted_group_is_denied(self) -> None:
        policy = AccountAccessPolicy(allowed_groups=["555"])

        decision = policy.decide(_group_event(group="999"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, DECISION_GROUP_MISSING)

    def test_listed_group_with_listed_user_is_allowed(self) -> None:
        policy = AccountAccessPolicy(allowed_groups=["555"], allowed_users=["67890"])

        self.assertTrue(policy.decide(_group_event(group="555", user="67890")).allowed)

    def test_listed_group_with_unlisted_user_is_denied(self) -> None:
        """A whitelisted group must not implicitly admit every member."""
        policy = AccountAccessPolicy(allowed_groups=["555"], allowed_users=["67890"])

        decision = policy.decide(_group_event(group="555", user="11111"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, DECISION_USER_MISSING)

    def test_listed_group_without_a_user_list_admits_members(self) -> None:
        policy = AccountAccessPolicy(allowed_groups=["555"])

        self.assertTrue(policy.decide(_group_event(group="555", user="11111")).allowed)

    def test_allow_all_groups_admits_any_group(self) -> None:
        policy = AccountAccessPolicy(allow_all_groups=True)

        self.assertTrue(policy.decide(_group_event(group="999")).allowed)

    def test_group_allow_all_still_requires_a_listed_user_when_one_exists(self) -> None:
        policy = AccountAccessPolicy(allow_all_groups=True, allowed_users=["67890"])

        self.assertTrue(policy.decide(_group_event(user="67890")).allowed)
        self.assertFalse(policy.decide(_group_event(user="11111")).allowed)


class PrivatePolicyTest(unittest.TestCase):
    def test_listed_friend_is_allowed(self) -> None:
        policy = AccountAccessPolicy(allowed_users=["67890"])

        self.assertTrue(policy.decide(_private_event(user="67890")).allowed)

    def test_unlisted_friend_is_denied(self) -> None:
        policy = AccountAccessPolicy(allowed_users=["67890"])

        decision = policy.decide(_private_event(user="11111"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, DECISION_USER_MISSING)

    def test_group_allowlist_does_not_admit_private_messages(self) -> None:
        policy = AccountAccessPolicy(allowed_groups=["555"])

        decision = policy.decide(_private_event())

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, DECISION_USER_MISSING)

    def test_allow_all_users_admits_any_private_sender(self) -> None:
        policy = AccountAccessPolicy(allow_all_users=True)

        self.assertTrue(policy.decide(_private_event(user="11111")).allowed)


class PolicyNormalizationTest(unittest.TestCase):
    def test_whitespace_and_empty_entries_are_ignored(self) -> None:
        policy = AccountAccessPolicy(allowed_groups=[" 555 ", "", "  "])

        self.assertEqual(policy.allowed_groups, frozenset({"555"}))
        self.assertTrue(policy.decide(_group_event(group="555")).allowed)

    def test_a_single_string_is_accepted(self) -> None:
        policy = AccountAccessPolicy(allowed_users="67890")

        self.assertTrue(policy.decide(_private_event(user="67890")).allowed)

    def test_has_any_allowlist_reports_configuration_presence(self) -> None:
        self.assertFalse(AccountAccessPolicy().has_any_allowlist)
        self.assertTrue(AccountAccessPolicy(allowed_groups=["555"]).has_any_allowlist)
        self.assertTrue(AccountAccessPolicy(allow_all_users=True).has_any_allowlist)

    def test_decision_description_is_human_readable(self) -> None:
        denied = AccountAccessPolicy().decide(_private_event())

        self.assertIn("默认拒绝", denied.describe())


class SettingsValidationTest(unittest.TestCase):
    def _valid(self, **overrides) -> Settings:
        payload = {
            "qq_account_self_id": "12345",
            "qq_account_ws_url": "ws://127.0.0.1:3001",
            "qq_account_groups": ["555"],
        }
        payload.update(overrides)
        return Settings(**payload)

    def test_valid_settings_report_no_problems(self) -> None:
        self.assertEqual(validate_account_settings(self._valid()), [])

    def test_missing_self_id_is_reported(self) -> None:
        problems = validate_account_settings(self._valid(qq_account_self_id=""))

        self.assertTrue(any("SELF_ID" in problem for problem in problems))

    def test_missing_ws_url_is_reported(self) -> None:
        problems = validate_account_settings(self._valid(qq_account_ws_url=""))

        self.assertTrue(any("WS_URL" in problem for problem in problems))

    def test_non_websocket_url_is_reported(self) -> None:
        problems = validate_account_settings(self._valid(qq_account_ws_url="http://127.0.0.1"))

        self.assertTrue(any("ws://" in problem for problem in problems))

    def test_secure_websocket_url_is_accepted(self) -> None:
        self.assertEqual(
            validate_account_settings(self._valid(qq_account_ws_url="wss://example.invalid/onebot")),
            [],
        )

    def test_disabled_by_default(self) -> None:
        self.assertFalse(Settings().qq_account_enabled)

    def test_account_settings_load_from_environment(self) -> None:
        import os
        from unittest.mock import patch

        from doraymon.config import load_settings

        with patch.dict(
            os.environ,
            {
                "BOT_QQ_ACCOUNT_ENABLED": "true",
                "BOT_QQ_ACCOUNT_SELF_ID": "12345",
                "BOT_QQ_ACCOUNT_WS_URL": "ws://127.0.0.1:3001",
                "BOT_QQ_ACCOUNT_GROUPS": "555,666",
                "BOT_QQ_ACCOUNT_FRIENDS": "67890",
                "BOT_QQ_ACCOUNT_GROUP_REPLY_MODE": "text",
            },
        ), patch("doraymon.config.load_dotenv"), patch(
            "doraymon.config._load_yaml_config", return_value={}
        ):
            settings = load_settings()

        self.assertTrue(settings.qq_account_enabled)
        self.assertEqual(settings.qq_account_self_id, "12345")
        self.assertEqual(settings.qq_account_groups, ["555", "666"])
        self.assertEqual(settings.qq_account_friends, ["67890"])
        self.assertEqual(settings.qq_account_group_reply_mode, "text")

    def test_build_policy_uses_the_configured_lists(self) -> None:
        policy = build_policy(Settings(qq_account_groups=["555"], qq_account_friends=["67890"]))

        self.assertTrue(policy.decide(_group_event(group="555")).allowed)
        self.assertTrue(policy.decide(_private_event(user="67890")).allowed)
        self.assertFalse(policy.decide(_group_event(group="999")).allowed)


if __name__ == "__main__":
    unittest.main()


class StringAllowlistTest(unittest.TestCase):
    """A string allowlist must never become a character allowlist."""

    def test_comma_separated_string_is_split(self) -> None:
        from doraymon.config import Settings

        policy = build_policy(
            Settings(qq_account_groups="30003,30004", qq_account_friends="10001")
        )

        self.assertEqual(policy.allowed_groups, frozenset({"30003", "30004"}))
        self.assertEqual(policy.allowed_users, frozenset({"10001"}))

    def test_a_single_id_string_does_not_become_digits(self) -> None:
        """The trap: "30003" iterates into {"3", "0"} and allows nothing real."""
        from doraymon.config import Settings

        policy = build_policy(Settings(qq_account_groups="30003"))

        self.assertEqual(policy.allowed_groups, frozenset({"30003"}))
        self.assertNotIn("3", policy.allowed_groups)
        self.assertNotIn("0", policy.allowed_groups)

    def test_a_list_is_still_accepted(self) -> None:
        from doraymon.config import Settings

        policy = build_policy(Settings(qq_account_friends=["10001", "10002"]))

        self.assertEqual(policy.allowed_users, frozenset({"10001", "10002"}))

    def test_blank_entries_are_dropped(self) -> None:
        from doraymon.config import Settings

        policy = build_policy(Settings(qq_account_groups="30003,,  ,30004"))

        self.assertEqual(policy.allowed_groups, frozenset({"30003", "30004"}))

    def test_an_empty_string_has_no_allowlist(self) -> None:
        from doraymon.config import Settings

        policy = build_policy(Settings(qq_account_access_mode="allowlist", qq_account_groups="", qq_account_friends=""))

        self.assertFalse(policy.has_any_allowlist)


class NormalChatPolicyTest(unittest.TestCase):
    def test_normal_chat_accepts_unlisted_incoming_people_and_joined_groups(self):
        policy = build_policy(Settings(qq_account_friends=["67890"]))
        self.assertTrue(policy.decide(_private_event(user="11111")).allowed)
        self.assertTrue(policy.decide(_group_event(user="11111", group="999")).allowed)

    def test_configured_group_scope_is_retained_without_filtering_its_members(self):
        policy = build_policy(Settings(qq_account_groups=["555"], qq_account_friends=["67890"]))
        self.assertTrue(policy.decide(_group_event(user="11111")).allowed)
        self.assertFalse(policy.decide(_group_event(group="999", user="11111")).allowed)
        self.assertTrue(policy.decide(_private_event(user="11111")).allowed)

    def test_empty_lists_can_start_in_chat_mode(self):
        self.assertEqual(validate_account_settings(Settings(qq_account_self_id="12345")), [])

    def test_invalid_mode_is_rejected(self):
        self.assertTrue(any("ACCESS_MODE" in problem for problem in validate_account_settings(
            Settings(qq_account_self_id="12345", qq_account_access_mode="typo"),
        )))

    def test_allowlist_mode_still_restricts_people(self):
        policy = build_policy(Settings(
            qq_account_access_mode="allowlist", qq_account_groups=["555"],
            qq_account_friends=["67890"],
        ))
        self.assertFalse(policy.decide(_private_event(user="11111")).allowed)
        self.assertFalse(policy.decide(_group_event(user="11111")).allowed)
