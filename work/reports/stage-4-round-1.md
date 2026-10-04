# Stage 4 tester report — round 1

- Stage folder: `/work/band-work/result/stage-4/`
- Revision tested: `f7975495e70902e15ce6a8031523863ad583c2e6` (`f797549`)
- Verdict: **FAIL** — one requirement violated.

## Summary of verification performed

- Shipped suites in `df-harness-runner` on a bridge against the stage-4 service:
  **stage 1 120/120, stage 2 25/25, stage 3 7/7, stage 4 6/6**.
- Independent probe `checks/probe/tk-s4-deep.py`: **14/14** (three-level plan ordering with a
  decisive case for each level; feasibility and `no_feasible_plan`; assignments in reference
  order; preview purity; apply → move/revision/`reassigned` history/closure effects;
  `stale_plan`, `plan_already_applied`, replay).
- Independent checks of `POST /series/{id}/amend`: revision counters (0→1→2→3 via export state),
  per-occurrence revision + `changed` history, scheduled dates preserved, no exceptions,
  no-op succeeds unchanged, `stale_revision` 409, invalid bodies 422, non-owner 404, no token 401,
  replay 200.
- Delegated testers: A (replan preview) — the one defect below, everything else green;
  B (apply and effects) **10/10 PASS**; C (series amend + seating repairs + cross-stage import)
  **8/8 PASS**.
- Harness isolated mode remains environmentally broken on this daemon (`Network is unreachable`),
  as for stages 1–3. Not a submission fault.

## FAILURE 1 — replan preview returns 404 instead of 422 when `table_id` is missing

**Requirement (stage-1.md §5, which continues to apply):**
> `422 validation_failed` — A required field or query parameter is missing, or a stated rule is
> violated with no more specific code … unless an endpoint specifies a different error.

stage-4.md ("Seating changes after a table closure") specifies 404 `not_found` only for an
**unknown table** ("invalid interval is 422 `validation_failed`, unknown table 404"). It does not
specify a different error for a *missing* `table_id`, so the §5 rule applies: a missing required
body field is 422 `validation_failed`.

**Steps to reproduce** (manager token on `/restaurants/r_anker/replans`):
```
POST /restaurants/r_anker/replans   Idempotency-Key: b
{"from": "<D>T18:00:00+02:00", "to": "<D>T23:00:00+02:00"}      (no table_id)
```
**Expected:** 422 `validation_failed`.
**Actual:** 404 `not_found`.

Raw evidence:
```
replan {}  (no fields)      -> 404 not_found
replan missing table_id     -> 404 not_found   <-- wrong
replan missing from,to      -> 422 validation_failed
replan table_id null        -> 404 not_found
replan unknown table        -> 404 not_found
```
The endpoint is inconsistent with itself (missing `from`/`to` is 422) and with the rest of the
service, which follows §5:
```
POST /reservations missing table_id  -> 422 validation_failed
POST /reservations missing starts_at -> 422 validation_failed
POST /reservations unknown table     -> 404 not_found
```

**File and line:** `stage-4/app.py`, `handle_replan_preview()` lines 2599–2603. `tid` may be
`None`; the code only type-checks it, then calls `find_table(restaurant, tid)`, which returns
`None` for a missing id and raises `404 not_found` at line 2602–2603 before presence is checked.
An unknown (present) table correctly gives 404; a *missing* field should give 422 first.
Fix: raise `422 validation_failed` when `table_id` is absent/None (or `""`), keeping 404 for a
present-but-unknown id.

## Observations (not defects)

- The per-restaurant `revision` counter is not exposed by a documented read endpoint; it is
  visible in replan responses and `_test/export` state. Testers observed it incrementing exactly
  once per successful mutating operation (0→1→2→3→… for create/adopt/amend), and unchanged for
  no-ops, failures, previews and replays; plan application bumps it once for the whole plan.
- `reassigned` history entries carry `plan_id` at the entry level and a `changes` item
  `{field: "table_ids", from: [...], to: [...]}` — matches the plan's stated reading of the spec.
- Planning `planning_limit` (422) triggers beyond 6 tables / 4 pairs / 6 considered bookings, as
  the spec permits.

## Environment

- `wait-for-docker` OK; images rebuilt from the committed revision.
- Isolated harness mode broken on this daemon (documented above).
