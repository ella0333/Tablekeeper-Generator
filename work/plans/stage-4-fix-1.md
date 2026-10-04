# Stage 4 fix plan 1

Report: `work/reports/stage-4-round-1.md` (revision tested `f797549`).
Scope: only the one confirmed failure. Everything else the tester passed stays as it is.
Target folder: `/work/band-work/result/stage-4/` (file `app.py`).

## F1 — replan preview must return 422 when `table_id` is missing

- Requirement: stage-1 §5 — "422 `validation_failed` — A required field or query parameter is
  missing … unless an endpoint specifies a different error." stage-4 specifies 404 only for an
  **unknown table** ("invalid interval is 422 `validation_failed`, unknown table 404"). A
  *missing* `table_id` therefore follows §5 and must be 422.
- Where: `app.py` `handle_replan_preview()` lines 2599–2603. `tid = body.get("table_id")` may be
  `None`; the code only type-checks it against `str`, then calls `find_table(restaurant, tid)`,
  which returns `None` for a missing id and raises `404 not_found` before presence is checked.
- Failing cases: `{}` → 404; `{"from":…,"to":…}` (no `table_id`) → 404; `{"table_id": null, …}`
  → 404. All must be 422 `validation_failed`.
- Fix: after reading `tid`, if it is absent, `null`, or the empty string, raise
  `422 validation_failed` ("table_id is required"). Keep the existing wrong-type rule (a
  non-string, non-null `table_id` → 400 `malformed_request`) and keep 404 `not_found` for a
  **present, non-empty, unknown** table id. Do this before the `from`/`to` handling so `{}` and
  a missing `table_id` both 422 consistently; do not change the order relative to the
  idempotency check (`check_replay` must still run first, per §7).
- Correct looks like:
  - `replan {}` → 422 `validation_failed`
  - `replan {"from":…,"to":…}` (no `table_id`) → 422 `validation_failed`
  - `replan {"table_id": null, …}` → 422 `validation_failed`
  - `replan {"table_id": "", …}` → 422 `validation_failed`
  - `replan {"table_id": "t_does_not_exist", …}` → 404 `not_found` (unchanged)
  - `replan {"table_id": 5, …}` → 400 `malformed_request` (unchanged)
  - a valid manager preview with a real table and `from < to` → 201 (unchanged)

## Regression guard

- Re-run the shipped stage-1–4 suites (expected 120/120, 25/25, 7/7, 6/6) and the tester's
  stage-4 deep probe (14/14), plus the series-amend checks.
- Re-confirm the unknown-table 404 and the whole apply/replay/stale_plan surface still behave.

## Definition of done

F1 fixed with no regression; `stage-4/` still builds and starts from `RUN.md` with no manual
steps; the tester's round-2 run is green.