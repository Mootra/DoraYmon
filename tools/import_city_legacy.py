"""Import specified old food exports; no credentials, model, map or QQ API is read."""

import argparse
import json
from pathlib import Path

from doraymon.catalog import CatalogStore, write_json
from doraymon.catalog_legacy import legacy_records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--yaohu-json", type=Path, required=True)
    parser.add_argument("--yangtze-json", type=Path, required=True)
    args = parser.parse_args()
    store = CatalogStore(args.folder / "city-candidates.sqlite3")
    records = []
    for path, city, campus_id, area in (
        (args.yaohu_json, "南昌", "jxnu_yaohu", "高新/瑶湖"),
        (args.yangtze_json, "荆州", "yangtze_east", "长江大学东校区周边"),
    ):
        value = json.loads(path.read_text(encoding="utf-8"))
        records.extend(legacy_records(value, city=city, campus_id=campus_id, area_name=area))
    store.import_records(records)
    path = args.folder / "catalog.json"
    previous = json.loads(path.read_text()) if path.exists() else {}
    value = store.export(
        path,
        previous.get("tasks", []),
        complete=previous.get("coverage", {}).get("source_directory_complete", False),
    )
    for key in ("collection", "collection_control"):
        if key in previous:
            value[key] = previous[key]
    write_json(path, value)
    print(
        json.dumps(
            {
                "legacy_records": len(records),
                "nanchang_records": len(store.rows("南昌")),
                "jingzhou_records": len(store.rows("荆州")),
            }
        )
    )


if __name__ == "__main__":
    main()
