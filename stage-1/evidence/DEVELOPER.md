# Stage 1 implementation evidence

Date: 2026-10-03. Requirements source: the complete 472-line Stage 1 specification
at `/home/saidev/ai-dark-factory-tablekeeper/challenge/tablekeeper/spec/stage-1.md`.
Implementation and tests were derived from this specification; no shipped test
source or existing reservation product source, schemas or documentation was used.

## Delivered behavior

All Stage 1 endpoints are implemented. A single state lock serializes reads and
writes, occupancy validation, completed retry receipts, reset and import. No-op
moves preserve stored values. Batch validation precedes any mutation and checks
non-occupancy failures in input order before aggregate occupancy. Credentials
use salted scrypt. Snapshots transfer hashed accounts, all issued tokens, full
configuration, reservation records and immutable create/batch responses.

Local grid generation uses IANA zones; gaps are omitted and rejected. Repeated
times use the first occurrence. Occupancy, end times and cutoff comparisons use
UTC instants; half-open boundaries allow adjacent bookings. Past dates are
accepted for booking and still subject to cancellation/amendment cutoff.

## Commands and results

Run from `/home/saidev/ai-dark-factory-tablekeeper/submission/stage-1`:

```sh
python3 -m py_compile server.py tests/test_http.py tests/check_runtime.py
git diff --check
docker build -t tablekeeper-developer-s1:review .
docker build -f tests/Dockerfile -t tablekeeper-developer-s1-tests .
docker network create --internal tablekeeper-developer-s1
docker run -d --name tablekeeper-developer-source --network tablekeeper-developer-s1 --cpus=2 --memory=2g -e PORT=9091 tablekeeper-developer-s1:review
docker run -d --name tablekeeper-developer-destination --network tablekeeper-developer-s1 --cpus=2 --memory=2g -e PORT=9092 tablekeeper-developer-s1:review
docker run --rm --network tablekeeper-developer-s1 tablekeeper-developer-s1-tests --source http://tablekeeper-developer-source:9091 --destination http://tablekeeper-developer-destination:9092
```

Compilation and whitespace checks passed. Both Docker builds passed. Both service
containers are fresh standalone instances with no mounts and no external service
dependencies. CPU is limited to 2 and memory to 2 GiB for each service.

The final HTTP suite ran **21 tests in 4.184 seconds, all passed**. First health
probes returned in 0.026 seconds (source) and 0.002 seconds (destination). These
probe timings are request timings after launch, rather than full boot timings.
50 simultaneous identical creates completed in 0.057 seconds: exactly one 201,
49 identical 200 responses, and one stored reservation. 50 distinct competing
keys produced one successful occupant and 49 `table_unavailable` responses.
50 identical concurrent batches also produced one 201 and 49 identical 200s.
Ordinary HTTP calls have a five-second client timeout; controls use ten seconds.

Coverage includes public browsing, fixture order, closed days, insufficient
capacity, signup/login and multiple tokens, wrong/missing fields, malformed JSON,
query integer syntax, ID/key bounds, slot grid, opening/end bounds, overlapping
and adjacent occupancy, owner visibility, descending listing, cancellation,
amendment rollback, past seeds, current/upcoming cutoff, ignored fields,
idempotency body equivalence and ordering, user and path scopes, retryable failed
keys, immutable replay after cancellation, atomic swaps and no-ops, batch shape,
ownership, cross-restaurant restrictions, per-item failure precedence, resulting
overlap, concurrent amendments, and concurrent export during repeated swaps.
Both Berlin and New York spring and fall transitions are checked, including
absolute elapsed duration across transitions. Separate-container import checks
replacement, repeatability, hashed login, multiple existing tokens, statuses,
unchanged identities/timestamps, original create and batch receipts, failed-key
reuse, frozen snapshots, invalid-import rollback, and clearing imported state.

Additional offline and mapping checks:

```sh
docker run -d --name tablekeeper-developer-offline --network none --cpus=2 --memory=2g -e PORT=19090 tablekeeper-developer-s1:review
docker cp tests/check_runtime.py tablekeeper-developer-offline:/tmp/check_runtime.py
docker exec tablekeeper-developer-offline python /tmp/check_runtime.py 19090
docker cp tests/check_runtime.py tablekeeper-developer-source:/tmp/check_runtime.py
docker exec tablekeeper-developer-source python /tmp/check_runtime.py 9091
docker run -d --name tablekeeper-developer-mapped --cpus=2 --memory=2g -e PORT=19091 -p 127.0.0.1:38231:19091 tablekeeper-developer-s1:review
```

The completely disconnected container returned health on port 19090 in 0.030
seconds. The internal-network source returned health on 9091 in 0.024 seconds.
Both had no default route; outbound sockets to 1.1.1.1 and 8.8.8.8 failed, and
both IANA zones loaded locally. Tests copied into `/tmp` are probes only; service
startup does not depend on them. The container server SHA-256 matches the source:
`e9cdf7022079655d3c0ee60c142a0109b541f235bfee5154a9534fd93ea29710`.
The mapped-port instance returned 200 `{"status":"ok"}` via Windows
`Invoke-WebRequest -UseBasicParsing -Uri http://127.0.0.1:38231/health`.

An initial host-port test attempt hit unrelated older containers already bound
to 18081/18082. It is excluded from passing evidence. The final full suite used
unique service names and container DNS on the internal network, avoiding those
host bindings. Unrelated containers were not removed. The mistaken host-port
suite reset their in-memory test data before the routing error was identified.

No credential/session snapshots are saved in this evidence. Independent
verification is required before the stage is reported complete.
