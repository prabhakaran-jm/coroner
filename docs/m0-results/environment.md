# M0 Environment Verification (Task 1.1)

Recorded on 2026-11-14 (host clock, Windows, PowerShell). All values below are copied from commands that were actually run.

## Toolchain

| Command | Observed output |
|---|---|
| `git --version` | `git version 2.55.0.windows.5` |
| `python --version` | `Python 3.12.10` |
| `python -m pip --version` | `pip 25.0.1 from C:\Users\<REDACTED_USER>\AppData\Local\Programs\Python\Python312\Lib\site-packages\pip (python 3.12)` (username redacted as <REDACTED_USER> before publication; the original output contained the local account name) |
| `where.exe python` | `C:\Users\<REDACTED_USER>\AppData\Local\Programs\Python\Python312\python.exe` (first), `C:\Users\<REDACTED_USER>\AppData\Local\Microsoft\WindowsApps\python.exe` (Store stub, second) (username redacted as <REDACTED_USER> before publication; the original output contained the local account name) |
| `docker --version` | `Docker version 29.9.3, build 5457199` |
| `docker compose version` | `Docker Compose version v5.7.0` |
| `docker info --format '{{.OSType}} {{.ServerVersion}}'` | `linux 29.9.3` |

Python 3.12.10 meets the design minimum of 3.11. The real interpreter is first on PATH. The Store stub is second and is not used.

An earlier attempt (before the environment was prepared) failed: `python` resolved to the Store stub, and `py` and `docker` were not found. That is superseded by the table above.

## Git

| Check | Observed |
|---|---|
| Branch before | `master` |
| `git branch --list feat/coroner-m0` | no output (branch did not exist) |
| Action | `git switch -c feat/coroner-m0` -> `Switched to a new branch 'feat/coroner-m0'` |
| `git branch --show-current` | `feat/coroner-m0` |
| `git remote -v` | `origin https://github.com/prabhakaran-jm/coroner.git (fetch)` and `(push)` |
| `git status --short` (before edit) | `?? .kiro/` |
| `git status --short` (after edit) | ` M .gitignore` and `?? .kiro/` |

No commit, push, stash, clean, or reset was run. Remote visibility (private) is not checked in this task; task 1.11 does that.

## .gitignore

`.kiro/` was not matched by any rule. Existing patterns that could hide planned files: `build/`, `lib/`, `lib64/`, `*.manifest`, `*.spec`, `*.log`, `.env`, `.envrc`, `.venv`. Consequences for later tasks: run manifests are named `manifest.json`, and no planned directory is named `lib/` or `build/`.

Added at the end of `.gitignore`:

```
# Coroner M0
runs/
.venv*/
spike/
```

## Ignore checks

| Command | Exit code | Result |
|---|---|---|
| `git check-ignore -v .kiro/specs/coroner-m0/tasks.md` | 1 | not ignored (expected) |
| `git check-ignore -v runs/x/manifest.json` | 0 | `.gitignore:223:runs/` |
| `git check-ignore -v .venv-verify/x` | 0 | `.gitignore:224:.venv*/` |
| `git check-ignore -v spike/x` | 0 | `.gitignore:225:spike/` |
| `git check-ignore -v docs/m0-results/environment.md` | 1 | not ignored (expected, this file is tracked) |

## Result

PASS. Docker Desktop was reachable (`docker info` returned `linux 29.9.3`). Base-image availability and the Docker memory-limit behaviour are not checked here; task 1.4 does that.
## Task 1.2: packaging and package skeleton

Python 3.12.10. Pytest was not run and is not installed (`importlib.util.find_spec('pytest')` is `None`). The skeleton has 0 test files by design, so "no tests exist yet" is a fact here, not a pass.

Created: `pyproject.toml`, `pytest.ini`, `coroner/` with `common`, `evidence`, `probe`, `demo_app`, `collector`, `runner`, `runner/experiments` (each an empty `__init__.py`), `tests/`, `tests/unit/`, `tests/docker_integration/` (each with an empty `__init__.py`), and `requirements/` and `docker/` (each holding a `.gitkeep` so Git tracks the empty directory).

`requires-python = ">=3.11"` in `pyproject.toml` is provisional (the design minimum). Task 1.5 sets the verified minimum.

| Check | Exit | Observed |
|---|---|---|
| Import of `coroner`, `coroner.common`, `coroner.evidence`, `coroner.probe`, `coroner.demo_app`, `coroner.collector`, `coroner.runner`, `coroner.runner.experiments` in one interpreter | 0 | `ok` |
| Same eight modules, one fresh interpreter each | 0 each | each printed `<module> ok` |
| `configparser` read of `pytest.ini` | 0 | section `['pytest']`, `testpaths = tests`, marker `docker_integration` registered |
| `tomllib` read of `pyproject.toml` | 0 | name `coroner`, `requires-python` `>=3.11`, `packages.find` `{'include': ['coroner*']}` |

Not verified: setuptools package discovery. `setuptools` is not installed in the global interpreter (`ModuleNotFoundError`), and installing is out of scope for this task. The `pyproject.toml` setuptools table is checked only for syntax and values. A real discovery check can run after the main `.venv` exists (task 1.8).
## Task 1.8: main `.venv`

Branch: `feat/coroner-m0`. `.venv` did not exist before this task. Created with the host interpreter, Python 3.12.10, and installed from `requirements/dev.txt` into `.venv` only. Nothing was installed globally (global `find_spec` check: pytest and fastapi are not installed).

| Check | Exit | Observed |
|---|---|---|
| `python -m venv .venv` | 0 | `.venv\Scripts\python.exe` created |
| `.\.venv\Scripts\python.exe --version` | 0 | `Python 3.12.10` (matches tasks 1.1 and 1.5) |
| `.\.venv\Scripts\python.exe -m pip --version` | 0 | `pip 25.0.1 from C:\Users\<REDACTED_USER>\Projects\coroner\.venv\Lib\site-packages\pip (python 3.12)` (username redacted as <REDACTED_USER> before publication; the original output contained the local account name) |
| `pip install -r requirements/dev.txt` | 0 | 24 packages installed |
| `pip check` | 0 | `No broken requirements found.` |
| `pip freeze` vs `requirements/dev.txt` | n/a | 24 lines each, 0 differences (case and `_`/`-` normalised). No extra packages. |
| `python -m pytest --version` | 0 | `pytest 8.3.4` |
| `importlib.metadata` versions | 0 | fastapi=0.115.6 uvicorn=0.34.0 httpx=0.28.1 hypothesis=6.122.3 pytest=8.3.4 PyYAML=6.0.2 |
| Third-party imports (fastapi, uvicorn, httpx, hypothesis, pytest, yaml) | 0 | `third-party imports ok` |
| Import of `coroner`, `coroner.common`, `coroner.evidence`, `coroner.probe`, `coroner.demo_app`, `coroner.collector`, `coroner.runner`, `coroner.runner.experiments` | 0 | `8 coroner packages ok` |
| `git check-ignore -v .venv/x` | 0 | `.gitignore:224:.venv*/` (also for `.venv/Scripts/python.exe`) |

Setuptools package discovery: `setuptools` is not in the pinned requirements and was not added to `.venv`. Discovery was checked with `.\.venv\Scripts\python.exe -m pip wheel . --no-deps` (pip's isolated build fetched setuptools in a temporary build environment). It exited 0 and built `coroner-0.0.0-py3-none-any.whl`. The wheel contains exactly the eight packages: `coroner`, `coroner.collector`, `coroner.common`, `coroner.demo_app`, `coroner.evidence`, `coroner.probe`, `coroner.runner`, `coroner.runner.experiments`. The wheel and the `build/` and `coroner.egg-info` directories it left behind were deleted.

No tests were run. The test infrastructure (task 1.3) and the structure tests (task 1.6) do not exist yet, so this task makes no claim that any test passes. The only pytest invocation was `--version`.

## Approved compatibility decision: minimum Python 3.12

Approved by the user after task 1.5.

- Python 3.12 is the minimum supported version for M0, and `pyproject.toml` keeps `requires-python = ">=3.12"`.
- `PyYAML==6.0.2` and `colorama==0.4.6` are kept. No dependency was substituted.
- Spec references updated: `design.md` Windows prerequisites now say "Python 3.12 or newer" (was "3.11 or newer"), and a new decision D24 records the minimum. `requirements.md` has no Python version reference, so it is unchanged. No other architecture, requirement, or decision was changed.
- Python 3.10 and 3.11 are not claimed. The pinned packages declare `>=3.10`, but only 3.12.10 and 3.12.8 were tested.
## Task 1.3: pytest opt-in, markers, and Docker-free/network-free guards

Run with `.\.venv\Scripts\python.exe` (Python 3.12.10, pytest 8.3.4) on the host, in the real repository. Raw output is in the gitignored `runs/t13_*.txt`.

Files: `tests/conftest.py`, `tests/unit/conftest.py`, `tests/unit/guards.py` (helper module, not in the task's file list), `tests/unit/test_optin.py`, `tests/unit/test_guards.py`, `tests/docker_integration/test_placeholder.py`.

| Command | Exit | Observed |
|---|---|---|
| `python -m pytest --collect-only -q` | 0 | `46/47 tests collected (1 deselected)`. N = 46 collected. |
| `python -m pytest -q` (default) | 0 | `46 passed, 1 deselected in 19.57s` |
| `python -m pytest --run-docker -q -rs` | 0 | `47 passed in 20.17s` (Docker was available, so the placeholder ran) |
| `$env:CORONER_RUN_DOCKER = "1"; python -m pytest -q -rs` | 0 | `47 passed in 19.85s`; the variable was removed afterwards |
| `--run-docker` with `PATH` set to an empty directory, `tests/docker_integration` only | 0 | `1 skipped`; reason `Docker is unavailable: the docker executable was not found on PATH` |

Expected by the task: default run exit 0, at least 2 passed, exactly 1 deselected (the placeholder). Observed: 46 passed, 1 deselected. Exit code 5 and zero collected tests did not occur.

An earlier default run during development had `1 failed, 44 passed, 1 deselected`. The failing test asserted the report header text under `-q`, which hides headers. The test was corrected (it now runs without `-q` in a new `test_header_states_how_to_opt_in`), and the runs in the table are after that fix.

What the tests cover (all Docker-free; the opt-in tests run pytest in a child process against a small fixture project with a copy of `tests/conftest.py` and a simulated Docker probe):
- default run deselects marked tests and tests in `tests/docker_integration` even without the marker, and never calls the Docker probe;
- `--run-docker` and `CORONER_RUN_DOCKER=1` both opt in; the values `0`, empty, `true`, `yes`, `2`, and ` 1` do not;
- opted in with Docker unavailable: integration tests skipped with `Docker is unavailable: ...`, unit tests still run, exit 0;
- real probe with no `docker` on `PATH` reports the missing executable;
- an opted-in failing integration test makes the run exit 1 while unit results are unchanged; without opt-in a failing integration test is ignored;
- the repository registers the `docker_integration` marker and the `--run-docker` option;
- guards block `socket.connect`, `connect_ex`, `getaddrinfo`, `create_connection`, asyncio `loop.create_connection` and `loop.sock_connect` (including numeric IPs that skip name resolution), and `subprocess.Popen` and `subprocess.run` of `docker`, `docker.exe`, `docker-compose`, a full path to `docker.exe`, and shell strings that run Docker;
- guards still allow `socket.socketpair`, a running asyncio event loop (Windows self-pipe), and non-Docker subprocesses such as `sys.executable`.

Limitations:
- The guards cover unit tests only (`tests/unit`). They patch Python-level calls. A C extension or a subprocess started by a unit test could still reach the network; the unit tests spawn only `sys.executable`.
- The Docker-available path of the real probe was exercised only by the real `--run-docker` run on this machine, where `docker info` succeeded. The fixture project simulates the Docker-down case through a seam (`config._coroner_docker_probe`), and the real missing-executable case through an empty `PATH`. A case where the `docker` executable exists but the daemon is stopped was not run for real.
- Integration tests were not run beyond the trivial placeholder. No crash experiment was run.

## Scheduler note: why 1.3 was not listed as ready

The task tool follows the waves in the Task Dependency Graph and holds a wave until every earlier wave is complete. Wave 1 is `["1.5", "1.11"]`. Task 1.11 is propose-only and still open, so waves 2 and later (1.8, 1.3, 1.9, 1.6, 1.10, ...) are never listed as ready. Task 1.8 was run on explicit instruction, which the tool allows. The real prerequisites of 1.3 are 1.2 and 1.8, both complete, and checkpoint 1.7 depends on 1.3, so it is not a prerequisite. Proposed smallest correction (not applied): move `1.11` from wave 1 to a later wave, for example wave 4 next to `1.6` and `1.10`, since no task depends on 1.11.
## Scheduler correction (approved 14 November 2026)

Task 1.11 (baseline specification commit proposal and remote privacy check) is an administrative Git checkpoint, not a technical dependency.

- `tasks.md`: the Task Dependency Graph wave 1 changed from `["1.5", "1.11"]` to `["1.5"]`, and a note under "Checkpoints are not part of the dependency graph" now states that 1.11 is outside the graph, stays tracked and pending, and must be completed before GitHub Checkpoint 2 (checkpoint 1.12). No other task's dependencies changed.
- Task 1.11 is still `not_started` and still listed; it was not run.
- Result: after the change, the task tool reported task 1.9 as newly ready when task 1.6 was started. `task_list ready` returns a single "next" task and still names 1.11 first, because 1.11 now has no wave. Tasks 1.6 and 1.9 are not in any wave with, or after, 1.11, so 1.11 does not block them. Task 1.6 was started and completed under this rule.

## Opt-in value check (CORONER_RUN_DOCKER)

Run in the real repository: `.\.venv\Scripts\python.exe -m pytest tests/docker_integration -q`, with the variable set per row.

| `CORONER_RUN_DOCKER` | Exit | Result |
|---|---|---|
| `1` | 0 | `1 passed` (integration test ran) |
| `0` | 5 | `1 deselected` |
| `true` | 5 | `1 deselected` |
| `yes` | 5 | `1 deselected` |
| empty | 5 | `1 deselected` |
| `2` | 5 | `1 deselected` |
| unset | 5 | `1 deselected` |

Only the exact value `1` opts in, matching Requirement 13.11 and the code. Exit code 5 in the non-opted rows is expected here: the command selects only the integration directory, so nothing remains to run. The default full `pytest` is unaffected (it collects the unit tests).

Documentation error found and corrected: `design.md` (testing section) documented the opt-in as `CORONER_RUN_DOCKER=1 pytest`, which is POSIX shell syntax and does not work in Windows PowerShell. It now also states that only `1` opts in and gives both forms (`$env:CORONER_RUN_DOCKER = "1"; pytest` for PowerShell). The Windows section of the design already used the PowerShell form. No test error was found: `test_optin.py` already covers `0`, empty, `true`, `yes`, `2`, and ` 1` as not opting in, and `1` as opting in.

## Task 1.6: structure tests

File created: `tests/unit/test_structure.py` (no other file changed). `.\.venv\Scripts\python.exe`, Python 3.12.10, pytest 8.3.4. Raw output is in `runs/t16_*.txt`.

| Command | Exit | Observed |
|---|---|---|
| `python -m pytest tests/unit/test_structure.py --collect-only -q` | 0 | `75 tests collected` |
| `python -m pytest tests/unit/test_structure.py -q -rs` | 0 | `75 passed in 1.29s` (0 failed, 0 skipped) |
| `python -m pytest --collect-only -q` | 0 | `121/122 tests collected (1 deselected)` |
| `python -m pytest -q -rs` (full default suite) | 0 | `121 passed, 1 deselected in 19.79s` (0 failed, 0 skipped) |

The task expected at least 3 tests collected and passed, and treats exit code 5 as a failure. Neither problem occurred.

What the tests check:
- AST import rules for the six packages, from the design's dependency direction: `common` imports no Coroner package; `evidence` may import `common`; `probe` imports none; `demo_app` may import `probe`; `collector` and `runner` may import `evidence` and `common`. Relative imports (`from .. import x`), `from coroner import x`, imports inside functions, and `importlib.import_module` or `__import__` with a constant string are all resolved and checked.
- Third-party imports: `probe` is stdlib-only (design). `runner` is stdlib-only because `requirements/runner.txt` has no packages, so `common` and `evidence` (which the runner imports) are held to stdlib-only too. This is an inference from the design and task 1.5, not a new design decision; if later code needs a third-party package there, the test fails and the choice goes to the user. `demo_app` and `collector` may import only packages pinned in their own requirements files.
- The checker is tested against 23 prohibited and 7 allowed synthetic snippets, so an empty skeleton cannot pass vacuously. A temporary violating file (`coroner/probe/_tmp_violation.py` with `import coroner.common` and `import fastapi`) made `test_package_follows_the_allowed_import_rules[probe]` and `test_probe_imports_only_the_standard_library` fail (`2 failed, 6 passed` for the selected tests, exit 1). The file was deleted and its removal confirmed.
- Fresh interpreter (`sys.executable -c`): each of the eight packages imports on its own; every module imports in one interpreter (import cycles); importing `coroner.probe` loads no other Coroner package and none of fastapi, starlette, pydantic, uvicorn, httpx, yaml.
- Pins: the four expected files exist; every non-comment line matches `^[A-Za-z0-9_.\-\[\]]+==\S+$` and has no range, wildcard, marker, or space; each package is pinned once per file; every pin in `collector.txt`, `demo_app.txt`, and `runner.txt` appears identically in `dev.txt`; `runner.txt` has no packages. The pin checker is tested against 11 bad and 5 good lines.

Limitations: the source packages are empty, so the real-tree checks hold only for the skeleton until code is added; they are meant to guard later tasks. The AST check cannot see imports built from non-constant strings. Structure tests do not check that the Demo_App and Collector images contain only their packages; that belongs to the Dockerfile tasks (5.1, 5.2).
## Checkpoint 1.7: M0.1 verification gate

Run on 14 November 2026 with `.\.venv\Scripts\python.exe` (Python 3.12.10, pytest 8.3.4). Raw output: `runs/t17_*.txt`.

| Command | Exit | Observed |
|---|---|---|
| `python -m pytest --collect-only -q` | 0 | `121/122 tests collected (1 deselected)` |
| `python -m pytest -q -rs` | 0 | `121 passed, 1 deselected in 20.55s` (0 failed, 0 skipped) |

Expected: exit code 0 and at least the counts of tasks 1.3 (46 passed) and 1.6 (75 passed). Observed 121 = 46 + 75. Exit code 5 did not occur. The 1 deselected test is the Docker integration placeholder, as designed.

Task status from the task tool: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, and 1.8 are completed. Real recorded results exist for 1.1 and 1.2 and 1.3 and 1.8 (this file), 1.4 (`docker-prereqs.md`), and 1.5 (`dependency-verification.md`).

Remaining M0.1 tasks: 1.9 (create the spike, code only), 1.10 (run the spike, PRIORITY), 1.11 (baseline commit proposal, administrative, pending), 1.12 (M0.1 completion checkpoint). Nothing from M0.1 has been committed since checkpoint 1; the new files and edits are uncommitted.
## Task 1.9: disposable OOM-survival spike (code only)

Created under the gitignored `spike/` (confirmed: `git check-ignore -v spike/collector.py` exit 0, rule `.gitignore:225:spike/`; `git status` does not list it). No container was started, no volume was created or deleted, and the spike was not run. Raw output: `runs/t19_*.txt`.

Files: `spike/compose.spike.yaml`, `spike/collector.py`, `spike/victim.py`, `spike/README.md` (plus `spike/__pycache__` from the compile check). The two Python files total 115 non-blank lines (task budget: about 120).

| Command | Exit | Observed |
|---|---|---|
| `.\.venv\Scripts\python.exe -m py_compile spike/collector.py spike/victim.py` | 0 | no output |
| `docker compose -f spike/compose.spike.yaml config` | 0 | parsed; normalised output below |
| `docker compose -f spike/compose.spike.yaml config --format json` | 0 | used by the static checks |
| `runs/t19_static_check.py` (31 static checks) | 0 | 31 checks, 0 failed |
| `docker volume ls`, `docker ps -a`, `docker network ls` after the checks | 0 | 0 volumes, 0 containers, networks `bridge host none` only: nothing was created |

Key lines of the `config` output: `name: coroner-spike`; services `collector` and `victim` with `container_name` `coroner-spike-collector` and `coroner-spike-victim`; `image: python:3.12.8-slim-bookworm` (the tag verified in task 1.4); victim `mem_limit: "67108864"`, `memswap_limit: "67108864"`, `restart: 'no'`, `user: 10001:10001`; both services `cap_drop: [ALL]`, `no-new-privileges:true`, `read_only: true`, no `ports`; collector mounts volume `coroner_spike_evidence` (real name `coroner-spike_coroner_spike_evidence`) at `/data`; the only bind mounts are `collector.py` and `victim.py`, read-only; network `coroner-spike_default`; victim `depends_on: collector: condition: service_healthy`.

Architecture:
- Collector container: stdlib `ThreadingHTTPServer` on port 8081, reachable only on the compose network. `POST /sample` validates the sample (JSON object with integer `seq` and `rss_bytes`, at most 4096 bytes), writes `{"recv_time", "sample"}` as one line to `/data/spike.jsonl`, calls `os.fsync` on the file (and on the directory at creation), and only then returns 200 `{"stored": true}`. Invalid samples get 422 and are not stored; a write failure gets 503 and no acknowledgement. `GET /healthz` returns 200 with `storage_ok`; the compose healthcheck requires `storage_ok` to be true.
- Victim container: every 200 ms reads its real RSS from `/proc/self/statm` times the page size, sends `{seq, ts, rss_bytes, retained_bytes, pid, container}` to the collector with a 200 ms timeout, then retains another 2 MiB `bytearray` filled with `0xA5` so every page is touched. The 64 MiB limit with swap equal to the limit means the kernel OOM killer ends it. No signal handler, no atexit hook, no `os._exit`, no `sys.exit`, no literal 137; the only exit it can choose is a safety cap at 256 MiB retained, which exits with code 3 (never 137) so a failed limit cannot pass as an OOM kill.
- Storage: only the collector mounts the evidence volume. The volume, network, and project name are in the `coroner-spike` namespace, separate from the main project's `coroner_coroner_evidence`.

Success rule for task 1.10: `State.OOMKilled` true and `State.ExitCode` 137 (both required), collector still running with `storage_ok` true, at least 3 valid samples on the volume, and the highest RSS above the first RSS.

Uncertainties (not tested in this task; task 1.10 tests them):
- Whether Docker Desktop with WSL2 reports `OOMKilled=true` for this kill. That is the point of the spike.
- A 2 MiB step every 200 ms means the kernel can kill the victim between samples, so the last stored RSS is expected to be below the 64 MiB limit. Container memory accounting also includes page cache and tmpfs, so RSS and the cgroup limit are not the same measure.
- A collector running as root with all capabilities dropped writing to a fresh named volume (owned by root) should work because the owner needs no capability. Not yet exercised; a failure shows as an unhealthy collector or a 503.
- Read access to the bind-mounted code files for UID 10001 on a Windows host, and Docker DNS resolution of `collector` from the victim, are untested.
- The collector's `storage_ok` is a writability check (`os.access`), not a write probe. Real storage health is shown only by the stored lines.
- `docker compose config` validates syntax and normalisation, not runtime behaviour.
## Task 1.11: GitHub Checkpoint 2 proposal (nothing committed or pushed)

Run on 14 November 2026. Raw output: `runs/cp2/`.

- Branch: `feat/coroner-m0`. Remote: `origin https://github.com/prabhakaran-jm/coroner.git` (fetch and push). Upstream: `origin/feat/coroner-m0`.
- Previous checkpoint: `git ls-remote --heads origin` (a live query) lists `a0c23ec8f50e439701120225e60a4e4016a53b4e refs/heads/feat/coroner-m0` and `bbc74ddfaea422ec269beafbafda69b9c1101700 refs/heads/master`. The commit `a0c23ec` exists on the remote branch, and `git branch -vv` shows the local branch at the same commit.
- Privacy: not formally confirmed. `gh` is not installed, so no authenticated GitHub API call was possible, and the anonymous API request returned HTTP 404 (which does not distinguish private from missing). Evidence gathered instead: `git ls-remote` with all Git configuration and credential helpers disabled and no prompt was refused (`fatal: Authentication failed`, exit 128). A public repository does not demand credentials to read, so the repository is not publicly readable. An authenticated `git ls-remote` on the same URL succeeded, so the repository exists. That points to private, but the setting itself is unverified; confirm "Private" in the repository settings on github.com. An earlier anonymous `ls-remote` that succeeded was run with only `credential.helper=` cleared; the machine's Git Credential Manager is also configured system-wide and answered, so that result is not evidence of public access.
- Default suite on the final tree: `.\.venv\Scripts\python.exe -m pytest -q -rs`: exit 0, `121 passed, 1 deselected`, 0 failed, 0 skipped (`--collect-only`: `121/122 tests collected (1 deselected)`).
- Task 1.10 documentation reference aligned to `docs/m0-results/oom-spike.md` in `tasks.md` (title, Files line, Expected line). The task id key in `tasks.meta.json` was renamed to match; task status is unchanged. The report was not renamed or duplicated.
- Excluded from staging: `.venv/`, `spike/`, `runs/` (including the raw Docker inspect output and `spike.jsonl` evidence), `__pycache__/`. `git status --short -uall` lists none of them because `.gitignore` covers them.
- Note: this file (lines for the Python and pip versions, lines in the Task 1.1 and 1.8 sections) contained Windows paths that included the local user name, for example `C:\Users\<REDACTED_USER>\...\python.exe`; the first of them was already committed in a0c23ec (the username remains in git history). These paths have now been redacted in the current version, with the username replaced by `<REDACTED_USER>`. `docker-prereqs.md` has none. They were not credentials.