# Tablekeeper — Stage 2: online booking and combined tables

A containerized HTTP service for restaurant reservations, adding a browser UI and
bookable **pairs of tables** on top of the Stage 1 API.

- Diners search availability, book a table (or a restaurant-approved pair of
  tables), and receive a confirmation reference. They can cancel or amend
  bookings, including several in one atomic request.
- A pair occupies **both** tables for the reservation's full duration. Occupancy
  is the half-open interval `[starts_at, starts_at + reservation_duration)`, and
  two confirmed reservations never share a table at overlapping times.
- Screens: `/` (search and availability grid), `/signup`, `/login`, `/lookup`.
- Pure Python standard library — no third-party runtime dependencies, no runtime
  build step, and **no outbound network at run time** (the IANA timezone database
  is baked into the base image). State is in memory and need not survive a restart.

The Stage 1 requirements continue to apply; `stage-1/` is unchanged.

## Where it lives

```
stage-2/
├── app.py       the service (source: JSON API + HTML screens + CSS/JS)
├── Dockerfile   builds the runtime image
├── RUN.md       build/run commands and a sample fixture
└── README.md    this file
```

The specifications are `tablekeeper/spec/stage-1.md` and `stage-2.md` in the
hackathon kickoff repository (not part of this repository).

## Build and run

```sh
# build the image
docker build -t tablekeeper-s2 stage-2

# start the service (listens on 0.0.0.0:$PORT, default 8080)
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-s2
```

No manual setup steps are required. Confirm it is healthy, then open the UI at
<http://localhost:8080/>:

```sh
curl -s http://localhost:8080/health
# {"status": "ok"}
```

The service starts with no restaurants. Load some by `POST`ing a fixture to
`/_test/reset` (see `RUN.md` for a complete sample body that includes a
`combinable` pair), then search in the browser or use the JSON API.

## Screens

| Route | Screen |
|---|---|
| `/` | Search and availability grid |
| `/signup` | Signup |
| `/login` | Login |
| `/lookup` | Look up a reservation by reference |

## API additions over Stage 1

- `GET /restaurants/{id}` also returns `combinable` (the declared pairs).
- `GET /availability` slots gain `available_options` (single tables then declared
  pairs, each with `capacity >= party_size` and no overlapping booking).
  `available_table_ids` stays single-table-only.
- `POST /reservations` and `PATCH /reservations/{reference}` accept `table_ids`
  (a list of one or two table ids) as well as `table_id`; responses always carry
  `table_ids`, and `table_id` only when the set has one member.
- `POST /reservation-moves` accepts `table_ids` per move.

Export stays `format_version: 1`, so a Stage 1 export imports unchanged.