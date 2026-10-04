# Tablekeeper — Stage 4: seating changes and recurring amendments

A containerized HTTP service for restaurant reservations, adding manager seating
repairs after a table closure and recurring-series amendments on top of the
Stage 1 API, the Stage 2 browser UI and the Stage 3 policies, history and
recurring reservations.

## What is here

- `app.py` — the whole service: a pure standard-library `ThreadingHTTPServer`
  that serves both the JSON API and server-rendered HTML screens.
- `Dockerfile` — `python:3.12-slim` plus `app.py`; no third-party runtime
  dependencies, no run-time network.
- `RUN.md` — build/run commands, the fixture shape and the endpoint reference.

## Where it lives

This stage lives in the repository at `stage-4/`:

```
stage-4/
├── app.py       the service (source)
├── Dockerfile   builds the runtime image
├── RUN.md       build/run commands and a sample fixture
└── README.md    this file
```

The specifications are `tablekeeper/spec/stage-1.md`, `stage-2.md`, `stage-3.md`
and `stage-4.md`
in the hackathon kickoff repository (not part of this repository).

## Build and run

```sh
# build the image
docker build -t tablekeeper-s4 stage-4

# start the service (listens on 0.0.0.0:$PORT, default 8080)
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-s4
```

No manual setup steps are required. Confirm it is healthy, then open the UI at
<http://localhost:8080/>:

```sh
curl -s http://localhost:8080/health
# {"status": "ok"}
```

The service starts with no restaurants. Load some by `POST`ing a fixture to
`/_test/reset` (see `RUN.md` for a complete sample body), then search in the
browser or use the JSON API.

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
- **Replans.** A closure preview brute-forces the feasible assignments over
  singles and declared pairs and minimizes change count, then unused seats, then
  the option-rank vector in reference order. Applying is atomic and moves each
  booking with a `reassigned` history entry; the closure then excludes the table
  (and any pair containing it) from availability and new bookings.
- **Restaurant revision.** Starts at 0 after a reset and increments once per
  successful booking, amendment, cancellation, policy publication, series
  adoption, moves batch or plan application; no-op writes, previews, failures and
  replays never change it.
- **Series amendments.** Change the clock time of eligible occurrences in place;
  they never mark exceptions, are idempotent, and bump the series and restaurant
  revisions once for the whole operation only if something changed.
- **Cross-stage import.** Exports stay at format version 1; an import from a
  stage-1, stage-2 or stage-3 export is normalised into the stage-4 model without
  regenerating ids, references or timestamps, and applied closures round-trip.

## Screens

`/`, `/signup`, `/login`, `/lookup` — unchanged from stage 2 and still driven by
the required `data-testid` attributes.

## Push this repository to GitHub

The repository is `main`-branch based. To publish a copy:

```sh
# 1. create an empty repository on GitHub (no README, no .gitignore) and note its
#    URL, e.g. https://github.com/<owner>/<repo>.git
# 2. from the repository root, add it as a remote and push main
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main
```

If the remote already exists, adjust it instead:

```sh
git remote set-url origin https://github.com/<owner>/<repo>.git
git push -u origin main
```
