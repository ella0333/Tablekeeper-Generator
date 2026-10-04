# Tablekeeper — Stage 2: run it

A containerized HTTP service (JSON API + browser screens). Pure Python standard
library; no runtime network, no manual setup.

## Build

```sh
docker build -t tablekeeper-s2 stage-2
```

## Run

```sh
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-s2
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
`204 No Content`. This sample includes a restaurant with a declared combinable
pair `[t_1, t_2]` (capacity 2 + 4 = 6):

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
        "combinable": [ ["t_1", "t_2"] ]
      }
    ],
    "reservations": []
  }'
# 204
```

Then, for example (use a date the restaurant is open):

```sh
curl -s 'http://localhost:8080/availability?restaurant_id=r_anker&date=2026-09-24&party_size=6'
curl -s -X POST http://localhost:8080/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"ada@example.com","password":"correct horse"}'
```

## Screens

| Route | Screen |
|---|---|
| `/` | Search and availability grid |
| `/signup` | Signup |
| `/login` | Login |
| `/lookup` | Look up a reservation by reference |

## Endpoints

Public: `GET /health`, `POST /_test/reset`, `GET /_test/export`,
`POST /_test/import`, `POST /auth/signup`, `POST /auth/login`,
`GET /restaurants`, `GET /restaurants/{id}`, `GET /availability`.

Bearer-token protected: `POST /reservations`, `GET /reservations`,
`GET /reservations/{reference}`, `POST /reservations/{reference}/cancel`,
`PATCH /reservations/{reference}`, `POST /reservation-moves`.

The browser UI signs in with an HttpOnly cookie set by `POST /login` and
`POST /signup`; the JSON API continues to use `Authorization: Bearer <token>`.