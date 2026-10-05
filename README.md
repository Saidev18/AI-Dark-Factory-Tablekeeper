# AI Dark Factory — Tablekeeper

Submission for the WeAreDevelopers AI Dark Factory challenge using the Tablekeeper track.

## Project

Tablekeeper is a restaurant reservation system developed progressively across four stages.

The system supports restaurant discovery and availability, authenticated reservations, individual and combined-table seating, cancellation and amendments, recurring reservations, policy and revision handling, reservation history, and Stage 4 seating replanning.

The customer-facing interface uses a responsive dark luxury dining design.

## Repository Structure

```text
.
├── README.md
├── FACTORY.md
├── room.json
├── mandates/
│   ├── ai-dark-factory-lead.md
│   ├── ai-dark-factory-developer.md
│   └── ai-dark-factory-verifier.md
├── stage-1/
├── stage-2/
├── stage-3/
└── stage-4/
```

Each stage-N directory is a standalone implementation of that stage and contains its own Dockerfile and RUN.md.

## Stage 1

Stage 1 implements the core Tablekeeper HTTP API.

Key capabilities include:

- Authentication and multiple sessions
- Restaurant availability
- Reservation creation, lookup, amendment and cancellation
- Idempotent reservation writes
- Concurrency-safe table occupancy
- Atomic reservation moves
- Timezone and DST handling
- Export and import
- Dockerized standalone service

## Stage 2

Stage 2 extends Stage 1 with the customer-facing web application and combined-table seating.

Key additions include:

- Responsive reservation UI
- Sign up and sign in
- Restaurant/date/party search
- Individual and combined-table availability
- Reservation creation and confirmation
- Reservation lookup and cancellation
- Recovery from stale searches and interrupted booking responses
- Stage 1 compatibility

## Stage 3

Stage 3 extends the previous stages with richer reservation lifecycle functionality.

Key additions include:

- Availability explanations
- Versioned restaurant policies
- Accepted terms
- Reservation revisions and history
- Recurring reservation series
- Series exceptions
- Policy-aware atomic reservation moves
- Backward-compatible state upgrades

Stage 3 also introduced a new dark luxury visual identity for the customer-facing application.

## Stage 4

Stage 4 is the final implementation.

Key additions include:

- Table closure handling
- Deterministic seating replanning
- Read-only replan previews
- Atomic application of replans
- Closure-aware availability
- Recurring-series amendments
- Revision and history preservation
- Import validation and replanning integrity checks

The Stage 3 dark luxury customer experience is preserved in Stage 4.

## Running Stage 4

From the stage-4 directory:
```text
docker build -t tablekeeper-stage-4 .
docker run --rm -e PORT=8080 -p 8080:8080 tablekeeper-stage-4
```
Then open:

http://127.0.0.1:8080

Each stage also contains its own RUN.md with stage-specific instructions.

## Validation

The implementation was validated with the official Tablekeeper harness in isolated mode.

### Final verification results

```text
Stage 1: 120/120
Stage 2: 25/25
Stage 3: 7/7
Stage 4: 6/6
```

The final Stage 4 verification also included independent specification-derived probes, replanning tests, browser testing, offline-runtime checks and regression checks across all previous stages.

Before submission, the repository is validated using:
```text
python -m harness check <repository> --track tablekeeper
```
```text
python -m harness run \
  --track tablekeeper \
  --repo <repository> \
  --all \
  --mode isolated
```
## AI Factory

The project was built using three collaborating AI agents:

- AI Dark Factory Lead
- AI Dark Factory Developer
- AI Dark Factory Verifier

The Lead coordinates delivery, the Developer implements and tests each stage, and the Verifier independently validates completed work before acceptance.

See FACTORY.md for the complete factory architecture and workflow.

The full BAND room history is provided unchanged in room.json.

Agent harness/model declarations are provided under mandates/.
