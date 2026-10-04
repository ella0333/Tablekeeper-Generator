# Stage 1 tester report — round 1 (FAIL)

- Revision tested: `fc93d2f818915803ef6584a9f6517c6cf9db1abe` (short `fc93d2f`, "Stage 1: Tablekeeper reservations API service")
- Stage folder: `/work/band-work/result/stage-1/`
- Service under test: image `tk-s1-tester` built from the committed `stage-1/Dockerfile`; build reproduced the builder's image `sha256:50d552f91d31d11b556aa883cce6e3fab113ab0a0e0627aa470818a6d7cfa4b9`; healthy at `http://172.31.250.3:18090` (`GET /health -> 200 {"status":"ok"}`).
- Verdict: **FAIL** — 5 confirmed defects (4 found by the shipped stage-1 checker, 1 confirmed independently and by a tester agent).

## How it was checked

- Built the committed revision and started it on the Docker daemon (`DOCKER_HOST=tcp://172.31.250.3:2375`), published on the daemon host.
- Ran the event checker's shipped `tablekeeper` stage-1 suite (base-url mode; see Environment note) against a dedicated container: **116 passed / 4 failed**.
- Ran three role-based tester agents against the running service: anonymous diner / public+auth (34/34 PASS), authenticated diner / lifecycle+idempotency+moves (64/65, 1 FAIL), adversarial / malformed+DST+concurrency+export/import (53/53 PASS). The agent that missed nothing beyond its brief is noted; the four checker failures were outside the agents' briefs and were found by the official suite plus a direct reproduction.
- Reproduced every failure below by hand against the running container.

## Failure F1 — `POST /_test/reset` accepts a fixture ID longer than 64 characters

- Requirement (spec §3.4): "IDs are opaque strings of at most 64 characters. Their format is yours. **This limit also applies to IDs supplied in reset fixtures.**" (spec §5: a field of the correct type with a value exceeding a stated maximum or length is `422 validation_failed`).
- Steps to reproduce:
  1. `POST /_test/reset` with body `{"users":[{"id":"uuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuu","email":"ada@example.com","password":"correct horse","display_name":"Ada"}],"restaurants":[],"reservations":[]}` (the `id` is 65 characters).
- Expected: `422` `{"error":{"code":"validation_failed",...}}`.
- Actual: `204 No Content` — the fixture is accepted and the over-long id stored.
- File/line: `stage-1/app.py`, `apply_fixture()` — user records are built at lines 370–386 with no length check on `u.get("id")`; restaurant/table ids likewise (`build_restaurant`, lines 300–324). No ID-length validation exists anywhere.
- Official check: `tablekeeper/test/stage_1/test_seeded_state.py::test_reset_rejects_ids_longer_than_64_characters`.

## Failure F2 — `POST /_test/reset` accepts a seeded reservation `reference` of length 1

- Requirement (spec §8): "`reference` is 6 to 12 characters of `A-Z0-9`, unique across all reservations"; (spec §4) seeded reservations use "the same fields as a `POST /reservations` body plus `id`, `reference` and `user_id`"; (spec §5) an invalid format gives `422 validation_failed`.
- Steps to reproduce:
  1. `POST /_test/reset` with a fixture whose `reservations[0].reference` is `"x"` (1 char).
- Expected: `422` `validation_failed`.
- Actual: `204 No Content` — the invalid reference is stored.
- File/line: `stage-1/app.py`, `apply_fixture()` line 439 (`new_reservations[rec["reference"]] = rec`) stores any supplied reference; `new_reference()` (line 172) only generates valid refs for API-created bookings, so seeded references are never format/length-checked.
- Official check: `test_seeded_state.py::test_reset_rejects_invalid_reservation_references[x]`.

## Failure F3 — `POST /_test/reset` accepts a lowercase seeded `reference` (`"lower01"`)

- Same requirement and file/line as F2.
- Steps: `POST /_test/reset` with `reservations[0].reference = "lower01"`.
- Expected: `422` `validation_failed` (reference must be `A-Z0-9`).
- Actual: `204 No Content`.
- Official check: `test_seeded_state.py::test_reset_rejects_invalid_reservation_references[lower01]`.

## Failure F4 — `POST /_test/reset` accepts a seeded `reference` longer than 12 characters (`"TOO-LONG-WITH-DASH"`)

- Same requirement and file/line as F2.
- Steps: `POST /_test/reset` with `reservations[0].reference = "TOO-LONG-WITH-DASH"`.
- Expected: `422` `validation_failed` (reference is 6–12 chars of `A-Z0-9`).
- Actual: `204 No Content`.
- Official check: `test_seeded_state.py::test_reset_rejects_invalid_reservation_references[TOO-LONG-WITH-DASH]`.

## Failure F5 — an idempotency key used on one path is rejected on a different path (key is not path-scoped)

- Requirement (spec §7): "A replay means the same user sending the **same method, the same path and the same body**. **The same key with the same body on a different path is a different request, not a replay, and must succeed normally.**" (Plan W5: "same key on a different path is a new request".)
- Steps to reproduce (future date so the moves body is valid and cutoff is not in play; `r_anker`, Friday 2026-12-03 19:00, `t_1` cap 2, `t_2` cap 4):
  1. Reset the fixture; log in as `ada`.
  2. `POST /reservations`, header `Idempotency-Key: X1`, body `{"restaurant_id":"r_anker","table_id":"t_1","starts_at_local":"2026-12-03T19:00","party_size":2}` → `201`, reference e.g. `BNSXBJ`.
  3. `POST /reservation-moves`, header `Idempotency-Key: X1`, body `{"moves":[{"reference":"BNSXBJ","table_id":"t_2"}]}`.
- Expected: the moves request is a different path, so it is not a replay and must be processed normally → `201 {"reservations":[...]}`.
- Actual: `409` `{"error":{"code":"idempotency_key_reuse","message":"key already used with a different body"}}`.
- Control: the identical moves body with a fresh key (`X3`/`X9`) returns `201` and commits the move, proving the body is valid and that keys are keyed by `(user, key)` only.
- File/line: `stage-1/app.py`, `check_replay()` lines 577–585 — the per-user store `STORE.idem[user_id]` is a flat map keyed by the idempotency key alone (`store_result()`, lines 588–595), and `check_replay` raises `409 idempotency_key_reuse` whenever a stored record's path differs. There is no `(path, key)` namespace, so a key cannot be reused on `/reservation-moves` after `/reservations` (or vice versa).
- Independently confirmed by tester agent B (its only failing check out of 65) and by direct reproduction.

## Environment note — isolated-mode checker could not run on this daemon

The task asks to also run the stage in the checker's isolated mode. `harness run ... --mode isolated` was attempted twice (outputs kept in `/work/band-work/checks/stage-1-round-2/` and `stage-1-round-2b/`). Both failed before any test ran with `service did not become healthy at http://df-svc-<id>:8080 ... httpx.ConnectError: [Errno 101] Network is unreachable`. A direct probe reproduced the cause: on this Docker daemon container-to-container traffic on an `--internal` network is not routable (`docker run --rm --network <internal> python:3.12-slim ...` → `OSError: [Errno 101] Network is unreachable`), which is exactly the transport isolated mode depends on. This is an environment/infrastructure limitation, not a defect in the submission; it is recorded so the result is not mistaken for a service failure. As a substitute, the shipped stage-1 suite was executed in base-url mode against the running container (evidence below).

## Evidence

- `/work/band-work/checks/stage-1-official-host2/stage-1.log` and `stage-1.counts.json` — shipped stage-1 suite: `{"collected":120,"passed":116,"failed":4,...}`; failures are F1–F4 above.
- `/work/band-work/checks/stage-1-round-2/`, `/work/band-work/checks/stage-1-round-2b/` — isolated-mode attempts (environment failure).
- Tester-agent reports: public+auth 34/34 PASS; lifecycle+idempotency+moves 64/65 (F5); adversarial/DST/concurrency/export-import 53/53 PASS.
- Direct reproduction of F1–F5 against `http://172.31.250.3:18090` / `:18091` (raw requests/statuses in this report).

## What passed (for the planner's context)

Health and reset basics; public catalogue and availability incl. slot grid, capacity filtering, fixture order, closed day, and strict plain-decimal integer query params; DST availability and booking across all four Berlin/New York transitions incl. fall-back first-occurrence and absolute-duration `ends_at`; auth (signup/login/duplicate/short-password/bad-email/401s, multiple non-expiring tokens); create shape/errors; idempotency same-path replay, reuse-with-different-body precedence, reuse-after-4xx, per-user scoping; read isolation and ordering; cancel incl. cutoff and double-cancel; PATCH incl. atomic no-change on failure and cutoff; atomic moves incl. swap, replay-after-amendment, and all-or-nothing rollback; concurrency (50 same-key → one 201 + 49×200 same body; 50 different keys → one 201 + 49×409; no 5xx); export/import round-trip preserving tokens, hashed-password login, references and idempotent replays; 64-char opaque IDs accepted.