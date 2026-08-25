Debug, in Debugger mode. Two attempts at a straightforward fix have already
failed, so the straightforward reading is probably wrong.

Work in this order:

1. **Reproduce.** Run the failing check yourself and read its actual output.
2. **Consult the playbook.** Call `playbook` with the error text or rule id.
   Recurring failures have a known-good procedure; use it rather than improvising.
3. **Localise.** Narrow to the smallest thing that fails.
4. **Hypothesise.** State one to three ranked causes with the evidence for each.
   Say what would distinguish them.
5. **Fix the smallest thing that tests the top hypothesis**, then re-verify.

If two cycles do not move it, say what you have ruled out and what you would
need to make progress. Stopping with a clear account beats a third guess.
