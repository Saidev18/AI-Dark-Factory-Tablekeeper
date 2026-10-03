# Stage 1 Verifier evidence — revision c73dbf65441523e74d12e439ab5da78e5f4c1f7c

Date: 2026-10-03. Verifier: AI Dark Factory Verifier (independent).
Spec: /home/saidev/ai-dark-factory-tablekeeper/challenge/tablekeeper/spec/stage-1.md (472 lines, read in full).
Clean checkout: /tmp/opencode/tk-verify-c73dbf6 at the pinned revision (worktree clean).

## 1. Harness, isolated mode (authoritative grader)

Command: from /home/saidev/ai-dark-factory-tablekeeper/challenge:
`​.venv/bin/python -m harness run --track tablekeeper --repo /tmp/opencode/tk-verify-c73dbf6 --stage 1 --mode isolated --out /home/saidev/ai-dark-factory-tablekeeper/verification/stage-1-c73dbf6-harness`

Result: **stage 1: pass — 120/120** in 14.67s (test_health_reset_auth 12, test_reservations 37,
test_restaurants_availability 18, test_retries_time_input 20, test_sample 20, test_seeded_state 13).
Stage 2 overshoot probe failed as expected (Playwright UI locator timeout) — not a Stage 1 defect,
does not authorize Stage 2 work. Evidence: report.json, stage-1.log, stage-2.log, stage-1.counts.json.

## 2. Independent spec-derived probes (beyond shipped checks): 148/148 PASS

Script: /tmp/opencode/verifier_probes.py (stdlib-only, black-box) against the clean-checkout image
built as tk-verify-s1; source container PORT=18191, destination container PORT=18192 (2 CPU/2 GiB each).

- Runtime: /health shape + exact content type; default PORT 8080 and custom PORT; unknown routes 4xx JSON;
  repeated resets; reset-to-empty clears state; unknown query params and body fields ignored.
- Auth: signup/login shapes; multiple concurrent tokens; email_taken/short-password/bad-email/wrong-password codes;
  tokenless access blocked; public endpoints open; export shows hashed passwords only (scrypt).
- Availability: exact slot grid 18:00..21:30 (90 min, closes 23:00), fri 22:00 last (closes 23:30);
  IANA offsets; fixture-order table ids; capacity filter; closed day -> []; all §5 query-param
  validations incl. 1e9 / 4.0 / +4 / 0 / -3 / 2026-02-30; unknown restaurant 404; past-date booking allowed.
- Reservations + idempotency: full 201 shape (RFC 3339 offsets, reference A-Z0-9 6..12); replay 200 identical
  JSON value regardless of key order/whitespace; replay makes no state changes; key+body reuse 409 even when the
  new body is invalid (idempotency before field validation); key reusable after 4xx; user-scoped keys;
  same key+body on different path is not a replay; missing/empty key 400; 256-char key 422; 255-char ok;
  half-open adjacency (19:00 + 20:30 ok; 20:00 overlap 409); all endpoint-specific 4xx codes.
- Concurrency: 40 concurrent same-key creates -> exactly 1x201 + 39x200 identical, one booking;
  40 concurrent distinct keys, one table -> 1x201 + 39x409 table_unavailable, exactly one booking.
- Listing/visibility/cancel/patch: starts_at desc incl. cancelled; foreign reservation 404 (no leak);
  cancel frees slot immediately; double cancel 200; patch cancelled -> reservation_cancelled;
  failed patch rollback (occupancy unchanged); identity/reference/created_at survive; old slot released;
  cutoff measured against current start (incl. after moving a booking into the past).
- DST (both zones, spring + fall): skipped 02:xx slots never appear and booking them is 422 invalid_local_time;
  crossing bookings end 04:00 local with UTC occupancy overlap across the gap; fall-back repeated slot resolves to
  first occurrence (Berlin +02:00 -> ends 02:00+01:00; New York -04:00 -> ends 02:00-05:00); exactly 5400 real
  seconds; second occurrence not bookable (same instant -> 409); repeated slot appears exactly once.
- Batch moves: swap 201 in input order; no-op retains all values; replay 200 original after later cancel;
  9 shape-invalid variants -> 422; foreign/unknown ref 404; overlap with unlisted booking 409 + full rollback;
  cancelled item -> reservation_cancelled; cutoff precedes other changes and input-order precedence;
  failed-batch key reusable; 8-item batch ok; 9 items 422; 40 concurrent same-key batches -> 1x201 + 39x200.
- Export/import (second container): export shape; import 204; destination credentials removed; source tokens
  valid; hashed login works; identities/timestamps/status preserved; create and batch receipts replay 200
  original; failed key reusable after import; repeat import no duplication; 6 invalid imports -> 422 with
  destination unchanged; malformed JSON -> 400; snapshot immutable (later source writes do not mutate it) and
  re-import restores exported state; reset clears imported state.
- Robustness: 21 fuzz probes (malformed JSON, wrong types, huge ints, extreme dates, encoded paths, empty
  bodies) -> zero 5xx, every 4xx carries the §5 JSON error shape with exact content type.

## 3. Reproduction of Developer evidence

- Developer's own suite (tests/Dockerfile client on --internal network, two fresh service containers):
  **21/21 OK in 4.063s** — matches the claimed 21 tests / 4.184s.
- Container integrity: sha256(/app/server.py in container) = e9cdf7022079655d3c0ee60c142a0109b541f235bfee5154a9534fd93ea29710
  = sha256(clean-checkout stage-1/server.py) = hash claimed in evidence/DEVELOPER.md.
- Runtime isolation: `--network none` container: health 200 on PORT=19090, no default route, outbound
  sockets to 1.1.1.1/8.8.8.8 fail, both IANA zones load locally (tests/check_runtime.py).

## 4. Static review notes (no violations found)

- Single RLock serializes occupancy checks, receipt writes, reset, import and snapshot — consistent with
  the concurrency requirements; ThreadingHTTPServer with daemon threads.
- Idempotency receipts stored only on success (4xx keys stay reusable); receipts keyed (user, method, path, key).
- fold=0 first-occurrence DST resolution; occupancy/ends/cutoffs compared in UTC instants; half-open intervals.
- Import fully validates a candidate (including receipt-response consistency) before the single STATE swap.
- Observation (ambiguous spec edge, accepted by harness): an unknown path without a token returns
  401 unauthenticated rather than 404; with a token it returns 404 not_found. Defensible under §6.

## 5. Verdict

**Stage 1 verification PASSED** at revision c73dbf65441523e74d12e439ab5da78e5f4c1f7c.
No spec violations found; no fixes required.