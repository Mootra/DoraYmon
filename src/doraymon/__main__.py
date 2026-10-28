import argparse
import asyncio
from pathlib import Path

from .config import Settings
from .models import Passage
from .store import Store


def load_passages(directory: Path) -> list[Passage]:
    if not directory.is_dir():
        raise ValueError("资料目录不存在")
    passages = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".txt"}:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("资料路径超出目录")
        if path.stat().st_size > 200_000:
            raise ValueError("单个资料文件不得超过 200KB")
        text = path.read_text(encoding="utf-8")
        for start in range(0, len(text), 700):
            if chunk := text[start : start + 800].strip():
                passages.append(Passage(source=path.relative_to(directory).as_posix(), body=chunk))
    return passages


async def index(directory: Path, scope: str, settings: Settings) -> int:
    if scope != "public" and not (
        len(scope.split(":")) == 3
        and scope.split(":")[0] in {"user", "group"}
        and all(scope.split(":"))
    ):
        raise ValueError("scope 必须为 public、user:机器人号:用户号 或 group:机器人号:群号")
    passages = await asyncio.to_thread(load_passages, directory)
    store = Store(settings.database)
    await store.open()
    try:
        await store.replace_knowledge(scope, passages)
    finally:
        await store.close()
    return len(passages)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run")
    command = sub.add_parser("index")
    command.add_argument("directory", type=Path)
    command.add_argument("--scope", required=True)
    args = parser.parse_args()
    if args.command == "index":
        count = asyncio.run(index(args.directory, args.scope, Settings()))
        print(f"已索引 {count} 个知识片段。")
        return
    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter

    nonebot.init(driver="~aiohttp")
    nonebot.get_driver().register_adapter(Adapter)
    if nonebot.load_plugin("doraymon.onebot") is None:
        raise RuntimeError("plugin_load_failed")
    nonebot.run()


if __name__ == "__main__":
    main()
