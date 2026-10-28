"""Consolidate classifications from captured listings without fetching or adding shops."""

import argparse
import hashlib
import json
from pathlib import Path

from .catalog import CatalogStore, food_group, normalize_classification, write_json
from .catalog_sources import category_from_listing


def consolidate(folder: Path, *, apply: bool = False) -> dict:
    store = CatalogStore(folder / "city-candidates.sqlite3")
    listings = {
        "ctrip:" + str(row["poiId"]): row
        for row in json.loads((folder / "source-listings.json").read_text(encoding="utf-8"))
    }
    rows = store.rows("南昌")
    changes = []
    report = {
        "shops": len(rows),
        "categories_normalized": 0,
        "categories_from_directory": 0,
        "categories_from_listing": 0,
        "newly_tagged": 0,
        "unknown_categories": 0,
        "changed": 0,
        "applied": apply,
    }
    for row in rows:
        updated = dict(row)
        listing = listings.get(row["source_key"])
        if listing:
            category, basis = category_from_listing(listing)
            if row["category"] == "类别待核对" and category != "类别待核对":
                updated.update(
                    category=category,
                    food_group=food_group(row["display_name"], category),
                    source_category=str(listing.get("cuisineName") or ""),
                    category_basis=basis,
                )
                if basis["kind"] == "source_directory":
                    updated["sources"] = [
                        *row["sources"],
                        {
                            "id": hashlib.sha256(basis["source_url"].encode()).hexdigest(),
                            "publisher": "Trip.com / 携程",
                            "title": "咖啡目录" if basis["directory"] == "cafes" else "烘焙目录",
                            "url": basis["source_url"],
                            "published_date": None,
                            "checked_at": basis["checked_at"],
                            "access": "public_directory",
                            "poi_id": str(listing["poiId"]),
                        },
                    ]
                key = (
                    "categories_from_directory"
                    if basis["kind"] == "source_directory"
                    else "categories_from_listing"
                )
                report[key] += 1
        updated = normalize_classification(updated, row)
        report["categories_normalized"] += (
            row["category"] != "类别待核对" and updated["category"] != row["category"]
        )
        report["unknown_categories"] += updated["category"] == "类别待核对"
        report["newly_tagged"] += bool(updated["tags"]) and not bool(row.get("tags"))
        if updated != row:
            changes.append(updated)
    report["changed"] = len(changes)
    if apply and changes:
        before = json.loads((folder / "catalog.json").read_text(encoding="utf-8"))
        store.import_records(changes)
        after = store.export(
            folder / "catalog.json",
            before.get("tasks"),
            complete=before.get("coverage", {}).get("source_directory_complete", False),
        )
        for key in ("collection", "collection_control"):
            if key in before:
                after[key] = before[key]
        write_json(folder / "catalog.json", after)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    import fcntl

    with (args.folder / "collection.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                "The catalog collector is running; consolidate after it stops."
            ) from None
        print(json.dumps(consolidate(args.folder, apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
