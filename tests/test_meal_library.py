import asyncio
import json
import sqlite3
from datetime import timedelta

import pytest
from helpers import make_place

from doraymon.meal import (
    Evidence,
    MealCandidate,
    MealLibrary,
    MealReport,
    MenuItem,
    ResearchSources,
    render_html,
    render_json,
    render_markdown,
)
from doraymon.meal import sources as source_module
from doraymon.meal.__main__ import main
from doraymon.meal.library import LibraryError
from doraymon.meal.models import utc_now


def menu(*, price=18, observed_at=None, **changes):
    values = dict(
        source="meituan",
        source_id="menu-1",
        title="合成商家菜单",
        text="商家列示的餐点。",
        url="https://menu.test/page?token=private-token&item=1",
        observed_at=observed_at or utc_now(),
        kind="menu",
        branch_confirmed=True,
        items=(
            MenuItem(
                name="清汤面",
                preparation="清汤煮制",
                flavor="可选少盐",
                portion="一人份",
                price_yuan=price,
                service_mode="dine_in",
            ),
        ),
    )
    values.update(changes)
    return Evidence(**values)


def test_versions_survive_reopen_and_out_of_order_import_without_relabeling_observation(tmp_path):
    path = tmp_path / "meal.sqlite3"
    library = MealLibrary(path)
    assert library.lookup(("p1",)) == {} and not path.exists()
    current = menu(price=20)
    older = menu(price=18, observed_at=current.observed_at - timedelta(days=3))
    assert library.save({"p1": [current]}, places=(make_place(1, distance=123),)) == 1
    assert library.save({"p1": [older]}) == 1
    assert library.save({"p1": [current]}) == 0
    reopened = MealLibrary(path)
    [saved] = reopened.lookup(("p1",))["p1"]
    assert saved.items[0].price_yuan == 20
    assert saved.items[0].flavor == "可选少盐"
    assert saved.observed_at == current.observed_at
    view = reopened.inspect("p1", history=True)
    assert [item.items[0].price_yuan for item in view["records"]] == [20, 18]
    assert "distance" not in view["place"]
    assert "private-token" not in path.read_bytes().decode("utf-8", errors="ignore")


def test_private_records_are_visible_only_in_their_namespace(tmp_path):
    library = MealLibrary(tmp_path / "meal.sqlite3")
    public = menu(source_id="public")
    personal = menu(source_id="personal", text="合成成员资料")
    library.save({"p1": [public]})
    library.save({"p1": [personal]}, scope="member-a")
    assert len(library.lookup(("p1",), scope="member-a")["p1"]) == 2
    assert len(library.lookup(("p1",), scope="member-b")["p1"]) == 1
    assert len(library.inspect("p1", history=True)["records"]) == 1


def test_batch_failure_rolls_back_sources_and_branch_rows(tmp_path):
    library = MealLibrary(tmp_path / "meal.sqlite3")
    library.initialize()
    with sqlite3.connect(library.path) as connection:
        connection.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON records
                           WHEN NEW.amap_id='p2' BEGIN
                           SELECT RAISE(ABORT, 'synthetic failure'); END""")
    with pytest.raises(LibraryError):
        library.save({"p1": [menu()], "p2": [menu()]}, places=(make_place(1), make_place(2)))
    assert library.lookup(("p1", "p2")) == {}
    assert library.inspect("p1")["place"] is None


async def test_concurrent_writers_keep_all_records_with_thread_local_connections(tmp_path):
    library = MealLibrary(tmp_path / "meal.sqlite3")
    await asyncio.gather(
        *(
            asyncio.to_thread(library.save, {"p1": [menu(source_id=f"menu-{number}")]})
            for number in range(8)
        )
    )
    assert len(library.lookup(("p1",))["p1"]) == 8


def test_unrelated_or_future_schema_is_not_overwritten(tmp_path):
    path = tmp_path / "other.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES('keep me')")
    with pytest.raises(LibraryError, match="unsupported_library"):
        MealLibrary(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "keep me"
    library = MealLibrary(tmp_path / "future.sqlite3")
    library.initialize()
    with sqlite3.connect(library.path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(LibraryError, match="unsupported_library"):
        library.save({"p1": [menu()]})


def test_future_observation_is_rejected_before_any_database_write(tmp_path):
    library = MealLibrary(tmp_path / "meal.sqlite3")
    with pytest.raises(ValueError):
        library.save({"p1": [menu(observed_at=utc_now() + timedelta(days=1))]})
    assert not library.path.exists()


def test_lookup_bounds_each_branch_without_hiding_other_branches(tmp_path):
    library = MealLibrary(tmp_path / "meal.sqlite3")
    library.save({"p1": [menu(source_id=f"menu-{n}") for n in range(40)], "p2": [menu()]})
    view = library.lookup(("p1", "p2"))
    assert len(view["p1"]) == 32 and len(view["p2"]) == 1


async def test_structured_only_catalog_keeps_unknown_fields_and_escapes_menu_markup(tmp_path):
    path = tmp_path / "menu.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "amap_id": "p1",
                        "title": "合成菜单",
                        "observed_at": utc_now().isoformat(),
                        "items": [{"name": "清汤面<script>bad</script>", "price_yuan": 0}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = await ResearchSources(catalog_path=path).enrich((make_place(1),), "", lambda _: None)
    [dish] = report.candidates[0].evidence[0].items
    assert dish.flavor == "" and dish.portion == "" and dish.service_mode == "unknown"
    html = render_html(report)
    assert "&lt;script&gt;" in html and "<script>" not in html and "<blockquote>" not in html
    assert "菜单记载 0 元" in html


async def test_stored_menu_is_used_offline_without_inventing_geography_or_new_capture_time(
    tmp_path,
):
    library = MealLibrary(tmp_path / "meal.sqlite3")
    observation = menu(observed_at=utc_now() - timedelta(days=3))
    library.save({"p1": [observation]}, places=(make_place(1, distance=5),))
    stages = []
    report = await ResearchSources(library=library).enrich(
        (make_place(1, distance=820), make_place(2)),
        "",
        stages.append,
    )
    assert stages == ["正在查已有的餐饮资料。"]
    [saved] = report.candidates[0].evidence
    assert saved.needs_review and saved.observed_at == observation.observed_at
    assert report.candidates[0].place.distance == 820
    assert not report.candidates[1].evidence
    assert "资料需要重新核对" in render_markdown(report)
    assert "资料需要重新核对" in render_html(report)
    for output in (render_markdown(report), render_html(report), render_json(report)):
        assert "清汤面" in output and "一人份" in output and "少盐" in output
        assert "private-token" not in output


class DianpingDouble:
    def __init__(self):
        self.searches = 0
        self.fail = False

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def call(self, name, arguments):
        if self.fail:
            raise RuntimeError("https://private.test/?token=secret")
        if name == "dianping_search":
            self.searches += 1
            return {"items": [{"id": "shop-1", "name": "合成餐馆1"}]}
        return {"name": "合成餐馆1", "address": "合成路一百号二楼", "rating": "4.2"}


async def test_cache_is_scoped_to_member_and_requirements_and_refreshes_after_expiry(
    tmp_path, monkeypatch
):
    library = MealLibrary(tmp_path / "meal.sqlite3")
    client = DianpingDouble()
    sources = ResearchSources(
        library=library, dianping_url="https://dp.test/mcp", client_factory=client
    )
    places = (make_place(1).model_copy(update={"city": "合成市"}),)
    with monkeypatch.context() as old_time:
        old_time.setattr(source_module, "utc_now", lambda: utc_now() - timedelta(days=3))
        first = await sources.enrich(places, "私密要求_少辣", lambda _: None, scope="member-a")
    assert first.candidates[0].evidence[0].needs_review
    client.fail = True
    fallback = await sources.enrich(places, "私密要求_少辣", lambda _: None, scope="member-a")
    assert fallback.candidates[0].evidence[0].needs_review
    assert (
        fallback.candidates[0].evidence[0].observed_at
        == first.candidates[0].evidence[0].observed_at
    )
    assert fallback.coverage[2].status == "unavailable"
    assert "secret" not in fallback.model_dump_json()
    client.fail = False
    current = await sources.enrich(places, "私密要求_少辣", lambda _: None, scope="member-a")
    assert not current.candidates[0].evidence[0].needs_review and client.searches == 2
    cached = await sources.enrich(places, "私密要求_少辣", lambda _: None, scope="member-a")
    assert cached.coverage[2].reason == "library_cache" and client.searches == 2
    await sources.enrich(places, "另一种要求", lambda _: None, scope="member-a")
    await sources.enrich(places, "私密要求_少辣", lambda _: None, scope="member-b")
    assert client.searches == 4
    stored = library.path.read_bytes()
    assert "私密要求_少辣".encode() not in stored and b"member-a" not in stored
    assert library.lookup(("p1",)) == {}


async def test_library_failure_does_not_block_available_catalog_information(tmp_path):
    path = tmp_path / "menu.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "amap_id": "p1",
                        "title": "商家菜单",
                        "text": "有明确出处的合成资料。",
                        "observed_at": utc_now().isoformat(),
                        "items": [{"name": "清汤面", "price_yuan": 18}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = await ResearchSources(library=MealLibrary(tmp_path), catalog_path=path).enrich(
        (make_place(1),),
        "",
        lambda _: None,
    )
    assert report.coverage[-1].status == "unavailable"
    assert report.candidates[0].evidence[0].items[0].price_yuan == 18


def test_library_cli_import_query_history_and_export_are_offline(tmp_path, capsys):
    database = tmp_path / "meal.sqlite3"
    catalog_path = tmp_path / "catalog.json"
    observation = menu()
    catalog_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "amap_id": "p1",
                        "title": observation.title,
                        "text": observation.text,
                        "source": "meituan",
                        "source_id": observation.source_id,
                        "source_url": observation.url,
                        "observed_at": observation.observed_at.isoformat(),
                        "items": [item.model_dump() for item in observation.items],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    base = ["--database", str(database)]
    assert main(["library", "init", *base]) == 0
    assert main(["library", "import-catalog", str(catalog_path), *base]) == 0
    assert main(["library", "show", "p1", *base, "--output-dir", str(tmp_path / "out")]) == 2
    report_path = tmp_path / "report.json"
    report_path.write_text(
        MealReport(
            candidates=(
                MealCandidate(
                    place=make_place(1),
                    evidence=(observation,),
                ),
            )
        ).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as missing_scope:
        main(["library", "import-report", str(report_path), *base])
    assert missing_scope.value.code == 2
    assert main(["library", "import-report", str(report_path), *base, "--shared"]) == 0
    assert main(["library", "show", "p1", *base, "--output-dir", str(tmp_path / "out")]) == 0
    assert {p.suffix for p in (tmp_path / "out").iterdir()} == {".html", ".md", ".json"}
    capsys.readouterr()
    assert main(["library", "show", "p1", *base, "--history"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["place"]["name"] == "合成餐馆1"
    assert view["records"][0]["items"][0]["service_mode"] == "dine_in"
    assert "scope" not in view and "distance" not in view["place"]
