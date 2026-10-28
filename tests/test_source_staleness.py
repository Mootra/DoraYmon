"""P0: a stale fact must be detectable rather than silently presented."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from services.recommendation_types import SourceKind, SourceRef
from services.synthetic_catalog import item_by_id


class StaleSourceTest(unittest.TestCase):
    def _source(self, *, age_days: int) -> SourceRef:
        return SourceRef(
            kind=SourceKind.MENU,
            reference="synthetic-menu/x",
            fetched_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        )

    def test_fresh_source_is_not_stale(self) -> None:
        self.assertFalse(self._source(age_days=1).is_stale(max_age_days=180))

    def test_old_source_is_stale(self) -> None:
        self.assertTrue(self._source(age_days=400).is_stale(max_age_days=180))

    def test_disabled_window_never_reports_stale(self) -> None:
        self.assertFalse(self._source(age_days=4000).is_stale(max_age_days=0))

    def test_stale_menu_from_the_synthetic_catalog_is_detected(self) -> None:
        stale = item_by_id("item-stale-mixian")
        fresh = item_by_id("item-beef-noodle")

        self.assertTrue(stale.source.is_stale(max_age_days=180))
        self.assertFalse(fresh.source.is_stale(max_age_days=180))


if __name__ == "__main__":
    unittest.main()
