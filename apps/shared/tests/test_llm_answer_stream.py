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


# ── padding under a constrained tool choice ─────────────────────────────────
#
# Session 12444d171543: forced to `finish` by name, the model wrote one sentence,
# closed the string, and emitted 8,000 tokens of spaces and tabs -- three times.


def _padded(arguments: str, pad_frames: int = 5000) -> list[str]:
    lines = _frames(arguments)[:-2]  # without finish_reason and [DONE]
    pad = {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " \t\t  "}}]}}]}
    lines += ["data: " + json.dumps(pad)] * pad_frames
    lines += ["data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]}), "data: [DONE]"]
    return lines


def _counting(lines):
    read = {"n": 0}

    def it():
        for line in lines:
            read["n"] += 1
            yield line

    return it(), read


def test_padding_is_cut_short_and_a_whole_call_under_it_is_kept() -> None:
    lines = _padded('{   "answer": "Here is what I found:"')
    stream, read = _counting(lines)
    result = _consume_stream(stream)
    assert result.degenerate
    assert read["n"] < 200, f"read {read['n']} of {len(lines)} frames; it should stop at the padding"
    assert result.finish_reason == "tool_calls"
    assert json.loads(result.tool_calls[0].arguments) == {"answer": "Here is what I found:"}
    assert result.incomplete_tool_calls() == []


def test_padding_inside_an_unfinished_value_is_reported_as_cut_off() -> None:
    result = _consume_stream(iter(_padded('{"answer": "Here is')))
    assert result.degenerate and result.finish_reason == "length"
    assert result.incomplete_tool_calls(), "handled by the loop's cut-off path, not dispatched"


def test_ordinary_indentation_in_an_answer_is_not_padding() -> None:
    code = "func main() {" + "\n" + " " * 12 + "return" + "\n}"
    arguments = '{"answer": "' + code + '"}'
    result = _consume_stream(iter(_frames(arguments)))
    assert not result.degenerate
