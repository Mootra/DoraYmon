"""Daily model usage and the cap that stops a runaway bill."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from services.model_usage import (
    MODEL_USAGE,
    UsageLedger,
    configure_from_settings,
    CapDecision,
)
from doraymon.config import Settings


class LedgerTest(unittest.TestCase):
    def _ledger(self, cap: int = 0, price: float = 0.0) -> UsageLedger:
        return UsageLedger(daily_token_cap=cap, price_per_million_cny=price)

    def test_a_fresh_ledger_has_no_usage(self) -> None:
        ledger = self._ledger()

        self.assertEqual(ledger.used_tokens, 0)
        self.assertEqual(ledger.request_count, 0)
        self.assertTrue(ledger.check().allowed)

    def test_recorded_tokens_accumulate(self) -> None:
        ledger = self._ledger()

        ledger.record(prompt_tokens=100, completion_tokens=50)
        ledger.record(prompt_tokens=10, completion_tokens=5)

        self.assertEqual(ledger.used_tokens, 165)
        self.assertEqual(ledger.request_count, 2)

    def test_a_missing_total_is_derived_not_invented(self) -> None:
        """The API may omit total_tokens; summing the parts is safe."""
        ledger = self._ledger()

        record = ledger.record(prompt_tokens=30, completion_tokens=20)

        self.assertEqual(record.total_tokens, 50)

    def test_a_reported_total_wins_over_the_sum(self) -> None:
        ledger = self._ledger()

        record = ledger.record(prompt_tokens=30, completion_tokens=20, total_tokens=77)

        self.assertEqual(record.total_tokens, 77)

    def test_missing_usage_is_not_counted_as_zero_usage(self) -> None:
        """A request with no reported usage still happened."""
        ledger = self._ledger()

        record = ledger.record()

        self.assertFalse(record.measured)
        self.assertEqual(ledger.request_count, 1)
        self.assertEqual(ledger.used_tokens, 0)

    def test_negative_numbers_are_clamped(self) -> None:
        ledger = self._ledger()

        record = ledger.record(prompt_tokens=-5, completion_tokens=-1)

        self.assertEqual(record.total_tokens, 0)


class CapTest(unittest.TestCase):
    def test_no_cap_never_refuses(self) -> None:
        ledger = UsageLedger(daily_token_cap=0)
        ledger.record(total_tokens=10_000_000)

        self.assertTrue(ledger.check().allowed)

    def test_the_cap_refuses_once_reached(self) -> None:
        ledger = UsageLedger(daily_token_cap=100)
        ledger.record(total_tokens=100)

        decision = ledger.check()

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "daily_cap_reached")
        self.assertIn("额度已用完", decision.describe())

    def test_the_cap_allows_the_request_that_reaches_it(self) -> None:
        """Stopping one request early would be surprising arithmetic."""
        ledger = UsageLedger(daily_token_cap=100)
        ledger.record(total_tokens=99)

        self.assertTrue(ledger.check().allowed)

    def test_a_refusal_is_counted(self) -> None:
        ledger = UsageLedger(daily_token_cap=1)
        ledger.record(total_tokens=5)

        ledger.check()
        ledger.check()

        self.assertEqual(ledger.refusals, 2)

    def test_a_refusal_explains_itself(self) -> None:
        ledger = UsageLedger(daily_token_cap=10)
        ledger.record(total_tokens=10)

        described = ledger.check().describe()

        self.assertIn("额度已用完", described)
        self.assertIn("BOT_DAILY_TOKEN_CAP", described)

    def test_usage_rolls_over_on_a_new_day(self) -> None:
        """An operator thinks in their own day, so the counter resets with it."""
        ledger = UsageLedger(daily_token_cap=100)
        yesterday = datetime(2026, 10, 18, 23, 59)
        ledger.record(total_tokens=100, now=yesterday)
        self.assertFalse(ledger.check(now=yesterday).allowed)

        today = yesterday + timedelta(minutes=2)

        self.assertTrue(ledger.check(now=today).allowed)
        self.assertEqual(ledger.used_tokens, 0)

    def test_the_day_is_recorded(self) -> None:
        ledger = UsageLedger()
        moment = datetime(2026, 10, 19, 12, 0)

        ledger.record(total_tokens=1, now=moment)

        self.assertEqual(ledger.day, moment.date())

    def test_a_decision_reports_a_human_readable_progress_line(self) -> None:
        ledger = UsageLedger(daily_token_cap=1000)
        ledger.record(total_tokens=250)

        described = ledger.check().describe()

        self.assertIn("250/1000", described)
        self.assertIn("剩余 750", described)


class EstimateTest(unittest.TestCase):
    def test_no_price_yields_no_estimate(self) -> None:
        """None, not 0.0: "unpriced" and "free" are different claims."""
        ledger = UsageLedger()
        ledger.record(total_tokens=1000)

        self.assertIsNone(ledger.estimated_cny)
        self.assertNotIn("¥", ledger.describe())

    def test_a_configured_price_yields_a_labelled_estimate(self) -> None:
        ledger = UsageLedger(price_per_million_cny=2.0)
        ledger.record(total_tokens=500_000)

        self.assertAlmostEqual(ledger.estimated_cny, 1.0)
        self.assertIn("估算", ledger.describe())

    def test_the_estimate_is_labelled_as_an_estimate(self) -> None:
        """A configured unit price changes without notice, so it is not a bill."""
        ledger = UsageLedger(price_per_million_cny=1.0)
        ledger.record(total_tokens=1_000_000)

        self.assertIn("（估算）", ledger.describe())


class UsageFromPayloadTest(unittest.TestCase):
    def test_usage_is_read_from_a_response(self) -> None:
        ledger = UsageLedger()

        usage = ledger.token_usage_from(
            {"usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}
        )

        self.assertEqual(usage["total_tokens"], 20)

    def test_a_response_without_usage_yields_nothing(self) -> None:
        ledger = UsageLedger()

        self.assertEqual(ledger.token_usage_from({}), {})
        self.assertEqual(ledger.token_usage_from({"usage": None}), {})
        self.assertEqual(ledger.token_usage_from({"usage": "nope"}), {})

    def test_unparseable_counts_are_treated_as_unknown(self) -> None:
        ledger = UsageLedger()

        usage = ledger.token_usage_from({"usage": {"total_tokens": "abc"}})

        self.assertEqual(usage["total_tokens"], 0)


class ConfigureTest(unittest.TestCase):
    def test_settings_configure_the_ledger(self) -> None:
        ledger = UsageLedger()

        configure_from_settings(
            Settings(daily_token_cap=5000, model_price_per_million_cny=3.0), ledger
        )

        self.assertEqual(ledger.daily_token_cap, 5000)
        self.assertAlmostEqual(ledger.price_per_million_cny, 3.0)

    def test_a_negative_cap_is_clamped_to_unlimited(self) -> None:
        ledger = UsageLedger()

        configure_from_settings(Settings(daily_token_cap=-5), ledger)

        self.assertEqual(ledger.daily_token_cap, 0)


class ServiceIntegrationTest(unittest.TestCase):
    """The cap must actually stop a model call, and say why."""

    def setUp(self) -> None:
        MODEL_USAGE.reset()
        self.addCleanup(MODEL_USAGE.reset)

    def _service(self, **overrides):
        from services.deepseek_service import DeepSeekService

        payload = {"deepseek_api_key": "test-key", "chat_history_enabled": False}
        payload.update(overrides)
        return DeepSeekService(Settings(**payload))

    def test_an_exhausted_budget_returns_an_explanation_not_a_call(self) -> None:
        MODEL_USAGE.daily_token_cap = 10
        MODEL_USAGE.record(total_tokens=10)
        service = self._service()

        with patch("services.deepseek_service.requests.post") as post:
            reply = service.chat("你好")

        post.assert_not_called()
        self.assertIn("额度已用完", reply)

    def test_a_call_within_budget_reports_its_usage(self) -> None:
        MODEL_USAGE.daily_token_cap = 10_000
        service = self._service()

        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [{"message": {"content": "回答"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }

        with patch("services.deepseek_service.requests.post", return_value=Response()):
            reply = service.chat("你好")

        self.assertEqual(reply, "回答")
        self.assertEqual(MODEL_USAGE.used_tokens, 15)

    def test_a_response_without_usage_is_still_counted_as_a_request(self) -> None:
        MODEL_USAGE.daily_token_cap = 10_000
        service = self._service()

        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "回答"}, "finish_reason": "stop"}]}

        with patch("services.deepseek_service.requests.post", return_value=Response()):
            service.chat("你好")

        self.assertEqual(MODEL_USAGE.request_count, 1)
        self.assertEqual(MODEL_USAGE.used_tokens, 0)

    def test_a_missing_key_is_reported_before_the_cap_is_checked(self) -> None:
        """Configuration comes first: the cap message would be misleading."""
        MODEL_USAGE.daily_token_cap = 1
        MODEL_USAGE.record(total_tokens=10)
        service = self._service(deepseek_api_key="")

        reply = service.chat("你好")

        self.assertIn("API Key 未配置", reply)
        self.assertNotIn("额度", reply)


if __name__ == "__main__":
    unittest.main()
