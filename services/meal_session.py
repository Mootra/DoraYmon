"""One ongoing nearby-meal decision, driven by follow-up messages.

The plan describes the target interaction: "附近吃什么 → 换一家 → 改预算 → 再近点
→ 换位置", where each follow-up keeps the parts of the request the user did not
change. This module owns that conversation: it turns a follow-up into a change of
``MealState``, re-runs selection, and reports what happened.

Rules taken literally from the plan:

- Changing the start point discards candidates from the old area instead of
  reusing them (``MealState.with_start`` already enforces this).
- "换一家" excludes the current shop for *this* decision only; it never becomes a
  permanent dislike.
- "再近点" tightens the distance bound, so an option whose distance is unknown is
  dropped rather than assumed near.
- A follow-up that changes nothing is not treated as a new request.

Pure orchestration: the catalogue is passed in, so every follow-up is testable
offline without a map or a network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from services.nearby_meal_service import (
    MealCatalog,
    MealRequest,
    MealSelection,
    select_meal,
)
from services.recommendation_types import (
    FeedbackKind,
    RecommendationFeedback,
    MealState,
    MealStateStore,
    StartPoint,
    StartPrecision,
)

# Follow-up intents the first version understands.
INTENT_NEARBY = "nearby"
INTENT_ANOTHER = "another"
INTENT_CHEAPER = "cheaper"
INTENT_BUDGET = "budget"
INTENT_PARTY = "party"
INTENT_MOVE = "move"
# Explicit statements about what happened, kept separate from requests.
INTENT_EATEN = "eaten"
INTENT_REJECTED = "rejected"

DEFAULT_RADIUS_METERS = 1500
MIN_RADIUS_METERS = 100
RADIUS_STEP_METERS = 500

CLOSER_MARKERS = ("近点", "近一点", "再近", "更近", "近些", "近一些")
ANOTHER_MARKERS = ("换一家", "换一个", "换家", "换店", "另一家", "别的店", "换一批")
CHEAPER_MARKERS = ("便宜点", "便宜一点", "再便宜", "贵了", "太贵", "便宜些")
# Statements of fact, checked before requests: "我吃了这家" must not be read as a
# request for somewhere else, and "这家不想去" must not become a taste preference.
EATEN_MARKERS = (
    "吃了这家", "吃了这家了", "已经吃了", "我吃了", "刚才吃了", "去吃了", "吃过了",
)
REJECTED_MARKERS = (
    "不想去这家", "不想吃这家", "不吃这家", "换掉这家", "这家不去", "不想去", "不吃了",
    "下次不去", "别推荐这家",
)

CHINESE_NUMERALS = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
PARTY_PATTERN = re.compile(r"(\d+|[一二两三四五六七八九十])\s*(?:个)?人")
BUDGET_PATTERN = re.compile(r"(?:预算|不超过|最多|只有)?\s*(\d+)\s*(?:元|块钱|块)")


# Location is only taken from an explicit marker: guessing that any noun is a
# place would produce confident nonsense.
LOCATION_PATTERNS = (
    re.compile(
        r"(?:^|[\s，,。！？?])(?:帮我(?:看看|找找|找|查一下)|请问)?"
        r"(?:我(?:现在)?在|换到|改到|换成|改成|在|去|到)?\s*"
        r"([^\s，。！？,?]{2,40}?)\s*(?:附近|周边|一带)"
    ),
    re.compile(r"(?:^|[\s，,])(?:在|去)\s*([^\s，。！？,?]{2,40}?)\s*(?:吃什么|吃啥|吃饭)"),
    re.compile(r"^(?:换到|改到)\s*([^\s，。！？,?]{2,40})[。！!?？]*$"),
    re.compile(r"^([^\s，。！？,?]{2,40}(?:大学|校区|车站|火车站|广场|公园|机场|地铁站))[。！!?？]*$"),
)
RELATIVE_LOCATIONS = {
    "这里", "这边", "这儿", "那边", "那里", "学校", "校区", "宿舍", "我家", "公司", "这", "那",
    "我们", "我这", "我这里", "我这边", "我们这", "我们这里", "我们学校",
}


def extract_start_point(text: str) -> StartPoint | None:
    """Pull a place name out of a message, or None when none is stated."""
    normalized = str(text or "").strip()
    if not normalized:
        return None
    for pattern in LOCATION_PATTERNS:
        match = pattern.search(normalized)
        if match is None:
            continue
        candidate = match.group(1).strip()
        candidate = re.sub(r"^(?:帮我|请)?(?:推荐|看看|找找|查一下|找一下)(?:一个|一家|个|家)?", "", candidate)
        if candidate and candidate not in RELATIVE_LOCATIONS:
            return StartPoint(text=candidate, precision=StartPrecision.LANDMARK)
    return None


@dataclass(frozen=True)
class MealTurn:
    """What one user message asked for."""

    intent: str
    text: str
    budget_cny: float | None = None
    party_size: int | None = None
    start: StartPoint | None = None


def _as_count(value: str) -> int | None:
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    return CHINESE_NUMERALS.get(text)


def parse_meal_turn(
    text: str,
    *,
    start: StartPoint | None = None,
    default_intent: str = INTENT_NEARBY,
) -> MealTurn:
    """Classify one message into a follow-up intent.

    Order matters: "换一家便宜点的" is first and foremost a request for another
    shop, with the budget change applied as well by the caller.
    """
    normalized = str(text or "").strip()
    lowered = normalized.lower()

    if start is not None:
        return MealTurn(intent=INTENT_MOVE, text=normalized, start=start)

    # Facts come before requests. Someone saying they ate somewhere is reporting
    # what happened, not asking for another option.
    if any(marker in lowered for marker in EATEN_MARKERS):
        return MealTurn(intent=INTENT_EATEN, text=normalized)
    if any(marker in lowered for marker in REJECTED_MARKERS):
        return MealTurn(intent=INTENT_REJECTED, text=normalized)

    party_match = PARTY_PATTERN.search(lowered)
    budget_match = BUDGET_PATTERN.search(lowered)

    if any(marker in lowered for marker in ANOTHER_MARKERS):
        return MealTurn(intent=INTENT_ANOTHER, text=normalized)
    if any(marker in lowered for marker in CLOSER_MARKERS):
        return MealTurn(intent=INTENT_NEARBY, text=normalized)
    if any(marker in lowered for marker in CHEAPER_MARKERS):
        return MealTurn(intent=INTENT_CHEAPER, text=normalized)
    if budget_match is not None:
        return MealTurn(
            intent=INTENT_BUDGET,
            text=normalized,
            budget_cny=float(budget_match.group(1)),
        )
    if party_match is not None:
        return MealTurn(
            intent=INTENT_PARTY,
            text=normalized,
            party_size=_as_count(party_match.group(1)),
        )
    return MealTurn(intent=default_intent, text=normalized)


@dataclass
class MealDecisionOutcome:
    """The result of one turn, including how the state changed."""

    selection: MealSelection
    state: MealState
    turn: MealTurn
    changed: tuple[str, ...] = field(default_factory=tuple)
    # Set when a narrowing attempt had to be undone because it excluded
    # everything. Carried explicitly rather than inferred, because the final
    # state can be consistent again yet the user still asked for something the
    # catalogue cannot provide.
    narrowing_failed: bool = False
    # A statement the user made, kept as data. Persisting it is somebody else's
    # job: this layer stays free of storage so it remains testable offline.
    feedback: RecommendationFeedback | None = None

    @property
    def states_a_fact(self) -> bool:
        return self.feedback is not None

    @property
    def is_usable(self) -> bool:
        return self.selection.is_usable

    @property
    def notes(self) -> tuple[str, ...]:
        notes = list(self.selection.notes)
        if self.selection.missing:
            notes.append("还缺：" + "、".join(self.selection.missing))
        return tuple(notes)


# Process-wide decision state, like the other bounded caches. It must outlive a
# single request: a follow-up such as "预算 15 元" carries no location, so the
# only way to continue the decision is to remember the previous one.
MEAL_STATES = MealStateStore()


class MealDecisionSession:
    """Drives one conversation's nearby-meal decision."""

    def __init__(
        self,
        *,
        catalog: MealCatalog,
        store: MealStateStore | None = None,
        default_radius_meters: int = DEFAULT_RADIUS_METERS,
    ) -> None:
        self.catalog = catalog
        # Sharing the process-wide store is the default so state survives across
        # messages; tests inject their own for isolation.
        self.store = store if store is not None else MEAL_STATES
        self.default_radius_meters = max(MIN_RADIUS_METERS, int(default_radius_meters))

    def begin(
        self,
        session_key: tuple[str, str, str],
        start: StartPoint,
    ) -> MealDecisionOutcome:
        """Start a decision for a new start point."""
        state = self.store.start(session_key, start)
        state = self.store.update(state.with_radius(self.default_radius_meters))
        return self._select(state, MealTurn(intent=INTENT_NEARBY, text="", start=start), ())

    def handle(
        self,
        session_key: tuple[str, str, str],
        text: str,
        *,
        start: StartPoint | None = None,
    ) -> MealDecisionOutcome:
        """Apply one follow-up to the ongoing decision.

        A message with no ongoing decision is treated as the start of one, which
        matches how a user actually talks: they do not announce a session first.
        """
        state = self.store.get(session_key)
        if state is None or state.start is None:
            if start is None:
                start = StartPoint(text=str(text or "").strip() or "未知", precision=StartPrecision.UNKNOWN)
            return self.begin(session_key, start)

        turn = parse_meal_turn(text, start=start)

        # A statement of fact is recorded and acknowledged, not turned into a new
        # search: "我吃了这家" does not mean "find me somewhere else".
        if turn.intent in (INTENT_EATEN, INTENT_REJECTED):
            feedback = self._fact_from(state, turn.intent)
            if feedback is None:
                return self._select(state, turn, ("没有正在进行的推荐可以回应",))
            return MealDecisionOutcome(
                selection=MealSelection(
                    recommendation=None,
                    refused_reason=self._acknowledge(feedback),
                ),
                state=state,
                turn=turn,
                changed=(feedback.kind.value,),
                feedback=feedback,
            )

        previous_nearby = state.current_item_id != ""
        updated, changed = self._apply(state, turn)
        updated = self.store.update(updated)
        outcome = self._select(updated, turn, changed)
        return self._finalize_nearby(turn, previous_nearby, outcome)

    def reset(self, session_key: tuple[str, str, str]) -> bool:
        return self.store.reset(session_key)

    # -- internals ----------------------------------------------------------

    def _apply(self, state: MealState, turn: MealTurn) -> tuple[MealState, tuple[str, ...]]:
        changed: list[str] = []

        if turn.intent == INTENT_MOVE and turn.start is not None:
            moved = state.with_start(turn.start)
            changed.append("起点")
            if not moved.candidates == state.candidates:
                changed.append("候选已重找")
            return moved.with_radius(self.default_radius_meters), tuple(changed)

        if turn.intent == INTENT_ANOTHER:
            current_shop = self._current_shop_id(state)
            if current_shop:
                changed.append("换一家")
                return state.exclude(current_shop, turn.text), tuple(changed)
            return state, tuple(changed)

        if turn.intent == INTENT_NEARBY:
            tighter = self._tighter_radius(state)
            if tighter >= (state.radius_meters or self.default_radius_meters):
                # Already at the floor: say so instead of pretending to narrow.
                changed.append("已是最小范围")
                return state, tuple(changed)
            changed.append("范围收紧")
            return state.with_radius(tighter), tuple(changed)

        if turn.intent == INTENT_CHEAPER:
            if state.budget_cny is not None:
                cheaper = max(1.0, float(state.budget_cny) * 0.8)
                changed.append("预算下调")
                return state.with_budget(cheaper), tuple(changed)
            changed.append("没有可下调的预算")
            return state, tuple(changed)

        if turn.intent == INTENT_BUDGET and turn.budget_cny is not None:
            changed.append("预算")
            return state.with_budget(turn.budget_cny), tuple(changed)

        if turn.intent == INTENT_PARTY and turn.party_size is not None:
            changed.append("人数")
            return state.with_party_size(turn.party_size), tuple(changed)

        return state, tuple(changed)

    def _finalize_nearby(
        self,
        turn: MealTurn,
        previous_nearby: bool,
        outcome: MealDecisionOutcome,
    ) -> MealDecisionOutcome:
        """Keep the reported range consistent with the recommendation.

        Invariant: a recommended shop must lie inside the stated bound. Narrowing
        can exclude everything, in which case the current choice is kept and the
        range must not claim to have changed, because it did not take effect.
        """
        if turn.intent != INTENT_NEARBY:
            return outcome

        distance = None
        if outcome.selection.is_usable:
            distance = outcome.selection.recommendation.shop.distance_meters
        bound = outcome.state.radius_meters
        consistent = distance is None or bound is None or int(distance) <= int(bound)
        state = outcome.state
        if not consistent:
            restored = self._widest_usable_radius(outcome.state)
            if restored is not None and int(restored) >= int(distance):
                state = self.store.update(outcome.state.with_radius(restored))

        if consistent and not outcome.narrowing_failed:
            return outcome

        # Either the range had to be widened again or the bound contradicted the
        # answer, so "range tightened" would be wrong in both cases.
        reported = [marker for marker in outcome.changed if marker != "范围收紧"]
        if "已是最小范围" in outcome.changed:
            reported.append("已是最小范围")
        elif previous_nearby:
            reported.append("没有更近的选择")
        else:
            reported.append("没有更近的选择")
        return MealDecisionOutcome(
            selection=outcome.selection,
            state=state,
            turn=outcome.turn,
            changed=tuple(reported),
            narrowing_failed=outcome.narrowing_failed,
        )

    def _fact_from(self, state: MealState, intent: str) -> RecommendationFeedback | None:
        """Build the fact the user just stated, or None when there is no subject.

        The subject is whatever was recommended last. Without one there is nothing
        to have eaten or refused, and guessing would attach the statement to the
        wrong shop.
        """
        shop_id = self._current_shop_id(state)
        if not shop_id:
            return None
        kind = FeedbackKind.EATEN if intent == INTENT_EATEN else FeedbackKind.REJECTED
        return RecommendationFeedback(
            kind=kind,
            shop_id=shop_id,
            item_id=state.current_item_id or "",
        )

    @staticmethod
    def _acknowledge(feedback: RecommendationFeedback) -> str:
        """Say what was recorded, and be explicit about what was not.

        A rejection is recorded as a rejection. Saying "已记住你不喜欢这家" would
        turn one refusal into a taste fact, which the user never stated.
        """
        if feedback.kind is FeedbackKind.EATEN:
            return "记下了：你在这家吃过。这只是记录，我不会替你总结口味偏好。"
        return (
            "记下了：这次不想去这家，本次推荐里不会再出现它。"
            "这只是这一次的选择，不代表你不喜欢这家店。"
        )

    def _widest_usable_radius(self, state: MealState) -> int | None:
        """The largest bound at which some option still qualifies.

        Used after a narrowing attempt excludes everything: the previous bound is
        the natural fallback and keeps the reported range consistent with the
        recommendation that is actually given.
        """
        if state.start is None:
            return None
        distances = [
            int(shop.distance_meters)
            for shop in self.catalog.shops()
            if shop.distance_meters is not None and shop.shop_id not in state.excluded
        ]
        if not distances:
            return None
        return max(distances)

    def _request_for(self, state: MealState) -> MealRequest:
        return MealRequest(
            budget_cny=state.budget_cny,
            party_size=state.party_size,
            max_distance_meters=state.radius_meters,
            excluded_shop_ids=state.excluded,
        )

    def _tighter_radius(self, state: MealState) -> int:
        """The next bound must actually exclude something.

        Subtracting a fixed step from a wide default bound can be a no-op: asking
        for somewhere closer while the recommendation stays 900m away is
        effectively ignoring the request. The bound therefore also tightens to
        below the current option.
        """
        current = state.radius_meters or self.default_radius_meters
        stepped = current - RADIUS_STEP_METERS

        current_distance = self._current_shop_distance(state)
        bound_by_choice = (
            max(MIN_RADIUS_METERS, current_distance - 1)
            if current_distance is not None
            else stepped
        )
        tighter = min(stepped, bound_by_choice)
        return int(max(MIN_RADIUS_METERS, tighter))

    def _current_shop_distance(self, state: MealState) -> int | None:
        shop_id = self._current_shop_id(state)
        if not shop_id:
            return None
        shop = self.catalog.shop_by_id(shop_id)
        if shop is None or shop.distance_meters is None:
            return None
        return int(shop.distance_meters)

    def _current_shop_id(self, state: MealState) -> str:
        if not state.current_item_id:
            return ""
        for item in self.catalog.items():
            if item.item_id == state.current_item_id:
                return item.shop_id
        return ""

    def _select(
        self,
        state: MealState,
        turn: MealTurn,
        changed: tuple[str, ...],
    ) -> MealDecisionOutcome:
        selection = select_meal(self.catalog, state.start, self._request_for(state))
        narrowing_failed = False
        if not selection.is_usable and turn.intent == INTENT_NEARBY:
            # Narrowing the range can exclude everything, which normally means the
            # current choice is already the closest option. Keep that choice and
            # say so, rather than answering with nothing or leaving a bound that
            # contradicts the answer.
            wider = self._widest_usable_radius(state)
            if wider is not None and wider != state.radius_meters:
                state = self.store.update(state.with_radius(wider))
                selection = select_meal(self.catalog, state.start, self._request_for(state))
                narrowing_failed = True
        if selection.is_usable:
            state = self.store.update(
                state.with_current(selection.recommendation.item.item_id)
            )
        return MealDecisionOutcome(
            selection=selection,
            state=state,
            turn=turn,
            changed=changed,
            narrowing_failed=narrowing_failed,
        )
