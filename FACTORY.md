# AI Dark Factory

## Overview

This Tablekeeper submission was produced using a three-agent software factory in BAND.

The factory separates coordination, implementation and independent verification so that the same agent that writes the application is not responsible for accepting its own work.

The factory consists of:

Human
  ↓
AI Dark Factory Lead
  ↓
AI Dark Factory Developer
  ↓
AI Dark Factory Verifier
  ↓
AI Dark Factory Lead
  ↓
Human

When verification finds a defect:

Verifier
  ↓
Developer
  ↓
fix + retest + new commit
  ↓
Verifier

Only independently verified work is accepted as stage-complete.

## Agent 1 — AI Dark Factory Lead

Harness: Codex
Model: gpt-6.1-sol

The Lead owns delivery of the current stage.

Responsibilities include:

- Reading the complete stage specification
- Inspecting the current repository state
- Planning the stage
- Delegating implementation to the Developer
- Tracking implementation and verification
- Returning concrete verification failures to the Developer when necessary
- Accepting the stage only after independent verification passes
- Reporting the final verified revision and evidence to the human

The Lead does not implement the application itself.

## Agent 2 — AI Dark Factory Developer

Harness: Codex
Model: gpt-6.1-sol

The Developer owns implementation.

Responsibilities include:

- Reading the assigned specification
- Implementing requirements in the current stage directory
- Preserving all required behavior from previous stages
- Writing and running tests
- Running the official harness during development
- Performing browser checks where required
- Committing completed work
- Handing the implementation commit and concise evidence to the Verifier
- Fixing concrete defects discovered during independent verification

The Developer does not decide whether its own implementation is accepted.

## Agent 3 — AI Dark Factory Verifier

Harness: OpenCode
Model: zai-org/GLM-5.3-Flash

The Verifier provides independent review using a different model and harness from the implementation agents.

Responsibilities include:

- Reading the complete stage specification independently
- Inspecting the actual repository state rather than trusting implementation claims
- Running the official harness
- Running additional specification-derived tests and probes
- Checking regressions against earlier stages
- Testing browser behavior where relevant
- Checking edge cases, concurrency, import/export and atomicity
- Reporting failures directly to the Developer
- Reporting successful verification to the Lead

The Verifier does not implement application features.

## Why These Models

GPT-6.1 Sol was used for the Lead and Developer because the primary work required long-horizon specification reading, implementation, debugging and repository-level reasoning.

GLM-5.3-Flash was used as the independent Verifier through Featherless and OpenCode.

Using a different model and harness for verification reduced the risk of the implementation agent simply confirming its own assumptions.

The Flash variant was selected because the full GLM-5.3 verifier was significantly more expensive for the long Tablekeeper verification context.

## Runtime Architecture

The Lead and Developer run through Codex.

The Verifier is a BAND remote agent connected through:

BAND Remote Agent
    ↓
Python BAND SDK runner
    ↓
OpenCode server
    ↓
Featherless
    ↓
zai-org/GLM-5.3-Flash

The Verifier works directly against the submission repository.

No model keys or BAND credentials are stored in this submission repository.

## Stage Workflow

For every stage, the human sends one stage-level task to the Lead.

The Lead reads the specification and delegates implementation to the Developer using an explicit BAND @mention.

The Developer implements and tests the stage, commits the work, and hands the revision to the Verifier.

The Verifier independently checks the submitted revision.

A failed verification returns to the Developer with concrete failures.

A successful verification is sent to the Lead.

The Lead then sends the final completion report to the human.

The next stage is not started until the current stage has passed verification.

## Stage Progression

### Stage 1

Stage 1 established the core reservation service.

The implementation was independently verified against the Stage 1 specification and official isolated harness.

Stage 1 finished with all 120 shipped Stage 1 checks passing.

### Stage 2

Stage 2 copied the verified Stage 1 implementation into a new stage-2 directory and extended only that copy.

The stage introduced the browser application and combined-table functionality.

The factory also performed desktop and 375px mobile visual checks.

Final isolated verification passed:

Stage 1: 120/120
Stage 2: 25/25

Stage 1 remained unchanged.

### Stage 3

Stage 3 copied Stage 2 forward and added policies, reservation history, revisions, recurring series and upgrade behavior.

The UI was deliberately redesigned during this stage.

The earlier cream/green hospitality presentation was replaced with a distinct dark luxury dining experience while preserving Stage 2 browser behavior.

Independent verification discovered an import-integrity edge case involving invalid series state.

The Developer corrected the issue and the Verifier independently retested the new revision.

Final Stage 3 revision:

0698f81471edbb1622f4c4995a36ba6ffcf0682c

Final isolated verification passed:

Stage 1: 120/120
Stage 2: 25/25
Stage 3: 7/7

The Verifier also reported 146/146 independent specification-derived probes passing.

Stage 1 and Stage 2 remained unchanged.

### Stage 4

Stage 4 copied Stage 3 forward and implemented the final replanning and series-amendment requirements.

The dark luxury UI was preserved.

The Developer implemented closure-aware availability, deterministic replanning, atomic application, recurring-series amendments and related revision/history behavior.

During review, additional import and stored-preview validation cases were identified and hardened before acceptance.

The final accepted Stage 4 revision was:

526e383ff6c9f91d0fafa5669da48b1a3300b07e

Independent verification reported:

Stage 1: 120/120
Stage 2: 25/25
Stage 3: 7/7
Stage 4: 6/6

Additional Stage 4 verification included:

104/104 specification-derived probes
17/17 replan tests
browser checks passed
offline-runtime checks passed

The earlier stage trees remained unchanged.

## Verification Strategy

The factory intentionally uses multiple levels of verification.

The Developer performs implementation-level testing before handoff.

The Verifier then independently reproduces important behavior from a clean checkout.

Verification includes:

- Official isolated harness tests
- Specification-derived edge cases
- HTTP/API probes
- Concurrency and atomicity tests
- Import/export validation
- Backward-compatibility checks
- Browser tests
- Desktop and mobile UI checks
- Offline-runtime behavior

This separation caught real issues during development rather than merely producing duplicate confirmation.

## UI Strategy

Stage 2 initially introduced a polished restaurant reservation interface.

Because several Tablekeeper implementations naturally converged on a similar cream, green and serif hospitality aesthetic, Stage 3 deliberately adopted a substantially different visual direction.

The final interface uses a dark luxury / fine-dining presentation with:

- Dark cinematic surfaces
- Gold accents
- Large editorial typography
- Distinct reservation panels
- Responsive desktop and mobile layouts
- Clear availability and disabled states
- Consistent booking, confirmation and reservation-management screens

The visual redesign was required to preserve all Stage 2 browser behavior and automated-test compatibility.

## Lessons and Factory Improvements

The first verification workflow generated too much duplicated evidence and occasionally repeated specification content in room messages.

The factory was subsequently tightened so that agents reference specification paths rather than copying full specifications into messages.

Verification artifacts and harness logs were also kept outside the submission repository unless explicitly required.

This reduced room noise and prevented evidence-only commits from changing an already verified implementation revision.

A later long-running Stage 4 verification demonstrated another operational lesson: a remote verifier may continue making model calls while processing queued room events before a visible response appears.

The final factory therefore favors concise handoffs and explicit final revision binding.

## Reproducing the Factory

A compatible setup requires:

1. BAND room with three distinct seats
2. Codex-backed Lead
3. Codex-backed Developer
4. OpenCode-backed remote Verifier
5. Featherless access to zai-org/GLM-5.3-Flash
6. Shared access to the submission repository
7. Official Tablekeeper challenge repository and harness

Agent-specific harness/model declarations are provided in mandates/.

The full BAND room history is provided in room.json.

## Final Validation

Before submission, the repository should be checked using:

python -m harness check \
  <submission-repository> \
  --track tablekeeper

and:

python -m harness run \
  --track tablekeeper \
  --repo <submission-repository> \
  --all \
  --mode isolated

The same checks should be repeated against a fresh clone of the public GitHub repository to ensure that every required file is actually committed and available to the judges.
