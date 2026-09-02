import json, sys
sys.path.insert(0, "/mnt/data/raghav/dakcoder/apps/agent/src")
sys.path.insert(0, "/mnt/data/raghav/dakcoder/apps/shared/src")
from repro_audit import ScriptedClient, chat_turn, make_workspace, make_loop, run_all
from dakcoder_shared.llm import ToolCall
from dakcoder_agent.modes import Intent

print("=== R10 / TC-2: follow-up carry undone by fresh Router ===")
ws = make_workspace({"a.go": "package a\nvar X = 1\n"})

# Run 1: a write (mutation) then a repeated search, then finish.
c1 = ScriptedClient([
    chat_turn(tool_calls=[ToolCall("w1", "patch_file", json.dumps({"path": "a.go", "old": "var X = 1", "new": "var X = 2"}))]),
    chat_turn(tool_calls=[ToolCall("s1", "search_repo", json.dumps({"pattern": "NoSuchThing"}))]),
    chat_turn(tool_calls=[ToolCall("f1", "finish", json.dumps({"answer": "done"}))]),
])
loop1 = make_loop(c1, ws)
# intent ASK would refuse patch_file (mode gate). Use AGENT? planner refuses too.
# Drive in ASK anyway: patch_file refused by mode; the search still lands in ledgers.
run_all(loop1, "task one", Intent.ASK)
print(f"  run1 seen_calls: {list(loop1.state.seen_calls)}")
print(f"  run1 router.mutations: {loop1.router.mutations}, mutations_seen: {loop1.state.mutations_seen}")

# Simulate a mutation having happened in run 1 (as a real AGENT run would have):
loop1.state.mutations_seen = 3
loop1.router.mutations = 3

# Follow-up: fresh loop + FRESH router (exactly what loopback._spawn does), carry_from
c2 = ScriptedClient([
    chat_turn(tool_calls=[ToolCall("s2", "search_repo", json.dumps({"pattern": "NoSuchThing"}))]),
    chat_turn(tool_calls=[ToolCall("f2", "finish", json.dumps({"answer": "done"}))]),
])
loop2 = make_loop(c2, ws)          # new Router: mutations == 0
loop2.context = loop1.context       # context carried, as loopback does
loop2.carry_from(loop1)
carried = dict(loop2.state.seen_calls)
print(f"  carried seen_calls into follow-up: {list(carried)} (mutations_seen={loop2.state.mutations_seen})")
run_all(loop2, "follow-up", Intent.ASK)
print(f"  after first tool batch of follow-up, seen_calls: {list(loop2.state.seen_calls)}")
survived = any(k in loop2.state.seen_calls and loop2.state.seen_calls[k] > 1 for k in carried)
if carried and not survived:
    print("  BUG REPRODUCED: router.mutations(0) != carried mutations_seen(3) at the first batch wipes "
          "every carried ledger before it can answer anything — the carry is a no-op after any mutating run.")
else:
    print("  not reproduced (carried counts survived)")
