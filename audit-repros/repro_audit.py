"""Executable reproductions for the DakCoder audit.

Runs against the REAL modules (ContextManager, AgentLoop, Router, fs tools).
Each repro prints PASS(bug reproduced) / FAIL(not reproduced) with evidence.
"""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, "/mnt/data/raghav/dakcoder/apps/agent/src")
sys.path.insert(0, "/mnt/data/raghav/dakcoder/apps/shared/src")

from dakcoder_shared.llm import ChatResult, ToolCall, Usage  # noqa: E402
from dakcoder_shared.paths import Workspace  # noqa: E402
from dakcoder_agent.context import ContextManager, Message, Recap, Role  # noqa: E402
from dakcoder_agent.loop import AgentLoop, _ReadLedger  # noqa: E402
from dakcoder_agent.modes import Intent, Mode  # noqa: E402
from dakcoder_agent.prompts import system_prompt  # noqa: E402
from dakcoder_agent.tools import fs, control  # noqa: E402
from dakcoder_agent.tools.router import Router  # noqa: E402


# ── scripted client ─────────────────────────────────────────────────────────

class ScriptedClient:
    """Returns pre-scripted ChatResults in order; records every request."""

    def __init__(self, turns: list[ChatResult]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    def chat(self, messages, **kw) -> ChatResult:
        self.requests.append({"messages": messages, **{k: v for k, v in kw.items() if k != "on_delta"}})
        if not self.turns:
            return ChatResult(content="(script exhausted)", finish_reason="stop",
                              usage=Usage(prompt_tokens=100, completion_tokens=5))
        return self.turns.pop(0)


def chat_turn(content="", tool_calls=(), finish_reason="stop") -> ChatResult:
    return ChatResult(
        content=content,
        tool_calls=list(tool_calls),
        finish_reason=finish_reason,
        usage=Usage(prompt_tokens=500, completion_tokens=50),
    )


def make_workspace(files: dict[str, str]) -> Workspace:
    tmp = Path(tempfile.mkdtemp(prefix="dakaudit-"))
    for rel, body in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return Workspace.at(tmp)


def make_loop(client, workspace, *, max_turns=10) -> AgentLoop:
    handlers = {**fs.HANDLERS, **control.HANDLERS}
    router = Router(workspace, handlers)
    context = ContextManager(mode=Mode.ASK, system_prompt=system_prompt())
    return AgentLoop(context, client, router, max_turns=max_turns)


def run_all(loop: AgentLoop, task: str, intent) -> list:
    return list(loop.run(task, intent=intent))


# ── R1: CM-1 — insertion cap invisible to the read ledger ───────────────────

def repro_cm1():
    print("\n=== R1 / CM-1: capped read then tail re-read ===")
    big = "\n".join(f"var line_{i} = {i} // padding padding padding padding padding" for i in range(1, 8001))
    ws = make_workspace({"big.go": big})
    client = ScriptedClient([
        chat_turn(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "big.go"}))]),
        chat_turn(tool_calls=[ToolCall("c2", "read_file", json.dumps({"path": "big.go", "start": 6000, "end": 6500}))]),
        chat_turn(tool_calls=[ToolCall("c3", "finish", json.dumps({"answer": "done"}))]),
    ])
    loop = make_loop(client, ws)
    events = run_all(loop, "look at big.go", Intent.ASK)

    # What did the first read actually put in context?
    first = next(m for m in loop.context.build() if m.tool_call_id == "c1")
    elided = "elided" in first.content
    has_6000 = "var line_6000 " in first.content
    # Was the second read intercepted?
    second = next((m for m in loop.context.build() if m.tool_call_id == "c2"), None)
    intercepted = second is not None and "already in context above" in second.content
    ledger = loop.state.reads.get("big.go")
    print(f"  first read elided at insertion: {elided}")
    print(f"  lines 6000-6500 present in context after read 1: {has_6000}")
    print(f"  ledger claims covered: {ledger.covered if ledger else None}")
    print(f"  re-read of 6000-6500 refused with 'already in context above': {intercepted}")
    if elided and not has_6000 and intercepted:
        print("  BUG REPRODUCED: ledger says delivered, context does not contain the lines, re-read refused.")
    else:
        print("  not reproduced")


# ── R2: CM-2 — compaction leaves the read ledger stale ──────────────────────

def repro_cm2():
    print("\n=== R2 / CM-2: compaction then re-read refused ===")
    body = "\n".join(f"line {i}" for i in range(1, 301))
    ws = make_workspace({"f.go": body})
    client = ScriptedClient([])
    loop = make_loop(client, ws)
    ctx = loop.context

    # a real dispatched read, recorded exactly the way the loop records it
    outcome = loop.router.dispatch("read_file", json.dumps({"path": "f.go", "start": 1, "end": 300}), mode=Mode.ASK)
    loop._record_read("f.go", (1, 300), 300)
    ctx.append_tool_result("read_file", outcome.for_model(), tool_call_id="x1", path="f.go", line_range=(1, 300))
    ctx.append_user("filler so compaction has something to keep")

    # compact away everything except the last message
    recap = ctx.compact(lambda msgs: Recap(goal="g", files_read=("f.go",)), keep_recent=1)
    still_there = any("line 250" in m.content for m in ctx.build())
    verdict = loop._re_reading(ToolCall("x2", "read_file", json.dumps({"path": "f.go", "start": 1, "end": 300})))
    print(f"  after compaction, lines still in context: {still_there}")
    print(f"  recap says files_read includes f.go: {'f.go' in recap.files_read}")
    print(f"  re-read verdict: {verdict[:90]!r}")
    if not still_there and verdict:
        print("  BUG REPRODUCED: content evicted, but the ledger refuses the re-read and claims it is 'in context above'.")
    else:
        print("  not reproduced")


# ── R3: CM-3 — retention cut blind to tool-call arguments ───────────────────

def repro_cm3():
    print("\n=== R3 / CM-3: write-heavy working set cannot be compacted ===")
    ctx = ContextManager(mode=Mode.AGENT, system_prompt=system_prompt())
    payload = "x" * 40_000  # one write_file argument blob, ~12.5k tokens
    for i in range(20):
        ctx.begin_turn()
        call = ToolCall(f"w{i}", "write_file", json.dumps({"path": f"f{i}.go", "content": payload}))
        ctx.append_assistant("", tool_calls=(call,))
        ctx.append_tool_result("write_file", f"wrote f{i}.go (1 lines)", tool_call_id=f"w{i}")
    use_before = ctx.usage()
    print(f"  usage.total before compact: {use_before.total:,} / budget {use_before.budget:,} "
          f"(should_compact={ctx.should_compact()})")
    recap = ctx.compact(lambda msgs: Recap(goal="g"))
    use_after = ctx.usage()
    print(f"  usage.total after compact:  {use_after.total:,}  (compactions={ctx.compactions})")
    print(f"  messages evicted: {use_before.total - use_after.total <= 0 and 'NONE' or 'some'}")
    if use_after.total >= use_before.total * 0.95 and ctx.should_compact():
        print("  BUG REPRODUCED: compaction freed (almost) nothing; every later turn will re-fire it "
              "(loop kills the run via the thrash detector or OverBudget->ERROR).")
    else:
        print("  not reproduced")


# ── R4: TC-1 — terminal tool orphans the rest of the batch ──────────────────

def repro_tc1():
    print("\n=== R4 / TC-1: [submit_plan, read_file] batch ===")
    ws = make_workspace({"a.go": "package a\n"})
    plan_args = json.dumps({"summary": "s", "steps": [{"file": "a.go", "action": "edit", "accepts": "builds"}]})
    client = ScriptedClient([
        chat_turn(tool_calls=[
            ToolCall("t1", "submit_plan", plan_args),
            ToolCall("t2", "read_file", json.dumps({"path": "a.go"})),
        ]),
        chat_turn(tool_calls=[ToolCall("t3", "finish", json.dumps({"answer": "done"}))]),
    ])
    loop = make_loop(client, ws)
    # PLANNER path: intent AGENT starts in planner. Baseline thread will run; workspace has no go.mod stages.
    events = run_all(loop, "change a.go", Intent.AGENT)

    wire = loop.context.wire()
    declared = {c["id"] for m in wire for c in m.get("tool_calls", ())}
    answered = {m.get("tool_call_id") for m in wire if m.get("tool_call_id")}
    orphans = declared - answered
    print(f"  declared tool_call ids: {sorted(declared)}")
    print(f"  answered tool_call ids: {sorted(answered)}")
    print(f"  ORPHANED (declared, never answered): {sorted(orphans)}")
    if "t2" in orphans:
        print("  BUG REPRODUCED: every later request in this session carries an assistant message "
              "declaring t2 with no tool result — a strict endpoint 400s the whole session.")
    else:
        print("  not reproduced")


# ── R5: second compaction discards the first recap ──────────────────────────

def repro_recap_loss():
    print("\n=== R5: repeated compaction loses do_not_retry ===")
    ctx = ContextManager(mode=Mode.AGENT, system_prompt=system_prompt())
    for i in range(6):
        ctx.begin_turn()
        ctx.append_user(f"message {i} " + "pad " * 50)
    ctx.compact(lambda msgs: Recap(goal="first", do_not_retry=("NEVER retry approach A",)), keep_recent=2)
    first_present = any("NEVER retry approach A" in m.content for m in ctx.build())
    for i in range(6):
        ctx.begin_turn()
        ctx.append_user(f"later message {i} " + "pad " * 50)
    ctx.compact(lambda msgs: Recap(goal="second", do_not_retry=("do not retry B",)), keep_recent=2)
    still_present = any("NEVER retry approach A" in m.content for m in ctx.build())
    print(f"  after compaction 1, dead-end present: {first_present}")
    print(f"  after compaction 2, dead-end from compaction 1 present: {still_present}")
    if first_present and not still_present:
        print("  BUG REPRODUCED: the recap is replaced, not merged; dead ends from earlier compactions vanish.")
    else:
        print("  not reproduced")


# ── R6: compaction can retain an orphaned tool result ───────────────────────

def repro_orphan_tail():
    print("\n=== R6: retained set = orphaned tool result ===")
    ctx = ContextManager(mode=Mode.AGENT, system_prompt=system_prompt())
    ctx.begin_turn()
    call = ToolCall("k1", "read_file", json.dumps({"path": "x.go"}))
    ctx.append_assistant("reading", tool_calls=(call,))
    ctx.append_tool_result("read_file", "content " * 2000, tool_call_id="k1")
    ctx.compact(lambda msgs: Recap(goal="g"), keep_recent=1)
    wire = ctx.wire()
    declared = {c["id"] for m in wire for c in m.get("tool_calls", ())}
    tool_msgs = [m for m in wire if m.get("tool_call_id")]
    orphaned = [m["tool_call_id"] for m in tool_msgs if m["tool_call_id"] not in declared]
    print(f"  retained tool messages: {[m['tool_call_id'] for m in tool_msgs]}, declared: {sorted(declared)}")
    if orphaned:
        print(f"  BUG REPRODUCED: retained tool result(s) {orphaned} answer a call no assistant message declares.")
    else:
        print("  not reproduced")


# ── R7: TC-3 — refused finish gets the wrong follow-up instruction ──────────

def repro_tc3():
    print("\n=== R7 / TC-3: finish('') refused, next-turn message ===")
    ws = make_workspace({"a.go": "package a\n"})
    client = ScriptedClient([
        chat_turn(tool_calls=[ToolCall("f1", "finish", json.dumps({"answer": ""}))]),
        chat_turn(tool_calls=[ToolCall("f2", "finish", json.dumps({"answer": "real answer"}))]),
    ])
    loop = make_loop(client, ws)
    run_all(loop, "explain a.go", Intent.ASK)
    msgs = [m.content for m in loop.context.build() if m.role is Role.USER]
    wrong = [m for m in msgs if "already been answered" in m]
    print(f"  follow-up user message after refused finish: {wrong[0][:120]!r}" if wrong else "  (no such message)")
    if wrong:
        print("  BUG REPRODUCED: the loop tells the model its refused `finish` 'has already been answered "
              "and asking it again returns the same thing' — false, and the opposite of the schema error it just got.")
    else:
        print("  not reproduced")


# ── R8: repeated output truncation has no dedicated stop ────────────────────

def repro_truncation_loop():
    print("\n=== R8: repeated truncation only stops at max_turns ===")
    ws = make_workspace({"a.go": "package a\n"})
    cut = ChatResult(
        content="", tool_calls=[ToolCall("z", "write_file", '{"path": "b.go", "content": "x')],
        finish_reason="length", usage=Usage(prompt_tokens=100, completion_tokens=100),
    )
    turns = 8
    client = ScriptedClient([cut] * (turns * 2))
    loop = make_loop(client, ws, max_turns=turns)
    events = run_all(loop, "write b.go", Intent.ASK)
    outcome = loop.result.outcome if loop.result else "?"
    print(f"  scripted: every reply truncated mid-tool-call; loop ran {loop.context.turn} turns, "
          f"outcome={outcome}, stalled_turns={loop.state.stalled_turns}, research_turns={loop.state.research_turns}")
    if loop.context.turn >= turns and outcome == "exhausted":
        print("  BUG REPRODUCED: no truncation counter — a model that always overruns burns the whole "
              "turn budget (40 by default) before anything stops it.")
    else:
        print("  not reproduced")


# ── R9: whole-file re-read with unknown length + start-only reads ───────────

def repro_reread_open_end():
    print("\n=== R9: open-ended re-read (start given, end omitted, length unknown) ===")
    led = _ReadLedger()
    led.add(50, 50)  # exactly one line seen; file length never reported
    loop = make_loop(ScriptedClient([]), make_workspace({"a.go": "x"}))
    loop.state.reads["h.go"] = led
    verdict = loop._re_reading(ToolCall("q", "read_file", json.dumps({"path": "h.go", "start": 50})))
    print(f"  ledger covers only line 50; call asks 50..EOF; verdict: {verdict[:80]!r}")
    if verdict:
        print("  BUG REPRODUCED: an open-ended read (start=50, no end) is refused because line 50 alone "
              "was seen — the tail past 50 was never delivered.")
    else:
        print("  not reproduced")


if __name__ == "__main__":
    repro_cm1()
    repro_cm2()
    repro_cm3()
    repro_tc1()
    repro_recap_loss()
    repro_orphan_tail()
    repro_tc3()
    repro_truncation_loop()
    repro_reread_open_end()
