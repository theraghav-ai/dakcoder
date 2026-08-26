"""How the daemon finds the sidecar.

Its own module rather than a few more cases in ``test_gotools_bridge.py``,
because that one skips wholesale when no binary is present — it validates the
resolver with the resolver, so the resolver is exactly what it cannot test.
These are plain unit tests and always run.
"""

from __future__ import annotations

from pathlib import Path

from dakcoder_agent.tools.gotools import _find_binary


def _decoy(directory: Path) -> Path:
    """A file named the way ``shutil.which`` expects to find one."""
    name = "gotools.exe" if __import__("os").name == "nt" else "gotools"
    path = directory / name
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_the_env_var_wins_over_path(tmp_path, monkeypatch) -> None:
    """The extension ships a platform-suffixed binary and resolves it against a
    checksum. Whatever a developer's shell happens to hold is not that."""
    packaged = tmp_path / "gotools-win32-x64.exe"
    packaged.write_text("", encoding="utf-8")
    on_path = tmp_path / "elsewhere"
    on_path.mkdir()
    _decoy(on_path)

    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.setenv("GOTOOLS_PATH", str(packaged))
    assert _find_binary() == str(packaged)


def test_a_stale_env_var_falls_through_to_path(tmp_path, monkeypatch) -> None:
    """An uninstalled extension leaves the variable pointing at nothing. That is
    a reason to keep looking, not to fail."""
    on_path = tmp_path / "elsewhere"
    on_path.mkdir()
    decoy = _decoy(on_path)

    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.setenv("GOTOOLS_PATH", str(tmp_path / "gone.exe"))
    # `shutil.which` echoes the PATHEXT casing on Windows, so compare insensitively.
    assert _find_binary().lower() == str(decoy).lower()


def test_an_unset_env_var_is_not_a_path(tmp_path, monkeypatch) -> None:
    """An empty value must not be resolved as the current directory."""
    on_path = tmp_path / "elsewhere"
    on_path.mkdir()
    decoy = _decoy(on_path)

    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.setenv("GOTOOLS_PATH", "   ")
    assert _find_binary().lower() == str(decoy).lower()
