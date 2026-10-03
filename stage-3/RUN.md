# Tablekeeper Stage 3

Build and run the complete HTTP and browser service from this directory:

```sh
docker build -t tablekeeper-stage3 . && docker run --rm --name tablekeeper-stage3 -e PORT=8080 -p 8080:8080 --cpus=2 --memory=2g tablekeeper-stage3
```

Open http://localhost:8080/. Routes `/`, `/signup`, `/login` and `/lookup` return
HTML. `GET /health` returns the service status. The image listens on `0.0.0.0`
with `PORT` (default 8080), runs as an unprivileged user, and needs no volumes,
compose, external services or outbound runtime access. CSS, JavaScript, system
fonts, illustrations and IANA timezone data are included locally. State is in
memory and need not survive restart.

The initial service is empty. `POST /_test/reset` accepts the fixture and clears
all accounts, tokens, reservations, policies, histories, series and receipts.
Restaurants may declare pairs and `manager_user_ids`; both default to `[]`.
Seeded reservations start at revision 1 under the original policy (version 0).

The inherited booking API remains available. `GET /availability` uses the policy
for the searched local date; `explain=true` adds both independent rules for every
table. Public `GET /restaurants/{id}/policies` lists immutable publications.
Managers publish a complete dated policy through authenticated, idempotent
`POST /restaurants/{id}/policies`. The ordinary detail retains its original
fixture rules. Browser capacity claims use searched options and confirmation
duration uses accepted terms.

Reservations include `revision` and a complete `accepted_terms` snapshot.
Owner-private history and decision reads return 404 for everyone else,
including tokenless callers. Amendments check optional `expected_revision`
before the old accepted cutoff. Real changes adopt the resulting date's policy;
no-ops and reversed pairs preserve terms, ends, revision and history. Cancellation
checks the accepted cutoff and records one terminal event.

Authenticated, idempotent `POST /series` adopts a confirmed editable anchor.
Weekly occurrences follow the local calendar and each date's policy and DST.
The anchor's identity, terms and history stay intact. Creation, amendments,
collective moves, occupancy, histories, revisions and receipts commit under a
single lock. A failed operation leaves every part of state unchanged. Individual
real changes permanently mark series exceptions; batches increment each affected
series and the restaurant once. No additional policy or series screens are needed.

`GET /_test/export` and `POST /_test/import` are unauthenticated test controls.
Import accepts this team's unchanged Stage 1, 2 and 3 exports. It atomically
replaces state while retaining hashed-password login, tokens, identities,
references, timestamps and exact original create/batch/policy/series receipts.
Legacy bookings gain version-0 terms, revision 1 and an initial history.
Pending browser retries keep the original body/key between requests across an
import. Exports contain private credentials/tokens: keep them private.

The HTTP clients use an internal network with two Stage 3 containers and both
earlier-stage services. Run from this directory:

```sh
docker build -t tablekeeper-stage3 .
docker build -t tablekeeper-stage1 ../stage-1
docker build -t tablekeeper-stage2 ../stage-2
docker build -f tests/Dockerfile -t tablekeeper-stage3-tests .
docker network create --internal tablekeeper-stage3-test
docker run -d --name tk-s3-source --network tablekeeper-stage3-test --cpus=2 --memory=2g -e PORT=9093 tablekeeper-stage3
docker run -d --name tk-s3-destination --network tablekeeper-stage3-test --cpus=2 --memory=2g -e PORT=9094 tablekeeper-stage3
docker run -d --name tk-s1-legacy --network tablekeeper-stage3-test -e PORT=9095 tablekeeper-stage1
docker run -d --name tk-s2-legacy --network tablekeeper-stage3-test -e PORT=9096 tablekeeper-stage2
docker run --rm --network tablekeeper-stage3-test tablekeeper-stage3-tests --source http://tk-s3-source:9093 --destination http://tk-s3-destination:9094
docker run --rm --network tablekeeper-stage3-test --entrypoint python tablekeeper-stage3-tests /test_pairs.py --source http://tk-s3-source:9093 --destination http://tk-s3-destination:9094 --legacy http://tk-s1-legacy:9095
docker run --rm --network tablekeeper-stage3-test --entrypoint python tablekeeper-stage3-tests /test_policies.py --source http://tk-s3-source:9093 --destination http://tk-s3-destination:9094 --legacy1 http://tk-s1-legacy:9095 --legacy2 http://tk-s2-legacy:9096
```

The tests use HTTP only, including 50-request concurrency, DST, occupancy,
immutable receipts, atomic rollback, permissions, policy/revision boundaries,
history, recurring agreements and portable upgrades. Browser checks use real
Chromium, with desktop and 375px screenshots kept outside the repository:

```sh
docker build -f tests/Dockerfile.browser -t tablekeeper-stage3-browser-tests .
docker run --rm --network tablekeeper-stage3-test -v /tmp/tablekeeper-stage3-browser:/out tablekeeper-stage3-browser-tests --source http://tk-s3-source:9093 --destination http://tk-s3-destination:9094 --legacy http://tk-s1-legacy:9095 --legacy2 http://tk-s2-legacy:9096 --out /out
docker rm -f tk-s3-source tk-s3-destination tk-s1-legacy tk-s2-legacy
docker network rm tablekeeper-stage3-test
```

Browser dependencies install during the separate test-client build; the service
itself uses the Python standard library. Clients reset their supplied services
and never write private exported state to disk.
