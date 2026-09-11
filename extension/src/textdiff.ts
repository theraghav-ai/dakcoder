/**
 * A line diff, in unified format, with no dependency.
 *
 * **Why this exists.** The approval card is the one moment in a run where the
 * developer is being asked to decide something, and until now it asked without
 * showing them the change: the card listed a tool, a reason and a path, and the
 * diff was behind a button that opened an editor tab. That is the same "it is in
 * here somewhere, go and find it" that the rest of this revision removes. The
 * card can only show a diff if something computes one, and the packaged
 * extension bundles no diff library.
 *
 * **Why LCS and not Myers.** The inputs are one file's before and after, capped
 * below at a few thousand lines, and the output is read by a person in a 340px
 * column. An O(n·m) table over that is microseconds and fits in twenty lines
 * that can be read and trusted; Myers' linear-space refinement buys nothing a
 * reviewer would notice and costs a page of index arithmetic. The cap is what
 * keeps the quadratic honest, and it is checked before the table is allocated.
 */

/** Past this, no table is built: the pair is reported as a wholesale replace. */
const MAX_LINES = 3000;

/** Unchanged lines kept either side of a change, as every diff tool shows. */
const CONTEXT = 3;

export interface DiffStat {
  added: number;
  removed: number;
}

export interface UnifiedDiff {
  /** The diff itself, ready to render. Empty when the two sides are identical. */
  text: string;
  stat: DiffStat;
  /** True when the file was too large to diff line by line and was summarised. */
  coarse: boolean;
}

type Op = ' ' | '+' | '-';

/**
 * The longest common subsequence of two line arrays, as a list of operations.
 *
 * Returns `null` rather than allocating when the pair is too large — the caller
 * turns that into a coarse summary, which is an honest answer, where a
 * half-computed diff would not be.
 */
function operations(before: string[], after: string[]): Array<[Op, string]> | null {
  if (before.length > MAX_LINES || after.length > MAX_LINES) return null;

  // A single row of the table at a time is all the recurrence needs, but the
  // walk back needs the whole thing, so the table is kept. (n+1)·(m+1) numbers
  // at 3000 each is 9M entries worst case — which is why MAX_LINES is checked
  // above rather than trusted to be small.
  const n = before.length;
  const m = after.length;
  const table: Uint32Array[] = [];
  for (let i = 0; i <= n; i += 1) table.push(new Uint32Array(m + 1));

  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i]![j] =
        before[i] === after[j]
          ? table[i + 1]![j + 1]! + 1
          : Math.max(table[i + 1]![j]!, table[i]![j + 1]!);
    }
  }

  const ops: Array<[Op, string]> = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (before[i] === after[j]) {
      ops.push([' ', before[i]!]);
      i += 1;
      j += 1;
    } else if (table[i + 1]![j]! >= table[i]![j + 1]!) {
      ops.push(['-', before[i]!]);
      i += 1;
    } else {
      ops.push(['+', after[j]!]);
      j += 1;
    }
  }
  while (i < n) {
    ops.push(['-', before[i]!]);
    i += 1;
  }
  while (j < m) {
    ops.push(['+', after[j]!]);
    j += 1;
  }
  return ops;
}

/** Split that does not invent a trailing empty line for a file ending in \n. */
function lines(text: string): string[] {
  const normalised = text.replace(/\r\n/g, '\n');
  if (normalised === '') return [];
  const out = normalised.split('\n');
  if (out[out.length - 1] === '') out.pop();
  return out;
}

/**
 * `before` and `after` as a unified diff with `@@` hunk headers.
 *
 * The header line numbers are real, so the output is a diff an editor or `patch`
 * would accept — not a decorative one. That matters because the panel offers the
 * text for copying, and a diff that cannot be applied is a diff that lies about
 * what it is.
 */
export function unified(before: string, after: string, path = ''): UnifiedDiff {
  if (before === after) return { text: '', stat: { added: 0, removed: 0 }, coarse: false };

  const left = lines(before);
  const right = lines(after);
  const ops = operations(left, right);

  if (!ops) {
    // Too large to diff. Say what changed in counts rather than pretending.
    return {
      text: '',
      stat: { added: right.length, removed: left.length },
      coarse: true,
    };
  }

  const stat: DiffStat = { added: 0, removed: 0 };
  for (const [op] of ops) {
    if (op === '+') stat.added += 1;
    if (op === '-') stat.removed += 1;
  }

  // Which operations are inside CONTEXT lines of a change. Everything else is
  // dropped, and the gap becomes a new hunk.
  const keep = new Array<boolean>(ops.length).fill(false);
  for (let i = 0; i < ops.length; i += 1) {
    if (ops[i]![0] === ' ') continue;
    for (let k = Math.max(0, i - CONTEXT); k <= Math.min(ops.length - 1, i + CONTEXT); k += 1) {
      keep[k] = true;
    }
  }

  /*
   * No change survived normalisation, so there is nothing to show.
   *
   * Reached whenever the two sides differ as strings but not as lines: a CRLF
   * checkout against an LF write, or a file that gained or lost its final
   * newline. Emitting the `---`/`+++` header here would put a two-line diff with
   * no hunks in the approval card, which reads as "something changed and the
   * panel will not tell you what".
   */
  if (stat.added === 0 && stat.removed === 0) {
    return { text: '', stat, coarse: false };
  }

  const out: string[] = [];
  if (path) {
    out.push(`--- a/${path}`);
    out.push(`+++ b/${path}`);
  }

  let oldLine = 1;
  let newLine = 1;
  let i = 0;
  while (i < ops.length) {
    if (!keep[i]) {
      if (ops[i]![0] !== '+') oldLine += 1;
      if (ops[i]![0] !== '-') newLine += 1;
      i += 1;
      continue;
    }

    // One hunk: everything kept, until the first dropped operation.
    const hunk: string[] = [];
    const oldStart = oldLine;
    const newStart = newLine;
    let oldSpan = 0;
    let newSpan = 0;
    while (i < ops.length && keep[i]) {
      const [op, text] = ops[i]!;
      hunk.push(op + text);
      if (op !== '+') {
        oldLine += 1;
        oldSpan += 1;
      }
      if (op !== '-') {
        newLine += 1;
        newSpan += 1;
      }
      i += 1;
    }
    out.push(`@@ -${oldStart},${oldSpan} +${newStart},${newSpan} @@`);
    for (const line of hunk) out.push(line);
  }

  return { text: out.join('\n'), stat, coarse: false };
}

/**
 * The same diff, cut to a line budget.
 *
 * The card is a card: a 900-line diff in it is a scroll through the panel rather
 * than a decision aid, and the full thing is one click away in a real diff
 * editor. The cut is reported so the card can say so instead of implying the
 * change was small.
 */
export function capped(diff: UnifiedDiff, budget: number): { text: string; cut: number } {
  const all = diff.text ? diff.text.split('\n') : [];
  if (all.length <= budget) return { text: diff.text, cut: 0 };
  return { text: all.slice(0, budget).join('\n'), cut: all.length - budget };
}
