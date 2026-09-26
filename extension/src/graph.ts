/**
 * `/graph`: the code-graph pilot as a slash command.
 *
 * The command does not call graphify itself. It turns what the developer typed
 * into a request that names the `code_graph` operation to use, and the agent
 * runs it -- so the answer comes back in the conversation, grounded in the
 * graph, and the model can follow up with a read when an edge is not enough.
 *
 * The four shapes, most specific first:
 *
 *   /graph                     an outline of the service's structure
 *   /graph callers X           what calls or uses X
 *   /graph path A B            how A reaches B (also `A -> B`, `A to B`)
 *   /graph X                   X's edges, when X is one identifier
 *   /graph <anything else>     a question answered from the graph
 *
 * Not localised: this is the instruction to the model, not UI. A translated
 * prompt changes what the agent is asked to do.
 */

const IDENT = /^[A-Za-z_][\w.]*(\(\))?$/;

/** `callers X`, `who calls X`, `usages of X`. */
const CALLERS = /^(?:callers|who\s+calls|who\s+uses|usages?\s+of|uses\s+of)\s+(?:of\s+)?([^\s?]+)\s*\??$/i;

/** `path A B`, `path A to B`, `A -> B`, `A to B`. */
const PATH_WORD = /^path\s+(?:from\s+)?(\S+)\s+(?:to\s+)?(\S+)$/i;
const PATH_ARROW = /^(\S+)\s*(?:->|→|\bto\b)\s*(\S+)$/i;

/** What the agent is told when the empty-callers answer comes back. */
const CONFIRM =
  'An empty answer is not proof: the graph misses calls through struct fields ' +
  'and interfaces, so confirm it with search_repo before saying nothing uses it.';

export function graphTask(argument: string): string {
  const arg = argument.trim();

  if (!arg) {
    return [
      "Outline this service's structure from the code graph.",
      '',
      'Use code_graph op=query for the entry points and the main handler, service',
      'and repository types, then op=explain on the two or three most connected',
      'ones. Keep it to what the graph shows; read a file only to settle a doubt.',
    ].join('\n');
  }

  const callers = CALLERS.exec(arg);
  if (callers && IDENT.test(callers[1]!)) {
    return [
      `What calls or uses ${callers[1]}?`,
      '',
      `Use code_graph op=callers symbol=${callers[1]}, and give each caller with its`,
      `file and line. ${CONFIRM}`,
    ].join('\n');
  }

  const path = PATH_WORD.exec(arg) ?? PATH_ARROW.exec(arg);
  if (path && IDENT.test(path[1]!) && IDENT.test(path[2]!)) {
    return [
      `How does ${path[1]} reach ${path[2]}?`,
      '',
      `Use code_graph op=path symbol=${path[1]} to=${path[2]}, and explain each hop`,
      'with its file and line. If there is no path, say so and check with',
      'code_graph op=explain on each end before concluding they are unrelated.',
    ].join('\n');
  }

  if (IDENT.test(arg)) {
    return [
      `Explain ${arg} from the code graph.`,
      '',
      `Use code_graph op=explain symbol=${arg}: where it is defined, what it calls`,
      'and references, and what uses it. Read its source only if the edges leave',
      'its purpose unclear.',
    ].join('\n');
  }

  return [
    arg,
    '',
    'Answer from the code graph: start with code_graph op=query, then op=explain',
    'or op=path on the symbols it turns up. Read files only to confirm.',
  ].join('\n');
}
