# Stage 1 fix plan 1

Report: `work/reports/stage-1-round-1.md` (revision tested `fc93d2f`).
Scope: only the 5 confirmed failures. Everything else the tester passed stays as it is.
Target folder: `/work/band-work/result/stage-1/` (file `app.py`).

## F1 — reset must reject fixture IDs longer than 64 characters

- Requirement: §3.4 "IDs are opaque strings of at most 64 characters … This limit also applies
  to IDs supplied in reset fixtures." A correct-type value exceeding a stated maximum is 422
  `validation_failed` (§5).
- Where: `app.py` `apply_fixture()` (user records ~lines 370–386) and `build_restaurant()`
  (~lines 300–324) store `id` with no length check; no ID validation exists anywhere.
- Fix: before building any record, validate every id the fixture supplies — `users[].id`,
  `restaurants[].id`, `restaurants[].tables[].id`, and each seeded reservation's
  `id`/`reservation_id`. An id that is not a string, or is longer than 64 characters, raises
  422 `validation_failed`. Do the checks while building the *new* state dicts and raise before
  the swap at the end of `apply_fixture`, so a rejected fixture leaves the previous state
  intact. IDs exactly 64 characters must still be accepted (the tester confirmed 64-char ids
  pass; do not break that).
- Correct looks like: `POST /_test/reset` with a 65-character user/restaurant/table id →
  `422 {"error":{"code":"validation_failed",...}}` and no state change; 64-character id → 204.

## F2/F3/F4 — reset must reject invalid seeded reservation `reference` values

- Requirement: §8 "`reference` is 6 to 12 characters of `A-Z0-9`, unique across all
  reservations"; §4 seeded reservations carry that same field; invalid format → 422
  `validation_failed` (§5).
- Where: `app.py` `apply_fixture()` line 439 stores `new_reservations[rec["reference"]] = rec`
  with no check; `new_reference()` (line 172) only guards API-created bookings.
- Failing cases: `"x"` (too short), `"lower01"` (lowercase not in `A-Z0-9`),
  `"TOO-LONG-WITH-DASH"` (too long / non-`A-Z0-9`).
- Fix: when a seeded reservation supplies `reference`, require it to match `^[A-Z0-9]{6,12}$`
  and to be unique within the fixture; otherwise 422 `validation_failed`. If `reference` is
  absent, keep generating a valid unique one (current behaviour). Validate before the state
  swap so a bad fixture changes nothing. A reference of exactly 6 or 12 `A-Z0-9` chars stays
  valid.
- Correct looks like: each of the three bad references → 422 `validation_failed`, no state
  change; a valid 6–12 char `A-Z0-9` reference → 204 and is then readable.

## F5 — an idempotency key must be scoped by path as well as by user

- Requirement: §7 "A replay means the same user sending the **same method, the same path and
  the same body**. The same key with the same body on a different path is a different request,
  not a replay, and must succeed normally."
- Where: `app.py` `check_replay()` (lines 577–585) and `store_result()` (lines 588–595) key the
  per-user map `STORE.idem[user_id]` by the idempotency key alone. A key used on
  `POST /reservations` is therefore found again on `POST /reservation-moves` and wrongly
  returns 409 `idempotency_key_reuse`.
- Fix: namespace each stored record by `(method, path, key)` inside the user map — e.g. store
  under a composite string key built from method, path and key. `check_replay` must look up
  that composite key, so a key used on one path is invisible on another and is processed as a
  first use (201). Reuse-with-a-different-body on the **same** path must still return 409
  `idempotency_key_reuse`, replay of the same method+path+body must still return the original
  response with 200, and the store must still be per user. Call sites are lines 792/827
  (`/reservations`) and 916/983 (`/reservation-moves`); update them only if the signature
  changes.
- Keep export/import working: the `idem` map is serialised and re-imported as opaque JSON
  (`~lines 544–548`), so a changed internal key shape must round-trip unchanged and preserve
  successful receipts.
- Correct looks like: create with key `X1` on `/reservations` → 201; then
  `POST /reservation-moves` with the same key `X1` → 201 (not 409); replaying that exact moves
  request → 200 with the original body.

## Regression guard

- Re-run the shipped stage-1 suite and confirm 120/120 (previously 116 passed / 4 failed).
- Re-confirm the passes listed in the report are untouched: same-path replay, reuse-different-
  body precedence, reuse-after-4xx, per-user scoping, 50-way concurrency (one 201), export/
  import round-trip, DST transitions.

## Definition of done

F1–F5 fixed, no regression in the previously passing checks, `stage-1/` still builds and starts
from `RUN.md` with no manual steps, and the tester's round-2 run is green.