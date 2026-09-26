"""AGENTS.md: read into every run, surfaced below the root, kept current by sessions.

The behaviour is the AGENTS.md standard as the agents that read it have settled
it (agents.md, Codex, Claude Code, Gemini CLI), so most of these tests pin a
rule another tool already follows: one file per directory with the override
first, the closest file last, imports four deep and never out of the
repository, a byte budget that names what it cut. The rest pin what makes
upkeep safe: the developers' text is never rewritten, credentials never land in
a committed file, and a memory that only grows is refused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dakcoder_agent.context import ContextManager
from dakcoder_agent.hooks import HookContext
from dakcoder_agent.messages import Layer
from dakcoder_agent.modes import Intent, Mode
from dakcoder_agent.tools import agents_md, control
from dakcoder_agent.tools.agents_md import (
    BEGIN,
    END,
    MAX_NOTES,
    NestedInstructions,
    NoteError,
    apply_notes,
    load,
)
from dakcoder_agent.tools.router import ApprovalRequest, Router
from dakcoder_shared.envelope import ToolResult
from dakcoder_shared.llm import ToolCall
from dakcoder_shared.paths import Workspace
from scripted import ScriptedClient, calls  # noqa: E402


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """No user-level file unless a test writes one: the developer's own
    ~/.dakcoder/AGENTS.md must never leak into a test."""
    home = tmp_path / "_home" / ".dakcoder"
    return {"DAKCODER_HOME": str(home)}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


# ── reading ─────────────────────────────────────────────────────────────────


def test_a_repository_without_one_is_told_to_start_one(repo: Path, env) -> None:
    docs = load(repo, env=env)
    assert not docs.exists
    assert docs.sources == ()
    assert "no AGENTS.md yet" in docs.text
    assert "update_agents_md" in docs.text, "the upkeep rule is the point of an empty block"


def test_the_root_file_is_read(repo: Path, env) -> None:
    write(repo / "AGENTS.md", "Run `make test` before finishing.")
    docs = load(repo, env=env)
    assert docs.exists
    assert "Run `make test` before finishing." in docs.text
    assert [s.label for s in docs.sources] == ["AGENTS.md"]


def test_the_override_replaces_agents_md_in_its_directory(repo: Path, env) -> None:
    write(repo / "AGENTS.md", "team rule")
    write(repo / "AGENTS.override.md", "temporary rule")
    docs = load(repo, env=env)
    assert "temporary rule" in docs.text
    assert "team rule" not in docs.text


def test_a_fallback_stands_in_only_when_there_is_no_agents_md(repo: Path, env) -> None:
    write(repo / "CLAUDE.md", "claude rules")
    assert "claude rules" in load(repo, env=env).text

    write(repo / "AGENTS.md", "agents rules")
    text = load(repo, env=env).text
    assert "agents rules" in text
    assert "claude rules" not in text, "one team file per directory, never two copies"


def test_the_fallbacks_are_configurable_and_can_be_switched_off(repo: Path, env) -> None:
    write(repo / "CLAUDE.md", "claude rules")
    assert "claude rules" not in load(repo, env={**env, "DAKCODER_PROJECT_DOC_FALLBACKS": ""}).text
    write(repo / "TEAM.md", "team guide")
    assert "team guide" in load(repo, env={**env, "DAKCODER_PROJECT_DOC_FALLBACKS": "TEAM.md"}).text


def test_copilot_instructions_count_at_the_repository_root(repo: Path, env) -> None:
    write(repo / ".github" / "copilot-instructions.md", "copilot rules")
    assert "copilot rules" in load(repo, env=env).text


def test_the_personal_file_comes_after_the_team_file(repo: Path, env) -> None:
    write(repo / "AGENTS.md", "TEAM")
    write(repo / "AGENTS.local.md", "MINE")
    text = load(repo, env=env).text
    assert text.index("TEAM") < text.index("MINE")


def test_the_walk_runs_from_the_git_root_down_so_the_closest_file_is_last(
    repo: Path, env
) -> None:
    """A service opened as a subdirectory of a monorepo still gets the
    monorepo's rules, and its own are read after them -- the later wins."""
    write(repo / "AGENTS.md", "MONOREPO")
    service = repo / "services" / "pension"
    write(service / "AGENTS.md", "SERVICE")
    docs = load(service, env=env)
    assert docs.text.index("MONOREPO") < docs.text.index("SERVICE")
    assert [s.label for s in docs.sources] == ["../../AGENTS.md", "AGENTS.md"]


def test_the_user_file_comes_first(repo: Path, env) -> None:
    write(Path(env["DAKCODER_HOME"]) / "AGENTS.md", "USER")
    write(repo / "AGENTS.md", "PROJECT")
    docs = load(repo, env=env)
    assert docs.text.index("USER") < docs.text.index("PROJECT")
    assert docs.sources[0].scope == "user"


def test_imports_are_followed_relative_to_the_importing_file(repo: Path, env) -> None:
    write(repo / "AGENTS.md", "See @docs/build.md for commands.")
    write(repo / "docs" / "build.md", "BUILD: make build")
    docs = load(repo, env=env)
    assert "BUILD: make build" in docs.text
    assert [s.scope for s in docs.sources] == ["project", "import"]


def test_imports_are_not_read_from_code_or_from_things_that_are_not_paths(
    repo: Path, env
) -> None:
    write(repo / "a.md", "A-IMPORTED")
    write(
        repo / "AGENTS.md",
        "\n".join(
            [
                "Mail ops@a.md, read @skill:legacy-migration, span `@a.md`.",
                "```",
                "@a.md",
                "```",
            ]
        ),
    )
    assert "A-IMPORTED" not in load(repo, env=env).text


def test_imports_stop_at_the_depth_limit_and_survive_a_cycle(repo: Path, env) -> None:
    write(repo / "AGENTS.md", "@l1.md")
    for i in range(1, 7):
        write(repo / f"l{i}.md", f"LEVEL{i} @l{i + 1}.md")
    write(repo / "l7.md", "LEVEL7 @AGENTS.md")
    text = load(repo, env=env).text
    assert "LEVEL4" in text
    assert "LEVEL5" not in text


def test_an_import_cannot_leave_the_repository(tmp_path: Path, repo: Path, env) -> None:
    write(tmp_path / "secret.md", "OUTSIDE")
    write(repo / "AGENTS.md", "@../secret.md")
    assert "OUTSIDE" not in load(repo, env=env).text


def test_the_byte_budget_cuts_at_a_line_and_names_what_it_dropped(
    repo: Path, env
) -> None:
    write(repo / "AGENTS.md", "\n".join(f"rule {i:04d}" for i in range(400)))
    write(repo / "AGENTS.local.md", "LOCAL")
    docs = load(repo, env={**env, "DAKCODER_PROJECT_DOC_MAX_BYTES": "1024"})
    assert docs.sources[0].truncated
    assert "was cut here" in docs.text
    assert "rule 0000" in docs.text and "rule 0399" not in docs.text
    assert docs.dropped == ("AGENTS.local.md",)
    assert "AGENTS.local.md" in docs.text, "a dropped file is named, never silently lost"


def test_the_block_is_byte_stable_across_line_endings_and_a_bom(repo: Path, env) -> None:
    """A head whose bytes depend on a checkout's git config is a different
    cache key on a colleague's machine."""
    write(repo / "AGENTS.md", "﻿one\r\ntwo\r\n")
    crlf = load(repo, env=env).text
    write(repo / "AGENTS.md", "one\ntwo\n")
    assert load(repo, env=env).text == crlf
    assert "\r" not in crlf


def test_the_feature_can_be_switched_off(repo: Path, env) -> None:
    write(repo / "AGENTS.md", "rules")
    assert load(repo, env={**env, "DAKCODER_PROJECT_DOCS": "0"}).text == ""


# ── the context layer ───────────────────────────────────────────────────────


def _manager() -> ContextManager:
    return ContextManager(mode=Mode.AGENT, system_prompt="SYSTEM")


def test_the_block_sits_between_the_system_prompt_and_the_mode_overlay() -> None:
    cm = _manager()
    cm.set_project("PROJECT")
    cm.switch_mode(Mode.AGENT, "MODE")
    cm.set_task("do it")
    layers = [m.layer for m in cm.build()[:4]]
    assert layers == [Layer.SYSTEM, Layer.PROJECT, Layer.MODE, Layer.TASK]


def test_an_unchanged_block_is_a_no_op_and_a_changed_one_says_so() -> None:
    cm = _manager()
    assert cm.set_project("A")
    first = cm.build()[1]
    assert not cm.set_project("A")
    assert cm.build()[1] is first, "identical text must not rebuild the head"
    assert cm.set_project("B")


def test_a_mode_switch_leaves_the_block_where_it_is() -> None:
    cm = _manager()
    cm.set_project("PROJECT")
    head = [m.content for m in cm.build()[:2]]
    for mode in (Mode.ASK, Mode.PLANNER, Mode.AGENT):
        cm.switch_mode(mode, f"overlay {mode}")
        assert [m.content for m in cm.build()[:2]] == head


def test_compaction_never_evicts_the_block() -> None:
    from test_context import _summariser

    cm = _manager()
    cm.set_project("PROJECT RULES")
    cm.set_task("t")
    for i in range(12):
        cm.begin_turn()
        cm.append_assistant(f"step {i}")
    cm.compact(_summariser)
    assert any(m.layer is Layer.PROJECT and m.content == "PROJECT RULES" for m in cm.build())


def test_the_block_is_charged_to_the_budget() -> None:
    cm = _manager()
    before = cm.usage().total
    cm.set_project("word " * 400)
    assert cm.usage().by_layer[Layer.PROJECT] > 0
    assert cm.usage().total > before


# ── nested files ────────────────────────────────────────────────────────────


def _after(nested: NestedInstructions, **arguments) -> str:
    call = ToolCall(id="c1", name="read_file", arguments=json.dumps(arguments))
    context = HookContext(call=call, arguments=arguments, mode=Mode.AGENT, turn=1)
    decision = nested.after(context, ToolResult.success("ok"))
    return decision.note if decision else ""


def test_a_subdirectory_file_is_surfaced_on_first_touch_only(repo: Path) -> None:
    write(repo / "AGENTS.md", "ROOT")
    write(repo / "handler" / "AGENTS.md", "HANDLER RULES")
    write(repo / "handler" / "user.go", "package handler\n")
    nested = NestedInstructions(repo)

    note = _after(nested, path="handler/user.go")
    assert "HANDLER RULES" in note
    assert "handler/" in note
    assert "ROOT" not in note, "the root chain is already pinned"
    assert _after(nested, path="handler/user.go") == ""


def test_a_compaction_surfaces_it_again(repo: Path) -> None:
    write(repo / "handler" / "AGENTS.md", "HANDLER RULES")
    write(repo / "handler" / "user.go", "package handler\n")
    epoch = {"n": 0}
    nested = NestedInstructions(repo, compactions=lambda: epoch["n"])
    assert _after(nested, path="handler/user.go")
    epoch["n"] = 1
    assert "HANDLER RULES" in _after(nested, path="handler/user.go")


def test_every_level_down_to_the_file_is_surfaced_general_first(repo: Path) -> None:
    write(repo / "a" / "AGENTS.md", "LEVEL A")
    write(repo / "a" / "b" / "AGENTS.md", "LEVEL B")
    write(repo / "a" / "b" / "x.go", "package b\n")
    note = _after(NestedInstructions(repo), paths="a/b/x.go")
    assert note.index("LEVEL A") < note.index("LEVEL B")


def test_vendored_instructions_are_not_surfaced(repo: Path) -> None:
    write(repo / "vendor" / "lib" / "AGENTS.md", "SOMEONE ELSE'S RULES")
    write(repo / "vendor" / "lib" / "x.go", "package lib\n")
    assert _after(NestedInstructions(repo), path="vendor/lib/x.go") == ""


def test_a_failed_call_surfaces_nothing(repo: Path) -> None:
    write(repo / "handler" / "AGENTS.md", "HANDLER RULES")
    nested = NestedInstructions(repo)
    call = ToolCall(id="c1", name="read_file", arguments="{}")
    context = HookContext(call=call, arguments={"path": "handler/x.go"}, mode=Mode.AGENT, turn=1)
    assert nested.after(context, ToolResult.failure("no such file")) is None


# ── writing ─────────────────────────────────────────────────────────────────


def test_the_first_note_creates_the_file_with_a_fence(repo: Path, env) -> None:
    edit = apply_notes(repo, op="add", section="commands", notes=["Build with `make build`."], env=env)
    assert edit.created
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# AGENTS.md")
    assert BEGIN in text and END in text
    assert "### Commands\n- Build with `make build`." in text
    # And it is read back on the next run.
    assert "Build with `make build`." in load(repo, env=env).text


def test_a_new_file_imports_the_fallback_it_now_shadows(repo: Path, env) -> None:
    """AGENTS.md beats CLAUDE.md in a directory, so creating it would have
    silently hidden the team's CLAUDE.md. It imports it instead."""
    write(repo / "CLAUDE.md", "CLAUDE RULES")
    apply_notes(repo, op="add", notes=["x"], env=env)
    assert "@CLAUDE.md" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "CLAUDE RULES" in load(repo, env=env).text


def test_the_developers_text_is_never_rewritten(repo: Path, env) -> None:
    human = "# Our service\n\nAlways run make lint.\n\n## Deploy\n\nAsk ops first.\n"
    write(repo / "AGENTS.md", human)
    apply_notes(repo, op="add", section="gotchas", notes=["pgx needs a timeout ctx"], env=env)
    apply_notes(repo, op="add", section="commands", notes=["make test"], env=env)
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith(human.rstrip("\n"))
    assert text.count(BEGIN) == 1


def test_a_note_already_in_the_file_is_not_added_twice(repo: Path, env) -> None:
    write(repo / "AGENTS.md", "- Always run make lint.\n")
    edit = apply_notes(repo, op="add", notes=["always run make lint", "new fact"], env=env)
    assert edit.changed == ("new fact",)
    assert edit.skipped == ("always run make lint",)
    again = apply_notes(repo, op="add", notes=["New fact."], env=env)
    assert again.changed == ()


def test_a_wrong_note_can_be_replaced_in_place_and_removed(repo: Path, env) -> None:
    apply_notes(repo, op="add", section="commands", notes=["test with go test ./...", "lint with make lint"], env=env)
    apply_notes(repo, op="replace", old="go test", notes=["test with make test"], env=env)
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "go test ./..." not in text
    assert text.index("make test") < text.index("make lint"), "replaced where it stood"
    apply_notes(repo, op="remove", old="lint with make lint", env=env)
    assert "make lint" not in (repo / "AGENTS.md").read_text(encoding="utf-8")


def test_a_developers_line_cannot_be_replaced_by_a_session(repo: Path, env) -> None:
    write(repo / "AGENTS.md", "Deploy on Fridays.\n")
    with pytest.raises(NoteError, match="developers write"):
        apply_notes(repo, op="remove", old="Deploy on Fridays", env=env)


def test_an_ambiguous_match_is_refused(repo: Path, env) -> None:
    apply_notes(repo, op="add", notes=["make lint first", "make lint in CI"], env=env)
    with pytest.raises(NoteError, match="2 notes match"):
        apply_notes(repo, op="remove", old="make lint", env=env)


@pytest.mark.parametrize(
    "note",
    [
        "the db password: hunter2hunter2",
        "use token=ghp_abcdefghijklmnopqrstuvwxyz0123",
        "AWS key AKIAABCDEFGHIJKLMNOP",
        "connect to postgres://svc:s3cretpw@db:5432/app",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_a_credential_is_refused(repo: Path, env, note: str) -> None:
    with pytest.raises(NoteError, match="credential"):
        apply_notes(repo, op="add", notes=[note], env=env)
    assert not (repo / "AGENTS.md").exists()


def test_naming_where_a_secret_lives_is_fine(repo: Path, env) -> None:
    apply_notes(repo, op="add", notes=["The DB password comes from $PGPASSWORD."], env=env)


def test_a_paragraph_is_refused(repo: Path, env) -> None:
    with pytest.raises(NoteError, match="one line"):
        apply_notes(repo, op="add", notes=["x" * 400], env=env)


def test_the_section_is_capped(repo: Path, env) -> None:
    apply_notes(repo, op="add", notes=[f"fact {i}" for i in range(MAX_NOTES)], env=env)
    with pytest.raises(NoteError, match="the limit is") as refused:
        apply_notes(repo, op="add", notes=["one too many"], env=env)
    assert "Replace or remove" in refused.value.fix


def test_crlf_files_stay_crlf(repo: Path, env) -> None:
    write(repo / "AGENTS.md", "# Rules\r\n\r\nBe nice.\r\n")
    apply_notes(repo, op="add", notes=["a fact"], env=env)
    raw = (repo / "AGENTS.md").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_an_unclosed_fence_does_not_swallow_the_next_section(repo: Path, env) -> None:
    write(
        repo / "AGENTS.md",
        f"# Rules\n\n{BEGIN} -->\n## Notes from dakcoder sessions\n- old\n\n## Deploy\nAsk ops.\n",
    )
    apply_notes(repo, op="add", notes=["new"], env=env)
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Deploy\nAsk ops." in text
    assert text.index(END) < text.index("## Deploy")


def test_local_notes_go_to_the_personal_file(repo: Path, env) -> None:
    apply_notes(repo, op="add", notes=["I like short answers"], scope="local", env=env)
    assert (repo / "AGENTS.local.md").is_file()
    assert not (repo / "AGENTS.md").exists()


# ── the tool, through the router ────────────────────────────────────────────


@pytest.fixture
def wired(repo: Path, monkeypatch) -> Router:
    monkeypatch.delenv("DAKCODER_AGENTS_MD_APPROVAL", raising=False)
    return Router(Workspace.at(repo), {**agents_md.HANDLERS, **control.HANDLERS})


@pytest.mark.parametrize("mode", list(Mode))
def test_every_mode_can_keep_the_file_current(wired: Router, repo: Path, mode: Mode) -> None:
    outcome = wired.dispatch(
        "update_agents_md", {"text": "make test runs the unit tests", "section": "testing"}, mode=mode
    )
    assert isinstance(outcome, ToolResult) and outcome.ok, outcome
    assert "Added to AGENTS.md (Testing)" in outcome.content
    assert "make test" in (repo / "AGENTS.md").read_text(encoding="utf-8")


def test_a_note_is_not_work_the_gate_judges(wired: Router) -> None:
    """`touched` scopes the gate and feeds the plan's progress; a note is neither."""
    wired.dispatch("update_agents_md", {"text": "a fact"}, mode=Mode.AGENT)
    assert wired.touched == []
    assert wired.mutations == 0


def test_a_refusal_says_what_to_do_instead(wired: Router) -> None:
    outcome = wired.dispatch("update_agents_md", {"text": "password=hunter2hunter2"}, mode=Mode.ASK)
    assert isinstance(outcome, ToolResult) and not outcome.ok
    assert "env var" in outcome.for_model()


def test_approval_on_puts_every_edit_in_front_of_the_developer(
    wired: Router, repo: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DAKCODER_AGENTS_MD_APPROVAL", "ask")
    outcome = wired.dispatch("update_agents_md", {"text": "a fact"}, mode=Mode.AGENT)
    assert isinstance(outcome, ApprovalRequest)
    assert "AGENTS.md (add): a fact" == outcome.reason
    assert not (repo / "AGENTS.md").exists()
    approved = wired.dispatch("update_agents_md", {"text": "a fact"}, mode=Mode.AGENT, approved=True)
    assert isinstance(approved, ToolResult) and approved.ok


def test_finish_remembers_without_a_turn_of_its_own(wired: Router, repo: Path) -> None:
    outcome = wired.dispatch(
        "finish",
        {"answer": "Done.", "remember": ["commands: make build compiles everything", "a plain fact"]},
        mode=Mode.AGENT,
    )
    assert isinstance(outcome, ToolResult) and outcome.ok
    assert "2 note(s) saved" in outcome.content
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "### Commands\n- make build compiles everything" in text
    assert "### Conventions\n- a plain fact" in text


def test_finish_under_approval_hands_the_notes_to_the_developer(
    wired: Router, repo: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DAKCODER_AGENTS_MD_APPROVAL", "ask")
    outcome = wired.dispatch("finish", {"answer": "Done.", "remember": ["a fact"]}, mode=Mode.ASK)
    assert isinstance(outcome, ToolResult) and outcome.ok
    assert "Suggested for AGENTS.md" in outcome.meta["answer"]
    assert not (repo / "AGENTS.md").exists()


def test_a_refused_note_never_fails_the_finish(wired: Router) -> None:
    outcome = wired.dispatch(
        "finish", {"answer": "Done.", "remember": ["token=ghp_abcdefghijklmnopqrstuvwxyz0123"]}, mode=Mode.ASK
    )
    assert isinstance(outcome, ToolResult) and outcome.ok
    assert "not saved" in outcome.content


# ── the loop ────────────────────────────────────────────────────────────────


class _Recording(ScriptedClient):
    def __init__(self, turns) -> None:
        super().__init__(turns, kind="question")
        self.heads: list[list[str]] = []

    def chat(self, messages, **kwargs):
        if kwargs.get("response_format") is None:
            self.heads.append([m.get("content") or "" for m in messages[:3]])
        return super().chat(messages, **kwargs)


def test_a_run_pins_the_file_and_the_next_run_sees_its_own_note(
    wired: Router, repo: Path, env, monkeypatch
) -> None:
    from dakcoder_agent.loop import AgentLoop

    monkeypatch.setenv("DAKCODER_HOME", env["DAKCODER_HOME"])
    write(repo / "AGENTS.md", "Run make lint first.\n")
    client = _Recording(
        [
            calls(("update_agents_md", json.dumps({"text": "handlers live in handler/"}))),
            calls(("finish", json.dumps({"answer": "Noted."}))),
        ]
    )
    loop = AgentLoop(
        ContextManager(mode=Mode.ASK, system_prompt="SYSTEM"),
        client,
        wired,
        project_docs=lambda: load(repo),
    )
    list(loop.run("where do handlers live?", intent=Intent.ASK))

    assert client.heads, "the model was never asked"
    assert client.heads[0][0] == "SYSTEM"
    assert "Run make lint first." in client.heads[0][1]
    # Mid-run the head does not move, even though the file did.
    assert all(h[1] == client.heads[0][1] for h in client.heads)
    assert "handlers live in handler/" not in client.heads[-1][1]

    # The next message of the session pins the new note. A new loop over the
    # same context, which is what `Loopback._spawn` does for a follow-up.
    client.turns = [calls(("finish", json.dumps({"answer": "ok"})))]
    follow_up = AgentLoop(loop.context, client, wired, project_docs=lambda: load(repo))
    list(follow_up.run("thanks", continued=True, intent=Intent.ASK))
    assert "handlers live in handler/" in client.heads[-1][1]


def test_the_prefix_check_names_a_changed_block() -> None:
    from test_phase_state import _headed, _tools_named

    loop = _headed()
    tools = _tools_named("read_file")
    loop._note_prefix(tools)
    loop.context.set_project("NEW RULES")
    assert loop._note_prefix(tools) == "project"
