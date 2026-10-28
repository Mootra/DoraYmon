"""Replay scenarios: synthetic conversations with a timeline and per-step expectations."""

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Expect = Literal["reply", "silent", "any"]


class Chat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    type: Literal["group", "private"]
    group_id: str = Field(default="", pattern=r"^\d*$")
    group_name: str = "测试群"


class Step(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    at: float = Field(ge=0, description="距场景开始的秒数")
    user: str = Field(min_length=1, description="members 中的成员代号")
    text: str = Field(min_length=1)
    mention_bot: bool = False
    mention_user: str | None = Field(default=None, description="同时 @ 的其他成员代号")
    quote: int | Literal["bot"] | None = Field(
        default=None, description="引用更早某一步的消息，或 bot 表示引用机器人最近一条消息"
    )
    expect: Expect = "any"


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    name: str = Field(min_length=1)
    split: Literal["development", "holdout"]
    chat: Chat
    members: dict[str, str] = Field(min_length=1, description="成员代号 → 昵称")
    steps: tuple[Step, ...] = Field(min_length=1)
    settle: float = Field(default=45, ge=0, description="最后一步之后等待机器人回复的秒数")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.chat.type == "group" and not self.chat.group_id:
            raise ValueError("群聊场景需要 group_id")
        if self.chat.type == "private" and len(self.members) != 1:
            raise ValueError("私聊场景只能有一个成员")
        times = [step.at for step in self.steps]
        if times != sorted(times):
            raise ValueError("steps 的 at 必须递增")
        for index, step in enumerate(self.steps):
            if step.user not in self.members:
                raise ValueError(f"第 {index} 步的成员 {step.user} 没有在 members 中声明")
            if isinstance(step.quote, int) and not 0 <= step.quote < index:
                raise ValueError(f"第 {index} 步只能引用更早的步骤")
            if (step.mention_bot or step.mention_user) and self.chat.type == "private":
                raise ValueError("私聊步骤不使用 @")
            if step.mention_user is not None and step.mention_user not in self.members:
                raise ValueError(
                    f"第 {index} 步 @ 的成员 {step.mention_user} 没有在 members 中声明"
                )
        return self


def load_scenarios(directory: Path, split: str | None = None) -> list[Scenario]:
    scenarios = [
        Scenario.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]
    names = [scenario.name for scenario in scenarios]
    if len(names) != len(set(names)):
        raise ValueError("场景 name 不能重复")
    selected = [scenario for scenario in scenarios if split in (None, scenario.split)]
    if not selected:
        raise ValueError(f"没有可用的回放场景：{directory}")
    return selected
