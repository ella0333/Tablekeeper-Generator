# Stage 4 tester report — round 2 (fix verification)

- Stage folder: `/work/band-work/result/stage-4/`
- Revision tested: `0319b873270f082604db0b14b4b7ce597482314f` (`0319b87`, parent `f797549`)
- Verdict: **PASS** — the round-1 defect is fixed and no regressions were found.

## Defect re-verification (F1 — replan preview missing `table_id`)

Round-1 report `work/reports/stage-4-round-1.md`; fix plan `work/plans/stage-4-fix-1.md`.

The fix is the change the plan specified, in `stage-4/app.py` `handle_replan_preview()`
(after the idempotency check): `table_id` being absent, `null` or `""` now raises
422 `validation_failed` before the table lookup; a non-string stays 400 `malformed_request`;
a present, non-empty, unknown id stays 404 `not_found`. Diff `f797549..0319b87` touches only
`stage-4/app.py` (1 insertion, 1 deletion); `stage-1/`, `stage-2/`, `stage-3/` unchanged.

Reproduced independently on the fixed image:
```
replan missing table_id -> (422, validation_failed)   (was 404)
replan table_id null    -> (422, validation_failed)
replan table_id ""      -> (422, validation_failed)
replan table_id 123     -> (400, malformed_request)
replan unknown "nope"   -> (404, not_found)
replan valid "t_2"      -> 201
```
Delegated tester A confirmed 34/34 across the fix, preview regression, the three-level
ordering (decisive case for each level, including singles-before-pairs), preview purity,
idempotency, `no_feasible_plan`, restaurant-revision accounting and `planning_limit`.

## Evidence

- Diff `f797549..0319b87 -- stage-4/app.py`: only the `table_id` presence check.
- Shipped suites in `df-harness-runner` on a bridge against the fixed service:
  **stage 1 120/120, stage 2 25/25, stage 3 7/7, stage 4 6/6**.
- Independent probe `checks/probe/tk-s4-deep.py`: **14/14**.
- Delegated regression testers on the fixed revision:
  - A (replan preview + the fix + ordering/purity/limits): **34/34 PASS**.
  - B (plan application and effects): **10/10 PASS**.
  - C (series amend + seating repairs + cross-stage import): **39/39 PASS**.
- Harness checker isolated mode remains environmentally broken on this daemon
  (probe container `Network is unreachable`) — recorded for stages 1–4, not a submission fault.

## Notes (not defects)

- The per-restaurant `revision` counter has no public read endpoint; it is visible in replan
  responses and `_test/export` state. Testers observed it increment exactly once per successful
  mutating operation and stay unchanged for no-ops, failures, previews and replays; plan
  application bumps it once for the whole plan.
- `reassigned` history entries carry `plan_id` at the entry level with a
  `{field: "table_ids", from: [...], to: [...]}` change — matches the plan's stated reading.
- `planning_limit` (422) triggers beyond 6 tables / 4 pairs / 6 considered bookings, as permitted.

## Environment

- `wait-for-docker` OK; image rebuilt from the committed fixed revision.
- All containers started for this round were stopped; repo contains only the report commit and
  the pre-existing untracked `costs.csv`.
