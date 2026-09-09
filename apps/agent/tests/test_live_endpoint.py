"""What the endpoint actually does, asked of the endpoint.

Skipped unless ``DAKCODER_LIVE=1``, so it never runs in CI and never gates a
commit. Run it before changing how the loop talks to the model.

This file exists because three fixes were shipped on inference about these
answers and all three were wrong:

* ``tool_choice: "none"`` was used to make a stalled turn answer in prose. It
  makes the model emit ``<tool_call>`` markup into ``content`` instead, which
  the loop then served to a developer as the answer.
* ``tools: []`` was proposed as the fix for that. It leaks the same way *and*
  the model invents tools from training -- ``<function=Grep>`` with an
  ``output_mode`` parameter, which is a different harness's signature.
* "the tool's zero-match message causes the loop" was the diagnosis for a run
  that repeated one search eight times. Measured: better wording helps at the
  first step (5/5 correct against 3/5) and cannot be depended on at depth --
  the same text got 5/5 repeats in one session and 0/5 in another.

Every one of those would have been caught here in about a minute. The findings
are written up in ``docs/ENDPOINT-CAPABILITIES.md``; this is the executable half.

    export DAKCODER_JWT=$(grep '^DAKCODER_JWT=' deploy/dakcoder.env | cut -d= -f2-)
    DAKCODER_LIVE=1 python -m pytest apps/agent/tests/test_live_endpoint.py -v -s
"""

from __future__ import annotations

import json
import os

import pytest

from dakcoder_shared.config import local_config
from dakcoder_shared.llm import LLMClient

pytestmark = pytest.mark.skipif(
    os.environ.get("DAKCODER_LIVE") != "1",
    reason="live endpoint test; set DAKCODER_LIVE=1 and DAKCODER_JWT to run",
)

GATEWAY = os.environ.get("DAKCODER_GATEWAY_URL", "https://ai.cept.gov.in/dakcoder")

#: How many samples a behavioural claim needs. The failures this file exists for
#: were all 5/5 or 0/5 -- deterministic enough that five is plenty and cheap
#: enough that more is waste.
SAMPLES = int(os.environ.get("DAKCODER_LIVE_SAMPLES", "5"))

#: Anything that means the model wrote a tool call instead of making one.
MARKUP = ("<tool_call", "<function=", "</tool_call>", "<toolcall")

SEARCH = {
    "type": "function",
    "function": {
        "name": "search_repo",
        "description": "Search file contents by regular expression.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression."},
                "glob": {"type": "string", "description": "Restrict to matching paths."},
            },
            "required": ["pattern"],
        },
    },
}

FINISH = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": (
            "End your turn and hand the developer your answer. Call this when the "
            "work is done, or when going further will not help."
        ),
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "string", "description": "The answer."}},
            "required": ["answer"],
        },
    },
}


@pytest.fixture(scope="module")
def client() -> LLMClient:
    jwt = os.environ.get("DAKCODER_JWT", "").strip()
    if not jwt:
        pytest.skip("DAKCODER_JWT is not set")
    for var in ("OPENAI_API_KEY", "DAKCODER_MODEL_API_KEY", "LITELLM_API_KEY"):
        os.environ.pop(var, None)
    live = LLMClient(local_config(GATEWAY, jwt))
    yield live
    live.close()


#: Ways the model says "I have no tools" when the schemas are taken away. This
#: is L11 in the failure report -- "the model's most honest replies are the ones
#: the loop punishes" -- and it is the second of the two things `tools: []` does
#: instead of answering.
DISCLAIMS = (
    "don't have access",
    "do not have access",
    "no tools",
    "cannot directly",
    "i cannot search",
    "unable to search",
)


def leaked(text: str) -> list[str]:
    return [m for m in MARKUP if m in (text or "")]


def unusable(text: str) -> str:
    """Why this reply is not an answer, or ``""`` if it is one.

    Suppressing the tools fails two different ways and which one you get is not
    stable across sessions: the model either writes the call as text, or refuses
    on the grounds that it has no tools. Asserting either symptom on its own
    makes a flaky test out of a reliable fact, so the tests below assert the
    property that actually rules the lever out -- that what comes back cannot be
    handed to a developer as the answer.
    """
    if marks := leaked(text):
        return f"wrote a tool call as text ({', '.join(marks)})"
    lowered = (text or "").lower()
    if any(phrase in lowered for phrase in DISCLAIMS):
        return "refused, saying it has no tools"
    return ""


# ── the parameters the loop depends on ──────────────────────────────────────


def test_tool_choice_required_forces_a_call(client: LLMClient) -> None:
    """The loop re-asks with this when a mode must call a tool and did not."""
    result = client.chat(
        [{"role": "user", "content": "Say hello. Do not use any tool."}],
        role="fast", max_tokens=200, enable_thinking=False,
        tools=[SEARCH], tool_choice="required",
    )
    assert result.tool_calls, "tool_choice: required did not force a call"


def test_a_named_tool_choice_is_honoured(client: LLMClient) -> None:
    """The loop's only reliable way to end a phase. Everything else was measured
    not to be dependable; see `test_wording_is_not_something_the_loop_can_depend_on`."""
    result = client.chat(
        [{"role": "user", "content": "Search the repository for Routes."}],
        role="fast", max_tokens=300, enable_thinking=False,
        tools=[SEARCH, FINISH],
        tool_choice={"type": "function", "function": {"name": "finish"}},
    )
    assert [c.name for c in result.tool_calls] == ["finish"]
    assert not leaked(result.content)


def test_tool_choice_none_does_not_produce_an_answer(client: LLMClient) -> None:
    """A *characterisation* test: it asserts the bug, not a fix.

    vLLM disables its tool parser for `tool_choice: "none"` while the schemas
    stay in the prompt template, so the model emits its native `<tool_call>`
    markup and nothing extracts it. The loop shipped this as a way to force a
    prose answer and served the markup to a developer as one.

    If this ever starts passing, the endpoint has changed and
    `docs/ENDPOINT-CAPABILITIES.md` is out of date -- worth knowing, so it fails
    loudly rather than being quietly skipped.
    """
    result = client.chat(
        [{"role": "user", "content": "Find where Routes is defined. Use the tool."}],
        role="fast", max_tokens=250, enable_thinking=False,
        tools=[SEARCH], tool_choice="none",
    )
    assert not result.tool_calls, "'none' should suppress structured calls"
    why = unusable(result.content)
    assert why, (
        "tool_choice='none' now returns a usable prose answer. That would make it "
        "a viable way to end a turn; re-measure and update "
        "docs/ENDPOINT-CAPABILITIES.md and loop._terminal_choice."
    )
    print(f"\n    tool_choice='none': {why}")


def test_empty_tools_does_not_produce_an_answer(client: LLMClient) -> None:
    """The other suppression lever, and the worse one.

    Two failure modes, and which one appears is not stable. Sometimes it writes
    a call for a tool that is not ours -- `<function=Grep>` with an
    `output_mode` parameter is Claude Code's, remembered from training.
    Sometimes it refuses outright: *"I don't have access to file system tools or
    code search capabilities in this environment."* That second one is L11 in
    the failure report, and serving either to a developer is worse than the
    problem it was meant to solve.
    """
    result = client.chat(
        [{"role": "user", "content": "Find where Routes is defined. Use the tool."}],
        role="fast", max_tokens=250, enable_thinking=False, tools=[],
    )
    why = unusable(result.content)
    assert why, (
        "tools=[] now returns a usable prose answer; re-check whether it is a "
        "viable fallback and update docs/ENDPOINT-CAPABILITIES.md."
    )
    print(f"\n    tools=[]: {why}")


def test_structured_output_classifies_intent(client: LLMClient) -> None:
    """The intent classifier's whole basis. Three cases, including the bare "go"
    that `_SAYS_GO` used to match with a regex over pinned directives."""
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "intent",
            "schema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["question", "change"]}
                },
                "required": ["kind"],
            },
        },
    }
    cases = [
        ("explain the bootstrapper and how it deviates from the template", "question"),
        ("write a migration.md file with a complete migration plan", "change"),
        ("go", "change"),
    ]
    for task, want in cases:
        result = client.chat(
            [{"role": "user", "content":
              "Answer with the JSON object only. Is this a 'question' (wants to be "
              "told something) or a 'change' (wants code changed)?\n\n" + task}],
            role="fast", max_tokens=64, enable_thinking=False, response_format=schema,
        )
        got = json.loads((result.content or "").strip()).get("kind")
        assert got == want, f"{task!r} classified {got!r}, wanted {want!r}"


def test_parallel_tool_calls_arrive_in_one_reply(client: LLMClient) -> None:
    """The truncation path assumes a reply can carry several calls, and answers
    every one of them. It can."""
    result = client.chat(
        [{"role": "user", "content":
          "Search for 'Routes' and then for 'Handler'. Issue both tool calls now."}],
        role="fast", max_tokens=300, enable_thinking=False, tools=[SEARCH],
    )
    assert len(result.tool_calls) >= 2


# ── the behaviour the loop is shaped around ─────────────────────────────────


def _stuck(depth: int) -> list[dict]:
    """A conversation with ``depth`` fruitless searches behind it."""
    zero = (
        "no matches for {p!r} in {n} files — this pattern is not in the searched "
        "files. If you were checking whether something exists here, that is your "
        "answer: it does not."
    )
    history = [
        (r"func \(.*\) Routes\(\)", None, 18),
        (r"serverRoute\.", None, 49),
        (r"Routes\(\)", "handler/*.go", 12),
        (r"serverRoute", "handler/*.go", 12),
        (r"Routes", "handler/*.go", 12),
        (r"Routes", "handler/employee.go", 0),
    ][:depth]

    messages: list[dict] = [
        {"role": "user", "content": "does any handler here have a Routes() method?"}
    ]
    for i, (pattern, glob, scanned) in enumerate(history):
        args = {"pattern": pattern} | ({"glob": glob} if glob else {})
        cid = f"chatcmpl-tool-{i:02x}"
        messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": cid, "type": "function", "function": {
                "name": "search_repo", "arguments": json.dumps(args)}}],
        })
        messages.append({
            "role": "tool", "tool_call_id": cid,
            "content": zero.format(p=pattern, n=scanned),
        })
    return messages


def _repeats(result) -> bool:
    if not result.tool_calls:
        return False
    call = result.tool_calls[0]
    if call.name != "search_repo":
        return False
    args = json.loads(call.arguments or "{}")
    return args.get("pattern") == "Routes" and args.get("glob") == "handler/employee.go"


def test_the_model_is_fine_until_it_is_not(client: LLMClient) -> None:
    """The cliff that `MAX_RESEARCH_TURNS` is a fence around.

    Five fruitless calls deep the model still widens, re-scopes, tries something
    else. Six deep it usually repeats its last call and does not recover.

    Asserted as a majority rather than unanimity, because that is what it is.
    The first measurement said 5/5 at depth 6 and a later one said 4/5, and a
    test that demands determinism from a stochastic behaviour fails for the
    wrong reason and gets muted. What matters for the design is that the
    behaviour changes sharply between five and six -- which is why the fence is
    a turn count rather than a repeat count, and why the *recovery* leans on
    forcing rather than on anything the model is told.
    """
    shallow = sum(
        _repeats(client.chat(_stuck(5), role="coder", max_tokens=250,
                             enable_thinking=False, tools=[SEARCH]))
        for _ in range(SAMPLES)
    )
    deep = sum(
        _repeats(client.chat(_stuck(6), role="coder", max_tokens=250,
                             enable_thinking=False, tools=[SEARCH]))
        for _ in range(SAMPLES)
    )
    print(f"\n    repeats at depth 5: {shallow}/{SAMPLES}")
    print(f"    repeats at depth 6: {deep}/{SAMPLES}")

    assert shallow <= SAMPLES // 5, f"the trap has moved earlier: {shallow}/{SAMPLES} at depth 5"
    assert deep > SAMPLES // 2, (
        f"only {deep}/{SAMPLES} repeated at depth 6 -- the cliff has moved and "
        "MAX_RESEARCH_TURNS should be re-measured against it"
    )


def test_a_named_tool_choice_rescues_a_stuck_model(client: LLMClient) -> None:
    """The load-bearing claim, and the reason `finish` exists.

    At the depth where every wording fails, naming the terminal tool in
    `tool_choice` ends the turn every time.
    """
    messages = _stuck(6)
    finished = 0
    for _ in range(SAMPLES):
        result = client.chat(
            messages, role="coder", max_tokens=300, enable_thinking=False,
            tools=[SEARCH, FINISH],
            tool_choice={"type": "function", "function": {"name": "finish"}},
        )
        finished += [c.name for c in result.tool_calls] == ["finish"]
    assert finished == SAMPLES, f"only {finished}/{SAMPLES} finished when forced"


def test_wording_is_not_something_the_loop_can_depend_on(client: LLMClient) -> None:
    """Why the loop forces rather than argues, stated as the comparison it is.

    The first measurement of this said "no wording works": the most explicit
    instruction anyone would write -- delivered as the tool's own answer -- got
    5/5 repeats at depth 6. A later session, same text, got 0/5. So better
    wording genuinely helps and it is **not dependable**, which is a different
    and more useful claim than the one first recorded.

    Asserting the negative would make this test flaky for the same reason the
    behaviour is. What it asserts instead is the comparison the design actually
    rests on: forcing is never worse than talking. Both rates are printed, so a
    drift in either shows up in the log rather than in a transcript.
    """
    messages = _stuck(6)
    messages[-1]["content"] = (
        "no matches — the glob 'handler/employee.go' matched no files at all, so "
        "nothing was searched. This says nothing about whether 'Routes' exists.\n\n"
        "You have now searched for 'Routes' three times without a hit. Do not "
        "search for it again: either search for something else, or tell the "
        "developer what you have established."
    )
    talked = sum(
        not _repeats(
            client.chat(messages, role="coder", max_tokens=250,
                        enable_thinking=False, tools=[SEARCH])
        )
        for _ in range(SAMPLES)
    )

    forced = 0
    for _ in range(SAMPLES):
        result = client.chat(
            messages, role="coder", max_tokens=300, enable_thinking=False,
            tools=[SEARCH, FINISH],
            tool_choice={"type": "function", "function": {"name": "finish"}},
        )
        forced += [c.name for c in result.tool_calls] == ["finish"]

    print(f"\n    wording escaped the loop: {talked}/{SAMPLES}")
    print(f"    forcing ended the turn  : {forced}/{SAMPLES}")
    assert forced >= talked, (
        f"talking ({talked}/{SAMPLES}) beat forcing ({forced}/{SAMPLES}); the "
        "forcing in `loop._terminal_choice` is worth re-examining"
    )
    assert forced == SAMPLES, f"forcing is no longer reliable: {forced}/{SAMPLES}"


# ── the fence has to be able to tell a question from a job ──────────────────


def _validating(task: str, reads, fence: str) -> list[dict]:
    """A Planner mid-task, at the turn the research fence fires."""
    from dakcoder_agent.modes import Mode
    from dakcoder_agent.prompts import mode_instruction, system_prompt

    out = [
        {"role": "system", "content": system_prompt() + chr(10) * 2 + mode_instruction(Mode.PLANNER)},
        {"role": "user", "content": task},
    ]
    for i, (name, args, result) in enumerate(reads):
        cid = f"call_{i:03d}"
        out.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]})
        out.append({"role": "tool", "tool_call_id": cid, "content": result})
    out.append({"role": "user", "content":
                "You have spent 12 turns calling tools in this phase without finishing "
                "it. That is enough to act on -- reading more will not make the "
                "decision easier." + chr(10) * 2 + fence})
    return out


NL = chr(10)
TAB = chr(9)
Q = chr(34)

_VALIDATION = (
    "read MIGRATION_PLAN.md and validate it against the codebase. every file",
    [
        ("read_file", {"path": "MIGRATION_PLAN.md"},
         "# Migration plan" + NL * 2 + "1. Swap api-* for n-api-*" + NL
         + "2. Delete routes/routes.go" + NL + "3. Rewrite handlers"),
        ("repo_map", {}, '{"packages":["handler","repo/postgres","core/domain","routes"]}'),
        ("read_file", {"path": "go.mod"},
         "module gotemplate" + NL * 2 + "require github.com/gin-gonic/gin v1.10.1"),
        ("read_file", {"path": "routes/routes.go"},
         "package routes" + NL * 2 + "func Routes(r *gin.Engine) {}"),
        ("read_file", {"path": "handler/objection.go"},
         "package handler" + NL * 2
         + "func (h *ObjectionHandler) Create(ctx *gin.Context) {}"),
        ("search_repo", {"pattern": "gin.Context"},
         "handler/objection.go:14" + NL + "handler/paogen.go:31"),
    ],
)

_CHANGE = (
    "add a status filter to the user list endpoint",
    [
        ("repo_map", {}, '{"packages":["handler","repo/postgres","core/domain"]}'),
        ("read_file", {"path": "handler/user.go"},
         "package handler" + NL * 2 + "func (h *UserHandler) List(sctx *route.Context, "
         "req request.ListUsers) (*resp.R, error) { return nil, nil }"),
        ("read_file", {"path": "handler/request/request.go"},
         "package request" + NL * 2 + "type ListUsers struct {" + NL + TAB
         + "Page int `json:" + Q + "page" + Q + "`" + NL + "}"),
        ("read_file", {"path": "repo/postgres/user.go"},
         "package postgres" + NL * 2 + "func (r *UserRepository) List(ctx context.Context) "
         "([]domain.User, error) { return nil, nil }"),
        ("read_file", {"path": "core/domain/user.go"},
         "package domain" + NL * 2 + "type User struct {" + NL + TAB + "ID int" + NL
         + TAB + "Status string" + NL + "}"),
        ("search_repo", {"pattern": "status"}, "core/domain/user.go:5"),
    ],
)


def _picks(client: LLMClient, case, samples: int) -> dict[str, int]:
    from dakcoder_agent.loop import AgentLoop, _TERMINAL
    from dakcoder_agent.modes import Mode
    from dakcoder_agent.tools.registry import schemas_for

    class _Planner:
        class state:
            mode = Mode.PLANNER
            forced_terminal = 0

    terminals = [s for s in schemas_for(Mode.PLANNER) if s["function"]["name"] in _TERMINAL]
    task, reads = case
    messages = _validating(task, reads, AgentLoop._fence_ask(_Planner()))
    out: dict[str, int] = {}
    for _ in range(samples):
        result = client.chat(messages, role="planner", max_tokens=8192,
                             enable_thinking=False, tools=terminals,
                             tool_choice="required", temperature=0.1)
        name = result.tool_calls[0].name if result.tool_calls else "NO-CALL"
        out[name] = out.get(name, 0) + 1
    return out


def test_the_fence_lets_a_validation_end_in_an_answer(client: LLMClient) -> None:
    """The 2026-09-09 field failure, at the turn where it was decided.

    `_terminal_choice` named `submit_plan`, so a run twelve turns into
    *validating* a migration document had one legal move and wrote an eight-step
    migration nobody asked for. The turn now offers the three terminals with
    `tool_choice: "required"`, and `_fence_ask` names all three.

    Which text is used matters more than the widened choice. Measured here,
    10 samples each: a text listing the three calls got `finish` 0/10 -- the
    Planner overlay says "plan the work" and one closing paragraph does not
    outweigh it. Telling the model to re-read what the developer actually asked
    for got 9/10. That is the text `_fence_ask` ships.
    """
    picks = _picks(client, _VALIDATION, SAMPLES)
    print("    validation at the fence:", picks)
    finished = picks.get("finish", 0)
    assert finished > SAMPLES // 2, (
        f"a validation still does not reach `finish` at the fence: {picks}"
    )


def test_the_fence_still_plans_real_work(client: LLMClient) -> None:
    """The control, and the reason the fence text is measured rather than argued.

    A text that always answers `finish` has not fixed the fence, it has moved
    the failure. Measured 10/10 `submit_plan` on the shipped text.
    """
    picks = _picks(client, _CHANGE, SAMPLES)
    print("    change at the fence:", picks)
    planned = picks.get("submit_plan", 0)
    assert planned > SAMPLES // 2, (
        f"a change task stopped producing a plan; the fence text over-corrected: {picks}"
    )


# ── the intent classifier, on labelled tasks ────────────────────────────────

#: 60 labelled requests. The six tagged as field cases are verbatim from the two
#: runs that made this necessary; the rest are the boundaries those runs exposed.
#:
#: ``(task, conversation, expected)``. ``conversation`` is what ``_classify``
#: builds from ``context.directives``, because a follow-up cannot be classified
#: without it: "go" is a question about nothing and an instruction about
#: whatever was just described.
_FIRST = "(this is the first message)"
_PLAN_TALK = "- I have read the objection handler and it needs six changes; here is what I would do"

INTENT_CASES: tuple[tuple[str, str, str], ...] = (
    # the field failures, verbatim
    ("read this migration file @MIGRATION_PLAN.md and tell me what do you understand from this", _FIRST, "question"),
    ("read MIGRATION_PLAN.md and validate it against the codebase. every file", _FIRST, "question"),
    ("validate the plan against the codebase", _FIRST, "question"),
    ("where is the assessment", _FIRST, "question"),
    ("show me the remaining answer", _FIRST, "question"),
    ("update those minor additions in the migration doc file",
     "- validate MIGRATION_PLAN.md against the codebase", "change"),
    # questions whose subject is work-shaped -- where the old prompt failed
    ("what would migrating this service to n-api-template involve?", _FIRST, "question"),
    ("explain how the migration plan wants us to handle gRPC handlers", _FIRST, "question"),
    ("compare our handler signatures against the n-api-template contract", _FIRST, "question"),
    ("audit repo/postgres for raw squirrel usage", _FIRST, "question"),
    ("review the objection handler for contract violations", _FIRST, "question"),
    ("check whether the objection repository has an N+1 query", _FIRST, "question"),
    ("what would we need to change to drop gin?", _FIRST, "question"),
    ("which files would a migration touch?", _FIRST, "question"),
    ("explain what deleting routes.go would break", _FIRST, "question"),
    ("should we delete routes/routes.go?", _FIRST, "question"),
    ("is it worth migrating paogen.go first?", _FIRST, "question"),
    ("go through every handler and list the contract violations", _FIRST, "question"),
    ("walk me through the migration plan", _FIRST, "question"),
    ("verify the migration plan covers the gRPC handlers", _FIRST, "question"),
    ("summarise what the repo layer does wrong", _FIRST, "question"),
    # plain questions
    ("which repository methods exist for users, and where are they defined?", _FIRST, "question"),
    ("is the user handler registered in the bootstrapper?", _FIRST, "question"),
    ("list every file that still imports gin", _FIRST, "question"),
    ("does this service still use volatiletech/null anywhere?", _FIRST, "question"),
    ("what fields does the User domain model have?", _FIRST, "question"),
    ("how many handlers still take gin.Context?", _FIRST, "question"),
    # plain changes
    ("add a status filter to the user list endpoint", _FIRST, "change"),
    ("migrate this service to the n-api-template", _FIRST, "change"),
    ("fix the N+1 query in GetClosingBalanceRepo", _FIRST, "change"),
    ("add a LastName string field to the User domain model", _FIRST, "change"),
    ("delete routes/routes.go and move the routes onto the handlers", _FIRST, "change"),
    ("replace squirrel with dblib.Psql in repo/postgres/objection.go", _FIRST, "change"),
    ("scaffold a remittance resource", _FIRST, "change"),
    ("make the handlers take sctx *route.Context", _FIRST, "change"),
    ("I want the handlers to take sctx *route.Context", _FIRST, "change"),
    ("can you drop gin from go.mod?", _FIRST, "change"),
    # writing a document is a change, however analytical it sounds
    ("rewrite MIGRATION_PLAN.md to include the gaps you found", _FIRST, "change"),
    ("add the missing sections to the migration doc", _FIRST, "change"),
    ("document the migration steps in MIGRATION_PLAN.md", _FIRST, "change"),
    ("write up your findings in AUDIT.md", _FIRST, "change"),
    # approvals and combinations
    ("go", _PLAN_TALK, "change"),
    ("yes please", _PLAN_TALK, "change"),
    ("do it", _PLAN_TALK, "change"),
    ("explain the handler, then migrate it", _FIRST, "change"),
    ("review the repo and fix what you find", _FIRST, "change"),
    ("tell me what is wrong with the repo layer and fix it", _FIRST, "change"),
    ("run the linter and fix what it reports", _FIRST, "change"),
    # verbs no candidate prompt named: the generalisation check
    ("diagnose why the cashbook reversion is slow", _FIRST, "question"),
    ("investigate whether paogen.go has dead code", _FIRST, "question"),
    ("trace how a transfer entry flows through the repo layer", _FIRST, "question"),
    ("critique the migration plan", _FIRST, "question"),
    ("sanity-check the fx wiring", _FIRST, "question"),
    ("evaluate whether dblib.Psql covers our query shapes", _FIRST, "question"),
    ("map out the dependencies between handler and repo", _FIRST, "question"),
    ("figure out why swagger_check is failing", _FIRST, "question"),
    ("wire up the objection handler in the bootstrapper", _FIRST, "change"),
    ("strip volatiletech/null out of core/domain", _FIRST, "change"),
    ("port the objection repository to dblib.Psql", _FIRST, "change"),
    ("harden the request validation on CreateUserRequest", _FIRST, "change"),
)


def test_the_intent_classifier_reads_the_verb_not_the_subject(client: LLMClient) -> None:
    """The decision the whole run turns on, on 60 labelled requests.

    A wrong "question" costs one word -- the answer is on screen and the next
    message starts the work. A wrong "change" put a run into the Planner for a
    request to *validate* a document, and twenty turns later the developer had
    an unrequested eight-step migration and no answer. That asymmetry is why
    `_classify` falls back to ASK, and it is why this is measured rather than
    reasoned about.

    The prompt this replaced scored 44/60. It enumerated the *subjects* of a
    change -- "a feature, a fix, a migration, a refactor" -- so "validate the
    migration plan" matched on the noun and the verb was never consulted. Three
    rewrites all scored 60/60 at three samples each, including on eight verbs
    none of them named (diagnose, critique, sanity-check, map out), so the
    enumerating ones were not winning by their enumeration. The shipped one
    states the test instead of a vocabulary.

    One sample per case here, to keep the suite affordable. Every miss is
    printed with the classifier's own `why`, so a regression names itself.
    """
    from dakcoder_agent.loop import _INTENT_PROMPT, _INTENT_SCHEMA, _parse_json_object

    wrong = []
    for task, conversation, expected in INTENT_CASES:
        reply = client.chat(
            [{"role": "user", "content": _INTENT_PROMPT.format(
                conversation=conversation, task=task)}],
            role="fast", max_tokens=64, enable_thinking=False,
            response_format=_INTENT_SCHEMA,
        )
        parsed = _parse_json_object(reply.content or "") or {}
        got = str(parsed.get("kind", "")).strip().lower()
        if got != expected:
            why = str(parsed.get("why", ""))[:60]
            wrong.append(f"{task[:52]!r} want {expected} got {got or '?'} ({why})")

    for row in wrong:
        print("    MISS", row)
    print(f"    intent: {len(INTENT_CASES) - len(wrong)}/{len(INTENT_CASES)}")
    assert len(wrong) <= 2, f"{len(wrong)} of {len(INTENT_CASES)} misclassified"
