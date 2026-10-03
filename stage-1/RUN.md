# Tablekeeper Stage 1

From this directory, build and start the standalone HTTP service:

```sh
docker build -t tablekeeper-stage1 . && docker run --rm --name tablekeeper-stage1 -e PORT=8080 -p 8080:8080 --cpus=2 --memory=2g tablekeeper-stage1
```

The service listens on `0.0.0.0`, honors `PORT` (default `8080`), and is ready when
`GET /health` returns `{"status":"ok"}`. No setup, volume, database, compose file,
or outbound networking is required at runtime. It starts with empty state.
Populate it with `POST /_test/reset` and the Stage 1 fixture. These test controls
are intentionally enabled without authentication.

The implementation uses Python's standard library and image-local IANA timezone
data. Passwords use salted scrypt (`N=16384`, `r=8`, `p=1`). Accounts may have
multiple non-expiring bearer tokens. All service state is in memory and is
discarded on restart. A single lock serializes state operations, including
occupancy checks and writes, snapshots, reset/import and idempotency receipts.
Half-open occupancy and reservation ends are compared in UTC. Local ambiguous
times resolve to the first occurrence; nonexistent local times are rejected.
The slot grid advances in local wall time, and the absolute duration must fit
before the restaurant's closing instant.

Public endpoints: `GET /restaurants`, `GET /restaurants/{id}`, `GET /availability`.
Authentication: `POST /auth/signup`, `POST /auth/login`. Authenticated reservation
endpoints: `POST /reservations`, `GET /reservations`, `GET /reservations/{reference}`,
`PATCH /reservations/{reference}`, `POST /reservations/{reference}/cancel`, and
`POST /reservation-moves`. The two create paths require `Idempotency-Key`; keys
are scoped to user, method and path. Failed requests save no receipt. Replays
return the immutable original response even after subsequent changes.

`GET /_test/export` returns a portable JSON snapshot, accepted unchanged by
`POST /_test/import`. Import validates a candidate before replacing all state.
It preserves hashed credentials, tokens, configuration, reservation identities
and timestamps, and both create and batch receipts. Snapshots contain private
test credentials and bearer tokens: keep them private. The test suite transfers
them only in memory and does not write snapshots into evidence files.

Run the specification-derived HTTP suite against two fresh containers on a
Docker network without outbound access:

```sh
docker build -t tablekeeper-stage1 .
docker build -f tests/Dockerfile -t tablekeeper-stage1-tests .
docker network create --internal tablekeeper-stage1-test
docker run -d --name tablekeeper-stage1-source --network tablekeeper-stage1-test --cpus=2 --memory=2g -e PORT=9091 tablekeeper-stage1
docker run -d --name tablekeeper-stage1-destination --network tablekeeper-stage1-test --cpus=2 --memory=2g -e PORT=9092 tablekeeper-stage1
docker run --rm --network tablekeeper-stage1-test tablekeeper-stage1-tests --source http://tablekeeper-stage1-source:9091 --destination http://tablekeeper-stage1-destination:9092
docker rm -f tablekeeper-stage1-source tablekeeper-stage1-destination
docker network rm tablekeeper-stage1-test
```

The suite resets the supplied services. It uses no third-party packages and
runs in a separate client image on the isolated network. The delivered image contains only the
service and its runtime; tests run externally through HTTP. Tests cover 50-way
contention/replays, isolated users, validation ordering, cutoff, atomic swaps
and rollback, both specified DST zones, and cross-container replacement/import.
