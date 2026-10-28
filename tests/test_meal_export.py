from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest
from helpers import make_place
from pydantic import ValidationError

from doraymon.meal import (
    Evidence,
    MealCandidate,
    MealReport,
    render_html,
    render_json,
    render_markdown,
)
from doraymon.meal.__main__ import main
from doraymon.meal.models import SourceCoverage


def sample_report():
    return MealReport(
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        candidates=(
            MealCandidate(
                place=make_place(1).model_copy(
                    update={
                        "name": "合成餐馆 <script>alert(1)</script>",
                        "category": "中餐",
                        "rating": "4.2",
                        "average_cost_yuan": 28,
                    }
                ),
                evidence=(
                    Evidence(
                        source="xiaohongshu",
                        title="用餐记录 [查看]",
                        text='<img src=x onerror="alert(2)"> 份量较足。',
                        url="https://source.test/note(one)?xsec_token=secret&item=1#private",
                        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
                        branch_confirmed=False,
                    ),
                ),
            ),
        ),
        coverage=(SourceCoverage(source="xiaohongshu", status="used"),),
    )


def test_exports_share_report_data_and_escape_source_content():
    report = sample_report()
    html, md, raw = render_html(report), render_markdown(report), render_json(report)
    for output in (html, md, raw):
        assert "份量较足" in output
        assert "secret" not in output and "#private" not in output
    assert "分店待核对的体验参考" in html and "分店待核对的体验参考" in md
    assert "2025-01-01 08:00" in html and "2025-01-01 08:00" in md
    assert "不代表餐点现价" in html and "不代表餐点现价" in md
    assert "高德评分：4.2" in html and "高德评分：4.2" in md
    assert "%28one%29" in md
    assert "&lt;script&gt;" in html and "&lt;img" in html

    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.names = []

        def handle_starttag(self, tag, attrs):
            self.names.append(tag)

    tags = Tags()
    tags.feed(html)
    assert not {"script", "img", "iframe"}.intersection(tags.names)
    imported = MealReport.model_validate_json(raw)
    assert imported.candidates[0].place == report.candidates[0].place
    assert imported.candidates[0].evidence[0].url == "https://source.test/note(one)?item=1"


def test_report_rejects_timezone_ambiguous_timestamps():
    data = sample_report().model_dump(mode="json")
    data["candidates"][0]["evidence"][0]["observed_at"] = "2025-01-01T10:00:00"
    with pytest.raises(ValidationError):
        MealReport.model_validate(data)


def test_offline_cli_exports_same_snapshot_without_personal_chat_context(tmp_path, capsys):
    path = tmp_path / "report.json"
    path.write_text(render_json(sample_report()), encoding="utf-8")
    target = tmp_path / "export"
    assert main([str(path), "--output-dir", str(target)]) == 0
    outputs = list(target.iterdir())
    assert {p.suffix for p in outputs} == {".html", ".md", ".json"}
    [snapshot] = [p for p in outputs if p.suffix == ".json"]
    exported = snapshot.read_text(encoding="utf-8")
    assert MealReport.model_validate_json(exported).schema_version == "doraymon.meal.v1"
    assert not {"msg_id", "user_id", "stream_id", "requirements"}.intersection(
        MealReport.model_validate_json(exported).model_dump()
    )
    path.write_text("invalid", encoding="utf-8")
    assert main([str(path), "--output-dir", str(target)]) == 2
    assert "导出失败" in capsys.readouterr().err
