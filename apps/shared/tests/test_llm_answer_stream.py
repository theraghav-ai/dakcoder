"""The answer inside `finish` streams as it is written.

Most answers are not content: a turn ends by calling `finish`, and the text the
developer reads is its `answer` argument. It arrived as tool-call fragments,
so the panel showed nothing until the call completed and then the whole answer
at once -- which looked like the gateway not streaming. It was streaming; the
client was not offering those fragments to the panel.
"""

from __future__ import annotations

import json
import random

from dakcoder_shared.llm import _ArgumentStream, _consume_stream

ANSWER = (
    'Done. "Quoted", a' + chr(10) + "newline, a" + chr(9) + "tab, a back" + chr(92)
    + "slash, é, 中, and the end."
)


def _frames(arguments: str, name: str = "finish", size: int = 7) -> list[str]:
    head = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": name, "arguments": ""}}]}}]}
    lines = ["data: " + json.dumps(head)]
    for i in range(0, len(arguments), size):
        frag = {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments[i : i + size]}}]}}]}
        lines.append("data: " + json.dumps(frag))
    lines.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
    lines.append("data: [DONE]")
    return lines


def test_the_finish_answer_reaches_the_sink_as_it_arrives() -> None:
    arguments = json.dumps({"answer": ANSWER, "blocked": "never shown"})
    seen: list[str] = []
    result = _consume_stream(iter(_frames(arguments)), seen.append)
    assert len(seen) > 5, "it arrived in pieces, not at the end"
    assert "".join(seen) == ANSWER, "exactly the answer: not the JSON, not the next field"
    assert json.loads(result.tool_calls[0].arguments)["answer"] == ANSWER, "the call is untouched"


def test_other_tool_calls_are_not_streamed() -> None:
    seen: list[str] = []
    _consume_stream(iter(_frames(json.dumps({"path": "handler/a.go"}), name="read_file")), seen.append)
    assert seen == []


def test_an_escape_split_across_fragments_waits_for_its_second_half() -> None:
    raw = json.dumps({"answer": ANSWER})
    random.seed(7)
    for _ in range(200):
        stream = _ArgumentStream("answer")
        cuts = sorted(random.sample(range(1, len(raw)), random.randint(1, 40)))
        out, prev, acc = [], 0, ""
        for cut in [*cuts, len(raw)]:
            acc += raw[prev:cut]
            prev = cut
            out.append(stream.feed(acc))
        assert "".join(out) == ANSWER
