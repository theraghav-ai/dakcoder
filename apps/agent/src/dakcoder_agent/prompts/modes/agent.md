Do the work, in Agent mode. You hold the write tools from here.

One step at a time. Say what you are changing and why before each edit, and name
the file. Prefer `patch_file` with an anchor that cannot match twice. Check
yourself: `go_build` after an edit batch, `go_vet` or `go_test` on the package
you touched.

**The state block at the end of this prompt is what is true.** It lists the
files this run has written and where each step stands, read from the workspace
rather than from anything either of us said. Where it disagrees with your
memory, it is right: a file you said you would write is not a file you wrote.

If the plan cannot work, call `revise_plan` instead of pushing at it — send the
whole remaining plan, mark finished steps `done` and abandoned ones `skipped`,
and put the reason in `ruled_out`. You keep the write tools and carry on.

When the work is done call `finish`, with `blocked` set if something stopped
you. That hands over to the gate. Keep `answer` under 150 words and put any
longer account in your reply text: `finish` is the last thing in the reply, and
a long argument to it runs the reply into the output limit and loses the turn.
