# Tablekeeper — Stage 3: booking policies, history and recurring reservations

A containerized HTTP service for restaurant reservations, adding dated booking
policies, per-table availability explanations, reservation history and recurring
reservations on top of the Stage 1 API and the Stage 2 browser UI.

## What is here

- `app.py` — the whole service: a pure standard-library `ThreadingHTTPServer`
  that serves both the JSON API and server-rendered HTML screens.
- `Dockerfile` — `python:3.12-slim` plus `app.py`; no third-party runtime
  dependencies, no run-time network.
- `RUN.md` — build/run commands, the fixture shape and the endpoint reference.

## Design notes

- **One global lock.** Every request runs under a single re-entrant lock, so
  check-then-act sequences (availability, overlap, idempotency, revision checks)
  cannot interleave: concurrent requests behave as some serial order.
- **Policies.** Policy 0 is the fixture configuration. Published policies are
  immutable and versioned from 1 per restaurant. A local date selects the policy
  with the greatest `effective_from` not later than it, ties by greatest version.
- **Accepted terms.** Each reservation snapshots the whole selected policy (minus
  `effective_from`) as `accepted_terms`; amendments adopt the resulting date's
  policy, while cancellation uses the accepted cutoff.
- **History.** A total order via `seq`; a no-op amendment records nothing; each
  entry keeps the resulting `revision` and `accepted_terms`.
- **Series.** A recurring agreement stores its occurrences' references and
  exception flags; generated occurrences select their own date's policy and obey
  opening, DST and occupancy rules. Failures leave no partial state.
- **DST.** Durations are added in absolute time (through UTC) and wall times are
  resolved with the first-occurrence (fold 0) rule; a nonexistent local time is
  `invalid_local_time`.
- **Cross-stage import.** Exports stay at format version 1; an import from a
  stage-1 or stage-2 export is normalised into the stage-3 model without
  regenerating ids, references or timestamps.

## Screens

`/`, `/signup`, `/login`, `/lookup` — unchanged from stage 2 and still driven by
the required `data-testid` attributes.
