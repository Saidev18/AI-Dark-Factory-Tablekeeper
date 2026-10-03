"""Tablekeeper Stage 2: browser booking and declared pairs with atomic state."""

import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LOCK = threading.RLock()


class APIError(Exception):
    def __init__(self, status, code, message=None):
        self.status = status
        self.code = code
        self.message = message or code.replace("_", " ")


def fail(status=422, code="validation_failed", message=None):
    raise APIError(status, code, message)


def empty_state():
    return {"users": {}, "restaurants": {}, "reservations": {}, "tokens": {}, "receipts": {}}


STATE = empty_state()


def field(obj, name, typ):
    if name not in obj:
        fail(message=f"Missing {name}")
    value = obj[name]
    if not isinstance(value, typ):
        fail(400, "malformed_request", f"Wrong type for {name}")
    return value


def identifier(value):
    if not isinstance(value, str):
        fail(400, "malformed_request")
    if not 1 <= len(value) <= 64:
        fail()
    return value


def integer(value, minimum=1, party=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(422 if party else 400, "validation_failed" if party else "malformed_request")
    if isinstance(value, float) and (not value.is_integer()):
        fail()
    if value < minimum:
        fail()
    return int(value)


def reference(value):
    if not isinstance(value, str):
        fail(400, "malformed_request")
    if not re.fullmatch(r"[A-Z0-9]{6,12}", value):
        fail()
    return value


def local_datetime(value):
    if not isinstance(value, str):
        fail(400, "malformed_request")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}", value):
        fail()
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        fail()


def resolve(naive, zone):
    # fold=0 chooses the occurrence before the backwards transition.
    aware = naive.replace(tzinfo=zone, fold=0)
    utc = aware.astimezone(UTC)
    if utc.astimezone(zone).replace(tzinfo=None) != naive:
        fail(422, "invalid_local_time")
    return aware


def parse_timestamp(value):
    if not isinstance(value, str):
        fail()
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            fail()
        return dt.astimezone(UTC)
    except (ValueError, OverflowError):
        fail()


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return {"algorithm": "scrypt", "salt": salt, "digest": digest}


def password_matches(password, encoded):
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(encoded["salt"]), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(digest, encoded["digest"])


def email_address(value):
    if not isinstance(value, str):
        fail(400, "malformed_request")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+", value):
        fail()
    return value


def same_json(a, b):
    # JSON booleans are distinct from numbers; object ordering is irrelevant.
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same_json(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same_json(x, y) for x, y in zip(a, b))
    return a == b


def restaurant_config(raw):
    if not isinstance(raw, dict):
        fail(400, "malformed_request")
    result = {"id": identifier(field(raw, "id", str)), "name": field(raw, "name", str),
              "timezone": field(raw, "timezone", str)}
    try:
        ZoneInfo(result["timezone"])
    except (ZoneInfoNotFoundError, ValueError, OSError):
        fail()
    for key, minimum in (("slot_minutes", 1), ("reservation_duration_minutes", 1),
                         ("cancellation_cutoff_minutes", 0)):
        if key not in raw:
            fail()
        result[key] = integer(raw[key], minimum)
    result["opening_hours"] = []
    seen = set()
    for hours in field(raw, "opening_hours", list):
        if not isinstance(hours, dict):
            fail(400, "malformed_request")
        entry = {key: field(hours, key, str) for key in ("weekday", "opens", "closes")}
        if entry["weekday"] not in WEEKDAYS or entry["weekday"] in seen:
            fail()
        for key in ("opens", "closes"):
            if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", entry[key]):
                fail()
        if entry["opens"] >= entry["closes"]:
            fail()
        seen.add(entry["weekday"])
        result["opening_hours"].append(entry)
    result["tables"] = []
    seen = set()
    for table in field(raw, "tables", list):
        if not isinstance(table, dict):
            fail(400, "malformed_request")
        entry = {"id": identifier(field(table, "id", str)), "label": field(table, "label", str)}
        if "capacity" not in table:
            fail()
        entry["capacity"] = integer(table["capacity"])
        if entry["id"] in seen:
            fail()
        seen.add(entry["id"])
        result["tables"].append(entry)
    pairs = raw.get("combinable", [])
    if not isinstance(pairs, list):
        fail(400, "malformed_request")
    result["combinable"] = []
    pair_sets = set()
    for pair in pairs:
        if not isinstance(pair, list):
            fail(400, "malformed_request")
        if len(pair) != 2:
            fail()
        for tid in pair:
            identifier(tid)
        if pair[0] == pair[1] or any(tid not in seen for tid in pair):
            fail()
        identity = frozenset(pair)
        if identity in pair_sets:
            fail()
        pair_sets.add(identity)
        result["combinable"].append(pair[:])
    return result


def hours_for(restaurant, naive):
    return next((h for h in restaurant["opening_hours"] if h["weekday"] == WEEKDAYS[naive.weekday()]), None)


def day_time(day, clock):
    hour, minute = map(int, clock.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute)


def selection(body, default=None):
    if "table_id" in body and "table_ids" in body:
        fail()
    if "table_id" in body:
        return [identifier(body["table_id"])]
    if "table_ids" in body:
        ids = field(body, "table_ids", list)
        if not ids:
            fail()
        for tid in ids:
            identifier(tid)
        if len(set(ids)) != len(ids):
            fail()
        if len(ids) > 2:
            fail(422, "combination_not_allowed")
        return ids[:]
    if default is None:
        fail()
    return table_ids(default)


def table_ids(booking):
    return booking.get("table_ids", [booking.get("table_id")])


def validated_booking(state, restaurant_id, ids, start_local, size):
    identifier(restaurant_id)
    if not isinstance(ids, list):
        fail(400, "malformed_request")
    if not ids:
        fail()
    for tid in ids:
        identifier(tid)
    if len(set(ids)) != len(ids):
        fail()
    if len(ids) > 2:
        fail(422, "combination_not_allowed")
    size = integer(size, party=True)
    naive = local_datetime(start_local)
    restaurant = state["restaurants"].get(restaurant_id)
    if restaurant is None:
        fail(404, "not_found")
    tables = {t["id"]: t for t in restaurant["tables"]}
    if any(tid not in tables for tid in ids):
        fail(404, "not_found")
    if len(ids) == 2 and not any(set(pair) == set(ids) for pair in restaurant["combinable"]):
        fail(422, "combination_not_allowed")
    if size > sum(tables[tid]["capacity"] for tid in ids):
        fail(422, "party_exceeds_capacity")
    zone = ZoneInfo(restaurant["timezone"])
    start = resolve(naive, zone)
    hours = hours_for(restaurant, naive)
    if hours is None:
        fail(422, "outside_opening_hours")
    opening, closing = day_time(naive, hours["opens"]), day_time(naive, hours["closes"])
    if not opening <= naive < closing:
        fail(422, "outside_opening_hours")
    if int((naive - opening).total_seconds() // 60) % restaurant["slot_minutes"]:
        fail(422, "not_on_slot_grid")
    try:
        end_utc = start.astimezone(UTC) + timedelta(minutes=restaurant["reservation_duration_minutes"])
    except OverflowError:
        fail(422, "outside_opening_hours")
    close_utc = resolve(closing, zone).astimezone(UTC)
    if end_utc > close_utc:
        fail(422, "outside_opening_hours")
    result = {"restaurant_id": restaurant_id, "table_ids": ids[:], "party_size": size,
            "starts_at_local": start_local, "starts_at": start.isoformat(timespec="seconds"),
            "ends_at": end_utc.astimezone(zone).isoformat(timespec="seconds")}
    if len(ids) == 1:
        result["table_id"] = ids[0]
    return result


def overlaps(a, b):
    return (a["restaurant_id"] == b["restaurant_id"] and bool(set(table_ids(a)) & set(table_ids(b)))
            and parse_timestamp(a["starts_at"]) < parse_timestamp(b["ends_at"])
            and parse_timestamp(b["starts_at"]) < parse_timestamp(a["ends_at"]))


def check_occupancy(state, candidates, excluded=()):
    others = [r for ref, r in state["reservations"].items()
              if ref not in excluded and r["status"] == "confirmed"]
    for candidate in candidates:
        if any(overlaps(candidate, r) for r in others):
            fail(409, "table_unavailable")
        others.append(candidate)


def view(reservation):
    return {key: value for key, value in reservation.items() if key != "user_id"}


def owned(state, ref, user_id):
    reservation = state["reservations"].get(ref)
    if reservation is None or reservation["user_id"] != user_id:
        fail(404, "not_found")
    return reservation


def cutoff(state, reservation):
    if reservation["status"] == "cancelled":
        fail(409, "reservation_cancelled")
    minutes = state["restaurants"][reservation["restaurant_id"]]["cancellation_cutoff_minutes"]
    remaining = (parse_timestamp(reservation["starts_at"]) - datetime.now(UTC)).total_seconds()
    if remaining <= minutes * 60:
        fail(409, "cutoff_passed")


def changed(state, current, item):
    # Preserve every value of no-op items, including imported timestamps.
    ids = selection(item, current)
    if same_json(ids, table_ids(current)) and not any(key in item and not same_json(item[key], current[key])
               for key in ("starts_at_local", "party_size")):
        return copy.deepcopy(current)
    fields = validated_booking(state, current["restaurant_id"], ids,
                               item.get("starts_at_local", current["starts_at_local"]),
                               item.get("party_size", current["party_size"]))
    candidate = {**current, **fields}
    if len(ids) != 1:
        candidate.pop("table_id", None)
    return candidate


def reset_state(body):
    state = empty_state()
    emails = set()
    for raw in field(body, "users", list):
        if not isinstance(raw, dict):
            fail(400, "malformed_request")
        uid = identifier(field(raw, "id", str))
        email = email_address(field(raw, "email", str))
        password = field(raw, "password", str)
        if uid in state["users"] or email in emails:
            fail()
        state["users"][uid] = {"id": uid, "email": email, "display_name": field(raw, "display_name", str),
                               "password_hash": password_hash(password)}
        emails.add(email)
    for raw in field(body, "restaurants", list):
        restaurant = restaurant_config(raw)
        if restaurant["id"] in state["restaurants"]:
            fail()
        state["restaurants"][restaurant["id"]] = restaurant
    ids = set()
    for raw in field(body, "reservations", list):
        if not isinstance(raw, dict):
            fail(400, "malformed_request")
        rid = identifier(field(raw, "id", str))
        ref = reference(field(raw, "reference", str))
        uid = identifier(field(raw, "user_id", str))
        if uid not in state["users"] or ref in state["reservations"] or rid in ids:
            fail()
        fields = validated_booking(state, field(raw, "restaurant_id", str), selection(raw),
                                   field(raw, "starts_at_local", str), raw.get("party_size"))
        status = raw.get("status", "confirmed")
        if not isinstance(status, str):
            fail(400, "malformed_request")
        if status not in ("confirmed", "cancelled"):
            fail()
        booking = {"reservation_id": rid, "reference": ref, "user_id": uid, **fields,
                   "status": status, "created_at": datetime.now(UTC).isoformat(timespec="seconds")}
        if status == "confirmed":
            check_occupancy(state, [booking])
        state["reservations"][ref] = booking
        ids.add(rid)
    return state


def imported_state(body):
    # State is portable JSON. Fully validate the candidate before the single replacement.
    if body.get("track") != "tablekeeper" or type(body.get("format_version")) is not int or body["format_version"] != 1:
        fail()
    state = copy.deepcopy(body.get("state"))
    if not isinstance(state, dict) or set(state) != set(empty_state()):
        fail()
    if any(not isinstance(value, dict) for value in state.values()):
        fail()
    try:
        emails = set()
        for uid, user in state["users"].items():
            identifier(uid)
            if not isinstance(user, dict) or user["id"] != uid or not isinstance(user["display_name"], str):
                fail()
            email_address(user["email"])
            if user["email"] in emails or "password" in user:
                fail()
            emails.add(user["email"])
            encoded = user["password_hash"]
            if (encoded["algorithm"] != "scrypt" or not re.fullmatch(r"[0-9a-f]{32}", encoded["salt"])
                    or not re.fullmatch(r"[0-9a-f]{128}", encoded["digest"])):
                fail()
        for rid, restaurant in state["restaurants"].items():
            configured = restaurant_config(restaurant)
            if configured != {"combinable": [], **restaurant} or rid != restaurant["id"]:
                fail()
            state["restaurants"][rid] = configured
        ids = set()
        confirmed = []
        for ref, booking in state["reservations"].items():
            if reference(ref) != booking["reference"] or booking["user_id"] not in state["users"]:
                fail()
            rid = identifier(booking["reservation_id"])
            if rid in ids or booking["status"] not in ("confirmed", "cancelled"):
                fail()
            ids.add(rid)
            fields = validated_booking(state, booking["restaurant_id"], table_ids(booking),
                                       booking["starts_at_local"], booking["party_size"])
            upgraded = {"table_ids": table_ids(booking), **booking}
            if any(not same_json(upgraded[key], value) for key, value in fields.items()):
                fail()
            parse_timestamp(booking["created_at"])
            if set(upgraded) != set(fields) | {"reservation_id", "reference", "user_id", "status", "created_at"}:
                fail()
            state["reservations"][ref] = upgraded
            if booking["status"] == "confirmed":
                if any(overlaps(booking, other) for other in confirmed):
                    fail()
                confirmed.append(booking)
        for token, uid in state["tokens"].items():
            if not token or uid not in state["users"]:
                fail()
        for scope, receipt in state["receipts"].items():
            uid, method, path, key = json.loads(scope)
            if (uid not in state["users"] or method != "POST" or path not in ("/reservations", "/reservation-moves")
                    or not isinstance(key, str) or not 1 <= len(key) <= 255
                    or not isinstance(receipt, dict) or set(receipt) != {"body", "response"}
                    or not isinstance(receipt["body"], dict) or not isinstance(receipt["response"], dict)):
                fail()
            responses = ([receipt["response"]] if path == "/reservations"
                         else receipt["response"]["reservations"])
            if not isinstance(responses, list) or not responses:
                fail()
            for response in responses:
                original = state["reservations"][response["reference"]]
                if original["user_id"] != uid or original["reservation_id"] != response["reservation_id"]:
                    fail()
                fields = validated_booking(state, response["restaurant_id"], table_ids(response),
                                           response["starts_at_local"], response["party_size"])
                upgraded_response = {"table_ids": table_ids(response), **response}
                if set(upgraded_response) != set(fields) | {"reservation_id", "reference", "status", "created_at"}:
                    fail()
                if any(not same_json(upgraded_response[k], v) for k, v in fields.items()) or response["status"] != "confirmed":
                    fail()
                parse_timestamp(response["created_at"])
        return copy.deepcopy(state)
    except (APIError, KeyError, TypeError, ValueError, OverflowError):
        fail()


def auth(state, authorization):
    if not authorization:
        fail(401, "unauthenticated")
    match = re.fullmatch(r"Bearer ([^\s]+)", authorization, re.IGNORECASE)
    if not match or match[1] not in state["tokens"]:
        fail(401, "unauthenticated")
    return state["tokens"][match[1]]


def execute(method, path, query, body, headers):
    global STATE
    state = STATE
    if method == "GET" and path == "/health":
        return 200, {"status": "ok"}
    if method == "POST" and path == "/_test/reset":
        STATE = reset_state(body)
        return 204, None
    if method == "GET" and path == "/_test/export":
        return 200, {"track": "tablekeeper", "format_version": 1, "state": copy.deepcopy(state)}
    if method == "POST" and path == "/_test/import":
        STATE = imported_state(body)
        return 204, None
    if method == "POST" and path in ("/auth/signup", "/auth/login"):
        email = email_address(field(body, "email", str))
        password = field(body, "password", str)
        user = next((u for u in state["users"].values() if u["email"] == email), None)
        if path == "/auth/signup":
            name = field(body, "display_name", str)
            if len(password) < 8:
                fail()
            if user:
                fail(409, "email_taken")
            uid = "u_" + uuid.uuid4().hex
            user = {"id": uid, "email": email, "display_name": name, "password_hash": password_hash(password)}
            state["users"][uid] = user
        elif user is None or not password_matches(password, user["password_hash"]):
            fail(401, "unauthenticated")
        token = secrets.token_urlsafe(32)
        state["tokens"][token] = user["id"]
        return (201 if path == "/auth/signup" else 200), {"user_id": user["id"], "display_name": user["display_name"], "token": token}
    if method == "GET" and path == "/restaurants":
        return 200, {"restaurants": [{k: r[k] for k in ("id", "name", "timezone")} for r in state["restaurants"].values()]}
    if method == "GET" and re.fullmatch(r"/restaurants/[^/]+", path):
        rid = identifier(unquote(path.split("/")[2]))
        if rid not in state["restaurants"]:
            fail(404, "not_found")
        return 200, state["restaurants"][rid]
    if method == "GET" and path == "/availability":
        params = {}
        for key in ("restaurant_id", "date", "party_size"):
            if key not in query:
                fail()
            params[key] = query[key][0]
        rid = identifier(params["restaurant_id"])
        if not re.fullmatch(r"[0-9]+", params["party_size"]):
            fail()
        size = integer(int(params["party_size"]), party=True)
        day = local_datetime(params["date"] + "T00:00")
        restaurant = state["restaurants"].get(rid)
        if restaurant is None:
            fail(404, "not_found")
        slots = []
        hours = hours_for(restaurant, day)
        if hours:
            zone = ZoneInfo(restaurant["timezone"])
            opening, closing = day_time(day, hours["opens"]), day_time(day, hours["closes"])
            close_utc = resolve(closing, zone).astimezone(UTC)
            minute = opening.hour * 60 + opening.minute
            while minute < closing.hour * 60 + closing.minute:
                naive = day.replace(hour=minute // 60, minute=minute % 60)
                minute += restaurant["slot_minutes"]
                try:
                    start = resolve(naive, zone)
                    end = start.astimezone(UTC) + timedelta(minutes=restaurant["reservation_duration_minutes"])
                except APIError as error:
                    if error.code == "invalid_local_time":
                        continue
                    raise
                except OverflowError:
                    continue
                if end > close_utc:
                    continue
                interval = {"restaurant_id": rid, "starts_at": start.isoformat(timespec="seconds"), "ends_at": end.isoformat(timespec="seconds")}
                available = []
                options = []
                for table in restaurant["tables"]:
                    if table["capacity"] < size:
                        continue
                    interval["table_ids"] = [table["id"]]
                    if not any(r["status"] == "confirmed" and overlaps(interval, r) for r in state["reservations"].values()):
                        available.append(table["id"])
                        options.append({"table_ids": [table["id"]], "capacity": table["capacity"]})
                table_map = {t["id"]: t for t in restaurant["tables"]}
                for pair in restaurant["combinable"]:
                    capacity = sum(table_map[tid]["capacity"] for tid in pair)
                    interval["table_ids"] = pair
                    if capacity >= size and not any(r["status"] == "confirmed" and overlaps(interval, r) for r in state["reservations"].values()):
                        options.append({"table_ids": pair[:], "capacity": capacity})
                slots.append({"starts_at_local": naive.isoformat(timespec="minutes"), "starts_at": interval["starts_at"], "available_table_ids": available, "available_options": options})
        return 200, {"restaurant_id": rid, "date": params["date"], "timezone": restaurant["timezone"], "slots": slots}
    uid = auth(state, headers.get("Authorization"))
    scope = None
    if method == "POST" and path in ("/reservations", "/reservation-moves"):
        key = headers.get("Idempotency-Key")
        if key is None or not key:
            fail(400, "missing_idempotency_key")
        if len(key) > 255:
            fail()
        scope = json.dumps([uid, method, path, key], separators=(",", ":"))
        receipt = state["receipts"].get(scope)
        if receipt:
            if not same_json(receipt["body"], body):
                fail(409, "idempotency_key_reuse")
            return 200, receipt["response"]
    if method == "POST" and path == "/reservations":
        fields = validated_booking(state, field(body, "restaurant_id", str), selection(body),
                                   field(body, "starts_at_local", str), body.get("party_size"))
        check_occupancy(state, [fields])
        ref = secrets.token_hex(5).upper()
        while ref in state["reservations"]:
            ref = secrets.token_hex(5).upper()
        booking = {"reservation_id": "res_" + uuid.uuid4().hex, "reference": ref, "user_id": uid, **fields,
                   "status": "confirmed", "created_at": datetime.now(UTC).isoformat(timespec="seconds")}
        response = view(booking)
        state["reservations"][ref] = booking
        state["receipts"][scope] = {"body": copy.deepcopy(body), "response": copy.deepcopy(response)}
        return 201, response
    if method == "POST" and path == "/reservation-moves":
        moves = body.get("moves")
        if not isinstance(moves, list) or not 1 <= len(moves) <= 8:
            fail()
        refs = []
        for item in moves:
            if not isinstance(item, dict) or not isinstance(item.get("reference"), str):
                fail()
            ref = item["reference"]
            if not 1 <= len(ref) <= 64 or ref in refs:
                fail()
            refs.append(ref)
        candidates = []
        restaurant_id = None
        for ref, item in zip(refs, moves):
            current = owned(state, ref, uid)
            if restaurant_id is not None and current["restaurant_id"] != restaurant_id:
                fail()
            restaurant_id = current["restaurant_id"]
            cutoff(state, current)
            candidates.append(changed(state, current, item))
        check_occupancy(state, candidates, refs)
        response = {"reservations": [view(r) for r in candidates]}
        for ref, candidate in zip(refs, candidates):
            state["reservations"][ref] = candidate
        state["receipts"][scope] = {"body": copy.deepcopy(body), "response": copy.deepcopy(response)}
        return 201, response
    if method == "GET" and path == "/reservations":
        reservations = sorted((r for r in state["reservations"].values() if r["user_id"] == uid),
                              key=lambda r: parse_timestamp(r["starts_at"]), reverse=True)
        return 200, {"reservations": [view(r) for r in reservations]}
    match = re.fullmatch(r"/reservations/([^/]+)(/cancel)?", path)
    if match:
        ref = identifier(unquote(match[1]))
        current = owned(state, ref, uid)
        if method == "GET" and not match[2]:
            return 200, view(current)
        if method == "POST" and match[2]:
            if current["status"] != "cancelled":
                cutoff(state, current)
                current["status"] = "cancelled"
            return 200, view(current)
        if method == "PATCH" and not match[2]:
            cutoff(state, current)
            candidate = changed(state, current, body)
            check_occupancy(state, [candidate], [ref])
            state["reservations"][ref] = candidate
            return 200, view(candidate)
    fail(404, "not_found")


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes each connection, keeping framing and concurrent clients simple.
    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PATCH(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler otherwise emits HTML, including for bad HTTP framing
        # and unsupported verbs. Keep protocol errors inside the JSON contract.
        status = 400 if code >= 500 else code
        payload = json.dumps({"error": {"code": "malformed_request", "message": "Invalid HTTP request"}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_request(self):
        try:
            parsed = urlsplit(self.path)
            if self.command == "GET":
                asset = {"/": ("index.html", "text/html"), "/signup": ("index.html", "text/html"),
                         "/login": ("index.html", "text/html"), "/lookup": ("index.html", "text/html"),
                         "/assets/app.css": ("app.css", "text/css"), "/assets/app.js": ("app.js", "text/javascript")}.get(parsed.path)
                if asset:
                    payload = (Path(__file__).parent / "web" / asset[0]).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", asset[1] + "; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            body = {}
            if self.command in ("POST", "PATCH", "PUT", "DELETE"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0:
                        fail(400, "malformed_request")
                    data = self.rfile.read(length)
                    if data:
                        body = json.loads(data, parse_constant=lambda value: fail(400, "malformed_request"))
                    elif not parsed.path.endswith("/cancel"):
                        fail(400, "malformed_request")
                except (ValueError, UnicodeDecodeError, RecursionError):
                    fail(400, "malformed_request")
                if not isinstance(body, dict):
                    fail(400, "malformed_request")
            with LOCK:
                status, response = execute(self.command, parsed.path, parse_qs(parsed.query, keep_blank_values=True), body, self.headers)
                encoded = b"" if response is None else json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode()
        except APIError as error:
            status = error.status
            encoded = json.dumps({"error": {"code": error.code, "message": error.message}}).encode()
        except (ValueError, OverflowError, RecursionError):
            status = 422
            encoded = json.dumps({"error": {"code": "validation_failed", "message": "Invalid value"}}).encode()
        except Exception:
            logging.exception("Unexpected request failure")
            status = 500
            encoded = json.dumps({"error": {"code": "internal_error", "message": "Unexpected error"}}).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        # Avoid query strings, bearer tokens and private export bodies in logs.
        pass


class Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    server = Server(("0.0.0.0", port), Handler)
    print(f"Tablekeeper listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()
