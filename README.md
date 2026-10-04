# Band Factory

This is an entry for the WeAreDevelopers x BAND Dark Factory hackathon, Tablekeeper track. Four coding agents (planner, builder, tester and delivery) work together in a BAND room and build a restaurant reservation app from the organizers' specification, one stage at a time. Each agent runs Hermes Agent in its own locked-down Docker container.

`FACTORY.md` explains how the agents work together, how the sandbox is built, what a run costs and how the factory catches bad work.

The slides are in `submission/tablekeeper-generator-slides.pdf`, and the video is attached to the [submission release](https://github.com/ella0333/band-factory/releases/tag/submission).

Music in the video: stream cafe - cherry blossoms ([youtu.be/ivVQYpGGvuc](https://youtu.be/ivVQYpGGvuc), [streamcafemusic.com](https://streamcafemusic.com)).

## What is in this repository

- `stage-1/` to `stage-4/` hold the app at each stage. Each folder has its own `RUN.md` with the commands to build and start it.
- `mandates/` holds each agent's standing instructions.
- `factory/` holds everything needed to run the agents yourself.
- `room.json` is the full BAND room the agents worked in, and `costs.csv` is the measured model spend.

## Run the app

Build and start the latest stage, then open http://localhost:8080 in your browser:

```
docker build -t tablekeeper stage-4
docker run --rm -p 8080:8080 tablekeeper
```

The app starts with no restaurants. Load some by sending a fixture to `POST /_test/reset`, as described in the stage's `RUN.md`.

## Run the factory yourself

You need Docker, Python 3.12 or newer, a BAND account and a model provider key.

This run used OpenRouter with `deepseek/deepseek-v4.1-flash`. Featherless is supported as an alternative: if you only fill in a Featherless key, setup switches every agent to `deepseek-ai/DeepSeek-V4-Flash` on Featherless. If you switch, update the `Model:` line at the top of each file in `mandates/` to match.

1. Put this repository at `<workspace>/band-work/result` and clone the hackathon kickoff repository to `<workspace>/dark-factory-wearedevs`.
2. Copy `factory/.env.example` to `<workspace>/local/.env` and fill in a model key and your BAND user API key. This file stays outside the repository.
3. Register the four agents on your BAND account and build the image:
   ```
   python factory/setup.py
   ```
4. Start the agents and create a room for them:
   ```
   docker compose -f factory/docker-compose.yml up -d
   python factory/make_room.py "Factory"
   ```
5. In that room, mention `@planner` and paste the task. The example lead prompt in the hackathon's participant guide works, with `/work` as the workspace root, because that is where the agents see the workspace inside their containers.

Stop the agents with `docker compose -f factory/docker-compose.yml stop`.
