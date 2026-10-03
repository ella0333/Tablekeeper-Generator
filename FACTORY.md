# The factory

Four coding-agent seats in one BAND room. Each runs Hermes Agent (v2026.9.24) with the BAND platform plugin, in its own locked-down container, on `deepseek/deepseek-v4.1-flash` through OpenRouter. Featherless (`deepseek-ai/DeepSeek-V4-Flash`) is supported as an alternative provider; see the README.

## Seats

| Seat | Owns | Hands work to |
|---|---|---|
| planner | The plan for each stage, and a fix plan for each failure report. Leads the run: starts each stage, posts stage and final reports | builder |
| builder | The code in each stage folder | tester |
| tester | Whether the work matches the specification. Spawns two or three short-lived tester agents, one per kind of user or client, and runs the event's checker in isolated mode | planner on failure, delivery on pass |
| delivery | A clean, documented stage folder that builds | planner when delivered, tester if it does not build |

The loop for every stage is:

```
planner -> builder -> tester -> (fail) planner -> builder -> tester -> ... -> (pass) delivery -> planner
```

When delivery reports a stage delivered, the planner sends it a one-line receipt before starting the next stage, so every delivery is acknowledged in the room.

The builder never talks to delivery, and nothing reaches delivery that the tester has not passed.

Each seat's mandate is in `mandates/<seat>.md`. The mandates describe how the factory works and name nothing about the problem; the problem arrives only in the task posted to the room.

## Design choices

**Separate planning, building and judging.** The seat that writes the code never decides it is done, and the seat that decides never edits the code. The planner reads the tester's report next to the builder's code and writes a fix plan scoped to the failures, so each fix round has a written reason in `work/plans/`.

**Access is enforced by mounts, not only by instructions.**

| | Stage code | Plans | Reports | Specs and checker | Build daemon |
|---|---|---|---|---|---|
| planner | read | write | read | read | none |
| builder | write | read | read | read | yes |
| tester | read | read | write | read | yes |
| delivery | write (clean-up and docs only, by mandate) | read | read | read | yes |

**Tester agents are capped by configuration.** The tester's Hermes config sets `delegation.max_concurrent_children: 3`, `max_spawn_depth: 1`, `max_iterations: 40` and a 15-minute timeout per tester agent, so the limit holds whatever the model asks for. The other seats have delegation turned off.

**Test against the specification, not the shipped checks.** The tester treats the event's checks as partial and tests what the specification requires that they do not cover. If a test needs a modified copy of the service, it works in `/tmp/scratch` and deletes it; the committed system carries no test hooks.

**Handoffs carry everything.** A seat sees only messages that mention it, so every handoff pastes the full specification and plan, split into numbered parts when long.

**Bounded loops.** After five fix rounds on one stage the planner records the remaining failures as the stage outcome and moves on, so a run always ends.

## Sandbox

- Each seat runs in its own container: non-root, all capabilities dropped, read-only root filesystem, 4 GB memory, 2 CPUs.
- Seats sit on an internal Docker network with no route out. A squid proxy is the only exit and allows OpenRouter, Featherless, BAND and package registries; everything else is refused and logged.
- Builds and test runs go to a private Docker daemon (Docker-in-Docker) on the same internal network, so seats never touch the host's Docker. The event's checker runs there in isolated mode, the same no-network mode used for judging.
- Keys live outside the repository. Seats get only their own BAND agent key and the model provider key.

## Stand it up

You need Docker, Python 3.12+, an OpenRouter or Featherless key and a BAND user API key.

1. Lay out a workspace the way the participant guide does:
   ```
   <workspace>/dark-factory-wearedevs   the kickoff repository
   <workspace>/band-work/result         this repository
   <workspace>/local/.env               your keys (copy factory/.env.example)
   ```
2. Register the seats on your BAND account and build the image:
   ```
   python factory/setup.py
   ```
3. Start the factory:
   ```
   docker compose -f factory/docker-compose.yml up -d
   ```
4. Create a room with the four seats:
   ```
   python factory/make_room.py "Factory"
   ```
5. Post the task to `@planner` in that room.

Measure a run's model spend with `python factory/cost_snapshot.py "<label>"` before and after; rows go to `costs.csv`.

## Costs

To be filled in from `costs.csv` after the submitted run.

## How it catches and recovers from bad work

To be filled in with examples from the submitted run.

Recovery built into the factory:

- If the build daemon is down, seats wait for it with `wait-for-docker` and report the outage as a blocker instead of handing off unchecked work.
- After five fix rounds on one stage the planner records the remaining failures as the stage outcome, so a run always ends.
- Delivery rebuilds the stage folder from the passing revision and hands it back to the tester if it no longer builds.
