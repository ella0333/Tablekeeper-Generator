# Stage 2 tester report — round 1 (PASS)

- Revision tested: `24fdf2dfcf4191733e2f1460799be70bce946ec9` (short `24fdf2d`, "Stage 2: browser booking UI and combinable table pairs")
- Stage folder: `/work/band-work/result/stage-2/` (`app.py`, `Dockerfile`, `RUN.md`, `README.md`); `stage-1/` unchanged.
- Verdict: **PASS** — no defect found. Stage-1 requirements continue to hold; stage-2 additions (browser UI, competing clients/uncertain outcomes, combinable table pairs, cross-stage upgrade) all behave per the specification.

## How it was run

The checker's isolated mode is environmentally broken on this Docker daemon (container-to-container traffic on an `--internal` network is not routable — `harness run --mode isolated` fails with `httpx.ConnectError: [Errno 101] Network is unreachable`, output kept at `/work/band-work/checks/stage-2-isolated/`). The shipped suites were therefore run inside the harness runner image (`df-harness-runner`, which ships Chromium) on a normal bridge network, with the stage-1 service as the previous stage for the upgrade checks.

## Evidence

- Build: `docker build -t tk-s2 stage-2` succeeded; `GET /health -> 200 {"status":"ok"}`; `GET / -> text/html`.
- **Shipped stage-1 suite: 120/120 passed** (`checks/stage-2-bridge/stage-1.counts.json`).
- **Shipped stage-2 suite: 25/25 passed** (`checks/stage-2-bridge/stage-2.counts.json`).
- Combination UI probe (`checks/stage-2-combo-ui.log`): a declared pair renders cell `slot-t_1+t_2-19:00` with `data-available="true"` at party 6 while single `slot-t_1-19:00` is `false` and `slot-t_3-19:00` is `true`; `booking-summary` = "Table 1 + 2 · 19:00 · Zum Anker"; `confirmation-tables` = "Tables: 1 + 2"; `confirmation-details` names the restaurant, both labels and the time; `/lookup` shows `reservation-status` "confirmed" and `reservation-tables` "Tables: 1 + 2"; no horizontal overflow at 375 px or 1280 px (scrollWidth == clientWidth).
- Deep UI probe (`checks/stage-2-deep-ui.log`), 8/8 PASS:
  - out-of-order search: a stalled earlier search released late does not restore its results;
  - `409 table_unavailable` after the form opens → `booking-error`, no confirmation, `booking-form` and its inputs preserved;
  - lost booking response → nonempty `booking-uncertain`, no `booking-error`, no confirmation, booking committed; unchanged retry recovers the original reference and clears the uncertainty, exactly one booking;
  - browser session survives an export/import; a stage-1 export imported into stage-2 keeps a stage-1 token valid and the browser signed in; a retained stage-1 reference works via `/lookup`; a pending retry identity survives the upgrade and recovers the original reference.
- API probe: `available_options` order/capacity exact (singles in fixture order, then pairs in `combinable` order, capacity summed; `available_table_ids` stays singles-only); pair create 201 with `table_ids` and **no** `table_id` key; `table_id` still a set of one (echoes `table_id`); both → 422; duplicate → 422; undeclared pair → 422 `combination_not_allowed`; triple → 422; `party_exceeds_capacity` → 422; occupancy blocks a single or pair overlapping a booked pair (409); cancel frees both tables; seeded `table_ids` / `table_id` / `status:cancelled` reservations behave; `PATCH` to `table_ids` keeps `reference`/`reservation_id`; moves accept `table_ids` (201 in input order, 200 replay identical, colliding → 409 with state unchanged, missing key 400, no token 401, foreign ref 404); 50 concurrent pair bookings with different keys → one 201 + 49×409 `table_unavailable`, with the same key → one 201 + 49×200 identical, no 5xx.
- Cross-stage import: a stage-1 `/_test/export` imported into stage-2 (`204`) preserves the stage-1 token, booking reference and occupancy.

## Notes (non-blocking)

- A combination cell is rendered for every declared pair whose summed capacity can seat the party, with `data-available` reflecting availability (an unavailable pair shows `data-available="false"`). The spec's "shown when a declared pair is available" plus "carries `data-available` like a single cell" is read as cells always present with a true/false attribute, matching the single-table cells; not scored as a defect.
- The three tester agents dispatched this round were blocked by the same missing Chromium system libraries that block host-mode browser runs; two produced no UI results and one hit its iteration cap. Every behaviour they were to check was verified directly by the tester using the runner-image recipe above, so their partial results add no open item.