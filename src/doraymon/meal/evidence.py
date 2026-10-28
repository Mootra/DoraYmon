"""Source text hygiene and record age, shared by ingestion and storage."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Evidence, MenuCatalog, utc_now

SENSITIVE_QUERY = {
    "key",
    "apikey",
    "api_key",
    "token",
    "access_token",
    "xsec_token",
    "xsectoken",
    "sig",
    "signature",
    "authorization",
    "requestcode",
    "responsecode",
}


def public_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
            return ""
        query = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in SENSITIVE_QUERY]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    except ValueError:
        return ""


def source_text(value: Any, limit: int = 2400) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value).strip()
    return re.sub(r"https?://[^\s<>\"]+", lambda m: public_url(m[0]), text)[:limit]


def clean_evidence(item: Evidence, *, now: datetime | None = None) -> Evidence:
    now = now or utc_now()
    if item.observed_at > now or (item.published_at and item.published_at > now):
        raise ValueError("Evidence timestamps cannot be in the future")
    data = item.model_dump(exclude={"needs_review"})
    data.update(
        title=source_text(item.title, 200),
        text=source_text(item.text),
        url=public_url(item.url),
        source_id=source_text(item.source_id, 120),
        observed_at=item.observed_at.astimezone(UTC),
        published_at=item.published_at.astimezone(UTC) if item.published_at else None,
    )
    data["items"] = [
        {
            key: source_text(value, 200) if isinstance(value, str) else value
            for key, value in dish.model_dump().items()
        }
        for dish in item.items
    ]
    return Evidence.model_validate(data)


def review_record(item: Evidence, max_age: timedelta, *, now: datetime | None = None) -> Evidence:
    now = now or utc_now()
    needs_review = item.observed_at < now - max_age
    if item.kind == "experience" and item.published_at is None:
        needs_review = True
    if item.published_at is not None and item.published_at < now - timedelta(days=90):
        needs_review = True
    return item.model_copy(update={"needs_review": needs_review})


def catalog_records(catalog: MenuCatalog) -> dict[str, list[Evidence]]:
    found: dict[str, list[Evidence]] = {}
    for item in catalog.entries:
        found.setdefault(item.amap_id, []).append(
            clean_evidence(
                Evidence(
                    source=item.source,
                    title=item.title,
                    text=item.text,
                    url=item.source_url,
                    observed_at=item.observed_at,
                    published_at=item.published_at,
                    source_id=item.source_id,
                    items=item.items,
                    branch_confirmed=True,
                    kind="menu",
                )
            )
        )
    return found


def evidence_key(item: Evidence) -> tuple[str, str, str]:
    return item.source, item.kind, item.source_id or item.url or item.title
