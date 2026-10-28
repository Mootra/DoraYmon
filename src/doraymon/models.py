import json

from pydantic import BaseModel, ConfigDict, Field


class InputPart(BaseModel):
    message_id: str
    text: str


class Incoming(BaseModel):
    model_config = ConfigDict(frozen=True)

    bot_id: str
    user_id: str
    message_id: str
    group_id: str | None = None
    text: str = Field(max_length=8000)
    addressed: bool = False
    mentions: frozenset[str] = frozenset()
    quote_id: str = ""
    has_attachment: bool = False
    parts: tuple[InputPart, ...] = ()

    @property
    def session(self) -> str:
        return json.dumps(["qq", self.bot_id, self.group_id, self.user_id], ensure_ascii=False)

    @property
    def room(self) -> str:
        return json.dumps(["qq", self.bot_id, self.group_id], ensure_ascii=False)

    @property
    def scopes(self) -> tuple[str, str]:
        specific = (
            f"group:{self.bot_id}:{self.group_id}"
            if self.group_id
            else f"user:{self.bot_id}:{self.user_id}"
        )
        return "public", specific


class Turn(BaseModel):
    question: str
    answer: str


class Passage(BaseModel):
    source: str
    body: str


class Place(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    address: str = ""
    distance: int | None = Field(default=None, ge=0)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)


class Meal(BaseModel):
    location: str
    radius: int = Field(ge=200)
    budget: float | None = Field(default=None, gt=0)
    excluded: frozenset[str] = frozenset()
    selected: tuple[Place, ...] = ()
