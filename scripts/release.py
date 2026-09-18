#!/usr/bin/env python3
"""Bump every version declaration and rebuild every shipped artifact.

    python scripts/release.py 0.3.7

Five files declare the version and they must agree: the extension manifest and
the four `pyproject.toml`s. Everything downstream is derived from them — the
`gotools` binaries are stamped with the version at link time, the wheels carry
it in their filename, and the `.vsix` takes its name from the manifest — so a
bump that is not followed by a rebuild ships the previous build under the new
number.

The order below is the part worth preserving. The binaries are rebuilt before
the extension is verified, because `check:gotools` compares them against the
checksum manifest; the wheels are rebuilt before the `.vsix` is packaged,
because `vsce` copies whatever is sitting in `extension/runtime` at that moment.
Getting that last one wrong is silent: the package succeeds and ships the
previous release's runtime.

Run from anywhere; paths are resolved against the repository root.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "extension"
RUNTIME = EXTENSION / "runtime"

#: The version is declared in exactly these five places. Each pattern anchors
#: the *top-level* declaration: `package.json` carries other `"version"` keys
#: further in, under deeper indentation, and `pyproject.toml`'s is the only key
#: at column zero.
VERSION_FILES = [
    (EXTENSION / "package.json", re.compile(r'^(  "version": ")([^"]+)(")', re.M)),
    (ROOT / "apps" / "agent" / "pyproject.toml", re.compile(r'^(version = ")([^"]+)(")', re.M)),
    (ROOT / "apps" / "shared" / "pyproject.toml", re.compile(r'^(version = ")([^"]+)(")', re.M)),
    (ROOT / "apps" / "gateway" / "pyproject.toml", re.compile(r'^(version = ")([^"]+)(")', re.M)),
    (ROOT / "apps" / "agentsvc" / "pyproject.toml", re.compile(r'^(version = ")([^"]+)(")', re.M)),
]

#: The two wheels this repository builds. The rest of `extension/runtime` is a
#: vendored third-party closure and is left alone.
OWN_WHEELS = ("dakcoder_agent", "dakcoder_shared")

GITIGNORE_BEGIN = "# --- Packaged extensions (managed by scripts/release.py) ---"
GITIGNORE_END = "# --- end packaged extensions ---"

BOLD, DIM, RED, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
    BOLD = DIM = RED = GREEN = YELLOW = RESET = ""

_step = 0


def step(title: str) -> None:
    global _step
    _step += 1
    print(f"\n{BOLD}[{_step}] {title}{RESET}")


def info(msg: str) -> None:
    print(f"    {msg}")


def warn(msg: str) -> None:
    print(f"    {YELLOW}! {msg}{RESET}")


def die(msg: str) -> "None":
    print(f"\n{RED}error: {msg}{RESET}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    """Run a command, streaming its output, and abort the release if it fails."""
    printable = " ".join(str(c) for c in cmd)
    print(f"    {DIM}$ {printable}{RESET}")
    exe = shutil.which(str(cmd[0]))
    if exe is None:
        die(f"{cmd[0]} is not on PATH")
    merged = {**os.environ, **(env or {})}
    result = subprocess.run([exe, *[str(c) for c in cmd[1:]]], cwd=cwd or ROOT, env=merged)
    if result.returncode != 0:
        die(f"`{printable}` failed with exit code {result.returncode}")


def capture(cmd: list[str], cwd: Path | None = None) -> str:
    exe = shutil.which(str(cmd[0]))
    if exe is None:
        die(f"{cmd[0]} is not on PATH")
    result = subprocess.run(
        [exe, *[str(c) for c in cmd[1:]]],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------- preflight


def pick_python() -> str:
    """The interpreter used to build the wheels.

    Prefers the repository's virtualenv, because that is where `build` and a
    modern `setuptools` are known to be. Falls back to the interpreter running
    this script so the release still works on a machine without `.venv`.
    """
    candidates = [
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        probe = subprocess.run(
            [str(candidate), "-c", "import build"], capture_output=True, text=True
        )
        if probe.returncode == 0:
            return str(candidate)
    die(
        "no interpreter with the `build` module was found.\n"
        "       Install it into the venv:  .venv/Scripts/python -m pip install build\n"
        "       (or into whichever interpreter you intend to build the wheels with)"
    )


def preflight(args: argparse.Namespace) -> str:
    step("Preflight")
    for tool in ("node", "npm", "go", "git"):
        if shutil.which(tool) is None:
            die(f"{tool} is not on PATH; the release needs node, npm, go and git")
    if not (EXTENSION / "node_modules").is_dir():
        die("extension/node_modules is missing - run `npm ci` in extension/ first")
    python = pick_python()
    info(f"go       {capture(['go', 'version'])}")
    info(f"node     {capture(['node', '--version'])}")
    info(f"python   {python}")

    dirty = capture(["git", "status", "--porcelain"])
    if dirty and not args.allow_dirty:
        tracked = [ln for ln in dirty.splitlines() if not ln.startswith("??")]
        if tracked:
            warn(f"{len(tracked)} tracked file(s) already modified - the release will add to them")
    return python


# ------------------------------------------------------------------- bump


def bump(version: str) -> str | None:
    step(f"Bumping every version declaration to {version}")
    previous: str | None = None
    for path, pattern in VERSION_FILES:
        if not path.exists():
            die(f"{path} does not exist")
        text = path.read_text(encoding="utf-8")
        match = pattern.search(text)
        if match is None:
            die(f"no top-level version declaration found in {path}")
        current = match.group(2)
        previous = previous or current
        if current != version:
            # newline='' so the file's existing line endings survive the rewrite.
            updated = pattern.sub(rf"\g<1>{version}\g<3>", text, count=1)
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
            info(f"{path.relative_to(ROOT)}  {current} -> {version}")
        else:
            info(f"{path.relative_to(ROOT)}  already {version}")
    return previous


# ---------------------------------------------------------------- artifacts


def build_gotools() -> None:
    """Cross-compile the sidecar for every shipped platform, stamped with the
    new version, and rewrite the checksum manifest the extension verifies."""
    step("Rebuilding the gotools binaries")
    run(["npm", "run", "build:gotools"], cwd=EXTENSION)


def build_wheels(python: str, no_isolation: bool) -> None:
    """Rebuild this repository's two wheels into the vendored runtime.

    Every previous version is removed first. Leaving them behind is not
    harmless: `vsce` ships the whole directory, so the `.vsix` would carry both
    releases and grow by a wheel per version.
    """
    step("Rebuilding the vendored wheels")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for name in OWN_WHEELS:
        for stale in RUNTIME.glob(f"{name}-*.whl"):
            info(f"removing {stale.name}")
            stale.unlink()

    flags = ["--wheel", "--outdir", str(RUNTIME)]
    if no_isolation:
        # For an offline machine: reuses the interpreter's own setuptools
        # instead of fetching a build environment.
        flags.append("--no-isolation")
    # shared first: the agent wheel declares a dependency on it.
    for package in ("shared", "agent"):
        run([python, "-m", "build", *flags, str(ROOT / "apps" / package)])

    # `python -m build` leaves a full copy of every source file behind. They are
    # gitignored, but they are also the copy reviewers read by mistake.
    for leftover in list(ROOT.glob("apps/*/build")) + list(ROOT.glob("apps/*/*.egg-info")):
        shutil.rmtree(leftover, ignore_errors=True)


def run_python_tests(python: str, full: bool) -> None:
    step("Running the Python test suite")
    if full:
        warn("including the slow end-to-end tests")
        run([python, "-m", "pytest", "apps", "--tb=short"])
    else:
        info("excluding the slow end-to-end suite (-m 'not slow')")
        run([python, "-m", "pytest", "apps", "-m", "not slow", "--tb=short"])


def snapshot_contract(python: str, version: str) -> None:
    """Record what this release promises clients, after checking it still
    promises everything the last one did. After the tests, before packaging:
    a release whose contract shrank stops here."""
    step("Snapshotting the wire contract")
    run([python, str(ROOT / "scripts" / "contract-baseline.py"), "--release", version])


def package_extension() -> None:
    """`npm run package` is verify + vsce: typecheck, unit tests, esbuild, the
    credential/command/l10n/checksum checks, then the `.vsix` itself."""
    step("Verifying and packaging the extension")
    run(["npm", "run", "package"], cwd=EXTENSION)


# ------------------------------------------------------------------- verify


def host_binary() -> Path | None:
    system = {"win32": "win32", "darwin": "darwin", "linux": "linux"}.get(sys.platform)
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if system is None:
        return None
    name = f"gotools-{system}-{arch}" + (".exe" if system == "win32" else "")
    candidate = EXTENSION / "bin" / name
    return candidate if candidate.exists() else None


def verify(version: str) -> Path:
    """Check that the artifacts on disk actually carry the new version.

    This exists because the failure it catches is silent. A `.vsix` packaged
    over a stale `extension/runtime` is a valid, installable extension that
    reports the new version in the marketplace and runs the previous release's
    agent.
    """
    step("Verifying the artifacts")

    for name in OWN_WHEELS:
        expected = RUNTIME / f"{name}-{version}-py3-none-any.whl"
        if not expected.exists():
            die(f"{expected.name} is missing - the wheel build did not produce this version")
        info(f"{expected.name}")
    strays = [
        p.name
        for name in OWN_WHEELS
        for p in RUNTIME.glob(f"{name}-*.whl")
        if p.name != f"{name}-{version}-py3-none-any.whl"
    ]
    if strays:
        die(f"older wheels are still in extension/runtime and would ship: {', '.join(strays)}")

    binary = host_binary()
    if binary is None:
        warn("no gotools binary for this platform to spot-check (the manifest was still verified)")
    else:
        stamped = capture([str(binary), "--version"])
        if stamped != version:
            die(f"{binary.name} reports {stamped!r}, expected {version!r}")
        info(f"{binary.name} reports {stamped}")

    vsix = EXTENSION / f"dakcoder-go-{version}.vsix"
    if not vsix.exists():
        die(f"{vsix.name} was not produced")
    info(f"{vsix.name}  ({vsix.stat().st_size / 1_048_576:.2f} MB)")
    return vsix


# ---------------------------------------------------------------- gitignore


def update_gitignore(version: str, untrack: bool) -> None:
    """Ignore every packaged extension except the one just built.

    A `.gitignore` entry does nothing to a file git already tracks, and thirteen
    of these were committed — roughly 240 MB of build output in history. So the
    older ones are dropped from the index as well. Nothing is removed from disk:
    `git rm --cached` unstages, it does not delete.
    """
    step("Updating .gitignore")
    keep = f"dakcoder-go-{version}.vsix"
    block = [
        GITIGNORE_BEGIN,
        "# A .vsix is build output — 18 MB of it per release. Only the current",
        "# one stays visible to git; `scripts/release.py` moves the exception",
        "# forward on every bump.",
        "extension/*.vsix",
        f"!extension/{keep}",
        GITIGNORE_END,
    ]

    path = ROOT / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()

    if GITIGNORE_BEGIN in lines and GITIGNORE_END in lines:
        start, end = lines.index(GITIGNORE_BEGIN), lines.index(GITIGNORE_END)
        lines[start : end + 1] = block
    else:
        # Drop any hand-written per-version entries this block supersedes.
        lines = [ln for ln in lines if not re.match(r"^!?extension/.*\.vsix\s*$", ln)]
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", *block]

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    info(f"ignoring extension/*.vsix, except {keep}")

    if not untrack:
        return
    tracked = [p for p in capture(["git", "ls-files", "extension/*.vsix"]).splitlines() if p.strip()]
    stale = [p for p in tracked if not p.endswith(keep)]
    if not stale:
        info("no previously tracked .vsix files to untrack")
        return
    run(["git", "rm", "--cached", "--quiet", *stale])
    info(f"untracked {len(stale)} .vsix file(s) - still on disk, staged as deletions")


# -------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bump the version and rebuild every shipped artifact.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:  python scripts/release.py 0.3.7",
    )
    parser.add_argument("version", help="the new version, e.g. 0.3.7")
    parser.add_argument("--skip-tests", action="store_true", help="skip the Python test suite")
    parser.add_argument(
        "--full-tests",
        action="store_true",
        help="include the slow end-to-end tests (test_happy_path has known pre-existing failures)",
    )
    parser.add_argument(
        "--no-isolation",
        action="store_true",
        help="build the wheels without an isolated build env (for an offline machine)",
    )
    parser.add_argument(
        "--keep-tracked-vsix",
        action="store_true",
        help="update .gitignore but leave already-committed .vsix files in the index",
    )
    parser.add_argument(
        "--allow-dirty", action="store_true", help="do not warn about existing local modifications"
    )
    args = parser.parse_args()

    version = args.version.strip().lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        die(f"{args.version!r} is not a three-part version; VS Code requires major.minor.patch")

    print(f"{BOLD}dakcoder release {version}{RESET}")
    python = preflight(args)
    previous = bump(version)
    build_gotools()
    build_wheels(python, args.no_isolation)
    if not args.skip_tests:
        run_python_tests(python, args.full_tests)
    snapshot_contract(python, version)
    package_extension()
    vsix = verify(version)
    update_gitignore(version, untrack=not args.keep_tracked_vsix)

    print(f"\n{GREEN}{BOLD}Release {version} built.{RESET}")
    if previous and previous != version:
        print(f"  bumped from {previous}")
    print(f"  {vsix.relative_to(ROOT)}")
    print("\nStill yours to do:")
    print(f"  - write the CHANGELOG entry for {version}")
    print("  - review `git status`, then commit")


if __name__ == "__main__":
    main()
