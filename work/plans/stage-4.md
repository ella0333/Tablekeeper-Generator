# Stage 4 plan — Tablekeeper: seating changes and recurring amendments

Spec: `/work/dark-factory-wearedevs/tablekeeper/spec/stage-4.md` (stages 1–3 continue to apply).
Result repo: `/work/band-work/result`
Stage folder: `/work/band-work/result/stage-4/` — starts as a **copy of `stage-3/`** with no `.git` inside it; `stage-1/`, `stage-2/` and `stage-3/` stay unchanged.

## Delivery shape

- Copy `stage-3/` → `stage-4/` first (`cp -a stage-3 stage-4`; remove any nested `.git`). Do not edit earlier folders.
- `stage-4/Dockerfile` and `stage-4/RUN.md` still build and start with no manual steps and no run-time network.
- Extend the same service; keep all stage-1/2/3 API behaviour and the UI working.

## Work items

### R1 — Copy the folder; restaurant revision plumbing
Acceptance: `stage-4/` is a copy of `stage-3/`. The per-restaurant `revision` counter starts at 0 after reset and increments **once** for each successful new booking, real amendment, cancellation, policy publication and plan application. No-op writes, failures, previews and replays never increment it. It is exposed in the replan responses (and may stay in export state).
Prove: reset then drive each mutating operation and read the counter from `_test/export` or a replan response.

### R2 — `POST /restaurants/{id}/replans` (preview)
Acceptance: manager only (401/403/404 as in stage 3) and requires an `Idempotency-Key` (stage-1 replay rules). Body `{table_id, from, to}` with explicit offsets and `from < to`; an invalid interval is 422 `validation_failed`; an unknown table is 404. The proposed closure is the half-open interval `[from, to)`. Consider every confirmed booking at that restaurant overlapping the interval; all other bookings keep their assignments.
Planning must handle up to **6 tables, 4 declared pairs and 6 considered bookings**; larger inputs may return 422 `planning_limit`.
Each considered booking must retain its reference, owner, party size, start, end and accepted terms, and be assigned a single table or a declared pair with enough capacity under **its own accepted terms**, with no conflict against fixed bookings, other assignments, previously applied closures or the proposed closure. Diners' cancellation cutoffs do **not** block an operator repair, and no booking may disappear or be cancelled.
Choose a feasible plan minimizing, in order:
1. number of bookings whose table set changes;
2. total unused seats across considered bookings (capacity − party size);
3. the vector of option ranks in ascending reservation-reference order — singles ranked first in fixture order, then pairs in declared order, starting at 0.
Returns 201 `{plan_id, restaurant_revision, closure, assignments:[{reference, table_ids, changed}], moved_count, unused_seats}` with every considered booking in reference order. Preview stores only a plan: no closure, occupancy, reservation revision or history change. No feasible plan → 409 `no_feasible_plan`, changing nothing.
Prove: a fixture with a closure that forces moves; assert the chosen plan follows the three-level ordering (construct a case where each level matters) and that preview left state untouched.

### R3 — `POST /restaurants/{id}/replans/{plan_id}/apply`
Acceptance: manager only and requires an idempotency key. Body `{}`. Returns 201 `{plan_id, restaurant_revision, reservations:[...]}` with every considered booking in reference order. Unknown plan, or one from another restaurant, is 404. Any intervening restaurant revision invalidates the plan: 409 `stale_plan`, changing nothing. A plan already applied under a **different** key gives 409 `plan_already_applied`; a replay of the successful key returns the original response with 200, even after later changes. Application is atomic.
Application records the closure and all assignments together. Each moved booking increments its revision once and gains one `reassigned` history entry with a `table_ids` change and `plan_id`; its accepted terms and times stay identical. Unmoved bookings gain nothing. The restaurant revision increments once for the **whole plan**. Thereafter the closure excludes singles and pairs from availability and rejects creates/amendments with 409 `table_unavailable`; with `explain=true`, `no_overlap` is false for a closure just as for a conflicting booking.
Prove: preview → apply; assert moved bookings' revisions/history/`plan_id`, unchanged bookings untouched, closure blocks availability and new bookings, `stale_plan`/`plan_already_applied`/replay, and that a closure at another restaurant does not invalidate the plan.

### R4 — `POST /series/{series_id}/amend`
Acceptance: owner-only idempotent write; unknown or another owner's series 404; no token 401. Body `{expected_revision, from_index, local_time}`. `expected_revision` positive integer; `from_index` integer in 0..count−1; `local_time` exactly `HH:MM` in 00:00..23:59. Booleans are not valid integers. Invalid input → 422 `validation_failed`; a mismatched series revision → 409 `stale_revision` **before** any occurrence's cutoff or booking validation. Unknown fields ignored.
Consider indices at or after `from_index`, **excluding cancelled occurrences and those marked exception**. Change their clock time on their original scheduled local dates, retaining each reference, owner, party size and current table selection. A change with identical resulting fields is a no-op and retains its terms. Each real change checks its old accepted cutoff, then adopts the policy for its resulting start date, exactly like an individual PATCH.
Resulting occurrences must not conflict with unchanged occurrences, other bookings or applied closures. On failure, histories, idempotency records and all revisions stay unchanged; non-occupancy errors take precedence in occurrence-index order, otherwise an occupancy conflict returns `table_unavailable`.
On success return 201 with the current series response. Each changed occurrence gains one ordinary `changed` history entry and one reservation revision. The series and restaurant revisions each increase once for the whole operation **if anything changed**. Series amendments do not mark exceptions. An all-no-op or empty eligible set succeeds without changing revisions. A replay returns the original response with 200 even after further edits or cancellations. Concurrent amendments from the same expected revision may not both make a real change.
Prove: amend from an index across several occurrences; assert per-occurrence history/revision, series+restaurant revision +1, no-op/empty-set success with no counter change, replay, `stale_revision`, and a conflict that changes nothing.

### R5 — Seating repairs preserve series occurrences
Acceptance: when a plan moves series occurrences it preserves their exception flags, scheduled dates, identities and accepted terms; each affected series revision increases once per plan application if at least one member moved.
Prove: adopt a series, apply a plan that moves an occurrence, then read `GET /series/{id}` and the occurrence history.

### R6 — Cross-stage import
Acceptance: a stage-4 service accepts exports produced by the same team's stages 1–3. Replan and series-amend must work on imported series, including moved and cancelled occurrences. Earlier booking and series receipts, histories and retries remain valid.
Prove: import a stage-3 export, run a replan and a series amend on it, and re-check old receipts.

### R7 — UI continuity
Acceptance: no new screens required; existing availability, confirmation and lookup screens reflect an applied plan (the availability grid follows the closure); stage-2/3 testids and competing-client behaviour still pass.
Prove: re-run the shipped stage-1, stage-2 and stage-3 suites.

### R8 — Concurrency and atomicity
Acceptance: concurrent applications never leave partially moved bookings; two concurrent applies of one plan yield one 201 and the other the defined 409/replay; concurrent series amendments from one expected revision make at most one real change; no 5xx.
Prove: parallel bursts on apply and series amend.

## Cross-cutting (a quick test would miss)

- The three-level plan ordering is lexicographic: changing fewer bookings always beats using fewer seats; option ranks break ties in ascending reference order (singles first, then pairs).
- A plan's `restaurant_revision` must be checked at apply time; **any** intervening mutation (a new booking, amendment, cancel, policy publication or another plan) makes it `stale_plan`, but an unrelated restaurant's plan does not.
- Preview must be side-effect free — no closure, no occupancy, no revision, no history.
- Closure affects `available_table_ids` **and** `available_options` and must reject both creates and amendments with 409 `table_unavailable`; `explain` reports `no_overlap: false`.
- `reassigned` history entries carry a `table_ids` change and a `plan_id`; unchanged bookings get nothing.
- Series amend excludes cancelled and exception occurrences and never marks new exceptions; `from_index` is absolute (0..count−1), not relative to eligible ones.
- `stale_revision` on amend is checked before cutoff/booking validation for every occurrence.
- All-no-op / empty eligible set is a success, not a 409, and must not bump any revision.
- Concurrency: at most one real change per shared expected revision; no partial plan application.

## Definition of done

`RUN.md` reproduces a healthy service; R1–R8 acceptance criteria hold; the stage-1–3 suites plus the stage-4 suite pass; `work/plans/stage-4.md` committed; earlier stage folders unchanged.