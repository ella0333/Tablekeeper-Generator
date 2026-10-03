"""Stand the factory up: build the seat image, register the four seats on your
BAND account, and write each seat's Hermes home.

Run from anywhere with Python 3.12+ and Docker running:

    python factory/setup.py

It expects this layout (the hackathon guide's), and you can override the three
outside paths with environment variables of the same name:

    <workspace>/dark-factory-wearedevs   KICKOFF_DIR
    <workspace>/band-work/result         this repository
    <workspace>/band-work/checks         CHECKS_DIR
    <workspace>/local                    FACTORY_LOCAL (holds .env, never inside the repo)

Re-running is safe: a seat that is already registered is left alone.
"""
import os
import pathlib
import subprocess
import sys

SEATS = {
    "planner": "Plans each stage from the specification and writes fix plans from bug reports.",
    "builder": "Builds and updates the system from the planner's plans.",
    "tester": "Tests the system against the specification with short-lived tester agents.",
    "delivery": "Packages each passing stage into a clean, documented folder.",
}
IMAGE = "band-factory-seat:latest"
REGISTER = "/opt/hermes/.venv/lib/python3.13/site-packages/hermes_band_platform/skills/add-band/scripts/register_agent.py"

FACTORY = pathlib.Path(__file__).resolve().parent
REPO = FACTORY.parent
WORKSPACE = REPO.parent.parent


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def read_env(path: pathlib.Path) -> dict[str, str]:
    values = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def set_env_value(path: pathlib.Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.is_file() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")


def remove_env_value(path: pathlib.Path, key: str) -> None:
    if path.is_file():
        lines = [l for l in path.read_text().splitlines() if not l.startswith(f"{key}=")]
        path.write_text("\n".join(lines) + "\n")


def main() -> None:
    local = pathlib.Path(os.environ.get("FACTORY_LOCAL", WORKSPACE / "local")).resolve()
    kickoff = pathlib.Path(os.environ.get("KICKOFF_DIR", WORKSPACE / "dark-factory-wearedevs")).resolve()
    checks = pathlib.Path(os.environ.get("CHECKS_DIR", REPO.parent / "checks")).resolve()

    if local == REPO or REPO in local.parents:
        fail(f"{local} is inside the repository. Keep keys outside it; the hackathon "
             f"checker scans every file in the repository for credentials.")
    if not (kickoff / "harness").is_dir():
        fail(f"No kickoff checkout at {kickoff}. Clone "
             f"https://github.com/band-ai/dark-factory-wearedevs there or set KICKOFF_DIR.")

    secrets = read_env(local / ".env")
    # OpenRouter is what the submitted run used; Featherless is the alternative.
    if secrets.get("OPENROUTER_API_KEY"):
        model_file, key_name = "model-openrouter.yaml", "OPENROUTER_API_KEY"
    elif secrets.get("FEATHERLESS_API_KEY"):
        model_file, key_name = "model-featherless.yaml", "FEATHERLESS_API_KEY"
    else:
        fail(f"Put OPENROUTER_API_KEY or FEATHERLESS_API_KEY in {local / '.env'} "
             f"(see factory/.env.example).")
    model_key = secrets[key_name]
    print(f"Model provider: {key_name.split('_')[0].lower()}")

    checks.mkdir(parents=True, exist_ok=True)
    for sub in ("work/plans", "work/reports"):
        (REPO / sub).mkdir(parents=True, exist_ok=True)
        (REPO / sub / ".gitkeep").touch()

    # Paths for docker compose, which reads factory/.env on its own. No secrets here.
    (FACTORY / ".env").write_text(
        f"FACTORY_LOCAL={local.as_posix()}\n"
        f"KICKOFF_DIR={kickoff.as_posix()}\n"
        f"CHECKS_DIR={checks.as_posix()}\n"
    )

    print("Building the seat image (a few minutes the first time).")
    subprocess.run(["docker", "build", "-t", IMAGE, str(FACTORY)], check=True)

    for seat, description in SEATS.items():
        home = local / "seats" / seat
        home.mkdir(parents=True, exist_ok=True)
        template = "tester.yaml" if seat == "tester" else "seat.yaml"
        (home / "config.yaml").write_text(
            (FACTORY / "config" / model_file).read_text()
            + (FACTORY / "config" / template).read_text())
        seat_env = home / ".env"
        for other in ("OPENROUTER_API_KEY", "FEATHERLESS_API_KEY"):
            if other != key_name:
                remove_env_value(seat_env, other)
        set_env_value(seat_env, key_name, model_key)

        if read_env(seat_env).get("BAND_AGENT_ID"):
            print(f"{seat}: already registered")
            continue
        user_key = secrets.get("BAND_USER_API_KEY", "")
        if not user_key:
            fail(f"Put BAND_USER_API_KEY in {local / '.env'} to register {seat}.")
        # The plugin's own registration helper, run once in a throwaway container.
        # It saves only the seat's agent id and key into the seat's .env.
        result = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "python",
             "-e", "BAND_USER_API_KEY", "-e", "HERMES_HOME=/hermes",
             "-e", f"BAND_AGENT_NAME={seat}", "-e", f"BAND_AGENT_DESCRIPTION={description}",
             "-v", f"{home.as_posix()}:/hermes", IMAGE, REGISTER],
            env={**os.environ, "BAND_USER_API_KEY": user_key},
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not read_env(seat_env).get("BAND_AGENT_ID"):
            fail(f"{seat}: registration failed\n{result.stdout}\n{result.stderr}")
        print(f"{seat}: registered")

    print(f"Ready. Start the factory with: docker compose -f {FACTORY / 'docker-compose.yml'} up -d")


if __name__ == "__main__":
    main()
