from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from storage.food_preference_store import (
    add_food_preference,
    list_food_preferences,
    remove_food_preference,
)


class FoodPreferenceStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"

        with closing(self._get_connection("doraymon")) as connection:
            connection.execute(
                """
                CREATE TABLE food_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_openid TEXT NOT NULL,
                    preference_text TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_openid, preference_text)
                )
                """
            )

        self.connection_patch = patch(
            "storage.food_preference_store.get_connection",
            side_effect=self._get_connection,
        )
        self.connection_patch.start()

    def tearDown(self) -> None:
        self.connection_patch.stop()
        self.temp_dir.cleanup()

    def _get_connection(self, name: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def test_add_and_list_preferences(self) -> None:
        self.assertTrue(add_food_preference("user-a", "我喜欢辣"))
        self.assertTrue(add_food_preference("user-a", "不要太油"))

        self.assertEqual(
            list_food_preferences("user-a"),
            ["我喜欢辣", "不要太油"],
        )

    def test_duplicate_preference_is_not_added(self) -> None:
        self.assertTrue(add_food_preference("user-a", "我喜欢辣"))
        self.assertFalse(add_food_preference("user-a", "我喜欢辣"))

        self.assertEqual(list_food_preferences("user-a"), ["我喜欢辣"])

    def test_preferences_are_isolated_by_user(self) -> None:
        add_food_preference("user-a", "我喜欢辣")
        add_food_preference("user-b", "我喜欢清淡")

        self.assertEqual(list_food_preferences("user-a"), ["我喜欢辣"])
        self.assertEqual(list_food_preferences("user-b"), ["我喜欢清淡"])

    def test_remove_exact_preference(self) -> None:
        add_food_preference("user-a", "我不吃香菜")

        self.assertTrue(remove_food_preference("user-a", "我不吃香菜"))
        self.assertFalse(remove_food_preference("user-a", "我不吃香菜"))
        self.assertEqual(list_food_preferences("user-a"), [])


if __name__ == "__main__":
    unittest.main()
