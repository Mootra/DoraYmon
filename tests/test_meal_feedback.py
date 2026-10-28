"""Explicit dining feedback: recommending is not eating, refusing is not disliking.

RecommendationFeedback modelled the distinction but nothing ever produced one, so
the bot could not record the only feedback that matters: the user saying what
happened. These tests cover the parsing, the recorded fact, the acknowledgement
wording, and the storage.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.meal_session import (
    INTENT_EATEN,
    INTENT_REJECTED,
    MEAL_STATES,
    MealDecisionSession,
    MealStateStore,
    parse_meal_turn,
)
from services.nearby_meal_service import MealCatalog
from services.recommendation_types import (
    FlavourNote,
    FeedbackKind,
    MealItem,
    PriceRecord,
    Shop,
    SourceKind,
    SourceRef,
    StartPoint,
    StartPrecision,
)
from storage.meal_feedback_store import (
    eaten_shop_ids,
    forget_session,
    record_feedback,
    rejected_shop_ids,
    summarise,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
SESSION = ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a")


def _source(kind: SourceKind, reference: str) -> SourceRef:
    return SourceRef(kind=kind, reference=reference, fetched_at=NOW)


def _catalog() -> MealCatalog:
    shops = (
        Shop(shop_id="s1", name="第一家拉面", source=_source(SourceKind.MAP, "map/1"),
             address="示例路 1 号", distance_meters=320),
        Shop(shop_id="s2", name="第二家米线", source=_source(SourceKind.MAP, "map/2"),
             address="示例路 9 号", distance_meters=700),
    )
    items = tuple(
        MealItem(
            item_id=f"i{index}",
            shop_id=shop.shop_id,
            name=shop.name,
            source=_source(SourceKind.MENU, f"menu/{index}"),
            composition=("主料", "配菜"),
            composition_source=_source(SourceKind.MENU, f"menu/{index}"),
            portion="一份",
            portion_source=_source(SourceKind.MENU, f"menu/{index}"),
            flavours=(FlavourNote(description="清汤底，面条偏硬", source=_source(SourceKind.MENU, f"menu/{index}")),),
            prices=(PriceRecord(basis="menu", source=_source(SourceKind.MENU, f"menu/{index}"), amount_cny=18),),
        )
        for index, shop in enumerate(shops, start=1)
    )
    return MealCatalog(shops=shops, items=items)


def _landmark() -> StartPoint:
    return StartPoint(text="示例大学", precision=StartPrecision.LANDMARK)


class ParsingTest(unittest.TestCase):
    def test_eating_statements_are_recognised(self) -> None:
        for text in ("我吃了这家", "吃了这家", "刚才吃了这家", "已经吃了", "吃过了"):
            with self.subTest(text=text):
                self.assertEqual(parse_meal_turn(text).intent, INTENT_EATEN)

    def test_refusal_statements_are_recognised(self) -> None:
        for text in ("这家不想去", "不想吃这家", "换掉这家", "别推荐这家"):
            with self.subTest(text=text):
                self.assertEqual(parse_meal_turn(text).intent, INTENT_REJECTED)

    def test_a_fact_is_not_mistaken_for_a_request(self) -> None:
        """\"我吃了这家\" must not be read as \"find me somewhere else\"."""
        self.assertNotEqual(parse_meal_turn("我吃了这家").intent, "another")
        self.assertNotEqual(parse_meal_turn("换掉这家").intent, "another")

    def test_ordinary_requests_still_parse_as_before(self) -> None:
        cases = {
            "换一家": "another",
            "再近点": "nearby",
            "便宜点": "cheaper",
        }
        for text, intent in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_meal_turn(text).intent, intent)


class SessionFactTest(unittest.TestCase):
    def setUp(self) -> None:
        MEAL_STATES.clear()
        self.addCleanup(MEAL_STATES.clear)
        self.session = MealDecisionSession(catalog=_catalog(), store=MealStateStore())
        self.session.begin(SESSION, _landmark())

    def test_eating_records_an_eaten_fact(self) -> None:
        outcome = self.session.handle(SESSION, "我吃了这家")

        self.assertTrue(outcome.states_a_fact)
        self.assertEqual(outcome.feedback.kind, FeedbackKind.EATEN)
        self.assertTrue(outcome.feedback.means_eaten)
        self.assertEqual(outcome.feedback.shop_id, "s1")

    def test_refusing_records_a_rejection_not_a_taste(self) -> None:
        """One refusal must never become a permanent dislike."""
        outcome = self.session.handle(SESSION, "这家不想去")

        self.assertEqual(outcome.feedback.kind, FeedbackKind.REJECTED)
        self.assertFalse(outcome.feedback.means_eaten)

    def test_a_recommendation_alone_records_nothing(self) -> None:
        """Recommending is not eating: an unanswered suggestion leaves no trace."""
        outcome = self.session.handle(SESSION, "在示例大学附近吃什么")

        self.assertFalse(outcome.states_a_fact)
        self.assertIsNone(outcome.feedback)

    def test_a_fact_does_not_start_a_new_search(self) -> None:
        outcome = self.session.handle(SESSION, "我吃了这家")

        self.assertFalse(outcome.is_usable)
        self.assertIn("记下了", outcome.selection.refused_reason)

    def test_a_fact_without_a_current_recommendation_is_refused(self) -> None:
        """Nothing to have eaten: guessing the subject would attach it to a wrong shop."""
        self.session.store.reset(SESSION)
        self.session.begin(SESSION, _landmark())
        self.session.store.update(MealStateStore().start(SESSION, _landmark()))

        outcome = self.session.handle(SESSION, "我吃了这家")

        self.assertFalse(outcome.states_a_fact)
        self.assertIn("没有正在进行的推荐", " ".join(outcome.changed))

    def test_the_eaten_acknowledgement_does_not_claim_a_preference(self) -> None:
        outcome = self.session.handle(SESSION, "我吃了这家")

        self.assertIn("不会替你总结口味偏好", outcome.selection.refused_reason)

    def test_the_refusal_acknowledgement_says_it_is_not_a_dislike(self) -> None:
        outcome = self.session.handle(SESSION, "这家不想去")

        self.assertIn("不代表你不喜欢这家店", outcome.selection.refused_reason)

    def test_the_state_survives_a_fact(self) -> None:
        """Recording a fact must not accidentally clear the decision."""
        self.session.handle(SESSION, "我吃了这家")

        state = self.session.store.get(SESSION)
        self.assertIsNotNone(state)
        self.assertEqual(state.start.text, "示例大学")


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._patch = patch(
            "storage.meal_feedback_store.get_connection", side_effect=self._connection
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._temp.cleanup)
        self._create_table()

    def _connection(self, name: str):
        connection = sqlite3.connect(Path(self._temp.name) / "test.db")
        connection.row_factory = sqlite3.Row
        return connection

    def _create_table(self) -> None:
        with self._connection("x") as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS meal_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    shop_id TEXT NOT NULL,
                    item_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.commit()

    def _feedback(self, kind: FeedbackKind, shop_id: str = "s1"):
        from services.recommendation_types import RecommendationFeedback

        return RecommendationFeedback(kind=kind, shop_id=shop_id)


class RecordTest(StoreTestCase):
    def test_eaten_is_stored_and_read_back(self) -> None:
        from services.recommendation_types import RecommendationFeedback

        self.assertTrue(record_feedback(self._feedback(FeedbackKind.EATEN), session_key="k"))
        self.assertEqual(eaten_shop_ids("k"), ("s1",))

    def test_each_kind_is_kept_separate(self) -> None:
        record_feedback(self._feedback(FeedbackKind.EATEN, "s1"), session_key="k")
        record_feedback(self._feedback(FeedbackKind.REJECTED, "s2"), session_key="k")

        self.assertEqual(eaten_shop_ids("k"), ("s1",))
        self.assertEqual(rejected_shop_ids("k"), ("s2",))

    def test_feedback_is_isolated_between_sessions(self) -> None:
        """One person's refusal must not silence a suggestion for someone else."""
        record_feedback(self._feedback(FeedbackKind.REJECTED, "s1"), session_key="a")

        self.assertEqual(rejected_shop_ids("b"), ())

    def test_a_blank_shop_id_is_refused(self) -> None:
        self.assertFalse(record_feedback(self._feedback(FeedbackKind.EATEN, ""), session_key="k"))

    def test_a_missing_kind_is_refused(self) -> None:
        from services.recommendation_types import RecommendationFeedback

        self.assertFalse(
            record_feedback(RecommendationFeedback(kind="eaten", shop_id="s1"), session_key="k")
        )

    def test_an_unavailable_table_is_reported_not_raised(self) -> None:
        """A storage problem must not break the conversation."""
        with patch(
            "storage.meal_feedback_store.get_connection",
            side_effect=sqlite3.OperationalError("no such table"),
        ):
            self.assertFalse(record_feedback(self._feedback(FeedbackKind.EATEN), session_key="k"))
            self.assertEqual(eaten_shop_ids("k"), ())

    def test_forgetting_a_session_removes_only_its_rows(self) -> None:
        record_feedback(self._feedback(FeedbackKind.EATEN, "s1"), session_key="a")
        record_feedback(self._feedback(FeedbackKind.EATEN, "s2"), session_key="b")

        removed = forget_session("a")

        self.assertEqual(removed, 1)
        self.assertEqual(eaten_shop_ids("a"), ())
        self.assertEqual(eaten_shop_ids("b"), ("s2",))

    def test_summary_does_not_quote_user_text(self) -> None:
        record_feedback(self._feedback(FeedbackKind.EATEN), session_key="k")

        described = summarise("k")

        self.assertIn("吃过 1 家", described)
        self.assertNotIn("s1", described)

    def test_an_empty_session_summarises_plainly(self) -> None:
        self.assertIn("没有记录", summarise("nothing"))


if __name__ == "__main__":
    unittest.main()
