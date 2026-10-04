# Stage 3 tester report — round 1

- Stage folder: `/work/band-work/result/stage-3/`
- Revision tested: `c6b960562c57d98a28b2825fe44622a324b6cdff` (`c6b9605`)
- Verdict: **FAIL** — one requirement violated.

## Summary of verification performed

- Shipped suites run inside `df-harness-runner` on a bridge network against the
  stage-3 service (proxy env cleared): **stage 1 120/120, stage 2 25/25, stage 3 7/7**.
- Independent spec-derived probe `/work/band-work/checks/probe/tk-s3-deep.py`:
  **44/44** (explain shape/strictness, policy versions/publication/selection/ties/past
  dates, amendment across a policy boundary, history created/changed/no-op/cancelled/replay,
  revision counters, decision owner-only 404-without-auth, `expected_revision`, series
  adoption/occurrences/replay/mutations/atomic-failure/spring-forward gap).
- Three delegated API testers (stage-3 is API-only, no browser needed):
  - Tester A (explain + policies + terms/decision): 73 PASS / 1 "FAIL" — that one is a
    bad *expectation* on the tester's side, not a defect (see Observations).
  - Tester B (history + PATCH/revision): **16/16 PASS**.
  - Tester C (series + moves + combined history + cross-stage import): **11/11 PASS**,
    plus the extra defect below (independently reproduced here).
- Harness checker in isolated mode: **environmentally broken** (probe container cannot
  route to the service container: `Network is unreachable`, `checks/stage-3-isolated/`).
  Same non-submission blocker seen for stages 1 and 2. Not a defect.

## FAILURE 1 — `POST /_test/reset` does not clear recurring-series state

**Requirement (stage-1.md §3.3, which continues to apply):**
> Replace all service state with the fixture in the request body (§4). When reset returns
> 204, subsequent requests must see only that fixture. Repeated resets are supported.

Series objects created by `POST /series` are service state; after a reset they must be gone
and `GET /series/{series_id}` for one of them must return 404 (stage-3.md: "Only the owner may
read it: another user or no token gives 404 `not_found`"). Today a previously created series
survives reset and still answers 200.

**Steps to reproduce** (fresh container, no prior state):
1. `POST /_test/reset` with any valid fixture → 204.
2. Log in, create a reservation (cutoff 0), then
   `POST /series` `{"anchor_reference": R, "count": 3, "interval_weeks": 1}` → 201,
   note `series_id`.
3. `POST /_test/reset` with the same fixture → 204.
4. `GET /series/{series_id}` with a token from after the reset.

**Expected:** 404 `not_found` (the series no longer exists; a never-created id correctly 404s).
**Actual:** 200, returning the stale series with its occurrence list. The stale record also
resolves occurrence 0 by reference lookup, so after re-seeding a reservation with that same
reference it is presented as part of a series that the current fixture does not define.

Raw evidence (fresh container `tk-s3-fresh`, port 38088):
```
reset#1 -> 204
created series: ser_c34d0b7e49963095 occ refs: ['0KL9FZ', 'OQJOWI', 'Z3DTDI']
reset#2 -> 204
GET /series/<created-id> after reset -> 200 (spec: 404)
GET /series/nonexistent              -> 404
GET /reservations (fixture only)     -> 0
```

**File and line:** `stage-3/app.py`, `apply_fixture()` (the handler called by
`handle_reset()`, line 1010 → 1017). It rebuilds and assigns `STORE.users`,
`STORE.email_index`, `STORE.tokens`, `STORE.restaurants`, `STORE.reservations`, `STORE.idem`
(lines 742–747) but never assigns `STORE.series` / `STORE.series_seq`. The class's own
`clear()` (lines 150–160) *does* reset both, and `import_state()` (lines 895–910) sets both —
so the reset path alone was missed. Fix is to add `STORE.series = {}` and
`STORE.series_seq = 0` where the other `STORE.*` fields are assigned in `apply_fixture`.

## Observations (not defects)

- Tester A's single "FAIL" — `opening_hours: []` accepted with 201 — is a wrong expectation:
  stage-1.md line 104 defines opening hours as "Per weekday. A day with no entry is closed",
  and stage-3.md line 126 says policy opening hours "follow stage 1". An empty list is the
  valid "always closed" case, so 201 is correct.
- Policy error-ordering on `POST /restaurants/{id}/policies`: observed
  no-token→401, authenticated non-manager→403, unknown restaurant→404,
  missing idempotency key→400; consistent with the builder's flag and not contradicted by the spec.
- Same-second writes yield a strict `seq` total order (identical `at`, seq 1,2,3).
- The per-restaurant `revision` counter is not exposed by any documented endpoint, so
  "restaurant revision increments once" cannot be observed externally; not counted as untested
  beyond that limitation.
- Tester C's cross-check of the "always cancellable" case was itself mis-worded; both
  directions were verified (accepted cutoff 0 stays cancellable under a later restrictive
  policy; accepted cutoff 10080 → 409 `cutoff_passed`), confirming cancel uses accepted terms.

## Environment

- `wait-for-docker` OK; image rebuilt from the committed revision.
- Isolated harness mode broken on this daemon (documented above).
