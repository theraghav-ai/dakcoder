"""Spawn the offline-installed runtime exactly as the extension does.

This is the one test that exercises the real seam: a venv built only from the
vendored wheels, spawned with the environment `runtime.ts` constructs, its port
read from stdout, then driven over HTTP. Nothing here is stubbed.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
WHEELS = ROOT / "extension" / "runtime"
VENV = pathlib.Path(os.environ.get("DAKCODER_SMOKE_VENV", tempfile.gettempdir())) / "dakcoder-offline-smoke"
PY = str(VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
WORKSPACE = str(ROOT / "new-template")
TOKEN = "smoke-loopback-token"


def build_venv() -> None:
    """A clean venv filled only from the vendored wheels.

    --no-index is the assertion, not a flag: if any dependency is missing
    from the closure this fails here rather than on a pilot developer's machine
    behind a proxy that will not let pip out.
    """
    if VENV.exists():
        shutil.rmtree(VENV, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    agent = next(WHEELS.glob("dakcoder_agent-*.whl"), None)
    if agent is None:
        sys.exit(f"no agent wheel in {WHEELS}; run: python -m build --wheel --outdir {WHEELS} apps/agent")
    subprocess.run(
        [PY, "-m", "pip", "install", "--no-index", "--find-links", str(WHEELS),
         "--disable-pip-version-check", "--quiet", f"{agent}[yaml]"],
        check=True,
    )
    print(f"installed offline from {len(list(WHEELS.glob('*.whl')))} vendored wheels")


build_venv()

env = dict(os.environ)
env.update(
    {
        "DAKCODER_MODE": "local",
        "DAKCODER_GATEWAY_URL": "https://aiops.cept.gov.in/coder/backend",
        "DAKCODER_GATEWAY_TOKEN": TOKEN,
        # The runtime refuses to start without one, correctly: every model call
        # goes through the gateway as the developer, and there is no local key.
        "DAKCODER_JWT": "test.jwt.value",
        "DAKCODER_VERSION": "0.1.0",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
)
# Exactly what runtime.ts strips. A model key must never reach the child, even
# one a developer exported for an unrelated project.
for name in (
    "DAKCODER_MODEL_API_KEY",
    "DAKCODER_MODEL_BASE_URL",
    "OPENAI_API_KEY",
    "LITELLM_API_KEY",
    "ANTHROPIC_API_KEY",
):
    env.pop(name, None)

print("spawning:", PY, "-m dakcoder_agent.serve --workspace", WORKSPACE, "--port 0 --no-prewarm")
child = subprocess.Popen(
    [PY, "-m", "dakcoder_agent.serve", "--workspace", WORKSPACE, "--port", "0", "--no-prewarm"],
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
)

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


try:
    # The announcement, read from stdout before the server serves.
    line = child.stdout.readline()
    announced = json.loads(line)
    print("\nannounced:", announced)
    check("port announced on stdout", isinstance(announced.get("port"), int))
    check("pid announced", isinstance(announced.get("pid"), int))
    check("version announced", announced.get("version") == "0.1.0")

    base = f"http://127.0.0.1:{announced['port']}"

    def call(method, path, body=None, token=TOKEN):
        req = urllib.request.Request(
            base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "content-type": "application/json",
                **({"authorization": f"Bearer {token}"} if token else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    # Poll health the way the extension does.
    print("\npolling /v1/health…")
    deadline = time.time() + 30
    health = None
    while time.time() < deadline:
        try:
            status, health = call("GET", "/v1/health", token=None)
            if status == 200:
                break
        except Exception:
            time.sleep(0.2)
    print("health:", json.dumps(health, indent=1)[:400])

    check("health needs no token", health is not None and health.get("ok") is True)
    check("api_version is 1.0", (health or {}).get("api_version") == "1.0")
    check("workspace reported", bool((health or {}).get("workspace")))

    status, tools = call("GET", "/v1/tools")
    check("tool catalogue served", status == 200 and len(tools.get("tools", [])) == 29,
          f"{len(tools.get('tools', []))} tools")

    status, _ = call("GET", "/v1/tools", token="wrong-token")
    check("a wrong token is refused", status == 401)

    status, sessions = call("GET", "/v1/sessions")
    check("sessions listed", status == 200 and sessions.get("sessions") == [])

    # The routes added for the extension.
    status, body = call("POST", "/v1/sessions/nope/resume")
    check("resume 404s an unknown session", status == 404)

    status, body = call("GET", "/v1/sessions/nope/context")
    check("context 404s an unknown session", status == 404)

    status, body = call("POST", "/v1/approvals/deadbeef/extend")
    check("extend 410s a gone approval", status == 410, body.get("error", ""))

    status, body = call("POST", "/v1/sessions/nope/messages", {"text": "hi"})
    check("steer 404s an unknown session", status == 404)

    status, body = call("POST", "/v1/approvals/x", {"decision": "banana"})
    check("a bad decision is refused", status in (400, 410))

finally:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
    stderr = child.stderr.read()
    if stderr.strip():
        print("\nstderr:", stderr.strip()[:600])

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all spawn checks passed")
