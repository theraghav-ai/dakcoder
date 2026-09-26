/**
 * Tests for `/graph`'s request builder.
 *
 * The property that matters is that each shape the developer types lands on
 * the `code_graph` operation that answers it, and that the callers request
 * never lets an empty graph answer stand as "nothing uses this".
 */

import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';

import { graphTask } from '../graph';

describe('graphTask', () => {
  it('outlines the service when given nothing', () => {
    const task = graphTask('  ');
    assert.match(task, /op=query/);
    assert.match(task, /op=explain/);
  });

  it('explains a single identifier', () => {
    for (const name of ['CreateObjectionHandler', 'ObjectionHandler.Routes', 'NewRepo()']) {
      assert.match(graphTask(name), new RegExp(`op=explain symbol=${name.replace(/[.()]/g, '\\$&')}`));
    }
  });

  it('asks for callers in every phrasing, with the struct-field caveat', () => {
    for (const phrase of ['callers GetAll', 'who calls GetAll?', 'usages of GetAll', 'who uses GetAll']) {
      const task = graphTask(phrase);
      assert.match(task, /op=callers symbol=GetAll\b/, phrase);
      assert.match(task, /search_repo/, phrase);
    }
  });

  it('traces a path in every phrasing', () => {
    for (const phrase of ['path A B', 'path from A to B', 'A -> B', 'A to B', 'A → B']) {
      assert.match(graphTask(phrase), /op=path symbol=A to=B/, phrase);
    }
  });

  it('sends anything else as a question answered from the graph', () => {
    const task = graphTask('how is an objection saved to the database');
    assert.ok(task.startsWith('how is an objection saved to the database'));
    assert.match(task, /op=query/);
  });

  it('does not mistake a sentence containing "to" for a path', () => {
    assert.doesNotMatch(graphTask('what writes to the ledger table'), /op=path symbol=/);
  });
});
