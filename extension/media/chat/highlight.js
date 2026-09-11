/*
 * A small, dependency-free syntax highlighter for the panel.
 *
 * **Why hand-rolled.** The webview runs under `default-src 'none'`; there is no
 * CDN to pull highlight.js or shiki from, and vendoring either costs hundreds of
 * kilobytes to colour what is, in this panel, almost entirely Go. A tokenizer
 * that covers Go properly and everything else adequately is a better trade than
 * a general one nobody can audit.
 *
 * **It returns tokens, never markup.** `tokenize()` hands back `[class, text]`
 * pairs and the caller builds nodes with `textContent`. There is no innerHTML
 * sink anywhere in this file, which is the same rule the renderer follows: tool
 * output and model output are attacker-adjacent text.
 *
 * **Unknown languages degrade to one plain token.** A language this file has
 * never heard of is not an error; it is uncoloured code, which is exactly what
 * the panel showed before this file existed.
 *
 * Token classes, kept to eight so the theme stays legible at 10.5px:
 *   c comment   s string   n number   k keyword
 *   t type      f function e entity/attribute   o operator
 */

(function (global) {
  'use strict';

  /** Rules are ordered; the first that matches at a position wins. */
  function compile(rules) {
    // Every rule source is non-capturing by contract, so group N is rule N-1.
    const source = rules
      .map(function (rule) {
        return '(' + rule[1] + ')';
      })
      .join('|');
    return {
      re: new RegExp(source, 'gm'),
      classes: rules.map(function (r) {
        return r[0];
      }),
    };
  }

  const NUM = '\\b(?:0[xXbBoO][0-9a-fA-F_]+|\\d[\\d_]*(?:\\.[\\d_]+)?(?:[eE][+-]?\\d+)?)\\w*';
  const LINE_HASH = '#[^\\n]*';
  const LINE_SLASH = '\\/\\/[^\\n]*';
  const BLOCK_C = '\\/\\*[\\s\\S]*?(?:\\*\\/|$)';
  const DQ = '"(?:\\\\[\\s\\S]|[^"\\\\])*"?';
  const SQ = "'(?:\\\\[\\s\\S]|[^'\\\\])*'?";
  const TICK = '`(?:\\\\[\\s\\S]|[^`\\\\])*`?';
  const OPS = '[-+*/%&|^!<>=~?:]+|[{}()\\[\\].,;]';

  function words(list) {
    return '\\b(?:' + list.join('|') + ')\\b';
  }

  // -- Go ---------------------------------------------------------------------
  // The language this panel actually shows. Types and builtins are separated
  // from keywords because in Go the type name is usually the thing being looked
  // for - a scan down a struct reads its field types, not its `struct` tokens.

  const GO_KW = ['break', 'case', 'chan', 'const', 'continue', 'default', 'defer', 'else',
    'fallthrough', 'for', 'func', 'go', 'goto', 'if', 'import', 'interface', 'map', 'package',
    'range', 'return', 'select', 'struct', 'switch', 'type', 'var'];
  const GO_LIT = ['true', 'false', 'nil', 'iota'];
  const GO_BUILTIN = ['append', 'cap', 'clear', 'close', 'complex', 'copy', 'delete', 'imag',
    'len', 'make', 'max', 'min', 'new', 'panic', 'print', 'println', 'real', 'recover'];
  const GO_TYPE = ['any', 'bool', 'byte', 'comparable', 'complex64', 'complex128', 'error',
    'float32', 'float64', 'int', 'int8', 'int16', 'int32', 'int64', 'rune', 'string', 'uint',
    'uint8', 'uint16', 'uint32', 'uint64', 'uintptr'];

  const GRAMMARS = {
    go: compile([
      ['c', LINE_SLASH], ['c', BLOCK_C],
      ['s', TICK], ['s', DQ], ['s', SQ],
      ['n', NUM],
      ['k', words(GO_KW)],
      ['n', words(GO_LIT)],
      ['f', words(GO_BUILTIN)],
      ['t', words(GO_TYPE)],
      // A leading capital is Go's own export marker, so it is also the most
      // reliable signal that an identifier names a type rather than a local.
      ['f', '\\b[A-Za-z_]\\w*(?=\\()'],
      ['t', '\\b[A-Z]\\w*\\b'],
      ['o', OPS],
    ]),

    /*
     * The output of `go build`, `go vet` and `go test` — which is not Go.
     *
     * Colouring a compiler log with the Go grammar was actively worse than
     * leaving it plain: every `/` in a package path, every `:` in a position and
     * the word `go` in every filename picked up a token colour, so the line that
     * mattered was the same confetti as the four around it. What a reader wants
     * from a build log is where (the position), and whether (FAIL or ok). That
     * is all this grammar marks.
     */
    golog: compile([
      ['c', '^#[^\\n]*'],
      ['del', '^(?:FAIL|---\\s*FAIL:|panic:|\\s*--- FAIL:)[^\\n]*'],
      ['add', '^(?:ok\\s|PASS|---\\s*PASS:|\\s*--- PASS:)[^\\n]*'],
      // `handler/user.go:42:7` — the thing anyone reading this is looking for.
      ['e', '\\b[\\w./@~-]*\\.go:\\d+(?::\\d+)?'],
      ['s', DQ], ['s', TICK],
      ['k', words(['undefined', 'cannot', 'expected', 'declared', 'imported',
        'unreachable', 'missing', 'unused', 'no', 'not'])],
    ]),

    json: compile([
      ['e', '"(?:\\\\[\\s\\S]|[^"\\\\])*"(?=\\s*:)'],
      ['s', DQ],
      ['n', NUM],
      ['k', words(['true', 'false', 'null'])],
      ['o', OPS],
    ]),

    yaml: compile([
      ['c', LINE_HASH],
      ['e', '^\\s*(?:-\\s+)?[\\w.$-]+(?=\\s*:(?:\\s|$))'],
      ['s', DQ], ['s', SQ],
      ['k', words(['true', 'false', 'null', 'yes', 'no', 'on', 'off'])],
      ['n', NUM],
      ['o', '[-:>|&*!\\[\\]{},]'],
    ]),

    toml: compile([
      ['c', LINE_HASH],
      ['t', '^\\s*\\[\\[?[^\\]\\n]*\\]\\]?'],
      ['e', '^\\s*[\\w.$-]+(?=\\s*=)'],
      ['s', DQ], ['s', SQ],
      ['k', words(['true', 'false'])],
      ['n', NUM],
      ['o', OPS],
    ]),

    sql: compile([
      ['c', '--[^\\n]*'], ['c', BLOCK_C],
      ['s', SQ], ['s', DQ],
      ['k', '\\b(?:SELECT|FROM|WHERE|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|ALTER|DROP|' +
        'TABLE|INDEX|VIEW|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|CROSS|ON|AS|AND|OR|NOT|NULL|IS|IN|' +
        'BETWEEN|LIKE|ILIKE|ORDER|GROUP|BY|HAVING|LIMIT|OFFSET|UNION|ALL|DISTINCT|CASE|WHEN|' +
        'THEN|ELSE|END|WITH|RETURNING|PRIMARY|FOREIGN|KEY|REFERENCES|CONSTRAINT|UNIQUE|DEFAULT|' +
        'CASCADE|BEGIN|COMMIT|ROLLBACK|TRANSACTION|EXISTS|IF|ADD|COLUMN|RENAME|TRUNCATE|' +
        'GRANT|REVOKE|USING|COALESCE|COUNT|SUM|AVG|MIN|MAX|NOW)\\b'],
      ['t', '\\b(?:INT|INTEGER|BIGINT|SMALLINT|SERIAL|BIGSERIAL|VARCHAR|CHAR|TEXT|BOOLEAN|BOOL|' +
        'DATE|TIME|TIMESTAMP|TIMESTAMPTZ|NUMERIC|DECIMAL|REAL|DOUBLE|PRECISION|JSON|JSONB|UUID|' +
        'BYTEA|ARRAY)\\b'],
      ['n', NUM],
      ['o', OPS],
    ]),

    shell: compile([
      ['c', LINE_HASH],
      ['s', DQ], ['s', SQ],
      ['e', '\\$\\{?[\\w@#?*-]+\\}?'],
      ['k', words(['if', 'then', 'elif', 'else', 'fi', 'for', 'while', 'until', 'do', 'done',
        'case', 'esac', 'in', 'function', 'return', 'break', 'continue', 'local', 'export',
        'set', 'unset', 'readonly', 'source', 'exit', 'trap'])],
      ['f', words(['echo', 'cd', 'ls', 'cat', 'grep', 'sed', 'awk', 'curl', 'go', 'git', 'make',
        'docker', 'kubectl', 'npm', 'node', 'python', 'pip', 'mkdir', 'rm', 'cp', 'mv', 'chmod',
        'test', 'sudo', 'apt', 'yum', 'tar', 'ssh', 'psql'])],
      ['n', NUM],
      ['o', '[|&;<>()$]+'],
    ]),

    js: compile([
      ['c', LINE_SLASH], ['c', BLOCK_C],
      ['s', TICK], ['s', DQ], ['s', SQ],
      ['n', NUM],
      ['k', words(['await', 'async', 'break', 'case', 'catch', 'class', 'const', 'continue',
        'debugger', 'default', 'delete', 'do', 'else', 'enum', 'export', 'extends', 'finally',
        'for', 'from', 'function', 'get', 'if', 'implements', 'import', 'in', 'instanceof',
        'interface', 'let', 'new', 'of', 'private', 'protected', 'public', 'readonly', 'return',
        'satisfies', 'set', 'static', 'super', 'switch', 'this', 'throw', 'try', 'type',
        'typeof', 'var', 'void', 'while', 'yield'])],
      ['n', words(['true', 'false', 'null', 'undefined', 'NaN', 'Infinity'])],
      ['t', words(['string', 'number', 'boolean', 'object', 'unknown', 'never', 'any', 'bigint',
        'symbol', 'Promise', 'Array', 'Record', 'Partial', 'Readonly', 'Map', 'Set'])],
      ['f', '\\b[A-Za-z_$][\\w$]*(?=\\()'],
      ['t', '\\b[A-Z][\\w$]*\\b'],
      ['o', OPS],
    ]),

    python: compile([
      ['c', LINE_HASH],
      ['s', '"""[\\s\\S]*?(?:"""|$)'], ['s', "'''[\\s\\S]*?(?:'''|$)"],
      ['s', DQ], ['s', SQ],
      ['n', NUM],
      ['k', words(['and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue', 'def',
        'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global', 'if', 'import',
        'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try',
        'while', 'with', 'yield', 'match', 'case'])],
      ['n', words(['True', 'False', 'None'])],
      ['t', words(['int', 'str', 'float', 'bool', 'bytes', 'list', 'dict', 'set', 'tuple',
        'self', 'cls'])],
      ['f', '\\b[A-Za-z_]\\w*(?=\\()'],
      ['o', OPS],
    ]),

    xml: compile([
      ['c', '<!--[\\s\\S]*?(?:-->|$)'],
      ['s', DQ], ['s', SQ],
      ['k', '<\\/?[\\w:.-]+'],
      ['e', '\\b[\\w:-]+(?==)'],
      ['o', '\\/?>'],
    ]),

    dockerfile: compile([
      ['c', LINE_HASH],
      ['k', '^\\s*(?:FROM|RUN|CMD|LABEL|MAINTAINER|EXPOSE|ENV|ADD|COPY|ENTRYPOINT|VOLUME|USER|' +
        'WORKDIR|ARG|ONBUILD|STOPSIGNAL|HEALTHCHECK|SHELL)\\b'],
      ['s', DQ], ['s', SQ],
      ['e', '\\$\\{?\\w+\\}?'],
      ['n', NUM],
      ['o', OPS],
    ]),

    makefile: compile([
      ['c', LINE_HASH],
      ['e', '^[\\w./%$()-]+(?=\\s*:(?!=))'],
      ['k', '^\\s*\\.(?:PHONY|DEFAULT|SILENT|SUFFIXES|PRECIOUS)\\b'],
      ['s', DQ], ['s', SQ],
      ['t', '\\$[({][\\w.-]+[)}]'],
      ['o', OPS],
    ]),

    proto: compile([
      ['c', LINE_SLASH], ['c', BLOCK_C],
      ['s', DQ], ['s', SQ],
      ['k', words(['syntax', 'package', 'import', 'option', 'message', 'enum', 'service', 'rpc',
        'returns', 'repeated', 'optional', 'required', 'oneof', 'map', 'reserved', 'extend',
        'stream', 'public'])],
      ['t', words(['double', 'float', 'int32', 'int64', 'uint32', 'uint64', 'sint32', 'sint64',
        'fixed32', 'fixed64', 'sfixed32', 'sfixed64', 'bool', 'string', 'bytes'])],
      ['n', NUM],
      ['o', OPS],
    ]),

    ini: compile([
      ['c', '[;#][^\\n]*'],
      ['t', '^\\s*\\[[^\\]\\n]*\\]'],
      ['e', '^\\s*[\\w.$-]+(?=\\s*=)'],
      ['s', DQ], ['s', SQ],
      ['n', NUM],
      ['o', '='],
    ]),
  };

  /** Aliases, so a fence tagged `sh` or `tsx` colours like the thing it is. */
  const ALIAS = {
    golang: 'go', gomod: 'ini', gosum: 'ini',
    sh: 'shell', bash: 'shell', zsh: 'shell', console: 'shell', shellsession: 'shell',
    javascript: 'js', typescript: 'js', ts: 'js', tsx: 'js', jsx: 'js', mjs: 'js', cjs: 'js',
    py: 'python',
    yml: 'yaml',
    html: 'xml', svg: 'xml', vue: 'xml',
    postgres: 'sql', postgresql: 'sql', mysql: 'sql', psql: 'sql',
    docker: 'dockerfile', containerfile: 'dockerfile',
    make: 'makefile', mk: 'makefile',
    protobuf: 'proto',
    cfg: 'ini', conf: 'ini', properties: 'ini', env: 'ini', dotenv: 'ini',
    jsonc: 'json', json5: 'json',
  };

  /** Guessed from a path when a fence carries a filename but no language. */
  const BY_EXT = {
    go: 'go', mod: 'ini', sum: 'ini', json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml',
    sql: 'sql', sh: 'shell', bash: 'shell', js: 'js', mjs: 'js', cjs: 'js', ts: 'js',
    tsx: 'js', jsx: 'js', py: 'python', html: 'xml', xml: 'xml', svg: 'xml', proto: 'proto',
    ini: 'ini', cfg: 'ini', conf: 'ini', env: 'ini', md: 'markdown', txt: 'plaintext',
  };

  function normalise(language) {
    const name = String(language || '').toLowerCase().trim();
    if (!name) return '';
    if (GRAMMARS[name]) return name;
    if (ALIAS[name]) return ALIAS[name];
    // `Dockerfile` and `Makefile` arrive as bare filenames as often as tags.
    if (name.indexOf('dockerfile') !== -1) return 'dockerfile';
    if (name.indexOf('makefile') !== -1) return 'makefile';
    const dot = name.lastIndexOf('.');
    if (dot !== -1 && BY_EXT[name.slice(dot + 1)]) return BY_EXT[name.slice(dot + 1)];
    return '';
  }

  /**
   * A unified diff, coloured by line rather than by token.
   *
   * Diffs are the one thing here where the interesting unit is the line: what a
   * reader wants is "what moved", and tokenizing inside a `-` line would give
   * the removed code the same weight as the code that replaced it.
   */
  function diffTokens(code) {
    const out = [];
    const all = String(code).split('\n');
    all.forEach(function (line, i) {
      const text = line + (i < all.length - 1 ? '\n' : '');
      if (/^(?:diff |index |--- |\+\+\+ |old mode|new mode|similarity |rename )/.test(line)) {
        out.push(['c', text]);
      } else if (/^@@/.test(line)) {
        out.push(['e', text]);
      } else if (line.charAt(0) === '+') {
        out.push(['add', text]);
      } else if (line.charAt(0) === '-') {
        out.push(['del', text]);
      } else {
        out.push([null, text]);
      }
    });
    return out;
  }

  /** The highlighter's own ceiling. Past it, colour is not worth the layout. */
  const MAX_CHARS = 120000;

  /**
   * `code` -> `[[class|null, text], ...]`, covering every character exactly once.
   *
   * The contract the caller depends on: concatenating the texts reproduces the
   * input character for character. A highlighter that drops a character
   * silently corrupts the one thing a code block exists to show.
   */
  function tokenize(code, language) {
    const text = String(code === undefined || code === null ? '' : code);
    if (!text) return [];

    const raw = String(language || '').toLowerCase();
    if (raw === 'diff' || raw === 'patch') return diffTokens(text);

    const grammar = GRAMMARS[normalise(language)];
    if (!grammar || text.length > MAX_CHARS) return [[null, text]];

    const out = [];
    let cursor = 0;
    grammar.re.lastIndex = 0;
    let match;
    while ((match = grammar.re.exec(text)) !== null) {
      // A zero-width match would spin the loop forever on a grammar bug.
      if (match[0] === '') {
        grammar.re.lastIndex += 1;
        continue;
      }
      if (match.index > cursor) out.push([null, text.slice(cursor, match.index)]);
      let cls = null;
      for (let g = 1; g < match.length; g += 1) {
        if (match[g] !== undefined) {
          cls = grammar.classes[g - 1];
          break;
        }
      }
      out.push([cls, match[0]]);
      cursor = match.index + match[0].length;
    }
    if (cursor < text.length) out.push([null, text.slice(cursor)]);
    return out;
  }

  /** Whether this build would colour a fence tagged `language` at all. */
  function supports(language) {
    const name = String(language || '').toLowerCase();
    return name === 'diff' || name === 'patch' || Boolean(GRAMMARS[normalise(language)]);
  }

  global.dakHighlight = { tokenize: tokenize, supports: supports, normalise: normalise };
})(typeof globalThis !== 'undefined' ? globalThis : this);
