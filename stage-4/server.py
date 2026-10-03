"""Tablekeeper Stage 4: atomic seating repairs and recurring amendments."""

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
    return {"users": {}, "restaurants": {}, "reservations": {}, "tokens": {}, "receipts": {},
            "policies": {}, "histories": {}, "series": {}, "restaurant_revisions": {},
            "plans": {}, "closures": {}, "mutation_sources": {}}


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
    managers = raw.get("manager_user_ids", [])
    if not isinstance(managers, list):
        fail(400, "malformed_request")
    result["manager_user_ids"] = [identifier(uid) for uid in managers]
    if len(set(managers)) != len(managers):
        fail()
    return result


POLICY_FIELDS = ("slot_minutes", "reservation_duration_minutes", "cancellation_cutoff_minutes", "opening_hours", "capacities")


def bounded_integer(value, minimum, maximum=None):
    # These endpoints explicitly use validation_failed for invalid types too.
    value = integer(value, minimum, party=True)
    if maximum is not None and value > maximum:
        fail()
    return value


def policy_zero(restaurant):
    return {"policy_version": 0, **{k: copy.deepcopy(restaurant[k]) for k in POLICY_FIELDS[:-1]},
            "capacities": {t["id"]: t["capacity"] for t in restaurant["tables"]}}


def policy_config(body, restaurant):
    try:
        date = body["effective_from"]
        if not isinstance(date, str):
            fail()
        local_datetime(date + "T00:00")
        result = {"effective_from": date,
                  "slot_minutes": bounded_integer(body["slot_minutes"], 1, 1440),
                  "reservation_duration_minutes": bounded_integer(body["reservation_duration_minutes"], 1, 1440),
                  "cancellation_cutoff_minutes": bounded_integer(body["cancellation_cutoff_minutes"], 0, 10080)}
        hours = body["opening_hours"]
        if not isinstance(hours, list):
            fail()
        # Reuse the opening-hours rules without allowing policy configuration changes.
        configured = restaurant_config({**restaurant, "opening_hours": hours})
        result["opening_hours"] = configured["opening_hours"]
        capacities = body["capacities"]
        if not isinstance(capacities, dict) or set(capacities) != {t["id"] for t in restaurant["tables"]}:
            fail()
        result["capacities"] = {tid: bounded_integer(value, 1, 100) for tid, value in capacities.items()}
        return result
    except (APIError, KeyError, TypeError, ValueError):
        fail()


def policy_terms(policy):
    return {"policy_version": policy["policy_version"], **{k: copy.deepcopy(policy[k]) for k in POLICY_FIELDS}}


def applicable_terms(state, rid, naive):
    policies = [p for p in state["policies"].get(rid, []) if p["effective_from"] <= naive.strftime("%Y-%m-%d")]
    if not policies:
        return policy_zero(state["restaurants"][rid])
    return policy_terms(max(policies, key=lambda p: (p["effective_from"], p["policy_version"])))


def canonical_tables(restaurant, ids):
    if len(ids) == 2:
        for pair in restaurant["combinable"]:
            if set(pair) == set(ids):
                return pair[:]
        fail(422, "combination_not_allowed")
    return ids[:]


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


def validated_booking(state, restaurant_id, ids, start_local, size, terms=None):
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
    ids = canonical_tables(restaurant, ids)
    terms = copy.deepcopy(terms if terms is not None else applicable_terms(state, restaurant_id, naive))
    if size > sum(terms["capacities"][tid] for tid in ids):
        fail(422, "party_exceeds_capacity")
    zone = ZoneInfo(restaurant["timezone"])
    start = resolve(naive, zone)
    hours = hours_for(terms, naive)
    if hours is None:
        fail(422, "outside_opening_hours")
    opening, closing = day_time(naive, hours["opens"]), day_time(naive, hours["closes"])
    if not opening <= naive < closing:
        fail(422, "outside_opening_hours")
    if int((naive - opening).total_seconds() // 60) % terms["slot_minutes"]:
        fail(422, "not_on_slot_grid")
    try:
        end_utc = start.astimezone(UTC) + timedelta(minutes=terms["reservation_duration_minutes"])
    except OverflowError:
        fail(422, "outside_opening_hours")
    close_utc = resolve(closing, zone).astimezone(UTC)
    if end_utc > close_utc:
        fail(422, "outside_opening_hours")
    result = {"restaurant_id": restaurant_id, "table_ids": ids[:], "party_size": size,
            "starts_at_local": start_local, "starts_at": start.isoformat(timespec="seconds"),
            "ends_at": end_utc.astimezone(zone).isoformat(timespec="seconds"), "accepted_terms": terms}
    if len(ids) == 1:
        result["table_id"] = ids[0]
    return result


def overlaps(a, b):
    return (a["restaurant_id"] == b["restaurant_id"] and bool(set(table_ids(a)) & set(table_ids(b)))
            and parse_timestamp(a["starts_at"]) < parse_timestamp(b["ends_at"])
            and parse_timestamp(b["starts_at"]) < parse_timestamp(a["ends_at"]))


def closure_conflict(state, booking, additional=()):
    return any(booking["restaurant_id"] == closure["restaurant_id"]
               and closure["table_id"] in table_ids(booking)
               and parse_timestamp(booking["starts_at"]) < parse_timestamp(closure["to"])
               and parse_timestamp(closure["from"]) < parse_timestamp(booking["ends_at"])
               for closure in [*state["closures"].values(), *additional])


def check_occupancy(state, candidates, excluded=()):
    others = [r for ref, r in state["reservations"].items()
              if ref not in excluded and r["status"] == "confirmed"]
    for candidate in candidates:
        if closure_conflict(state, candidate) or any(overlaps(candidate, r) for r in others):
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
    minutes = reservation["accepted_terms"]["cancellation_cutoff_minutes"]
    remaining = (parse_timestamp(reservation["starts_at"]) - datetime.now(UTC)).total_seconds()
    if remaining <= minutes * 60:
        fail(409, "cutoff_passed")


def changed(state, current, item):
    if "expected_revision" in item:
        expected = bounded_integer(item["expected_revision"], 1)
        if expected != current["revision"]:
            fail(409, "stale_revision")
    cutoff(state, current)
    # Preserve every value of no-op items, including imported timestamps.
    ids = selection(item, current)
    if set(ids) == set(table_ids(current)) and not any(key in item and not same_json(item[key], current[key])
               for key in ("starts_at_local", "party_size")):
        return copy.deepcopy(current)
    fields = validated_booking(state, current["restaurant_id"], ids,
                               item.get("starts_at_local", current["starts_at_local"]),
                               item.get("party_size", current["party_size"]))
    candidate = {**copy.deepcopy(current), **fields, "revision": current["revision"] + 1}
    if len(ids) != 1:
        candidate.pop("table_id", None)
    return candidate


def bump_restaurant(state, rid):
    state["restaurant_revisions"][rid] += 1


def append_history(state, booking, event, before=None, at=None, plan_id=None):
    entries = state["histories"].setdefault(booking["reference"], [])
    timestamp = at or datetime.now(UTC).isoformat(timespec="microseconds")
    if entries and parse_timestamp(timestamp) < parse_timestamp(entries[-1]["at"]):
        timestamp = entries[-1]["at"]
    changes = []
    if event != "cancelled":
        old_ids = table_ids(before) if before else None
        new_ids = table_ids(booking)
        if old_ids is None or set(old_ids) != set(new_ids):
            pair = event == "reassigned" or len(new_ids) == 2 or old_ids is not None and len(old_ids) == 2
            changes.append({"field": "table_ids" if pair else "table_id",
                            "from": old_ids if pair else old_ids[0] if old_ids else None,
                            "to": new_ids[:] if pair else new_ids[0]})
        for key in ("starts_at_local", "party_size"):
            if before is None or not same_json(before[key], booking[key]):
                changes.append({"field": key, "from": before[key] if before else None, "to": booking[key]})
    entries.append({"seq": len(entries) + 1, "at": timestamp, "event": event, "changes": changes,
                    "revision": booking["revision"], "accepted_terms": copy.deepcopy(booking["accepted_terms"])})
    if event == "reassigned":
        entries[-1]["plan_id"] = plan_id


def record_source(state, booking, kind, scope):
    # Private provenance keeps collective amendments separate from diner exceptions.
    seq = str(len(state["histories"][booking["reference"]]))
    state["mutation_sources"].setdefault(booking["reference"], {})[seq] = {"kind": kind, "operation": scope}


def new_booking(state, uid, fields):
    ref = secrets.token_hex(5).upper()
    while ref in state["reservations"]:
        ref = secrets.token_hex(5).upper()
    return {"reservation_id": "res_" + uuid.uuid4().hex, "reference": ref, "user_id": uid, **fields,
            "status": "confirmed", "revision": 1, "created_at": datetime.now(UTC).isoformat(timespec="microseconds")}


def containing_series(state, ref):
    return next((s for s in state["series"].values() if any(o["reference"] == ref for o in s["occurrences"])), None)


def series_view(state, series):
    return {"series_id": series["series_id"], "revision": series["revision"], "interval_weeks": series["interval_weeks"],
            "occurrences": [{**o, "reservation": view(state["reservations"][o["reference"]])} for o in series["occurrences"]]}


def commit_changes(state, currents, candidates):
    affected = set()
    changed_any = False
    for before, after in zip(currents, candidates):
        if after["revision"] == before["revision"]:
            continue
        changed_any = True
        state["reservations"][after["reference"]] = after
        append_history(state, after, "changed", before)
        series = containing_series(state, after["reference"])
        if series:
            affected.add(series["series_id"])
            next(o for o in series["occurrences"] if o["reference"] == after["reference"])["exception"] = True
    for sid in affected:
        state["series"][sid]["revision"] += 1
    if changed_any:
        bump_restaurant(state, candidates[0]["restaurant_id"])


def closure_input(body, restaurant):
    tid = identifier(field(body, "table_id", str))
    if tid not in {t["id"] for t in restaurant["tables"]}:
        fail(404, "not_found")
    for key in ("from", "to"):
        value = body.get(key)
        if not isinstance(value, str) or not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value):
            fail()
        parse_timestamp(value)
    if parse_timestamp(body["from"]) >= parse_timestamp(body["to"]):
        fail()
    return {"table_id": tid, "from": body["from"], "to": body["to"]}


def seating_plan(state, restaurant, closure):
    rid = restaurant["id"]
    start, end = parse_timestamp(closure["from"]), parse_timestamp(closure["to"])
    bookings = sorted((r for r in state["reservations"].values()
                       if r["restaurant_id"] == rid and r["status"] == "confirmed"
                       and parse_timestamp(r["starts_at"]) < end and start < parse_timestamp(r["ends_at"])),
                      key=lambda r: r["reference"])
    if len(restaurant["tables"]) > 6 or len(restaurant["combinable"]) > 4 or len(bookings) > 6:
        fail(422, "planning_limit")
    refs = {b["reference"] for b in bookings}
    fixed = [b for b in state["reservations"].values() if b["status"] == "confirmed" and b["reference"] not in refs]
    options = [[t["id"]] for t in restaurant["tables"]] + restaurant["combinable"]
    bits = {t["id"]: 1 << i for i, t in enumerate(restaurant["tables"])}
    choices = []
    for booking in bookings:
        candidates = []
        for rank, ids in enumerate(options):
            capacity = sum(booking["accepted_terms"]["capacities"][tid] for tid in ids)
            candidate = {**booking, "table_ids": ids}
            if capacity < booking["party_size"] or closure_conflict(state, candidate, [{"restaurant_id": rid, **closure}]):
                continue
            if any(overlaps(candidate, other) for other in fixed):
                continue
            candidates.append((int(set(ids) != set(table_ids(booking))), capacity - booking["party_size"],
                               rank, sum(bits[tid] for tid in ids)))
        if not candidates:
            fail(409, "no_feasible_plan")
        choices.append(sorted(candidates))
    time_conflicts = [[parse_timestamp(a["starts_at"]) < parse_timestamp(b["ends_at"])
                       and parse_timestamp(b["starts_at"]) < parse_timestamp(a["ends_at"])
                       for b in bookings] for a in bookings]
    suffix = [(0, 0)] * (len(bookings) + 1)
    for i in range(len(bookings) - 1, -1, -1):
        suffix[i] = (suffix[i + 1][0] + min(c[0] for c in choices[i]),
                     suffix[i + 1][1] + min(c[1] for c in choices[i]))
    best = None
    selected = []

    def search(index, moved, unused, masks, ranks):
        nonlocal best, selected
        if best is not None and (moved + suffix[index][0], unused + suffix[index][1]) > best[:2]:
            return
        if index == len(bookings):
            score = (moved, unused, tuple(ranks))
            if best is None or score < best:
                best, selected = score, ranks[:]
            return
        for change, waste, rank, mask in choices[index]:
            if any(time_conflicts[index][j] and mask & old for j, old in enumerate(masks)):
                continue
            search(index + 1, moved + change, unused + waste, [*masks, mask], [*ranks, rank])

    search(0, 0, 0, [], [])
    if best is None:
        fail(409, "no_feasible_plan")
    return {"plan_id": "plan_" + uuid.uuid4().hex, "restaurant_revision": state["restaurant_revisions"][rid],
            "closure": closure, "assignments": [{"reference": b["reference"], "table_ids": options[rank][:],
                 "changed": set(options[rank]) != set(table_ids(b))} for b, rank in zip(bookings, selected)],
            "moved_count": best[0], "unused_seats": best[1]}


def adoption_receipt(state, sid):
    return next(receipt["response"] for scope, receipt in state["receipts"].items()
                if json.loads(scope)[2] == "/series" and receipt["response"]["series_id"] == sid)


def validate_plans(state, checked_record):
    for pid, plan in state["plans"].items():
        identifier(pid)
        if not isinstance(plan, dict) or set(plan) != {"restaurant_id", "preview", "applied", "bookings"} or type(plan["applied"]) is not bool:
            fail()
        rid = plan["restaurant_id"]
        restaurant = state["restaurants"][rid]
        preview = plan["preview"]
        if set(preview) != {"plan_id", "restaurant_revision", "closure", "assignments", "moved_count", "unused_seats"} or preview["plan_id"] != pid:
            fail()
        bounded_integer(preview["restaurant_revision"], 0, state["restaurant_revisions"][rid])
        if not same_json(preview["closure"], closure_input(preview["closure"], restaurant)):
            fail()
        assignments, bookings = preview["assignments"], plan["bookings"]
        if not isinstance(assignments, list) or not isinstance(bookings, list) or len(assignments) != len(bookings) or len(bookings) > 6:
            fail()
        bounded_integer(preview["moved_count"], 0, len(bookings))
        bounded_integer(preview["unused_seats"], 0)
        refs, candidates = [], []
        moved, unused = 0, 0
        for assignment, booking in zip(assignments, bookings):
            checked_record(booking)
            if booking["status"] != "confirmed" or booking["restaurant_id"] != rid:
                fail()
            original = state["reservations"][booking["reference"]]
            if original["reservation_id"] != booking["reservation_id"]:
                fail()
            if set(assignment) != {"reference", "table_ids", "changed"} or assignment["reference"] != booking["reference"] or type(assignment["changed"]) is not bool:
                fail()
            fields = validated_booking(state, rid, assignment["table_ids"], booking["starts_at_local"],
                                       booking["party_size"], booking["accepted_terms"])
            ids = fields["table_ids"]
            if ids != assignment["table_ids"] or assignment["changed"] != (set(ids) != set(table_ids(booking))):
                fail()
            capacity = sum(booking["accepted_terms"]["capacities"][tid] for tid in ids)
            if capacity < booking["party_size"]:
                fail()
            moved += assignment["changed"]
            unused += capacity - booking["party_size"]
            candidate = {**booking, "table_ids": ids}
            closure = {"restaurant_id": rid, **preview["closure"]}
            if not (parse_timestamp(booking["starts_at"]) < parse_timestamp(closure["to"])
                    and parse_timestamp(closure["from"]) < parse_timestamp(booking["ends_at"])):
                fail()
            if closure_conflict({"closures": {}}, candidate, [closure]) or any(overlaps(candidate, prior) for prior in candidates):
                fail()
            candidates.append(candidate)
            refs.append(booking["reference"])
        if refs != sorted(set(refs)) or preview["moved_count"] != moved or preview["unused_seats"] != unused:
            fail()
        receipts = [(json.loads(scope)[2], receipt["response"]) for scope, receipt in state["receipts"].items()]
        if not any(path == f"/restaurants/{rid}/replans" and response.get("plan_id") == pid for path, response in receipts):
            fail()
        applications = [response for path, response in receipts if path == f"/restaurants/{rid}/replans/{pid}/apply"]
        if plan["applied"]:
            if len(applications) != 1 or state["closures"].get(pid) != {"restaurant_id": rid, **preview["closure"]}:
                fail()
            for assignment, before, after in zip(assignments, bookings, applications[0]["reservations"]):
                expected = copy.deepcopy(before)
                if assignment["changed"]:
                    expected["table_ids"] = assignment["table_ids"][:]
                    expected.pop("table_id", None)
                    if len(expected["table_ids"]) == 1:
                        expected["table_id"] = expected["table_ids"][0]
                    expected["revision"] += 1
                    entries = [e for e in state["histories"][before["reference"]] if e.get("plan_id") == pid]
                    if len(entries) != 1 or entries[0]["revision"] != expected["revision"] or entries[0]["changes"] != [{"field": "table_ids", "from": before["table_ids"], "to": expected["table_ids"]}]:
                        fail()
                if not same_json(expected, after):
                    fail()
        elif applications or pid in state["closures"]:
            fail()
    if set(state["closures"]) != {pid for pid, plan in state["plans"].items() if plan["applied"]}:
        fail()


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
        state["policies"][restaurant["id"]] = []
        state["restaurant_revisions"][restaurant["id"]] = 0
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
                   "status": status, "revision": 1, "created_at": datetime.now(UTC).isoformat(timespec="microseconds")}
        if status == "confirmed":
            check_occupancy(state, [booking])
        state["reservations"][ref] = booking
        append_history(state, booking, "created", at=booking["created_at"])
        if status == "cancelled":
            append_history(state, booking, "cancelled", at=booking["created_at"])
        ids.add(rid)
    return state


def imported_state(body):
    # State is portable JSON. Fully validate the candidate before the single replacement.
    if body.get("track") != "tablekeeper" or type(body.get("format_version")) is not int or body["format_version"] != 1:
        fail()
    state = copy.deepcopy(body.get("state"))
    legacy_keys = {"users", "restaurants", "reservations", "tokens", "receipts"}
    prior_keys = set(empty_state()) - {"plans", "closures", "mutation_sources"}
    if not isinstance(state, dict) or set(state) not in (legacy_keys, prior_keys, set(empty_state())):
        fail()
    legacy = set(state) == legacy_keys
    if legacy:
        state.update({"policies": {}, "histories": {}, "series": {}, "restaurant_revisions": {}})
    for key in ("plans", "closures", "mutation_sources"):
        state.setdefault(key, {})
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
            if configured != {"combinable": [], "manager_user_ids": [], **restaurant} or rid != restaurant["id"]:
                fail()
            state["restaurants"][rid] = configured
            if legacy:
                state["policies"][rid] = []
                state["restaurant_revisions"][rid] = 0
        if set(state["policies"]) != set(state["restaurants"]) or set(state["restaurant_revisions"]) != set(state["restaurants"]):
            fail()
        for rid, policies in state["policies"].items():
            if not isinstance(policies, list):
                fail()
            bounded_integer(state["restaurant_revisions"][rid], 0)
            for index, policy in enumerate(policies, 1):
                if not isinstance(policy, dict) or type(policy.get("policy_version")) is not int or policy["policy_version"] != index:
                    fail()
                if not same_json(policy, {**policy_config(policy, state["restaurants"][rid]), "policy_version": index}):
                    fail()

        def checked_terms(rid, terms):
            if not isinstance(terms, dict) or type(terms.get("policy_version")) is not int:
                fail()
            version = terms["policy_version"]
            if version == 0:
                expected = policy_zero(state["restaurants"][rid])
            elif 1 <= version <= len(state["policies"][rid]):
                expected = policy_terms(state["policies"][rid][version - 1])
            else:
                fail()
            if not same_json(terms, expected):
                fail()
            return terms

        def checked_record(record, include_owner=False, old=False):
            rid = record["restaurant_id"]
            terms = (policy_zero(state["restaurants"][rid]) if old
                     else checked_terms(rid, record["accepted_terms"]))
            fields = validated_booking(state, rid, table_ids(record), record["starts_at_local"], record["party_size"], terms)
            for key, value in fields.items():
                if old and key in ("accepted_terms", "table_ids"):
                    continue
                if not same_json(record[key], value):
                    fail()
            if old and set(table_ids(record)) != set(fields["table_ids"]):
                fail()
            base = {"reservation_id", "reference", "status", "created_at"}
            if include_owner:
                base.add("user_id")
            if old:
                allowed = (set(fields) - {"accepted_terms"}) | base
                if "table_ids" not in record:
                    allowed.remove("table_ids")
            else:
                bounded_integer(record["revision"], 1)
                allowed = set(fields) | base | {"revision"}
            if set(record) != allowed or record["status"] not in ("confirmed", "cancelled"):
                fail()
            identifier(record["reservation_id"])
            reference(record["reference"])
            parse_timestamp(record["created_at"])
            return fields
        ids = set()
        confirmed = []
        for ref, booking in state["reservations"].items():
            if reference(ref) != booking["reference"] or booking["user_id"] not in state["users"]:
                fail()
            rid = identifier(booking["reservation_id"])
            if rid in ids or booking["status"] not in ("confirmed", "cancelled"):
                fail()
            ids.add(rid)
            fields = checked_record(booking, include_owner=True, old=legacy)
            upgraded = {**booking, **fields, "revision": 1} if legacy else booking
            state["reservations"][ref] = upgraded
            if legacy:
                append_history(state, upgraded, "created", at=upgraded["created_at"])
                if upgraded["status"] == "cancelled":
                    append_history(state, upgraded, "cancelled", at=upgraded["created_at"])
            if booking["status"] == "confirmed":
                if any(overlaps(booking, other) for other in confirmed):
                    fail()
                confirmed.append(booking)
        if set(state["histories"]) != set(state["reservations"]):
            fail()
        for ref, entries in state["histories"].items():
            booking = state["reservations"][ref]
            if not isinstance(entries, list) or not entries:
                fail()
            previous_at = None
            previous_revision = 0
            for seq, entry in enumerate(entries, 1):
                allowed = {"seq", "at", "event", "changes", "revision", "accepted_terms"}
                if isinstance(entry, dict) and entry.get("event") == "reassigned":
                    allowed.add("plan_id")
                if not isinstance(entry, dict) or set(entry) != allowed:
                    fail()
                if type(entry["seq"]) is not int or entry["seq"] != seq:
                    fail()
                instant = parse_timestamp(entry["at"])
                revision = bounded_integer(entry["revision"], 1, booking["revision"])
                if previous_at is not None and (instant < previous_at or revision < previous_revision):
                    fail()
                previous_at, previous_revision = instant, revision
                checked_terms(booking["restaurant_id"], entry["accepted_terms"])
                event = entry["event"]
                if event not in ("created", "changed", "cancelled", "reassigned") or (seq == 1) != (event == "created"):
                    fail()
                if not isinstance(entry["changes"], list):
                    fail()
                if event == "cancelled" and (entry["changes"] or seq != len(entries)):
                    fail()
                names = []
                for change in entry["changes"]:
                    if not isinstance(change, dict) or set(change) != {"field", "from", "to"}:
                        fail()
                    if change["field"] not in ("table_id", "table_ids", "starts_at_local", "party_size"):
                        fail()
                    names.append(change["field"])
                order = {"table_id": 0, "table_ids": 0, "starts_at_local": 1, "party_size": 2}
                if len(names) != len(set(names)) or [order[n] for n in names] != sorted(set(order[n] for n in names)):
                    fail()
                if event == "created" and (len(names) != 3 or any(c["from"] is not None for c in entry["changes"])):
                    fail()
                if event == "changed" and not names:
                    fail()
                if event == "reassigned" and (names != ["table_ids"] or entry["plan_id"] not in state["plans"]):
                    fail()
            if previous_revision != booking["revision"] or not same_json(entries[-1]["accepted_terms"], booking["accepted_terms"]):
                fail()
            if (entries[-1]["event"] == "cancelled") != (booking["status"] == "cancelled"):
                fail()
        adopted = set()
        for sid, series in state["series"].items():
            if not isinstance(series, dict) or set(series) != {"series_id", "user_id", "restaurant_id", "revision", "interval_weeks", "occurrences"}:
                fail()
            if identifier(sid) != series["series_id"] or series["user_id"] not in state["users"]:
                fail()
            bounded_integer(series["revision"], 1)
            bounded_integer(series["interval_weeks"], 1, 4)
            occurrences = series["occurrences"]
            if not isinstance(occurrences, list) or not 2 <= len(occurrences) <= 12:
                fail()
            for index, occurrence in enumerate(occurrences):
                if not isinstance(occurrence, dict) or set(occurrence) != {"index", "reference", "exception"}:
                    fail()
                ref = occurrence["reference"]
                booking = state["reservations"][ref]
                if (type(occurrence["index"]) is not int or occurrence["index"] != index or type(occurrence["exception"]) is not bool
                        or ref in adopted or booking["user_id"] != series["user_id"] or booking["restaurant_id"] != series["restaurant_id"]):
                    fail()
                adopted.add(ref)
        for token, uid in state["tokens"].items():
            if not token or uid not in state["users"]:
                fail()
        adoption_revisions = {}
        for scope, receipt in state["receipts"].items():
            uid, method, path, key = json.loads(scope)
            policy_path = re.fullmatch(r"/restaurants/([^/]+)/policies", path)
            replan_path = re.fullmatch(r"/restaurants/([^/]+)/replans(?:/([^/]+)/apply)?", path)
            amend_path = re.fullmatch(r"/series/([^/]+)/amend", path)
            if (uid not in state["users"] or method != "POST" or path not in ("/reservations", "/reservation-moves", "/series") and not (policy_path or replan_path or amend_path)
                    or not isinstance(key, str) or not 1 <= len(key) <= 255
                    or not isinstance(receipt, dict) or set(receipt) != {"body", "response"}
                    or not isinstance(receipt["body"], dict) or not isinstance(receipt["response"], dict)):
                fail()
            if policy_path:
                rid = unquote(policy_path[1])
                response = receipt["response"]
                version = bounded_integer(response["policy_version"], 1, len(state["policies"][rid]))
                if not same_json(response, state["policies"][rid][version - 1]):
                    fail()
                continue
            if replan_path:
                response = receipt["response"]
                rid = unquote(replan_path[1])
                plan = state["plans"][response["plan_id"]]
                if plan["restaurant_id"] != rid or uid not in state["restaurants"][rid]["manager_user_ids"]:
                    fail()
                if replan_path[2] is None:
                    if not same_json(response, plan["preview"]):
                        fail()
                    if not same_json(closure_input(receipt["body"], state["restaurants"][rid]), response["closure"]):
                        fail()
                    continue
                if unquote(replan_path[2]) != response["plan_id"] or not plan["applied"]:
                    fail()
                if set(response) != {"plan_id", "restaurant_revision", "reservations"} or response["restaurant_revision"] != plan["preview"]["restaurant_revision"] + 1:
                    fail()
                responses = response["reservations"]
                if [r["reference"] for r in responses] != [a["reference"] for a in plan["preview"]["assignments"]]:
                    fail()
                for record, assignment in zip(responses, plan["preview"]["assignments"]):
                    if record["table_ids"] != assignment["table_ids"] or record["restaurant_id"] != rid:
                        fail()
            elif amend_path:
                series = state["series"][unquote(amend_path[1])]
                response = receipt["response"]
                if series["user_id"] != uid or response["series_id"] != series["series_id"] or response["interval_weeks"] != series["interval_weeks"]:
                    fail()
                bounded_integer(response["revision"], 1, series["revision"])
                if len(response["occurrences"]) != len(series["occurrences"]):
                    fail()
                for item, stored in zip(response["occurrences"], series["occurrences"]):
                    if item["reference"] != stored["reference"] or item["index"] != stored["index"] or type(item["exception"]) is not bool:
                        fail()
                responses = [o["reservation"] for o in response["occurrences"]]
            elif path == "/series":
                response = receipt["response"]
                series = state["series"][response["series_id"]]
                if series["user_id"] != uid or response["revision"] != 1 or response["interval_weeks"] != series["interval_weeks"]:
                    fail()
                occurrences = response["occurrences"]
                if len(occurrences) != len(series["occurrences"]):
                    fail()
                for item, stored in zip(occurrences, series["occurrences"]):
                    if item["reference"] != stored["reference"] or item["index"] != stored["index"] or item["exception"] is not False:
                        fail()
                sid = series["series_id"]
                if sid in adoption_revisions:
                    fail()
                adoption_revisions[sid] = occurrences[0]["reservation"]["revision"]
                responses = [o["reservation"] for o in occurrences]
            else:
                responses = ([receipt["response"]] if path == "/reservations" else receipt["response"]["reservations"])
            if not isinstance(responses, list) or not responses and not replan_path:
                fail()
            for response in responses:
                original = state["reservations"][response["reference"]]
                if (not replan_path and original["user_id"] != uid) or original["reservation_id"] != response["reservation_id"]:
                    fail()
                checked_record(response, old="accepted_terms" not in response)
                if response["status"] != "confirmed" and not amend_path:
                    fail()
        validate_plans(state, checked_record)
        for booking in confirmed:
            if closure_conflict(state, booking):
                fail()
        for ref, sources in state["mutation_sources"].items():
            if ref not in state["histories"] or not isinstance(sources, dict):
                fail()
            for seq, source in sources.items():
                if not isinstance(seq, str) or not re.fullmatch(r"[1-9][0-9]*", seq) or not isinstance(source, dict) or set(source) != {"kind", "operation"}:
                    fail()
                entry = state["histories"][ref][int(seq) - 1]
                receipt = state["receipts"][source["operation"]]
                uid, method, path, key = json.loads(source["operation"])
                if source["kind"] == "series_amend":
                    if entry["event"] != "changed" or not re.fullmatch(r"/series/[^/]+/amend", path):
                        fail()
                    records = [o["reservation"] for o in receipt["response"]["occurrences"]]
                elif source["kind"] == "replan":
                    if entry["event"] != "reassigned" or not re.fullmatch(r"/restaurants/[^/]+/replans/[^/]+/apply", path) or receipt["response"]["plan_id"] != entry["plan_id"]:
                        fail()
                    records = receipt["response"]["reservations"]
                else:
                    fail()
                record = next((r for r in records if r["reference"] == ref), None)
                if record is None or record["revision"] != entry["revision"] or not same_json(record["accepted_terms"], entry["accepted_terms"]):
                    fail()
        for ref, entries in state["histories"].items():
            for entry in entries:
                if entry["event"] == "reassigned" and state["mutation_sources"].get(ref, {}).get(str(entry["seq"]), {}).get("kind") != "replan":
                    fail()
        # Every agreement originates in a completed idempotent adoption. Its
        # immutable receipt identifies the anchor revision at adoption, so prior
        # anchor amendments are not mistaken for series mutations.
        if set(adoption_revisions) != set(state["series"]):
            fail()
        for sid, series in state["series"].items():
            anchor = state["reservations"][series["occurrences"][0]["reference"]]
            adopted_revision = bounded_integer(adoption_revisions[sid], 1, anchor["revision"])
            changes = []
            cancellations = 0
            collective = set()
            for occurrence in series["occurrences"]:
                entries = state["histories"][occurrence["reference"]]
                post_adoption = (entries if occurrence["index"] else
                                 [entry for entry in entries if entry["revision"] > adopted_revision])
                changed_count = 0
                for entry in post_adoption:
                    source = state["mutation_sources"].get(occurrence["reference"], {}).get(str(entry["seq"]))
                    if source:
                        collective.add(source["operation"])
                    elif entry["event"] == "reassigned":
                        fail()
                    elif entry["event"] == "changed":
                        changed_count += 1
                if occurrence["exception"] != bool(changed_count):
                    fail()
                changes.append(changed_count)
                cancellations += sum(entry["event"] == "cancelled" for entry in post_adoption)
            # Each cancellation is individual. One collective move can change
            # at most eight distinct occurrences, incrementing this series once.
            # Histories don't record batch grouping; require the tight derivable
            # bounds without rejecting a valid multi-occurrence batch export.
            changed_operations_min = max(max(changes), (sum(changes) + 7) // 8)
            minimum = 1 + cancellations + changed_operations_min + len(collective)
            maximum = 1 + cancellations + sum(changes) + len(collective)
            if not minimum <= series["revision"] <= maximum:
                fail()
        return copy.deepcopy(state)
    except (APIError, KeyError, TypeError, ValueError, OverflowError, IndexError):
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
    policy_match = re.fullmatch(r"/restaurants/([^/]+)/policies", path)
    if method == "GET" and policy_match:
        rid = identifier(unquote(policy_match[1]))
        if rid not in state["restaurants"]:
            fail(404, "not_found")
        return 200, {"policies": state["policies"][rid]}
    if method == "GET" and re.fullmatch(r"/restaurants/[^/]+", path):
        rid = identifier(unquote(path.split("/")[2]))
        if rid not in state["restaurants"]:
            fail(404, "not_found")
        return 200, state["restaurants"][rid]
    if method == "GET" and path == "/availability":
        if "explain" in query and query["explain"] != ["true"]:
            fail()
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
        terms = applicable_terms(state, rid, day)
        slots = []
        hours = hours_for(terms, day)
        if hours:
            zone = ZoneInfo(restaurant["timezone"])
            opening, closing = day_time(day, hours["opens"]), day_time(day, hours["closes"])
            close_utc = resolve(closing, zone).astimezone(UTC)
            minute = opening.hour * 60 + opening.minute
            while minute < closing.hour * 60 + closing.minute:
                naive = day.replace(hour=minute // 60, minute=minute % 60)
                minute += terms["slot_minutes"]
                try:
                    start = resolve(naive, zone)
                    end = start.astimezone(UTC) + timedelta(minutes=terms["reservation_duration_minutes"])
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
                explanations = []
                for table in restaurant["tables"]:
                    capacity = terms["capacities"][table["id"]]
                    enough = capacity >= size
                    interval["table_ids"] = [table["id"]]
                    free = not closure_conflict(state, interval) and not any(r["status"] == "confirmed" and overlaps(interval, r) for r in state["reservations"].values())
                    explanations.append({"table_id": table["id"], "policy_version": terms["policy_version"], "available": enough and free,
                                         "rules": [{"rule": "capacity", "holds": enough}, {"rule": "no_overlap", "holds": free}]})
                    if enough and free:
                        available.append(table["id"])
                        options.append({"table_ids": [table["id"]], "capacity": capacity})
                for pair in restaurant["combinable"]:
                    capacity = sum(terms["capacities"][tid] for tid in pair)
                    interval["table_ids"] = pair
                    if capacity >= size and not closure_conflict(state, interval) and not any(r["status"] == "confirmed" and overlaps(interval, r) for r in state["reservations"].values()):
                        options.append({"table_ids": pair[:], "capacity": capacity})
                slot = {"starts_at_local": naive.isoformat(timespec="minutes"), "starts_at": interval["starts_at"], "available_table_ids": available, "available_options": options}
                if "explain" in query:
                    slot["explain"] = explanations
                slots.append(slot)
        return 200, {"restaurant_id": rid, "date": params["date"], "timezone": restaurant["timezone"], "slots": slots}
    private_read = (method == "GET" and (re.fullmatch(r"/reservations/[^/]+/(history|decision)", path)
                                         or re.fullmatch(r"/series/[^/]+", path)))
    if private_read:
        try:
            uid = auth(state, headers.get("Authorization"))
        except APIError:
            fail(404, "not_found")
        record_match = re.fullmatch(r"/reservations/([^/]+)/(history|decision)", path)
        if record_match:
            ref = identifier(unquote(record_match[1]))
            current = owned(state, ref, uid)
            if record_match[2] == "history":
                return 200, {"reference": ref, "entries": state["histories"][ref]}
            return 200, {"reference": ref, "revision": current["revision"], "accepted_terms": current["accepted_terms"]}
        sid = identifier(unquote(path.split("/")[2]))
        series = state["series"].get(sid)
        if not series or series["user_id"] != uid:
            fail(404, "not_found")
        return 200, series_view(state, series)
    uid = auth(state, headers.get("Authorization"))
    scope = None
    replan_match = re.fullmatch(r"/restaurants/([^/]+)/replans(?:/([^/]+)/apply)?", path)
    amend_match = re.fullmatch(r"/series/([^/]+)/amend", path)
    if method == "POST" and (path in ("/reservations", "/reservation-moves", "/series") or policy_match or replan_match or amend_match):
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
    if method == "POST" and replan_match:
        rid = identifier(unquote(replan_match[1]))
        restaurant = state["restaurants"].get(rid)
        if restaurant is None:
            fail(404, "not_found")
        if uid not in restaurant["manager_user_ids"]:
            fail(403, "forbidden")
        if replan_match[2] is None:
            response = seating_plan(state, restaurant, closure_input(body, restaurant))
            state["plans"][response["plan_id"]] = {"restaurant_id": rid, "preview": copy.deepcopy(response), "applied": False,
                "bookings": [copy.deepcopy(view(state["reservations"][a["reference"]])) for a in response["assignments"]]}
        else:
            pid = identifier(unquote(replan_match[2]))
            plan = state["plans"].get(pid)
            if plan is None or plan["restaurant_id"] != rid:
                fail(404, "not_found")
            if plan["applied"]:
                fail(409, "plan_already_applied")
            preview = plan["preview"]
            if preview["restaurant_revision"] != state["restaurant_revisions"][rid]:
                fail(409, "stale_plan")
            affected = set()
            reservations = []
            for assignment in preview["assignments"]:
                before = state["reservations"][assignment["reference"]]
                after = copy.deepcopy(before)
                if assignment["changed"]:
                    after["table_ids"] = assignment["table_ids"][:]
                    if len(after["table_ids"]) == 1:
                        after["table_id"] = after["table_ids"][0]
                    else:
                        after.pop("table_id", None)
                    after["revision"] += 1
                    state["reservations"][after["reference"]] = after
                    append_history(state, after, "reassigned", before, plan_id=pid)
                    record_source(state, after, "replan", scope)
                    series = containing_series(state, after["reference"])
                    if series:
                        affected.add(series["series_id"])
                reservations.append(view(after))
            state["closures"][pid] = {"restaurant_id": rid, **preview["closure"]}
            plan["applied"] = True
            for sid in affected:
                state["series"][sid]["revision"] += 1
            bump_restaurant(state, rid)
            response = {"plan_id": pid, "restaurant_revision": state["restaurant_revisions"][rid], "reservations": reservations}
        state["receipts"][scope] = {"body": copy.deepcopy(body), "response": copy.deepcopy(response)}
        return 201, response
    if method == "POST" and amend_match:
        sid = identifier(unquote(amend_match[1]))
        series = state["series"].get(sid)
        if series is None or series["user_id"] != uid:
            fail(404, "not_found")
        expected = bounded_integer(body.get("expected_revision"), 1)
        if expected != series["revision"]:
            fail(409, "stale_revision")
        from_index = bounded_integer(body.get("from_index"), 0, len(series["occurrences"]) - 1)
        clock = body.get("local_time")
        if not isinstance(clock, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock):
            fail()
        original = adoption_receipt(state, sid)
        currents, candidates = [], []
        for occurrence in series["occurrences"][from_index:]:
            current = state["reservations"][occurrence["reference"]]
            if occurrence["exception"] or current["status"] == "cancelled":
                continue
            date = original["occurrences"][occurrence["index"]]["reservation"]["starts_at_local"][:10]
            starts_at = date + "T" + clock
            currents.append(current)
            candidates.append(copy.deepcopy(current) if starts_at == current["starts_at_local"]
                              else changed(state, current, {"starts_at_local": starts_at}))
        check_occupancy(state, candidates, [b["reference"] for b in currents])
        any_change = False
        for before, after in zip(currents, candidates):
            if before["revision"] == after["revision"]:
                continue
            any_change = True
            state["reservations"][after["reference"]] = after
            append_history(state, after, "changed", before)
            record_source(state, after, "series_amend", scope)
        if any_change:
            series["revision"] += 1
            bump_restaurant(state, series["restaurant_id"])
        response = series_view(state, series)
        state["receipts"][scope] = {"body": copy.deepcopy(body), "response": copy.deepcopy(response)}
        return 201, response
    if method == "POST" and policy_match:
        rid = identifier(unquote(policy_match[1]))
        restaurant = state["restaurants"].get(rid)
        if not restaurant:
            fail(404, "not_found")
        if uid not in restaurant["manager_user_ids"]:
            fail(403, "forbidden")
        policy = {**policy_config(body, restaurant), "policy_version": len(state["policies"][rid]) + 1}
        state["policies"][rid].append(policy)
        bump_restaurant(state, rid)
        state["receipts"][scope] = {"body": copy.deepcopy(body), "response": copy.deepcopy(policy)}
        return 201, policy
    if method == "POST" and path == "/reservations":
        fields = validated_booking(state, field(body, "restaurant_id", str), selection(body),
                                   field(body, "starts_at_local", str), body.get("party_size"))
        check_occupancy(state, [fields])
        booking = new_booking(state, uid, fields)
        ref = booking["reference"]
        response = view(booking)
        state["reservations"][ref] = booking
        append_history(state, booking, "created", at=booking["created_at"])
        bump_restaurant(state, fields["restaurant_id"])
        state["receipts"][scope] = {"body": copy.deepcopy(body), "response": copy.deepcopy(response)}
        return 201, response
    if method == "POST" and path == "/series":
        count = bounded_integer(body.get("count"), 2, 12)
        weeks = bounded_integer(body.get("interval_weeks"), 1, 4)
        ref = identifier(field(body, "anchor_reference", str))
        anchor = owned(state, ref, uid)
        if anchor["status"] == "cancelled":
            fail(409, "reservation_cancelled")
        if containing_series(state, ref):
            fail(409, "already_in_series")
        cutoff(state, anchor)
        naive = local_datetime(anchor["starts_at_local"])
        generated = []
        for index in range(1, count):
            try:
                occurrence_date = naive + timedelta(days=index * weeks * 7)
            except OverflowError:
                fail()
            fields = validated_booking(state, anchor["restaurant_id"], table_ids(anchor),
                                       occurrence_date.isoformat(timespec="minutes"), anchor["party_size"])
            check_occupancy(state, [*generated, fields])
            # New identities are allocated locally; failures never expose or commit them.
            booking = new_booking(state, uid, fields)
            while any(b["reference"] == booking["reference"] for b in generated):
                booking = new_booking(state, uid, fields)
            generated.append(booking)
        sid = "ser_" + uuid.uuid4().hex
        series = {"series_id": sid, "user_id": uid, "restaurant_id": anchor["restaurant_id"], "revision": 1,
                  "interval_weeks": weeks, "occurrences": [{"index": i, "reference": b["reference"], "exception": False}
                                                           for i, b in enumerate([anchor, *generated])]}
        for booking in generated:
            state["reservations"][booking["reference"]] = booking
            append_history(state, booking, "created", at=booking["created_at"])
        state["series"][sid] = series
        bump_restaurant(state, anchor["restaurant_id"])
        response = series_view(state, series)
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
        currents = []
        restaurant_id = None
        for ref, item in zip(refs, moves):
            current = owned(state, ref, uid)
            if restaurant_id is not None and current["restaurant_id"] != restaurant_id:
                fail()
            restaurant_id = current["restaurant_id"]
            currents.append(current)
            candidates.append(changed(state, current, item))
        check_occupancy(state, candidates, refs)
        response = {"reservations": [view(r) for r in candidates]}
        commit_changes(state, currents, candidates)
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
                current["revision"] += 1
                append_history(state, current, "cancelled")
                bump_restaurant(state, current["restaurant_id"])
                series = containing_series(state, ref)
                if series:
                    series["revision"] += 1
            return 200, view(current)
        if method == "PATCH" and not match[2]:
            candidate = changed(state, current, body)
            check_occupancy(state, [candidate], [ref])
            commit_changes(state, [current], [candidate])
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
