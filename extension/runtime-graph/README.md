# Vendored wheels for the code-graph pilot

graphify and the part of its closure this product needs, filled by
`python scripts/vendor-graphify.py` (which `scripts/release.py` runs). The
wheels are gitignored, like `runtime/`'s.

The extension installs them offline (`--no-index --no-deps`) into the runtime
venv only when `dakcoder.codeGraph.enabled` is on. See the script's docstring
for why the list is hand-kept and why `--no-deps` is safe here.
