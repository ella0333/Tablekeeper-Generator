# Stage 2 plan — Tablekeeper: browser booking and combined tables

Spec: `/work/dark-factory-wearedevs/tablekeeper/spec/stage-2.md` (stage-1 requirements continue to apply).
Result repo: `/work/band-work/result`
Stage folder: `/work/band-work/result/stage-2/` — starts as a **copy of `stage-1/`** with no `.git` inside it; `stage-1/` stays unchanged.

## Delivery shape

- Copy `stage-1/` → `stage-2/` first (`cp -a stage-1 stage-2`; delete any nested `.git`). Do not edit `stage-1/`.
- `stage-2/Dockerfile` and `stage-2/RUN.md` still build and start with no manual steps and no run-time network.
- The same container serves both the HTML screens and the JSON API. Keep the stage-1 API behaviour intact; the public/api routes are unchanged except where this plan says otherwise.
- Front end: server-rendered HTML plus a small amount of vanilla JS (fetches to the API) with **no run-time build step** and no external asset downloads — fonts/styles/scripts baked into the image. The tests drive `data-testid` elements, so every listed attribute must exist with the exact value.

## Work items

### S1 — Copy the folder and add the HTML routes
Acceptance: `stage-2/` is a copy of `stage-1/` (no `.git`); `GET /`, `/signup`, `/login`, `/lookup` return HTML (any status/content-type that a browser renders), not JSON; the service still passes every stage-1 check.
Prove: fresh build from `stage-2/Dockerfile`; `curl -s localhost:8080/ | head`; re-run the shipped stage-1 suite.

### S2 — Restaurant model gains `combinable` and `table_ids`
Acceptance: fixture may carry `restaurants[].combinable`, a list of unordered **pairs** of table ids. Capacity of a pair = sum of the two tables' capacities. Seeded `reservations` may hold `table_id` **or** `table_ids`, default `confirmed` unless `status` is `cancelled`. A reservation, whether created or seeded, carries `table_ids` always and `table_id` only when the set has exactly one member. Internally normalise every reservation to a table-id set; occupancy is a set vs. set check on the half-open interval.
Prove: reset with a `combinable: [["t_1","t_2"]]` fixture and a seeded pair booking; read it back.

### S3 — Availability with `available_options`
Acceptance: `GET /availability` keeps `available_table_ids` **exactly** as stage 1 (singles only). Each slot gains `available_options`: every single table and every declared **pair** with `capacity >= party_size` and no overlapping confirmed reservation on any member. Order: singles first in fixture order, then pairs in `combinable` order; `table_ids` within a pair in `combinable` order. Pairs are never transitive and never larger than two; an undeclared pair never appears.
Prove: seeded availability → assert option order, summed capacity, and that `available_table_ids` is unchanged.

### S4 — `POST /reservations` with table sets
Acceptance: body takes `table_ids`; `table_id` is still accepted as a set of one; sending **both** → 422 `validation_failed`. Responses always carry `table_ids` and carry `table_id` only for a singleton. Errors: undeclared pair or >2 tables → 422 `combination_not_allowed`; any member overlapping a confirmed booking → 409 `table_unavailable`; party size above the summed capacity → 422 `party_exceeds_capacity`; duplicate table id in the set → 422 `validation_failed`. All stage-1 validation/occupancy/idempotency/DST/opening-hours rules still apply, now over the whole set atomically.
Prove: create a pair booking; attempt an undeclared pair, a triple, a duplicate, an overlapping pair, and an over-capacity pair.

### S5 — PATCH, cancel and moves with table sets
Acceptance: `PATCH /reservations/{reference}` accepts `table_ids` under the same rules as create (and still accepts `table_id`); cancel frees **every** table in the set; `POST /reservation-moves` accepts `table_ids` per move, keeps stage-1 all-or-nothing semantics, and no table may belong to overlapping resulting bookings.
Prove: patch single→pair and back; cancel a pair and re-check availability; moves that swap a pair.

### S6 — Screens: search and availability grid (`/`)
Acceptance: these `data-testid` elements exist and behave:
- `restaurant-select` (option values are restaurant ids), `date-input` (`YYYY-MM-DD`), `party-size-input` (number), `search-button`, `availability-grid`, `no-slots`.
- One cell `slot-{table_id}-{HH:MM}` per table per slot, plus a combination cell `slot-{t_a}+{t_b}-{HH:MM}` (ids in `combinable` order) for each declared pair available at the searched party size.
- Each cell has `data-available="true"` iff its table/pair is in that slot's `available_options` for the searched party size, else `"false"`. A `true` cell opens the booking form for that table/pair and slot; a `false` cell does nothing. A closed day shows `no-slots` instead of the grid.
- Clicking an available cell while signed out shows `auth-error` or navigates to `/login` (choice).
Prove: a headless browser session driving the testids against a seeded fixture.

### S7 — Booking form and confirmation
Acceptance: `booking-form`, `booking-summary` (contains every table **label** and the local start time), `booking-party-size` (pre-filled from the search), `booking-submit`, `booking-error`. On success show `confirmation`, `confirmation-reference` (text exactly the reference, no surrounding words), `confirmation-details` (restaurant name, every table label, local start time) and `confirmation-tables` (contains every table label). Keep the form on screen after success: re-submitting it **unchanged** returns the same `confirmation-reference` with no `booking-error` and no second booking; changing a field makes the next submit a new booking request. Retries follow stage-1 §7 (same key+body → original receipt).
Prove: book, re-submit unchanged (same reference), change party size (new booking), and a pair booking whose confirmation names both tables.

### S8 — Lookup screen (`/lookup`) and auth screens
Acceptance: `/lookup` has `lookup-reference-input`, `lookup-submit`, `reservation-detail`, `reservation-status` (text exactly `confirmed` or `cancelled`), `reservation-cancel-button` (absent once cancelled), `reservation-error` (not found or cancel refused), and `reservation-tables` (contains every table label). `/signup` has `signup-email`, `signup-password`, `signup-display-name`, `signup-submit`; `/login` has `login-email`, `login-password`, `login-submit`; `auth-error` present only when there is one; `current-user` visible on every screen when signed in and containing the display name; `logout-button` present when signed in.
Prove: headless session: signup → book → look up by reference → cancel → status flips and the cancel button disappears.

### S9 — Competing clients and uncertain outcomes
Acceptance:
- Out-of-order search: if search A starts before B but finishes after B, the grid, table labels and booking form describe **B**; a late A never restores A's results (sequence token / abort on the client).
- 409 `table_unavailable` after the form opens: show `booking-error`, refresh availability, **preserve** the selected form and its inputs so the diner can change the choice, and do **not** show a confirmation.
- Lost booking response (including after commit): show non-empty `booking-uncertain` text with **no** `booking-error` and **no** new confirmation; the unchanged form retries with the **same** idempotency key and body; on success remove the uncertainty/error elements and show the original `confirmation-reference`; a confirmed rejection uses `booking-error`.
These apply to combination bookings too. No background polling, live updates, cross-tab sync or reload recovery is required; the server stays authoritative and the browser never manufactures success from cache.
Prove: headless session with a stubbed/failed fetch and a controlled 409; assert the exact element states.

### S10 — Upgrade compatibility (stage-1 export → stage-2)
Acceptance: the stage-2 service imports an export produced by the same team's **stage-1** service unchanged. A browser signed in before the upgrade stays signed in (its token survives import). A retained reference still works on `/lookup`. A booking whose response was lost before export stays retryable after import with the same body and key, and the UI recovers the original confirmation. These hold when import completes between browser requests; migration during an in-flight request is not required. No reload or new screen required; the form and pending retry identity survive.
Prove: export from the stage-1 image → import into the stage-2 image → assert token/session, lookup and the retried booking.

### S11 — Product and visual quality
Acceptance: a coherent, presentation-ready hospitality UI: consistent typography/spacing/colour/controls; obvious visual hierarchy; human-readable restaurant and table labels prominent, technical ids shown only where helpful; combined tables read as intentional seating options (e.g. "Tables 1 + 2"), not concatenated ids. Available/unavailable/selected/loading/successful/refused/uncertain states are visually distinct. Usable with no horizontal scrolling at 375 CSS px and at desktop widths; visible input labels, apparent keyboard focus, sufficient contrast; considered empty/loading/error states; consistent navigation across `/`, `/signup`, `/login`, `/lookup`.
Prove: headless screenshots at 375px and desktop width; assert no horizontal overflow; eyeball the state styling.

### S12 — Concurrency and atomicity under the new model
Acceptance: concurrent requests produce the same result as some serial order at every read; concurrent identical-key creates still yield one 201; concurrent pair bookings for the same tables yield one 201 and the rest 409; no 5xx; moves remain all-or-nothing.
Prove: 50-way parallel bursts on singles and pairs, plus a moves burst.

## Cross-cutting (a quick test would miss)

- Pair booking occupies **both** tables for the full duration; a single booking for either table afterwards must be 409.
- `available_table_ids` must stay singles-only even when pairs are available; only `available_options` gains pairs.
- Combination cell/value ordering must follow `combinable` order, and a reversed input pair names the same set (not a change).
- `table_id` must be **omitted**, not null, for a pair in every response.
- Idempotency receipts (from stage 1, keyed by method+path+key) must survive export/import so post-import retries still return the original body.
- Importing a stage-1 export must normalise `table_id`-only reservations to a table-id set without regenerating ids, references or timestamps.
- Keep `format_version: 1` on export so cross-stage import stays mutually acceptable.

## Definition of done

`RUN.md` reproduces a healthy service; all S1–S12 acceptance criteria hold; the full stage-1 suite plus the stage-2 UI suite pass; `work/plans/stage-2.md` committed; `stage-1/` still unchanged.