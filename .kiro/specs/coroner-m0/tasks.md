# Implementation Plan: Coroner M0 (Crash Evidence Survival)

## Overview

This plan implements the approved requirements and design for Coroner M0 in eight milestones, in dependency order. The implementation language is Python (the design uses Python signatures). The mandatory M0 proof comes first: the Evidence library, the Collector with `storage_ok`, the Probe and Demo_App, the Compose environment, the Experiment_Runner, and the three experiments (A: `os._exit(1)`, B: SIGKILL, C: genuine Docker OOM kill) together with the tests that validate them. Deferrable items are marked optional with `*` and sit at the end of their milestones.

Conventions used in every task:

- **Files** lists files to create or modify. **Expected** states the observable result. **Verify** gives PowerShell-friendly commands (repository root; after task 1.8 the main `.venv` is activated). **Done when** gives the completion criteria.
- Property test sub-tasks reference the design's Correctness Property numbers (1 to 29, including 26) and use Hypothesis with `max_examples=100` minimum and the tag comment `# Feature: coroner-m0, Property N: <title>`.
- Results notes live in `docs/m0-results/` (tracked in Git). Raw run output lives in `runs/` (gitignored).

## Execution Rules

These rules apply to every task and are part of the completion criteria.

1. **No unverified claims.** A task is complete only after its verification commands were actually executed and the real output (pass, fail, skip counts, exit codes, versions) was recorded in the task's completion note or in `docs/m0-results/`. Never write that a test passed, an image pulled, or an experiment succeeded unless it was executed and observed. If a command could not be run, record that and why, and do not tick the task.
2. **Experiment results are recorded, not assumed.** Experiment tasks (7.x) must write real run ids, observed exit codes, `State.OOMKilled`, `State.Status`, inspect time, and Collector health results into `docs/m0-results/experiment-results.md`, copied from the Run_Manifest and from an independent `docker inspect`. Simulated or hand-written values are forbidden.
3. **Stop and ask on conflict.** The approved requirements and design are authoritative. If implementation reveals a conflict, gap, or infeasibility (for example a pinned version that cannot satisfy a design decision, a tolerance that cannot hold, or a Docker behavior that contradicts the design), STOP, record the conflict, and ask the user. Do not silently deviate, relax a check, or change a documented constant (including the 250 ms Experiment B tolerance).
4. **Candidate pins are unverified.** The design's candidate dependency pins and Python image tag are unverified until tasks 1.4 and 1.5 pass. The main `.venv` is created only after task 1.5 (task 1.8), from the verified pinned requirements. Do not finalise `requirements/*.txt` or Dockerfile tags before those tasks record real results.
5. **Default `pytest` is Docker-free and network-free.** Docker integration tests live in `tests/docker_integration/`, carry the marker `docker_integration`, and run only with `--run-docker` or `CORONER_RUN_DOCKER=1`.
6. **Destructive endpoints stay local and off by default.** They return 404 unless `CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS` is exactly `1`, and host ports bind to `127.0.0.1` only.
7. **Genuine OOM only.** Experiment C is never replaced by a simulated result, a runner-sent signal, or the crash endpoint. Its verdict comes from real `docker inspect` `State.OOMKilled` and `State.ExitCode`.
8. **Single-clock comparisons.** All timestamps are UTC and compared on the host clock, using the measured and recorded host-to-container offset (task 7.1) where a container clock is involved.
9. **Git.** All work happens on branch `feat/coroner-m0` (task 1.1 creates it only if it does not exist; otherwise it checks `git status --short` and switches to it). The approved `.kiro/specs/` files are never discarded or overwritten: no `git reset --hard`, `git checkout -- .`, `git clean`, `git stash -u`, or forced switch. `.kiro/` must stay tracked. Tasks PROPOSE commits and never perform them: task 1.11 proposes the baseline specification commit, and each milestone checkpoint proposes a milestone commit, only after its tests were actually executed and the real counts recorded. No `git commit` and no `git push` without the user's explicit approval given at that moment. Before any push, verify the GitHub remote is private (task 1.11 and task 8.5). Never change repository visibility; the user decides.
10. **Pytest invocation rule (no false green).** Every task that runs pytest states the expected minimum number of collected and passed tests (determine it with `python -m pytest --collect-only -q` first and record it). Pytest exit code 5 (no tests collected) is a FAILURE for that task, never a pass. The only exception is a task that explicitly says the suite is intentionally empty: it must expect exit code 5 and report it as "no tests collected", not as "pass". Record the exit code (`$LASTEXITCODE`) and the collected, passed, failed, skipped, and deselected counts. No task may claim pytest passes before pytest is installed in the main `.venv` (task 1.8) or before the tests exist. Before then, use `python -c` import checks.
11. **Docker evidence and volume protection.** Prefer `docker compose down` (no `-v`). Before ANY `docker compose down -v`, `docker volume rm`, or `docker volume prune` (prune is forbidden), the task must: (a) name the exact volume (`coroner_coroner_evidence` for Compose project `coroner`; confirm the real name with `docker volume ls --filter label=com.docker.compose.project=coroner`, or the spike volume in task 1.9) and verify it by name and by label (`docker volume inspect <name> --format '{{json .Labels}}'` shows `com.docker.compose.project=<project>`); (b) list its contents and count run files (for example `docker run --rm -v <name>:/data:ro --entrypoint python <verified image> -c "import pathlib;p=pathlib.Path('/data/evidence');print(sorted(x.name for x in p.glob('*.jsonl')))"`) and record the count; (c) if evidence exists that must be retained, copy it out first to the gitignored `runs/archive/<UTC timestamp>/` (via `docker cp` from a stopped helper container or a read-only helper container) and verify the copy by file count and size; (d) obtain the user's explicit approval at that moment, showing the volume name, file count, and archive path; (e) delete only that named volume. Never remove unrelated Docker volumes, images, networks, or containers, and never delete a volume whose name or label does not match.
12. **Priority and cut-line.** Tasks whose title contains `(PRIORITY)` are mandatory M0 proof tasks. Optional tasks (marked `*`) are dropped first when behind schedule. A mandatory task is never dropped without asking the user (see "Schedule and Priorities").

## Tasks

- [x] 1. M0.1 Repository setup, Python packaging, Docker prerequisites and dependency verification
  - [x] 1.1 Verify toolchain and repository hygiene (.gitignore, .kiro tracked)
    - FIRST verify the Python toolchain and record versions: `git --version`, `python --version`, `python -m pip --version` (also `where.exe python` to record which interpreter is used). If `python` or `pip` is missing or older than the design's minimum, STOP and ask. Do not install anything in this task.
    - Git branch: run `git status --short` and `git branch --list feat/coroner-m0`. If the branch does not exist, create it with `git switch -c feat/coroner-m0`. If it exists, switch with `git switch feat/coroner-m0` after checking status. The untracked approved `.kiro/specs/` files must remain untouched (never use `git stash -u`, `git clean`, `git reset --hard`, or `git checkout -f`). Record `git branch --show-current`.
    - Confirm `.kiro/` is not ignored. Add `runs/`, `.venv*/` (covers `.venv-verify`, `.venv-repro`), and `spike/` to `.gitignore`. Check that no existing pattern hides planned files: the current `.gitignore` ignores `lib/`, `build/`, `*.log`, `*.manifest`, `*.spec`, and `.env`, so run manifests must be named `manifest.json` (not `*.manifest`), and no planned directory may be called `lib/` or `build/`.
    - Files: modify `.gitignore`; create `docs/m0-results/environment.md`.
    - Expected: current branch is `feat/coroner-m0`; `.kiro/specs/coroner-m0/*` is trackable and still present; `runs/`, `.venv-verify/`, and `spike/` are ignored.
    - Verify: `python --version; python -m pip --version; git branch --show-current; git check-ignore -v .kiro/specs/coroner-m0/tasks.md` (no output, exit code 1); `git check-ignore -v runs/x/manifest.json; git check-ignore -v .venv-verify/x; git check-ignore -v spike/x` (each shows its rule); `git status --short` (the `.kiro/` files are still listed).
    - Done when: command output is recorded in `docs/m0-results/environment.md`, the branch is `feat/coroner-m0`, and `.kiro` is confirmed not ignored. No commit is made.
    - _Requirements: 14.1, 14.5_

  - [x] 1.2 Create Python packaging and package skeleton (no pytest yet)
    - Create `pyproject.toml` (project metadata, `requires-python` set after task 1.5, setuptools package discovery for `coroner*`, no runtime dependencies listed here), `pytest.ini` (`testpaths = tests`, register `docker_integration`), and empty packages with `__init__.py`: `coroner/common`, `coroner/evidence`, `coroner/probe`, `coroner/demo_app`, `coroner/collector`, `coroner/runner`, `coroner/runner/experiments`.
    - Create directories `tests/unit/`, `tests/docker_integration/`, `requirements/`, `docker/`, `docs/m0-results/`.
    - Files: `pyproject.toml`, `pytest.ini`, `coroner/**/__init__.py`, `tests/unit/__init__.py` or equivalent.
    - Expected: `python -c "import coroner.common, coroner.evidence, coroner.probe, coroner.demo_app, coroner.collector, coroner.runner"` succeeds.
    - Pytest is not installed at this point (it is installed in tasks 1.5 and 1.8), so this task does not run pytest and does not claim that tests pass.
    - Verify: `python -c "import coroner.common, coroner.evidence, coroner.probe, coroner.demo_app, coroner.collector, coroner.runner; print('ok')"`; `python -c "import configparser; c=configparser.ConfigParser(); c.read('pytest.ini'); print(c.sections(), dict(c['pytest']))"` (checks `pytest.ini` parses and has `testpaths` and the marker); `python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['name'])"` (needs Python 3.11 or newer; otherwise record that `tomllib` is unavailable and skip).
    - Done when: the import and parse checks succeed with output recorded. The skeleton has zero tests by design; "no tests exist yet" is recorded as a fact, not as a pass.
    - _Requirements: 14.4_

  - [x] 1.3 Implement pytest opt-in, markers, and Docker-free/network-free guards with tests
    - `tests/conftest.py`: `--run-docker` option, `CORONER_RUN_DOCKER=1` env opt-in, deselect `docker_integration` items when not opted in (report them), skip with "Docker is unavailable" when opted in and `docker info` fails.
    - `tests/unit/conftest.py`: autouse guards that make `socket.getaddrinfo` and `socket.socket.connect` raise (allow `socket.socketpair` for the Windows asyncio self-pipe), and make `subprocess.Popen` fail for argv starting with `docker` or `docker-compose`.
    - Tests: `tests/unit/test_optin.py` using `pytester` or a subprocess of `sys.executable -m pytest` against a tiny fixture project; `tests/unit/test_guards.py` showing a connect attempt and a `docker` Popen are blocked.
    - Files: `tests/conftest.py`, `tests/unit/conftest.py`, `tests/unit/test_optin.py`, `tests/unit/test_guards.py`, `tests/docker_integration/test_placeholder.py` (marked, trivial, removed or replaced in M0.7).
    - Expected: default `pytest` deselects the marked placeholder; `--run-docker` selects it; with Docker unavailable it is skipped with the stated message.
    - Prerequisite: the main `.venv` from task 1.8 exists and is used (`.\.venv\Scripts\python.exe`); this task must not run before 1.8.
    - Expected counts: first `python -m pytest --collect-only -q` and record the collected count N (at least the tests in `test_optin.py` and `test_guards.py` plus the placeholder). Default run: exit code 0, at least 2 passed, exactly 1 deselected (the placeholder). `--run-docker` run: exit code 0; the placeholder is either passed (Docker available) or skipped with "Docker is unavailable". Exit code 5 or zero collected tests is a FAILURE.
    - Verify: `.\.venv\Scripts\python.exe -m pytest --collect-only -q; .\.venv\Scripts\python.exe -m pytest -q; .\.venv\Scripts\python.exe -m pytest --run-docker -q; $env:CORONER_RUN_DOCKER="1"; .\.venv\Scripts\python.exe -m pytest -q; Remove-Item Env:CORONER_RUN_DOCKER`.
    - Done when: the runs were executed and their exit codes and passed, failed, skipped, and deselected counts are recorded against the expected counts.
    - _Requirements: 13.6, 13.7, 13.8, 13.9, 13.10, 13.11_

  - [x] 1.4 (PRIORITY) Verify Docker prerequisites and Python base-image availability
    - Record `docker version`, `docker compose version`, `docker info` (check Server OS/Arch is Linux and the WSL2 backend on Windows), and available memory for the VM.
    - Check the candidate image tag from the design (`python:3.12.8-slim-bookworm`) with `docker manifest inspect python:3.12.8-slim-bookworm` and `docker pull python:3.12.8-slim-bookworm`. If the tag is missing, unavailable for the host platform, or inappropriate, STOP and ask the user before choosing another tag. Record the image digest.
    - Check that `docker run --rm --memory 64m --memory-swap 64m python:3.12.8-slim-bookworm python -c "print(1)"` works (memory limits are supported).
    - Files: create `docs/m0-results/docker-prereqs.md`.
    - Expected: a real record of tool versions, pull result, digest, and platform.
    - Verify: `docker version; docker compose version; docker info; docker manifest inspect python:3.12.8-slim-bookworm; docker pull python:3.12.8-slim-bookworm; docker image inspect python:3.12.8-slim-bookworm --format '{{.Id}} {{.Os}}/{{.Architecture}}'`.
    - Done when: actual outputs and the chosen, verified tag are recorded. No Dockerfile is written yet.
    - _Requirements: 8.2, 8.9, 14.1, 16.4_

  - [x] 1.5 (PRIORITY) Verify dependency compatibility and finalise exact pins
    - In a clean venv (`python -m venv .venv-verify`), install the design's candidate pins (`fastapi==0.115.6`, `uvicorn==0.34.0`, `pytest==8.3.4`, `hypothesis==6.122.3`, `httpx==0.28.1`) and run `pip check`. Use `pip index versions <pkg>` or `pip download --no-deps` to confirm each version exists. If a candidate is missing, yanked, or incompatible with the verified Python version (also check against the image's Python from task 1.4), STOP and ask before substituting.
    - Generate transitive pins with `pip freeze` into `requirements/demo_app.txt`, `requirements/collector.txt`, `requirements/runner.txt` (stdlib-only plus any needed), and `requirements/dev.txt` (includes the others, pytest, hypothesis, httpx, PyYAML if the compose parse test needs it). All lines use `==`, with no ranges.
    - Set `requires-python` in `pyproject.toml` to the verified minimum.
    - Files: `requirements/*.txt`, `pyproject.toml`, `docs/m0-results/dependency-verification.md`.
    - Expected: pinned files whose install in a fresh venv passes `pip check`.
    - This is the clean-venv dependency verification and comes BEFORE the main `.venv` (task 1.8). It installs and checks packages but does not run the test suite (no tests exist yet).
    - Verify: `python -m venv .venv-verify; .\.venv-verify\Scripts\python.exe -m pip install -r requirements/dev.txt; .\.venv-verify\Scripts\python.exe -m pip check; .\.venv-verify\Scripts\python.exe -m pip freeze; .\.venv-verify\Scripts\python.exe -m pytest --version`.
    - Done when: real install, `pip check`, and `pytest --version` output are recorded, the image-build `pip check` is marked as pending for task 5.5, and `.venv-verify` is removed (it is also gitignored). Removal is of this one directory only.
    - _Requirements: 14.5, 14.1_

  - [x] 1.6 Add structure tests: import graph, fresh-interpreter import, exact pins
    - `tests/unit/test_structure.py`: AST check of the allowed-import table from the design (probe imports only the stdlib; evidence imports common; collector imports evidence and common; runner imports evidence and common; demo_app imports probe; nothing imports runner, collector, or demo_app except its entry points); fresh-interpreter `python -c "import coroner.<pkg>"` per package using `sys.executable` only; every non-comment line in `requirements/*.txt` matches `^[A-Za-z0-9_.\-\[\]]+==\S+$`.
    - Files: `tests/unit/test_structure.py`.
    - Prerequisite: main `.venv` from task 1.8 (pytest must be installed there).
    - Expected: tests pass on the skeleton and keep guarding later code. Expected count: at least 3 test functions collected and passed (import graph, fresh-interpreter import, exact pins); exit code 5 is a FAILURE.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_structure.py --collect-only -q; .\.venv\Scripts\python.exe -m pytest tests/unit/test_structure.py -q`.
    - Done when: output is recorded with zero failures, exit code 0, and the passed count meets the expected count.
    - _Requirements: 14.4, 14.5, 13.8_

  - [x] 1.7 Checkpoint - M0.1 verification gate (toolchain, dependencies, Docker, tests)
    - Run `.\.venv\Scripts\python.exe -m pytest -q` (expected: exit code 0 and at least the counts of 1.3 plus 1.6; exit code 5 is a FAILURE), record the real counts, confirm that 1.1, 1.4, 1.5, and 1.8 recorded real results, and ask the user if questions arise. The spike (1.9, 1.10) and the baseline commit proposal (1.11) are completed after this gate and closed by checkpoint 1.12.

  - [x] 1.8 (PRIORITY) Create the main `.venv` from the verified pinned requirements
    - Only after 1.5 passed. `python -m venv .venv`; `.\.venv\Scripts\python.exe -m pip install -r requirements/dev.txt`; `pip check`; `pip freeze` compared to the pins in `requirements/dev.txt` (every pinned package present at the pinned version). The interpreter version must equal the one verified in 1.1 and 1.5. If the install differs from the verified result, STOP and ask.
    - Do not run the test suite to prove this task. Verify by `python -m pytest --version` only.
    - Files: `docs/m0-results/environment.md` (append the `.venv` section). `.venv` is gitignored (existing `.venv` rule).
    - Expected: reproducible `.venv` with pinned packages and pytest 8.3.4 (or the version pinned in 1.5).
    - Verify: `python -m venv .venv; .\.venv\Scripts\python.exe --version; .\.venv\Scripts\python.exe -m pip install -r requirements/dev.txt; .\.venv\Scripts\python.exe -m pip check; .\.venv\Scripts\python.exe -m pip freeze; .\.venv\Scripts\python.exe -m pytest --version; git check-ignore -v .venv/x`.
    - Done when: real install, `pip check`, and `pytest --version` output are recorded, and `.venv` is confirmed ignored.
    - _Requirements: 14.5, 14.1_

  - [x] 1.9 Create the disposable OOM-survival spike (code only, `spike/`)
    - Purpose: a minimal early technical check that an independent collector container keeps a real telemetry sample after a genuine container OOM kill of a separate container. It is disposable, does NOT replace any M0 test or Experiment C, and never counts a simulated OOM as success. Prerequisites: 1.4 (verified image tag) and 1.8.
    - Create under `spike/` (gitignored): `spike/compose.spike.yaml` with `name: coroner-spike`, a uniquely named volume `coroner_spike_evidence`, containers `coroner-spike-collector` and `coroner-spike-victim`, the image tag verified in 1.4, no host ports (the victim reaches the collector over the compose network), no bind mounts of host paths other than read-only code. The victim has `mem_limit: 64m`, `memswap_limit: 64m`, `restart: "no"`, `privileged: false`. The collector has no memory limit tied to the victim and `restart: "no"`.
    - `spike/collector.py` (stdlib only): `http.server` on the compose network that appends each received JSON line to `/data/spike.jsonl` with write, flush, and `os.fsync`, then acknowledges; `GET /healthz`.
    - `spike/victim.py` (stdlib only): every 200 ms read real RSS from `/proc/self/statm` times the page size (no psutil, no fabricated value), grow memory for real by retaining `bytearray` chunks (for example 2 MiB per step, filled with non-zero bytes so pages are touched) until the kernel OOM-kills it, and POST each sample (`seq`, UTC time, `rss_bytes`) to the collector with a 200 ms timeout. No signal handler, no `atexit`, no simulated exit.
    - Files: `spike/compose.spike.yaml`, `spike/collector.py`, `spike/victim.py`, `spike/README.md` (how to run, how to clean up). Keep the whole spike under about 120 lines of Python.
    - Expected: `docker compose -f spike/compose.spike.yaml config` parses and shows the limits and names. No container is started in this task.
    - Verify: `docker compose -f spike/compose.spike.yaml config; python -m py_compile spike/collector.py spike/victim.py`.
    - Done when: the config output and compile check are recorded.
    - _Requirements: 11.1, 11.3, 4.3, 5.1_

  - [x] 1.10 (PRIORITY) Run the spike and record real results in `docs/m0-results/oom-spike.md`
    - Start only the spike project: `docker compose -f spike/compose.spike.yaml -p coroner-spike up -d --build collector`, wait for `/healthz`, then start the victim. Wait for the victim to stop (up to 120 s). Never use plain `docker compose` against the main project here.
    - Independent checks (copy real output, never hand-write): `docker inspect coroner-spike-victim` shows `State.OOMKilled` true, `State.ExitCode` 137, `State.Status` exited, and `HostConfig.Memory` 67108864; `docker inspect coroner-spike-collector` shows `State.Running` true; the volume holds `spike.jsonl` with at least 3 lines of valid JSON samples; read back the sample count and the highest real `rss_bytes` (greater than the first RSS, below or near the 64 MiB limit); `docker logs coroner-spike-victim` does not claim a handled exit.
    - Result rule: all of OOMKilled true, exit 137, collector running, and at least 3 samples present on the volume are required for PASS. Any other outcome (for example OOMKilled false, collector stopped, no samples) is recorded as FAIL with the real values, and the task STOPS and asks the user. Do not retry with a different tactic, do not simulate, do not relax a check.
    - Cleanup: `docker compose -p coroner-spike -f spike/compose.spike.yaml down` (containers and network only, no `-v`). Delete the spike volume `coroner-spike_coroner_spike_evidence` (verify the exact name with `docker volume ls --filter label=com.docker.compose.project=coroner-spike`) only by following Execution Rule 11: list contents and count run files, archive `spike.jsonl` to `runs/archive/spike-<UTC timestamp>/`, and obtain explicit user approval. Leave the `spike/` directory in place (gitignored) unless the user approves deleting it.
    - Files: `docs/m0-results/oom-spike.md` (tracked), `runs/archive/` (gitignored).
    - Expected: `oom-spike.md` contains the real container ids, inspect values, collector status, sample count, first and highest RSS, the host time of the run, and a PASS or FAIL line. It states that it is a disposable spike and not an M0 experiment result.
    - Verify: `docker inspect coroner-spike-victim --format '{{.State.OOMKilled}} {{.State.ExitCode}} {{.State.Status}} {{.HostConfig.Memory}}'; docker inspect coroner-spike-collector --format '{{.State.Running}}'; docker volume ls --filter label=com.docker.compose.project=coroner-spike`.
    - Done when: results and PASS or FAIL are recorded from real output, the collector and victim containers are removed with `down`, and any volume deletion followed Execution Rule 11.
    - _Requirements: 11.3, 11.4, 11.5, 11.10, 5.3, 4.3_

  - [x] 1.11 Propose the baseline specification commit and verify remote privacy (propose only)
    - Show the user: `git branch --show-current` (must be `feat/coroner-m0`), `git status --short`, `git diff --stat`, and the exact paths that would be staged: `.kiro/specs/coroner-m0/` (requirements.md, design.md, tasks.md, `.config.kiro`) and `.gitignore`. Add nothing else. Draft a commit message (for example `docs: add approved coroner-m0 specification and gitignore rules`).
    - Verify the GitHub remote is private before any push: `git remote -v`, then `gh repo view --json visibility,url` (or, if `gh` is not available, ask the user to confirm visibility in the GitHub settings). Record the real result. If the repository is public or its visibility cannot be determined, do NOT push; report and ask. Never change visibility.
    - Do NOT run `git add`, `git commit`, or `git push` unless the user explicitly approves in this step; if approved, stage only the listed paths and commit only. Pushing needs a separate explicit approval.
    - Files: none modified.
    - Verify: `git branch --show-current; git status --short; git diff --stat; git remote -v; gh repo view --json visibility,url`.
    - Done when: the staging list, draft message, and recorded visibility result are shown to the user, and either the user's approval or a "deferred" decision is recorded.
    - _Requirements: 14.1_

  - [x] 1.12 Checkpoint - M0.1 complete (including spike)
    - Confirm that 1.10 recorded a PASS from real output (on FAIL, stop and ask), run `.\.venv\Scripts\python.exe -m pytest -q` again (expected exit code 0, passed count at least that of checkpoint 1.7, exit code 5 is a FAILURE), and record the counts. Propose (do not perform) a milestone commit per Execution Rule 9: list specific paths (packaging, `tests/`, `requirements/`, `docs/m0-results/`, `.gitignore`), draft the message, and wait for explicit approval. Ask the user if questions arise. Target date: 14 November 2026.

- [x] 2. M0.2 Evidence schema, JSONL writer, reader, persistence and repair, with unit tests
  - [x] 2.1 Implement `coroner.common` helpers with unit tests
    - `timeutil.py`: `utc_now_ms()`, `format_utc_ms(dt)`, `parse_utc_ms(text)` (UTC, `YYYY-MM-DDTHH:mm:ss.sssZ`). `envparse.py`: `parse_int_env()`, `parse_mem_size()` (units b, k, m, g, powers of 1024, error names the value).
    - Files: `coroner/common/timeutil.py`, `coroner/common/envparse.py`, `tests/unit/common/test_timeutil.py`, `tests/unit/common/test_envparse.py`.
    - Expected: UTC millisecond strings round-trip; invalid sizes raise an error naming the value.
    - Verify: `python -m pytest tests/unit/common -q`.
    - Done when: tests pass with output recorded; `common` imports only the stdlib.
    - _Requirements: 2.2, 4.2, 8.3, 8.4_

  - [x] 2.2 Implement `evidence/schema.py`
    - `SCHEMA_VERSION`, `RUN_ID_RE` (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`), `RECORD_TYPES`, `ValidationIssue`, `validate_run_id`, `validate_sample` (types, ranges, bool-not-int, null needs reason of 1 to 200 characters, unknown fields rejected, `interval_ms` 100 to 60000, `dropped` 0 to 4294967295), `validate_record`.
    - Files: `coroner/evidence/schema.py`.
    - Expected: valid Samples from the design examples pass; each mutation yields a specific `reason_code`.
    - Verify: `python -m pytest tests/unit/evidence/test_schema.py -q` (written in 2.3).
    - Done when: module implemented and covered by 2.3.
    - _Requirements: 4.5, 4.7, 4.9, 7.6, 7.7, 2.9_

  - [x] 2.3 Write schema unit tests
    - Cases: the design's valid and null-measurement Sample examples; missing, wrong type, out of range, empty or over-64-character or pattern-violating run ids; null without reason; unknown field; boolean as integer; NaN.
    - Files: `tests/unit/evidence/test_schema.py`.
    - Expected: every mutation is rejected with the expected `field` and `reason_code`.
    - Verify: `python -m pytest tests/unit/evidence/test_schema.py -q`.
    - Done when: tests pass with output recorded.
    - _Requirements: 4.5, 4.9, 7.7_

  - [x] 2.4 (PRIORITY) Implement `evidence/writer.py`
    - `Durability`, `ConfigError`, `parse_durability`, `serialize_record` (`ensure_ascii=True`, `allow_nan=False`, compact separators, one `\n`), `fsync_directory` (no-op on win32), `EvidenceWriter` with injectable `fsync` and `fsync_dir`, `path_for`, `append` (one buffered write, flush, fsync under crash, directory fsync on creation), `append_bytes_durably`, `close`. Binary mode only.
    - Files: `coroner/evidence/writer.py`.
    - Expected: `durability` reflects what was actually done; a failing fsync raises `OSError`.
    - Verify: `python -m pytest tests/unit/evidence/test_writer.py -q` (written in 2.5).
    - Done when: module implemented and covered by 2.5.
    - _Requirements: 4.6, 4.10, 4.11, 4.12, 4.13, 4.14, 4.15, 5.4, 5.12_

  - [x] 2.5 Write writer unit tests and Property 6 (durability configuration parsing)
    - Unit tests with a recording `fsync` double: crash calls fsync on the file after write and on the directory on creation; process never calls fsync; invalid value raises `ConfigError` naming the value; one line per record with exactly one `\n`.
    - **Property 6: Durability configuration parsing** over arbitrary text plus the boundary values `process`, `crash`, `Crash`, ` process`, empty, unset.
    - Files: `tests/unit/evidence/test_writer.py`, `tests/unit/evidence/test_prop_durability.py`.
    - Expected: Property 6 holds; crash default confirmed.
    - Verify: `python -m pytest tests/unit/evidence/test_writer.py tests/unit/evidence/test_prop_durability.py -q`.
    - Done when: tests pass with output recorded.
    - **Validates: Requirements 4.10, 4.11, 4.12, 13.14**
    - _Requirements: 4.10, 4.11, 4.12, 4.13, 4.14, 13.14_

  - [x] 2.6 Implement `evidence/gaps.py`
    - `IntervalSet`, `merge_ranges`, `union_size`, `reconcile`, `derive_drop_range`, `SeqState`, `SeqEvents`, `classify` (new, duplicate, reset; first `seq > 0` yields `[0, seq-1]`; anomalies do not move the baseline), `missing_sample_count` (sequence discontinuities only), `unreconciled_drops` (design pseudocode).
    - Files: `coroner/evidence/gaps.py`.
    - Expected: the design's counting rules hold: discontinuities only for Missing_Sample_Count; drops, silence never counted there.
    - Verify: `python -m pytest tests/unit/evidence -q -k "prop_seq or prop_drop or prop_gap"` (written in 2.7 to 2.9).
    - Done when: module implemented and covered by 2.7 to 2.9.
    - _Requirements: 6.1, 6.3, 6.4, 6.8, 6.9, 6.10, 6.11, 6.12, 6.13_

  - [x] 2.7 Write Property 10 test (sequence anomalies classified exactly and separately)
    - **Property 10: Sequence anomalies are classified exactly and separately** over arrival-order lists on a small integer domain. Include the example with one missing, one duplicate, and one reset (Requirement 13.3).
    - Files: `tests/unit/evidence/test_prop_seq.py`.
    - Verify: `python -m pytest tests/unit/evidence/test_prop_seq.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 6.1, 6.4, 13.3**
    - _Requirements: 6.1, 6.4, 13.3_

  - [x] 2.8 Write Property 11 test (drop range derivation and reconciliation fields)
    - **Property 11: Probe-reported drop ranges and reconciliation fields agree with the ranges**, including the rule that drop records never carry `missing_count`.
    - Files: `tests/unit/evidence/test_prop_drop.py`.
    - Verify: `python -m pytest tests/unit/evidence/test_prop_drop.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 6.3, 6.8, 6.9, 6.10, 6.11**
    - _Requirements: 6.3, 6.8, 6.9, 6.10, 6.11_

  - [x] 2.9 Write Property 12 test (gap accounting) and the five worked examples
    - **Property 12: Gap accounting counts each number once, from discontinuities only**, checked against a brute-force set union. Example tests (a) to (e) from the design: overlap, partial overlap, unknown range, silence adds nothing, duplicated discontinuity counts once.
    - Files: `tests/unit/evidence/test_prop_gap_accounting.py`.
    - Verify: `python -m pytest tests/unit/evidence/test_prop_gap_accounting.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 6.11, 6.12, 6.13, 13.12**
    - _Requirements: 6.11, 6.12, 6.13, 13.12_

  - [x] 2.10 (PRIORITY) Implement `evidence/repair.py`
    - `TailInspection`, `inspect_tail` (backward scan in 64 KiB chunks), `repair_file` (append-only: isolating newline plus `repair` record with `fragment_b64` capped at 1 MiB, `fragment_len`, `file`, `line_no`, `reason`, `action`; one durable append; idempotent; never truncates).
    - Recovery responsibility (approved 14 November 2026, design D26): this task owns incomplete JSONL-tail repair and the repair-before-append requirement. After an `OSError` from an append, or when a startup scan finds an incomplete or invalid last line, `repair_file` must complete durably (isolating newline plus Repair_Record, at the configured durability level) before the run accepts another append. Tests show that an append after a completed repair starts on a fresh line, that a partially written repair is repaired again by a second run (the partial repair record becomes another Malformed_Line and no byte is lost), and that repair never truncates. Which run is dirty (the gate) belongs to 3.2, and the replay of missing gap records belongs to 3.5. No recovery code beyond tail repair is written here.
    - Files: `coroner/evidence/repair.py`.
    - Expected: original bytes remain an unmodified prefix; a second repair changes nothing.
    - Verify: covered by tests in 2.14 to 2.16; quick check `python -m pytest tests/unit/evidence/test_repair_reader_examples.py -q`.
    - Done when: module implemented and covered by 2.14 to 2.16.
    - _Requirements: 5.5, 5.6, 5.7, 5.8_

  - [x] 2.11 (PRIORITY) Implement `evidence/reader.py` (`read_evidence`)
    - Binary read, split on `\n`, strict UTF-8, `json.loads`, `ReadError` kinds (`invalid_utf8`, `invalid_json`, `not_an_object`, `incomplete_last_line`, `invalid_record`, `isolated_by_repair`), quarantine, `has_repair_record` link, `line_numbers`, accepts `Path` or `bytes`, never modifies the file. Lines isolated by a `repair` record with `appended_newline` are always quarantined (D19).
    - Files: `coroner/evidence/reader.py`.
    - Verify: covered by 2.13, 2.14, 2.16.
    - Done when: module implemented and covered by those tests.
    - _Requirements: 5.9, 5.10, 5.11_

  - [x] 2.12 Implement `summarize` and `RunSummary` in `reader.py`
    - Compute `sample_count` from valid `sample` records only, `missing_sample_count`, `unreconciled_drop_count` with `unreconciled_drop_uncertain`, `gap_counts_by_type`, `silence_intervals` (`sample_count: "unknown"`), `duplicate_count`, `reset_count`, `rejection_count`, `malformed_line_count`, `last_sample_time` or `"none received"`, `termination_cause` (`unknown` without a record), `unobserved_interval_note`, `durability_levels`, and a warning when a stored `reconciliation` field disagrees with the ranges.
    - Files: `coroner/evidence/reader.py`.
    - Verify: covered by 2.17.
    - Done when: module implemented and covered by 2.17.
    - _Requirements: 6.5, 6.6, 6.7, 6.11, 6.12, 6.13, 12.5_

  - [x] 2.13 Write Property 1 test (record framing and round trip)
    - **Property 1: Record framing and round trip** with 0 to 1,000 records containing newlines, U+2028, non-ASCII; uses a temporary directory and an `fsync` double.
    - Files: `tests/unit/evidence/test_prop_roundtrip.py`.
    - Verify: `python -m pytest tests/unit/evidence/test_prop_roundtrip.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 4.6, 5.4, 5.9, 12.2, 12.3, 13.2**
    - _Requirements: 4.6, 5.4, 5.9, 12.2, 12.3, 13.2_

  - [x] 2.14 Write Property 2 test (reader quarantine) and repair/reader example tests
    - **Property 2: Reader quarantines bad lines and keeps going**.
    - Examples (Requirement 13.13): incomplete last line preserved in the `repair` record with length, file, and line number; repair persisted before the next append; new records start on a fresh line; Malformed_Line reported with file, line number, byte length; excluded from Sample counts; a fragment that is valid JSON is still quarantined (D19); the file is never truncated; the `repair` record is not counted as a Sample.
    - Files: `tests/unit/evidence/test_prop_reader_quarantine.py`, `tests/unit/evidence/test_repair_reader_examples.py`.
    - Verify: `python -m pytest tests/unit/evidence/test_prop_reader_quarantine.py tests/unit/evidence/test_repair_reader_examples.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 5.10, 13.13**
    - _Requirements: 5.5, 5.7, 5.10, 13.1, 13.13_

  - [x] 2.15 Write Property 8 test (repair never loses or alters bytes)
    - **Property 8: Repair never loses or alters bytes** over arbitrary `bytes` tails, using an `fsync` double that records ordering (repair persisted before the next append).
    - Files: `tests/unit/evidence/test_prop_repair_bytes.py`.
    - Verify: `python -m pytest tests/unit/evidence/test_prop_repair_bytes.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 5.5, 5.6, 5.7, 5.8, 13.13**
    - _Requirements: 5.5, 5.6, 5.7, 5.8, 13.13_

  - [x] 2.16 Write Property 9 test (repair idempotent and file stays readable)
    - **Property 9: Repair is idempotent and keeps the file readable**.
    - Files: `tests/unit/evidence/test_prop_repair_idempotent.py`.
    - Verify: `python -m pytest tests/unit/evidence/test_prop_repair_idempotent.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 5.5, 5.10, 13.13**
    - _Requirements: 5.5, 5.10, 13.13_

  - [x] 2.17 Write Property 13 test (run summary consistent with records)
    - **Property 13: Run summary is consistent with the records**, including `none received`, unknown termination cause, and the unobserved-interval note.
    - Files: `tests/unit/evidence/test_prop_summary.py`.
    - Verify: `python -m pytest tests/unit/evidence/test_prop_summary.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 6.5, 6.6, 6.7, 12.5**
    - _Requirements: 6.5, 6.6, 6.7, 12.5_

  - [x] 2.18 Checkpoint - M0.2 complete
    - Run the default unit tests (`python -m pytest -q`; expected exit code 0 and a passed count not lower than the previous checkpoint, exit code 5 is a FAILURE), record real counts, confirm Properties 1, 2, 6, 8, 9, 10, 11, 12, 13 passed, and ask the user if questions arise. Then propose (do not perform) a tested milestone commit per Execution Rule 9: show the branch, git status --short, the specific paths, and a draft message, and wait for explicit approval; no commit or push without it. Confirm that task 2.19 (payload contracts of the six non-sample record types) is complete or that the user explicitly deferred it. Target date: 15 November 2026.

  - [x] 2.19 Follow-up (tracked): define and validate the payload contracts of the six non-sample record types
    - Gap, approved by the user on 14 November 2026 (with tasks 2.2 to 2.4): `validate_record` checks only the envelope (`record_type`, `schema_version`, `run_id`, `recv_time`) for `run_metadata`, `gap`, `sequence_anomaly`, `rejection`, `repair`, and `termination`. Full validation exists for `sample` records only, so `EvidenceWriter.append` cannot guarantee those six payloads. No payload schema has been invented for them.
    - Before any component relies on those records (the Collector service 3.5, the termination endpoint, the runner), STOP and ask the user to approve a payload contract for each type, taken from the design's "Data Models" examples. Do not invent fields, types, or ranges. After approval, add one validator per type to `coroner/evidence/schema.py`, extend `validate_record` to call them, and keep validation non-raising and deterministic (sorted issue order).
    - Tests: `tests/unit/evidence/test_schema_payloads.py` with, per type, the design example (valid), each missing field, wrong type, out-of-range value, and unknown field, plus the sequence of gap types.
    - Until this task is complete, every document that mentions record validation must keep stating that these six payloads are envelope-validated only.
    - Files: `coroner/evidence/schema.py`, `tests/unit/evidence/test_schema_payloads.py`, `docs/m0-results/m0.2-evidence-library.md`.
    - Expected: each of the six types is rejected with a specific `field` and `reason_code` when its payload is wrong.
    - Verify: `python -m pytest tests/unit/evidence/test_schema_payloads.py tests/unit/evidence/test_schema.py -q`.
    - Done when: the user approved the contracts, tests pass with output recorded, and the writer, repair, and gap tests still pass (default suite exit code 0).
    - _Requirements: 5.5, 6.8, 12.2_
- [x] 3. M0.3 Independent collector, ingestion API, storage health checks, and persistence tests
  - [x] 3.1 Implement `collector/config.py` with tests
    - `CollectorConfig`, `load_config(env)`: durability via `parse_durability` (invalid value exits non-zero with a message naming it), `CORONER_MAX_BODY_BYTES` (default 65536), `CORONER_WATCHDOG_TICK_MS` (default 50), evidence directory `/data/evidence` (overridable for tests).
    - Files: `coroner/collector/config.py`, `tests/unit/collector/test_config.py`.
    - Verify: `python -m pytest tests/unit/collector/test_config.py -q`.
    - Done when: passes with output recorded.
    - _Requirements: 4.10, 4.11, 4.12_

  - [x] 3.2 Implement `collector/state.py` (RunState, RunRegistry)
    - Per-run `threading.Lock`, lazy state load by scanning the run file (highest seq, seen IntervalSet, last sample times, interval, next gap number, `silence_open`, `terminated`, `dirty`), `open_run` (ensure file, run repair, write `run_metadata` `created` or `resumed` with real durability and `fsync_before_ack`), `mark_dirty`, `apply` (state changes only after a durable write), `.collector.lock` flock on POSIX (no-op on win32).
    - Recovery responsibility (approved 14 November 2026, design D26): this task owns Collector startup storage verification, directory durability, and recovery gating. At startup, before serving: create the evidence directory if it is missing, verify it is a writable directory, run the repair scan (2.10) over every `*.jsonl`, then `fsync` the evidence directory itself, after every restart as well, so a file created by an earlier process or by a failed first append has a durable directory entry (a documented no-op on win32). `mark_dirty` blocks `open_run` and every append for that run until recovery completes in this order: tail repair (2.10), replay of persisted Samples to regenerate missing gap records (3.5), then a rebuild of the in-memory run state from the file. `next_gap_number` is rebuilt as 1 plus the highest existing valid gap identifier (`gap-` followed by ASCII digits, in a valid gap record), and an identifier is never reused. Final file permissions are reviewed in task 5.6.
    - Files: `coroner/collector/state.py`.
    - Verify: covered by 3.6 to 3.8 and 3.13.
    - Done when: module implemented and covered by those tests.
    - _Requirements: 4.16, 5.2, 5.3, 5.5_

  - [x] 3.3 (PRIORITY) Implement `collector/storage_probe.py`
    - Real write, flush, fsync, directory fsync (when created), read-back, and compare under `<evidence>/.coroner_probe/probe.tmp`; same injected `fsync` and `fsync_dir` as the writer; 1 s result cache; invalidated by a failed append; takes no run lock and never touches run files; also run once at startup.
    - Files: `coroner/collector/storage_probe.py`.
    - Verify: covered by 3.4.
    - Done when: module implemented and covered by 3.4.
    - _Requirements: 4.4, 4.17, 4.18_

  - [x] 3.4 Write Property 28 test, Collector half (`storage_ok` requires a successful probe)
    - **Property 28: Health readiness requires a successful storage probe**, collector side: every probe step failing alone and in combinations yields `storage_ok: false` with HTTP 200; all steps passing yields true; run files stay byte-identical; a failed ingest invalidates the cache.
    - Files: `tests/unit/collector/test_prop_storage_ok.py`.
    - Verify: `python -m pytest tests/unit/collector/test_prop_storage_ok.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 4.4, 4.17, 4.18**
    - _Requirements: 4.4, 4.17, 4.18_

  - [x] 3.5 (PRIORITY) Implement `collector/service.py` (IngestService)
    - Ordering: size check, parse, validate, per-run lock, `open_run`, `classify`, `build_records` (sample or `sequence_anomaly`, discontinuity gap, drop gap with `range_status`, reconciliation marking, one write), `writer.append`, then `apply`, then acknowledge. `OSError` leads to `mark_dirty`, 503, no acknowledgement, in-memory state unchanged. Rejections go to the run file for a valid run id, otherwise `_rejections.jsonl` (`rejection_recorded: false` if that write fails). Termination recording. Log `ingest_ms`.
    - Recovery responsibility (approved 14 November 2026, design D26): this task owns recovery of missing gap records by replaying persisted Samples. After tail repair, replay the valid `sample` records in file order through `classify` and `apply` (anomalies are not needed to rebuild the baseline) to compute the expected sequence discontinuity and Probe-reported drop gaps, compare them with the stored gap records by semantic identity, and append only the missing ones in one durable append before the run accepts new Samples. Semantic identity, never `gap_id` or times alone: for a discontinuity `gap_type`, `detected_at_seq`, `first_missing_seq`, `last_missing_seq`; for a drop `gap_type`, `detected_at_seq`, `dropped_count`, `range_status`, `first_missing_seq`, `last_missing_seq`. A stored gap with the same `gap_type` and `detected_at_seq` but different remaining fields is a conflict: report it, add no second record, overwrite nothing. Silence-timeout gaps are time-based and are not regenerated. New gap ids come from the `next_gap_number` rebuilt in 3.2. Keep the existing batch order (the Sample or anomaly record first, then its gap records); do not write gap records first. Late Samples keep their `reset` classification and stay counted as missing. Recovery is idempotent: a second replay adds nothing.
    - Files: `coroner/collector/service.py`.
    - Verify: covered by 3.6 to 3.8.
    - Done when: module implemented and covered by those tests.
    - _Requirements: 4.2, 4.3, 4.5, 4.7, 4.8, 4.9, 4.15, 6.1, 6.3, 6.4, 7.5, 7.7_

  - [x] 3.6 Write Properties 3 and 5 tests (stored exactly as received; per-run isolation)
    - **Property 3: Valid Samples are stored exactly as received**; **Property 5: Run files are isolated by Run_Identifier**.
    - Files: `tests/unit/collector/test_prop_ingest_stored.py`, `tests/unit/collector/test_prop_isolation.py`.
    - Verify: `python -m pytest tests/unit/collector/test_prop_ingest_stored.py tests/unit/collector/test_prop_isolation.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 4.2, 4.7, 5.2, 7.5, 12.1**
    - _Requirements: 4.2, 4.7, 5.2, 7.5, 12.1_

  - [x] 3.7 Write Property 4 test (invalid input rejected, never stored as a Sample)
    - **Property 4: Invalid input is rejected and never stored as a Sample** covering unparseable body, missing or wrong-typed fields, bad run ids, missing or null-without-reason RSS and loop delay.
    - Files: `tests/unit/collector/test_prop_rejection.py`.
    - Verify: `python -m pytest tests/unit/collector/test_prop_rejection.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 4.5, 4.9, 7.7**
    - _Requirements: 4.5, 4.9, 7.7_

  - [x] 3.8 Write Property 7 test (persistence precedes acknowledgement) and ordering tests
    - **Property 7: Persistence precedes acknowledgement at the claimed level** using a recording writer and `fsync` doubles: write, flush, fsync (file, then directory on creation) occur before the acknowledgement; process level never fsyncs; `durability` equals `crash` only if fsync ran; a write error produces no acknowledgement and unchanged state; dirty files are repaired before the next append.
    - Files: `tests/unit/collector/test_prop_ack_after_persist.py`.
    - Verify: `python -m pytest tests/unit/collector/test_prop_ack_after_persist.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 4.3, 4.8, 4.13, 4.14, 4.15, 4.16, 5.7, 13.14**
    - _Requirements: 4.3, 4.8, 4.13, 4.14, 4.15, 4.16, 5.7, 13.14_

  - [x] 3.9 Implement `collector/watchdog.py` (SilenceWatchdog)
    - Daemon thread, injectable clock and tick; only runs that received a Sample in this process lifetime; threshold `3 * interval_ms`; re-checks under the run lock; `silence_open` set only after a durable write; cleared by the next Sample; stops after `termination`.
    - Files: `coroner/collector/watchdog.py`.
    - Verify: covered by 3.10.
    - Done when: module implemented and covered by 3.10.
    - _Requirements: 6.2, 6.13_

  - [x] 3.10 Write Property 14 test (one silence gap per silence period)
    - **Property 14: Silence watchdog emits one gap per silence period** with an injected clock, no sleeping.
    - Files: `tests/unit/collector/test_prop_watchdog.py`.
    - Verify: `python -m pytest tests/unit/collector/test_prop_watchdog.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 6.2, 6.13**
    - _Requirements: 6.2, 6.13_

  - [x] 3.11 (PRIORITY) Implement `collector/app.py` and `collector/main.py` (HTTP API and startup)
    - Routes: `GET /healthz` (always 200; body `status`, `durability`, `server_time`, `uptime_s`, `storage_ok`, `storage_error` on failure), `POST /v1/samples` (200/422/413/503), `POST /v1/runs/{run_id}/termination`, minimal read-only `GET /v1/runs/{run_id}/evidence` (`application/x-ndjson`, 404 unknown, 422 invalid id). Startup order: config, lock, storage probe, repair scan of every `*.jsonl`, watchdog, serve. One uvicorn worker, sync handlers.
    - Files: `coroner/collector/app.py`, `coroner/collector/main.py`.
    - Verify: covered by 3.12 and 3.13.
    - Done when: module implemented and covered by those tests.
    - _Requirements: 4.1, 4.3, 4.4, 4.8, 4.17, 4.18, 5.1, 5.5_

  - [x] 3.12 Write Collector route tests (TestClient, Docker-free)
    - 200/422/413/503 paths with a failing writer double, health body with `storage_ok` true and false (HTTP 200 in both), termination record, evidence 404/422/200, `_rejections.jsonl` for an unattributable rejection, no acknowledgement on write failure, process keeps serving.
    - Files: `tests/unit/collector/test_routes.py`.
    - Verify: `python -m pytest tests/unit/collector/test_routes.py -q`.
    - Done when: passes with output recorded.
    - _Requirements: 4.5, 4.8, 4.9, 4.17, 4.18, 13.1_

  - [x] 3.13 Write Collector persistence tests (Docker-free)
    - Simulate a Collector restart by building a new registry and app over the same temporary directory: acknowledged records are unchanged, not duplicated; `resumed` metadata appended; an incomplete last line is repaired at startup before accepting new appends; Process and Crash durability reported as configured; stopped Demo_App (no Samples) leaves records untouched.
    - Files: `tests/unit/collector/test_persistence.py`.
    - Verify: `python -m pytest tests/unit/collector/test_persistence.py -q`.
    - Done when: passes with output recorded.
    - _Requirements: 4.4, 4.16, 5.3, 5.5, 5.7_

  - [ ]* 3.14 Polish the evidence endpoint (deferrable)
    - Optional pagination, range requests, and content-type refinements for `GET /v1/runs/{run_id}/evidence`. The minimal route from 3.11 already satisfies M0. Skip first if the plan is tight.
    - Files: `coroner/collector/app.py`, `tests/unit/collector/test_evidence_endpoint.py`.
    - Verify: `python -m pytest tests/unit/collector -q`.
    - Done when: tests pass with output recorded.
    - _Requirements: 5.11, 12.5_

  - [x] 3.15 Checkpoint - M0.3 complete
    - Run the default unit tests (`python -m pytest -q`; expected exit code 0 and a passed count not lower than the previous checkpoint, exit code 5 is a FAILURE), record real counts, confirm Properties 3, 4, 5, 7, 14, and the Collector half of 28 passed, and ask the user if questions arise. Then propose (do not perform) a tested milestone commit per Execution Rule 9: show the branch, git status --short, the specific paths, and a draft message, and wait for explicit approval; no commit or push without it. Target date: 16 November 2026.

- [x] 4. M0.4 Python telemetry probe and FastAPI demo application incl. controlled destructive endpoints and unit tests
  - [x] 4.1 Implement Probe config and real readers (`probe/probe.py`, part 1)
    - `ProbeConfig`, `load_config(env, warn)` (invalid `CORONER_SAMPLE_INTERVAL_MS` gives 500 and exactly one warning; default run id `manual-<8 hex>`), `read_rss_bytes()` from `/proc/self/statm` times page size (current RSS, not `getrusage` peak; raises where `/proc` is missing), `read_container_id()` (mountinfo, then cgroup, then `HOSTNAME`, with `container_id_source`). Stdlib only. No synthetic fallback.
    - Files: `coroner/probe/probe.py`, `coroner/probe/__init__.py`.
    - Verify: covered by 4.4.
    - Done when: implemented and covered by 4.4.
    - _Requirements: 2.3, 2.6, 2.8, 2.11, 7.1, 7.3, 7.6_

  - [x] 4.2 Implement `build_sample` and the sampler loop (`probe/probe.py`, part 2)
    - Absolute deadlines (`t0 + k*interval`), Event_Loop_Delay `max(0, loop.time() - deadline)`, skip-forward without bursting, `seq` +1 per produced Sample, null fields carry reasons (1 to 200 characters) with no substitution, `request_count` via `count_request()`, `state` property.
    - Files: `coroner/probe/probe.py`.
    - Verify: covered by 4.4, 4.5, 4.7.
    - Done when: implemented and covered by those tests.
    - _Requirements: 2.1, 2.2, 2.4, 2.5, 2.7, 2.9, 7.2, 7.6_

  - [x] 4.3 (PRIORITY) Implement the sender thread, transport, and dropped counter (`probe/probe.py`, part 3)
    - One daemon sender thread with a one-slot handoff (offer returns well under 10 ms), 200 ms hard deadline, only HTTP 200 counts as acknowledged, no queue, no retry, counter saturates at 4,294,967,295 and is reduced by the carried value only on acknowledgement. No `atexit`, no signal handler, no shutdown-dependent flush.
    - Files: `coroner/probe/probe.py`.
    - Verify: covered by 4.4, 4.6, 4.9.
    - Done when: implemented and covered by those tests.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x] 4.4 Write Probe unit tests (Docker-free, network-free)
    - Readers (fake `/proc` content), container-id source selection, null-with-reason, interval config boundary examples (99, 100, 60000, 60001, text), Event_Loop_Delay with a fake loop clock, sender deadline and busy handling with a fake `Transport`, delivery starts promptly, no real sockets.
    - Files: `tests/unit/probe/test_probe_units.py`.
    - Verify: `python -m pytest tests/unit/probe/test_probe_units.py -q`.
    - Done when: passes with output recorded.
    - _Requirements: 2.2, 2.3, 2.5, 2.6, 2.9, 2.11, 3.1, 3.5, 3.6, 13.1_

  - [x] 4.5 Write Property 15 test (Probe Samples well formed, never fabricated)
    - **Property 15: Probe Samples are well formed and never fabricated** with injected reader failures and values; each output validates against `evidence.schema.validate_sample`.
    - Files: `tests/unit/probe/test_prop_sample_wellformed.py`.
    - Verify: `python -m pytest tests/unit/probe/test_prop_sample_wellformed.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.9, 7.6**
    - _Requirements: 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.9, 7.6_

  - [x] 4.6 Write Property 18 test (dropped counter accounting)
    - **Property 18: Dropped counter accounting** over delivery-outcome lists (`ack`, `error`, `timeout`, `busy`), including saturation.
    - Files: `tests/unit/probe/test_prop_dropped_counter.py`.
    - Verify: `python -m pytest tests/unit/probe/test_prop_dropped_counter.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 3.3, 3.4, 3.5, 3.7**
    - _Requirements: 3.3, 3.4, 3.5, 3.7_

  - [ ]* 4.7 Write Property 16 test (sampling schedule does not drift) (deferrable)
    - **Property 16: Sampling schedule does not drift**. Schedule-drift fuzzing is deferrable; one example test is already in 4.4.
    - Files: `tests/unit/probe/test_prop_schedule.py`.
    - Verify: `python -m pytest tests/unit/probe/test_prop_schedule.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 2.1, 7.2**
    - _Requirements: 2.1, 7.2_

  - [ ]* 4.8 Write Property 17 test (sample interval configuration) (deferrable)
    - **Property 17: Sample interval configuration** over arbitrary text with the boundary values. Boundary examples are already in 4.4.
    - Files: `tests/unit/probe/test_prop_interval_config.py`.
    - Verify: `python -m pytest tests/unit/probe/test_prop_interval_config.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 2.8, 2.11**
    - _Requirements: 2.8, 2.11_

  - [x] 4.9 Write Probe static and size tests
    - Sum of non-blank lines over `coroner/probe/**/*.py` is at most 300; stdlib-only imports; no `random` in `probe` and `demo_app`; no `atexit` or `signal` use in the Probe; Probe output validates against `validate_sample` (drift guard).
    - Files: `tests/unit/probe/test_probe_static.py`.
    - Verify: `python -m pytest tests/unit/probe/test_probe_static.py -q`.
    - Done when: passes with output recorded and the measured line count is noted.
    - _Requirements: 3.2, 7.3, 14.4, 14.6_

  - [x] 4.10 Implement `demo_app/leak.py` and `demo_app/counting.py`
    - `LeakStore.retain(n)`: `b"\xa5" * n`, lock-protected list, total updated only after allocation succeeds. `RequestCountMiddleware`: pure ASGI, calls `probe.count_request()` on every HTTP request including 404s.
    - Files: `coroner/demo_app/leak.py`, `coroner/demo_app/counting.py`.
    - Verify: covered by 4.12 and 4.14.
    - Done when: implemented and covered by those tests.
    - _Requirements: 1.2, 1.3, 1.7, 2.4_

  - [x] 4.11 (PRIORITY) Implement `demo_app/app.py` and `demo_app/main.py`
    - `create_app(env, *, probe, exit_fn=os._exit)`. Routes: `GET /health` (`status`, `pid`, `run_id`, `probe.next_seq`, `probe.dropped`); `POST /leak?bytes=N` (1 to 104,857,600; 422; 503 `allocation_failed` on `MemoryError`); `POST /crash` calls `exit_fn(1)`; `GET /error` raises `RuntimeError`. The three destructive routes are registered only when the flag is exactly `1` (404 otherwise). Probe start in lifespan; no evidence depends on shutdown. Comments and strings call `/crash` a "controlled fatal process exit via os._exit(1)", never an exception.
    - Files: `coroner/demo_app/app.py`, `coroner/demo_app/main.py`.
    - Verify: covered by 4.12 to 4.14.
    - Done when: implemented and covered by those tests.
    - _Requirements: 1.1, 1.5, 1.6, 1.8, 1.9, 1.10, 1.11, 9.12, 15.6_

  - [x] 4.12 Write Property 19 test (leak accumulates exactly or changes nothing)
    - **Property 19: Leak endpoint accumulates exactly or changes nothing**, including `MemoryError` injection and byte content check (non-zero).
    - Files: `tests/unit/demo_app/test_prop_leak.py`.
    - Verify: `python -m pytest tests/unit/demo_app/test_prop_leak.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 1.2, 1.3, 1.4, 1.7, 1.8**
    - _Requirements: 1.2, 1.3, 1.4, 1.7, 1.8_

  - [x] 4.13 Write Property 20 test (destructive endpoints routed only when flag is exactly 1)
    - **Property 20: Destructive endpoints are routed only when the flag is exactly 1** over arbitrary values (unset, empty, `0`, `true`, ` 1`); when not routed each endpoint returns 404 and `exit_fn` is never called.
    - Files: `tests/unit/demo_app/test_prop_gating.py`.
    - Verify: `python -m pytest tests/unit/demo_app/test_prop_gating.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 1.10, 1.11, 13.16, 15.6**
    - _Requirements: 1.10, 1.11, 13.17, 15.6_

  - [x] 4.14 Write Demo_App unit tests including the ordinary HTTP 500 in-process test
    - `/health` 200; `/crash` calls the injected `exit_fn(1)` and nothing else; `/error` returns 500, then `/health` returns 200 with the same `pid`, and fake-transport Samples keep the same `pid` with `seq` increasing by 1 (Requirements 13.15, 15); request counting includes 404s; wording scan: "exception" and "unhandled exception" never describe the crash endpoint.
    - Files: `tests/unit/demo_app/test_app.py`, `tests/unit/demo_app/test_http500_nontermination.py`.
    - Verify: `python -m pytest tests/unit/demo_app -q`.
    - Done when: passes with output recorded.
    - _Requirements: 1.5, 1.6, 1.9, 9.12, 13.1, 13.15, 15.1, 15.2, 15.4, 15.5_

  - [x] 4.15 Checkpoint - M0.4 complete
    - Run the default unit tests (`python -m pytest -q`; expected exit code 0 and a passed count not lower than the previous checkpoint, exit code 5 is a FAILURE), record real counts and the measured Probe line count, confirm Properties 15, 18, 19, 20 passed, and ask the user if questions arise. Then propose (do not perform) a tested milestone commit per Execution Rule 9: show the branch, git status --short, the specific paths, and a draft message, and wait for explicit approval; no commit or push without it. Target date: 17 November 2026.

- [x] 5. M0.5 Docker Compose environment with container isolation, health checks, persistent evidence volume
  - [x] 5.1 Write `docker/collector.Dockerfile` with non-root UID/GID 10001 and image-baked volume ownership
    - Base image is the pinned tag verified in task 1.4. Create group and user with UID/GID 10001 (`groupadd -g 10001`, `useradd -u 10001 -g 10001 --no-create-home`). Create `/data/evidence` in the image and `chown 10001:10001` it with mode `0750`, so a fresh named volume mounted there inherits that ownership on first use (Docker initialises an empty named volume from the image directory). Install `requirements/collector.txt` with `pip install --no-cache-dir -r` and then `pip check`. Copy only `coroner/common`, `coroner/evidence`, `coroner/collector`. `USER 10001:10001`. Exec-form `CMD` for one uvicorn worker. If the image-baked approach fails in task 5.6 (for example on Docker Desktop), STOP and ask before switching to an init step.
    - Files: `docker/collector.Dockerfile`, `.dockerignore`.
    - Expected: the file builds in 5.5.
    - Verify: static check in 5.4; build in 5.5.
    - Done when: file written with a pinned tag (no `latest`).
    - _Requirements: 4.1, 5.1, 8.9, 14.5_

  - [x] 5.2 Write `docker/demo_app.Dockerfile`
    - Same pinned base. Non-root user 10001:10001. Copy only `coroner/probe` and `coroner/demo_app`. Install `requirements/demo_app.txt`, then `pip check`. Exec-form `CMD` runs `uvicorn --workers 1` so the server is PID 1, listening on `0.0.0.0:8080` inside the container.
    - Files: `docker/demo_app.Dockerfile`.
    - Verify: static check in 5.4; build in 5.5.
    - Done when: file written with a pinned tag (no `latest`).
    - _Requirements: 1.1, 1.6, 8.9, 8.13_

  - [x] 5.3 (PRIORITY) Write `compose.yaml`
    - Follow the design sketch: `name: coroner`; collector with `127.0.0.1:${CORONER_COLLECTOR_PORT:-8081}:8081`, `coroner_evidence:/data/evidence`, `restart: on-failure:5`, `user: "10001:10001"`, `cap_drop: [ALL]`, `no-new-privileges`, healthcheck requiring HTTP 200 and `storage_ok is True`; demo_app with `depends_on: collector: condition: service_healthy`, `mem_limit` and `memswap_limit` from `${CORONER_DEMO_MEM_LIMIT:-128m}`, `restart: "no"`, `privileged: false`, `read_only: true`, tmpfs `/tmp`, loopback port, flag default `0`, no volumes, no bind mounts. Use `python -c` healthchecks (no curl).
    - Files: `compose.yaml`.
    - Verify: `docker compose config` (parse check only, no containers started).
    - Done when: `docker compose config` output recorded and shows the expected values.
    - _Requirements: 8.1, 8.2, 8.3, 8.5, 8.6, 8.7, 8.9, 8.10, 8.11, 8.12, 8.13_

  - [x] 5.4 Write static Compose and Dockerfile tests (Docker-free)
    - Parse `compose.yaml` and the Dockerfiles with PyYAML and text parsing: pinned tags (no `latest`, no untagged), loopback-only ports, restart policies, no host bind mounts, `depends_on` health condition, default flag value `0`, Collector healthcheck command checks both the HTTP status and `storage_ok`, user 10001, `cap_drop`, memory limit variable default `128m`, volume mounted only in the collector.
    - Files: `tests/unit/test_compose_static.py`.
    - Verify: `python -m pytest tests/unit/test_compose_static.py -q`.
    - Done when: passes with output recorded. These tests do not call Docker.
    - _Requirements: 8.3, 8.5, 8.6, 8.7, 8.9, 8.10, 8.11, 8.13, 13.8_

  - [x] 5.5 Build both images and verify dependencies inside the images
    - `docker compose build`, then run `pip check` and `pip freeze` inside each built image and compare the frozen versions with `requirements/*.txt`. Resolve any mismatch (STOP and ask if a pin must change). Record image ids and the digest of the base image actually used.
    - Files: `docs/m0-results/dependency-verification.md` (append the image-build section).
    - Verify: `docker compose build; docker run --rm --entrypoint python coroner-collector:m0 -m pip check; docker run --rm --entrypoint python coroner-demo-app:m0 -m pip check; docker run --rm --entrypoint python coroner-collector:m0 -m pip freeze`.
    - Done when: real build output, `pip check` output (expected "No broken requirements found."), and freeze comparison are recorded.
    - _Requirements: 14.5, 8.9_

  - [x] 5.6 (PRIORITY) Verify named-volume ownership and real persistence under UID/GID 10001
    - Write a `docker_integration` test and also run it manually: (1) stop the stack with `docker compose down` (no `-v`) and check whether the project volume exists; if a fresh volume is needed, remove only the named Coroner volume by following Execution Rule 11 (verify name and label, list contents and count run files, archive evidence to `runs/archive/`, obtain the user's explicit approval); otherwise use a uniquely named test volume; (2) start the Collector; (3) `docker compose exec collector id` shows `uid=10001 gid=10001`; (4) `stat -c '%u:%g %a' /data/evidence` shows `10001:10001`; (5) healthz returns `storage_ok: true` (a real write plus fsync probe as non-root); (6) POST a valid Sample, confirm a `.jsonl` file exists and holds the record; (7) `docker compose restart collector`, wait for health, read the evidence back through `GET /v1/runs/{id}/evidence` and confirm the record is unchanged and not duplicated. Also confirm a read-only root filesystem does not break writes to the volume.
    - Files: `tests/docker_integration/test_volume_persistence.py`, `docs/m0-results/volume-ownership.md`.
    - Expected: ownership and persistence work without root.
    - Verify: `python -m pytest tests/docker_integration/test_volume_persistence.py --run-docker -q`; manual: `docker compose exec collector id; docker compose exec collector stat -c '%u:%g %a' /data/evidence`.
    - Done when: real command output and test results are recorded. If the volume is not writable by 10001, STOP, record, and ask which fix (image-baked directory vs init step) to apply.
    - _Requirements: 4.3, 4.13, 5.1, 5.3, 8.12_

  - [x] 5.7 (PRIORITY) Verify Compose isolation and health behavior
    - `docker_integration` tests plus recorded manual checks: `docker compose up -d --build` reaches running within 60 s; ports are bound to `127.0.0.1` only (`docker compose port`, `docker inspect` `NetworkSettings.Ports`); Collector healthz body has `storage_ok: true`; Demo_App `HostConfig.Memory` equals 134217728 and `MemorySwap` equals `Memory`; Demo_App has no mounts and is not privileged; restart policies are as designed; with the flag unset, `/crash`, `/leak`, and `/error` return 404; an invalid `CORONER_DEMO_MEM_LIMIT` makes Docker reject the Demo_App and leaves the Collector and volume untouched.
    - Files: `tests/docker_integration/test_compose_isolation.py`, `docs/m0-results/compose-checks.md`.
    - Verify: `python -m pytest tests/docker_integration/test_compose_isolation.py --run-docker -q`; `docker compose ps`; `docker inspect <id> --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.Privileged}} {{json .Mounts}}'`.
    - Done when: real output recorded, `docker compose down` run afterwards (without `-v`).
    - _Requirements: 8.2, 8.3, 8.4, 8.5, 8.6, 8.8, 8.10, 8.11, 8.12, 8.13, 1.10_

  - [x] 5.8 Checkpoint - M0.5 complete
    - Run the default unit tests (`python -m pytest -q`; expected exit code 0 and a passed count not lower than the previous checkpoint, exit code 5 is a FAILURE), record real counts, confirm 5.5 to 5.7 recorded real Docker output, and ask the user if questions arise. Then propose (do not perform) a tested milestone commit per Execution Rule 9: show the branch, git status --short, the specific paths, and a draft message, and wait for explicit approval; no commit or push without it. Target date: 18 November 2026.

- [x] 6. M0.6 Windows-compatible experiment runner, run manifests, Docker inspection and failure evaluation
  - [x] 6.1 (PRIORITY) Implement `runner/docker_port.py` (`DockerPort`, `DockerCli`, `build_child_env`)
    - Every call is `subprocess.run(argv_list, env=build_child_env(os.environ, overrides), capture_output=True, timeout=..., check=False)`: no `shell=True`, no inline `VAR=value`, `docker_exe` from `shutil.which("docker")`, injectable `run`. Methods: `compose_up` (`-p coroner up -d --no-deps --force-recreate demo_app`), `container_id` (`ps -a -q demo_app`, must be 64 hex), `inspect` (raw bytes, parsed JSON, UTC inspect time), `kill`, `available`. Never mutates `os.environ`.
    - Files: `coroner/runner/docker_port.py`.
    - Verify: covered by 6.2.
    - Done when: implemented and covered by 6.2.
    - _Requirements: 16.1, 16.2, 16.3_

  - [x] 6.2 Write Property 29 test and runner static scan
    - **Property 29: Runner child process environment and argv** with a fake `run`; plus a static scan that `coroner/runner` has no `shell=True`, `bash`, `os.system`, `fcntl`, and builds paths with `pathlib`.
    - Files: `tests/unit/runner/test_prop_env_argv.py`, `tests/unit/runner/test_runner_static.py`.
    - Verify: `python -m pytest tests/unit/runner/test_prop_env_argv.py tests/unit/runner/test_runner_static.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 16.1, 16.2**
    - _Requirements: 16.1, 16.2, 16.3_

  - [x] 6.3 Implement `runner/evaluate.py` part 1 (`parse_mem_size` and container-state evaluation)
    - `parse_mem_size` (reuse `common.envparse`), `ContainerState`, `Evaluation`, `evaluate_container_state(experiment, state)` with expected pairs A `(1, false)`, B `(137, false)`, C `(137, true)` and status `exited`; failure messages include observed values.
    - Files: `coroner/runner/evaluate.py`.
    - Verify: covered by 6.4.
    - Done when: implemented and covered by 6.4.
    - _Requirements: 8.4, 9.6, 9.10, 9.11, 10.4, 10.6, 10.9, 11.5, 11.6_

  - [x] 6.4 Write Properties 23 and 21 tests (memory size parsing; container state evaluation)
    - **Property 23: Memory size parsing**; **Property 21: Container state evaluation**.
    - Files: `tests/unit/runner/test_prop_memsize.py`, `tests/unit/runner/test_prop_container_state.py`.
    - Verify: `python -m pytest tests/unit/runner/test_prop_memsize.py tests/unit/runner/test_prop_container_state.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 8.3, 8.4, 9.6, 9.10, 9.11, 10.4, 10.6, 10.9, 11.5, 11.6, 11.12, 11.15**
    - _Requirements: 8.3, 8.4, 9.6, 9.10, 9.11, 10.4, 10.6, 10.9, 11.5, 11.6_

  - [x] 6.5 Implement `evaluate.py` part 2 (Experiment B timing)
    - `SampleView`, `BTiming`, `classify_b_samples(samples, sigkill_sent, finished_at, tolerance_ms=250, clock_offset_ms=0)` and `evaluate_experiment_b`. Constant `CLOCK_TOLERANCE_MS = 250` (never above 250; do not change it without asking the user). Classification by offset-corrected Capture_Time only; `pre_kill_late_received` counted and valid; `after_container_stop` flagged independently.
    - Files: `coroner/runner/evaluate.py`.
    - Verify: covered by 6.6.
    - Done when: implemented and covered by 6.6.
    - _Requirements: 10.8, 10.10, 10.11, 10.12, 10.13_

  - [x] 6.6 Write Property 27 test and Experiment B timing examples
    - **Property 27: Experiment B Sample timing classification**. Examples: pre-kill capture received after the kill accepted and counted; no pre-kill Sample fails; capture at kill + 250 ms within tolerance; kill + 251 ms fails and is listed; capture after `FinishedAt` reported as an anomaly; Receive_Time never changes the category.
    - Files: `tests/unit/runner/test_prop_b_timing.py`.
    - Verify: `python -m pytest tests/unit/runner/test_prop_b_timing.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 10.8, 10.10, 10.11, 10.12, 10.13**
    - _Requirements: 10.8, 10.10, 10.11, 10.12, 10.13, 13.16_

  - [x] 6.7 Implement `evaluate.py` part 3 (Experiment C verdict and diagnostics)
    - `diagnostic_metrics` (`rss_monotonic_tail` over the last min(5, n) Samples, `peak_rss_ratio_of_limit`, `sample_count`), `evaluate_experiment_c` using only the six mandatory checks (the verdict function never reads Diagnostic_Metrics), `sampling_limitations` builder (always the three statements). "First Sample" is the first with non-null RSS.
    - Files: `coroner/runner/evaluate.py`.
    - Verify: covered by 6.8.
    - Done when: implemented and covered by 6.8.
    - _Requirements: 11.10, 11.11, 11.13, 11.14, 11.17, 11.18, 11.19, 11.20_

  - [x] 6.8 Write Properties 22 and 26 tests and Experiment C evaluation examples
    - **Property 22: Experiment C verdict depends only on mandatory conditions**; **Property 26: Diagnostic metrics and sampling limitations are recorded**. Examples: each mandatory condition failing alone fails (OOMKilled false, exit 138, health 200 with `storage_ok` false or absent, no pre-crash Sample, flat or all-null RSS, incomplete inspect record); non-monotonic tail and peak ratio 0.01 still pass; manifest holds `diagnostics` and `sampling_limitations` on pass and fail.
    - Files: `tests/unit/runner/test_prop_c_verdict.py`, `tests/unit/runner/test_prop_c_diagnostics.py`.
    - Verify: `python -m pytest tests/unit/runner/test_prop_c_verdict.py tests/unit/runner/test_prop_c_diagnostics.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 11.5, 11.6, 11.10, 11.11, 11.12, 11.13, 11.14, 11.17, 11.18, 11.19, 11.20**
    - _Requirements: 11.10, 11.11, 11.17, 11.18, 11.19, 11.20, 13.5, 13.16_

  - [x] 6.9 Implement `runner/manifest.py` and run id generation
    - Manifest dataclasses, atomic write (temp file, `os.fsync`, `os.replace`, binary mode with explicit `\n`), read, `as_posix()` for stored paths, inspect excerpt extraction (`Id`, `State.OOMKilled`, `State.ExitCode`, `State.Status`, `HostConfig.Memory`) with raw output saved unchanged, `generate_run_id(experiment, existing)` (`<exp>-<UTC yyyymmddThhmmssZ>-<8 hex>`, lowercase, matches `RUN_ID_RE`, unique across `runs/` and known ids).
    - Files: `coroner/runner/manifest.py`.
    - Verify: covered by 6.10.
    - Done when: implemented and covered by 6.10.
    - _Requirements: 9.1, 10.1, 11.1, 11.13, 11.14, 12.1, 12.2, 12.7_

  - [x] 6.10 Write Properties 24 and 25 tests (run id uniqueness; manifest and inspect fidelity)
    - **Property 24: Run identifiers are unique and valid**; **Property 25: Run_Manifest and inspect excerpt fidelity**.
    - Files: `tests/unit/runner/test_prop_run_id.py`, `tests/unit/runner/test_prop_manifest.py`.
    - Verify: `python -m pytest tests/unit/runner/test_prop_run_id.py tests/unit/runner/test_prop_manifest.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 9.1, 10.1, 11.1, 11.13, 11.14, 12.1, 12.2, 12.7**
    - _Requirements: 9.1, 10.1, 11.1, 11.13, 11.14, 12.1, 12.2, 12.7_

  - [x] 6.11 Implement `runner/collector_client.py` (health, evidence, termination) and clock offset estimation
    - Stdlib HTTP client with timeouts. `check_ready(client, clock)` accepts only HTTP 200 and `storage_ok is True`; 200 with `storage_ok` false, absent, or non-boolean aborts immediately with "Collector storage is not healthy"; timeout after 60 s gives `collector_unhealthy`. `estimate_clock_offset_ms(host_before, host_after, server_time)` (midpoint method) returned with the measurement window so the uncertainty (half the round trip) is also recorded.
    - Files: `coroner/runner/collector_client.py`.
    - Verify: covered by 6.12.
    - Done when: implemented and covered by 6.12.
    - _Requirements: 8.7, 8.8, 8.14, 9.8, 10.7, 11.10_

  - [x] 6.12 Write Property 28 runner-side test, readiness examples, and clock offset tests
    - **Property 28: Health readiness requires a successful storage probe**, runner side: arbitrary status codes and bodies; accepted iff status 200 and `storage_ok` exactly `true`. Readiness examples with a fake client and fake clock (200 true proceeds; 200 false and 200 absent abort and the Demo_App is never started; 503 or timeout path). Clock-offset examples with known synthetic times.
    - Files: `tests/unit/runner/test_prop_readiness.py`, `tests/unit/runner/test_clock_offset.py`.
    - Verify: `python -m pytest tests/unit/runner/test_prop_readiness.py tests/unit/runner/test_clock_offset.py -q`.
    - Done when: passes with output recorded.
    - **Validates: Requirements 8.7, 8.8, 8.14, 9.8, 10.7, 11.10**
    - _Requirements: 8.7, 8.8, 8.14, 13.16_

  - [x] 6.13 (PRIORITY) Implement `runner/experiments/base.py` (common preamble and shared checks)
    - Preamble: Docker availability (exit 2), run id, Collector readiness (60 s) with `durability` and `clock_offset_ms` recorded, start Demo_App with env overrides, container id (64 hex), wait for `/health`, wait for a first Sample, manifest created and rewritten atomically after each phase and on every failure (Requirement 12.7). Shared: leak driver, evidence fetch via the Collector, termination post, survival check, growth check (`growth >= 0.5 * cumulative_bytes` using the first Sample at least 1 s after the last leak and the last Sample before the first leak), `durability_matches_run_metadata` check, `cause_label` derived from the observed state.
    - Files: `coroner/runner/experiments/base.py`.
    - Verify: covered by 6.17.
    - Done when: implemented and covered by 6.17.
    - _Requirements: 4.16, 7.4, 8.7, 8.14, 9.1, 9.2, 12.1, 12.7_

  - [x] 6.14 (PRIORITY) Implement `runner/experiments/exp_a.py` (controlled fatal process exit)
    - Leak for at least 5 s (abort without crashing on a failed or over-5-s leak call), record `crash_request_time`, POST `/crash` (a dropped connection is the expected outcome), poll inspect up to 30 s, expect exit 1 and OOMKilled false, survival check within 5 s, at least one Sample before the crash request time (offset-corrected). Description string: "controlled fatal process exit via os._exit(1)". Never uses the word exception for it.
    - Files: `coroner/runner/experiments/exp_a.py`.
    - Verify: covered by 6.17.
    - Done when: implemented and covered by 6.17.
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12_

  - [x] 6.15 (PRIORITY) Implement `runner/experiments/exp_b.py` (SIGKILL)
    - Leak at least 5 s, record `sigkill_sent_time` immediately before `docker kill --signal=KILL <full id>`, poll inspect every 200 ms up to 10 s, expect exit 137 and OOMKilled false, record `State.FinishedAt`, classify Samples with `classify_b_samples`, survival check, `no_shutdown_handler_records` check.
    - Files: `coroner/runner/experiments/exp_b.py`.
    - Verify: covered by 6.17.
    - Done when: implemented and covered by 6.17.
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 10.10, 10.11, 10.12, 10.13_

  - [x] 6.16 (PRIORITY) Implement `runner/experiments/exp_c.py` (genuine Docker OOM kill)
    - No `kill`, no `/crash`, no simulated exit code. Wait up to 30 s for running; leak 4 MiB chunks (`--leak-chunk-bytes`) at intervals of at most 1 s until the container stops or 120 s elapse (on timeout: stop leaking, fail, leave the container running, record `container_left_running: true`); inspect within 5 s (poll 250 ms); save raw `docker_inspect.json`; record the five inspect values plus `inspect_time`; `memory_limit_bytes` equals `HostConfig.Memory`; `highest_rss_bytes`; evaluate with `evaluate_experiment_c`; always write `diagnostics` and `sampling_limitations`.
    - Files: `coroner/runner/experiments/exp_c.py`.
    - Verify: covered by 6.17.
    - Done when: implemented and covered by 6.17.
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9, 11.10, 11.11, 11.12, 11.13, 11.14, 11.17, 11.18, 11.19, 11.20_

  - [x] 6.17 Write scripted experiment-flow tests with fake DockerPort and fake Collector client
    - Cover every row of the design's Error Handling table that concerns the runner: Docker unavailable (exit 2, no manifest), collector unhealthy, storage not healthy (Demo_App never started), Demo_App not healthy in 30 s, leak failure aborts A and B without crash or kill, timeouts, wrong exit code, OOMKilled mismatch, inspect failure, 120 s timeout, evidence fetch failure, survival failure; a manifest is written on every failure; Experiment C never calls `kill` and never posts to `/crash`; wording rule for Experiment A (no "exception"); manifest field sets are identical across A, B, C records.
    - Files: `tests/unit/runner/test_experiment_flows.py`.
    - Verify: `python -m pytest tests/unit/runner/test_experiment_flows.py -q`.
    - Done when: passes with output recorded.
    - _Requirements: 9.2, 9.4, 9.7, 9.12, 10.5, 11.2, 11.7, 11.8, 11.9, 12.7, 13.16_

  - [x] 6.18 (PRIORITY) Implement `runner/cli.py` and `runner/summary.py` (including `clock-check`)
    - `python -m coroner.runner run a|b|c` (`--runs-dir`, `--mem-limit`, `--leak-chunk-bytes`), exit codes 0, 1, 2; `summary RUN_ID` prints sample count, gap count, last sample time, Container_State, unreconciled drops, malformed lines, durability, and (C only) diagnostics and limitations; Experiment B prints the timing classification; missing or unparseable run exits 2 and writes nothing. `clock-check` command: measure host-to-container offset (Collector `server_time` over N round trips) and print offset and uncertainty. Pre-validates `--mem-limit` with `parse_mem_size`.
    - Files: `coroner/runner/cli.py`, `coroner/runner/__main__.py`, `coroner/runner/summary.py`.
    - Verify: covered by 6.19.
    - Done when: implemented and covered by 6.19.
    - _Requirements: 8.4, 11.20, 12.5, 12.6, 16.1_

  - [x] 6.19 Write CLI and summary tests
    - Exit codes, argument handling, summary output for each experiment with fixture manifests and evidence, unknown run id exits 2 without modifying files, unparseable evidence exits 2, no network and no Docker.
    - Files: `tests/unit/runner/test_cli_summary.py`.
    - Verify: `python -m pytest tests/unit/runner/test_cli_summary.py -q`.
    - Done when: passes with output recorded.
    - _Requirements: 12.5, 12.6, 13.1_

  - [ ]* 6.20 CLI niceties and `--repeat K` comparison printout (deferrable)
    - `--repeat K` runs K independent runs and compares `exit_code` and `oom_killed` across manifests (exit 1 on mismatch); `summary --fetch` convenience; formatting polish. The manifest remains the source of truth.
    - Files: `coroner/runner/cli.py`, `tests/unit/runner/test_repeat.py`.
    - Verify: `python -m pytest tests/unit/runner/test_repeat.py -q`.
    - Done when: passes with output recorded.
    - _Requirements: 12.4_

  - [x] 6.21 Checkpoint - M0.6 complete
    - Run the default unit tests (`python -m pytest -q`; expected exit code 0 and a passed count not lower than the previous checkpoint, exit code 5 is a FAILURE), record real counts, confirm Properties 21 to 29 passed, and ask the user if questions arise. Then propose (do not perform) a tested milestone commit per Execution Rule 9: show the branch, git status --short, the specific paths, and a draft message, and wait for explicit approval; no commit or push without it. Target date: 19 November 2026.

- [x] 7. M0.7 Execute and validate Experiments A, B, C and the ordinary HTTP 500 non-termination test
  - [x] 7.1 (PRIORITY) Preflight: clean build, readiness, and host-vs-container clock offset measurement
    - `docker compose down` (no `-v`) and, only if a fresh volume is really required, the protected removal of Execution Rule 11 (verify the volume name `coroner_coroner_evidence` and label `com.docker.compose.project=coroner`, list contents and count run files, archive evidence to `runs/archive/<UTC timestamp>/`, obtain the user's explicit approval at that moment, remove only that volume), `docker compose build`, `docker compose up -d collector`. Run `python -m coroner.runner clock-check` (at least 20 round trips) and also compare `docker exec <collector> date -u +%s.%N` against the host UTC time to measure the offset between the host clock and the container (Docker Desktop VM) clock. Record offset, spread, and round-trip uncertainty. The documented 250 ms tolerance stays unchanged. If the measured offset plus uncertainty approaches or exceeds 250 ms, STOP, record, and ask the user; never change the constant silently. If testing shows a better-justified bound, document the measurement and propose it to the user.
    - Files: `docs/m0-results/clock-offset.md`.
    - Verify: `python -m coroner.runner clock-check; docker compose exec collector python -c "import time;print(time.time())"; Get-Date -AsUTC -UFormat %s`.
    - Done when: real measured values and the decision on the tolerance are recorded.
    - _Requirements: 10.12, 10.13, 14.12, 8.7_

  - [x] 7.2 Write and run the ordinary HTTP 500 non-termination integration test
    - `tests/docker_integration/test_ordinary_500.py` (non-destructive to the container): start with the flag set, record `pid` and `next_seq`, `GET /error` returns 500, `/health` returns 200 within 1 s with the same `pid`, Samples continue with the same `pid` and `seq` +1 per Sample, `docker inspect` shows `Status=running`.
    - Files: `tests/docker_integration/test_ordinary_500.py`, `docs/m0-results/experiment-results.md` (section).
    - Verify: `python -m pytest tests/docker_integration/test_ordinary_500.py --run-docker -q`.
    - Done when: real result recorded.
    - _Requirements: 13.4, 13.15, 15.1, 15.2, 15.3, 15.4, 15.5_

  - [x] 7.3 (PRIORITY) Write, execute, and validate Experiment A (fatal exit via `os._exit(1)`)
    - `tests/docker_integration/test_experiment_a.py` plus a real run: `python -m coroner.runner run a`. Assert exit code 1, OOMKilled false, at least one Sample before `crash_request_time`, Collector healthy within 5 s with `storage_ok=true`, manifest fields, and that the description never calls it an exception.
    - Files: `tests/docker_integration/test_experiment_a.py`, `docs/m0-results/experiment-results.md`.
    - Verify: `python -m coroner.runner run a; python -m pytest tests/docker_integration/test_experiment_a.py --run-docker -q; python -m coroner.runner summary <run_id>`.
    - Done when: the run id, exit code, OOMKilled, status, inspect time, health result, and Sample counts are copied from the real manifest and an independent `docker inspect` into the results note.
    - _Requirements: 9.1, 9.3, 9.5, 9.6, 9.8, 9.9, 9.10, 9.11, 9.12, 13.4_

  - [x] 7.4 (PRIORITY) Write, execute, and validate Experiment B (SIGKILL)
    - `tests/docker_integration/test_experiment_b.py` plus a real run: `python -m coroner.runner run b`. Assert exit code 137, OOMKilled false, at least one Sample with Capture_Time before `sigkill_sent_time`, no Sample beyond kill + 250 ms, late-received pre-kill Samples counted in the manifest (the test does not require that every Sample arrives before the kill), Collector healthy with `storage_ok=true`.
    - Files: `tests/docker_integration/test_experiment_b.py`, `docs/m0-results/experiment-results.md`.
    - Verify: `python -m coroner.runner run b; python -m pytest tests/docker_integration/test_experiment_b.py --run-docker -q`.
    - Done when: run id, send time, exit code, OOMKilled, `FinishedAt`, timing classification counts, and offset used are recorded from real output. If an anomaly occurs, record it and STOP and ask before touching any tolerance.
    - _Requirements: 10.1, 10.3, 10.4, 10.7, 10.8, 10.9, 10.10, 10.11, 10.12, 10.13, 13.4_

  - [x] 7.5 (PRIORITY) Write, execute, and validate Experiment C (genuine Docker OOM kill)
    - `tests/docker_integration/test_experiment_c.py` plus a real run with the default 128 MiB limit: `python -m coroner.runner run c`. Independently run `docker inspect` and assert equality with the manifest for `State.OOMKilled`, `State.ExitCode`, `State.Status`, container id, and `HostConfig.Memory` (must equal 134217728); container id matches Sample `container_id` where reported (prefix match if source is `hostname`); Collector survival, at least one pre-crash Sample, peak RSS above first RSS; `diagnostics` and `sampling_limitations` present (values not asserted). If OOMKilled is not true, report the failure honestly, do not fake, retry with a different tactic, or relax checks, and STOP and ask.
    - Files: `tests/docker_integration/test_experiment_c.py`, `docs/m0-results/experiment-results.md`.
    - Verify: `python -m coroner.runner run c; python -m pytest tests/docker_integration/test_experiment_c.py --run-docker -q; $id = docker compose -p coroner ps -a -q demo_app; docker inspect $id --format '{{.State.OOMKilled}} {{.State.ExitCode}} {{.State.Status}} {{.HostConfig.Memory}}'`.
    - Done when: real run id, `OOMKilled`, `ExitCode`, `Status`, full container id, `HostConfig.Memory`, highest RSS, Diagnostic_Metrics, and Sampling_Limitations are recorded from real output.
    - _Requirements: 11.1, 11.3, 11.4, 11.5, 11.10, 11.11, 11.12, 11.13, 11.14, 11.15, 11.16, 11.17, 11.18, 11.19, 11.20, 13.5_

  - [x] 7.6 (PRIORITY) Cross-validate results and write the experiment results note
    - Confirm the three results table (A: 1/false, B: 137/false, C: 137/true) from real data, confirm Collector survival after each run, evidence files readable with `Get-Content`, `Select-String`, and `(Get-Content file).Count` (standard tools, Requirement 12.3), and the summary command output for each run id. Record any anomaly or deviation without reinterpreting it.
    - Files: `docs/m0-results/experiment-results.md`.
    - Verify: `Get-Content runs\<run_id>\evidence.jsonl -TotalCount 3; (Get-Content runs\<run_id>\evidence.jsonl).Count; python -m coroner.runner summary <run_id>`.
    - Done when: the note contains only values copied from real output, with run ids.
    - _Requirements: 12.3, 12.5, 14.7, 9.8, 10.7, 11.10_

  - [ ]* 7.7 Repeat consistency: three consecutive runs per experiment (deferrable)
    - Run each experiment three times (sequentially, never in parallel) and compare `exit_code` and `oom_killed` across manifests; write `tests/docker_integration/test_repeat_consistency.py`.
    - Files: `tests/docker_integration/test_repeat_consistency.py`, `docs/m0-results/experiment-results.md`.
    - Verify: `python -m pytest tests/docker_integration/test_repeat_consistency.py --run-docker -q`.
    - Done when: real run ids and the comparison are recorded.
    - _Requirements: 12.1, 12.4_

  - [ ]* 7.8 Collector restart integration test (deferrable)
    - Restart the Collector container while evidence exists and verify acknowledged records are unchanged and not duplicated (extends the volume test of 5.6 to a full experiment run).
    - Files: `tests/docker_integration/test_collector_restart.py`.
    - Verify: `python -m pytest tests/docker_integration/test_collector_restart.py --run-docker -q`.
    - Done when: real result recorded.
    - _Requirements: 4.4, 5.3_

  - [ ]* 7.9 Probe CPU overhead and Collector acknowledgement latency measurement (deferrable)
    - `test_probe_overhead_and_ack_latency`: idle CPU over 60 s against a probe-disabled baseline run (at most 5 percent added) and acknowledgement within 500 ms at 1 Sample per second using the logged `ingest_ms`. If a threshold cannot be met, record the measurement and ask the user.
    - Files: `tests/docker_integration/test_probe_overhead.py`, `docs/m0-results/overhead.md`.
    - Verify: `python -m pytest tests/docker_integration/test_probe_overhead.py --run-docker -q`.
    - Done when: real measurements recorded.
    - _Requirements: 2.10, 4.3_

  - [x] 7.10 Checkpoint - M0.7 complete
    - Run the default unit tests (`python -m pytest -q`; expected exit code 0 and a passed count not lower than the previous checkpoint, exit code 5 is a FAILURE), confirm the experiment results note contains real run data for A, B, C, and the ordinary 500 test, and ask the user if questions arise. Then propose (do not perform) a tested milestone commit per Execution Rule 9: show the branch, git status --short, the specific paths, and a draft message, and wait for explicit approval; no commit or push without it. Target date: 20 November 2026.

- [ ] 8. M0.8 Final M0 acceptance tests, documentation (incl. Windows section), reproducibility checks, Git commit preparation
  - [x] 8.1 Write `README.md` (reproduction, limitations, Windows section)
    - Prerequisites with minimum versions (use the verified versions), Compose start commands, how to run each experiment, how to inspect evidence, expected exit codes and OOMKilled per experiment (A: 1/false, B: 137/false, C: 137/true), known limitations (the 15 items in the design, including the unobserved interval and the Experiment C Sampling_Limitations), Durability_Levels and `CORONER_DURABILITY` with the fsync caveat, Experiment A is a controlled fatal process exit using `os._exit(1)`, destructive endpoints disabled unless `CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS=1` and for local use only, `storage_ok` semantics (HTTP 200 alone is not proof), the Experiment B capture versus receive note with the documented 250 ms tolerance and the measured clock offset from 7.1, default pytest is Docker-free and network-free with the opt-in commands. Windows section: Docker Desktop with Linux containers and WSL2, minimum Python, PowerShell verification commands, how to confirm the memory limit is enforced, WSL2 caveats.
    - Files: `README.md`.
    - Verify: `Get-Content README.md | Select-String "os._exit","storage_ok","250","WSL2","run-docker"`.
    - Done when: every statement about results matches `docs/m0-results/` and no result is claimed that was not recorded.
    - _Requirements: 14.1, 14.2, 14.3, 14.8, 14.9, 14.10, 14.11, 14.12, 16.4_

  - [x] 8.2 Write documentation phrase tests
    - Docker-free test that README contains the required statements (expected codes per experiment, destructive endpoint note, `storage_ok`, durability levels, opt-in commands, 250 ms tolerance, Windows section items) and does not describe the Crash_Endpoint as an exception.
    - Files: `tests/unit/test_docs.py`.
    - Verify: `python -m pytest tests/unit/test_docs.py -q`.
    - Done when: passes with output recorded.
    - _Requirements: 9.12, 14.2, 14.3, 14.8, 14.9, 14.10, 14.11, 14.12, 16.4_

  - [ ] 8.3 (PRIORITY) Run the full acceptance suites and record real results
    - Run the default suite (`python -m pytest -q`) and the opt-in suite (`python -m pytest --run-docker -q`). Record exit codes and total passed, failed, skipped, and deselected counts against the expected counts (exit code 5 or zero collected tests is a FAILURE). Confirm default runs deselect all `docker_integration` tests and need no Docker or network.
    - Files: `docs/m0-results/test-runs.md`.
    - Verify: `python -m pytest -q; python -m pytest --run-docker -q`.
    - Done when: real outputs and counts are recorded. Any failure is reported, not hidden.
    - _Requirements: 13.4, 13.6, 13.7, 13.9, 13.10, 13.11_

  - [ ] 8.4 Reproducibility check from a clean state
    - Fresh venv from `requirements/dev.txt`, `pip check`, `docker compose down` (no `-v`; a volume reset only via Execution Rule 11 with the user's approval) and `docker compose build --no-cache`, then follow the README commands in order and confirm exit code and OOMKilled for each experiment match the documentation. Record any README step that failed and fix the README (not the results).
    - Files: `docs/m0-results/reproducibility.md`, `README.md` (fixes only).
    - Verify: `python -m venv .venv-repro; .\.venv-repro\Scripts\python.exe -m pip install -r requirements/dev.txt; .\.venv-repro\Scripts\python.exe -m pip check; docker compose build --no-cache; python -m coroner.runner run a; python -m coroner.runner run b; python -m coroner.runner run c`.
    - Done when: real outputs and run ids are recorded and `.venv-repro` is removed.
    - _Requirements: 14.7, 12.4, 8.2_

  - [ ] 8.5 Prepare the Git commit (do not commit)
    - Confirm `.kiro/` is tracked and not ignored (`git check-ignore -v .kiro/specs/coroner-m0/tasks.md` prints nothing), confirm `runs/` (including `runs/archive/`), `.venv*`, `spike/`, and `__pycache__` are ignored, review `git status --short` and `git diff --stat`, list the files that would be staged (specific paths, including `.kiro/specs/coroner-m0/`, `docs/m0-results/`, source, tests, `requirements/`, `docker/`, `compose.yaml`, `README.md`, `pyproject.toml`, `pytest.ini`, `.gitignore`, `.dockerignore`), flag any file that may hold secrets, and draft a commit message. Confirm the branch is `feat/coroner-m0`. Verify the GitHub remote is private (`git remote -v; gh repo view --json visibility,url`, or ask the user to confirm) and record the result; if it is public or unknown, do not push and ask. Never change visibility. Present the list, message, and the real test counts from 8.3 to the user and wait for explicit approval. Do NOT run `git commit` or `git push` in this task; a push needs its own explicit approval.
    - Files: none modified (prepare only).
    - Verify: `git status --short; git diff --stat; git check-ignore -v .kiro/specs/coroner-m0/tasks.md`.
    - Done when: the staging list and draft message are shown to the user and approval is awaited.
    - _Requirements: 14.1_

  - [ ] 8.6 Final checkpoint - M0 complete
    - Run the default unit tests (`python -m pytest -q`; expected exit code 0 and a passed count not lower than the previous checkpoint, exit code 5 is a FAILURE), confirm the recorded results of 7.x and 8.3 are real, summarise any open deviations or unresolved questions, and ask the user for approval to commit. Then propose (do not perform) the final tested milestone commit per Execution Rule 9, after the remote privacy check of task 8.5. Target date: 20 November 2026.

## Schedule and Priorities

M0 completion target: **20 November 2026** (planning date: 13 November 2026). The time after M0 (21 November 2026 onward) is reserved for the forensic UI, deployment, the demo video, and the submission, so M0 must not slip into it.

| Milestone | Target date | Main content |
| --- | --- | --- |
| M0.1 | 14 November 2026 | Toolchain, Docker and dependency verification, main `.venv`, OOM spike (1.10), baseline commit proposal |
| M0.2 | 15 November 2026 | Evidence schema, writer, reader, repair |
| M0.3 | 16 November 2026 | Collector, `storage_ok`, persistence tests |
| M0.4 | 17 November 2026 | Probe and Demo_App |
| M0.5 | 18 November 2026 | Dockerfiles, Compose, volume and isolation checks |
| M0.6 | 19 November 2026 | Experiment runner, evaluation, manifests |
| M0.7 and M0.8 | 20 November 2026 | Experiments A, B, C, acceptance runs, README, commit preparation |

Priority tasks (mandatory M0 proof, marked `(PRIORITY)` in the title): 1.4, 1.5, 1.8, 1.10, 2.4, 2.10, 2.11, 3.3, 3.5, 3.11, 4.3, 4.11, 5.3, 5.6, 5.7, 6.1, 6.13, 6.14, 6.15, 6.16, 6.18, 7.1, 7.3, 7.4, 7.5, 7.6, 8.3. All other unmarked, non-optional tasks are required supporting work (tests, documentation, wiring). Optional tasks (marked `*`: 3.14, 4.7, 4.8, 6.20, 7.7, 7.8, 7.9) are deferrable.

Cut-line rule: if the work is behind the target dates above, drop optional (`*`) tasks first, in this order: 7.9, 7.8, 7.7, 6.20, 3.14, 4.8, 4.7. Do not drop any mandatory task (priority or supporting) without asking the user first. The genuine OOM proof (7.5), the `storage_ok` gating (3.3, 6.11), and the Experiment B timing classification (6.5) are never dropped. Record any schedule slip and each dropped optional task in `docs/m0-results/schedule.md`.
## Notes

- Tasks marked with `*` are optional (deferrable) and can be skipped for the mandatory M0 proof. Never skip the genuine OOM verification (7.5), the `storage_ok` readiness gating (3.3, 6.11), or the Experiment B capture-versus-receive classification (6.5).
- Each task references specific requirements for traceability. Property tests reference the design's Correctness Property numbers.
- The M0.1 spike (1.9, 1.10) must finish with a recorded PASS before any M0.2 work starts; on FAIL, stop and ask the user.
- Pytest never runs before the main `.venv` exists (1.8); exit code 5 (no tests collected) is a failure (Execution Rule 10).
- Git and Docker volume safety follow Execution Rules 9 and 11; commits and volume deletions need the user's explicit approval at that moment.
- Docker tasks (5.5 to 5.7, 7.x, 8.3, 8.4) must run sequentially, never in parallel, because they share the Compose project `coroner`.
- Checkpoints are not part of the dependency graph.
- Task 2.19 (payload contracts of the six non-sample record types) sits in wave 10 so it finishes before wave 11 (Collector service 3.5), which is the first component that relies on those records. It needs the user's approval of the contracts before any code (STOP and ask).
- Task 1.11 (baseline specification commit proposal and remote privacy check) is an administrative Git checkpoint, not a technical dependency. It is not in the dependency graph, so it blocks no wave. It stays tracked and pending, and must be completed before GitHub Checkpoint 2 (checkpoint 1.12). Approved by the user on 14 November 2026; no other dependency changed.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.4"] },
    { "id": 1, "tasks": ["1.5"] },
    { "id": 2, "tasks": ["1.8"] },
    { "id": 3, "tasks": ["1.3", "1.9"] },
    { "id": 4, "tasks": ["1.6", "1.10"] },
    { "id": 5, "tasks": ["2.1"] },
    { "id": 6, "tasks": ["2.2", "4.1"] },
    { "id": 7, "tasks": ["2.3", "2.4", "2.6", "4.2", "4.10"] },
    { "id": 8, "tasks": ["2.5", "2.7", "2.8", "2.9", "2.10", "2.11", "4.3"] },
    { "id": 9, "tasks": ["2.12", "2.13", "2.14", "2.15", "2.16", "3.1", "3.3", "4.4", "4.5", "4.6", "4.7", "4.8", "4.9", "4.11"] },
    { "id": 10, "tasks": ["2.17", "2.19", "3.2", "3.4", "4.12", "4.13", "4.14", "6.1", "6.3", "6.9", "6.11"] },
    { "id": 11, "tasks": ["3.5", "3.9", "6.2", "6.4", "6.5", "6.10", "6.12"] },
    { "id": 12, "tasks": ["3.6", "3.7", "3.8", "3.10", "3.11", "6.6", "6.7", "6.13"] },
    { "id": 13, "tasks": ["3.12", "3.13", "3.14", "6.8", "6.14"] },
    { "id": 14, "tasks": ["5.1", "5.2", "6.15", "6.16"] },
    { "id": 15, "tasks": ["5.3", "6.17", "6.18"] },
    { "id": 16, "tasks": ["5.4", "5.5", "6.19", "6.20"] },
    { "id": 17, "tasks": ["5.6"] },
    { "id": 18, "tasks": ["5.7"] },
    { "id": 19, "tasks": ["7.1"] },
    { "id": 20, "tasks": ["7.2"] },
    { "id": 21, "tasks": ["7.3"] },
    { "id": 22, "tasks": ["7.4"] },
    { "id": 23, "tasks": ["7.5"] },
    { "id": 24, "tasks": ["7.6"] },
    { "id": 25, "tasks": ["7.7", "8.1"] },
    { "id": 26, "tasks": ["7.8", "8.2"] },
    { "id": 27, "tasks": ["7.9"] },
    { "id": 28, "tasks": ["8.3"] },
    { "id": 29, "tasks": ["8.4"] },
    { "id": 30, "tasks": ["8.5"] }
  ]
}
```
