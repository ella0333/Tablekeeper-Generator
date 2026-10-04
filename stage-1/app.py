#!/usr/bin/env python3
"""Tablekeeper - Stage 1 reservations service.

Pure standard library implementation (no third-party runtime dependencies).
State is held in memory; the process is single-service and thread-safe via one
global re-entrant lock around every request, so check-then-act sequences
(availability/overlap/idempotency) can never interleave.
"""

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone, date as _date, time as _time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

UTC = timezone.utc
LOCK = threading.RLock()

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

RE_LOCAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_TIME = re.compile(r"^\d{2}:\d{2}$")
RE_UINT = re.compile(r"^[0-9]+$")
RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
REF_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


# --------------------------------------------------------------------------
# password hashing (stdlib scrypt)
# --------------------------------------------------------------------------

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14,
                        r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + dk.hex()


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14,
                            r=8, p=1, dklen=len(expected))
        return secrets.compare_digest(dk, expected)
    except Exception:
        return False


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def resolve_local(naive: datetime, tz):
    """Resolve a naive wall-clock datetime in tz.

    Returns an aware datetime, or None when the wall time does not exist
    (spring-forward gap).  For a repeated (fall-back) wall time the first
    occurrence -- fold=0, the one before the clocks change -- is returned.
    """
    for fold in (0, 1):
        cand = naive.replace(tzinfo=tz, fold=fold)
        back = cand.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
        if back == naive:
            return cand
    return None


def fmt_offset(dt: datetime) -> str:
    dt = dt.replace(microsecond=0)
    return dt.isoformat()


def add_absolute(dt: datetime, minutes: int, tz) -> datetime:
    """Add an absolute number of minutes, then present the result in tz.

    Plain ``dt + timedelta`` on a zoneinfo datetime shifts the wall clock and
    re-evaluates the offset, which is wrong across a DST transition.  Going
    through UTC keeps the duration absolute.
    """
    return (dt.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(tz)


def minus_absolute(dt: datetime, minutes: int) -> datetime:
    """Return the instant ``minutes`` absolute minutes before ``dt`` (in UTC)."""
    return dt.astimezone(UTC) - timedelta(minutes=minutes)


def fmt_utc(dt: datetime) -> str:
    dt = dt.astimezone(UTC).replace(microsecond=0)
    return dt.isoformat()


def parse_hhmm(s: str):
    if not isinstance(s, str) or not RE_TIME.match(s):
        return None
    hh = int(s[0:2])
    mm = int(s[3:5])
    if hh > 23 or mm > 59:
        return None
    return hh * 60 + mm


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def err(status, code, message=""):
    return ApiError(status, code, message or code)


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.users = {}          # user_id -> record
        self.email_index = {}    # email -> user_id
        self.tokens = {}         # token -> user_id
        self.restaurants = {}    # restaurant id -> record
        self.reservations = {}   # reference -> record
        self.idem = {}           # user_id -> { key -> record }
        self.user_seq = 0
        self.res_seq = 0

    def clear(self):
        self.users = {}
        self.email_index = {}
        self.tokens = {}
        self.restaurants = {}
        self.reservations = {}
        self.idem = {}
        self.user_seq = 0
        self.res_seq = 0

    def next_user_id(self):
        while True:
            self.user_seq += 1
            uid = "u_%d" % self.user_seq
            if uid not in self.users:
                return uid

    def next_res_id(self):
        while True:
            self.res_seq += 1
            rid = "res_%d" % self.res_seq
            if all(r["reservation_id"] != rid for r in self.reservations.values()):
                return rid

    def new_reference(self):
        while True:
            ref = "".join(secrets.choice(REF_ALPHABET) for _ in range(6))
            if ref not in self.reservations:
                return ref


STORE = Store()


# --------------------------------------------------------------------------
# restaurant helpers
# --------------------------------------------------------------------------

def get_restaurant(rid):
    r = STORE.restaurants.get(rid)
    if r is None:
        raise err(404, "not_found", "no such restaurant")
    return r


def get_table(restaurant, tid):
    for t in restaurant["tables"]:
        if t["id"] == tid:
            return t
    raise err(404, "not_found", "no such table")


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def table_busy(restaurant_id, table_id, start, end, exclude_ref=None):
    for ref, rec in STORE.reservations.items():
        if exclude_ref is not None and ref == exclude_ref:
            continue
        if rec["status"] != "confirmed":
            continue
        if rec["restaurant_id"] != restaurant_id or rec["table_id"] != table_id:
            continue
        if overlaps(start, end, rec["starts_at"], rec["ends_at"]):
            return True
    return False


def public_reservation(rec):
    return {
        "reservation_id": rec["reservation_id"],
        "reference": rec["reference"],
        "restaurant_id": rec["restaurant_id"],
        "table_id": rec["table_id"],
        "party_size": rec["party_size"],
        "status": rec["status"],
        "starts_at_local": rec["starts_at_local"],
        "starts_at": fmt_offset(rec["starts_at"]),
        "ends_at": fmt_offset(rec["ends_at"]),
        "created_at": fmt_utc(rec["created_at"]),
    }


# --------------------------------------------------------------------------
# request parsing helpers
# --------------------------------------------------------------------------

def require_object(raw: bytes) -> dict:
    if raw is None or len(raw) == 0:
        raise err(400, "malformed_request", "empty body")
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        raise err(400, "malformed_request", "body is not valid JSON")
    if not isinstance(body, dict):
        raise err(400, "malformed_request", "body must be a JSON object")
    return body


def get_str_field(body, name, required=True):
    if name not in body:
        if required:
            raise err(422, "validation_failed", "missing field %s" % name)
        return None
    v = body[name]
    if not isinstance(v, str):
        raise err(400, "malformed_request", "field %s has the wrong type" % name)
    return v


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

def authenticate(headers):
    auth = headers.get("Authorization")
    if not auth:
        raise err(401, "unauthenticated", "missing bearer token")
    parts = auth.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise err(401, "unauthenticated", "malformed bearer token")
    token = parts[1].strip()
    uid = STORE.tokens.get(token)
    if uid is None or uid not in STORE.users:
        raise err(401, "unauthenticated", "unknown bearer token")
    return uid


# --------------------------------------------------------------------------
# fixture / state (reset, export, import)
# --------------------------------------------------------------------------

def build_opening_hours(raw):
    hours = {}
    if not isinstance(raw, list):
        return hours
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        wk = entry.get("weekday")
        if wk not in WEEKDAYS:
            continue
        opens = parse_hhmm(entry.get("opens"))
        closes = parse_hhmm(entry.get("closes"))
        if opens is None or closes is None:
            continue
        hours[wk] = {"opens": entry.get("opens"), "closes": entry.get("closes"),
                     "opens_min": opens, "closes_min": closes}
    return hours


def build_restaurant(raw):
    rid = raw.get("id")
    tzname = raw.get("timezone")
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        raise err(422, "validation_failed", "unknown timezone %r" % (tzname,))
    tables = []
    for t in (raw.get("tables") or []):
        if not isinstance(t, dict):
            continue
        tables.append({"id": t.get("id"), "label": t.get("label"),
                       "capacity": t.get("capacity")})
    return {
        "id": rid,
        "name": raw.get("name"),
        "timezone": tzname,
        "slot_minutes": raw.get("slot_minutes"),
        "reservation_duration_minutes": raw.get("reservation_duration_minutes"),
        "cancellation_cutoff_minutes": raw.get("cancellation_cutoff_minutes"),
        "opening_hours": build_opening_hours(raw.get("opening_hours")),
        "opening_hours_raw": raw.get("opening_hours") or [],
        "tables": tables,
        "_tz": tz,
    }


def restaurant_public_detail(r):
    return {
        "id": r["id"],
        "name": r["name"],
        "timezone": r["timezone"],
        "slot_minutes": r["slot_minutes"],
        "reservation_duration_minutes": r["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": r["cancellation_cutoff_minutes"],
        "opening_hours": r["opening_hours_raw"],
        "tables": [{"id": t["id"], "label": t["label"], "capacity": t["capacity"]}
                   for t in r["tables"]],
    }


def make_reservation_record(restaurant, user_id, table_id, starts_at_local,
                            party_size, reference=None, reservation_id=None,
                            status="confirmed", created_at=None):
    naive = datetime.strptime(starts_at_local, "%Y-%m-%dT%H:%M")
    aware = resolve_local(naive, restaurant["_tz"])
    duration = restaurant["reservation_duration_minutes"]
    end = add_absolute(aware, duration, restaurant["_tz"])
    return {
        "reservation_id": reservation_id or STORE.next_res_id(),
        "reference": reference or STORE.new_reference(),
        "user_id": user_id,
        "restaurant_id": restaurant["id"],
        "table_id": table_id,
        "party_size": party_size,
        "status": status,
        "starts_at_local": starts_at_local,
        "starts_at": aware,
        "ends_at": end,
        "created_at": created_at or datetime.now(UTC),
    }


def apply_fixture(fixture: dict):
    """Replace all state with the fixture.  Raises ApiError on invalid input."""
    if not isinstance(fixture, dict):
        raise err(400, "malformed_request", "fixture must be an object")

    new_users = {}
    new_email_index = {}
    for u in (fixture.get("users") or []):
        if not isinstance(u, dict):
            continue
        uid = u.get("id")
        email = u.get("email")
        password = u.get("password")
        display = u.get("display_name")
        if uid is None or email is None or not isinstance(password, str):
            continue
        rec = {
            "id": uid,
            "email": email,
            "password_hash": hash_password(password),
            "display_name": display if display is not None else email,
        }
        new_users[uid] = rec
        new_email_index[email] = uid

    new_restaurants = {}
    for r in (fixture.get("restaurants") or []):
        if not isinstance(r, dict):
            continue
        rec = build_restaurant(r)
        if rec["id"] is None:
            continue
        # normalise timings
        try:
            rec["slot_minutes"] = int(rec["slot_minutes"])
            rec["reservation_duration_minutes"] = int(rec["reservation_duration_minutes"])
            rec["cancellation_cutoff_minutes"] = int(rec["cancellation_cutoff_minutes"])
        except Exception:
            raise err(422, "validation_failed", "invalid restaurant config")
        new_restaurants[rec["id"]] = rec

    new_reservations = {}
    for item in (fixture.get("reservations") or []):
        if not isinstance(item, dict):
            continue
        rid = item.get("restaurant_id")
        restaurant = new_restaurants.get(rid)
        if restaurant is None:
            continue
        tid = item.get("table_id")
        sal = item.get("starts_at_local")
        ps = item.get("party_size")
        try:
            naive = datetime.strptime(sal, "%Y-%m-%dT%H:%M")
        except Exception:
            continue
        aware = resolve_local(naive, restaurant["_tz"])
        if aware is None:
            continue
        rec = {
            "reservation_id": item.get("id") or item.get("reservation_id"),
            "reference": item.get("reference"),
            "user_id": item.get("user_id"),
            "restaurant_id": rid,
            "table_id": tid,
            "party_size": ps,
            "status": item.get("status") or "confirmed",
            "starts_at_local": sal,
            "starts_at": aware,
            "ends_at": add_absolute(aware, restaurant["reservation_duration_minutes"], restaurant["_tz"]),
            "created_at": datetime.now(UTC),
        }
        if not rec["reservation_id"]:
            rec["reservation_id"] = "res_%s" % secrets.token_hex(6)
        if not rec["reference"]:
            rec["reference"] = "".join(secrets.choice(REF_ALPHABET) for _ in range(8))
        new_reservations[rec["reference"]] = rec

    STORE.users = new_users
    STORE.email_index = new_email_index
    STORE.tokens = {}
    STORE.restaurants = new_restaurants
    STORE.reservations = new_reservations
    STORE.idem = {}
    STORE.user_seq = 500
    STORE.res_seq = 500


def rec_to_json(rec):
    return {
        "reservation_id": rec["reservation_id"],
        "reference": rec["reference"],
        "user_id": rec["user_id"],
        "restaurant_id": rec["restaurant_id"],
        "table_id": rec["table_id"],
        "party_size": rec["party_size"],
        "status": rec["status"],
        "starts_at_local": rec["starts_at_local"],
        "starts_at": rec["starts_at"].isoformat(),
        "ends_at": rec["ends_at"].isoformat(),
        "created_at": rec["created_at"].isoformat(),
    }


def rest_to_json(r):
    return {
        "id": r["id"],
        "name": r["name"],
        "timezone": r["timezone"],
        "slot_minutes": r["slot_minutes"],
        "reservation_duration_minutes": r["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": r["cancellation_cutoff_minutes"],
        "opening_hours": r["opening_hours_raw"],
        "tables": r["tables"],
    }


def export_state():
    return {
        "users": {uid: dict(u) for uid, u in STORE.users.items()},
        "email_index": dict(STORE.email_index),
        "tokens": dict(STORE.tokens),
        "restaurants": {rid: rest_to_json(r) for rid, r in STORE.restaurants.items()},
        "reservations": {ref: rec_to_json(rec) for ref, rec in STORE.reservations.items()},
        "idem": {uid: {k: dict(v) for k, v in m.items()} for uid, m in STORE.idem.items()},
        "user_seq": STORE.user_seq,
        "res_seq": STORE.res_seq,
    }


def import_state(state: dict):
    """Atomically replace state from an exported state object."""
    if not isinstance(state, dict):
        raise err(422, "validation_failed", "invalid state")
    if not isinstance(state.get("users"), dict):
        raise err(422, "validation_failed", "invalid state")
    if not isinstance(state.get("restaurants"), dict):
        raise err(422, "validation_failed", "invalid state")
    if not isinstance(state.get("reservations"), dict):
        raise err(422, "validation_failed", "invalid state")
    if not isinstance(state.get("tokens"), dict):
        raise err(422, "validation_failed", "invalid state")

    new_users = {}
    for uid, u in state["users"].items():
        if not isinstance(u, dict) or "password_hash" not in u:
            raise err(422, "validation_failed", "invalid state")
        new_users[uid] = dict(u)

    new_restaurants = {}
    for rid, r in state["restaurants"].items():
        if not isinstance(r, dict):
            raise err(422, "validation_failed", "invalid state")
        rebuilt = build_restaurant(r)
        rebuilt["slot_minutes"] = int(r.get("slot_minutes"))
        rebuilt["reservation_duration_minutes"] = int(r.get("reservation_duration_minutes"))
        rebuilt["cancellation_cutoff_minutes"] = int(r.get("cancellation_cutoff_minutes"))
        new_restaurants[rid] = rebuilt

    new_reservations = {}
    for ref, rec in state["reservations"].items():
        if not isinstance(rec, dict):
            raise err(422, "validation_failed", "invalid state")
        try:
            r = {
                "reservation_id": rec["reservation_id"],
                "reference": rec["reference"],
                "user_id": rec["user_id"],
                "restaurant_id": rec["restaurant_id"],
                "table_id": rec["table_id"],
                "party_size": rec["party_size"],
                "status": rec["status"],
                "starts_at_local": rec["starts_at_local"],
                "starts_at": datetime.fromisoformat(rec["starts_at"]),
                "ends_at": datetime.fromisoformat(rec["ends_at"]),
                "created_at": datetime.fromisoformat(rec["created_at"]),
            }
        except Exception:
            raise err(422, "validation_failed", "invalid state")
        new_reservations[ref] = r

    new_idem = {}
    for uid, m in (state.get("idem") or {}).items():
        if not isinstance(m, dict):
            raise err(422, "validation_failed", "invalid state")
        new_idem[uid] = {k: dict(v) for k, v in m.items()}

    STORE.users = new_users
    STORE.email_index = dict(state.get("email_index") or {})
    STORE.tokens = dict(state["tokens"])
    STORE.restaurants = new_restaurants
    STORE.reservations = new_reservations
    STORE.idem = new_idem
    STORE.user_seq = int(state.get("user_seq") or 500)
    STORE.res_seq = int(state.get("res_seq") or 500)


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------

def canonical(body) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def idempotency_header(headers):
    key = headers.get("Idempotency-Key")
    if key is None or key == "":
        raise err(400, "missing_idempotency_key", "Idempotency-Key header is required")
    if len(key) > 255:
        raise err(422, "validation_failed", "Idempotency-Key is too long")
    return key


def check_replay(user_id, key, method, path, body):
    """Return (replayed_response_or_None).  Raises idempotency_key_reuse."""
    user_map = STORE.idem.get(user_id, {})
    rec = user_map.get(key)
    if rec is None:
        return None
    if rec["method"] == method and rec["path"] == path and rec["body"] == canonical(body):
        return rec["response"]
    raise err(409, "idempotency_key_reuse", "key already used with a different body")


def store_result(user_id, key, method, path, body, status, response):
    STORE.idem.setdefault(user_id, {})[key] = {
        "method": method,
        "path": path,
        "body": canonical(body),
        "status": status,
        "response": response,
    }


# --------------------------------------------------------------------------
# reservation field validation
# --------------------------------------------------------------------------

def validate_party_size(value):
    if value is None:
        raise err(422, "validation_failed", "missing party_size")
    if isinstance(value, bool) or not isinstance(value, int):
        raise err(422, "validation_failed", "party_size must be an integer")
    if value < 1:
        raise err(422, "validation_failed", "party_size must be at least 1")
    return value


def resolve_and_check_time(restaurant, starts_at_local):
    if not isinstance(starts_at_local, str):
        raise err(400, "malformed_request", "starts_at_local must be a string")
    if not RE_LOCAL.match(starts_at_local):
        raise err(422, "validation_failed", "starts_at_local must be YYYY-MM-DDTHH:MM")
    try:
        naive = datetime.strptime(starts_at_local, "%Y-%m-%dT%H:%M")
    except Exception:
        raise err(422, "validation_failed", "invalid starts_at_local")
    aware = resolve_local(naive, restaurant["_tz"])
    if aware is None:
        raise err(422, "invalid_local_time", "local time does not exist")
    return naive, aware


def check_opening(restaurant, naive, aware):
    weekday = WEEKDAYS[naive.weekday()]
    hours = restaurant["opening_hours"].get(weekday)
    if hours is None:
        raise err(422, "outside_opening_hours", "restaurant is closed")
    tod = naive.hour * 60 + naive.minute
    if tod < hours["opens_min"] or tod >= hours["closes_min"]:
        raise err(422, "outside_opening_hours", "outside opening hours")
    step = restaurant["slot_minutes"]
    if (tod - hours["opens_min"]) % step != 0:
        raise err(422, "not_on_slot_grid", "time is not on the slot grid")
    if tod + restaurant["reservation_duration_minutes"] > hours["closes_min"]:
        raise err(422, "outside_opening_hours", "reservation would end after closing")


# --------------------------------------------------------------------------
# endpoint handlers
# --------------------------------------------------------------------------

def handle_health():
    return 200, {"status": "ok"}


def handle_reset(raw):
    try:
        fixture = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        raise err(400, "malformed_request", "invalid JSON")
    if not isinstance(fixture, dict):
        raise err(400, "malformed_request", "fixture must be a JSON object")
    apply_fixture(fixture)
    return 204, None


def handle_export():
    return 200, {"track": "tablekeeper", "format_version": 1, "state": export_state()}


def handle_import(raw):
    try:
        obj = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        raise err(400, "malformed_request", "invalid JSON")
    if not isinstance(obj, dict):
        raise err(422, "validation_failed", "invalid import document")
    if obj.get("track") != "tablekeeper":
        raise err(422, "validation_failed", "wrong track")
    if obj.get("format_version") != 1:
        raise err(422, "validation_failed", "wrong format_version")
    if "state" not in obj:
        raise err(422, "validation_failed", "missing state")
    import_state(obj["state"])
    return 204, None


def handle_signup(raw):
    body = require_object(raw)
    email = get_str_field(body, "email")
    password = get_str_field(body, "password")
    display_name = get_str_field(body, "display_name")
    if not RE_EMAIL.match(email):
        raise err(422, "validation_failed", "invalid email")
    if len(password) < 8:
        raise err(422, "validation_failed", "password too short")
    if email in STORE.email_index:
        raise err(409, "email_taken", "email already registered")
    uid = STORE.next_user_id()
    STORE.users[uid] = {
        "id": uid, "email": email, "password_hash": hash_password(password),
        "display_name": display_name,
    }
    STORE.email_index[email] = uid
    token = secrets.token_urlsafe(32)
    STORE.tokens[token] = uid
    return 201, {"user_id": uid, "display_name": display_name, "token": token}


def handle_login(raw):
    body = require_object(raw)
    email = get_str_field(body, "email")
    password = get_str_field(body, "password")
    uid = STORE.email_index.get(email)
    if uid is None:
        raise err(401, "unauthenticated", "unknown email or password")
    user = STORE.users.get(uid)
    if user is None or not verify_password(password, user["password_hash"]):
        raise err(401, "unauthenticated", "unknown email or password")
    token = secrets.token_urlsafe(32)
    STORE.tokens[token] = uid
    return 200, {"user_id": uid, "display_name": user["display_name"], "token": token}


def handle_list_restaurants():
    items = [{"id": r["id"], "name": r["name"], "timezone": r["timezone"]}
             for r in STORE.restaurants.values()]
    return 200, {"restaurants": items}


def handle_get_restaurant(rid):
    r = STORE.restaurants.get(rid)
    if r is None:
        raise err(404, "not_found", "no such restaurant")
    return 200, restaurant_public_detail(r)


def handle_availability(qs):
    rid = first(qs, "restaurant_id")
    date_s = first(qs, "date")
    party_s = first(qs, "party_size")
    if rid is None or rid == "":
        raise err(422, "validation_failed", "restaurant_id is required")
    if date_s is None or date_s == "":
        raise err(422, "validation_failed", "date is required")
    if party_s is None or party_s == "":
        raise err(422, "validation_failed", "party_size is required")
    if not RE_DATE.match(date_s):
        raise err(422, "validation_failed", "date must be YYYY-MM-DD")
    try:
        d = _date.fromisoformat(date_s)
    except Exception:
        raise err(422, "validation_failed", "invalid date")
    if not RE_UINT.match(party_s):
        raise err(422, "validation_failed", "party_size must be a plain integer")
    party_size = int(party_s)
    if party_size < 1:
        raise err(422, "validation_failed", "party_size must be at least 1")

    r = STORE.restaurants.get(rid)
    if r is None:
        raise err(404, "not_found", "no such restaurant")

    tz = r["_tz"]
    weekday = WEEKDAYS[d.weekday()]
    hours = r["opening_hours"].get(weekday)
    slots = []
    if hours is not None:
        step = r["slot_minutes"]
        duration = r["reservation_duration_minutes"]
        tod = hours["opens_min"]
        while tod + duration <= hours["closes_min"]:
            naive = datetime.combine(d, _time(tod // 60, tod % 60))
            aware = resolve_local(naive, tz)
            if aware is not None:
                end = add_absolute(aware, duration, tz)
                available = []
                for t in r["tables"]:
                    cap = t["capacity"]
                    if not isinstance(cap, int) or cap < party_size:
                        continue
                    if not table_busy(rid, t["id"], aware, end):
                        available.append(t["id"])
                slots.append({
                    "starts_at_local": naive.strftime("%Y-%m-%dT%H:%M"),
                    "starts_at": fmt_offset(aware),
                    "available_table_ids": available,
                })
            tod += step

    return 200, {"restaurant_id": rid, "date": date_s, "timezone": r["timezone"],
                 "slots": slots}


def handle_create_reservation(user_id, raw, headers):
    body = require_object(raw)
    key = idempotency_header(headers)
    replay = check_replay(user_id, key, "POST", "/reservations", body)
    if replay is not None:
        return 200, replay

    rid = body.get("restaurant_id")
    tid = body.get("table_id")
    sal = body.get("starts_at_local")
    ps = body.get("party_size")

    if rid is None:
        raise err(422, "validation_failed", "restaurant_id is required")
    if not isinstance(rid, str):
        raise err(400, "malformed_request", "restaurant_id must be a string")
    if tid is None:
        raise err(422, "validation_failed", "table_id is required")
    if not isinstance(tid, str):
        raise err(400, "malformed_request", "table_id must be a string")
    if sal is None:
        raise err(422, "validation_failed", "starts_at_local is required")
    validate_party_size(ps)

    restaurant = get_restaurant(rid)
    table = get_table(restaurant, tid)
    rewards_naive, aware = resolve_and_check_time(restaurant, sal)
    check_opening(restaurant, rewards_naive, aware)
    if ps > table["capacity"]:
        raise err(422, "party_exceeds_capacity", "party exceeds table capacity")

    end = add_absolute(aware, restaurant["reservation_duration_minutes"], restaurant["_tz"])
    if table_busy(rid, tid, aware, end):
        raise err(409, "table_unavailable", "table is not available")

    rec = make_reservation_record(restaurant, user_id, tid, sal, ps)
    STORE.reservations[rec["reference"]] = rec
    resp = public_reservation(rec)
    store_result(user_id, key, "POST", "/reservations", body, 201, resp)
    return 201, resp


def handle_list_reservations(user_id):
    items = [public_reservation(r) for r in STORE.reservations.values()
             if r["user_id"] == user_id]
    items.sort(key=lambda x: (x["starts_at"], x["created_at"]), reverse=True)
    return 200, {"reservations": items}


def handle_get_reservation(user_id, reference):
    rec = STORE.reservations.get(reference)
    if rec is None or rec["user_id"] != user_id:
        raise err(404, "not_found", "no such reservation")
    return 200, public_reservation(rec)


def handle_cancel(user_id, reference):
    rec = STORE.reservations.get(reference)
    if rec is None or rec["user_id"] != user_id:
        raise err(404, "not_found", "no such reservation")
    if rec["status"] == "cancelled":
        return 200, public_reservation(rec)
    restaurant = STORE.restaurants.get(rec["restaurant_id"])
    cutoff = restaurant["cancellation_cutoff_minutes"] if restaurant else 0
    if datetime.now(UTC) >= minus_absolute(rec["starts_at"], cutoff):
        raise err(409, "cutoff_passed", "cancellation cutoff has passed")
    rec["status"] = "cancelled"
    return 200, public_reservation(rec)


def apply_amendment_fields(restaurant, rec, body):
    """Validate PATCH-style fields against the current reservation.

    Returns the new (table_id, starts_at_local, party_size) without mutating."""
    tid = rec["table_id"]
    sal = rec["starts_at_local"]
    ps = rec["party_size"]

    if "table_id" in body:
        v = body["table_id"]
        if not isinstance(v, str):
            raise err(400, "malformed_request", "table_id must be a string")
        tid = v
    if "party_size" in body:
        ps = validate_party_size(body["party_size"])
    if "starts_at_local" in body:
        v = body["starts_at_local"]
        if not isinstance(v, str):
            raise err(400, "malformed_request", "starts_at_local must be a string")
        sal = v

    table = get_table(restaurant, tid)
    naive, aware = resolve_and_check_time(restaurant, sal)
    check_opening(restaurant, naive, aware)
    if ps > table["capacity"]:
        raise err(422, "party_exceeds_capacity", "party exceeds table capacity")
    return tid, sal, ps, aware


def handle_patch(user_id, reference, raw):
    body = require_object(raw)
    rec = STORE.reservations.get(reference)
    if rec is None or rec["user_id"] != user_id:
        raise err(404, "not_found", "no such reservation")
    if rec["status"] == "cancelled":
        raise err(409, "reservation_cancelled", "reservation is cancelled")
    restaurant = STORE.restaurants.get(rec["restaurant_id"])
    cutoff = restaurant["cancellation_cutoff_minutes"] if restaurant else 0
    if datetime.now(UTC) >= minus_absolute(rec["starts_at"], cutoff):
        raise err(409, "cutoff_passed", "cutoff has passed")

    tid, sal, ps, aware = apply_amendment_fields(restaurant, rec, body)
    end = add_absolute(aware, restaurant["reservation_duration_minutes"], restaurant["_tz"])
    if table_busy(rec["restaurant_id"], tid, aware, end, exclude_ref=reference):
        raise err(409, "table_unavailable", "table is not available")

    rec["table_id"] = tid
    rec["starts_at_local"] = sal
    rec["party_size"] = ps
    rec["starts_at"] = aware
    rec["ends_at"] = end
    return 200, public_reservation(rec)


def handle_moves(user_id, raw, headers):
    body = require_object(raw)
    key = idempotency_header(headers)
    replay = check_replay(user_id, key, "POST", "/reservation-moves", body)
    if replay is not None:
        return 200, replay

    moves = body.get("moves")
    if not isinstance(moves, list) or not (1 <= len(moves) <= 8):
        raise err(422, "validation_failed", "moves must be a list of 1..8 items")
    refs = []
    for m in moves:
        if not isinstance(m, dict):
            raise err(422, "validation_failed", "each move must be an object")
        ref = m.get("reference")
        if not isinstance(ref, str):
            raise err(422, "validation_failed", "move reference must be a string")
        refs.append(ref)
    if len(set(refs)) != len(refs):
        raise err(422, "validation_failed", "duplicate references")

    # process strictly in input order: reference, cancelled, cutoff, fields
    records = []
    planned = []
    for move in moves:
        ref = move["reference"]
        rec = STORE.reservations.get(ref)
        if rec is None or rec["user_id"] != user_id:
            raise err(404, "not_found", "no such reservation")
        records.append(rec)
        if rec["status"] == "cancelled":
            raise err(409, "reservation_cancelled", "reservation is cancelled")
        restaurant = STORE.restaurants.get(rec["restaurant_id"])
        cutoff = restaurant["cancellation_cutoff_minutes"] if restaurant else 0
        if datetime.now(UTC) >= minus_absolute(rec["starts_at"], cutoff):
            raise err(409, "cutoff_passed", "cutoff has passed")
        tid, sal, ps, aware = apply_amendment_fields(restaurant, rec, move)
        end = add_absolute(aware, restaurant["reservation_duration_minutes"], restaurant["_tz"])
        planned.append((rec, tid, sal, ps, aware, end))

    if len({rec["restaurant_id"] for rec in records}) != 1:
        raise err(422, "validation_failed", "all bookings must share a restaurant")

    # occupancy: overlap among results and against unlisted confirmed bookings
    listed_refs = set(refs)
    for i, (rec, tid, sal, ps, aware, end) in enumerate(planned):
        for j in range(i + 1, len(planned)):
            orec, otid, osal, ops, oaware, oend = planned[j]
            if tid == otid and overlaps(aware, end, oaware, oend):
                raise err(409, "table_unavailable", "resulting bookings overlap")
    for rec, tid, sal, ps, aware, end in planned:
        for ref, other in STORE.reservations.items():
            if ref in listed_refs:
                continue
            if other["status"] != "confirmed":
                continue
            if other["restaurant_id"] != rec["restaurant_id"] or other["table_id"] != tid:
                continue
            if overlaps(aware, end, other["starts_at"], other["ends_at"]):
                raise err(409, "table_unavailable", "table is not available")

    # commit
    for rec, tid, sal, ps, aware, end in planned:
        rec["table_id"] = tid
        rec["starts_at_local"] = sal
        rec["party_size"] = ps
        rec["starts_at"] = aware
        rec["ends_at"] = end

    resp = {"reservations": [public_reservation(rec) for rec in records]}
    store_result(user_id, key, "POST", "/reservation-moves", body, 201, resp)
    return 201, resp


def first(qs, name):
    vals = qs.get(name)
    if not vals:
        return None
    return vals[0]


# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------

PUBLIC_PATHS = {"/health", "/_test/reset", "/_test/export", "/_test/import",
                "/auth/signup", "/auth/login", "/restaurants", "/availability"}


def dispatch(method, path, qs, raw, headers):
    parts = [unquote(p) for p in path.split("/") if p != ""]

    if path == "/health":
        if method != "GET":
            raise err(404, "not_found", "no such route")
        return handle_health()

    if path == "/_test/reset":
        if method != "POST":
            raise err(404, "not_found", "no such route")
        return handle_reset(raw)
    if path == "/_test/export":
        if method != "GET":
            raise err(404, "not_found", "no such route")
        return handle_export()
    if path == "/_test/import":
        if method != "POST":
            raise err(404, "not_found", "no such route")
        return handle_import(raw)

    if path == "/auth/signup":
        if method != "POST":
            raise err(404, "not_found", "no such route")
        return handle_signup(raw)
    if path == "/auth/login":
        if method != "POST":
            raise err(404, "not_found", "no such route")
        return handle_login(raw)

    if path == "/restaurants":
        if method != "GET":
            raise err(404, "not_found", "no such route")
        return handle_list_restaurants()
    if path == "/availability":
        if method != "GET":
            raise err(404, "not_found", "no such route")
        return handle_availability(qs)

    if len(parts) == 2 and parts[0] == "restaurants":
        if method != "GET":
            raise err(404, "not_found", "no such route")
        return handle_get_restaurant(parts[1])

    if path == "/reservations":
        user_id = authenticate(headers)
        if method == "POST":
            return handle_create_reservation(user_id, raw, headers)
        if method == "GET":
            return handle_list_reservations(user_id)
        raise err(404, "not_found", "no such route")

    if path == "/reservation-moves":
        user_id = authenticate(headers)
        if method != "POST":
            raise err(404, "not_found", "no such route")
        return handle_moves(user_id, raw, headers)

    if len(parts) == 2 and parts[0] == "reservations":
        user_id = authenticate(headers)
        if method == "GET":
            return handle_get_reservation(user_id, parts[1])
        if method == "PATCH":
            return handle_patch(user_id, parts[1], raw)
        raise err(404, "not_found", "no such route")

    if len(parts) == 3 and parts[0] == "reservations" and parts[2] == "cancel":
        user_id = authenticate(headers)
        if method != "POST":
            raise err(404, "not_found", "no such route")
        return handle_cancel(user_id, parts[1])

    raise err(404, "not_found", "no such route")


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Tablekeeper/1.0"

    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        try:
            cl = self.headers.get("Content-Length")
            n = int(cl) if cl else 0
        except Exception:
            n = 0
        if n <= 0:
            return b""
        try:
            return self.rfile.read(n)
        except Exception:
            return b""

    def _handle(self, method):
        raw = self._read_body()
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query, keep_blank_values=True)
        try:
            with LOCK:
                status, body = dispatch(method, path, qs, raw, self.headers)
        except ApiError as e:
            status, body = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception:
            status, body = 500, {"error": {"code": "internal_error",
                                           "message": "internal server error"}}
        self._send(status, body)

    def _send(self, status, body):
        if body is None:
            self.send_response(status)
            self.end_headers()
            return
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    server.request_queue_size = 256
    server.serve_forever()


if __name__ == "__main__":
    main()
