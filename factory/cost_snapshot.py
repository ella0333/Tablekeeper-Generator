"""Append the OpenRouter key's total spend to costs.csv. Usage: python factory/cost_snapshot.py LABEL"""
import csv
import datetime
import json
import os
import pathlib
import sys
import urllib.request

from setup import REPO, WORKSPACE, fail, read_env


def main() -> None:
    label = " ".join(sys.argv[1:]) or "snapshot"
    local = pathlib.Path(os.environ.get("FACTORY_LOCAL", WORKSPACE / "local")).resolve()
    key = read_env(local / ".env").get("OPENROUTER_API_KEY", "")
    if not key:
        fail(f"Put OPENROUTER_API_KEY in {local / '.env'}.")
    request = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                     headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        usage = json.loads(response.read())["data"]["usage"]
    path = REPO / "costs.csv"
    new = not path.is_file()
    with path.open("a", newline="") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(["utc_time", "label", "openrouter_total_usd"])
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([now, label, f"{usage:.4f}"])
    print(f"{label}: ${usage:.4f} spent on this key so far")


if __name__ == "__main__":
    main()
