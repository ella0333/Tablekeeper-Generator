# Tablekeeper — Stage 1: reservations

A containerized HTTP service implementing the Stage 1 restaurant-reservation API:
diners browse restaurant availability, book a table, and receive a confirmation
reference; they can cancel or amend bookings, including changing several bookings
in one atomic request. Each restaurant has its own tables, opening hours and
cancellation policy, and two confirmed reservations can never occupy the same
table at overlapping times (occupancy is the half-open interval
`[starts_at, starts_at + reservation_duration)`).

Pure Python standard library (no third-party runtime dependencies); state is held
in memory and need not survive a restart. The image needs **no outbound network at
run time** — everything it uses (including the IANA timezone database for DST
handling) is baked into the base image.

## Where it lives

This stage lives in the repository at `stage-1/`:

```
stage-1/
├── app.py       the service (source)
├── Dockerfile   builds the runtime image
├── RUN.md       build/run commands and a sample fixture
└── README.md    this file
```

The full specification for this stage is `tablekeeper/spec/stage-1.md` in the
hackathon kickoff repository (not part of this repository).

## Build and run

Start with `RUN.md`; the short version is:

```sh
# build the image
docker build -t tablekeeper-s1 stage-1

# start the service (listens on 0.0.0.0:$PORT, default 8080)
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-s1
```

No manual setup steps are required: the image starts the service directly. Confirm
it is healthy:

```sh
curl -s http://localhost:8080/health
# {"status": "ok"}
```

The service starts with no restaurants. Load some by `POST`ing a fixture to
`/_test/reset` (see `RUN.md` for a complete sample body), then browse
`GET /restaurants`, query `GET /availability`, and create bookings with
`POST /reservations`.

## Push this repository to GitHub

The repository is `main`-branch based. To publish a copy:

```sh
# 1. create an empty repository on GitHub (no README, no .gitignore) and note its
#    URL, e.g. https://github.com/<owner>/<repo>.git
# 2. from the repository root, add it as a remote and push main
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main
```

If the remote already exists, adjust it instead:

```sh
git remote set-url origin https://github.com/<owner>/<repo>.git
git push -u origin main
```
