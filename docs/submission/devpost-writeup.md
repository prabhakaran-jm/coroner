# Devpost description (replacement text)

Status: draft for the user to paste into Devpost by hand. Nothing here was submitted. The Devpost page was not edited. The demo video is not yet recorded, so the text refers to it only as "to be added".

Every figure comes from a repository record. Sources are named in brackets and listed at the end. Remove the brackets when pasting.

---

## Coroner: crash forensics for Python containers

### Category: Frankenstein (proposed)
This is the proposed category. The live Devpost category is unchanged and the decision is the author's to make on Devpost.
Coroner brings together four parts that each see a different side of a container's death: in-container telemetry (the Probe), an independent Collector outside the container, Docker's own termination records (`docker inspect` and the run manifest) and a read-only forensic Viewer. Using Python or Docker is not the novelty. The differentiator is how the parts are combined:
- **Evidence preservation:** Samples are stored outside the container, so they survive its death.
- **Source comparison:** the manifest is checked against the raw `docker inspect` output and any discrepancy is reported, not smoothed over.
- **Explicit uncertainty:** derived clocks are labelled as derived, and a cause is never invented when the record does not show one.

### The problem

When a container dies, two different failures can leave the same exit code. Docker reports `ExitCode` 137 both for a process that was killed with SIGKILL and for a process that the kernel killed for exceeding its memory limit. The exit code alone does not say which one happened, and the telemetry the container was sending is often gone with it.

### What Coroner does

Coroner keeps the evidence outside the container and shows only what was recorded.

- **Probe** (`coroner/probe`) runs inside the container and posts a Sample (including RSS) every 500 ms by default.
- **Collector** (`coroner/collector`) runs outside it, stores each acknowledged Sample as append-only JSON Lines on a named volume and reports `storage_ok` from a real write probe.
- **Runner** (`coroner/runner`) starts the Demo App with Docker Compose, causes one termination (A, B or C), reads `docker inspect` and writes a run manifest with mandatory checks.
- **Viewer** (`coroner/viewer`) is a read-only local web page and JSON API over saved runs. It binds `127.0.0.1` only and never writes to the runs directory.

Coroner reports what Docker and the Samples recorded. It does not infer a root cause from an exit code.

### The real A, B and C demonstration

Three real runs were recorded on one Windows host with Docker Desktop (Linux containers, WSL2). The values below were checked against the manifests, `docker inspect` files and the Viewer API [canonical-run-verification.md].

| Run | Termination as recorded | ExitCode | OOMKilled | Samples | Peak RSS (bytes) |
|---|---|---|---|---|---|
| A | controlled fatal process exit via `os._exit(1)` | 1 | false | 15 | 74,326,016 |
| B | SIGKILL sent by the Runner (`docker kill --signal=KILL`) | 137 | false | 15 | 74,174,464 |
| C | Docker OOM kill at a 134,217,728-byte (128 MiB) limit | 137 | true | 45 | 141,090,816 |

All three had zero missing Samples, zero unreconciled drops and zero malformed evidence lines. C's peak Sample RSS (about 1.05 times the limit) is a different measure from the cgroup accounting that triggered the kill, so the verdict rests on `OOMKilled` true and ExitCode 137 only. The underlying cause of C's memory growth is not established by the saved evidence.

### Why matching exit codes do not prove matching causes

B and C both ended with ExitCode 137. Docker recorded `OOMKilled` false for B and true for C, and the Runner's manifest records the SIGKILL it sent in B and records no Runner kill in C. The Viewer's compare page puts these side by side under the headline "The same exit code does not always mean the same failure". It shows each run's own recorded values and never infers B from C or C from B.

### Provenance and clock uncertainty

The Viewer labels every time with the clock it came from: Container, Collector, Host or Docker. These clocks are not recorded as synchronised. For B, the Viewer draws the SIGKILL marker at the Host clock time the Runner stamped just before it issued the kill command, and shifts Sample times by the recorded clock offset (9 ms, uncertainty 12.5 ms). That correction is derived from Sample timing and is not independently verified. A and C use raw Probe Capture_Time. Unobserved intervals between the last Sample and the termination are reported as unobserved, not filled in [canonical-run-verification.md; README sections 10, 11 and 14].

### Synthetic judge build

The real `runs/` directory is not published. Judges instead generate three synthetic runs with `scripts/make-sample-runs.py` and open them in the Viewer, with no Docker. The synthetic runs carry the same exit-code and OOMKilled values, but no container ran and nothing was measured, so they do not reproduce the real experiments. Instructions: `JUDGING.md`. Rationale: `docs/m2-plan/judge-test-build.md`.

### Verified tests and limitations

- Latest recorded runs on Windows with Python 3.12.10: `tests/unit` 5,962 collected, 5,950 passed, 0 failed, 12 skipped, exit code 0 (on a PR #4 worktree, before the PR #3 additions). The Viewer, structure, docs, preflight and sample generator tests: 1,604 collected, 1,601 passed, 3 skipped, exit code 0. A whole-tree run from a clean checkout of this branch is PENDING.
- A screenshot privacy audit inspected all 75 tracked images (54 unique) at pixel level and found nothing.
- The Viewer is "accepted with documented exceptions" [m1.1-acceptance.md]. Automated headless Chrome 158 ran the pages. Human-operated Chrome, real 200% browser zoom and a screen reader are NOT TESTED. The optional Node test did not run.
- Verified on Windows with Docker Desktop on one host. Native Linux hosts are not verified, macOS was not tested, and there is no repeat-consistency proof across hosts.
- The Viewer is local only. It is not hosted.

### How Kiro specs, tasks and checkpoints guided development

Task history is documentation, not proof of completion. The `tasks.md` checkboxes and the `tasks.meta.json` `executionHistory` record that tasks were run and marked complete. The evidence that the resulting code works is the test suite and the acceptance records, not the task log itself. The `.kiro` directory contains only `specs/`: no hooks, steering, MCP servers, powers, custom agents, Kiro Crew or sub-agent activity are recorded, and none is claimed.

The work was organised as three Kiro specs in `.kiro/specs/`: `coroner-m0` (evidence pipeline and the three experiments), `coroner-m1-ui` (the Viewer) and `coroner-m2-demo` (demo preparation). M0 and M1 each have `requirements.md`, `design.md` and `tasks.md`. The tasks files include checkpoint tasks (9 in M0, 4 in M1, 6 in M2) that stop for review and require real test counts before the next group starts. The M2 task list and the M1.1 acceptance record forbid recording an expected result as an observed one, which is why the acceptance record says NOT TESTED where nothing was observed. Detail: `docs/submission/kiro-usage.md`.

### How Hypothesis property tests contributed

The repository has 28 `test_prop_*` files that hold 83 Hypothesis `@given` tests (collector 10, demo app 3, evidence 32, probe 3, runner 35), and 94 `@given` tests across the test tree. They cover sequence and gap accounting, evidence repair and round trips, the Probe's dropped counter, the Collector's acknowledgement ordering and `storage_ok`, and the Runner's verdict and manifest logic. The M0 results record two defects in the `storage_ok` probe, each with a counterexample (a close error masking an earlier fsync failure, and non-printable characters in an error string), found and fixed while that property harness was built [m0.3-collector.md]. The M0 tasks record 6 property-test statuses, all passed.

Sources: `docs/m2-plan/canonical-run-verification.md`, `docs/m2-plan/judge-test-build.md`, `docs/m1-results/m1.1-acceptance.md`, `docs/m0-results/m0.3-collector.md`, `README.md`, `.kiro/specs/*/tasks.md`, `.kiro/specs/*/tasks.meta.json`.
