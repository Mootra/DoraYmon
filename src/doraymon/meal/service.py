from collections.abc import Callable
from typing import Protocol

from .amap import AmapError
from .models import MealOutcome, MealRequest, MealState, Place
from .sources import ResearchSources


class NearbyLookup(Protocol):
    async def nearby(self, location: str, radius: int) -> tuple[Place, ...]: ...


class MealService:
    """Turns one planner request and the previous state into facts and the next state."""

    def __init__(
        self,
        lookup: NearbyLookup | None,
        *,
        default_location: str,
        radius: int = 1500,
        closer_step: int = 500,
        min_radius: int = 200,
        sources: ResearchSources | None = None,
    ):
        self.lookup = lookup
        self.default_location = default_location.strip()
        self.radius, self.closer_step, self.min_radius = radius, closer_step, min_radius
        self.sources = sources or ResearchSources()

    async def handle(
        self,
        state: MealState | None,
        request: MealRequest,
        *,
        progress: Callable[[str], None] | None = None,
        scope: str | None = None,
    ) -> MealOutcome:
        if request.change == "cancel":
            return MealOutcome(kind="cancelled", state=None)
        stage = progress or (lambda _: None)
        location = request.location.strip()
        # A newly named place starts a fresh decision; earlier exclusions belonged to the old area.
        if state is None or (location and location != state.location):
            start = location or self.default_location
            if not start:
                return MealOutcome(kind="need_location", state=state)
            state = MealState(location=start, radius=self.radius)
        if request.budget_yuan is not None:
            state = state.model_copy(update={"budget_yuan": request.budget_yuan})
        if request.requirements is not None:
            state = state.model_copy(update={"requirements": request.requirements.strip()})
        if request.change == "map":
            if not state.selected:
                return MealOutcome(kind="need_search", state=state)
            return MealOutcome(kind="map", state=state, places=state.selected[:1])
        if request.change == "closer":
            radius = max(self.min_radius, state.radius - self.closer_step)
            state = state.model_copy(update={"radius": radius})
        elif request.change == "another":
            excluded = state.excluded | {place.id for place in state.selected}
            state = state.model_copy(update={"excluded": excluded})
        if self.lookup is None:
            return MealOutcome(kind="unavailable", state=state, detail="key_missing")
        try:
            stage("正在查你附近的餐厅。")
            found = await self.lookup.nearby(state.location, state.radius)
        except AmapError as error:
            return MealOutcome(kind="unavailable", state=state, detail=error.code)
        selected = tuple(p for p in found if p.id not in state.excluded)[: request.count]
        report = (
            await self.sources.enrich(selected, state.requirements, stage, scope=scope)
            if selected
            else None
        )
        state = state.model_copy(update={"selected": selected, "report": report})
        return MealOutcome(kind="places" if selected else "empty", state=state, places=selected)
