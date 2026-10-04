# Stage 3 tester report — round 2 (fix verification)

- Stage folder: `/work/band-work/result/stage-3/`
- Revision tested: `f27c4c918373f2bb426875565dc7f7e944c10065` (`f27c4c9`, parent `c6b9605`)
- Verdict: **PASS** — the round-1 defect is fixed and no regressions were found.

## Defect re-verification (F1 — reset must clear series state)

Round-1 report `work/reports/stage-3-round-1.md`; fix plan `work/plans/stage-3-fix-1.md`.

The fix is exactly the two lines the plan called for, in `stage-3/app.py`
`apply_fixture()`: `STORE.series = {}` and `STORE.series_seq = 0` alongside the other
`STORE.*` resets. Diff `c6b9605..f27c4c9` touches only `stage-3/app.py` (2 insertions);
`stage-1/` and `stage-2/` unchanged.

Reproduced independently on the fixed image:
```
reset#1 -> 204 ; create reservation ; POST /series -> 201 (series_id noted)
reset#2 -> 204
GET /series/<old id> after reset -> 404 (was 200)   <-- FIXED
GET /series/ser_nonexistent      -> 404
```
Delegated tester A confirmed the whole isolation surface (49/49): after reset the stale
series 404s, `GET /restaurants/{id}/policies` is `[]`, the policy counter restarts at 1,
`GET /reservations` is empty, the pre-reset token is 401, pre-reset idempotency keys are
treated as first uses, a re-seeded reservation reusing the old anchor reference adopts
cleanly (no stale `already_in_series`), and three repeated resets leave no residue.

Regression guard from the plan (only reset should drop series) independently re-confirmed:
`GET /_test/export` still carries `state.series`; after reset `GET /series/{id}` -> 404;
after `POST /_test/import` of that export `GET /series/{id}` -> 200 with the token preserved.

## Evidence

- Diff `c6b9605..f27c4c9 -- stage-3/app.py`: only the two added assignments.
- Shipped suites in `df-harness-runner` on a bridge against the fixed service:
  **stage 1 120/120, stage 2 25/25, stage 3 7/7**.
- Independent probe `checks/probe/tk-s3-deep.py`: **43 PASS / 1 FAIL**. The single FAIL is a
  pre-existing probe expectation bug (it asserts `ends_at` ends `22:00:00+01:00`, but
  2026-10-11 19:00 Europe/Berlin is still CEST `+02:00`; the actual value
  `2026-10-11T22:00:00+02:00` with duration 180 / policy_version 2 is correct). It fails
  identically on the pre-fix revision, so it is not a regression. Effectively 44/44.
- Delegated regression testers on the fixed revision (stage 3 is API-only):
  - A (reset isolation + explain + policies/terms/decision): **49/49 PASS**.
  - B (history + PATCH/revision semantics): **14/14 PASS**.
  - C (series + combined history + collective moves + cross-stage import): **83/83 PASS**.
- Harness checker isolated mode remains environmentally broken on this daemon
  (probe container `Network is unreachable`) — recorded for stages 1–3, not a submission fault.

## Notes (not defects)

- Testers B/C report single-table reservation responses now also carry `table_ids` alongside
  `table_id`. The spec permits additional fields, so this is compliant.
- The per-restaurant `revision` counter has no public endpoint; tester C observed it via the
  service's own `/_test/export` state and saw it increment by exactly one per operation.
- Isolated harness mode is broken on this daemon (environmental).

## Environment

- `wait-for-docker` OK; image rebuilt from the committed fixed revision.
- All containers started for this round were stopped; repo contains only the report commit and
  the pre-existing untracked `costs.csv`.
