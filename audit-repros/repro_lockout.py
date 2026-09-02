import json, sys
sys.path.insert(0, "/mnt/data/raghav/dakcoder/apps/agent/src")
sys.path.insert(0, "/mnt/data/raghav/dakcoder/apps/shared/src")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from repro_audit import ScriptedClient, chat_turn, make_workspace, make_loop
from dakcoder_shared.llm import ToolCall
from dakcoder_agent.modes import Intent

print("=== R11 / STM-1: acting phase forced to finish after 12 tool turns ===")
files = {"a.go": "package a\n"}
for i in range(20):
    files[f"src{i}.txt"] = "\n".join(f"l{j}" for j in range(50))
ws = make_workspace(files)

turns = [
    chat_turn(tool_calls=[ToolCall("p", "submit_plan", json.dumps(
        {"summary": "s", "steps": [{"file": "a.go", "action": "edit", "accepts": "ok"}]}))]),
    # acting turn 1: write the plan's file (all targets now touched)
    chat_turn(tool_calls=[ToolCall("w", "patch_file", json.dumps(
        {"path": "a.go", "old": "package a", "new": "package a // edited"}))]),
]
# acting turns 2..13: legitimate, novel reads (each a different file)
for i in range(12):
    turns.append(chat_turn(tool_calls=[ToolCall(f"r{i}", "read_file", json.dumps({"path": f"src{i}.txt"}))]))
# whatever comes after the fence
turns += [chat_turn(tool_calls=[ToolCall("fz", "finish", json.dumps({"answer": "forced out"}))])] * 3

client = ScriptedClient(turns)
loop = make_loop(client, ws, max_turns=25)
list(loop.run("edit a.go", intent=Intent.AGENT))

for i, req in enumerate(client.requests):
    tc = req.get("tool_choice")
    if tc:
        print(f"  request {i}: tool_choice={tc}")
print(f"  research_turns at end: {loop.state.research_turns}")
print(f"  outcome: {loop.result.outcome if loop.result else '?'} — {loop.result.summary[:100] if loop.result else ''}")
forced = [r for r in client.requests if isinstance(r.get("tool_choice"), dict)
          and r["tool_choice"].get("function", {}).get("name") == "finish"]
if forced:
    print("  BUG REPRODUCED: after 12 tool-calling acting turns with plan targets touched, the loop")
    print("  dispatches with tool_choice={'function':{'name':'finish'}} — if the gate had just failed and told")
    print("  the model 'make the edit', the model is simultaneously FORBIDDEN from calling patch_file.")
else:
    print("  not reproduced")
