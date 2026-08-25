# Vendored wheels

CI fills this directory with `dakcoder_agent`, `dakcoder_shared` and their full
transitive closure, built per platform tag with `pip download`.

The runtime installs from here with `--no-index --find-links`, so first run
touches the network **zero** times. Behind the India Post proxy a network
install is minutes at best and the documented failure mode at worst, and a
first-run experience that can fail on the network will fail for someone on day
one of the pilot.

`dakcoder-gateway` is deliberately absent. The wheel shipped here carries the
agent and nothing else — no OAuth exchange, no quota enforcement, no ledger, and
no code path that reads a model credential.
