"""Adapt already authorized catalog exports, retaining their original evidence dates."""

from typing import Any

from .catalog import food_group


def legacy_records(
    value: dict[str, Any], *, city: str, campus_id: str, area_name: str
) -> list[dict]:
    shared_sources = value.get("sources", {})
    records = []
    for original in value["shops"]:
        row = dict(original)
        sources = []
        if row.get("source_refs") and isinstance(shared_sources, dict):
            sources = [
                dict(shared_sources[ref]) for ref in row["source_refs"] if ref in shared_sources
            ]
        for source in row.get("sources", []):
            if source.get("url"):
                sources.append(dict(source))
            else:
                sources.append(
                    {
                        "id": source.get("source_id"),
                        "title": row["display_name"],
                        "publisher": source.get("platform", "既有截图资料"),
                        "access": "operator_supplied_screenshot",
                        "published_date": source.get("captured_at"),
                        "checked_at": source.get("received_date"),
                    }
                )
        if not sources:
            raise ValueError("A legacy candidate has no retained source evidence")
        row.update(
            source_key=f"legacy:{campus_id}:" + row["id"],
            city=city,
            campus_id=campus_id,
            area_name=area_name,
            sources=sources,
            baseline=True,
        )
        row["food_group"] = food_group(row["display_name"], row["category"])
        row.setdefault("checked_at", value.get("checked_at") or value.get("exported_at") or "")
        row.setdefault(
            "location_status", "listed_address" if row.get("listed_location") else "needs_review"
        )
        row.pop("source_refs", None)
        records.append(row)
    return records
