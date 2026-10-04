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
        self.series = {}         # series_id -> record
        self.plans = {}          # plan_id -> record (stage 4)
        self.user_seq = 0
        self.res_seq = 0
        self.series_seq = 0
        self.plan_seq = 0

    def clear(self):
        self.users = {}
        self.email_index = {}
        self.tokens = {}
        self.restaurants = {}
        self.reservations = {}
        self.idem = {}
        self.series = {}
        self.plans = {}
        self.user_seq = 0
        self.res_seq = 0
        self.series_seq = 0
        self.plan_seq = 0

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

    def next_series_id(self):
        while True:
            sid = "ser_" + secrets.token_hex(8)
            if sid not in self.series:
                return sid

    def next_plan_id(self):
        while True:
            pid = "plan_" + secrets.token_hex(8)
            if pid not in self.plans:
                return pid


STORE = Store()


# --------------------------------------------------------------------------
# restaurant helpers
# --------------------------------------------------------------------------

def get_restaurant(rid):
    r = STORE.restaurants.get(rid)
    if r is None:
        raise err(404, "not_found", "no such restaurant")
    return r


def find_table(restaurant, tid):
    for t in restaurant["tables"]:
        if t["id"] == tid:
            return t
    return None


def get_table(restaurant, tid):
    t = find_table(restaurant, tid)
    if t is None:
        raise err(404, "not_found", "no such table")
    return t


def seeded_table_ids(item):
    """A seeded reservation may carry ``table_ids`` or a single ``table_id``."""
    raw = item.get("table_ids")
    if isinstance(raw, list):
        ids = [x for x in raw if isinstance(x, str)]
        if ids:
            return ids
    single = item.get("table_id")
    if isinstance(single, str):
        return [single]
    return []


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def is_declared_pair(restaurant, table_ids):
    want = set(table_ids)
    for pair in restaurant.get("combinable") or []:
        if set(pair) == want:
            return True
    return False


def canonical_table_ids(restaurant, table_ids):
    """Order a table set the way the restaurant declares it (combinable order)."""
    if len(table_ids) <= 1:
        return list(table_ids)
    want = set(table_ids)
    for pair in restaurant.get("combinable") or []:
        if set(pair) == want:
            return [pair[0], pair[1]]
    return list(table_ids)


# --------------------------------------------------------------------------
# booking policies (stage 3)
# --------------------------------------------------------------------------

def normalized_hours(raw):
    out = []
    seen = set()
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        wk = entry.get("weekday")
        if wk not in WEEKDAYS or wk in seen:
            continue
        if parse_hhmm(entry.get("opens")) is None or parse_hhmm(entry.get("closes")) is None:
            continue
        seen.add(wk)
        out.append({"weekday": wk, "opens": entry.get("opens"), "closes": entry.get("closes")})
    return out


def policy0(restaurant):
    return {
        "policy_version": 0,
        "effective_from": None,
        "slot_minutes": restaurant["slot_minutes"],
        "reservation_duration_minutes": restaurant["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": restaurant["cancellation_cutoff_minutes"],
        "opening_hours": normalized_hours(restaurant.get("opening_hours_raw")),
        "capacities": {t["id"]: t.get("capacity") for t in restaurant["tables"]},
    }


def load_policies(raw):
    out = []
    if not isinstance(raw, list):
        return out
    for p in raw:
        if not isinstance(p, dict):
            continue
        try:
            out.append({
                "policy_version": int(p["policy_version"]),
                "effective_from": p["effective_from"],
                "slot_minutes": int(p["slot_minutes"]),
                "reservation_duration_minutes": int(p["reservation_duration_minutes"]),
                "cancellation_cutoff_minutes": int(p["cancellation_cutoff_minutes"]),
                "opening_hours": normalized_hours(p.get("opening_hours")),
                "capacities": {k: int(v) for k, v in (p.get("capacities") or {}).items()},
            })
        except Exception:
            continue
    out.sort(key=lambda p: p["policy_version"])
    return out


def policy_public(p):
    return {
        "policy_version": p["policy_version"],
        "effective_from": p["effective_from"],
        "slot_minutes": p["slot_minutes"],
        "reservation_duration_minutes": p["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": p["cancellation_cutoff_minutes"],
        "opening_hours": [dict(x) for x in p.get("opening_hours") or []],
        "capacities": dict(p.get("capacities") or {}),
    }


def terms_of(policy):
    return {
        "policy_version": policy["policy_version"],
        "slot_minutes": policy["slot_minutes"],
        "reservation_duration_minutes": policy["reservation_duration_minutes"],
        "cancellation_cutoff_minutes": policy["cancellation_cutoff_minutes"],
        "opening_hours": [dict(x) for x in policy.get("opening_hours") or []],
        "capacities": dict(policy.get("capacities") or {}),
    }


def copy_terms(t):
    t = t or {}
    return {
        "policy_version": t.get("policy_version", 0),
        "slot_minutes": t.get("slot_minutes"),
        "reservation_duration_minutes": t.get("reservation_duration_minutes"),
        "cancellation_cutoff_minutes": t.get("cancellation_cutoff_minutes"),
        "opening_hours": [dict(x) for x in t.get("opening_hours") or []],
        "capacities": dict(t.get("capacities") or {}),
    }


def select_policy(restaurant, date_str):
    """Greatest effective_from not later than date; ties take the greatest version."""
    best = None
    best_key = None
    for p in restaurant.get("policies") or []:
        ef = p.get("effective_from")
        if ef is not None and ef <= date_str:
            key = (ef, p["policy_version"])
            if best_key is None or key > best_key:
                best_key = key
                best = p
    return best if best is not None else policy0(restaurant)


def policy_capacity(policy, table_ids):
    caps = policy.get("capacities") or {}
    total = 0
    for tid in table_ids:
        c = caps.get(tid)
        if isinstance(c, int) and not isinstance(c, bool):
            total += c
    return total


def policy_hours_map(policy):
    return build_opening_hours(policy.get("opening_hours"))


def check_opening_policy(policy, naive):
    weekday = WEEKDAYS[naive.weekday()]
    hours = policy_hours_map(policy).get(weekday)
    if hours is None:
        raise err(422, "outside_opening_hours", "restaurant is closed")
    tod = naive.hour * 60 + naive.minute
    if tod < hours["opens_min"] or tod >= hours["closes_min"]:
        raise err(422, "outside_opening_hours", "outside opening hours")
    step = policy["slot_minutes"]
    if (tod - hours["opens_min"]) % step != 0:
        raise err(422, "not_on_slot_grid", "time is not on the slot grid")
    if tod + policy["reservation_duration_minutes"] > hours["closes_min"]:
        raise err(422, "outside_opening_hours", "reservation would end after closing")


def bump_revision(restaurant):
    if restaurant is not None:
        restaurant["revision"] = int(restaurant.get("revision", 0)) + 1


def any_table_busy(restaurant_id, table_ids, start, end, exclude_ref=None):
    members = set(table_ids)
    for ref, rec in STORE.reservations.items():
        if exclude_ref is not None and ref == exclude_ref:
            continue
        if rec["status"] != "confirmed":
            continue
        if rec["restaurant_id"] != restaurant_id:
            continue
        if not (set(rec["table_ids"]) & members):
            continue
        if overlaps(start, end, rec["starts_at"], rec["ends_at"]):
            return True
    return False


def load_closures(raw):
    out = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        if not isinstance(c, dict):
            continue
        tid = c.get("table_id")
        frm = parse_instant(c.get("from"))
        to = parse_instant(c.get("to"))
        if isinstance(tid, str) and frm is not None and to is not None and frm < to:
            out.append({"table_id": tid, "from": frm, "to": to})
    return out


def parse_instant(value):
    """Parse an RFC 3339 instant that carries an explicit offset."""
    if not isinstance(value, str):
        return None
    try:
        dtv = datetime.fromisoformat(value)
    except Exception:
        return None
    if dtv.tzinfo is None or dtv.utcoffset() is None:
        return None
    return dtv


def closure_blocks(restaurant, table_ids, start, end):
    members = set(table_ids)
    for c in restaurant.get("closures") or []:
        if c["table_id"] in members and overlaps(start, end, c["from"], c["to"]):
            return True
    return False


def public_reservation(rec):
    tids = list(rec["table_ids"])
    out = {
        "reservation_id": rec["reservation_id"],
        "reference": rec["reference"],
        "restaurant_id": rec["restaurant_id"],
        "table_ids": tids,
        "party_size": rec["party_size"],
        "status": rec["status"],
        "starts_at_local": rec["starts_at_local"],
        "starts_at": fmt_offset(rec["starts_at"]),
        "ends_at": fmt_offset(rec["ends_at"]),
        "created_at": fmt_utc(rec["created_at"]),
        "revision": rec.get("revision", 1),
        "accepted_terms": copy_terms(rec.get("accepted_terms")),
    }
    if len(tids) == 1:
        out["table_id"] = tids[0]
    return out


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

def cookie_value(header, name):
    if not header:
        return None
    for part in header.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == name:
                return unquote(v.strip())
    return None


def bearer_token(headers):
    auth = headers.get("Authorization")
    if auth:
        parts = auth.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise err(401, "unauthenticated", "malformed bearer token")
        return parts[1].strip()
    # The browser UI signs in with a cookie; the JSON API keeps using Bearer.
    return cookie_value(headers.get("Cookie"), "tk_token")


def authenticate(headers):
    token = bearer_token(headers)
    if not token:
        raise err(401, "unauthenticated", "missing bearer token")
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
    if "id" in raw and (not isinstance(rid, str) or len(rid) > 64):
        raise err(422, "validation_failed", "invalid restaurant id")
    tzname = raw.get("timezone")
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        raise err(422, "validation_failed", "unknown timezone %r" % (tzname,))
    tables = []
    for t in (raw.get("tables") or []):
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        if "id" in t and (not isinstance(tid, str) or len(tid) > 64):
            raise err(422, "validation_failed", "invalid table id")
        tables.append({"id": tid, "label": t.get("label"),
                       "capacity": t.get("capacity")})
    known = {t["id"] for t in tables if t["id"] is not None}
    combinable = []
    raw_comb = raw.get("combinable")
    if isinstance(raw_comb, list):
        for entry in raw_comb:
            if not isinstance(entry, list) or len(entry) != 2:
                continue
            a, b = entry[0], entry[1]
            if not isinstance(a, str) or not isinstance(b, str) or a == b:
                continue
            if a not in known or b not in known:
                continue
            combinable.append([a, b])
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
        "combinable": combinable,
        "manager_user_ids": [x for x in (raw.get("manager_user_ids") or [])
                             if isinstance(x, str)],
        "policies": load_policies(raw.get("policies") or []),
        "policy_seq": int(raw.get("policy_seq") or 0),
        "revision": int(raw.get("revision") or 0),
        "closures": load_closures(raw.get("closures") or []),
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
        "combinable": r.get("combinable") or [],
    }


def created_changes(rec):
    tids = list(rec["table_ids"])
    if len(tids) == 1:
        table_change = {"field": "table_id", "from": None, "to": tids[0]}
    else:
        table_change = {"field": "table_ids", "from": None, "to": tids}
    return [
        table_change,
        {"field": "starts_at_local", "from": None, "to": rec["starts_at_local"]},
        {"field": "party_size", "from": None, "to": rec["party_size"]},
    ]


def add_history(rec, restaurant, event, changes):
    entry = {
        "seq": len(rec["history"]) + 1,
        "at": fmt_offset(datetime.now(UTC).astimezone(restaurant["_tz"])),
        "event": event,
        "changes": changes,
        "revision": rec["revision"],
        "accepted_terms": copy_terms(rec.get("accepted_terms")),
    }
    rec["history"].append(entry)
    return entry


def build_reservation(restaurant, user_id, table_ids, starts_at_local, party_size,
                      policy, aware, reference=None, reservation_id=None,
                      status="confirmed", created_at=None, revision=1,
                      accepted=None, history=None, series_id=None,
                      series_index=None, exception=False):
    end = add_absolute(aware, policy["reservation_duration_minutes"], restaurant["_tz"])
    return {
        "reservation_id": reservation_id or STORE.next_res_id(),
        "reference": reference or STORE.new_reference(),
        "user_id": user_id,
        "restaurant_id": restaurant["id"],
        "table_ids": list(table_ids),
        "party_size": party_size,
        "status": status,
        "starts_at_local": starts_at_local,
        "starts_at": aware,
        "ends_at": end,
        "created_at": created_at or datetime.now(UTC),
        "revision": revision,
        "accepted_terms": accepted if accepted is not None else terms_of(policy),
        "history": history if history is not None else [],
        "series_id": series_id,
        "series_index": series_index,
        "exception": exception,
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
        if "id" in u and (not isinstance(uid, str) or len(uid) > 64):
            raise err(422, "validation_failed", "invalid user id")
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
        sid = item.get("id")
        if "id" in item and (not isinstance(sid, str) or len(sid) > 64):
            raise err(422, "validation_failed", "invalid reservation id")
        srid = item.get("reservation_id")
        if "reservation_id" in item and (not isinstance(srid, str) or len(srid) > 64):
            raise err(422, "validation_failed", "invalid reservation_id")
        supplied_ref = item.get("reference")
        if supplied_ref is not None:
            if not isinstance(supplied_ref, str) or not re.match(r"^[A-Z0-9]{6,12}$", supplied_ref):
                raise err(422, "validation_failed", "invalid reservation reference")
            if supplied_ref in new_reservations:
                raise err(422, "validation_failed", "duplicate reservation reference")
        rid = item.get("restaurant_id")
        restaurant = new_restaurants.get(rid)
        if restaurant is None:
            continue
        tids = seeded_table_ids(item)
        if not tids or any(find_table(restaurant, x) is None for x in tids):
            continue
        sal = item.get("starts_at_local")
        ps = item.get("party_size")
        try:
            naive = datetime.strptime(sal, "%Y-%m-%dT%H:%M")
        except Exception:
            continue
        aware = resolve_local(naive, restaurant["_tz"])
        if aware is None:
            continue
        p0 = policy0(restaurant)
        rec = {
            "reservation_id": item.get("id") or item.get("reservation_id"),
            "reference": item.get("reference"),
            "user_id": item.get("user_id"),
            "restaurant_id": rid,
            "table_ids": canonical_table_ids(restaurant, tids),
            "party_size": ps,
            "status": item.get("status") or "confirmed",
            "starts_at_local": sal,
            "starts_at": aware,
            "ends_at": add_absolute(aware, p0["reservation_duration_minutes"], restaurant["_tz"]),
            "created_at": datetime.now(UTC),
            "revision": 1,
            "accepted_terms": terms_of(p0),
            "history": [],
            "series_id": None,
            "series_index": None,
            "exception": False,
        }
        if not rec["reservation_id"]:
            rec["reservation_id"] = "res_%s" % secrets.token_hex(6)
        if not rec["reference"]:
            while True:
                cand = "".join(secrets.choice(REF_ALPHABET) for _ in range(8))
                if cand not in new_reservations:
                    break
            rec["reference"] = cand
        add_history(rec, restaurant, "created", created_changes(rec))
        new_reservations[rec["reference"]] = rec

    STORE.users = new_users
    STORE.email_index = new_email_index
    STORE.tokens = {}
    STORE.restaurants = new_restaurants
    STORE.reservations = new_reservations
    STORE.idem = {}
    STORE.series = {}
    STORE.plans = {}
    STORE.user_seq = 500
    STORE.res_seq = 500
    STORE.series_seq = 0
    STORE.plan_seq = 0


def rec_to_json(rec):
    tids = list(rec["table_ids"])
    out = {
        "reservation_id": rec["reservation_id"],
        "reference": rec["reference"],
        "user_id": rec["user_id"],
        "restaurant_id": rec["restaurant_id"],
        "table_ids": tids,
        "party_size": rec["party_size"],
        "status": rec["status"],
        "starts_at_local": rec["starts_at_local"],
        "starts_at": rec["starts_at"].isoformat(),
        "ends_at": rec["ends_at"].isoformat(),
        "created_at": rec["created_at"].isoformat(),
        "revision": rec.get("revision", 1),
        "accepted_terms": copy_terms(rec.get("accepted_terms")),
        "history": [dict(e) for e in rec.get("history", [])],
        "series_id": rec.get("series_id"),
        "series_index": rec.get("series_index"),
        "exception": bool(rec.get("exception", False)),
    }
    if len(tids) == 1:
        out["table_id"] = tids[0]
    return out


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
        "combinable": r.get("combinable") or [],
        "manager_user_ids": list(r.get("manager_user_ids") or []),
        "policies": [policy_public(p) for p in r.get("policies") or []],
        "policy_seq": int(r.get("policy_seq", 0)),
        "revision": int(r.get("revision", 0)),
        "closures": [{"table_id": c["table_id"], "from": c["from"].isoformat(),
                      "to": c["to"].isoformat()} for c in r.get("closures") or []],
    }


def export_state():
    return {
        "users": {uid: dict(u) for uid, u in STORE.users.items()},
        "email_index": dict(STORE.email_index),
        "tokens": dict(STORE.tokens),
        "restaurants": {rid: rest_to_json(r) for rid, r in STORE.restaurants.items()},
        "reservations": {ref: rec_to_json(rec) for ref, rec in STORE.reservations.items()},
        "idem": {uid: {k: dict(v) for k, v in m.items()} for uid, m in STORE.idem.items()},
        "series": {sid: {k: (list(v) if isinstance(v, list) else v)
                         for k, v in s.items()} for sid, s in STORE.series.items()},
        "user_seq": STORE.user_seq,
        "res_seq": STORE.res_seq,
        "series_seq": STORE.series_seq,
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
            raw_ids = rec.get("table_ids")
            if not isinstance(raw_ids, list):
                raw_ids = [rec["table_id"]] if rec.get("table_id") is not None else []
            tids = [x for x in raw_ids if isinstance(x, str)]
            r = {
                "reservation_id": rec["reservation_id"],
                "reference": rec["reference"],
                "user_id": rec["user_id"],
                "restaurant_id": rec["restaurant_id"],
                "table_ids": tids,
                "party_size": rec["party_size"],
                "status": rec["status"],
                "starts_at_local": rec["starts_at_local"],
                "starts_at": datetime.fromisoformat(rec["starts_at"]),
                "ends_at": datetime.fromisoformat(rec["ends_at"]),
                "created_at": datetime.fromisoformat(rec["created_at"]),
                "revision": int(rec.get("revision") or 1),
                "series_id": rec.get("series_id"),
                "series_index": rec.get("series_index"),
                "exception": bool(rec.get("exception", False)),
            }
        except Exception:
            raise err(422, "validation_failed", "invalid state")
        rest = new_restaurants.get(r["restaurant_id"])
        at = rec.get("accepted_terms")
        if isinstance(at, dict):
            r["accepted_terms"] = copy_terms(at)
        elif rest is not None:
            r["accepted_terms"] = terms_of(policy0(rest))
        else:
            r["accepted_terms"] = None
        hist = rec.get("history")
        if isinstance(hist, list) and hist:
            r["history"] = hist
        elif rest is not None:
            if r["revision"] != 1:
                r["revision"] = 1
            r["history"] = []
            add_history(r, rest, "created", created_changes(r))
        else:
            r["history"] = []
        new_reservations[ref] = r

    new_idem = {}
    for uid, m in (state.get("idem") or {}).items():
        if not isinstance(m, dict):
            raise err(422, "validation_failed", "invalid state")
        new_idem[uid] = {k: dict(v) for k, v in m.items()}

    new_series = {}
    for sid, s in (state.get("series") or {}).items():
        if isinstance(s, dict):
            new_series[sid] = {k: (list(v) if isinstance(v, list) else v)
                               for k, v in s.items()}

    STORE.users = new_users
    STORE.email_index = dict(state.get("email_index") or {})
    STORE.tokens = dict(state["tokens"])
    STORE.restaurants = new_restaurants
    STORE.reservations = new_reservations
    STORE.idem = new_idem
    STORE.series = new_series
    STORE.plans = {}
    STORE.user_seq = int(state.get("user_seq") or 500)
    STORE.res_seq = int(state.get("res_seq") or 500)
    STORE.series_seq = int(state.get("series_seq") or 0)
    STORE.plan_seq = 0


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


def _idem_store_key(method, path, key):
    # Namespace each receipt by method+path+key so a key used on one path is
    # invisible on another (spec §7: same key + same body on a different path
    # is a different request, not a replay).
    return "%s\x00%s\x00%s" % (method, path, key)


def check_replay(user_id, key, method, path, body):
    """Return (replayed_response_or_None).  Raises idempotency_key_reuse."""
    user_map = STORE.idem.get(user_id, {})
    rec = user_map.get(_idem_store_key(method, path, key))
    if rec is None:
        return None
    if rec["body"] == canonical(body):
        return rec["response"]
    raise err(409, "idempotency_key_reuse", "key already used with a different body")


def store_result(user_id, key, method, path, body, status, response):
    STORE.idem.setdefault(user_id, {})[_idem_store_key(method, path, key)] = {
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


def do_signup(email, password, display_name):
    if not isinstance(email, str) or not RE_EMAIL.match(email):
        raise err(422, "validation_failed", "invalid email")
    if not isinstance(password, str) or len(password) < 8:
        raise err(422, "validation_failed", "password too short")
    if email in STORE.email_index:
        raise err(409, "email_taken", "email already registered")
    uid = STORE.next_user_id()
    STORE.users[uid] = {
        "id": uid, "email": email, "password_hash": hash_password(password),
        "display_name": display_name if display_name is not None else email,
    }
    STORE.email_index[email] = uid
    token = secrets.token_urlsafe(32)
    STORE.tokens[token] = uid
    return uid, STORE.users[uid]["display_name"], token


def do_login(email, password):
    uid = STORE.email_index.get(email) if isinstance(email, str) else None
    if uid is None:
        raise err(401, "unauthenticated", "unknown email or password")
    user = STORE.users.get(uid)
    if (user is None or not isinstance(password, str)
            or not verify_password(password, user["password_hash"])):
        raise err(401, "unauthenticated", "unknown email or password")
    token = secrets.token_urlsafe(32)
    STORE.tokens[token] = uid
    return uid, user["display_name"], token


def handle_signup(raw):
    body = require_object(raw)
    email = get_str_field(body, "email")
    password = get_str_field(body, "password")
    display_name = get_str_field(body, "display_name")
    uid, name, token = do_signup(email, password, display_name)
    return 201, {"user_id": uid, "display_name": name, "token": token}


def handle_login(raw):
    body = require_object(raw)
    email = get_str_field(body, "email")
    password = get_str_field(body, "password")
    uid, name, token = do_login(email, password)
    return 200, {"user_id": uid, "display_name": name, "token": token}


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

    explain_raw = first(qs, "explain")
    explain = False
    if explain_raw is not None:
        if explain_raw != "true":
            raise err(422, "validation_failed", "explain must be true")
        explain = True

    r = STORE.restaurants.get(rid)
    if r is None:
        raise err(404, "not_found", "no such restaurant")

    tz = r["_tz"]
    policy = select_policy(r, date_s)
    caps = policy.get("capacities") or {}
    weekday = WEEKDAYS[d.weekday()]
    hours = policy_hours_map(policy).get(weekday)
    slots = []
    if hours is not None:
        step = policy["slot_minutes"]
        duration = policy["reservation_duration_minutes"]
        tod = hours["opens_min"]
        while tod + duration <= hours["closes_min"]:
            naive = datetime.combine(d, _time(tod // 60, tod % 60))
            aware = resolve_local(naive, tz)
            if aware is not None:
                end = add_absolute(aware, duration, tz)
                available = []
                options = []
                explain_list = []
                for t in r["tables"]:
                    cap = caps.get(t["id"])
                    cap_ok = (isinstance(cap, int) and not isinstance(cap, bool)
                              and cap >= party_size)
                    no_overlap = (not any_table_busy(rid, [t["id"]], aware, end)
                                  and not closure_blocks(r, [t["id"]], aware, end))
                    avail = cap_ok and no_overlap
                    if avail:
                        available.append(t["id"])
                        options.append({"table_ids": [t["id"]], "capacity": cap})
                    if explain:
                        explain_list.append({
                            "table_id": t["id"],
                            "policy_version": policy["policy_version"],
                            "available": avail,
                            "rules": [{"rule": "capacity", "holds": cap_ok},
                                      {"rule": "no_overlap", "holds": no_overlap}],
                        })
                for pair in r.get("combinable") or []:
                    ca, cb = caps.get(pair[0]), caps.get(pair[1])
                    if not isinstance(ca, int) or not isinstance(cb, int):
                        continue
                    if ca + cb < party_size:
                        continue
                    if any_table_busy(rid, [pair[0], pair[1]], aware, end):
                        continue
                    if closure_blocks(r, [pair[0], pair[1]], aware, end):
                        continue
                    options.append({"table_ids": [pair[0], pair[1]], "capacity": ca + cb})
                slot = {
                    "starts_at_local": naive.strftime("%Y-%m-%dT%H:%M"),
                    "starts_at": fmt_offset(aware),
                    "available_table_ids": available,
                    "available_options": options,
                }
                if explain:
                    slot["explain"] = explain_list
                slots.append(slot)
            tod += step

    return 200, {"restaurant_id": rid, "date": date_s, "timezone": r["timezone"],
                 "slots": slots}


def resolve_table_set(body):
    """Return the requested table ids from ``table_id`` or ``table_ids``."""
    has_single = "table_id" in body
    has_multi = "table_ids" in body
    if has_single and has_multi:
        raise err(422, "validation_failed", "use table_id or table_ids, not both")
    if has_multi:
        v = body["table_ids"]
        if not isinstance(v, list):
            raise err(400, "malformed_request", "table_ids must be a list")
        ids = []
        for x in v:
            if not isinstance(x, str):
                raise err(400, "malformed_request", "table_ids must be strings")
            ids.append(x)
        if not ids:
            raise err(422, "validation_failed", "table_ids must not be empty")
        return ids
    if has_single:
        v = body["table_id"]
        if not isinstance(v, str):
            raise err(400, "malformed_request", "table_id must be a string")
        return [v]
    raise err(422, "validation_failed", "table_id or table_ids is required")


def validate_table_set(restaurant, table_ids, policy=None):
    """Validate a requested table set; return (canonical ids, total capacity)."""
    if len(table_ids) != len(set(table_ids)):
        raise err(422, "validation_failed", "duplicate table id")
    if len(table_ids) > 2:
        raise err(422, "combination_not_allowed", "at most two tables may be combined")
    for tid in table_ids:
        if find_table(restaurant, tid) is None:
            raise err(404, "not_found", "no such table")
    if len(table_ids) == 2 and not is_declared_pair(restaurant, table_ids):
        raise err(422, "combination_not_allowed", "tables are not combinable")
    if policy is not None:
        total = policy_capacity(policy, table_ids)
    else:
        total = 0
        for tid in table_ids:
            cap = (find_table(restaurant, tid) or {}).get("capacity")
            if isinstance(cap, int) and not isinstance(cap, bool):
                total += cap
    return canonical_table_ids(restaurant, table_ids), total


def handle_create_reservation(user_id, raw, headers):
    body = require_object(raw)
    key = idempotency_header(headers)
    replay = check_replay(user_id, key, "POST", "/reservations", body)
    if replay is not None:
        return 200, replay

    rid = body.get("restaurant_id")
    sal = body.get("starts_at_local")
    ps = body.get("party_size")

    if rid is None:
        raise err(422, "validation_failed", "restaurant_id is required")
    if not isinstance(rid, str):
        raise err(400, "malformed_request", "restaurant_id must be a string")
    tids = resolve_table_set(body)
    if sal is None:
        raise err(422, "validation_failed", "starts_at_local is required")
    validate_party_size(ps)

    restaurant = get_restaurant(rid)
    tids, _ = validate_table_set(restaurant, tids)
    naive, aware = resolve_and_check_time(restaurant, sal)
    policy = select_policy(restaurant, sal[:10])
    check_opening_policy(policy, naive)
    capacity = policy_capacity(policy, tids)
    if ps > capacity:
        raise err(422, "party_exceeds_capacity", "party exceeds table capacity")

    end = add_absolute(aware, policy["reservation_duration_minutes"], restaurant["_tz"])
    if any_table_busy(rid, tids, aware, end):
        raise err(409, "table_unavailable", "table is not available")
    if closure_blocks(restaurant, tids, aware, end):
        raise err(409, "table_unavailable", "table is not available")

    rec = build_reservation(restaurant, user_id, tids, sal, ps, policy, aware)
    add_history(rec, restaurant, "created", created_changes(rec))
    STORE.reservations[rec["reference"]] = rec
    bump_revision(restaurant)
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
    cutoff = (rec.get("accepted_terms") or {}).get("cancellation_cutoff_minutes", 0)
    if datetime.now(UTC) >= minus_absolute(rec["starts_at"], cutoff):
        raise err(409, "cutoff_passed", "cancellation cutoff has passed")
    rec["status"] = "cancelled"
    rec["revision"] = int(rec.get("revision", 1)) + 1
    add_history(rec, restaurant, "cancelled", [])
    bump_revision(restaurant)
    sid = rec.get("series_id")
    if sid:
        series = STORE.series.get(sid)
        if series is not None:
            series["revision"] = int(series.get("revision", 1)) + 1
    return 200, public_reservation(rec)


def diff_changes(restaurant, old_tids, new_tids, old_sal, new_sal, old_ps, new_ps):
    changes = []
    if set(old_tids) != set(new_tids):
        if len(old_tids) > 1 or len(new_tids) > 1:
            changes.append({"field": "table_ids",
                            "from": canonical_table_ids(restaurant, old_tids),
                            "to": canonical_table_ids(restaurant, new_tids)})
        else:
            changes.append({"field": "table_id", "from": old_tids[0], "to": new_tids[0]})
    if old_sal != new_sal:
        changes.append({"field": "starts_at_local", "from": old_sal, "to": new_sal})
    if old_ps != new_ps:
        changes.append({"field": "party_size", "from": old_ps, "to": new_ps})
    return changes


def plan_amendment(restaurant, rec, body):
    """Validate PATCH fields against the resulting policy; mutate nothing.

    Returns (changes, tids, sal, ps, policy, aware, end)."""
    old_tids = list(rec["table_ids"])
    old_sal = rec["starts_at_local"]
    old_ps = rec["party_size"]
    tids = old_tids
    sal = old_sal
    ps = old_ps
    if "table_id" in body or "table_ids" in body:
        tids = resolve_table_set(body)
    if "party_size" in body:
        ps = validate_party_size(body["party_size"])
    if "starts_at_local" in body:
        v = body["starts_at_local"]
        if not isinstance(v, str):
            raise err(400, "malformed_request", "starts_at_local must be a string")
        sal = v

    tids, _ = validate_table_set(restaurant, tids)
    naive, aware = resolve_and_check_time(restaurant, sal)
    policy = select_policy(restaurant, sal[:10])
    check_opening_policy(policy, naive)
    if ps > policy_capacity(policy, tids):
        raise err(422, "party_exceeds_capacity", "party exceeds table capacity")
    end = add_absolute(aware, policy["reservation_duration_minutes"], restaurant["_tz"])
    changes = diff_changes(restaurant, old_tids, tids, old_sal, sal, old_ps, ps)
    return changes, tids, sal, ps, policy, aware, end


def handle_patch(user_id, reference, raw):
    body = require_object(raw)
    rec = STORE.reservations.get(reference)
    if rec is None or rec["user_id"] != user_id:
        raise err(404, "not_found", "no such reservation")
    if rec["status"] == "cancelled":
        raise err(409, "reservation_cancelled", "reservation is cancelled")
    if "expected_revision" in body:
        er = body["expected_revision"]
        if isinstance(er, bool) or not isinstance(er, int) or er < 1:
            raise err(422, "validation_failed", "expected_revision must be a positive integer")
        if er != rec.get("revision", 1):
            raise err(409, "stale_revision", "revision is stale")
    restaurant = STORE.restaurants.get(rec["restaurant_id"])
    cutoff = (rec.get("accepted_terms") or {}).get("cancellation_cutoff_minutes", 0)
    if datetime.now(UTC) >= minus_absolute(rec["starts_at"], cutoff):
        raise err(409, "cutoff_passed", "cutoff has passed")

    changes, tids, sal, ps, policy, aware, end = plan_amendment(restaurant, rec, body)
    if any_table_busy(rec["restaurant_id"], tids, aware, end, exclude_ref=reference):
        raise err(409, "table_unavailable", "table is not available")
    if closure_blocks(restaurant, tids, aware, end):
        raise err(409, "table_unavailable", "table is not available")

    if not changes:
        return 200, public_reservation(rec)

    rec["table_ids"] = tids
    rec["starts_at_local"] = sal
    rec["party_size"] = ps
    rec["starts_at"] = aware
    rec["ends_at"] = end
    rec["accepted_terms"] = terms_of(policy)
    rec["revision"] = int(rec.get("revision", 1)) + 1
    add_history(rec, restaurant, "changed", changes)
    bump_revision(restaurant)
    sid = rec.get("series_id")
    if sid:
        series = STORE.series.get(sid)
        if series is not None:
            series["revision"] = int(series.get("revision", 1)) + 1
            for occ in series.get("occurrences", []):
                if occ.get("reference") == reference:
                    occ["exception"] = True
            rec["exception"] = True
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

    # each real change uses individual PATCH semantics
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
        if "expected_revision" in move:
            er = move["expected_revision"]
            if isinstance(er, bool) or not isinstance(er, int) or er < 1:
                raise err(422, "validation_failed",
                          "expected_revision must be a positive integer")
            if er != rec.get("revision", 1):
                raise err(409, "stale_revision", "revision is stale")
        restaurant = STORE.restaurants.get(rec["restaurant_id"])
        cutoff = (rec.get("accepted_terms") or {}).get("cancellation_cutoff_minutes", 0)
        if datetime.now(UTC) >= minus_absolute(rec["starts_at"], cutoff):
            raise err(409, "cutoff_passed", "cutoff has passed")
        changes, tids, sal, ps, policy, aware, end = plan_amendment(restaurant, rec, move)
        planned.append((rec, restaurant, changes, tids, sal, ps, policy, aware, end))

    if len({rec["restaurant_id"] for rec in records}) != 1:
        raise err(422, "validation_failed", "all bookings must share a restaurant")

    # occupancy: overlap among results and against unlisted confirmed bookings
    listed_refs = set(refs)
    for i, item in enumerate(planned):
        tids, aware, end = item[3], item[7], item[8]
        for j in range(i + 1, len(planned)):
            other = planned[j]
            otids, oaware, oend = other[3], other[7], other[8]
            if (set(tids) & set(otids)) and overlaps(aware, end, oaware, oend):
                raise err(409, "table_unavailable", "resulting bookings overlap")
    for item in planned:
        rec, tids, aware, end = item[0], item[3], item[7], item[8]
        for ref, other in STORE.reservations.items():
            if ref in listed_refs:
                continue
            if other["status"] != "confirmed":
                continue
            if other["restaurant_id"] != rec["restaurant_id"]:
                continue
            if not (set(other["table_ids"]) & set(tids)):
                continue
            if overlaps(aware, end, other["starts_at"], other["ends_at"]):
                raise err(409, "table_unavailable", "table is not available")

    for item in planned:
        restaurant_i = item[1]
        if closure_blocks(restaurant_i, item[3], item[7], item[8]):
            raise err(409, "table_unavailable", "table is not available")

    # commit
    affected_series = set()
    for rec, restaurant, changes, tids, sal, ps, policy, aware, end in planned:
        if not changes:
            continue
        rec["table_ids"] = tids
        rec["starts_at_local"] = sal
        rec["party_size"] = ps
        rec["starts_at"] = aware
        rec["ends_at"] = end
        rec["accepted_terms"] = terms_of(policy)
        rec["revision"] = int(rec.get("revision", 1)) + 1
        add_history(rec, restaurant, "changed", changes)
        sid = rec.get("series_id")
        if sid:
            affected_series.add(sid)
            series = STORE.series.get(sid)
            if series is not None:
                for occ in series.get("occurrences", []):
                    if occ.get("reference") == rec["reference"]:
                        occ["exception"] = True
            rec["exception"] = True

    for sid in affected_series:
        series = STORE.series.get(sid)
        if series is not None:
            series["revision"] = int(series.get("revision", 1)) + 1

    if any(item[2] for item in planned):
        bump_revision(STORE.restaurants.get(records[0]["restaurant_id"]))
    resp = {"reservations": [public_reservation(rec) for rec in records]}
    store_result(user_id, key, "POST", "/reservation-moves", body, 201, resp)
    return 201, resp


def first(qs, name):
    vals = qs.get(name)
    if not vals:
        return None
    return vals[0]


# --------------------------------------------------------------------------
# browser UI (server-rendered shell + vanilla-JS behaviour)
# --------------------------------------------------------------------------

def h(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


CSS_TEXT = """
:root{
  --bg:#faf6f0; --panel:#fffdfa; --ink:#2b2118; --muted:#8a7a6b;
  --line:#e8dfd4; --brand:#b4531f; --brand-dark:#8f3f14;
  --avail-bg:#e7f4ea; --avail-ink:#1f5c37; --avail-border:#59a878;
  --unavail-bg:#f0ebe4; --unavail-ink:#a89a8c; --unavail-border:#ded4c8;
  --error-bg:#fdeceb; --error-ink:#9b2c1f; --error-border:#e3b3ac;
  --warn-bg:#fff5e0; --warn-ink:#8a5a00; --warn-border:#e8cf9a;
  --ok-bg:#e7f4ea; --ok-ink:#1f5c37; --ok-border:#59a878;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--ink);
  font-family:"Segoe UI",system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif;
  line-height:1.5;font-size:16px}
a{color:var(--brand-dark)}
h1{font-size:1.6rem;margin:0 0 .3rem}
.site-header{display:flex;flex-wrap:wrap;gap:.75rem 1.25rem;align-items:center;
  justify-content:space-between;padding:.9rem 1.25rem;background:var(--panel);
  border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.brand{font-weight:700;font-size:1.15rem;color:var(--brand-dark);text-decoration:none}
.site-nav{display:flex;flex-wrap:wrap;gap:.5rem .9rem;align-items:center}
.site-nav a{text-decoration:none;color:var(--ink);font-weight:500}
.site-nav a:hover{color:var(--brand-dark)}
.who{font-weight:600;color:var(--brand-dark)}
.page{max-width:1080px;margin:0 auto;padding:1.5rem 1.25rem 3rem}
.hero{margin-bottom:1rem}
.lede{color:var(--muted);margin:0 0 1rem}
.search-form{display:flex;flex-wrap:wrap;gap:1rem;align-items:flex-end;
  background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:1rem;margin-bottom:1.25rem}
.field{display:flex;flex-direction:column;gap:.3rem;min-width:8rem}
.field label{font-size:.82rem;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.04em}
input,select{font:inherit;color:var(--ink);background:#fff;border:1px solid var(--line);
  border-radius:9px;padding:.55rem .65rem;min-width:0}
input:focus,select:focus,button:focus,a:focus{outline:3px solid #f0c39a;outline-offset:2px}
.btn{font:inherit;font-weight:600;border-radius:10px;border:1px solid transparent;
  padding:.6rem 1.1rem;cursor:pointer;text-decoration:none;display:inline-block}
.btn-primary{background:var(--brand);color:#fff}
.btn-primary:hover{background:var(--brand-dark)}
.btn-ghost{background:transparent;border-color:var(--line);color:var(--ink)}
.btn-danger{background:#fff;border-color:var(--error-border);color:var(--error-ink)}
.results-title{font-weight:600;margin:.25rem 0 .9rem;color:var(--ink)}
.availability-grid{display:flex;flex-direction:column;gap:.6rem;overflow-x:auto;padding-bottom:.4rem}
.slot-row{display:flex;gap:.6rem;align-items:center;min-width:max-content}
.slot-time{flex:0 0 3.5rem;width:3.5rem;font-weight:600;color:var(--muted)}
.slot-cells{display:flex;gap:.5rem}
.slot-cell{min-width:4.5rem;padding:.5rem .7rem;border-radius:10px;border:1px solid;font:inherit;cursor:pointer}
.slot-cell.is-available{background:var(--avail-bg);color:var(--avail-ink);border-color:var(--avail-border)}
.slot-cell.is-available:hover{filter:brightness(.97)}
.slot-cell.is-unavailable{background:var(--unavail-bg);color:var(--unavail-ink);
  border-color:var(--unavail-border);cursor:not-allowed}
.empty-state{background:var(--panel);border:1px dashed var(--line);border-radius:12px;
  padding:1.2rem;color:var(--muted)}
.error-box{background:var(--error-bg);color:var(--error-ink);border:1px solid var(--error-border);
  border-radius:10px;padding:.7rem .9rem;margin:.6rem 0}
.warn-box{background:var(--warn-bg);color:var(--warn-ink);border:1px solid var(--warn-border);
  border-radius:10px;padding:.7rem .9rem;margin:.6rem 0}
.booking-form,.confirmation,.reservation-detail,.card{background:var(--panel);
  border:1px solid var(--line);border-radius:14px;padding:1.1rem;margin-top:1rem;
  box-shadow:0 1px 2px rgba(43,33,24,.05)}
.booking-summary{font-weight:600;margin-bottom:.8rem}
.booking-form .field{margin-bottom:.8rem}
.confirmation-head{font-weight:600;color:var(--ok-ink)}
.confirmation-ref{font-size:1.6rem;font-weight:700;letter-spacing:.12em;
  color:var(--brand-dark);margin:.2rem 0}
.status-badge{display:inline-block;padding:.25rem .6rem;border-radius:999px;font-weight:600;
  background:var(--ok-bg);color:var(--ok-ink);border:1px solid var(--ok-border);margin-bottom:.6rem}
.auth-card{max-width:26rem}
.stack{display:flex;flex-direction:column;gap:.8rem}
.muted{color:var(--muted)}
@media (max-width:520px){
  .site-header{padding:.7rem .9rem}
  .page{padding:1rem .9rem 2.5rem}
  .search-form{padding:.8rem}
  h1{font-size:1.35rem}
}
"""


JS_TEXT = """
(function () {
  "use strict";
  var signedIn = document.body.getAttribute("data-signed-in") === "1";
  var state = { seq: 0, restaurant: null, date: null, party: 1 };

  function qs(sel, root) { return (root || document).querySelector(sel); }
  function byTestid(name) { return qs('[data-testid="' + name + '"]'); }
  function findTable(rest, id) {
    if (!rest || !rest.tables) return null;
    for (var i = 0; i < rest.tables.length; i++) if (rest.tables[i].id === id) return rest.tables[i];
    return null;
  }
  function labelOf(rest, id) {
    var t = findTable(rest, id);
    return (t && t.label != null) ? String(t.label) : String(id);
  }
  function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }
  function sameSet(a, b) {
    if (!a || !b || a.length !== b.length) return false;
    var s = {}; a.forEach(function (x) { s[x] = 1; });
    return b.every(function (x) { return s[x] === 1; });
  }
  function makeKey() {
    try { if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID(); } catch (e) {}
    var s = "", c = "abcdefghijklmnopqrstuvwxyz0123456789";
    for (var i = 0; i < 32; i++) s += c.charAt(Math.floor(Math.random() * c.length));
    return s;
  }
  function el(tag, cls, testid) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (testid) e.setAttribute("data-testid", testid);
    return e;
  }

  /* ---- search and availability ---- */
  var searchForm = document.getElementById("search-form");
  if (searchForm) {
    searchForm.addEventListener("submit", function (ev) { ev.preventDefault(); runSearch(); });
  }
  function setResultsTitle(party, date, rest) {
    var t = qs(".results-title");
    if (!t) return;
    t.hidden = false;
    t.textContent = "Availability for " + party + (party === 1 ? " guest" : " guests")
      + " at " + rest.name + " on " + date;
  }
  function showAuthError(msg) {
    var mount = document.getElementById("auth-mount");
    if (!mount) return;
    var box = byTestid("auth-error");
    if (!box) { box = el("div", "error-box", "auth-error"); mount.appendChild(box); }
    box.textContent = msg;
  }
  function clearAuthError() { var b = byTestid("auth-error"); if (b) b.remove(); }
  function notice(container, message) {
    if (!container) return;
    container.innerHTML = "";
    var d = el("div", "empty-state"); d.textContent = message; container.appendChild(d);
  }
  function runSearch() {
    var rid = document.getElementById("restaurant-select").value;
    var date = document.getElementById("date-input").value;
    var party = parseInt(document.getElementById("party-size-input").value, 10);
    if (!party || party < 1) party = 1;
    state.date = date; state.party = party;
    clearBookingForm();
    var seq = ++state.seq;
    var params = "restaurant_id=" + encodeURIComponent(rid)
      + "&date=" + encodeURIComponent(date) + "&party_size=" + party;
    Promise.all([
      fetch("/restaurants/" + encodeURIComponent(rid)),
      fetch("/availability?" + params)
    ]).then(function (res) {
      if (seq !== state.seq) return null;
      return Promise.all([res[0].ok ? res[0].json() : null, res[1].ok ? res[1].json() : null]);
    }).then(function (data) {
      if (!data || seq !== state.seq) return;
      var rest = data[0], avail = data[1];
      if (!rest) { notice(document.getElementById("grid-mount"), "We could not find that restaurant."); return; }
      state.restaurant = rest;
      setResultsTitle(party, date, rest);
      renderGrid(avail, rest);
    }).catch(function () {
      if (seq !== state.seq) return;
      notice(document.getElementById("grid-mount"), "We could not load availability. Please try again.");
    });
  }
  function refreshAvailability() {
    if (!state.restaurant) return;
    var seq = ++state.seq;
    var params = "restaurant_id=" + encodeURIComponent(state.restaurant.id)
      + "&date=" + encodeURIComponent(state.date) + "&party_size=" + state.party;
    fetch("/availability?" + params)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data && seq === state.seq) renderGrid(data, state.restaurant); })
      .catch(function () {});
  }
  function renderGrid(avail, rest) {
    var mount = document.getElementById("grid-mount");
    if (!mount) return;
    mount.innerHTML = "";
    var slots = (avail && avail.slots) || [];
    if (!slots.length) {
      var ns = el("div", "empty-state", "no-slots");
      ns.textContent = "No tables are available for this date. Try another day or party size.";
      mount.appendChild(ns);
      return;
    }
    var grid = el("div", "availability-grid", "availability-grid");
    var pairs = (rest.combinable || []).filter(function (p) {
      var a = findTable(rest, p[0]), b = findTable(rest, p[1]);
      return a && b && (num(a.capacity) + num(b.capacity)) >= state.party;
    });
    slots.forEach(function (slot) {
      var hhmm = String(slot.starts_at_local).slice(-5);
      var row = el("div", "slot-row");
      var time = el("div", "slot-time"); time.textContent = hhmm; row.appendChild(time);
      var cells = el("div", "slot-cells");
      (rest.tables || []).forEach(function (t) {
        var available = (slot.available_table_ids || []).indexOf(t.id) >= 0;
        cells.appendChild(makeCell("slot-" + t.id + "-" + hhmm, available,
          labelOf(rest, t.id), [t.id], slot, rest));
      });
      pairs.forEach(function (p) {
        var opt = (slot.available_options || []).some(function (o) { return sameSet(o.table_ids, p); });
        var lbl = labelOf(rest, p[0]) + " + " + labelOf(rest, p[1]);
        cells.appendChild(makeCell("slot-" + p[0] + "+" + p[1] + "-" + hhmm, opt, lbl,
          [p[0], p[1]], slot, rest));
      });
      row.appendChild(cells);
      grid.appendChild(row);
    });
    mount.appendChild(grid);
  }
  function makeCell(testid, available, label, ids, slot, rest) {
    var b = el("button", "slot-cell " + (available ? "is-available" : "is-unavailable"), testid);
    b.type = "button";
    b.setAttribute("data-available", available ? "true" : "false");
    b.textContent = label;
    if (available) {
      b.addEventListener("click", function () { openBookingForm(ids, slot, rest); });
    } else {
      b.disabled = true;
    }
    return b;
  }
  function clearBookingForm() {
    var m = document.getElementById("booking-mount");
    if (m) m.innerHTML = "";
    var c = document.getElementById("confirmation-mount");
    if (c) c.innerHTML = "";
    clearAuthError();
  }
  function openBookingForm(ids, slot, rest) {
    if (!signedIn) { showAuthError("Please log in or sign up to book a table."); return; }
    clearAuthError();
    var mount = document.getElementById("booking-mount");
    mount.innerHTML = "";
    var conf = document.getElementById("confirmation-mount");
    if (conf) conf.innerHTML = "";
    var form = el("div", "booking-form", "booking-form");
    var labels = ids.map(function (id) { return labelOf(rest, id); }).join(" + ");
    var summary = el("div", "booking-summary", "booking-summary");
    summary.textContent = "Table " + labels + " \\u00b7 " + String(slot.starts_at_local).slice(-5)
      + " \\u00b7 " + rest.name;
    form.appendChild(summary);
    var field = el("div", "field");
    var lab = document.createElement("label");
    lab.setAttribute("for", "booking-party-size"); lab.textContent = "Party size";
    var input = document.createElement("input");
    input.type = "number"; input.min = "1"; input.id = "booking-party-size";
    input.setAttribute("data-testid", "booking-party-size");
    input.value = String(state.party);
    field.appendChild(lab); field.appendChild(input);
    form.appendChild(field);
    var submit = el("button", "btn btn-primary", "booking-submit");
    submit.type = "button"; submit.textContent = "Confirm booking";
    form.appendChild(submit);
    var ctx = { ids: ids, slot: slot, restaurant: rest, key: makeKey() };
    input.addEventListener("input", function () { ctx.key = makeKey(); });
    submit.addEventListener("click", function () { submitBooking(form, ctx); });
    form._ctx = ctx;
    mount.appendChild(form);
  }
  function bookingError(form, msg) {
    var box = form.querySelector('[data-testid="booking-error"]');
    if (!box) { box = el("div", "error-box", "booking-error"); form.appendChild(box); }
    box.textContent = msg;
  }
  function clearBookingError(form) { var b = form.querySelector('[data-testid="booking-error"]'); if (b) b.remove(); }
  function uncertain(form, msg) {
    var box = form.querySelector('[data-testid="booking-uncertain"]');
    if (!box) { box = el("div", "warn-box", "booking-uncertain"); form.appendChild(box); }
    box.textContent = msg;
  }
  function clearUncertain(form) { var b = form.querySelector('[data-testid="booking-uncertain"]'); if (b) b.remove(); }
  function submitBooking(form, ctx) {
    var mount = document.getElementById("confirmation-mount");
    if (mount) mount.innerHTML = "";
    clearBookingError(form);
    clearUncertain(form);
    var input = form.querySelector('[data-testid="booking-party-size"]');
    var party = parseInt(input.value, 10);
    if (!party || party < 1) { bookingError(form, "Please enter a valid party size."); return; }
    var body = {
      restaurant_id: ctx.restaurant.id,
      table_ids: ctx.ids,
      starts_at_local: ctx.slot.starts_at_local,
      party_size: party
    };
    fetch("/reservations", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": ctx.key },
      body: JSON.stringify(body)
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; })
        .then(function (payload) { return { ok: resp.ok, status: resp.status, payload: payload }; });
    }).then(function (r) {
      if (r.ok) {
        clearUncertain(form); clearBookingError(form);
        showConfirmation(r.payload, ctx.restaurant);
      } else {
        var err = r.payload && r.payload.error ? r.payload.error : {};
        bookingError(form, err.message || "That table could not be booked.");
        if (r.status === 409 && err.code === "table_unavailable") refreshAvailability();
      }
    }).catch(function () {
      uncertain(form, "We could not confirm your booking \\u2014 the connection dropped. "
        + "Press Confirm booking again to retry; your request will not be duplicated.");
    });
  }
  function showConfirmation(data, rest) {
    var mount = document.getElementById("confirmation-mount");
    if (!mount) return;
    mount.innerHTML = "";
    var box = el("div", "confirmation", "confirmation");
    var head = el("div", "confirmation-head"); head.textContent = "Booking confirmed";
    box.appendChild(head);
    var ref = el("div", "confirmation-ref", "confirmation-reference");
    ref.textContent = String(data.reference);
    box.appendChild(ref);
    var ids = data.table_ids || (data.table_id ? [data.table_id] : []);
    var labels = ids.map(function (id) { return labelOf(rest, id); }).join(" + ");
    var details = el("div", "confirmation-details", "confirmation-details");
    details.textContent = rest.name + " \\u00b7 Table " + labels + " \\u00b7 "
      + String(data.starts_at_local).slice(-5);
    box.appendChild(details);
    var tables = el("div", "confirmation-tables", "confirmation-tables");
    tables.textContent = "Tables: " + labels;
    box.appendChild(tables);
    mount.appendChild(box);
  }

  /* ---- lookup ---- */
  var lookupForm = document.getElementById("lookup-form");
  if (lookupForm) {
    lookupForm.addEventListener("submit", function (ev) { ev.preventDefault(); runLookup(); });
  }
  function runLookup() {
    var ref = document.getElementById("lookup-reference-input").value.trim();
    var mount = document.getElementById("lookup-result");
    mount.innerHTML = "";
    fetch("/reservations/" + encodeURIComponent(ref)).then(function (resp) {
      return resp.json().catch(function () { return {}; })
        .then(function (payload) { return { ok: resp.ok, payload: payload }; });
    }).then(function (r) {
      if (!r.ok) { lookupError(mount); return; }
      var data = r.payload;
      return fetch("/restaurants/" + encodeURIComponent(data.restaurant_id))
        .then(function (rr) { return rr.ok ? rr.json() : null; })
        .then(function (rest) { renderReservation(mount, data, rest); });
    }).catch(function () { lookupError(mount); });
  }
  function lookupError(mount) {
    mount.innerHTML = "";
    var box = el("div", "error-box", "reservation-error");
    box.textContent = "We could not find a reservation with that reference.";
    mount.appendChild(box);
  }
  function renderReservation(mount, data, rest) {
    mount.innerHTML = "";
    var box = el("div", "reservation-detail", "reservation-detail");
    var status = el("div", "status-badge", "reservation-status");
    status.textContent = String(data.status);
    box.appendChild(status);
    var ids = data.table_ids || (data.table_id ? [data.table_id] : []);
    var labels = ids.map(function (id) { return labelOf(rest, id); }).join(" + ");
    var meta = el("div", "reservation-meta");
    meta.textContent = (rest ? rest.name : data.restaurant_id) + " \\u00b7 "
      + String(data.starts_at_local).slice(-5) + " \\u00b7 " + data.party_size + " guests";
    box.appendChild(meta);
    var tables = el("div", "reservation-tables", "reservation-tables");
    tables.textContent = "Tables: " + labels;
    box.appendChild(tables);
    if (data.status !== "cancelled") {
      var btn = el("button", "btn btn-danger", "reservation-cancel-button");
      btn.type = "button"; btn.textContent = "Cancel reservation";
      btn.addEventListener("click", function () {
        btn.disabled = true;
        fetch("/reservations/" + encodeURIComponent(data.reference) + "/cancel", { method: "POST" })
          .then(function (resp) {
            return resp.json().catch(function () { return {}; })
              .then(function (p) { return { ok: resp.ok, payload: p }; });
          }).then(function (r) {
            if (r.ok) {
              status.textContent = String(r.payload.status);
              var b = box.querySelector('[data-testid="reservation-cancel-button"]');
              if (b) b.remove();
            } else {
              btn.disabled = false;
              var eb = el("div", "error-box", "reservation-error");
              eb.textContent = "This reservation can no longer be cancelled.";
              box.appendChild(eb);
            }
          }).catch(function () { btn.disabled = false; });
      });
      box.appendChild(btn);
    }
    mount.appendChild(box);
  }
})();
"""


def account_html(user):
    if user:
        return ('<span class="who" data-testid="current-user">%s</span>'
                '<a class="btn btn-ghost" data-testid="logout-button" href="/logout">Log out</a>'
                % h(user["display_name"]))
    return ('<a class="btn btn-ghost" href="/login">Log in</a>'
            '<a class="btn btn-primary" href="/signup">Sign up</a>')


def page_shell(title, user, body):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s · Tablekeeper</title>
<link rel="stylesheet" href="/static/app.css">
</head>
<body data-signed-in="%s" data-user="%s">
<header class="site-header">
  <a class="brand" href="/">Tablekeeper</a>
  <nav class="site-nav">
    <a href="/">Find a table</a>
    <a href="/lookup">My booking</a>
    %s
  </nav>
</header>
<main class="page">
%s
</main>
<script src="/static/app.js"></script>
</body>
</html>""" % (h(title), "1" if user else "0",
               h(user["display_name"]) if user else "", account_html(user), body)


def render_search_page(user):
    options = "".join('<option value="%s">%s</option>' % (h(r["id"]), h(r["name"]))
                      for r in STORE.restaurants.values())
    body = """
<section class="hero">
  <h1>Find your table</h1>
  <p class="lede">Search availability, choose a table, and book in seconds.</p>
</section>
<form id="search-form" class="search-form" autocomplete="off">
  <div class="field">
    <label for="restaurant-select">Restaurant</label>
    <select id="restaurant-select" data-testid="restaurant-select" aria-label="Restaurant">%s</select>
  </div>
  <div class="field">
    <label for="date-input">Date</label>
    <input id="date-input" data-testid="date-input" type="date" aria-label="Date">
  </div>
  <div class="field">
    <label for="party-size-input">Party size</label>
    <input id="party-size-input" data-testid="party-size-input" type="number" min="1"
           value="2" aria-label="Party size">
  </div>
  <button type="submit" class="btn btn-primary" data-testid="search-button">Search</button>
</form>
<div id="auth-mount"></div>
<div class="results">
  <div class="results-title" hidden></div>
  <div id="grid-mount"></div>
  <div id="booking-mount"></div>
  <div id="confirmation-mount"></div>
</div>
""" % options
    return page_shell("Find a table", user, body)


def render_login_page(user, error=None, email=""):
    err_html = ('<div class="error-box" data-testid="auth-error">%s</div>' % h(error)) if error else ""
    body = """
<section class="card auth-card">
  <h1>Log in</h1>
  %s
  <form method="post" action="/login" class="stack">
    <div class="field">
      <label for="login-email">Email</label>
      <input id="login-email" data-testid="login-email" name="email" type="email"
             value="%s" required>
    </div>
    <div class="field">
      <label for="login-password">Password</label>
      <input id="login-password" data-testid="login-password" name="password"
             type="password" required>
    </div>
    <button type="submit" class="btn btn-primary" data-testid="login-submit">Log in</button>
  </form>
  <p class="muted">New here? <a href="/signup">Create an account</a>.</p>
</section>
""" % (err_html, h(email))
    return page_shell("Log in", user, body)


def render_signup_page(user, error=None, email="", name=""):
    err_html = ('<div class="error-box" data-testid="auth-error">%s</div>' % h(error)) if error else ""
    body = """
<section class="card auth-card">
  <h1>Create your account</h1>
  %s
  <form method="post" action="/signup" class="stack">
    <div class="field">
      <label for="signup-display-name">Your name</label>
      <input id="signup-display-name" data-testid="signup-display-name" name="display_name"
             type="text" value="%s" required>
    </div>
    <div class="field">
      <label for="signup-email">Email</label>
      <input id="signup-email" data-testid="signup-email" name="email" type="email"
             value="%s" required>
    </div>
    <div class="field">
      <label for="signup-password">Password</label>
      <input id="signup-password" data-testid="signup-password" name="password"
             type="password" minlength="8" required>
    </div>
    <button type="submit" class="btn btn-primary" data-testid="signup-submit">Sign up</button>
  </form>
  <p class="muted">Already have an account? <a href="/login">Log in</a>.</p>
</section>
""" % (err_html, h(name), h(email))
    return page_shell("Sign up", user, body)


def render_lookup_page(user):
    body = """
<section class="hero">
  <h1>Find my booking</h1>
  <p class="lede">Enter the confirmation reference from your booking.</p>
</section>
<form id="lookup-form" class="search-form" autocomplete="off">
  <div class="field">
    <label for="lookup-reference-input">Confirmation reference</label>
    <input id="lookup-reference-input" data-testid="lookup-reference-input" type="text"
           aria-label="Confirmation reference" placeholder="e.g. K3P7QW">
  </div>
  <button type="submit" class="btn btn-primary" data-testid="lookup-submit">Look up</button>
</form>
<div id="lookup-result"></div>
"""
    return page_shell("My booking", user, body)


def html_response(text):
    return 200, "text/html; charset=utf-8", text.encode("utf-8"), []


def redirect_with_cookie(token, location):
    return (303, "text/html; charset=utf-8", b"",
            [("Set-Cookie", "tk_token=%s; Path=/; HttpOnly; SameSite=Lax" % token),
             ("Location", location)])


def parse_form(raw):
    try:
        text = raw.decode("utf-8") if raw else ""
    except Exception:
        text = ""
    parsed = parse_qs(text, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def current_user(headers):
    token = cookie_value(headers.get("Cookie"), "tk_token")
    if not token:
        return None
    uid = STORE.tokens.get(token)
    if uid is None:
        return None
    return STORE.users.get(uid)


def try_page(method, path, raw, headers):
    """Serve the HTML screens and static assets.

    Returns (status, content_type, body_bytes, extra_headers) for a screen
    route, or None when the request belongs to the JSON API.
    """
    if method == "GET" and path == "/static/app.css":
        return 200, "text/css; charset=utf-8", CSS_TEXT.encode("utf-8"), []
    if method == "GET" and path == "/static/app.js":
        return 200, "application/javascript; charset=utf-8", JS_TEXT.encode("utf-8"), []

    user = current_user(headers)
    if method == "GET" and path == "/":
        return html_response(render_search_page(user))
    if method == "GET" and path == "/login":
        return html_response(render_login_page(user))
    if method == "GET" and path == "/signup":
        return html_response(render_signup_page(user))
    if method == "GET" and path == "/lookup":
        return html_response(render_lookup_page(user))
    if method == "GET" and path == "/logout":
        return (303, "text/html; charset=utf-8", b"",
                [("Set-Cookie", "tk_token=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"),
                 ("Location", "/")])

    if method == "POST" and path in ("/login", "/signup"):
        form = parse_form(raw)
        email = form.get("email", "")
        password = form.get("password", "")
        if path == "/signup":
            name = form.get("display_name", "")
            try:
                uid, disp, token = do_signup(email, password, name)
            except ApiError as e:
                return html_response(render_signup_page(user, error=e.message,
                                                        email=email, name=name))
            return redirect_with_cookie(token, "/")
        try:
            uid, disp, token = do_login(email, password)
        except ApiError as e:
            return html_response(render_login_page(user, error=e.message, email=email))
        return redirect_with_cookie(token, "/")

    return None


# --------------------------------------------------------------------------
# history, decision, policies and series (stage 3)
# --------------------------------------------------------------------------

def resolve_optional_user(headers):
    try:
        token = bearer_token(headers)
    except ApiError:
        return None
    if not token:
        return None
    uid = STORE.tokens.get(token)
    return uid if uid in STORE.users else None


def owned_reservation_or_404(headers, reference):
    uid = resolve_optional_user(headers)
    rec = STORE.reservations.get(reference)
    if uid is None or rec is None or rec["user_id"] != uid:
        raise err(404, "not_found", "no such reservation")
    return rec


def handle_history(headers, reference):
    rec = owned_reservation_or_404(headers, reference)
    return 200, {"reference": rec["reference"],
                 "entries": [dict(e) for e in rec["history"]]}


def handle_decision(headers, reference):
    rec = owned_reservation_or_404(headers, reference)
    return 200, {"reference": rec["reference"], "revision": rec["revision"],
                 "accepted_terms": copy_terms(rec["accepted_terms"])}


def validate_policy_body(restaurant, body):
    if "effective_from" not in body:
        raise err(422, "validation_failed", "effective_from is required")
    ef = body["effective_from"]
    if not isinstance(ef, str) or not RE_DATE.match(ef):
        raise err(422, "validation_failed", "effective_from must be YYYY-MM-DD")
    try:
        _date.fromisoformat(ef)
    except Exception:
        raise err(422, "validation_failed", "invalid effective_from")

    def int_field(name, lo, hi):
        if name not in body:
            raise err(422, "validation_failed", "%s is required" % name)
        v = body[name]
        if isinstance(v, bool) or not isinstance(v, int):
            raise err(422, "validation_failed", "%s must be an integer" % name)
        if v < lo or v > hi:
            raise err(422, "validation_failed", "%s is out of range" % name)
        return v

    slot_minutes = int_field("slot_minutes", 1, 1440)
    duration = int_field("reservation_duration_minutes", 1, 1440)
    cutoff = int_field("cancellation_cutoff_minutes", 0, 10080)

    raw_hours = body.get("opening_hours")
    if not isinstance(raw_hours, list):
        raise err(422, "validation_failed", "opening_hours must be a list")
    hours = []
    seen = set()
    for entry in raw_hours:
        if not isinstance(entry, dict):
            raise err(422, "validation_failed", "invalid opening_hours entry")
        wk = entry.get("weekday")
        if wk not in WEEKDAYS or wk in seen:
            raise err(422, "validation_failed", "invalid or duplicate weekday")
        seen.add(wk)
        om = parse_hhmm(entry.get("opens"))
        cm = parse_hhmm(entry.get("closes"))
        if om is None or cm is None or cm <= om:
            raise err(422, "validation_failed", "invalid opening hours")
        hours.append({"weekday": wk, "opens": entry["opens"], "closes": entry["closes"]})

    caps_raw = body.get("capacities")
    if not isinstance(caps_raw, dict):
        raise err(422, "validation_failed", "capacities must be an object")
    table_ids = [t["id"] for t in restaurant["tables"]]
    if set(caps_raw.keys()) != set(table_ids):
        raise err(422, "validation_failed", "capacities must name exactly the table ids")
    caps = {}
    for tid in table_ids:
        v = caps_raw[tid]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1 or v > 100:
            raise err(422, "validation_failed", "invalid capacity")
        caps[tid] = v

    return {
        "effective_from": ef,
        "slot_minutes": slot_minutes,
        "reservation_duration_minutes": duration,
        "cancellation_cutoff_minutes": cutoff,
        "opening_hours": hours,
        "capacities": caps,
    }


def handle_create_policy(user_id, rid, raw, headers, path):
    restaurant = STORE.restaurants.get(rid)
    if restaurant is None:
        raise err(404, "not_found", "no such restaurant")
    if user_id not in (restaurant.get("manager_user_ids") or []):
        raise err(403, "forbidden", "not a manager")
    body = require_object(raw)
    key = idempotency_header(headers)
    replay = check_replay(user_id, key, "POST", path, body)
    if replay is not None:
        return 200, replay
    policy = validate_policy_body(restaurant, body)
    restaurant["policy_seq"] = int(restaurant.get("policy_seq", 0)) + 1
    policy["policy_version"] = restaurant["policy_seq"]
    restaurant.setdefault("policies", []).append(policy)
    bump_revision(restaurant)
    resp = policy_public(policy)
    store_result(user_id, key, "POST", path, body, 201, resp)
    return 201, resp


def handle_list_policies(rid):
    restaurant = STORE.restaurants.get(rid)
    if restaurant is None:
        raise err(404, "not_found", "no such restaurant")
    return 200, {"policies": [policy_public(p) for p in restaurant.get("policies") or []]}


def series_public(series):
    occ = []
    for o in series.get("occurrences", []):
        rec = STORE.reservations.get(o["reference"])
        occ.append({
            "index": o["index"],
            "reference": o["reference"],
            "exception": bool(o.get("exception", False)),
            "reservation": public_reservation(rec) if rec is not None else None,
        })
    return {"series_id": series["series_id"], "revision": series["revision"],
            "interval_weeks": series["interval_weeks"], "occurrences": occ}


def handle_series_create(user_id, raw, headers):
    body = require_object(raw)
    key = idempotency_header(headers)
    replay = check_replay(user_id, key, "POST", "/series", body)
    if replay is not None:
        return 200, replay

    anchor_ref = body.get("anchor_reference")
    if not isinstance(anchor_ref, str):
        raise err(422, "validation_failed", "anchor_reference is required")
    count = body.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or not (2 <= count <= 12):
        raise err(422, "validation_failed", "count must be an integer 2..12")
    iw = body.get("interval_weeks")
    if isinstance(iw, bool) or not isinstance(iw, int) or not (1 <= iw <= 4):
        raise err(422, "validation_failed", "interval_weeks must be an integer 1..4")

    anchor = STORE.reservations.get(anchor_ref)
    if anchor is None or anchor["user_id"] != user_id:
        raise err(404, "not_found", "no such reservation")
    if anchor["status"] == "cancelled":
        raise err(409, "reservation_cancelled", "reservation is cancelled")
    if anchor.get("series_id"):
        raise err(409, "already_in_series", "reservation is already in a series")
    cutoff = (anchor.get("accepted_terms") or {}).get("cancellation_cutoff_minutes", 0)
    if datetime.now(UTC) >= minus_absolute(anchor["starts_at"], cutoff):
        raise err(409, "cutoff_passed", "cancellation cutoff has passed")

    restaurant = STORE.restaurants.get(anchor["restaurant_id"])
    base_date = _date.fromisoformat(anchor["starts_at_local"][:10])
    clock = anchor["starts_at_local"][11:]
    tids = list(anchor["table_ids"])

    planned = []
    for i in range(1, count):
        d = base_date + timedelta(days=i * iw * 7)
        sal = "%sT%s" % (d.isoformat(), clock)
        naive, aware = resolve_and_check_time(restaurant, sal)
        policy = select_policy(restaurant, d.isoformat())
        check_opening_policy(policy, naive)
        if anchor["party_size"] > policy_capacity(policy, tids):
            raise err(422, "party_exceeds_capacity", "party exceeds table capacity")
        end = add_absolute(aware, policy["reservation_duration_minutes"], restaurant["_tz"])
        if any_table_busy(restaurant["id"], tids, aware, end):
            raise err(409, "table_unavailable", "table is not available")
        if closure_blocks(restaurant, tids, aware, end):
            raise err(409, "table_unavailable", "table is not available")
        for ptids, paware, pend, _psal, _ppol in planned:
            if (set(ptids) & set(tids)) and overlaps(aware, end, paware, pend):
                raise err(409, "table_unavailable", "resulting bookings overlap")
        planned.append((tids, aware, end, sal, policy))

    sid = STORE.next_series_id()
    series = {
        "series_id": sid, "user_id": user_id, "restaurant_id": restaurant["id"],
        "interval_weeks": iw, "revision": 1,
        "occurrences": [{"index": 0, "reference": anchor["reference"],
                         "exception": bool(anchor.get("exception", False))}],
    }
    anchor["series_id"] = sid
    anchor["series_index"] = 0
    for idx, (tids_i, aware, end, sal, policy) in enumerate(planned, start=1):
        rec = build_reservation(restaurant, user_id, tids_i, sal, anchor["party_size"],
                                policy, aware, series_id=sid, series_index=idx)
        add_history(rec, restaurant, "created", created_changes(rec))
        STORE.reservations[rec["reference"]] = rec
        series["occurrences"].append({"index": idx, "reference": rec["reference"],
                                      "exception": False})
    STORE.series[sid] = series
    bump_revision(restaurant)
    resp = series_public(series)
    store_result(user_id, key, "POST", "/series", body, 201, resp)
    return 201, resp


def handle_series_get(headers, sid):
    uid = resolve_optional_user(headers)
    series = STORE.series.get(sid)
    if uid is None or series is None or series["user_id"] != uid:
        raise err(404, "not_found", "no such series")
    return 200, series_public(series)


# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------

PUBLIC_PATHS = {"/health", "/_test/reset", "/_test/export", "/_test/import",
                "/auth/signup", "/auth/login", "/restaurants", "/availability"}


# --------------------------------------------------------------------------
# stage 4: replans (seat repair after a closure) and series amendments
# --------------------------------------------------------------------------

def _require_manager(rid, user_id):
    restaurant = STORE.restaurants.get(rid)
    if restaurant is None:
        raise err(404, "not_found", "no such restaurant")
    if user_id not in (restaurant.get("manager_user_ids") or []):
        raise err(403, "forbidden", "not a manager")
    return restaurant


def build_plan(restaurant, closed_tid, frm, to):
    """Return (assignments, moved_count, unused_seats) for the least-cost plan.

    Raises 422 planning_limit when the instance is larger than the supported
    bounds, and 409 no_feasible_plan when no assignment exists.
    """
    tables = [t["id"] for t in restaurant["tables"]]
    pairs = [list(p) for p in (restaurant.get("combinable") or [])]
    if len(tables) > 6 or len(pairs) > 4:
        raise err(422, "planning_limit", "too many tables or pairs to plan")
    considered = []
    for rec in STORE.reservations.values():
        if rec["restaurant_id"] != restaurant["id"] or rec["status"] != "confirmed":
            continue
        if overlaps(rec["starts_at"], rec["ends_at"], frm, to):
            considered.append(rec)
    considered.sort(key=lambda r: r["reference"])
    if len(considered) > 6:
        raise err(422, "planning_limit", "too many considered bookings to plan")
    considered_refs = {r["reference"] for r in considered}
    fixed = [rec for rec in STORE.reservations.values()
             if rec["restaurant_id"] == restaurant["id"] and rec["status"] == "confirmed"
             and rec["reference"] not in considered_refs]
    all_options = [[t] for t in tables] + pairs

    feasible = []
    for b in considered:
        caps = (b.get("accepted_terms") or {}).get("capacities") or {}
        opts = []
        for rank, tids in enumerate(all_options):
            cap = sum(caps.get(t, 0) for t in tids if isinstance(caps.get(t), int))
            if cap < b["party_size"]:
                continue
            if closed_tid in tids:
                continue  # the proposed closure always overlaps the booking
            if closure_blocks(restaurant, tids, b["starts_at"], b["ends_at"]):
                continue
            clash = False
            for f in fixed:
                if (set(f["table_ids"]) & set(tids)) and overlaps(
                        b["starts_at"], b["ends_at"], f["starts_at"], f["ends_at"]):
                    clash = True
                    break
            if clash:
                continue
            opts.append((rank, tuple(tids), cap))
        if not opts:
            raise err(409, "no_feasible_plan", "no feasible plan for every booking")
        feasible.append(opts)

    n = len(considered)
    best = {"key": None, "assign": None}
    assign = [None] * n

    def score():
        changed = unused = 0
        ranks = []
        for i, b in enumerate(considered):
            rank, tids, cap = assign[i]
            if set(tids) != set(b["table_ids"]):
                changed += 1
            unused += cap - b["party_size"]
            ranks.append(rank)
        return (changed, unused, tuple(ranks))

    def dfs(i, cur_changed, cur_unused):
        k = best["key"]
        if k is not None:
            if cur_changed > k[0]:
                return
            if cur_changed == k[0] and cur_unused > k[1]:
                return
        if i == n:
            s = score()
            if best["key"] is None or s < best["key"]:
                best["key"] = s
                best["assign"] = list(assign)
            return
        b = considered[i]
        for opt in feasible[i]:
            rank, tids, cap = opt
            assign[i] = opt
            ok = True
            for j in range(i):
                aj = assign[j]
                if (set(aj[1]) & set(tids)) and overlaps(
                        considered[j]["starts_at"], considered[j]["ends_at"],
                        b["starts_at"], b["ends_at"]):
                    ok = False
                    break
            if ok:
                dfs(i + 1,
                    cur_changed + (0 if set(tids) == set(b["table_ids"]) else 1),
                    cur_unused + (cap - b["party_size"]))
            assign[i] = None

    dfs(0, 0, 0)
    if best["key"] is None:
        raise err(409, "no_feasible_plan", "no feasible plan")

    assignments = []
    moved = unused = 0
    for i, b in enumerate(considered):
        rank, tids, cap = best["assign"][i]
        tids = list(tids)
        changed = set(tids) != set(b["table_ids"])
        if changed:
            moved += 1
        unused += cap - b["party_size"]
        assignments.append({"reference": b["reference"], "table_ids": tids,
                            "changed": changed})
    return assignments, moved, unused


def handle_replan_preview(user_id, rid, raw, headers, path):
    restaurant = _require_manager(rid, user_id)
    body = require_object(raw)
    key = idempotency_header(headers)
    replay = check_replay(user_id, key, "POST", path, body)
    if replay is not None:
        return 200, replay
    tid = body.get("table_id")
    if tid is None or tid == "":
        raise err(422, "validation_failed", "table_id is required")
    if not isinstance(tid, str):
        raise err(400, "malformed_request", "table_id must be a string")
    if find_table(restaurant, tid) is None:
        raise err(404, "not_found", "no such table")
    for name in ("from", "to"):
        if name in body and not isinstance(body[name], str):
            raise err(400, "malformed_request", "%s must be a string" % name)
    frm = parse_instant(body.get("from"))
    to = parse_instant(body.get("to"))
    if frm is None or to is None or not (frm < to):
        raise err(422, "validation_failed", "invalid closure interval")

    assignments, moved, unused = build_plan(restaurant, tid, frm, to)
    plan_id = STORE.next_plan_id()
    plan = {"plan_id": plan_id, "restaurant_id": restaurant["id"],
            "closure": {"table_id": tid, "from": frm, "to": to},
            "assignments": assignments,
            "restaurant_revision": int(restaurant.get("revision", 0)),
            "applied": False}
    STORE.plans[plan_id] = plan
    resp = {"plan_id": plan_id,
            "restaurant_revision": int(restaurant.get("revision", 0)),
            "closure": {"table_id": tid, "from": fmt_offset(frm), "to": fmt_offset(to)},
            "assignments": assignments,
            "moved_count": moved, "unused_seats": unused}
    store_result(user_id, key, "POST", path, body, 201, resp)
    return 201, resp


def handle_replan_apply(user_id, rid, plan_id, raw, headers, path):
    restaurant = _require_manager(rid, user_id)
    body = require_object(raw)
    key = idempotency_header(headers)
    replay = check_replay(user_id, key, "POST", path, body)
    if replay is not None:
        return 200, replay
    plan = STORE.plans.get(plan_id)
    if plan is None or plan["restaurant_id"] != rid:
        raise err(404, "not_found", "no such plan")
    if plan.get("applied"):
        raise err(409, "plan_already_applied", "plan already applied")
    if int(restaurant.get("revision", 0)) != plan["restaurant_revision"]:
        raise err(409, "stale_plan", "plan is stale")

    closure = plan["closure"]
    restaurant.setdefault("closures", []).append(
        {"table_id": closure["table_id"], "from": closure["from"], "to": closure["to"]})
    affected_series = set()
    for a in plan["assignments"]:
        if not a["changed"]:
            continue
        rec = STORE.reservations.get(a["reference"])
        if rec is None:
            continue
        old_tids = list(rec["table_ids"])
        new_tids = list(a["table_ids"])
        rec["table_ids"] = new_tids
        rec["revision"] = int(rec.get("revision", 1)) + 1
        entry = add_history(rec, restaurant, "reassigned",
                            [{"field": "table_ids", "from": old_tids, "to": new_tids}])
        entry["plan_id"] = plan_id
        sid = rec.get("series_id")
        if sid:
            affected_series.add(sid)
    for sid in affected_series:
        series = STORE.series.get(sid)
        if series is not None:
            series["revision"] = int(series.get("revision", 1)) + 1
    bump_revision(restaurant)
    plan["applied"] = True
    plan["applied_key"] = key
    resp = {"plan_id": plan_id,
            "restaurant_revision": int(restaurant.get("revision", 0)),
            "reservations": [public_reservation(STORE.reservations[a["reference"]])
                             for a in plan["assignments"]
                             if a["reference"] in STORE.reservations]}
    store_result(user_id, key, "POST", path, body, 201, resp)
    return 201, resp


def handle_series_amend(user_id, sid, raw, headers, path):
    body = require_object(raw)
    series = STORE.series.get(sid)
    if series is None or series.get("user_id") != user_id:
        raise err(404, "not_found", "no such series")
    key = idempotency_header(headers)
    replay = check_replay(user_id, key, "POST", path, body)
    if replay is not None:
        return 200, replay
    er = body.get("expected_revision")
    if isinstance(er, bool) or not isinstance(er, int) or er < 1:
        raise err(422, "validation_failed", "expected_revision must be a positive integer")
    occurrences = series.get("occurrences") or []
    fi = body.get("from_index")
    if isinstance(fi, bool) or not isinstance(fi, int) or not (0 <= fi < len(occurrences)):
        raise err(422, "validation_failed", "from_index is out of range")
    lt = body.get("local_time")
    if not isinstance(lt, str) or parse_hhmm(lt) is None:
        raise err(422, "validation_failed", "local_time must be HH:MM")
    if er != int(series.get("revision", 1)):
        raise err(409, "stale_revision", "revision is stale")

    restaurant = STORE.restaurants.get(series["restaurant_id"])
    real_changes = []
    for o in sorted(occurrences, key=lambda x: x["index"]):
        if o["index"] < fi or o.get("exception"):
            continue
        rec = STORE.reservations.get(o["reference"])
        if rec is None or rec["status"] == "cancelled":
            continue
        date_s = rec["starts_at_local"][:10]
        sal = "%sT%s" % (date_s, lt)
        if sal == rec["starts_at_local"]:
            continue
        cutoff = (rec.get("accepted_terms") or {}).get("cancellation_cutoff_minutes", 0)
        if datetime.now(UTC) >= minus_absolute(rec["starts_at"], cutoff):
            raise err(409, "cutoff_passed", "cutoff has passed")
        naive, aware = resolve_and_check_time(restaurant, sal)
        policy = select_policy(restaurant, date_s)
        check_opening_policy(policy, naive)
        tids = list(rec["table_ids"])
        if rec["party_size"] > policy_capacity(policy, tids):
            raise err(422, "party_exceeds_capacity", "party exceeds table capacity")
        end = add_absolute(aware, policy["reservation_duration_minutes"], restaurant["_tz"])
        real_changes.append((rec, rec["starts_at_local"], sal, aware, end, policy, tids))

    new_for = {rc[0]["reference"]: (rc[6], rc[3], rc[4]) for rc in real_changes}

    def interval_of(ref):
        if ref in new_for:
            return new_for[ref]
        other = STORE.reservations[ref]
        return (other["table_ids"], other["starts_at"], other["ends_at"])

    for rec, osal, sal, aware, end, policy, tids in real_changes:
        if closure_blocks(restaurant, tids, aware, end):
            raise err(409, "table_unavailable", "table is not available")
    for ref, (tids, aware, end) in new_for.items():
        for oref, other in STORE.reservations.items():
            if oref == ref or other["status"] != "confirmed":
                continue
            if other["restaurant_id"] != restaurant["id"]:
                continue
            otids, ostart, oend = interval_of(oref)
            if (set(otids) & set(tids)) and overlaps(aware, end, ostart, oend):
                raise err(409, "table_unavailable", "resulting bookings overlap")

    for rec, osal, sal, aware, end, policy, tids in real_changes:
        rec["starts_at_local"] = sal
        rec["starts_at"] = aware
        rec["ends_at"] = end
        rec["accepted_terms"] = terms_of(policy)
        rec["revision"] = int(rec.get("revision", 1)) + 1
        add_history(rec, restaurant, "changed",
                    [{"field": "starts_at_local", "from": osal, "to": sal}])
    if real_changes:
        series["revision"] = int(series.get("revision", 1)) + 1
        bump_revision(restaurant)
    resp = series_public(series)
    store_result(user_id, key, "POST", path, body, 201, resp)
    return 201, resp


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

    if len(parts) == 3 and parts[0] == "restaurants" and parts[2] == "policies":
        if method == "GET":
            return handle_list_policies(parts[1])
        if method == "POST":
            user_id = authenticate(headers)
            return handle_create_policy(user_id, parts[1], raw, headers, path)
        raise err(404, "not_found", "no such route")

    if len(parts) == 3 and parts[0] == "restaurants" and parts[2] == "replans":
        user_id = authenticate(headers)
        if method != "POST":
            raise err(404, "not_found", "no such route")
        return handle_replan_preview(user_id, parts[1], raw, headers, path)

    if (len(parts) == 5 and parts[0] == "restaurants" and parts[2] == "replans"
            and parts[4] == "apply"):
        user_id = authenticate(headers)
        if method != "POST":
            raise err(404, "not_found", "no such route")
        return handle_replan_apply(user_id, parts[1], parts[3], raw, headers, path)

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

    if len(parts) == 3 and parts[0] == "reservations" and parts[2] == "history":
        if method != "GET":
            raise err(404, "not_found", "no such route")
        return handle_history(headers, parts[1])

    if len(parts) == 3 and parts[0] == "reservations" and parts[2] == "decision":
        if method != "GET":
            raise err(404, "not_found", "no such route")
        return handle_decision(headers, parts[1])

    if path == "/series":
        user_id = authenticate(headers)
        if method != "POST":
            raise err(404, "not_found", "no such route")
        return handle_series_create(user_id, raw, headers)

    if len(parts) == 3 and parts[0] == "series" and parts[2] == "amend":
        user_id = authenticate(headers)
        if method != "POST":
            raise err(404, "not_found", "no such route")
        return handle_series_amend(user_id, parts[1], raw, headers, path)

    if len(parts) == 2 and parts[0] == "series":
        if method != "GET":
            raise err(404, "not_found", "no such route")
        return handle_series_get(headers, parts[1])

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
                page = try_page(method, path, raw, self.headers)
                if page is not None:
                    status, ctype, data, extra = page
                    self._send_raw(status, ctype, data, extra)
                    return
                status, body = dispatch(method, path, qs, raw, self.headers)
        except ApiError as e:
            status, body = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception:
            status, body = 500, {"error": {"code": "internal_error",
                                           "message": "internal server error"}}
        self._send(status, body)

    def _send_raw(self, status, content_type, data, extra_headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        if data:
            try:
                self.wfile.write(data)
            except Exception:
                pass

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
