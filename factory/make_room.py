"""Create a BAND room with the four seats in it. Usage: python factory/make_room.py TITLE"""
import json
import os
import pathlib
import sys
import urllib.request

from setup import REPO, SEATS, WORKSPACE, fail, read_env

BASE = os.environ.get("BAND_BASE_URL", "https://app.band.ai").rstrip("/")


def call(method: str, path: str, key: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{BASE}/api/v1{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": key, "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "Mozilla/5.0 band-factory"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def main() -> None:
    if len(sys.argv) != 2:
        fail('usage: python factory/make_room.py "Room title"')
    local = pathlib.Path(os.environ.get("FACTORY_LOCAL", WORKSPACE / "local")).resolve()
    key = read_env(local / ".env").get("BAND_USER_API_KEY", "")
    if not key:
        fail(f"Put BAND_USER_API_KEY in {local / '.env'}.")

    room = call("POST", "/me/chats", key, {"chat": {"title": sys.argv[1]}})
    room_id = (room.get("data") or room)["id"]
    for seat in SEATS:
        agent_id = read_env(local / "seats" / seat / ".env").get("BAND_AGENT_ID")
        if not agent_id:
            fail(f"{seat} is not registered; run factory/setup.py first.")
        call("POST", f"/me/chats/{room_id}/participants", key,
             {"participant": {"participant_id": agent_id, "role": "member"}})
        print(f"added {seat}")
    print(f"room {sys.argv[1]!r}: {room_id}")


if __name__ == "__main__":
    main()
