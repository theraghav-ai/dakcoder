#!/usr/bin/env python3
"""Render /v1/health's capability block for status.sh.

A file rather than a `python3 -c` string inside the shell script: the quoting
needed to nest an f-string in a single-quoted shell argument is unreadable, and
it was wrong — the block printed nothing at all, which read as "the probe has
not run" rather than "this line is broken".
"""
import json
import sys

try:
    health = json.load(sys.stdin)
except Exception:
    print("    gateway not answering")
    raise SystemExit(0)

caps = health.get("capabilities", {})
print(f"    identity={caps.get('identity', 'unknown')}  ok={caps.get('ok', caps.get('status'))}")
for name, check in (caps.get("checks") or {}).items():
    print(f"    {check['status']:<6} {name:<30} {check['duration_ms']:>5} ms")
    if check["status"] != "pass":
        print(f"           {check['detail'][:100]}")

# Which model answers for which role. Printed here for the same reason the
# probe is: a routing change applied by restarting the gateway is one you should
# be able to see, rather than infer from how the Planner starts behaving.
models = health.get("models") or {}
if models:
    print()
    print("  models:")
    for role, model in models.items():
        print(f"    {role:<12} {model}")
