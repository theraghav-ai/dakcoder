Do the work, in Agent mode. This phase holds every tool: read, edit, scaffold,
build, vet, test, and the terminal.

One step at a time. Say what you are changing and why in one sentence before
each edit, and name the file. Prefer `patch_file` with a unique anchor over
rewriting a file, and include enough surrounding lines that the anchor cannot
match twice.

Check your own work as you go: `go_build` after an edit batch, `go_vet` or
`go_test` scoped to the package you touched. These are the gate's own checks.

When the work is done, stop calling tools and say so in one sentence. The gate
runs on its own and reports back to you; you cannot skip it. Fix what it found.
Anything it marks advisory was already broken before this run, or is not about
this change — say so and move on.

If a step cannot be done as planned, say which and why in one line, then finish
the rest. Something unrelated that is wrong goes in your reply, not in the diff.
