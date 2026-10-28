import sqlite3
from pathlib import Path

from doraymon.meal import CampusCatalog, Place

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_campus_catalog(root: Path) -> CampusCatalog:
    path = root / "candidates.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA application_id=1146307410")
        connection.execute("PRAGMA user_version=2")
        connection.execute(
            """CREATE TABLE shops (
            id TEXT PRIMARY KEY, display_name TEXT, category TEXT,
            campus_scope TEXT, scope_basis TEXT, name_complete INTEGER)"""
        )
        connection.executemany(
            "INSERT INTO shops VALUES(?,?,?,?,?,?)",
            [
                ("a", "合成粉店", "鱼粉", "校内", "合成食堂位置", 1),
                ("b", "合成面店", "面食", "校内", "合成食堂位置", 1),
                ("c", "合成饭店", "套餐饭", "校内", "合成食堂位置", 1),
                ("d", "合成校内咖啡", "咖啡", "校内", "合成食堂位置", 1),
                ("e", "合成火锅店", "火锅", "校外", "合成商场地址", 1),
                ("f", "合成校外咖啡", "咖啡", "校外", "合成商场地址", 1),
                ("g", "合成待确认粉店", "米粉", None, "", 1),
                ("h", "合成截断粉店", "米粉", "校内", "合成食堂位置", 0),
            ],
        )
    return CampusCatalog(path)


def make_place(number: int, distance: int | None = 100) -> Place:
    return Place(
        id=f"p{number}",
        name=f"合成餐馆{number}",
        address=f"合成路{number}号",
        distance=distance,
        longitude=116.02,
        latitude=28.67,
    )


class FakeLookup:
    def __init__(self, places=(), error: Exception | None = None):
        self.places, self.error = tuple(places), error
        self.calls: list[tuple[str, int]] = []

    async def nearby(self, location: str, radius: int) -> tuple[Place, ...]:
        self.calls.append((location, radius))
        if self.error is not None:
            raise self.error
        return self.places
