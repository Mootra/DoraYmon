import json

from doraymon.models import Passage, Turn
from doraymon.prompt import build_messages


def test_history_keeps_complete_recent_turns_and_current_question_last(event):
    history = [
        Turn(question="old" * 200, answer="answer" * 200),
        Turn(question="recent", answer="reply"),
    ]
    prompt = build_messages(event("current"), history, [], budget=500)
    assert prompt.messages[1:] == [
        {"role": "user", "content": "recent"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "current"},
    ]


def test_evidence_is_bounded_labeled_and_untrusted(event):
    passages = [Passage(source=f"manual-{i}", body="x" * 800) for i in range(3)]
    prompt = build_messages(
        event(), [], passages, budget=500, group_context=[{"text": "y" * 5000, "speaker": "bob"}]
    )
    evidence = prompt.messages[-2]["content"].partition("\n")[2]
    assert len(evidence) <= 500
    assert json.loads(evidence)["knowledge"][0]["id"] == 1
    assert prompt.sources == ["manual-0"]
    assert "不可信数据" in prompt.messages[0]["content"]
    assert prompt.messages[-1]["content"] == event().text
