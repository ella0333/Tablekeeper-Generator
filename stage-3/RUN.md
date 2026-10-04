# Tablekeeper — Stage 3: run it

A containerized HTTP service (JSON API + browser screens). Pure Python standard
library; no runtime network, no manual setup.

## Build

```sh
docker build -t tablekeeper-s3 stage-3
```

## Run

```sh
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-s3
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

## Stage 3 endpoints

Public: `GET /health`, `POST /_test/reset`, `GET /_test/export`,
`POST /_test/import`, `POST /auth/signup`, `POST /auth/login`,
`GET /restaurants`, `GET /restaurants/{id}`, `GET /restaurants/{id}/policies`,
`GET /availability`.

Bearer-token protected: `POST /reservations`, `GET /reservations`,
`GET /reservations/{reference}`, `POST /reservations/{reference}/cancel`,
`PATCH /reservations/{reference}`, `POST /reservation-moves`, `POST /series`.

Owner-only, 404 for anyone else (even without a token):
`GET /reservations/{reference}/history`, `GET /reservations/{reference}/decision`,
`GET /series/{series_id}`.

Manager-only: `POST /restaurants/{id}/policies` (401 without a token, 404 for an
unknown restaurant, 403 `forbidden` for a non-manager).

### Availability explanations

`GET /availability?restaurant_id=r_anker&date=2026-09-24&party_size=4&explain=true`

`explain` is optional and accepts only `true`. Without it the response is
unchanged. With it every slot gains `explain`: every table of the restaurant once,
in fixture order, each `{table_id, policy_version, available, rules}` with both
`capacity` and `no_overlap` rules always present. `available` is true exactly when
both rules hold, and the tables whose `available` is true are exactly
`available_table_ids` in the same order. Any other `explain` value (including
`false`, `1`, empty) is `422 validation_failed`.

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

## Screens

| Route | Screen |
|---|---|
| `/` | Search and availability grid |
| `/signup` | Signup |
| `/login` | Login |
| `/lookup` | Look up a reservation by reference |

Stage 3 adds no new screens. The browser UI signs in with an HttpOnly cookie set
by `POST /login` and `POST /signup`; the JSON API continues to use
`Authorization: Bearer <token>`.

## Export / import

`GET /_test/export` and `POST /_test/import` use format version 1. A stage-3 image
accepts exports produced by this team's stage-1 or stage-2 service.
