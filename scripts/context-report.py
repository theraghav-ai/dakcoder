#!/usr/bin/env python3
"""Is the context window big enough for this codebase?

Reads what runs left behind and answers with numbers instead of impressions.

    scripts/context-report.py                       # this workspace
    scripts/context-report.py --workspace /srv/repo # another one
    scripts/context-report.py --json                # the same, machine-readable
    scripts/context-report.py --session <id>        # one run, in detail

**What it reads.** `.dakcoder/sessions/<id>/events.jsonl`, written by
`journal.py`. Nothing here talks to the model or the gateway, so it is safe to
run against a live server, and it works on sessions recorded before the
`metrics` event existed — `dakcoder_agent.metrics.from_events` rebuilds the
record from the transcript, and says which fields it could not recover rather
than reporting them as zero.

**What the numbers mean**, because a claim rests on the distinction:

*Pressure* is the window shaping the run: a compaction fired, or a reply was cut
off. It is real but it is not, on its own, proof of anything — a threshold can
be moved and a budget can be retuned.

*Loss* is the window costing the run something it needed: a file was evicted and
then read again, or a read was refused because the content was already held.
That is the evidence. A window large enough for the task produces none of it,
whatever the thresholds are set to.

The last section states what the task actually required — the unique source the
run had to read — against what the window holds. When the first number is larger
than the second, no loop design closes the gap.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apps" / "agent" / "src"), str(ROOT / "apps" / "shared" / "src")]

from dakcoder_agent.metrics import RunMetrics, from_events  # noqa: E402

#: Characters per token for turning bytes of source into tokens. The estimator's
#: own figure for code (`tokens.py`), used here for the same reason it is used
#: there: it is the conservative one, so "this did not fit" is understated
#: rather than overstated.
CHARS_PER_TOKEN = 3.2


def sessions(workspace: Path) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Every journalled session, oldest first."""
    root = workspace / ".dakcoder" / "sessions"
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        events = entry / "events.jsonl"
        if not entry.is_dir() or not events.is_file():
            continue
        rows: list[dict[str, Any]] = []
        with events.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue  # a truncated last line is what a hard kill leaves
                if isinstance(parsed, dict):
                    rows.append(parsed)
        if rows:
            yield entry.name, rows


def compact(n: float) -> str:
    return f"{n/1_000_000:.1f}M" if n >= 1_000_000 else (f"{n/1_000:.1f}k" if n >= 1_000 else f"{n:,.0f}")


def report(records: list[RunMetrics], *, out=sys.stdout) -> None:
    if not records:
        print("No journalled sessions found. Runs record to .dakcoder/sessions/", file=out)
        return

    window = max((r.context_window for r in records), default=0)
    print(f"\n{len(records)} run(s)" + (f", model window {window:,} tokens" if window else ""), file=out)

    print("\n── per run ─────────────────────────────────────────────────────────", file=out)
    head = f"{'session':<14}{'outcome':<12}{'turns':>6}{'peak prompt':>13}{'% win':>7}"
    print(head + f"{'compact':>9}{'trunc':>7}{'re-read':>9}{'read':>9}", file=out)
    for r in records:
        peak = f"{r.peak_prompt_tokens:,}" if r.peak_prompt_tokens else "-"
        pct = f"{r.peak_pct_of_window:.0f}%" if r.context_window else "-"
        flag = "!" if r.lost_work else (" " if not r.pressed_the_ceiling else "·")
        print(
            f"{r.session_id[:13]:<14}{(r.outcome or '?')[:11]:<12}{r.turns:>6}"
            f"{peak:>13}{pct:>7}{len(r.compactions):>9}{r.truncations:>7}"
            f"{len(r.evicted_paths_reread) + r.intercepted_re_read:>9}"
            f"{compact(r.bytes_read):>8}{flag}",
            file=out,
        )

    pressed = [r for r in records if r.pressed_the_ceiling]
    lost = [r for r in records if r.lost_work]

    print("\n── pressure: the window shaped the run ─────────────────────────────", file=out)
    print(f"  runs that compacted or were truncated   {len(pressed):>4} of {len(records)}", file=out)
    print(f"  compactions, total                      {sum(len(r.compactions) for r in records):>4}", file=out)
    print(f"  tokens discarded by compaction          {compact(sum(c['freed'] for r in records for c in r.compactions)):>7}", file=out)
    print(f"  replies cut off by the output limit     {sum(r.truncations for r in records):>4}", file=out)

    print("\n── loss: the window cost the run something it needed ───────────────", file=out)
    print(f"  runs that lost work                     {len(lost):>4} of {len(records)}", file=out)
    print(f"  files evicted and then read again       {sum(len(r.evicted_paths_reread) for r in records):>4}", file=out)
    print(f"  reads refused as already-in-context     {sum(r.intercepted_re_read for r in records):>4}", file=out)
    print(f"  bytes re-read after eviction            {compact(sum(r.bytes_reread for r in records)):>7}", file=out)
    if lost:
        worst = max(lost, key=lambda r: len(r.evicted_paths_reread))
        for path in worst.evicted_paths_reread[:5]:
            print(f"    {worst.session_id[:8]}  evicted, then needed again: {path}", file=out)

    print("\n── what the task needed, against what the window holds ─────────────", file=out)
    for r in sorted(records, key=lambda r: r.bytes_read, reverse=True)[:5]:
        need = int(r.bytes_read / CHARS_PER_TOKEN)
        verdict = "fits" if not r.context_window or need < r.budget else "DOES NOT FIT"
        print(
            f"  {r.session_id[:8]}  read {compact(r.bytes_read):>7}B of source "
            f"= ~{compact(need):>6} tokens of files alone, against a "
            f"{compact(r.budget)} prompt budget: {verdict}",
            file=out,
        )
    print(
        "\n  'Files alone' excludes the system prompt, the tool schemas, the plan,\n"
        "  every assistant message and every non-read tool result — so it is a\n"
        "  floor on what the task required, not an estimate of it.\n",
        file=out,
    )

    incomplete = {note for r in records for note in r.incomplete}
    if incomplete:
        print("── recorded gaps ───────────────────────────────────────────────────", file=out)
        for note in sorted(incomplete):
            print(f"  {note}", file=out)
        print(file=out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", type=Path, default=Path.cwd(), help="the repository runs were journalled in")
    ap.add_argument("--session", help="one session id, in full detail")
    ap.add_argument("--json", action="store_true", help="machine-readable, for a spreadsheet or a ticket")
    args = ap.parse_args()

    records = [
        from_events(rows, session_id=sid)
        for sid, rows in sessions(args.workspace)
        if not args.session or sid.startswith(args.session)
    ]

    if args.json:
        json.dump([r.as_dict() for r in records], sys.stdout, indent=1, default=str)
        print()
        return 0

    if args.session:
        for r in records:
            json.dump(r.as_dict(), sys.stdout, indent=1, default=str)
            print()
        return 0 if records else 1

    report(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
