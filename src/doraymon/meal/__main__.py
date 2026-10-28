import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from .evidence import catalog_records, review_record
from .export import write_exports
from .library import LibraryError, MealLibrary
from .models import MealCandidate, MealReport, MenuCatalog, Place, SourceCoverage


def library_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="管理本机餐饮信息资料库，不联网")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "import-catalog", "import-report", "show"):
        command = commands.add_parser(name)
        command.add_argument("--database", type=Path, required=True)
        if name.startswith("import-"):
            command.add_argument("file", type=Path)
        if name == "import-report":
            sharing = command.add_mutually_exclusive_group(required=True)
            sharing.add_argument("--shared", action="store_true", help="明确作为获准共享的资料导入")
            sharing.add_argument("--scope", help="仅供该本机命名空间读取")
        if name == "show":
            command.add_argument("amap_id")
            command.add_argument("--scope")
            command.add_argument("--history", action="store_true")
            command.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    library = MealLibrary(args.database)
    try:
        if args.command == "init":
            library.initialize()
            print("资料库已初始化。")
        elif args.command == "import-catalog":
            catalog = MenuCatalog.model_validate_json(args.file.read_text(encoding="utf-8"))
            count = library.save(catalog_records(catalog))
            print(f"新增 {count} 条资料版本。")
        elif args.command == "import-report":
            report = MealReport.model_validate_json(args.file.read_text(encoding="utf-8"))
            count = library.save(
                {c.place.id: c.evidence for c in report.candidates},
                places=tuple(c.place for c in report.candidates),
                scope=args.scope or "",
            )
            print(f"新增 {count} 条资料版本。")
        else:
            view = library.inspect(args.amap_id, scope=args.scope, history=args.history)
            records = [review_record(item, timedelta(hours=24)) for item in view["records"]]
            if args.output_dir:
                if args.history or view["place"] is None:
                    print(
                        "导出需要已核对的高德门店信息，并且不能同时选择历史模式。", file=sys.stderr
                    )
                    return 2
                report = MealReport(
                    candidates=(
                        MealCandidate(
                            place=Place.model_validate(view["place"]),
                            evidence=tuple(records[:8]),
                        ),
                    ),
                    coverage=(SourceCoverage(source="library", status="used"),),
                )
                for path in write_exports(report, args.output_dir).values():
                    print(path)
            else:
                view["records"] = [item.model_dump(mode="json") for item in records]
                print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0
    except (LibraryError, OSError, UnicodeError, ValidationError, ValueError):
        print("资料库操作失败：请检查资料格式、库版本与路径权限。", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "library":
        return library_main(argv[1:])
    parser = argparse.ArgumentParser(description="导出已保存的用餐资料，不发起联网查询")
    parser.add_argument("report", type=Path, help="用餐资料 JSON")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = MealReport.model_validate_json(args.report.read_text(encoding="utf-8"))
        files = write_exports(report, args.output_dir)
    except (OSError, UnicodeError, ValidationError, ValueError):
        print("导出失败：请检查资料格式、时间字段与输出目录权限。", file=sys.stderr)
        return 2
    for path in files.values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
