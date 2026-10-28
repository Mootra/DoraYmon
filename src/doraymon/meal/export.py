"""Portable views rendered from the same meal report, without another model call."""

import json
from html import escape
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from .amap import map_url
from .models import Evidence, MealReport, MenuItem
from .sources import SOURCE_LABELS, public_url, source_text

STATUS_LABELS = {
    "used": "已取得资料",
    "partial": "部分资料可用",
    "not_configured": "未接入",
    "unavailable": "暂不可用",
    "no_match": "没有匹配资料",
}


def _time(value) -> str:
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")


def _evidence_heading(item: Evidence) -> str:
    label = {"menu": "商家菜单记录", "listing": "门店资料", "experience": "体验参考"}[item.kind]
    if not item.branch_confirmed:
        label = "分店待核对的" + label
    return label if item.source == "catalog" else f"{label} · {SOURCE_LABELS[item.source]}"


def _md(text: str) -> str:
    for char in "\\`*_{}[]<>#|":
        text = text.replace(char, "\\" + char)
    return text


def _item_line(item: MenuItem) -> str:
    parts = []
    if item.kind == "package":
        parts.append("套餐")
    for key, label in (
        ("specification", "规格"),
        ("preparation", "做法"),
        ("flavor", "口味"),
        ("portion", "分量"),
    ):
        if value := getattr(item, key):
            parts.append(f"{label}：{source_text(value, 200)}")
    if item.service_mode != "unknown":
        parts.append("堂食" if item.service_mode == "dine_in" else "外卖")
    if item.price_yuan is not None:
        parts.append(f"菜单记载 {item.price_yuan:g} 元")
    return source_text(item.name, 100) + ("｜" + "；".join(parts) if parts else "")


def render_markdown(report: MealReport) -> str:
    lines = ["# 附近用餐", f"资料整理于 {_time(report.created_at)}（北京时间）", ""]
    for number, candidate in enumerate(report.candidates, 1):
        place = candidate.place
        lines += [f"## {number}. {_md(place.name)}", _md(place.address or "地址未知")]
        if place.distance is not None:
            lines.append(f"直线约 {place.distance} 米")
        else:
            lines.append("距离未知")
        if place.category:
            lines.append(f"高德分类：{_md(place.category)}")
        if place.tags:
            lines.append("高德特色标签：" + _md("、".join(place.tags)))
        if place.rating:
            lines.append(f"高德评分：{_md(place.rating)}")
        if place.average_cost_yuan is not None:
            lines.append(f"高德人均参考：{place.average_cost_yuan:g} 元（不代表餐点现价）")
        lines += [f"[地图位置]({map_url(place)})", ""]
        if not candidate.evidence:
            lines += ["尚无可核对的菜单、口味和用餐体验资料。", ""]
        for evidence in candidate.evidence:
            lines += [f"### {_evidence_heading(evidence)}"]
            if evidence.needs_review:
                lines.append("资料需要重新核对。")
            lines += [f"> {_md(line)}" for line in source_text(evidence.text).splitlines()]
            lines += [f"- {_md(_item_line(item))}" for item in evidence.items]
            url = public_url(evidence.url)
            title = _md(source_text(evidence.title, 200))
            source = f"[{title}]({quote(url, safe=':/?=&%#+,')})" if url else title
            lines.append(f"来源：{source}；获取于 {_time(evidence.observed_at)}")
            if evidence.published_at:
                lines.append(f"资料发布时间：{_time(evidence.published_at)}")
            lines.append("")
    if report.coverage:
        lines += ["## 资料来源状态"]
        lines += [
            f"- {SOURCE_LABELS[c.source]}：{STATUS_LABELS[c.status]}" for c in report.coverage
        ]
    return "\n".join(lines).rstrip() + "\n"


def render_html(report: MealReport) -> str:
    cards = []
    for number, candidate in enumerate(report.candidates, 1):
        place = candidate.place
        facts = []
        if place.distance is not None:
            facts.append(f"直线约 {place.distance} 米")
        if place.category:
            facts.append("高德分类：" + escape(place.category))
        if place.rating:
            facts.append("高德评分：" + escape(place.rating))
        if place.average_cost_yuan is not None:
            facts.append(f"高德人均参考 {place.average_cost_yuan:g} 元（不代表餐点现价）")
        details = []
        for item in candidate.evidence:
            url = public_url(item.url)
            title = escape(source_text(item.title, 200))
            link = (
                f'<a href="{escape(url, quote=True)}" rel="noreferrer">{title}</a>'
                if url
                else title
            )
            published = f" · 发布于 {_time(item.published_at)}" if item.published_at else ""
            review = '<p class="missing">资料需要重新核对。</p>' if item.needs_review else ""
            dishes = "".join(
                f'<p class="menu-item">{escape(_item_line(dish))}</p>' for dish in item.items
            )
            quotation = (
                f"<blockquote>{escape(source_text(item.text))}</blockquote>" if item.text else ""
            )
            details.append(
                f'<section class="evidence"><h3>{_evidence_heading(item)}</h3>'
                f"{review}{quotation}{dishes}"
                f'<p class="source">{link} · 获取于 {_time(item.observed_at)}'
                f"{published}</p></section>"
            )
        if not details:
            details.append('<p class="missing">尚无可核对的菜单、口味和用餐体验资料。</p>')
        tags = f"<p>高德特色标签：{escape('、'.join(place.tags))}</p>" if place.tags else ""
        cards.append(
            f'<article><div class="place-heading"><h2><span>{number:02}</span>'
            f"{escape(place.name)}</h2>"
            f'<a class="map" href="{escape(map_url(place), quote=True)}" '
            'rel="noreferrer">地图位置 ↗</a></div>'
            f'<p class="address">{escape(place.address or "地址未知")}</p>'
            f'<p class="facts">{" · ".join(facts)}</p>{tags}{"".join(details)}</article>'
        )
    coverage = "".join(
        f"<li><strong>{SOURCE_LABELS[c.source]}</strong><span>{STATUS_LABELS[c.status]}</span></li>"
        for c in report.coverage
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>附近用餐</title>
<style>
:root{{color-scheme:light;font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;
color:#202b27;background:#f5f3ed}}
*{{box-sizing:border-box}}body{{margin:0}}main{{max-width:920px;margin:auto;padding:42px 28px 56px}}
header{{border-bottom:2px solid #284e40;padding-bottom:24px;margin-bottom:24px}}
h1{{font-size:32px;letter-spacing:.03em;margin:0 0 10px}}
header p,.source{{color:#53645b;font-size:14px;line-height:1.65}}header p{{margin:0}}
article{{background:#fffefa;border:1px solid #dcded3;border-radius:12px;
padding:25px 28px;margin:0 0 20px}}
.place-heading{{display:flex;align-items:baseline;justify-content:space-between;gap:20px}}h2{{font-size:23px;line-height:1.5;margin:0;overflow-wrap:anywhere}}
h2 span{{color:#738476;font-size:15px;margin-right:12px;font-weight:500}}
a{{color:#265d47;text-underline-offset:4px;overflow-wrap:anywhere}}
.map{{white-space:nowrap;font-size:15px}}p{{font-size:16px;line-height:1.8;margin:10px 0}}
.address{{margin:8px 0 2px}}
.facts{{color:#53645b;font-size:14px;margin-top:0}}h3{{font-size:16px;margin:0 0 12px}}
.menu-item{{overflow-wrap:anywhere}}
.evidence{{border-top:1px solid #e4e7dc;margin-top:22px;padding-top:20px}}
blockquote{{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;border-left:3px solid #bcccb8;
padding:3px 0 3px 16px;line-height:1.85;font-size:16px}}
.source{{margin:14px 0 0}}.missing{{color:#775324;border-top:1px solid #e4e7dc;padding-top:16px}}
aside{{padding:12px 0}}aside h2{{font-size:18px}}
ul{{list-style:none;margin:14px 0 0;padding:0;display:flex;gap:20px;flex-wrap:wrap}}
li{{font-size:14px;line-height:1.8}}li span{{display:block;color:#53645b}}
a:focus-visible{{outline:3px solid #a1b8a5;outline-offset:5px}}
@media(max-width:540px){{main{{padding:25px 16px 36px}}h1{{font-size:27px}}
article{{padding:20px 18px}}h2{{font-size:21px}}.place-heading{{display:block}}
.map{{display:inline-block;margin-top:10px}}ul{{gap:14px 24px}}}}
@media print{{body{{background:white}}main{{padding:0}}article{{break-inside:avoid}}}}
</style></head><body><main><header><h1>附近用餐</h1>
<p>资料整理于 {_time(report.created_at)}（北京时间）</p></header>
{"".join(cards)}<aside><h2>资料来源状态</h2><ul>{coverage}</ul></aside>
</main></body></html>"""


def render_json(report: MealReport) -> str:
    payload = report.model_dump(mode="json")
    for candidate in payload["candidates"]:
        for item in candidate["evidence"]:
            item["url"] = public_url(item["url"])
            item["text"] = source_text(item["text"])
            item["title"] = source_text(item["title"], 200)
            item["source_id"] = source_text(item["source_id"], 120)
            for dish in item["items"]:
                for key, value in dish.items():
                    if isinstance(value, str):
                        dish[key] = source_text(value, 200)
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def write_exports(report: MealReport, directory: Path) -> dict[str, Path]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stem = f"meal-{report.created_at:%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    result = {}
    for suffix, render in (("md", render_markdown), ("html", render_html), ("json", render_json)):
        path = directory / f"{stem}.{suffix}"
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(render(report))
        path.chmod(0o600)
        result[suffix] = path
    return result
