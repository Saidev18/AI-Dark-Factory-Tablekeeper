"""Specification-derived black-box tests. No implementation imports or shipped tests."""
import argparse
import concurrent.futures
import copy
import json
import re
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SOURCE = "http://127.0.0.1:18081"
DESTINATION = "http://127.0.0.1:18082"


def request(method, path, body=None, token=None, key=None, base=None, raw=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if key is not None:
        headers["Idempotency-Key"] = key
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = Request((base or SOURCE) + path, data=data, headers=headers, method=method)
    try:
        response = urlopen(req, timeout=10 if path.startswith("/_test/") else 5)
    except HTTPError as error:
        response = error
    with response:
        payload = response.read()
        assert response.headers["Content-Type"].lower() == "application/json; charset=utf-8"
        value = json.loads(payload) if payload else None
        assert response.status < 500, (response.status, value)
        if response.status >= 400:
            assert isinstance(value["error"]["message"], str)
        return response.status, value


def fixture(zone="Europe/Berlin", hours=("18:00", "23:00"), duration=90):
    return {"users": [{"id": "ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada"},
                      {"id": "bob", "email": "bob@example.com", "password": "correct horse", "display_name": "Bob"}],
            "restaurants": [{"id": "r", "name": "Restaurant", "timezone": zone, "slot_minutes": 30,
                             "reservation_duration_minutes": duration, "cancellation_cutoff_minutes": 120,
                             "opening_hours": [{"weekday": day, "opens": hours[0], "closes": hours[1]}
                                               for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")],
                             "tables": [{"id": "t1", "label": "One", "capacity": 2},
                                        {"id": "t2", "label": "Two", "capacity": 4}]}],
            "reservations": []}


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(request("POST", "/_test/reset", fixture())[0], 204)
        self.token = self.login("ada")
        self.bob = self.login("bob")

    def login(self, user, base=None):
        status, result = request("POST", "/auth/login", {"email": user + "@example.com", "password": "correct horse"}, base=base)
        self.assertEqual(status, 200)
        return result["token"]

    def booking(self, table="t2", start="2036-09-24T19:00", size=4, **extras):
        return {"restaurant_id": "r", "table_id": table, "starts_at_local": start, "party_size": size, **extras}

    def create(self, body=None, key="first", token=None, base=None):
        return request("POST", "/reservations", body or self.booking(), token or self.token, key, base)

    def error(self, result, status, code):
        self.assertEqual(result[0], status, result)
        self.assertEqual(result[1]["error"]["code"], code, result)

    def test_public_browsing_and_closed_days(self):
        self.assertEqual(request("GET", "/health"), (200, {"status": "ok"}))
        self.assertEqual(request("GET", "/restaurants")[1]["restaurants"][0]["id"], "r")
        self.assertEqual(request("GET", "/restaurants/r")[1], {**fixture()["restaurants"][0], "combinable": []})
        slots = request("GET", "/availability?restaurant_id=r&date=2036-09-24&party_size=2&ignored=yes")[1]["slots"]
        self.assertEqual(len(slots), 8)
        self.assertEqual(slots[0]["available_table_ids"], ["t1", "t2"])
        self.assertEqual(slots[-1]["starts_at_local"], "2036-09-24T21:30")
        self.assertEqual(request("GET", "/availability?restaurant_id=r&date=2036-09-24&party_size=20")[1]["slots"][0]["available_table_ids"], [])
        data = fixture()
        data["restaurants"][0]["opening_hours"] = []
        request("POST", "/_test/reset", data)
        self.assertEqual(request("GET", "/availability?restaurant_id=r&date=2036-09-24&party_size=2")[1]["slots"], [])
        self.error(request("GET", "/restaurants/unknown"), 404, "not_found")

    def test_signup_login_sessions_and_password_hashes(self):
        data = {"email": "new@example.com", "password": "long password", "display_name": "New", "ignored": 1}
        status, first = request("POST", "/auth/signup", data)
        self.assertEqual(status, 201)
        second = request("POST", "/auth/login", data)[1]
        self.assertNotEqual(first["token"], second["token"])
        for token in (first["token"], second["token"]):
            self.assertEqual(request("GET", "/reservations", token=token), (200, {"reservations": []}))
        self.error(request("POST", "/auth/signup", data), 409, "email_taken")
        for patch in ({"password": "short"}, {"email": "bad"}, {"email": "a@@b"}):
            self.error(request("POST", "/auth/signup", {**data, **patch}), 422, "validation_failed")
        self.error(request("POST", "/auth/login", {**data, "password": "incorrect"}), 401, "unauthenticated")
        self.error(request("POST", "/auth/login", {**data, "email": "unknown@example.com"}), 401, "unauthenticated")
        state = request("GET", "/_test/export")[1]["state"]
        for user in state["users"].values():
            self.assertNotIn("password", user)
            self.assertEqual(user["password_hash"]["algorithm"], "scrypt")
        self.error(request("GET", "/reservations"), 401, "unauthenticated")
        self.error(request("GET", "/reservations", token="unknown"), 401, "unauthenticated")

    def test_validation_and_types(self):
        for query in ("", "restaurant_id=r", "restaurant_id=r&date=2026-02-30&party_size=4",
                      "restaurant_id=r&date=2026-01-01&party_size=1e9", "restaurant_id=r&date=2026-01-01&party_size=4.0",
                      "restaurant_id=r&date=2026-01-01&party_size=%2B4", "restaurant_id=r&date=2026-01-01&party_size=0"):
            self.error(request("GET", "/availability?" + query), 422, "validation_failed")
        self.error(request("POST", "/reservations", self.booking(), self.token), 400, "missing_idempotency_key")
        self.error(self.create(key="k" * 256), 422, "validation_failed")
        self.error(request("POST", "/reservations", token=self.token, key="bad", raw=b"{"), 400, "malformed_request")
        self.error(request("POST", "/reservations", token=self.token, key="bad", raw=b"[]"), 400, "malformed_request")
        for size in ("4", True, False, 0, -1, 1.5, None):
            self.error(self.create(self.booking(size=size), "bad"), 422, "validation_failed")
        for start in ("2036-09-24T19:00Z", "2036-09-24T19:00+02:00", "2036-09-24T19:00:00", "2036-02-30T19:00", "x"):
            self.error(self.create(self.booking(start=start), "bad"), 422, "validation_failed")
        for patch in ({"table_id": []}, {"restaurant_id": 3}, {"starts_at_local": 3}):
            self.error(self.create({**self.booking(), **patch}, "bad"), 400, "malformed_request")
        for patch, code in (({"party_size": 5}, "party_exceeds_capacity"), ({"starts_at_local": "2036-09-24T19:15"}, "not_on_slot_grid"),
                            ({"starts_at_local": "2036-09-24T17:00"}, "outside_opening_hours"),
                            ({"starts_at_local": "2036-09-24T22:00"}, "outside_opening_hours")):
            self.error(self.create({**self.booking(), **patch}, "bad"), 422, code)
        self.error(self.create({**self.booking(), "table_id": "missing"}, "bad"), 404, "not_found")
        self.error(self.create({**self.booking(), "table_id": "x" * 65}, "bad"), 422, "validation_failed")
        self.assertEqual(self.create(key="bad")[0], 201)

    def test_half_open_overlap_visibility_and_sorting(self):
        status, first = self.create()
        self.assertEqual(status, 201)
        self.assertRegex(first["reference"], r"^[A-Z0-9]{6,12}$")
        self.assertEqual(first["ends_at"], "2036-09-24T20:30:00+02:00")
        self.error(self.create(self.booking(start="2036-09-24T20:00"), "overlap"), 409, "table_unavailable")
        second = self.create(self.booking(start="2036-09-24T20:30"), "adjacent")[1]
        self.assertEqual(request("GET", "/reservations", token=self.token)[1]["reservations"], [second, first])
        path = "/reservations/" + first["reference"]
        for method, suffix, body in (("GET", "", None), ("PATCH", "", {}), ("POST", "/cancel", {})):
            self.error(request(method, path + suffix, body, self.bob), 404, "not_found")

    def test_replays_ordering_scopes_and_immutable_receipts(self):
        body = self.booking(unknown={"nested": [True, 1]})
        first = self.create(body)[1]
        self.assertEqual(self.create(dict(reversed(list(body.items())))), (200, first))
        self.error(self.create({"invalid": True}), 409, "idempotency_key_reuse")
        self.error(self.create(self.booking(unknown={"nested": [1, 1]})), 409, "idempotency_key_reuse")
        bob_body = self.booking(table="t1", size=2)
        self.assertEqual(self.create(bob_body, token=self.bob)[0], 201)
        batch_body = {"moves": [{"reference": first["reference"]}], **body}
        self.assertEqual(request("POST", "/reservation-moves", batch_body, self.token, "first")[0], 201)
        path = "/reservations/" + first["reference"]
        self.assertEqual(request("POST", path + "/cancel", {}, self.token)[0], 200)
        self.assertEqual(self.create(body), (200, first))
        self.assertEqual(request("GET", path, token=self.token)[1]["status"], "cancelled")
        self.assertEqual(request("GET", "/availability?restaurant_id=r&date=2036-09-24&party_size=4")[1]["slots"][2]["available_table_ids"], ["t2"])

    def test_patch_cancel_and_failed_atomic_amendment(self):
        first = self.create(self.booking(size=2))[1]
        second = self.create(self.booking(table="t1", size=2), "second")[1]
        path = "/reservations/" + first["reference"]
        self.error(request("PATCH", path, {"table_id": "t1"}, self.token), 409, "table_unavailable")
        self.assertEqual(request("GET", path, token=self.token)[1], first)
        updated = request("PATCH", path, {"starts_at_local": "2036-09-24T20:30", "party_size": 1, "ignored": True}, self.token)[1]
        for key in ("reference", "reservation_id", "created_at"):
            self.assertEqual(updated[key], first[key])
        self.assertEqual(self.create(key="freed")[0], 201)
        cancelled = request("POST", path + "/cancel", {}, self.token)
        self.assertEqual(cancelled[0], 200)
        self.assertEqual(request("POST", path + "/cancel", token=self.token), cancelled)
        self.error(request("PATCH", path, {}, self.token), 409, "reservation_cancelled")
        self.assertEqual(request("GET", "/reservations/" + second["reference"], token=self.token)[1], second)

    def test_past_bookings_seed_and_cutoff_current_start(self):
        data = fixture()
        data["reservations"] = [{**self.booking(start="2001-01-01T19:00"), "id": "seed", "reference": "SEEDED", "user_id": "ada"}]
        self.assertEqual(request("POST", "/_test/reset", data)[0], 204)
        self.token = self.login("ada")
        seed = request("GET", "/reservations/SEEDED", token=self.token)[1]
        self.assertEqual(seed["reservation_id"], "seed")
        self.error(request("POST", "/reservations/SEEDED/cancel", {}, self.token), 409, "cutoff_passed")
        self.error(request("PATCH", "/reservations/SEEDED", {"starts_at_local": "2036-09-24T19:00"}, self.token), 409, "cutoff_passed")
        self.assertEqual(self.create(self.booking(start="2001-01-02T19:00"))[0], 201)
        current = self.create(key="future")[1]
        path = "/reservations/" + current["reference"]
        self.assertEqual(request("PATCH", path, {"starts_at_local": "2001-01-03T19:00"}, self.token)[0], 200)
        self.error(request("PATCH", path, {"starts_at_local": "2036-09-24T19:00"}, self.token), 409, "cutoff_passed")

    def test_50_concurrent_same_key_exactly_one_create(self):
        barrier = threading.Barrier(50)
        def worker(index):
            barrier.wait()
            return self.create(key="concurrent")
        start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(worker, range(50)))
        self.assertEqual([s for s, _ in results].count(201), 1)
        self.assertEqual([s for s, _ in results].count(200), 49)
        self.assertTrue(all(body == results[0][1] for _, body in results))
        self.assertEqual(len(request("GET", "/reservations", token=self.token)[1]["reservations"]), 1)
        print(f"50 identical requests: {time.monotonic() - start:.3f}s", flush=True)

    def test_50_concurrent_distinct_keys_single_occupant(self):
        barrier = threading.Barrier(50)
        def worker(index):
            barrier.wait()
            return self.create(key="race-" + str(index), token=self.token if index % 2 else self.bob)
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(worker, range(50)))
        self.assertEqual([s for s, _ in results].count(201), 1)
        self.assertEqual([s for s, _ in results].count(409), 49)
        self.assertTrue(all(s == 201 or b["error"]["code"] == "table_unavailable" for s, b in results))

    def test_dst_both_zones_spring_and_fall(self):
        cases = (("Europe/Berlin", "2026-03-29", "2026-10-25", "02:30", "+02:00", "03:00:00+01:00"),
                 ("America/New_York", "2026-03-08", "2026-11-01", "01:30", "-04:00", "02:00:00-05:00"))
        for zone, spring, fall, repeated, offset, end in cases:
            with self.subTest(zone=zone):
                request("POST", "/_test/reset", fixture(zone, ("00:00", "05:00")))
                self.token = self.login("ada")
                slots = request("GET", f"/availability?restaurant_id=r&date={spring}&party_size=2")[1]["slots"]
                self.assertFalse(any("T02:" in s["starts_at_local"] for s in slots))
                self.error(self.create(self.booking(start=spring + "T02:30"), "gap"), 422, "invalid_local_time")
                crossing = self.create(self.booking(start=spring + "T01:30"), "cross-spring")[1]
                self.assertEqual(crossing["ends_at"][11:16], "04:00")
                self.error(self.create(self.booking(start=spring + "T03:30"), "absolute-overlap"), 409, "table_unavailable")
                created = self.create(self.booking(start=fall + "T" + repeated), "fall")[1]
                self.assertTrue(created["starts_at"].endswith(offset), created)
                self.assertTrue(created["ends_at"].endswith(end), created)
                self.assertEqual((datetime.fromisoformat(created["ends_at"]).astimezone(timezone.utc) - datetime.fromisoformat(created["starts_at"]).astimezone(timezone.utc)).total_seconds(), 5400)
                slots = request("GET", f"/availability?restaurant_id=r&date={fall}&party_size=2")[1]["slots"]
                self.assertEqual(sum(s["starts_at_local"] == fall + "T" + repeated for s in slots), 1)

    def test_batch_swap_noop_replay_and_failed_key_reuse(self):
        a = self.create(self.booking(size=2), "a")[1]
        b = self.create(self.booking(table="t1", size=2), "b")[1]
        body = {"moves": [{"reference": a["reference"], "table_id": "t1"}, {"reference": b["reference"], "table_id": "t2"}]}
        status, swapped = request("POST", "/reservation-moves", body, self.token, "swap")
        self.assertEqual(status, 201)
        self.assertEqual([r["table_id"] for r in swapped["reservations"]], ["t1", "t2"])
        self.assertEqual(request("POST", "/reservation-moves", body, self.token, "swap"), (200, swapped))
        noops = {"moves": [{"reference": a["reference"], "ignored": True}, {"reference": b["reference"]}]}
        self.assertEqual(request("POST", "/reservation-moves", noops, self.token, "noops"), (201, swapped))
        conflict = {"moves": [{"reference": a["reference"], "table_id": "t2"}]}
        self.error(request("POST", "/reservation-moves", conflict, self.token, "retryable"), 409, "table_unavailable")
        self.assertEqual(request("POST", "/reservation-moves", noops, self.token, "retryable")[0], 201)
        self.error(request("POST", "/reservation-moves", {"moves": []}, self.token, "swap"), 409, "idempotency_key_reuse")
        request("POST", "/reservations/" + a["reference"] + "/cancel", {}, self.token)
        self.assertEqual(request("POST", "/reservation-moves", body, self.token, "swap"), (200, swapped))

    def test_batch_shape_ownership_capacity_and_atomic_rollback(self):
        a = self.create(self.booking(size=2), "a")[1]
        b = self.create(self.booking(table="t1", size=2), "b")[1]
        before = request("GET", "/reservations", token=self.token)[1]
        bad = (None, [], [{"reference": a["reference"]}] * 2, ["bad"], [{}], [{"reference": 4}], [{"reference": a["reference"]}] * 9)
        for moves in bad:
            self.error(request("POST", "/reservation-moves", {"moves": moves}, self.token, "failed"), 422, "validation_failed")
        self.error(request("POST", "/reservation-moves", {"moves": [{"reference": a["reference"]}]}, self.bob, "failed"), 404, "not_found")
        body = {"moves": [{"reference": a["reference"], "starts_at_local": "2036-09-25T19:00"},
                          {"reference": b["reference"], "party_size": 3}]}
        self.error(request("POST", "/reservation-moves", body, self.token, "failed"), 422, "party_exceeds_capacity")
        self.assertEqual(request("GET", "/reservations", token=self.token)[1], before)
        self.assertEqual(request("POST", "/reservation-moves", {"moves": [{"reference": a["reference"]}]}, self.token, "failed")[0], 201)

    def test_batch_error_precedence_cutoff_and_overlap(self):
        old = self.create(self.booking(start="2001-01-01T19:00", size=2), "old")[1]
        future = self.create(self.booking(size=2), "future")[1]
        body = {"moves": [{"reference": old["reference"], "party_size": "bad"}, {"reference": future["reference"], "table_id": "missing"}]}
        self.error(request("POST", "/reservation-moves", body, self.token, "cutoff"), 409, "cutoff_passed")
        body["moves"].reverse()
        self.error(request("POST", "/reservation-moves", body, self.token, "order"), 404, "not_found")
        other = self.create(self.booking(table="t1", size=2, start="2036-09-24T20:30"), "other")[1]
        body = {"moves": [{"reference": future["reference"], "starts_at_local": "2036-09-24T20:30", "table_id": "t1"},
                          {"reference": other["reference"]}]}
        self.error(request("POST", "/reservation-moves", body, self.token, "result-overlap"), 409, "table_unavailable")
        self.assertEqual(request("GET", "/reservations/" + future["reference"], token=self.token)[1], future)

    def test_batch_different_restaurants_and_foreign_tables(self):
        data = fixture()
        r2 = copy.deepcopy(data["restaurants"][0])
        r2["id"] = "r2"
        r2["tables"] = [{"id": "foreign", "label": "Foreign", "capacity": 4}]
        data["restaurants"].append(r2)
        request("POST", "/_test/reset", data)
        self.token = self.login("ada")
        a = self.create()[1]
        b = self.create({**self.booking(), "restaurant_id": "r2", "table_id": "foreign"}, "r2")[1]
        self.error(self.create(self.booking(table="foreign"), "foreign"), 404, "not_found")
        self.error(request("POST", "/reservation-moves", {"moves": [{"reference": a["reference"]}, {"reference": b["reference"]}]}, self.token, "multi"), 422, "validation_failed")

    def test_concurrent_batch_receipt_and_patch_contention(self):
        a = self.create(self.booking(size=2))[1]
        b = self.create(self.booking(table="t1", size=2), "b")[1]
        body = {"moves": [{"reference": a["reference"], "table_id": "t1"}, {"reference": b["reference"], "table_id": "t2"}]}
        barrier = threading.Barrier(50)
        def worker(index):
            barrier.wait()
            return request("POST", "/reservation-moves", body, self.token, "parallel-moves")
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(worker, range(50)))
        self.assertEqual(sum(status == 201 for status, _ in results), 1)
        self.assertEqual(sum(status == 200 for status, _ in results), 49)
        self.assertTrue(all(value == results[0][1] for _, value in results))

    def test_concurrent_patches_only_one_takes_free_target(self):
        a = self.create(self.booking(size=2))[1]
        b = self.create(self.booking(table="t1", size=2), "b")[1]
        barrier = threading.Barrier(2)
        def worker(booking):
            barrier.wait()
            return request("PATCH", "/reservations/" + booking["reference"],
                           {"table_id": "t2", "starts_at_local": "2036-09-25T19:00"}, self.token)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, (a, b)))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        for booking, (status, result) in zip((a, b), results):
            expected = result if status == 200 else booking
            self.assertEqual(request("GET", "/reservations/" + booking["reference"], token=self.token)[1], expected)

    def test_same_body_same_key_on_distinct_paths(self):
        a = self.create()[1]
        body = self.booking(start="2036-09-25T19:00", moves=[{"reference": a["reference"]}])
        created = self.create(body, "shared-path-key")
        moved = request("POST", "/reservation-moves", body, self.token, "shared-path-key")
        self.assertEqual(created[0], 201)
        self.assertEqual(moved, (201, {"reservations": [a]}))
        self.assertEqual(self.create(body, "shared-path-key"), (200, created[1]))
        self.assertEqual(request("POST", "/reservation-moves", body, self.token, "shared-path-key"), (200, moved[1]))

    def test_upcoming_cutoff_and_invalid_reset_are_atomic(self):
        data = fixture("UTC")
        data["restaurants"][0]["cancellation_cutoff_minutes"] = 2880
        request("POST", "/_test/reset", data)
        self.token = self.login("ada")
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        a = self.create(self.booking(start=tomorrow + "T19:00"))[1]
        path = "/reservations/" + a["reference"]
        self.error(request("POST", path + "/cancel", {}, self.token), 409, "cutoff_passed")
        self.error(request("PATCH", path, {"table_id": []}, self.token), 409, "cutoff_passed")
        malformed = fixture()
        malformed["users"][0]["id"] = "x" * 65
        self.error(request("POST", "/_test/reset", malformed), 422, "validation_failed")
        self.assertEqual(request("GET", path, token=self.token)[1], a)

    def test_atomic_export_during_swaps(self):
        a = self.create(self.booking(size=2))[1]
        b = self.create(self.booking(table="t1", size=2), "b")[1]
        forward = {"moves": [{"reference": a["reference"], "table_id": "t1"}, {"reference": b["reference"], "table_id": "t2"}]}
        backward = {"moves": [{"reference": a["reference"], "table_id": "t2"}, {"reference": b["reference"], "table_id": "t1"}]}
        def swapper():
            for i in range(20):
                self.assertEqual(request("POST", "/reservation-moves", forward if i % 2 == 0 else backward,
                                         self.token, "export-swap-" + str(i))[0], 201)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(swapper)
            for i in range(20):
                snapshot = request("GET", "/_test/export")[1]
                bookings = snapshot["state"]["reservations"]
                self.assertIn((bookings[a["reference"]]["table_id"], bookings[b["reference"]]["table_id"]),
                              (("t1", "t2"), ("t2", "t1")))
                self.assertEqual(request("POST", "/_test/import", snapshot, base=DESTINATION)[0], 204)
            running.result()

    def test_invalid_import_state_and_json_leave_destination_unchanged(self):
        a = self.create()[1]
        snapshot = request("GET", "/_test/export")[1]
        self.assertEqual(request("POST", "/_test/import", snapshot, base=DESTINATION)[0], 204)
        invalids = []
        for key, value in (("tokens", {"token": "missing"}), ("receipts", {"bad": {}}),
                           ("reservations", {a["reference"]: {"status": "confirmed"}})):
            altered = copy.deepcopy(snapshot)
            altered["state"][key] = value
            invalids.append(altered)
        for value in invalids:
            self.error(request("POST", "/_test/import", value, base=DESTINATION), 422, "validation_failed")
            self.assertEqual(request("GET", "/reservations/" + a["reference"], token=self.token, base=DESTINATION)[1], a)
        self.error(request("POST", "/_test/import", raw=b"{", base=DESTINATION), 400, "malformed_request")
        self.assertEqual(request("GET", "/reservations/" + a["reference"], token=self.token, base=DESTINATION)[1], a)

    def test_portable_import_replaces_and_preserves_original_receipts(self):
        first_body = self.booking(size=2)
        a = self.create(first_body)[1]
        b = self.create(self.booking(table="t1", size=2), "b")[1]
        moves = {"moves": [{"reference": a["reference"], "table_id": "t1"}, {"reference": b["reference"], "table_id": "t2"}]}
        receipt = request("POST", "/reservation-moves", moves, self.token, "move")[1]
        request("POST", "/reservations/" + a["reference"] + "/cancel", {}, self.token)
        self.error(self.create(self.booking(start="bad"), "failed"), 422, "validation_failed")
        token2 = self.login("ada")
        snapshot = request("GET", "/_test/export")[1]
        original_list = request("GET", "/reservations", token=self.token)[1]
        request("POST", "/_test/reset", fixture(), base=DESTINATION)
        destination_token = self.login("bob", base=DESTINATION)
        for repeat in range(2):
            self.assertEqual(request("POST", "/_test/import", snapshot, base=DESTINATION)[0], 204)
            self.error(request("GET", "/reservations", token=destination_token, base=DESTINATION), 401, "unauthenticated")
            for token in (self.token, token2):
                self.assertEqual(request("GET", "/reservations", token=token, base=DESTINATION)[1], original_list)
            self.login("ada", base=DESTINATION)
            self.assertEqual(self.create(first_body, base=DESTINATION), (200, a))
            self.assertEqual(request("POST", "/reservation-moves", moves, self.token, "move", DESTINATION), (200, receipt))
            self.assertEqual(self.create(self.booking(start="2036-09-26T19:00"), "failed", base=DESTINATION)[0], 201)
        # Later source writes do not mutate the already-exported JSON.
        request("POST", "/reservations/" + b["reference"] + "/cancel", {}, self.token)
        request("POST", "/_test/import", snapshot, base=DESTINATION)
        self.assertEqual(request("GET", "/reservations", token=self.token, base=DESTINATION)[1], original_list)
        for invalid in ({}, {**snapshot, "track": "other"}, {**snapshot, "format_version": True},
                        {**snapshot, "state": {}}, {**snapshot, "state": {**snapshot["state"], "users": []}}):
            self.error(request("POST", "/_test/import", invalid, base=DESTINATION), 422, "validation_failed")
            self.assertEqual(request("GET", "/reservations", token=self.token, base=DESTINATION)[1], original_list)
        request("POST", "/_test/reset", fixture(), base=DESTINATION)
        self.error(request("GET", "/reservations", token=self.token, base=DESTINATION), 401, "unauthenticated")
        self.assertEqual(request("GET", "/_test/export", base=DESTINATION)[1]["state"]["receipts"], {})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=SOURCE)
    parser.add_argument("--destination", default=DESTINATION)
    args = parser.parse_args()
    SOURCE, DESTINATION = args.source, args.destination
    for base in (SOURCE, DESTINATION):
        start = time.monotonic()
        while True:
            try:
                if request("GET", "/health", base=base)[0] == 200:
                    break
            except OSError:
                if time.monotonic() - start > 60:
                    raise
                time.sleep(0.1)
        print(f"Healthy {base}: {time.monotonic() - start:.3f}s", flush=True)
    unittest.main(argv=["test_http.py"], verbosity=2)
