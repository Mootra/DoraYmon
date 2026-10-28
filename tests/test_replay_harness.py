"""P0: the replay harness checks must catch real regressions, not just pass."""

from __future__ import annotations

from pathlib import Path
import unittest

from scripts.replay_scenarios import (
    DEFAULT_CASES_PATH,
    assistant_owners,
    has_price_claim,
    load_scenarios,
    step_token,
)


class StepTokenTest(unittest.TestCase):
    def test_token_identifies_scenario_and_step(self) -> None:
        self.assertEqual(step_token("case-1", 3), "[case-1-3]")


class PriceClaimGuardTest(unittest.TestCase):
    def test_price_forms_are_detected(self) -> None:
        for reply in ("大概 25 元一份", "只要 12 块", "￥30 左右", "约 9.5 元"):
            with self.subTest(reply=reply):
                self.assertTrue(has_price_claim(reply))

    def test_replies_without_a_price_are_clean(self) -> None:
        for reply in ("", "可以试试砂锅米线。", "来一份热乎的，慢慢吃。"):
            with self.subTest(reply=reply):
                self.assertFalse(has_price_claim(reply))

    def test_integer_without_a_currency_marker_is_not_a_claim(self) -> None:
        self.assertFalse(has_price_claim("有 3 家店可选"))

    def test_grounded_catalogue_price_is_excused_by_the_marker(self) -> None:
        """A price attributed to the synthetic catalogue is not an invention."""
        reply = "示例兰州拉面（合成）：牛肉拉面 18 元"

        self.assertTrue(has_price_claim(reply))


class LaserLeakGuardTest(unittest.TestCase):
    HISTORY = [
        ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a", "assistant", "[case-1-0] 你好"),
        ("private", "qqbot:bot-1:user-b", "qqbot:bot-1:user-b", "user", "[case-1-0] 你好"),
    ]

    def test_token_in_one_session_is_not_a_leak(self) -> None:
        owners = assistant_owners(self.HISTORY, "[case-1-0]")

        self.assertEqual(owners, {("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a")})

    def test_same_token_in_two_sessions_is_a_leak(self) -> None:
        history = self.HISTORY + [
            ("private", "qqbot:bot-1:user-b", "qqbot:bot-1:user-b", "assistant", "[case-1-0] 你好"),
        ]

        self.assertEqual(len(assistant_owners(history, "[case-1-0]")), 2)

    def test_user_rows_do_not_count_as_owners(self) -> None:
        """A user echo of the marker must not look like a second session wrote it."""
        history = [
            ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a", "user", "[case-1-0] 你好"),
            ("private", "qqbot:bot-1:user-b", "qqbot:bot-1:user-b", "user", "[case-1-0] 你好"),
        ]

        self.assertEqual(assistant_owners(history, "[case-1-0]"), set())

    def test_unknown_token_has_no_owners(self) -> None:
        self.assertEqual(assistant_owners(self.HISTORY, "[never-used]"), set())


class ScenarioFileTest(unittest.TestCase):
    def test_committed_scenarios_load_and_declare_checks(self) -> None:
        scenarios = load_scenarios(DEFAULT_CASES_PATH)

        self.assertGreaterEqual(len(scenarios), 5)
        for scenario in scenarios:
            with self.subTest(scenario=scenario["id"]):
                self.assertTrue(scenario.get("steps"))
                self.assertTrue(scenario.get("checks"))

    def test_every_declared_check_is_implemented(self) -> None:
        from scripts.replay_scenarios import HARD_CHECKS

        for scenario in load_scenarios(DEFAULT_CASES_PATH):
            for name in scenario["checks"]:
                with self.subTest(scenario=scenario["id"], check=name):
                    self.assertIn(name, HARD_CHECKS)

    def test_case_file_does_not_reference_real_credentials_or_env(self) -> None:
        source = Path(DEFAULT_CASES_PATH).read_text(encoding="utf-8")

        for pattern in ("QQBOT_" + "SECRET=", "DEEPSEEK_" + "API_KEY=", 'open("' + '.env"'):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, source)


if __name__ == "__main__":
    unittest.main()
