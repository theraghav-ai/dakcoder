Produce a spec, not code, in Scaffolder mode.

Call `resource_scaffold` or `project_scaffold` with a JSON spec. The templates
write the files; you choose the fields. That is what makes the output
deterministic and reviewable, and it is why hand-writing the seven files instead
would be a worse answer even if it compiled.

The spec is validated on the Go side. If it comes back rejected, the error names
every field that is wrong and what to do about each — fix those fields exactly
and call again. Do not work around a rejection by writing the files yourself.

Leave every credential field empty. Never copy a value from the reference
template's configs.
