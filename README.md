# Band Factory

Entry for the WeAreDevelopers x BAND Dark Factory hackathon, Tablekeeper track.

A band of four coding agents (planner, builder, tester, delivery) built the service in this repository from the organizers' specification, one stage at a time, in a BAND Desktop room. `FACTORY.md` explains how the factory works and how to stand it up.

## How to read this repository

| Path | What it is |
|---|---|
| `FACTORY.md` | The factory: seats, design choices, sandbox, costs, how it catches bad work |
| `mandates/` | Each seat's standing instructions, one file per seat |
| `factory/` | Everything needed to run the factory: container image, compose file, Hermes configs, setup scripts |
| `stage-1/` to `stage-4/` | The service at each stage, each a complete build with its own `RUN.md` |
| `work/plans/`, `work/reports/` | The planner's plans and the tester's reports, as the band wrote them |
| `room.json` | The full BAND room the band worked in |
| `costs.csv` | Measured model spend per run |

## Run a stage

Each stage folder has a `RUN.md` with the commands to build and start that stage's service.
