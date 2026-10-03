# Final isolated-mode harness evidence

Implementation revision tested: `c73dbf65441523e74d12e439ab5da78e5f4c1f7c`.
Run ID: `7647c2cf058347b386416aa0d86027f3`.
Started: `2026-10-03T13:31:45.337029+00:00`; finished: `2026-10-03T13:32:17.530674+00:00`.
Mode: `isolated`; run state: `completed`; provenance: `working-tree`.

Command, run from `/home/saidev/ai-dark-factory-tablekeeper/challenge`:

```sh
.venv/bin/python -m harness run --track tablekeeper --repo ../submission --stage 1 --mode isolated --out /home/saidev/ai-dark-factory-tablekeeper/checks/developer-s1-final-01
```

Stage 1: **120 collected, 120 passed, 0 failed/errors/skipped/deselected/xfailed** in 15.47 seconds.
Highest contiguous stage: **1**. Claimed stage: **1 on the shipped checks**.
The automatically executed Stage 2 overshoot probe failed as expected on the absent Stage 2 UI; no Stage 2 implementation work was performed. Exit code: **0**.
One Stage 1 warning concerns pytest attempting to write a cache to the harness's read-only test mount, and does not skip tests. The runner and standalone service operate on the harness's internal network with no outbound access and service limits of 2 vCPU/2 GiB.

Original artifacts remain at the unique output path above. Exact text copies of `report.json`, both suite logs and both count files are committed in `evidence/isolated-final-01/`, including the failed Stage 2 overshoot log. The earlier failed developer host-port run is separately retained in `evidence/failed-host-port-run.log`.
The full 21-test specification-derived edge/concurrency suite and disconnected startup checks are documented in `evidence/DEVELOPER.md`. This shipped harness result is directional evidence; independent review against the full spec remains required.

Observed harness stdout:

```text
building ../submission/stage-1 ...
  stage 1: pass  (log: /home/saidev/ai-dark-factory-tablekeeper/checks/developer-s1-final-01/stage-1.log)
  stage 2: fail  (log: /home/saidev/ai-dark-factory-tablekeeper/checks/developer-s1-final-01/stage-2.log)
highest contiguous stage: 1
claimed stage: 1 on the shipped checks
report: /home/saidev/ai-dark-factory-tablekeeper/checks/developer-s1-final-01/report.json
NOTE: this run only includes a portion of the full tests that are applied before judging; this is meant to provide directional feedback, and ultimately you may not pass the stage with the full set of tests.
```
