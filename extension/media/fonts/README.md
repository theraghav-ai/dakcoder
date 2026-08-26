# Vendored fonts

Two families, four files, ~84 KB. Both are variable fonts: one file per
unicode-range covers every weight the panel uses, which is why there is no
`-400` / `-600` split here.

| File | Family | Weights | Range |
|---|---|---|---|
| `instrument-sans-latin.woff2` | Instrument Sans | 400–600 | latin |
| `instrument-sans-latin-ext.woff2` | Instrument Sans | 400–600 | latin-ext |
| `martian-mono-latin.woff2` | Martian Mono | 400–500 | latin |
| `martian-mono-latin-ext.woff2` | Martian Mono | 400–500 | latin-ext |

Cyrillic is deliberately not vendored. Nothing in the string bundle needs it, and
the fallback chain in `chat.css` hands anything outside these ranges to
`--vscode-font-family`, which is the font the developer already chose.

## Why vendored rather than linked

The chat webview runs under `default-src 'none'` with `font-src` limited to the
extension's own origin (`media/chat/index.html`). A `<link>` to
`fonts.googleapis.com` is blocked by that policy, and relaxing the policy to
admit a font host would also admit everything else on it. A webview that cannot
reach the network cannot leak a transcript to one, and that guarantee is worth
more than 84 KB.

It also means the panel renders identically offline, which matters on a host
where the gateway is reachable but the public internet may not be.

## Licence

Both families are under the SIL Open Font License 1.1, which permits
redistribution inside the `.vsix`. See `OFL.txt`.

- Instrument Sans — Copyright 2022 The Instrument Sans Project Authors
  (https://github.com/Instrument/instrument-sans)
- Martian Mono — Copyright 2022 The Martian Mono Project Authors
  (https://github.com/evilmartians/mono)

## Refreshing them

```bash
# the css2 endpoint returns woff2 only for a modern User-Agent
curl -A "$MODERN_UA" "https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600&display=swap"
curl -A "$MODERN_UA" "https://fonts.googleapis.com/css2?family=Martian+Mono:wght@400;500&display=swap"
```

Take the `latin` and `latin-ext` `src:` URLs from each response and save them
under the names in the table above. If the upstream version bumps (`/v4/`,
`/v6/`), the filenames here stay the same — `chat.css` references these names,
not Google's.
