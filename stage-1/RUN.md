# Tablekeeper — Stage 1: reservations

A containerized HTTP service implementing the Stage 1 reservation API.
Pure Python standard library (no third-party runtime dependencies beyond the
baked-in IANA `tzdata`). State is in memory; the service is stateless across
restarts and needs no outbound network at run time.

## Build

```sh
docker build -t tablekeeper-s1 stage-1
```

## Run

```sh
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-s1
```

The service listens on `0.0.0.0:$PORT` (default `8080`).

## Health check

```sh
curl -s http://localhost:8080/health
# {"status": "ok"}
```

## Seed a fixture

`POST /_test/reset` replaces all state with the posted fixture and returns
`204 No Content`:

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
          { "weekday": "thu", "opens": "18:00", "closes": "23:00" },
          { "weekday": "fri", "opens": "18:00", "closes": "23:30" }
        ],
        "tables": [
          { "id": "t_1", "label": "1", "capacity": 2 },
          { "id": "t_2", "label": "2", "capacity": 4 }
        ]
      }
    ],
    "reservations": []
  }'
# 204
```

Then, for example:

```sh
curl -s 'http://localhost:8080/availability?restaurant_id=r_anker&date=2026-09-24&party_size=4'
curl -s -X POST http://localhost:8080/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"ada@example.com","password":"correct horse"}'
```

## Endpoints

Public: `GET /health`, `POST /_test/reset`, `GET /_test/export`,
`POST /_test/import`, `POST /auth/signup`, `POST /auth/login`,
`GET /restaurants`, `GET /restaurants/{id}`, `GET /availability`.

Bearer-token protected: `POST /reservations`, `GET /reservations`,
`GET /reservations/{reference}`, `POST /reservations/{reference}/cancel`,
`PATCH /reservations/{reference}`, `POST /reservation-moves`.
