# Stage 3 fix plan 1

Report: `work/reports/stage-3-round-1.md` (revision tested `c6b9605`).
Scope: only the one confirmed failure. Everything else the tester passed stays as it is.
Target folder: `/work/band-work/result/stage-3/` (file `app.py`).

## F1 — `POST /_test/reset` must clear recurring-series state

- Requirement (stage-1 §3.3, still in force): "Replace all service state with the fixture in
  the request body … When reset returns 204, subsequent requests must see only that fixture."
  A series created by `POST /series` is service state, so after a reset it must no longer exist
  and `GET /series/{series_id}` for it must return 404 `not_found`.
- Where: `app.py` `apply_fixture()` — the assignment block at lines 742–749 rebuilds and
  assigns `STORE.users`, `STORE.email_index`, `STORE.tokens`, `STORE.restaurants`,
  `STORE.reservations`, `STORE.idem`, `STORE.user_seq`, `STORE.res_seq`, but never assigns
  `STORE.series` or `STORE.series_seq`. `Store.clear()` (lines 150–160) and `import_state()`
  (lines 895–910) both reset them, so only the reset path was missed.
- Fix: add `STORE.series = {}` and `STORE.series_seq = 0` to that assignment block, so a reset
  drops every series exactly as `clear()` does. The per-restaurant `revision` counter lives on
  the freshly built restaurant records, so rebuilding them already resets it; confirm no other
  series- or agreement-related state survives.
- Correct looks like: reset → create a series → reset again → `GET /series/{previous_id}`
  returns `404 not_found` with the standard error body, while a token issued after the reset
  still works for the current fixture's reservations. A never-created series id still 404s.
  Repeated resets still work.

## Regression guard

- Re-run the shipped stage-1, stage-2 and stage-3 suites (expected 120/120, 25/25, 7/7) and the
  tester's deep probe (44/44).
- Re-confirm export/import still carries series state across a `/_test/export` → `/_test/import`
  round-trip (import must keep restoring series, unlike reset).

## Definition of done

F1 fixed with no regression; `stage-3/` still builds and starts from `RUN.md` with no manual
steps; the tester's round-2 run is green.