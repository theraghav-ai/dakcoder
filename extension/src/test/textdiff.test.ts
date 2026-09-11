/**
 * Tests for the line differ behind the approval card's preview.
 *
 * The property that matters is not "it produces a diff" but "the diff it
 * produces is the change": the card is the surface a developer accepts a write
 * on, and a preview that understates a change is worse than no preview at all.
 * So the tests reconstruct the right-hand side from the diff and compare.
 */

import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';

import { capped, unified } from '../textdiff';

/** Replay a unified diff against the left-hand side. */
function apply(before: string, diff: string): string {
  const source = before === '' ? [] : before.replace(/\r\n/g, '\n').replace(/\n$/, '').split('\n');
  const out: string[] = [];
  let cursor = 0;

  for (const line of diff.split('\n')) {
    if (line.startsWith('--- ') || line.startsWith('+++ ')) continue;
    const hunk = /^@@ -(\d+),(\d+) \+(\d+),(\d+) @@$/.exec(line);
    if (hunk) {
      const start = Number(hunk[1]) - 1;
      while (cursor < start) out.push(source[cursor++]!);
      continue;
    }
    const op = line.charAt(0);
    const text = line.slice(1);
    if (op === ' ') {
      out.push(text);
      cursor += 1;
    } else if (op === '-') {
      cursor += 1;
    } else if (op === '+') {
      out.push(text);
    }
  }
  while (cursor < source.length) out.push(source[cursor++]!);
  return out.join('\n');
}

const rows = (n: number, tag = 'l'): string =>
  Array.from({ length: n }, (_, i) => `${tag}${i}`).join('\n');

describe('the line differ', () => {
  it('says nothing about two identical files', () => {
    const same = 'package main\n\nfunc main() {}';
    const diff = unified(same, same, 'main.go');
    assert.equal(diff.text, '');
    assert.deepEqual(diff.stat, { added: 0, removed: 0 });
  });

  it('reconstructs the new file exactly from its own diff', () => {
    const before = 'package main\n\nfunc Handle() error {\n\treturn nil\n}';
    const after = 'package main\n\nimport "fmt"\n\nfunc Handle() error {\n\tfmt.Println("x")\n\treturn nil\n}';
    const diff = unified(before, after, 'handler.go');
    assert.equal(apply(before, diff.text), after.replace(/\n$/, ''));
  });

  it('counts what it changed, and only what it changed', () => {
    const diff = unified('a\nb\nc', 'a\nB\nc', 'x.go');
    assert.deepEqual(diff.stat, { added: 1, removed: 1 });
  });

  it('treats a new file as all additions and a delete as all removals', () => {
    assert.deepEqual(unified('', 'a\nb', 'new.go').stat, { added: 2, removed: 0 });
    assert.deepEqual(unified('a\nb', '', 'gone.go').stat, { added: 0, removed: 2 });
  });

  it('keeps three lines of context and elides the rest into hunks', () => {
    // The point of hunks: a one-line change in a 200-line file is a diff you can
    // read, not 200 lines with one of them marked.
    const before = rows(200);
    const after = before.replace('l100', 'CHANGED');
    const diff = unified(before, after, 'big.go');
    const body = diff.text.split('\n').filter((l) => !l.startsWith('---') && !l.startsWith('+++'));
    assert.ok(body.length <= 10, `one changed line produced ${body.length} lines of diff`);
    assert.ok(body.some((l) => l.startsWith('@@')), 'no hunk header');
    assert.equal(apply(before, diff.text), after);
  });

  it('emits one hunk per region rather than one spanning the file', () => {
    const before = rows(120);
    const after = before.replace('l5', 'A').replace('l110', 'B');
    const diff = unified(before, after, 'two.go');
    const hunks = diff.text.split('\n').filter((l) => l.startsWith('@@')).length;
    assert.equal(hunks, 2, 'two distant changes collapsed into one hunk over the whole file');
    assert.equal(apply(before, diff.text), after);
  });

  it('reports a file too large to diff rather than diffing it slowly', () => {
    // The quadratic table is only honest because of this bound. Past it the
    // answer is counts plus `coarse`, which the card turns into a sentence.
    const diff = unified(rows(4000), rows(4000, 'x'), 'huge.go');
    assert.equal(diff.coarse, true);
    assert.equal(diff.text, '');
    assert.deepEqual(diff.stat, { added: 4000, removed: 4000 });
  });

  it('normalises CRLF so a Windows checkout is not one enormous change', () => {
    // Every line differs byte for byte between the two, and none of them
    // differs in any way a reviewer cares about.
    const diff = unified('a\r\nb\r\nc', 'a\nb\nc', 'crlf.go');
    assert.equal(diff.text, '');
  });

  it('does not invent a trailing change for a file that ends in a newline', () => {
    assert.equal(unified('a\nb\n', 'a\nb', 'nl.go').text, '');
  });

  it('caps a long diff and says how much it cut', () => {
    const before = rows(400);
    const after = before.split('\n').map((l) => l + '!').join('\n');
    const cut = capped(unified(before, after, 'all.go'), 50);
    assert.equal(cut.text.split('\n').length, 50);
    assert.ok(cut.cut > 0, 'a capped diff must say it was capped');
  });

  it('leaves a short diff whole, and says it cut nothing', () => {
    const cut = capped(unified('a', 'b', 'x.go'), 50);
    assert.equal(cut.cut, 0);
  });
});
