"""A `finish` answer that arrived with no line breaks, and what is put back.

Four long answers in the field sessions -- 5,800 to 13,900 characters of
markdown each -- came back from the endpoint with not one newline, so their
headings, tables and lists rendered as a single paragraph. The fixture below
is a cut-down copy of session f27b69128073's, in its original shape.
"""

from __future__ import annotations

from dakcoder_agent.tools.control import FLAT_ANSWER_CHARS, _restore_breaks

FLAT = (
    "I read the paogen handler and repository. Here is the mapping.## Handler-to-Repository "
    "Mapping### 1. Lookup Handlers| Handler | Repo Function | Notes ||---------|---------------|"
    "-------|| `FetchOfficenameHandler` | `GetOfficenameRepo` | Select by office id || "
    "`ListPAOHandler` | `GetPAOsRepo` | Distinct PAO codes |### 10. `dblib.CopyFrom` usage "
    "is correct for bulk inserts.## Summary**Critical (contract violations):**1. All repo "
    "methods take `*gin.Context` instead of `context.Context`2. Several methods use "
    "`context.Background()` with a hardcoded timeout3. `GetClosingBalanceRepo` has an N+1 "
    "query**Moderate (quality issues):**4. Mixed use of squirrel and raw SQL in version 2. "
    "of the file"
)


def test_the_fixture_is_long_enough_to_be_repaired() -> None:
    assert len(FLAT) >= FLAT_ANSWER_CHARS and "\n" not in FLAT


def test_headings_start_their_own_lines() -> None:
    fixed = _restore_breaks(FLAT)
    assert "\n\n## Handler-to-Repository Mapping" in fixed
    assert "\n\n### 1. Lookup Handlers" in fixed
    assert "\n\n## Summary" in fixed


def test_a_numbered_heading_is_not_split_as_a_list_item() -> None:
    assert "\n\n### 10. `dblib.CopyFrom` usage" in _restore_breaks(FLAT)


def test_table_rows_are_one_per_line() -> None:
    fixed = _restore_breaks(FLAT)
    assert "| Handler | Repo Function | Notes |\n|---------|" in fixed
    assert "|\n| `FetchOfficenameHandler` |" in fixed
    assert "|\n| `ListPAOHandler` |" in fixed


def test_list_items_follow_the_count_and_prose_numbers_do_not() -> None:
    fixed = _restore_breaks(FLAT)
    assert "\n1. All repo methods" in fixed
    assert "\n2. Several methods" in fixed
    assert "\n3. `GetClosingBalanceRepo`" in fixed
    assert "\n4. Mixed use" in fixed
    assert "version 2. of the file" in fixed, "a '2.' that breaks the count is prose"


def test_bold_labels_open_a_block() -> None:
    fixed = _restore_breaks(FLAT)
    assert "\n\n**Critical (contract violations):**\n" in fixed
    assert "\n\n**Moderate (quality issues):**\n" in fixed


def test_an_answer_with_any_line_break_is_left_exactly_as_sent() -> None:
    sent = FLAT.replace("## Summary", "\n## Summary", 1)
    assert _restore_breaks(sent) == sent


def test_a_short_flat_answer_is_left_alone() -> None:
    short = "Added the Routes method. See step 2. for the wiring."
    assert _restore_breaks(short) == short
