# Stage 1 tester report — round 2 (PASS)

- Revision tested: `35a52650e4f36ef6fc4a69430b434d28700f28e4` (short `35a5265`, "Stage 1 fix 1: validate fixture ids/references, path-scope idempotency keys")
- Previous revision (round 1): `fc93d2f818915803ef6584a9f6517c6cf9db1abe` — failed with the 5 defects in `work/reports/stage-1-round-1.md`.
- Stage folder: `/work/band-work/result/stage-1/`
- Verdict: **PASS** — the 5 reported defects are fixed and no regression was found.

## Evidence

- Built the committed revision from `stage-1/Dockerfile`; image `sha256:5894fe3a2acf9c3fa4ed484c23d02d80a6f904b22704dc841947cd5d33994176` (matches the builder's).
- Shipped stage-1 suite, base-url mode: **120 passed / 0 failed** (`/work/band-work/checks/stage-1-fix1-official/stage-1.log`, `stage-1.counts.json`). Previously 116/120 with F1–F4 failing.
- Direct reproduction of every fix against the running container (round-1 cases all now behave as reported).
- Three tester agents on isolated per-role containers: fixture-validation 100% of its explicit checks; idempotency path-scoping **10/10**; broad regression **63/63** (health/public, availability, create/errors, half-open, DST, read/cancel/patch, moves, 50-way concurrency, export/import).

## Fix verification

- **F1** — `POST /_test/reset` rejects over-long / non-string fixture IDs with `422 validation_failed` and no state change: user/restaurant/table/reservation `id`/`reservation_id` at 65 chars all 422; exactly 64 chars accepted (204). Rejected fixtures leave the previous state intact (verified).
- **F2/F3/F4** — seeded reservation `reference` must match `^[A-Z0-9]{6,12}$` and be unique in the fixture: `x`, `lower01`, `TOO-LONG-WITH-DASH`, `ABC12` (5), `ABC-123`, 13 chars all 422; 6- and 12-char refs accepted and readable; duplicate references 422; absent/null reference generates a valid unique one.
- **F5** — idempotency receipts are namespaced by `(method, path, key)` per user: a key first used on `POST /reservations` is a first use on `POST /reservation-moves` (201, not 409); same-path same-body replays still return the cached original body with 200; same-path different-body still 409 `idempotency_key_reuse`; store still per-user; export/import round-trips the receipts.

## Observations (non-blocking)

- A non-string fixture ID (e.g. a JSON number) returns `422 validation_failed`, matching the spec's convention for a malformed state document (§10: an invalid `state` gives `422 validation_failed`, while only unparseable JSON is `400 malformed_request`). Recorded for the planner; not scored as a defect.
- Environment: the checker's isolated mode still cannot run on this Docker daemon — container-to-container traffic on an `--internal` network is not routable, so `harness run --mode isolated` fails before any test with `httpx.ConnectError: [Errno 101] Network is unreachable` (`/work/band-work/checks/stage-1-round-2-isolated/`). This is an infrastructure limitation, not a submission failure. The shipped suite was therefore run in base-url mode.
