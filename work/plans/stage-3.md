# Stage 3 plan — Tablekeeper: policies, history and recurring reservations

Spec: `/work/dark-factory-wearedevs/tablekeeper/spec/stage-3.md` (stages 1 and 2 continue to apply).
Result repo: `/work/band-work/result`
Stage folder: `/work/band-work/result/stage-3/` — starts as a **copy of `stage-2/`** with no `.git` inside it; `stage-1/` and `stage-2/` stay unchanged.

## Delivery shape

- Copy `stage-2/` → `stage-3/` first (`cp -a stage-2 stage-3`; remove any nested `.git`). Do not edit earlier folders.
- `stage-3/Dockerfile` and `stage-3/RUN.md` still build and start with no manual steps and no run-time network.
- Extend the same service; keep all stage-1/2 API behaviour and the UI working.

## Work items

### P1 — Copy the folder; policy model
Acceptance: `stage-3/` is a copy of `stage-2/`. Restaurants gain `manager_user_ids` (default `[]`). Introduce **policy 0** = the fixture's original rules (`slot_minutes`, `reservation_duration_minutes`, `cancellation_cutoff_minutes`, `opening_hours`, and capacities from the fixture tables). Track a per-restaurant policy list and a per-restaurant `revision` counter (starts 0 after reset). Track a per-reservation `revision` (1 at creation/seeding) and `accepted_terms`.
Prove: reset, then read a reservation and see `revision: 1` and policy-0 `accepted_terms`.

### P2 — `POST /restaurants/{id}/policies`
Acceptance: manager-only (unknown restaurant 404; authenticated non-manager 403 `forbidden`; no token 401). Requires an `Idempotency-Key` with stage-1 §7 replay rules. Body is a **complete** policy: `effective_from` (real `YYYY-MM-DD`), `slot_minutes` and `reservation_duration_minutes` integers 1..1440, `cancellation_cutoff_minutes` integer 0..10080, `opening_hours` (stage-1 rules, no duplicate weekdays), `capacities` naming **exactly** the restaurant's table ids with integer capacities 1..100. Booleans are not integers. Any invalid field → 422 `validation_failed` with **no version and no state change**. Unknown fields ignored. Returns 201 with the supplied policy plus `policy_version` (integer from 1, +1 per restaurant). Failed writes and replays allocate no version. Policies are immutable. Table ids, labels, timezone and declared combinations cannot be changed by a policy.
Prove: publish two policies; assert versions 1 then 2, a replay returns the original 201/200 body with no new version, and an invalid body changes nothing.

### P3 — `GET /restaurants/{id}/policies` and policy selection
Acceptance: public, returns `{"policies":[...]}` in publication order, omitting policy 0. `GET /restaurants/{id}` still returns the original fixture configuration. Availability and booking decisions use the **selected** policy, not the detail. Selection: for a booking's local start date choose the greatest `effective_from` not later than that date; ties choose the greatest `policy_version`. A new same-date policy supersedes for future decisions without changing accepted reservations. Effective dates may be in the past and publication never retroactively edits a booking.
Prove: publish a policy effective before/after a booking; assert availability duration/slot grid/opening hours follow the selected policy while the old booking is unchanged.

### P4 — Availability `explain=true`
Acceptance: `explain` optional; only accepted value is `true`; anything else (including `false`, `1`, empty) → 422 `validation_failed`. **Without it the response keeps stage 1's shape** (no explanation fields). With it, each slot carries `explain`: every table of the restaurant exactly once in fixture order; each entry `{table_id, policy_version, available, rules:[{rule:"capacity",holds:…},{rule:"no_overlap",holds:…}]}` with both rules always present; `available` true iff both hold; the `table_id`s whose `available` is true are exactly `available_table_ids` in the same order. Closed day still `"slots": []`; a slot with no available table still appears with a full `explain`.
Prove: compare `explain` table ids/order/available flags against `available_table_ids`; assert the shape is absent without `explain` and that `false`/`1`/empty are 422.

### P5 — Reservation history and decision
Acceptance: `GET /reservations/{reference}/history` — owner only; anyone else, signed in or not, gets 404 `not_found` (this overrides stage-1's general 401 for this endpoint). Returns `{"reference":..., "entries":[...]}` oldest first. `seq` starts at 1 and +1; entries in `seq` order (total order even in the same second). `created` names all three fields `table_id`, `starts_at_local`, `party_size` with `"from": null`; `changed` names only fields that actually changed in the order `table_id`, `starts_at_local`, `party_size`; a no-op PATCH succeeds and records **no entry**; `cancelled` carries empty `changes` and nothing follows. Replaying an idempotent `POST /reservations` records nothing. Each entry also carries the reservation's resulting `revision` and complete `accepted_terms`; old entries never acquire newer terms. `GET /reservations/{reference}/decision` returns `{"reference", "revision", "accepted_terms"}` for the current booking, including after cancellation, with the same owner-only 404 rule (404 even without auth).
Prove: create → patch (real and no-op) → cancel; assert seq/events/changes and that history/decision 404 for another user and for no token.

### P6 — Revisions, accepted terms and amendment semantics
Acceptance: every reservation response gains `revision` and `accepted_terms` (a snapshot of the entire selected policy **excluding** `effective_from`). Seeded bookings start at revision 1 under policy 0. Responses to old idempotency keys remain the original response including original revision/terms. A policy publication does not change existing bookings, end times or history. Cancel checks the **accepted** cutoff against the current start and increments revision once (repeated cancel does not). A real diner amendment checks the old accepted cutoff first, then validates **all** resulting fields against the policy applicable to the **resulting** start date; it atomically replaces accepted terms and end time and increments revision once. A no-op amendment retains terms/end/revision and records no history (but still requires a confirmed editable booking). Failed amendments change nothing. `PATCH` optionally accepts `expected_revision`: a positive integer differing from the current revision → 409 `stale_revision` before cutoff/validation; invalid type/range → 422; omission keeps stage-1 semantics. Two concurrent amendments using one revision: at most one real change succeeds.
Prove: publish a policy, amend across it, assert terms/end/revision; assert `stale_revision` and the concurrency rule.

### P7 — `POST /series` (recurring adoption)
Acceptance: idempotency key required; no token 401. Body `{anchor_reference, count, interval_weeks}`. Anchor must belong to the caller, be confirmed and satisfy its accepted cutoff; unknown/other owner 404 `not_found`; cancelled 409 `reservation_cancelled`; already adopted 409 `already_in_series`. `count` integer 2..12 including the anchor; `interval_weeks` integer 1..4; invalid values (including booleans) 422 `validation_failed`. Occurrence zero is the anchor unchanged (reference, identity, revision, terms, history, timestamps, original idempotent response). Occurrence i starts on the anchor's local date + i×interval_weeks×7 days at the same local clock time. Each occurrence independently selects its date's policy (duration and capacity) and obeys opening, DST and occupancy rules. A nonexistent local time rejects the entire adoption with `invalid_local_time`; repeated times use the stage-1 first-occurrence rule. Generated occurrences use the anchor's party size and table selection. **No partial** series, reservations, histories, counters or idempotency claim survives failure; the first failing occurrence in index order determines the ordinary booking error. Return 201 `{series_id, revision, interval_weeks, occurrences:[{index, reference, exception:false, reservation}]}` — all `count` occurrences in index order, each with a distinct ordinary reference; references and indices never change. Occurrences appear in ordinary lists, occupy tables and have ordinary histories. `GET /series/{series_id}` returns the shape with current states; owner-only, else 404. Adoption increments the restaurant revision once. Replays return the original series response and change no counter. Unknown fields ignored.
Prove: adopt an 8-occurrence weekly series across a DST boundary; assert references, occupancy, per-occurrence policy, replay, and a deliberate failure that leaves nothing behind.

### P8 — Series mutations (individual PATCH/cancel side effects)
Acceptance: a real individual PATCH permanently marks that occurrence `exception: true` and increments the series revision once; a no-op or failure changes neither. Cancellation increments the series revision once and retains the cancelled occurrence but does **not** mark an exception; repeated cancel does nothing. Cancelling the anchor does not cancel siblings. Ordinary cutoff and revision checks still apply.
Prove: patch one occurrence (exception true, series revision +1), cancel another (series revision +1, no exception), no-op (no change).

### P9 — Combined-table history
Acceptance: accepted terms apply to combinations with capacity = sum of the **selected policy's** capacities. History keeps stage-3 fields for single-to-single operations. Creating a pair replaces the `table_id` change with `table_ids` (null → the pair). A change involving a pair uses `table_ids` (complete before/after lists) instead of `table_id`. Table-set order is the declared combination order; a reversed input pair names the same set and is not an amendment on its own.
Prove: create a pair, patch it, and read the history entries.

### P10 — Collective moves under policies and agreements
Acceptance: each real change in `POST /reservation-moves` uses individual PATCH semantics — check the old accepted cutoff, then adopt the resulting date's policy. Per-move `expected_revision` is optional and follows PATCH validation/stale-revision rules. A no-op retains its terms and history. All resulting bookings must satisfy amendment and occupancy rules; failure leaves every booking unchanged. Every changed booking gains one revision and a changed history entry; the restaurant revision increases once for the whole batch; each affected series revision increases once and each changed series occurrence becomes a permanent diner exception. A failed batch or replay changes no revisions, histories or exception flags.
Prove: a batch that changes two bookings (assert revisions/history/restaurant revision), a failing batch (assert nothing changed), and a replay.

### P11 — Cross-stage import
Acceptance: a stage-3 service accepts exports produced by the same team's stage-1 **or** stage-2 service. Adoption works on reservations imported this way. Existing confirmation links, sessions and original booking retries remain valid.
Prove: import a stage-1 export and a stage-2 export; adopt a series on an imported reservation; re-check tokens and retries.

### P12 — UI continuity
Acceptance: no new screens required; the availability grid continues to follow the stage-2 rules; existing availability/confirmation/lookup screens keep working; `explain` is an API-only addition and does not break the grid. Existing stage-2 testids and competing-client behaviour still pass.
Prove: re-run the shipped stage-2 suite.

## Cross-cutting (a quick test would miss)

- Policy selection ties resolve by greatest `policy_version`; publication order ≠ effective order.
- A policy publication must not touch existing bookings' end times or history, nor their `accepted_terms`.
- Amendment validates against the policy for the **resulting** start date, not the old one; cutoff uses the **old accepted** terms.
- `seq` is a total order; same-second writes must not collide.
- A no-op PATCH records no entry, yet a cancelled or failed one is distinguished (cancelled → 409 `reservation_cancelled`, failed → unchanged).
- Series failure atomicity: no orphan reservations, counters or idempotency claims.
- Fall-back first-occurrence and spring-forward nonexistence apply to every generated occurrence.
- `explain` must not appear at all without the flag; `explain=false` is 422, not "off".
- Restaurant revision increments exactly once per successful new booking/amendment/cancel/policy publication/adoption.
- History/decision 404 without auth even though most endpoints are 401 without auth.

## Definition of done

`RUN.md` reproduces a healthy service; P1–P12 acceptance criteria hold; the stage-1 and stage-2 suites plus the stage-3 suite pass; `work/plans/stage-3.md` committed; `stage-1/` and `stage-2/` unchanged.