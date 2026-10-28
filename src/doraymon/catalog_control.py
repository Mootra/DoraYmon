"""Control the one independently supervised public catalog job."""

import argparse
import asyncio
import json
from pathlib import Path

from .catalog import write_json

COLLECTION_UNIT = "doraymon-catalog-nanchang.service"


class CollectionControl:
    def __init__(self, folder: Path, *, runner=None):
        self.folder = folder
        self._runner = runner or self._start

    def is_paused(self) -> bool:
        path = self.folder / "collection-control.json"
        return path.exists() and bool(json.loads(path.read_text(encoding="utf-8")).get("paused"))

    @staticmethod
    async def _start() -> bool:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "start",
            COLLECTION_UNIT,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(10):
                return await process.wait() == 0
        except TimeoutError:
            process.kill()
            await process.wait()
            return False

    async def execute(self, action: str) -> dict:
        if action not in {"pause", "resume"}:
            raise ValueError("Unknown collection operation")
        if not self.folder.is_dir():
            return {"ok": False, "message": "采集目录尚未配置。"}
        await asyncio.to_thread(
            write_json, self.folder / "collection-control.json", {"paused": action == "pause"}
        )
        if action == "pause":
            return {"ok": True, "message": "暂停请求已提交，当前读取结束后暂停。"}
        try:
            ok = await self._runner()
        except (OSError, RuntimeError):
            ok = False
        return {"ok": ok, "message": "已继续采集。" if ok else "采集任务尚未启动，请检查后台服务。"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--action", choices=("pause", "resume"), required=True)
    args = parser.parse_args()
    result = asyncio.run(CollectionControl(args.folder).execute(args.action))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
