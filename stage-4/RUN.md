# Tablekeeper — Stage 4: run it

A containerized HTTP service (JSON API + browser screens). Pure Python standard
library; no runtime network, no manual setup.

## Build

```sh
docker build -t tablekeeper-s4 stage-4
```

## Run

```sh
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-s4
```

The service listens on `0.0.0.0:$PORT` (default `8080`). Open
<http://localhost:8080/> for the booking UI.

## Health check

```sh
curl -s http://localhost:8080/health
# {"status": "ok"}
```

## Seed a fixture

`POST /_test/reset` replaces all state with the posted fixture and returns
`204 No Content`. This sample adds a manager and a declared combinable pair
`[t_1, t_2]` (capacity 2 + 4 = 6):

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/_test/reset \
  -H 'Content-Type: application/json' \
  -d '{
    "users": [
      { "id": "u_ada", "email": "ada@example.com",
        "password": "correct horse", "display_name": "Ada" }
    ],
    "restaurants": [
      { "id": "r_anker", "name": "Zum Anker", "timezone": "Europe/Berlin",
        "slot_minutes": 30, "reservation_duration_minutes": 90,
        "cancellation_cutoff_minutes": 120,
        "opening_hours": [
          { "weekday": "mon", "opens": "18:00", "closes": "23:00" },
          { "weekday": "tue", "opens": "18:00", "closes": "23:00" },
          { "weekday": "wed", "opens": "18:00", "closes": "23:00" },
          { "weekday": "thu", "opens": "18:00", "closes": "23:00" },
          { "weekday": "fri", "opens": "18:00", "closes": "23:30" },
          { "weekday": "sat", "opens": "18:00", "closes": "23:30" },
          { "weekday": "sun", "opens": "18:00", "closes": "23:00" }
        ],
        "tables": [
          { "id": "t_1", "label": "1", "capacity": 2 },
          { "id": "t_2", "label": "2", "capacity": 4 },
          { "id": "t_3", "label": "3", "capacity": 6 }
        ],
        "combinable": [ ["t_1", "t_2"] ],
        "manager_user_ids": [ "u_ada" ]
      }
    ],
    "reservations": []
  }'
# 204
```

## Endpoints

Public: `GET /health`, `POST /_test/reset`, `GET /_test/export`,
`POST /_test/import`, `POST /auth/signup`, `POST /auth/login`,
`GET /restaurants`, `GET /restaurants/{id}`, `GET /restaurants/{id}/policies`,
`GET /availability`.

Bearer-token protected: `POST /reservations`, `GET /reservations`,
`GET /reservations/{reference}`, `POST /reservations/{reference}/cancel`,
`PATCH /reservations/{reference}`, `POST /reservation-moves`, `POST /series`,
`POST /series/{series_id}/amend`.

Owner-only, 404 for anyone else (even without a token):
`GET /reservations/{reference}/history`, `GET /reservations/{reference}/decision`,
`GET /series/{series_id}`.

Manager-only (401 without a token, 404 for an unknown restaurant, 403
`forbidden` for a non-manager): `POST /restaurants/{id}/policies`,
`POST /restaurants/{id}/replans`, `POST /restaurants/{id}/replans/{plan_id}/apply`.

### Availability explanations

`GET /availability?restaurant_id=r_anker&date=2026-09-24&party_size=4&explain=true`

`explain` is optional and accepts only `true`. Without it the response is
unchanged. With it every slot gains `explain`: every table of the restaurant once,
in fixture order, each `{table_id, policy_version, available, rules}` with both
`capacity` and `no_overlap` rules always present. `available` is true exactly when
both rules hold, and the tables whose `available` is true are exactly
`available_table_ids` in the same order. A table closed by an applied plan reports
`no_overlap: false`, as for a conflicting booking. Any other `explain` value
(including `false`, `1`, empty) is `422 validation_failed`.

### Policies

`POST /restaurants/{id}/policies` requires an `Idempotency-Key` and accepts a
**complete** policy:

```json
{ "effective_from": "2026-09-28", "slot_minutes": 30,
  "reservation_duration_minutes": 120, "cancellation_cutoff_minutes": 60,
  "opening_hours": [{"weekday": "mon", "opens": "18:00", "closes": "23:00"}],
  "capacities": {"t_1": 2, "t_2": 4, "t_3": 6} }
```

Returns 201 with the supplied policy plus `policy_version` (from 1, +1 per
restaurant). Policy 0 is the fixture's own rules and applies before any published
policy. Policies are immutable. For a booking's local start date the selected
policy is the one with the greatest `effective_from` not later than that date,
ties by greatest `policy_version`.

Every reservation response carries `revision` (1 at creation) and
`accepted_terms` — a snapshot of the selected policy without `effective_from`.

### History and decision

`GET /reservations/{reference}/history` returns `{"reference", "entries"}` oldest
first; every entry carries `seq`, `at`, `event`, `changes`, plus the resulting
`revision` and complete `accepted_terms`. `GET /reservations/{reference}/decision`
returns `{"reference", "revision", "accepted_terms"}`.

### Recurring reservations

`POST /series` adopts an existing booking as occurrence zero of a recurring
agreement:

```json
{ "anchor_reference": "ABC12345", "count": 8, "interval_weeks": 1 }
```

`count` is 2..12 including the anchor, `interval_weeks` is 1..4. Returns 201 with
`{series_id, revision, interval_weeks, occurrences}`. The anchor is unchanged;
generated occurrences start on the anchor's local date plus
`i x interval_weeks x 7` days at the same clock time, each selecting its own
date's policy. A nonexistent local time rejects the whole adoption with
`invalid_local_time`.

`POST /series/{series_id}/amend` (owner-only, idempotent) changes the clock time
of every occurrence at or after `from_index` that is neither cancelled nor an
exception:

```json
{ "expected_revision": 3, "from_index": 2, "local_time": "20:00" }
```

Each real change is an ordinary amendment (old accepted cutoff, then the
resulting date's policy); the series and restaurant revisions each increase once
for the whole operation only if something changed. A mismatched series revision
is `409 stale_revision` before any per-occurrence validation. Series amendments
never mark exceptions.

### Seating changes after a table closure

A manager previews a seating arrangement before applying it:

```json
POST /restaurants/{id}/replans
{ "table_id": "t_2", "from": "2026-09-28T18:00:00+02:00",
  "to": "2026-09-28T23:00:00+02:00" }
```

The instants carry explicit offsets and `from < to`. Every confirmed booking
overlapping the half-open interval `[from, to)` is re-seated on a single table or
a declared pair with enough capacity under its own accepted terms, avoiding fixed
bookings, other assignments, applied closures and the proposed closure. Bookings
keep their reference, owner, party size, start, end and accepted terms. Among
feasible plans the service minimizes change count, then unused seats, then the
option-rank vector in ascending reference order (singles first in fixture order,
then pairs in declared order). Returns 201
`{plan_id, restaurant_revision, closure, assignments, moved_count, unused_seats}`;
a preview changes no state.

```json
POST /restaurants/{id}/replans/{plan_id}/apply
{}
```

Applies the plan atomically: 201 `{plan_id, restaurant_revision, reservations}`.
Each moved booking gains one revision and one `reassigned` history entry with a
`table_ids` change and the `plan_id`; unchanged bookings gain nothing. Any
intervening restaurant revision is `409 stale_plan`; a plan already applied under
another key is `409 plan_already_applied`, while a replay of the successful key
returns the original response with 200. The restaurant revision increments once
for the whole plan. Afterwards the closure excludes the closed table (and any
pair containing it) from availability and rejects creates and amendments with
`409 table_unavailable`.

The restaurant revision starts at 0 after a reset and increments once for each
successful new booking, real amendment, cancellation, policy publication, series
adoption, moves batch or plan application; no-op writes, failures, previews and
replays never increment it.

## Screens

| Route | Screen |
|---|---|
| `/` | Search and availability grid |
| `/signup` | Signup |
| `/login` | Login |
| `/lookup` | Look up a reservation by reference |

Stage 4 adds no new screens; the availability grid reflects an applied plan. The
browser UI signs in with an HttpOnly cookie set by `POST /login` and
`POST /signup`; the JSON API continues to use
`Authorization: Bearer <token>`.

## Export / import

`GET /_test/export` and `POST /_test/import` use format version 1. A stage-4 image
accepts exports produced by this team's stage-1, stage-2 or stage-3 service;
applied closures and revisions round-trip through export/import.
