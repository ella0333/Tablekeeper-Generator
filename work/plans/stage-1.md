# Stage 1 plan — Tablekeeper reservations API

Spec: `/work/dark-factory-wearedevs/tablekeeper/spec/stage-1.md`
Result repo: `/work/band-work/result`
Stage folder: `/work/band-work/result/stage-1/`

## Delivery shape

- `stage-1/` holds the whole service: source, `Dockerfile`, `RUN.md`.
- `Dockerfile` builds an image that starts the service with **no manual steps** and **no
  run-time network**. Build-time package installs are allowed; everything else (fonts, data,
  seed code) must be baked in.
- `RUN.md` shows one build command and one run command, e.g.
  `docker build -t tablekeeper-s1 stage-1` then
  `docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-s1`, plus a `curl` health check and a
  sample `POST /_test/reset` body.
- The container must listen on `0.0.0.0:$PORT` (default 8080), answer `/health` with 200 within
  60 s, and stay inside 2 vCPU / 2 GiB / 50 in-flight requests / 5 s per request.

## Recommended stack (builder's choice, must meet the contract)

Python 3.12 + FastAPI + uvicorn, single process, with:
- `zoneinfo` (stdlib) for IANA time zones and DST;
- `hashlib.scrypt` (stdlib) for password hashing — avoids a run-time dependency;
- one global write lock (or an asyncio lock) around every mutating operation so
  check-then-act is atomic under 50 concurrent requests;
- in-memory state only (disk is ephemeral, state need not survive restart).
Pin every dependency in the image; no downloads at run time.

## Work items

### W1 — Container skeleton and health
Acceptance: `docker build` succeeds; container starts with `-e PORT=<port>` and a mapping;
`GET /health` returns `200 {"status":"ok"}`. All bodies are `application/json; charset=utf-8`.
Prove: `curl -si localhost:8080/health`.

### W2 — Reset and seed (`POST /_test/reset`)
Acceptance: body is a fixture (§4) with `users`, `restaurants` (each with `timezone`,
`slot_minutes`, `reservation_duration_minutes`, `cancellation_cutoff_minutes`, `opening_hours`,
`tables`) and `reservations`; replaces *all* state; returns `204 No Content`; repeated resets
work; unauthenticated. Seeded users log in immediately with the given password; seeded
reservations are `confirmed` with the create-response shape plus `id`, `reference`, `user_id`.
Prove: POST a fixture, then `GET /restaurants` and `GET /reservations/{reference}`.

### W3 — Auth (`POST /auth/signup`, `POST /auth/login`)
Acceptance: signup 201 `{user_id, display_name, token}`; login 200 same shape; duplicate email
409 `email_taken`; password <8 chars 422 `validation_failed`; email not `local@domain` 422
`validation_failed`; bad login 401 `unauthenticated`. Passwords hashed (never plaintext). Tokens
never expire; multiple valid tokens per account. Bearer tokens required on every endpoint except
`/health`, `/_test/reset`, auth routes, and the three public endpoints of §8.
Prove: signup → login → call a protected endpoint with and without the token.

### W4 — Public catalogue and availability (`GET /restaurants`, `GET /restaurants/{id}`, `GET /availability`)
Acceptance: list returns `{id,name,timezone}`; detail returns the fixture shape (`slot_minutes`,
`reservation_duration_minutes`, `cancellation_cutoff_minutes`, `opening_hours`, `tables`), 404
unknown; availability takes required `restaurant_id`, `date`, `party_size` (missing → 422
`validation_failed`), returns slots every `slot_minutes` from `opens` while
`slot + reservation_duration_minutes <= closes`, each with `starts_at_local`, `starts_at`
(RFC 3339 with offset) and `available_table_ids` (fixture order, `capacity >= party_size`, no
overlapping confirmed reservation). Closed day → `"slots": []`; empty slot still appears with
`[]`. Unknown query params ignored.
Prove: seeded fixture → curl availability and compare slot set/labels by hand.

### W5 — Create reservation (`POST /reservations`) with idempotency
Acceptance: requires bearer token and `Idempotency-Key` (absent/empty → 400
`missing_idempotency_key`; length 1..255 else 422). Valid create 201 with `reservation_id`,
6–12 char `A-Z0-9` unique `reference`, `restaurant_id`, `table_id`, `party_size`, `status`,
`starts_at_local`, `starts_at`, `ends_at` (absolute duration), `created_at` (RFC 3339 +00:00).
Errors: overlapping table 409 `table_unavailable`; off-grid 422 `not_on_slot_grid`; outside
opening hours / ends after close 422 `outside_opening_hours`; party > capacity 422
`party_exceeds_capacity`; party <1 or non-integer 422 `validation_failed`; nonexistent DST time
422 `invalid_local_time`; unknown restaurant/table or foreign table 404 `not_found`.
Idempotency (§7): first use 201; replay same key+method+path+body → 200 with identical JSON;
same key different body → 409 `idempotency_key_reuse` (checked *before* field validation);
key after a 4xx failure is reusable; key scoped per user; same key on a different path is a new
request. Concurrent identical requests with one unused key → exactly one 201, rest 200 same body,
one effect.
Prove: create, replay, reuse-with-different-body, and a parallel `xargs -P` burst.

### W6 — Read / cancel / amend (`GET /reservations`, `GET /reservations/{reference}`, `POST /reservations/{reference}/cancel`, `PATCH /reservations/{reference}`)
Acceptance: list is the caller's reservations, `starts_at` descending, confirmed and cancelled,
same shape as create. Detail is owner-only, otherwise 404 (no leak). Cancel returns 200 with
`status:"cancelled"`, frees the table immediately (availability offers it again), idempotent
(double cancel 200, not an error), 409 `cutoff_passed` within
`cancellation_cutoff_minutes` of start or later, 404 if not the caller's. PATCH changes any subset
of `table_id`, `starts_at_local`, `party_size`; validation identical to create; cutoff measured
against the *current* start (409 `cutoff_passed`); cancelled booking 409 `reservation_cancelled`;
success releases the old slot and reserves the new one atomically; failure changes nothing;
`reference` and `reservation_id` survive.
Prove: create → patch → cancel → re-availability, plus a force-failed PATCH that leaves state
intact.

### W7 — Errors and conventions
Acceptance: every 4xx/5xx body is `{"error":{"code":...,"message":...}}` with the specified code;
no 5xx under load; unknown body fields ignored, never an error; integer query params must be
plain decimal (`1e9`, `4.0`, `+4` → 422 `validation_failed`); wrong JSON field type → 400
`malformed_request`; IDs are opaque strings ≤64 chars (including in fixtures).
Prove: table-driven curl calls asserting status + `error.code`.

### W8 — Time and DST (§9)
Acceptance: `Europe/Berlin` spring-forward 2026-03-29 02:00→03:00 and fall-back 2026-10-25
03:00→02:00; `America/New_York` 2026-03-08 02:00→03:00 and 2026-11-01 02:00→01:00. Nonexistent
local times never appear in availability and booking one is 422 `invalid_local_time`. Repeated
times resolve to the **first** occurrence; the slot appears once and the second is not bookable.
`reservation_duration_minutes` is absolute, so 01:30 + 90 min on a fall-back night ends at local
02:00. Offsets follow IANA rules.
Prove: scripted bookings across all four transitions with the offsets asserted.

### W9 — Export / import (§10)
Acceptance: `GET /_test/export` → 200 `{track:"tablekeeper", format_version:1, state:{...}}`,
atomic read-only snapshot, unauthenticated. `POST /_test/import` takes that object, atomically
replaces state, returns 204; accepts an unchanged export; replacement not merge; repeated import
restores without duplication. Invalid JSON per §5; missing fields / wrong track or version /
invalid state → 422 `validation_failed` with no change. Preserves accounts and hashed-password
login, bearer tokens, fixture config, reservations, references, all completed idempotent request
body+response pairs; identities/statuses/timestamps not regenerated. Failed keys stay reusable.
Reset clears imported state. 10 s timeout.
Prove: export → reset to a different fixture → import → assert reservations, tokens and an
idempotent replay still work.

### W10 — Atomic reservation moves (`POST /reservation-moves`)
Acceptance: auth + idempotency key required. Body `{"moves":[{reference,table_id,starts_at_local?,party_size?}...]}`
with 1..8 distinct string references; bad shape / duplicates → 422 `validation_failed`. All
bookings owned by caller and same restaurant; unknown/foreign → 404 `not_found`; different
restaurants → 422 `validation_failed`; no token 401. Per-move fields optional (omitted retain
current), unknown ignored; identity/owner/created_at unchanged; cancelled → 409
`reservation_cancelled`; each booking's own cutoff applies; non-occupancy errors use ordinary
amendment codes in input order, cutoff before other changes for a booking; overlap among
resulting bookings or with unlisted bookings → 409 `table_unavailable`. All-or-nothing (occupancy,
records, retry keys). Success 201 `{reservations:[...]}` in input order including unchanged;
replays 200 with original body even after later changes; no-op moves retain values.
Prove: two-booking move that commits, a move that fails on overlap (assert both unchanged), and
a replay.

### W11 — Concurrency and idempotent-write atomicity
Acceptance: every mutating path holds one lock so check-then-act cannot interleave; exactly one
201 for concurrent identical idempotency keys; concurrent creates for the same table at the same
slot yield one 201 and the rest 409 `table_unavailable` (never two confirmations and never 5xx).
Prove: parallel bursts with `xargs -P 50` asserting the counts.

## Cross-cutting (a quick test would miss)

- Half-open occupancy `[start, start+duration)`: a 90-minute 19:00 booking must **not** overlap
  one starting 20:30.
- Retries and rejections must never create duplicate or partial rows or partial idempotency
  records.
- Cutoff comparisons need a clock; use a real "now" and make cutoff math exact at the boundary.
- Fall-back first-occurrence and absolute-duration arithmetic are the two easiest DST bugs.
- `created_at` must carry an explicit `+00:00` offset.
- Seeded fixtures may use any calendar date; a past start must **not** be rejected on its own.
- Reference uniqueness spans seeded and created reservations.

## Definition of done

`RUN.md` commands reproduce a healthy service from a clean checkout; the WP1–W11 acceptance
criteria hold; no 5xx under a 50-way concurrent burst; `work/plans/stage-1.md` committed; the
`stage-1/` folder is complete and self-contained.
