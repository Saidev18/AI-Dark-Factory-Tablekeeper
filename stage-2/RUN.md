# Tablekeeper Stage 2

Build and start the complete browser and HTTP service from this directory:

```sh
docker build -t tablekeeper-stage2 . && docker run --rm --name tablekeeper-stage2 -e PORT=8080 -p 8080:8080 --cpus=2 --memory=2g tablekeeper-stage2
```

Open http://localhost:8080/. Browser routes: /, /signup, /login and /lookup.
Health is GET /health. The image listens on 0.0.0.0 using PORT (default 8080).
It needs no volumes, compose, manual setup, external services or outbound runtime
access. System fonts, CSS, JavaScript and the seating illustration are local.
IANA timezone data is bundled. State is in memory and discarded on restart.

The service starts empty. Seed it through unauthenticated POST /_test/reset
using the Stage 2 fixture. Restaurants may declare combinable pairs; omitting
this field means no combinations. Reset clears accounts, tokens and receipts.

The inherited API remains available. Responses add table_ids; singles also
include table_id. Pairs occupy both members. A lock serializes occupancy checks,
mutations, receipts and state controls. Amendments and batch moves validate
before committing. Passwords use salted scrypt; tokens do not expire.

POST /_test/import accepts this team's unchanged Stage 1 and Stage 2 exports.
Accounts, tokens, references, timestamps and original create/batch receipts
survive. Legacy bookings gain table_ids while legacy retries return the exact
original response. Browser pending body/key survive imports between requests.
Lost responses produce uncertainty; unchanged retries check the same request.
Field changes start new requests. Reload recovery is not required.
Snapshots contain private credentials and tokens: keep them private.

HTTP checks use two Stage 2 services plus Stage 1 on an internal network:

```sh
docker build -t tablekeeper-stage2 .
docker build -t tablekeeper-stage1 ../stage-1
docker build -f tests/Dockerfile -t tablekeeper-stage2-tests .
docker network create --internal tablekeeper-stage2-test
docker run -d --name tk-s2-source --network tablekeeper-stage2-test --cpus=2 --memory=2g -e PORT=9093 tablekeeper-stage2
docker run -d --name tk-s2-destination --network tablekeeper-stage2-test --cpus=2 --memory=2g -e PORT=9094 tablekeeper-stage2
docker run -d --name tk-s1-legacy --network tablekeeper-stage2-test --cpus=2 --memory=2g -e PORT=9095 tablekeeper-stage1
docker run --rm --network tablekeeper-stage2-test tablekeeper-stage2-tests --source http://tk-s2-source:9093 --destination http://tk-s2-destination:9094
docker run --rm --network tablekeeper-stage2-test --entrypoint python tablekeeper-stage2-tests /test_pairs.py --source http://tk-s2-source:9093 --destination http://tk-s2-destination:9094 --legacy http://tk-s1-legacy:9095
```

The inherited suite checks HTTP, DST, concurrency, rollback and receipts.
The pair suite checks option ordering, capacity, non-transitivity, shared
occupancy, cancelled seeds, atomic moves and Stage 1 migration.

Build the separate real-browser test client for desktop/375px checks, stale
searches, stolen seating, lost response retries and browser upgrade recovery.
Keep transient screenshots outside the submission repository:

```sh
docker build -f tests/Dockerfile.browser -t tablekeeper-stage2-browser-tests .
docker run --rm --network tablekeeper-stage2-test -v /tmp/tablekeeper-stage2-browser:/out tablekeeper-stage2-browser-tests --source http://tk-s2-source:9093 --destination http://tk-s2-destination:9094 --legacy http://tk-s1-legacy:9095 --out /out
docker rm -f tk-s2-source tk-s2-destination tk-s1-legacy
docker network rm tablekeeper-stage2-test
```

Client dependencies install during their build, separate from the service.
Tests reset supplied instances and never write exported state to disk.
