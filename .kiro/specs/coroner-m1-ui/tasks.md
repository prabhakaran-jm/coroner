# Implementation Plan: Coroner M1 (Forensic Viewer)

## Overview

This plan implements the approved requirements and design of `coroner-m1-ui` (branch `feat/coroner-forensic-ui`) in four independently testable checkpoint groups. The implementation language is Python 3.12 for `coroner.viewer` (FastAPI, uvicorn, `os`/`stat` file access) and vanilla JavaScript (ES modules, no build step, no npm) for the browser code. The design uses Python signatures, so no language question is needed.

| Group | Top-level task | Content |
|---|---|---|
| M1.1-A | 1 | Safe local viewer foundation: CLI, literal `127.0.0.1` bind, Host guard, routes, security headers, bounded read-only artifact access, import-boundary test update, static read-only scan |
| M1.1-B | 2 | Evidence API: parsing, classification, loader, Samples, clocks, timeline payload, Docker state, serializers, `GET /api/runs`, `GET /api/runs/{run_id}` |
| M1.1-C | 3 | Investigation UI: shell, tokens, overview, incident detail, SVG RSS timeline, Evidence vs Uncertainty panel, states, static tests, manual visual review |
| M1.1-D | 4 | Comparison and acceptance: compare page and logic, golden vectors, browser smoke test, real-run launch check, README, acceptance ledger, final read-only verification |

Conventions used in every task:

- **Files** lists the files a task creates or modifies (full paths from the repository root `<repo root>`). **Verify** gives PowerShell commands run from the repository root. **Done when** gives completion criteria.
- Property test sub-tasks are optional (`*`), reference the design's Correctness Property numbers (1 to 23), use Hypothesis (`max_examples` of at least 100, `deadline=None`) and carry the comment `# Feature: coroner-m1-ui, Property N: <title>`. Example tests are core and mandatory.
- Every test fixture is built in `tmp_path`. No test reads the real `runs/` directory.
- Results notes live in `docs/m1-results/` (tracked in Git). Raw run output lives in `runs/` (gitignored, never staged).

## Execution Rules

These rules apply to every task and are part of the completion criteria.

1. **No unverified claims.** A task is complete only after its verification commands were actually executed and the real output (collected, passed, failed, skipped and deselected counts, exit codes, versions) was recorded in the task's completion note or in `docs/m1-results/`. Never write that a test passed, that JavaScript ran, or that a page rendered unless it was executed and observed. If a command could not be run, record that and why, and do not tick the task. Checkpoint reports contain real results, never predicted ones.
2. **Single agent.** The tasks are executed by one agent in order of the dependency graph. No task delegates work to another agent beyond what Kiro task execution normally does.
3. **No Docker, of any kind.** No `docker`, no `docker compose`, no image build or pull, no experiment, no SIGKILL, no `/crash`, no `/leak`, no container start, stop or removal, no volume command. The Viewer has no Docker dependency and the tests never call Docker.
4. **Protected volumes.** These six Docker volumes are never touched, listed, mounted, copied or deleted: `coroner_coroner_evidence`, `coroner-spike_coroner_spike_evidence`, `coroner-m07_coroner_evidence`, `coroner-t83-m05_coroner_evidence`, `coroner-t83_coroner_evidence`, `coroner-repro_coroner_evidence`.
5. **`runs/` is read-only.** Never modify, rename, delete or add a file under `runs/`. Never stage anything from `runs/` (it is gitignored; task 1.1 verifies this, task 4.13 verifies it again). The real `runs/` directory is read only by the Viewer in the manual steps of tasks 3.8, 4.7 and 4.8 and by the hash commands of tasks 1.1 and 4.8.
6. **M0 is frozen.** Do not modify any file in `coroner/` except new files under `coroner/viewer/`. Do not modify the evidence schema, the Run_Manifest schema, Dockerfiles, `docker/`, `compose.yaml`, `requirements/*.txt` or any M0 test. The one allowed edit outside `coroner/viewer/`, the new tests and `docs/` is the Viewer update of `tests/unit/test_structure.py` (task 1.8) and the `README.md` Viewer section (task 4.9).
7. **No new dependency pins.** Use only `fastapi`, `starlette`, `uvicorn`, `httpx`, `pytest` and `hypothesis`, already pinned in `requirements/`. Task 1.1 verifies the pins. If anything else seems needed, STOP and ask.
8. **File edits use the file-edit tools.** Create and change repository files with the file-edit tools, never with PowerShell `Set-Content`, `Out-File`, `>` or `Add-Content`. Every repository file uses LF line endings and no BOM. (PowerShell may write scratch output outside the repository, for example `$env:TEMP`.)
9. **Git.** Work on branch `feat/coroner-forensic-ui`. No `git commit`, `git push` or `git merge` unless the user explicitly instructs it at that moment. No `git reset --hard`, `git checkout -- .`, `git clean`, `git stash -u`, forced switch or force push. If the user asks for a commit, stage specific paths by name.
10. **Pytest invocation rule (no false green).** Every task that runs pytest uses `.\.venv\Scripts\python.exe -m pytest <paths> -q -p no:cacheprovider`, states the expected minimum collected and passed count (found first with `--collect-only -q` and recorded), and records `$LASTEXITCODE` with the collected, passed, failed, skipped and deselected counts. Exit code 5 (nothing collected) is a FAILURE. Never pass `--run-docker` and never set `CORONER_RUN_DOCKER`. Do not run the full 4,510-test suite in the first pass; only the optional task 4.11 may do that, and only with the user's approval.
11. **Stop and ask on conflict.** `requirements.md` and `design.md` are authoritative and are not edited by these tasks. If implementation reveals a conflict, gap or infeasibility, STOP, record it, and ask the user. Do not silently deviate. The open items listed at the end of this file are review items, not decisions: do not resolve them silently.
12. **Node is optional.** Detect it with `node --version`. The Node vector test runs only when `node` is on `PATH`, uses no npm package and no `package.json`, and is reported as skipped when `node` is missing. Never report a skipped test as passed.
13. **The real-browser smoke test is mandatory before M1.1 acceptance.** No browser is available to the agent. The user (or the agent using the user's browser) performs the procedure of task 4.7, and only the real observed results, the browser name and version, the run ids and the observed statements are recorded. The agent never writes an expected result as an observed result.
14. **Checkpoints stop for independent review.** Each checkpoint task ends with a report of real results and a stop. The next group does not start until the user has reviewed the group and said to continue.
15. **Long-running processes.** A running Viewer server (needed in tasks 3.8, 4.7 and 4.8) is started with the process tool or by the user, never in a blocking shell call, and is stopped when the check ends. Use a free port, for example `8765`.
16. **Optional tasks.** Sub-tasks marked `*` are optional and are not implemented unless the user opts in. The checkpoint report states which optional property tests were run and which were not. Core tests are never optional.

## Checkpoint Groups

### M1.1-A: Safe local viewer foundation (task 1)

Files to create:

- `coroner/viewer/__init__.py`, `coroner/viewer/__main__.py`, `coroner/viewer/cli.py`, `coroner/viewer/files.py`, `coroner/viewer/hostcheck.py`, `coroner/viewer/app.py`, `coroner/viewer/static/index.html` (minimal shell, finished in task 3.1)
- `tests/unit/viewer/__init__.py`, `tests/unit/viewer/builder.py`, `tests/unit/viewer/test_builder_selftest.py`, `tests/unit/viewer/test_files_containment.py`, `tests/unit/viewer/test_http_guard.py`, `tests/unit/viewer/test_cli.py`, `tests/unit/viewer/test_readonly.py`, `tests/unit/viewer/test_static_rules.py`, `tests/unit/viewer/test_import_boundary.py`
- Optional: `tests/unit/viewer/test_prop_p03_run_id.py`, `test_prop_p04_bounded_read.py`, `test_prop_p16_host_guard.py`, `test_prop_p18_launch_args.py`, `test_prop_p19_readonly.py`
- `docs/m1-results/runs-baseline.md`

Files to modify: `tests/unit/test_structure.py` (test update only).

Acceptance criteria: Requirements 1.1 to 1.11, 2.1 to 2.8, 2.12, 3.15, 10.2, 10.5, 13.1 to 13.17 (route set, 404, 405, Host guard, headers, static allowlist), 16.1 to 16.10, 11.42, 11.45.

Interim state at the end of group A, recorded so the reviewer is not surprised: the API success bodies come from an injected `DataProvider` stub in tests, and `/compare` serves the static shell without query validation. The real loader arrives in group B and the `/compare` query validation in group D (task 4.1). No file access happens on the `/compare` path in the meantime.

Docker-free test command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py -q -p no:cacheprovider
```

### M1.1-B: Evidence API (task 2)

Files to create:

- `coroner/viewer/timefmt.py`, `coroner/viewer/model.py`, `coroner/viewer/samples.py`, `coroner/viewer/timeline.py`, `coroner/viewer/clocks.py`, `coroner/viewer/classify.py`, `coroner/viewer/loader.py`
- `tests/unit/viewer/test_docker_time_parity.py`, `test_tolerance_parity.py`, `test_classification.py`, `test_docker_state.py`, `test_evidence_assembly.py`, `test_timeline.py`, `test_segments_ticks.py`, `test_clocks.py`, `test_discovery.py`, `test_run_order.py`, `test_not_read.py`, `test_cache.py`, `test_error_paths.py`, `test_logging.py`, `test_api_contract.py`, `test_truncation.py`
- Tool (not collected): `tests/unit/viewer/measure_parse_memory.py`
- Optional property files: `tests/unit/viewer/test_prop_p01_selection.py`, `test_prop_p02_cap_independence.py`, `test_prop_p05_allowlist.py`, `test_prop_p06_null.py`, `test_prop_p07_classification.py`, `test_prop_p08_timestamps.py`, `test_prop_p09_correction.py`, `test_prop_p10_ordering.py`, `test_prop_p11_gap_before.py`, `test_prop_p12_last_persisted.py`, `test_prop_p13_docker_state.py`, `test_prop_p14_discovery_order.py`, `test_prop_p15_cache.py`, `test_prop_p20_segments.py`, `test_prop_p21_ticks.py`, `test_prop_p22_docker_time.py`, `test_prop_p23_not_read.py`
- `docs/m1-results/parse-memory.md`

Files to modify: `coroner/viewer/app.py` (wire the real provider), `coroner/viewer/model.py` (serializers, task 2.9).

Acceptance criteria: Requirements 3.1 to 3.18, 5.8 to 5.13, 6.2 to 6.9, 6.11 to 6.12, 6.20, 6.22, 6.23, 6.25, 9.1 to 9.15, 11.2 to 11.5, 11.9, 11.12 to 11.18, 11.24 to 11.31, 11.35 to 11.40, 12.1 to 12.10, 14.1 to 14.12, 15.1 to 15.30 (data side), 11.44.

Docker-free test command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py -q -p no:cacheprovider
```

### M1.1-C: Investigation UI (task 3)

Files to create:

- `coroner/viewer/static/style.css`, `copy.js`, `dom.js`, `api.js`, `app.js`, `overview.js`, `incident.js`, `evidence-panel.js`, `chart.js`, `compare.js` (placeholder replaced in group D)
- `tests/unit/viewer/test_static_ui.py`
- `docs/m1-results/m1.1-acceptance.md` (skeleton and visual review record)

Files to modify: `coroner/viewer/static/index.html` (finish the shell).

Acceptance criteria: Requirements 2.10, 3.6, 3.8, 3.9, 3.12, 3.17, 4.1 to 4.11, 5.1 to 5.13, 6.1, 6.10 to 6.21, 6.23 to 6.25, 7.1 to 7.13, 11.43, 13.14, 15.29. Browser behaviour is established only by the recorded manual review (task 3.8), never by pytest.

Docker-free test command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py -q -p no:cacheprovider
```

### M1.1-D: Comparison and acceptance (task 4)

Files to create:

- `coroner/viewer/compare.py`, `coroner/viewer/static/compare-logic.js`
- `tests/unit/viewer/compare_vectors.json`, `compare_reference.py`, `compare_vectors_runner.mjs`, `test_compare_query.py`, `test_compare_logic.py`, `test_compare_logic_node.py`
- Optional: `tests/unit/viewer/test_prop_p17_compare.py`

Files to modify: `coroner/viewer/app.py` (`/compare` validation), `coroner/viewer/static/compare.js`, `copy.js`, `style.css`, `tests/unit/viewer/test_static_ui.py`, `README.md` (Viewer section), `docs/m1-results/m1.1-acceptance.md`.

Acceptance criteria: Requirements 8.1 to 8.15, 10.1 to 10.6, 11.32 to 11.34, 13.16, 16.11, and the mandatory real-browser record.

Docker-free test command (focused; the full suite is not run in the first pass):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py tests/unit/test_docs.py tests/unit/common/test_timeutil.py tests/unit/evidence/test_reader.py tests/unit/evidence/test_summary.py tests/unit/runner/test_prop_manifest.py -q -p no:cacheprovider
```

## Tasks

- [x] 1. M1.1-A Safe local viewer foundation
  - [x] 1.1 Preflight: branch, pins, runs/ protection, hash baseline, Node detection
    - Record `git branch --show-current` (expected `feat/coroner-forensic-ui`), `git status --short`, `git branch --list master`, `.\.venv\Scripts\python.exe --version`, and `node --version` (a failure means "node not available", which is not an error).
    - Verify the pins exist and are exact: `Select-String -Path requirements\dev.txt -Pattern '^(fastapi|starlette|uvicorn|httpx|pytest|hypothesis)=='`. Verify the installed versions with `.\.venv\Scripts\python.exe -m pip list`. If a package is missing, STOP and ask (Execution Rule 7).
    - Verify `runs/` is ignored and untracked: `git check-ignore -v runs/x/manifest.json` (shows a rule) and `git ls-files runs` (no output). Verify `.hypothesis` is ignored: `git check-ignore -v .hypothesis/x`.
    - **Before-snapshot of the real `runs/` directory (create once, outside the repository).** Write the full per-file listing (relative path, SHA-256, size, sorted by path) to ONE file outside the repository, `$env:TEMP\coroner-m1\runs-before.snapshot.txt`, with create-new semantics: the command first checks `Test-Path -LiteralPath` and STOPS with "snapshot exists; refusing to overwrite" if the file already exists, and opens the file with `[System.IO.File]::Open($snap, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)` so an existing file can never be overwritten or appended to. After writing, mark it read-only (`Set-ItemProperty -LiteralPath $snap -Name IsReadOnly -Value $true`). Suggested command (scratch output outside the repository is allowed by Execution Rule 8):
      `$root = (Resolve-Path runs).Path; $dir = Join-Path $env:TEMP 'coroner-m1'; New-Item -ItemType Directory -Force $dir | Out-Null; $snap = Join-Path $dir 'runs-before.snapshot.txt'; if (Test-Path -LiteralPath $snap) { throw 'snapshot exists; refusing to overwrite' }; $files = Get-ChildItem runs -Recurse -File | Sort-Object FullName; $lines = $files | ForEach-Object { "$($_.FullName.Substring($root.Length + 1)) $((Get-FileHash $_.FullName -Algorithm SHA256).Hash) $($_.Length)" }; $fs = [System.IO.File]::Open($snap, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write); try { $b = [System.Text.Encoding]::ASCII.GetBytes((($lines -join "`n") + "`n")); $fs.Write($b, 0, $b.Length) } finally { $fs.Dispose() }; Set-ItemProperty -LiteralPath $snap -Name IsReadOnly -Value $true; $files.Count; ($files | Measure-Object Length -Sum).Sum; (Get-FileHash -LiteralPath $snap -Algorithm SHA256).Hash`.
    - **What the repository records.** `docs/m1-results/runs-baseline.md` records ONLY: the file count, the total bytes, the aggregate SHA-256 (the SHA-256 of the snapshot file), the snapshot file NAME as a label (`runs-before.snapshot.txt`, no directory path), the date, and the statement that the full listing is kept outside the repository until task 4.8 acceptance. The per-file listing and every scratch file name stay out of the repository and out of every document and report. The SHA-256 of `manifest.json`, `evidence.jsonl` and `docker_inspect.json` of each run directory that has a `manifest.json` may be recorded keyed by run id only.
    - **Snapshot rules (all are completion criteria).** The snapshot is created exactly once. It is never overwritten, never appended to, never moved into the repository and never committed. It lives outside the repository. It is deleted only after task 4.8 acceptance and only with the user's approval (asked in checkpoint 4.14). If the file already exists when task 1.1 starts, STOP and ask the user (it may be a baseline from an earlier attempt that must not be replaced).
    - Files: create `docs/m1-results/runs-baseline.md`.
    - Verify: the commands above; `Test-Path "$env:TEMP\coroner-m1\runs-before.snapshot.txt"` is true; `(Get-Item "$env:TEMP\coroner-m1\runs-before.snapshot.txt").IsReadOnly` is true; `git status --short` shows no snapshot file in the repository. A second run of the create command must refuse (record that it refused, without deleting anything). No pytest in this task.
    - Done when: the real count, bytes and aggregate digest are recorded in `docs/m1-results/runs-baseline.md`, the snapshot exists outside the repository and is read-only, `runs/` is confirmed ignored and untracked, the pins are confirmed, and `node` availability is recorded.
    - _Requirements: 1.5, 10.3, 10.4, 10.5, 10.6_

  - [x] 1.2 Create the `coroner.viewer` package skeleton and the CLI
    - Create `coroner/viewer/__init__.py` (only `VIEWER_VERSION`, nothing heavy imported), `coroner/viewer/__main__.py` (`if __name__ == "__main__": raise SystemExit(main())`, import-safe), and `coroner/viewer/cli.py` with `DEFAULT_RUNS_DIR`, `DEFAULT_HOST`, `DEFAULT_PORT`, `ViewerConfigError`, `ViewerArgs`, `parse_args`, `validate_host` (only the exact string `127.0.0.1`), `validate_port` (ASCII digits only, 1 to 65535, so 80 is valid), `build_server_config(app, args)` with the literal host constant `127.0.0.1` and the uvicorn options of the design, and `main`. `cli.py` must not parse `sys.argv` at import time. In this task `main` validates arguments and the Runs_Directory; the call to `create_app` is wired in task 1.7.
    - Error messages: host rejection prints `error: the Viewer binds only to 127.0.0.1 (got '<repr, cut to 64 characters>')` to stderr and exits 2; an invalid port names the value; a missing or non-directory Runs_Directory names the path as given. `argparse` usage errors exit 2.
    - Files: `coroner/viewer/__init__.py`, `coroner/viewer/__main__.py`, `coroner/viewer/cli.py`.
    - Verify: `.\.venv\Scripts\python.exe -c "import coroner.viewer, coroner.viewer.cli, coroner.viewer.__main__; print('ok')"`.
    - Done when: the import check succeeds and the import has no side effects.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.8, 1.9, 1.11, 3.9, 13.17_

  - [x] 1.3 Create the fixture builder and its self-test
    - Create `tests/unit/viewer/__init__.py` and `tests/unit/viewer/builder.py` with `RunsBuilder(root)`, `add_run(...)` (parameters as in the design: experiment, run_id, outcome, samples, gaps, extra_records, container_state, inspect, manifest_extra, clock_offset_ms, uncertainty_ms, sample_timing, evidence_bytes, manifest_bytes, start_time), `add_loose_file`, `add_empty_dir`, `try_symlink` (returns `False` when the platform cannot create the link), and `snapshot()` (name to `(size, sha256)`). Manifests are written with the M0 `serialize_manifest` so they pass `deserialize_manifest` by construction. Evidence lines are written by hand in the Collector envelope. The raw inspect document is a template list with one object (64-hex `Id`, `State`, `HostConfig`, a nine-digit `FinishedAt`).
    - Create `tests/unit/viewer/test_builder_selftest.py`: the builder output passes `schema.validate_record`, `read_evidence` returns every line, `deserialize_manifest` accepts every manifest, and `snapshot()` is stable.
    - The builder never reads the real `runs/` directory.
    - Files: `tests/unit/viewer/__init__.py`, `tests/unit/viewer/builder.py`, `tests/unit/viewer/test_builder_selftest.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_builder_selftest.py --collect-only -q -p no:cacheprovider` (record the count), then `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_builder_selftest.py -q -p no:cacheprovider`.
    - Done when: exit code 0 and the passed count equals the collected count (at least 4).
    - _Requirements: 11.1, 11.5, 11.7_

  - [x] 1.4 Implement `coroner/viewer/files.py` (containment and bounded read)
    - Implement `ARTIFACT_LIMIT` (16,777,216), `READ_LIMIT`, `READ_CHUNK`, `ArtifactName`, `ArtifactFault`, `ArtifactStatus` (six values; `NOT_READ` is assigned by `loader.py` only), `STATUS_OF`, `RunsRoot`, `RunEntry`, `ArtifactRead`, the `ArtifactIO` protocol with `DEFAULT_IO` (the test seam: scandir, lstat, open, fstat, read, close, realpath), `open_runs_root`, `list_run_entries`, `find_run` and `read_artifact`, following the containment algorithm of the design exactly: scandir exact-name matching (the request id is never joined into a path), reparse-point and symlink rejection (`FILE_ATTRIBUTE_REPARSE_POINT` on Windows), exact-case artifact name matching, pre-open `lstat`, `realpath` containment with a separator boundary, early `stat` size rejection that never replaces the bounded read, read-only no-follow `os.open` flags via `getattr(os, flag, 0)`, post-open `fstat` regular-file and identity checks (`st_dev`, `st_ino`; fallback to size and mtime when the inode is 0), second `realpath` comparison, chunked read of at most `READ_LIMIT` bytes in total, `too_large` handling that drops the bytes, the cache signature `(size, mtime_ns, dev, ino)`, and `OSError` handling that discards the message and filename.
    - The module imports only the standard library and `coroner.runner.manifest.validate_run_id_for_path` / `RunIdError`. It performs no write, no `subprocess`, no `socket`.
    - Files: `coroner/viewer/files.py`.
    - Verify: `.\.venv\Scripts\python.exe -c "import coroner.viewer.files as f; print(f.ARTIFACT_LIMIT, f.READ_LIMIT)"` prints `16777216 16777217`. Behaviour is tested in tasks 1.9 and 1.13 to 1.14.
    - Done when: the module imports and the constants are correct.
    - _Requirements: 2.2, 2.6, 2.7, 2.8, 3.12, 3.15, 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.8, 16.9, 16.10_

  - [x] 1.5 Implement `coroner/viewer/hostcheck.py` (Host guard, security headers, method guard)
    - Implement the pure function `host_allowed(host_header, port)`: exactly `127.0.0.1:<port>` or `localhost:<port>` with the canonical decimal port, plus the two hostnames without a port only when `port == 80`, compared case-sensitively and byte for byte, with no parsing. Implement the pure ASGI classes `HostGuard(app, port)` (zero or repeated Host header gives 400 `missing_host`; a header that fails `host_allowed` gives 421 `misdirected_host`; the value is decoded as `latin-1`), `SecurityHeaders` (`Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin`, and for `text/html` the CSP and `X-Frame-Options: DENY` of the design; removes any `Access-Control-*` header and never adds one), and the small `MethodGuard` (405 with `Allow: GET`), enabled only if the framework behaviour test of task 1.10 fails.
    - Error bodies are `{"error":{"code":"<stable>","message":"<fixed text>"}}` for API paths and one line of `text/plain` for UI paths, and never echo the request value.
    - Files: `coroner/viewer/hostcheck.py`.
    - Verify: `.\.venv\Scripts\python.exe -c "from coroner.viewer.hostcheck import host_allowed as h; print(h('localhost:8765', 8765), h('localhost', 8765), h('localhost', 80), h('[::1]:80', 80))"` prints `True False True False`.
    - Done when: the check prints the expected values.
    - _Requirements: 1.10, 1.11, 13.5, 13.9, 13.10, 13.12, 13.13, 13.14_

  - [x] 1.6 Implement `coroner/viewer/app.py` (factory, routes, static allowlist, error handlers) and the minimal shell
    - Implement `create_app(runs_dir, port, *, runs_dir_label=None, io=DEFAULT_IO, cache=None, clock_ns=time.time_ns, provider=None)` and `make_runs_dir_label`. The app uses `FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)`, no CORS middleware, no `StaticFiles`, middleware `SecurityHeaders` outermost and `HostGuard(port)` inside it.
    - Routes (plain `def` handlers): `GET /`, `GET /runs/{run_id:path}`, `GET /compare` (static shell only in this group, no file access), `GET /static/{asset:path}` (in-memory dictionary built at start-up from `os.scandir` of the package `static/` directory, names matching `^[a-z0-9][a-z0-9._-]*\.(html|js|css)$`, fixed content types), `GET /api/runs`, `GET /api/runs/{run_id:path}`. `validate_run_id_for_path` gives 400 `invalid_run_id` with no file access; a valid id that `find_run` does not find gives 404 `not_found`. API success bodies come from an injected `DataProvider` protocol (`list_runs()`, `load_detail(run_id)`) whose real implementation arrives in task 2.10; tests inject a stub.
    - Add a catch-all `Exception` handler and a detail-handler `try/except` that return `500 {"error":{"code":"internal_error","message":"Unexpected error."}}` with no exception text, path or traceback, and log only the exception class name and the run id.
    - Create a minimal valid `coroner/viewer/static/index.html` (one `<main id="main">`, one module script `/static/app.js`, no inline script or style). Task 3.1 finishes it.
    - Files: `coroner/viewer/app.py`, `coroner/viewer/static/index.html`.
    - Verify: `.\.venv\Scripts\python.exe -c "from coroner.viewer.app import create_app; import tempfile; a = create_app(tempfile.mkdtemp(), 8765); print(sorted(r.path for r in a.routes))"` lists exactly the six routes (plus no docs routes).
    - Done when: the app builds with exactly the registered route set of Requirement 13 criteria 1 and 2.
    - _Requirements: 2.1, 2.3, 2.4, 2.5, 2.12, 9.10, 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8, 13.11, 13.12, 13.13, 13.14, 13.15_

  - [x] 1.7 Wire `cli.main` to the app and uvicorn
    - `main` builds the app with `create_app`, builds the server with `build_server_config` (host constant `127.0.0.1`, `workers=1`, `reload=False`, `log_level="warning"`, `access_log=False`, `lifespan="off"`, `server_header=False`, `proxy_headers=False`, `ws="none"`, `h11_max_incomplete_event_size=16384`, `timeout_keep_alive=5`, `limit_concurrency=64`), prints the start line `Coroner Viewer: http://127.0.0.1:<port>/  (runs: <label>, read-only)` to stdout, and runs `uvicorn.Server(...).run()`. `uvicorn` is imported only in `cli.py`. No code path binds `localhost` or `::1`.
    - Files: `coroner/viewer/cli.py`.
    - Verify: `.\.venv\Scripts\python.exe -m coroner.viewer --host 0.0.0.0` exits with code 2 and the stderr message; `.\.venv\Scripts\python.exe -m coroner.viewer --port 70000` exits 2 naming `70000`; `.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir does-not-exist-xyz` exits 2 naming the path. Record each exit code. Do not start a server in this task.
    - Done when: the three failure invocations exit with code 2 and the expected messages.
    - _Requirements: 1.1, 1.3, 1.4, 1.8, 1.9, 1.11, 3.9, 16.10_

  - [x] 1.8 Update `tests/unit/test_structure.py` and add the module-level import boundary test
    - This is a TEST update; M0 production code stays unchanged. In `tests/unit/test_structure.py`: add `"viewer"` to `PACKAGES` and `"coroner.viewer"` to `IMPORTABLE`; set `ALLOWED_COMPONENTS["viewer"] = frozenset({"evidence", "common", "runner"})`; make `third_party_for("viewer")` return the pins of `requirements/collector.txt`; keep `viewer` out of `STDLIB_ONLY`; add detection cases for `viewer` to `test_checker_detects_prohibited_imports` (`import coroner.collector`, `from coroner.probe import x`, `import docker`, `import yaml`, `from coroner.runner.experiments.base import parse_docker_time`) and acceptance cases to `test_checker_accepts_allowed_imports` (`import fastapi`, `from coroner.evidence import reader`). Read the file first and follow its conventions.
    - Create `tests/unit/viewer/test_import_boundary.py` enforcing the module-level rules with the same AST helper `imported_modules`: from `runner` only `runner.manifest`; no `runner.experiments.base`; `uvicorn` only in `cli.py`; none of `subprocess`, `socket`, `urllib`, `http.client`, `shutil`, `ctypes`, `multiprocessing`; `fastapi`/`starlette` only in `app.py` and `hostcheck.py`; the checker detects each violation on synthetic source. No test asserts `subprocess` is absent from `sys.modules`.
    - Files: `tests/unit/test_structure.py`, `tests/unit/viewer/test_import_boundary.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_structure.py tests/unit/viewer/test_import_boundary.py --collect-only -q -p no:cacheprovider` (record the count), then the same without `--collect-only`. Expected: exit code 0, every previously existing structure test still passes, and the passed count is at least the pre-change count of `test_structure.py` plus 4.
    - Done when: real counts are recorded and the fresh-interpreter import test imports `coroner.viewer.__main__` successfully.
    - _Requirements: 1.5, 1.6, 1.7, 10.3, 11.11_

  - [x] 1.9 Write the file-access tests (containment, bounds, listing entries)
    - `tests/unit/viewer/test_files_containment.py`: exact-limit (16,777,216) and limit-plus-one artifacts for each of the three names, using the IO seam for the size cases; the growth case with a fake IO whose `read` yields more bytes than `stat` reported (at most 16,777,217 bytes requested, `too_large`, nothing parsed); link, junction (Windows) and reparse-point entries as run directory and as artifact (skip when not creatable and record the skip); non-regular entry; identity-changed swap through a fake `lstat`/`fstat`; path outside the root; `runs-evil` sibling is not "inside" `runs`; an `OSError` whose text holds a path yields `unreadable` with no path in `reason`; exact-case name matching (`MANIFEST.JSON` does not match); reserved names (`con`) match only by scandir name; listing of loose files, directories with and without `manifest.json`, a `manifest.json` that is a directory (still counted as present), and an empty Runs_Directory.
    - No test reads a file outside `tmp_path`.
    - Files: `tests/unit/viewer/test_files_containment.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_files_containment.py --collect-only -q -p no:cacheprovider` (record the count), then run it. Record the skipped link/reparse tests as skipped, not passed.
    - Done when: exit code 0, passed plus skipped equals collected, and skips are explained.
    - _Requirements: 2.2, 2.6, 2.8, 3.1, 3.2, 3.15, 11.2, 11.6, 11.15, 11.22, 11.30, 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.9_

  - [x] 1.10 Write the HTTP guard and route-surface tests
    - `tests/unit/viewer/test_http_guard.py` with a stub provider and `TestClient` (set the `Host` header explicitly on every request): the Host matrix for a port other than 80 (accepted `localhost:<port>`, `127.0.0.1:<port>`; rejected `[::1]:<port>`, `localhost`, `127.0.0.1`, `localhost:<other>`, `127.0.0.1.evil.test:<port>`, `LOCALHOST:<port>`, repeated, missing); the port-80 matrix through `host_allowed(h, 80)` and through the guard on a stub ASGI app (accepted `localhost`, `127.0.0.1`, `localhost:80`, `127.0.0.1:80`; rejected `localhost:8080`, `[::1]`); no test binds port 80; `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` and `HEAD` return 405 with `Allow: GET` on every registered route (if the pinned framework answers differently, enable `MethodGuard` and record it); `/docs`, `/redoc`, `/openapi.json` and an unregistered path return 404; `Cache-Control: no-store` on every response, `nosniff` on API responses, the CSP and `X-Frame-Options` on HTML, no `Access-Control-*` header; the registered route set equals Requirement 13 criteria 1 and 2; static allowlist and traversal names (`..`, `a/b`, `a\b`, `C:`) return 404; invalid ids (`..`, `a%2Fb`, overlong, trailing newline, empty) return 400 with zero IO calls on a spy IO; a valid unknown id returns 404; a stub-provider exception returns 500 `internal_error` with no path or traceback and the next request succeeds.
    - Files: `tests/unit/viewer/test_http_guard.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_http_guard.py --collect-only -q -p no:cacheprovider` (record the count), then run it.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 1.10, 1.11, 2.3, 2.4, 2.5, 2.12, 9.10, 11.8, 11.19, 11.20, 11.21, 11.23, 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8, 13.9, 13.10, 13.11, 13.12, 13.13, 13.14, 13.15_

  - [x] 1.11 Write the CLI tests
    - `tests/unit/viewer/test_cli.py`: only the exact string `127.0.0.1` is accepted as host (`localhost`, `::1`, `0.0.0.0`, `127.0.0.2`, empty, trailing space are rejected with exit 2 and the message); port validation (`80` valid; `0`, `65536`, `+80`, `0x50`, `8765\n`, `８０８０`, empty rejected naming the value); defaults; missing Runs_Directory message names the path; `build_server_config` has host `127.0.0.1` for every valid port including 80 and the documented options; the Runs_Directory_Label for relative (as supplied) and absolute (final component only, drive and UNC forms, root gives `(runs directory)`).
    - **Bind configuration test with NO network socket (resolved review item).** The guarded unit-test suite contains no real-socket test: no thread starts a server, nothing calls `bind`, `listen`, `getsockname`, `connect` or `getaddrinfo`. Instead the test replaces `coroner.viewer.cli.uvicorn.Server` (monkeypatch) with a spy class that stores the `uvicorn.Config` it receives and whose `run()` does nothing, then calls `main([...])` with valid arguments and `build_server_config(...)`. It asserts: `config.host == "127.0.0.1"` (exact string) for ports 8765 and 80; `config.port` equals the validated port; the host the CLI passes is never `localhost`, `0.0.0.0` or `::1` (also checked with `--host localhost`, which exits 2 before any server object exists, so the spy records no `Server` call); `config.workers == 1`, `reload` false and the other documented options; the spy's `run()` was called exactly once for a valid launch and zero times for a rejected one. The port-80 case only checks the configuration (no bind).
    - The real bind, the HTTP request to `http://127.0.0.1:8765` and the Host-header checks against a running server are NOT part of this task; they are manual acceptance checks in task 4.8 and are recorded as manual in the acceptance ledger (task 4.12).
    - Files: `tests/unit/viewer/test_cli.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_cli.py --collect-only -q -p no:cacheprovider` (record the count), then run it. Also confirm by source inspection that the file contains no `socket`, `bind(`, `getsockname` or `Thread` reference: `Select-String -Path tests\unit\viewer\test_cli.py -Pattern 'socket|bind\(|getsockname|Thread|connect\('` prints nothing.
    - Done when: exit code 0, passed equals collected (no skip is acceptable for the bind test), and the source check prints nothing.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.8, 1.9, 1.11, 3.9, 11.10, 11.23, 11.30, 11.45, 13.17_

  - [x] 1.12 Write the read-only test and the static read-only scan
    - `tests/unit/viewer/test_readonly.py`: a snapshot (`RunsBuilder.snapshot()`) of names, sizes and SHA-256 hashes of a fixture Runs_Directory is identical before and after a mix of GET requests (valid and invalid ids, static names, compare shell, API with the stub provider) and no file is created; the request mix also exercises `list_run_entries`, `find_run` and `read_artifact` directly.
    - `tests/unit/viewer/test_static_rules.py` (Python part): AST and text scans of `coroner/viewer/*.py`: no `open(` with a mode containing `w`, `a`, `x` or `+`; no `os.remove`, `os.unlink`, `os.rename`, `os.replace`, `os.rmdir`, `os.mkdir`, `os.makedirs`, `os.chmod`, `os.utime`, `shutil`, `tempfile`; no `subprocess`, `os.system`, `os.popen`, `os.exec*`, `os.spawn*`, `os.posix_spawn*`, `socket`, `urllib`, `http.client`; `os.open` is called only with read-only flags; no call to `read_manifest`, `build_summary`, or the path form of `read_evidence`; no module imports `coroner.runner.experiments.base` or `coroner.runner.evaluate`.
    - **Docker rule (revised): ban operations and imports, not the word.** The Viewer shows Docker evidence, so its source legitimately contains the word `docker` and field or artifact names such as `docker_state`, `docker_inspect.json` and `container_state`; the scan MUST NOT flag them. The scan MUST flag (AST for imports and calls, text for strings): an import of a Docker client module (top-level name `docker`, `docker-py`, `python_on_whales`, `dockerpty`); any use of `subprocess`, `os.system`, `os.popen`, `os.exec*`, `os.spawn*`; a call `shutil.which` with the argument `docker`; a Docker CLI command string or argument list passed to an execution function (for example `docker compose ...`, `docker run ...`, `docker kill ...`); the container-control operations `compose up`, `compose down`, `kill`, `stop`, `rm` as Docker arguments; the path `/var/run/docker.sock` (and the Windows pipe name `docker_engine`); and any read of `DOCKER_HOST` (`os.environ["DOCKER_HOST"]`, `os.getenv("DOCKER_HOST")`, `os.environ.get("DOCKER_HOST")`).
    - **Negative controls (all asserted).** A synthetic source `import docker` is detected; `from docker import APIClient` is detected; `import subprocess` and `subprocess.run(["docker", "kill", "x"])` are detected; `os.system("docker ps")` is detected; `shutil.which("docker")` is detected; a string `"/var/run/docker.sock"` is detected; `os.getenv("DOCKER_HOST")` is detected. A synthetic source that only contains `docker_state`, `"docker_inspect.json"`, `container_state`, a comment mentioning Docker and a label "Docker state" is NOT flagged. The scan is re-run in later checkpoints as new modules appear.
    - Files: `tests/unit/viewer/test_readonly.py`, `tests/unit/viewer/test_static_rules.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_readonly.py tests/unit/viewer/test_static_rules.py --collect-only -q -p no:cacheprovider` (record the count), then run both.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 2.1, 2.2, 3.3, 10.2, 10.5, 11.7, 11.8, 11.11, 11.42_

  - [ ]* 1.13 Write property test for run id containment
    - **Property 3: Run ids cannot escape the Runs_Directory**
    - **Validates: Requirements 2.3, 2.4, 2.7, 13.8, 13.15, 16.1**
    - File: `tests/unit/viewer/test_prop_p03_run_id.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p03_run_id.py -q -p no:cacheprovider`.

  - [ ]* 1.14 Write property test for bounded reads
    - **Property 4: Bounded reads never exceed the limit and never parse an oversized artifact**
    - **Validates: Requirements 2.8, 3.12, 3.15, 11.15, 11.22, 16.5, 16.6, 16.7, 16.8**
    - File: `tests/unit/viewer/test_prop_p04_bounded_read.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p04_bounded_read.py -q -p no:cacheprovider`.

  - [ ]* 1.15 Write property test for the request guard
    - **Property 16: The request guard is exact and total**
    - **Validates: Requirements 1.10, 1.11, 11.19, 11.20, 11.23, 13.3, 13.5, 13.6, 13.7, 13.9, 13.10, 13.12, 13.13**
    - File: `tests/unit/viewer/test_prop_p16_host_guard.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p16_host_guard.py -q -p no:cacheprovider`.

  - [ ]* 1.16 Write property test for launch argument validation
    - **Property 18: Launch arguments are validated exactly**
    - **Validates: Requirements 1.3, 1.8, 1.9, 1.11, 11.10, 13.17**
    - File: `tests/unit/viewer/test_prop_p18_launch_args.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p18_launch_args.py -q -p no:cacheprovider`.

  - [ ]* 1.17 Write property test that requests never change the Runs_Directory
    - **Property 19: Requests never change the Runs_Directory**
    - **Validates: Requirements 2.2, 10.5, 11.7**
    - File: `tests/unit/viewer/test_prop_p19_readonly.py`. In group A this runs against the stub provider and the file layer; task 2.18 repeats the read-only check end to end against the real loader. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p19_readonly.py -q -p no:cacheprovider`.

  - [x] 1.18 Checkpoint - review before continuing (M1.1-A)
    - Run `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py -q -p no:cacheprovider` (determine the collected count with `--collect-only -q` first; exit code 5 is a FAILURE). Record `$LASTEXITCODE` and the real collected, passed, failed, skipped and deselected counts, which optional property tests were run and which were not, and every skipped test with its reason.
    - Re-run the hash check of task 1.1 only if the user asks; no group A test touches `runs/`.
    - Report: the files created and modified (`git status --short`), the test results, and the open items of the section "Open items for review" below, especially (R-A) to (R-G) and the interim state (stub provider, `/compare` shell without validation).
    - **Independent review by the user is required before M1.1-B starts. STOP here and wait for the user's decision. Report real results, never predicted results. Do not commit.**

- [x] 2. M1.1-B Evidence API
  - [x] 2.1 Implement `coroner/viewer/timefmt.py` and the Docker time parity test
    - Implement `display_utc_ms`, `display_docker`, `epoch_ms_utc`, `epoch_ms_docker` (floor to milliseconds), `epoch_ms_to_text` (`EPOCH + timedelta(milliseconds=ms)` then `format_utc_ms`, exact integer arithmetic) and the local `parse_docker_time` (the Docker_Time_Parser): `fullmatch` of `([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z`; non-`str` gives `None`; `year <= 1` gives `None`; microseconds `int(((frac or "") + "000000")[:6])`; a `datetime` `ValueError` gives `None`; result is a UTC-aware `datetime`. Use only the public `parse_utc_ms` and `format_utc_ms`; never `_fmt`, never `datetime.fromisoformat`. The module never imports `coroner.runner.experiments.base`.
    - Create `tests/unit/viewer/test_docker_time_parity.py` (the only Viewer test that imports `coroner.runner.experiments.base`): fixed examples (nanosecond, microsecond, millisecond, no-fraction, the zero date `0001-01-01T00:00:00Z`, a non-UTC offset, lower-case `t` and `z`, a trailing newline, malformed strings, non-`str` values) and a seeded generated-string loop, asserting equal results or equal exception types; and a source-inspection test that no `coroner.viewer` module imports the M0 module.
    - Files: `coroner/viewer/timefmt.py`, `tests/unit/viewer/test_docker_time_parity.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_docker_time_parity.py --collect-only -q -p no:cacheprovider` (record the count), then run it.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 3.3, 9.12, 11.31, 12.9, 12.10, 14.1, 15.3_

  - [ ]* 2.2 Write property test for the Docker_Time_Parser parity
    - **Property 22: The local Docker_Time_Parser equals the M0 `parse_docker_time`**
    - **Validates: Requirements 3.3, 11.31, 12.10, 14.1**
    - File: `tests/unit/viewer/test_prop_p22_docker_time.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p22_docker_time.py -q -p no:cacheprovider`.

  - [x] 2.3 Implement `coroner/viewer/model.py` data classes, null helpers and JSON dump
    - Implement the null-policy helpers `safe_int` (an `int` that is not a `bool`, `abs(v) <= 2**53 - 1`), `safe_nonneg_int`, `safe_number` (finite `int` or `float`), `safe_bool`, `safe_str`, `capped(value, cap)` (returns the value with `<f>_truncated` and `<f>_original_length` semantics from the design; cut by code points), `dump_json` (`ensure_ascii=True`, `allow_nan=False`, compact separators, ASCII bytes), `assert_allowlisted(obj, schema)`, and the internal dataclasses used by the loader (`RunRecord`, `ListFacts`, `ArtifactState` and the types the loader and serializers share). Serializers (`to_list_item`, `to_detail`, `to_listing`) are added in task 2.9.
    - Files: `coroner/viewer/model.py`.
    - Verify: `.\.venv\Scripts\python.exe -c "from coroner.viewer.model import safe_int, safe_number, capped, dump_json; print(safe_int(True), safe_int(2**60), safe_number(float('nan')), dump_json({'a': '\u2028'}))"` prints `None None None b'{"a":"\\u2028"}'`.
    - Done when: the check prints the expected values.
    - _Requirements: 2.9, 2.11, 9.7, 9.8, 9.14, 9.15, 12.2, 12.3_

  - [x] 2.4 Implement `coroner/viewer/samples.py`
    - Implement `SampleRow`, `extract_samples(result)` (file order, `file_pos`), `is_plottable`, `capture_order` (key `(ts_ms, seq, file_pos)`), `last_persisted` (last `sample` record in file order), `latest_capture` (maximum `ts_ms`, earliest file position on ties), and `rss_stats` (first in Capture order, highest with the earliest tie, last as the Last_Persisted_Sample when plottable, otherwise the last plottable row in file order, with `basis` `last_persisted` or `last_plottable_in_file_order`). Values come only from `ReadResult.records`; `summarize().last_sample_time` is never used as either Sample.
    - Files: `coroner/viewer/samples.py`.
    - Verify: `.\.venv\Scripts\python.exe -c "import coroner.viewer.samples as s; print(s.SampleRow.__name__)"`. Behaviour is tested in tasks 2.13 and 2.28.
    - Done when: the module imports.
    - _Requirements: 5.12, 6.12, 9.11, 12.5, 12.6, 12.8_

  - [x] 2.5 Implement `coroner/viewer/timeline.py`
    - Implement `CHART_CAP = 5000`, `select_points` (the integer-arithmetic stride plus forced peak of the design), `gap_index` (sorted merged sequence intervals, silence file positions, `unknown_range_drop_present`), `segment_styles` (`solid`, `dashed`, `none` with `omitted_before` and `skipped_before`), `nice_ticks` (the step list of the design, 1 to 7 ticks), `x_axis` (domain and tick labels from `epoch_ms_to_text`), and `build_chart(rows, gaps, mode, offset_ms, cap, sigkill_x_ms)` which computes every full-run statistic before selection so `cap` never feeds a statistic, sets `partial_view` to exactly `plottable_samples > cap`, and falls back to `raw_uncorrected` with reason `correction_out_of_range` on a datetime overflow.
    - Files: `coroner/viewer/timeline.py`.
    - Verify: `.\.venv\Scripts\python.exe -c "from coroner.viewer.timeline import nice_ticks; print(nice_ticks(0, 10000))"` prints a strictly increasing list of 2 to 7 integers.
    - Done when: the check prints a valid list.
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.7, 6.8, 6.9, 6.10, 6.11, 6.22, 6.23, 6.25, 9.3, 9.4, 9.5_

  - [x] 2.6 Implement `coroner/viewer/clocks.py`
    - Implement `OffsetResolution`, `resolve_offset` (Experiment B only; `safe_int` on `sample_timing.clock_offset_ms` and top-level `clock_offset_ms`; both present and different is a conflict with `offset_ms` `None` and both values returned; one present is used; equal values use the `sample_timing` value), `axis_mode`, `corrected_ms` (`ts_ms - offset_ms`, Receive_Time never shifted), `ordering` (the pseudocode of the design: uncertainty `None` gives no difference and no ordering; tolerance `None` or `abs(diff) <= max(uncertainty, tolerance)` gives `not_established`), and `derived_intervals` (Experiment B only: `sample_to_sigkill` and `sample_to_finished_at`, both requiring a usable offset and using the Corrected_Capture_Time with `from_basis` `corrected`, and `finished_at_to_sigkill`; Docker-target kinds always `ordering` `None`, `bounded` false, reason `no_recorded_bound_for_docker_clock`; **Experiment A, Experiment C and unknown types produce NO interval: `intervals` is an empty list and `suppressed` is false** (R-C resolved); conflict gives `suppressed` true and an empty list; `finished_at_source` manifest first, then raw truncated to ms).
    - **Tolerance rule (R-E resolved).** Define the module constant `VIEWER_CLOCK_TOLERANCE_MAX_MS = 250` with a comment stating it mirrors M0 `CLOCK_TOLERANCE_MS` and is never imported from M0, and `tolerance_status(recorded)`: `valid` only for a Python `int` that is not a `bool` and lies from 0 to 250 inclusive; `not_recorded` when the key is missing or `None`; `invalid_type` for a float, bool, string, list or object; `out_of_range` for an int below 0 or above 250. The recorded value is returned unchanged (never clamped, rounded or replaced). When the status is not `valid`, `ordering` returns `None` with the reason `tolerance_not_recorded`, `tolerance_invalid_type` or `tolerance_out_of_range`, so no ordering is stated for any interval of that run; the `difference_ms` is still returned and labelled derived. `CLOCK_TOLERANCE_MS` and `classify_b_samples` are not imported, and `coroner.runner.evaluate` is not imported (Requirement 12 criterion 10 reuse list unchanged).
    - Files: `coroner/viewer/clocks.py`.
    - Verify: `.\.venv\Scripts\python.exe -c "from coroner.viewer.clocks import ordering, tolerance_status; print(tolerance_status(250), tolerance_status(251), tolerance_status(True), tolerance_status(250.0), tolerance_status(None))"` prints `valid out_of_range invalid_type invalid_type not_recorded`; `ordering(-1420, 12.5, 250, 'valid')` shows a before-SIGKILL ordering; `ordering(100, 12.5, 250, 'valid')` shows `not_established`; `ordering(100, None, 250, 'valid')` shows no difference and no ordering; `ordering(-1420, 12.5, 251, 'out_of_range')` shows no ordering and the reason `tolerance_out_of_range`.
    - Done when: the checks print the expected results.
    - _Requirements: 5.13, 7.3, 12.10, 15.5, 15.6, 15.7, 15.10, 15.11, 15.12, 15.13, 15.17, 15.19, 15.20, 15.21, 15.26, 15.27, 15.28, 15.29, 15.30_

  - [x] 2.7 Implement `coroner/viewer/classify.py`
    - Implement `experiment_type` (`experiment.upper()` in `A`, `B`, `C`, else `None`; never inferred from ExitCode, OOMKilled or run id), `verdict_of` (`passed` to PASS, `failed` to FAIL, anything else UNKNOWN), and `resolve_docker_state` returning the five `source` codes of the design, `raw_unavailable_reason` (`missing`, `unreadable`, `invalid_json`, `rejected`, `too_large`; `invalid_json` versus `rejected` decided from `InspectError.__cause__` being a `ValueError`, never from message text), the six compared fields with the equality rules (the `finished_at` comparison at millisecond truncation, the zero value and unparseable values as not recorded), the per-field `result` values, the `effective` values (manifest first, then raw, never a default) and `memory_limit_kind` (`none_configured` for 0, `bytes` for a positive int). No inference of OOMKilled from RSS, limit or ExitCode.
    - Files: `coroner/viewer/classify.py`.
    - Verify: `.\.venv\Scripts\python.exe -c "from coroner.viewer.classify import experiment_type, verdict_of; print(experiment_type('b'), experiment_type('X'), verdict_of('aborted'))"` prints `B None UNKNOWN`.
    - Done when: the check prints the expected values.
    - _Requirements: 4.11, 11.3, 11.4, 11.18, 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8, 14.9, 14.10, 14.11, 14.12_

  - [x] 2.8 Implement `coroner/viewer/loader.py`
    - Implement artifact bytes to `RunRecord`: `deserialize_manifest` on bounded bytes (catching `RecursionError` and any other exception as `manifest_invalid`), the `run_id` mismatch rule (manifest entry `invalid` with reason `run_id_mismatch`), the `not_read` rule (an unusable manifest means `read_artifact` is never called for `evidence.jsonl` and `docker_inspect.json`; both get `not_read` with reason `manifest_unusable`), `read_evidence(data, name=f"{run_id}.jsonl")` plus `summarize`, `extract_inspect_excerpt` plus the local Docker parse for raw `FinishedAt`, the `record_count_mismatch` warning, `total_samples` equal to `summarize` `sample_count`, and every status taken from `ArtifactStatus`.
    - Listing: `load_listing` in Run_Order (valid `start_time` descending, equal instants and missing or unparseable `start_time` by `run_id` descending as plain strings, the two groups in that order), the 500-run cap (`total_found`, `listed`, `truncated`, `max_listed`), a per-run `try/except Exception` that turns one failure into an UNKNOWN entry with reason `internal_error`, the sort-key cache and the list-facts cache keyed by `(size, mtime_ns, dev, ino)` signatures with the "racily clean" 2-second rule and the no-change-during-load rule, `RunCache` with 1,024 LRU entries, and `threading.BoundedSemaphore(2)` around read plus parse. The detail path is not cached.
    - The module docstring states the stat-signature cache limitation in plain words (a rewrite that keeps the same size, modification time and file identity can be served from the cache; a restart discards the cache) and never claims the cache cannot be stale (open item R-F).
    - Files: `coroner/viewer/loader.py`.
    - Verify: `.\.venv\Scripts\python.exe -c "import coroner.viewer.loader as l; print(l.MAX_LISTED_RUNS)"` prints `500`. Behaviour is tested in tasks 2.16 to 2.18.
    - Done when: the module imports and the docstring contains the limitation text.
    - _Requirements: 3.1, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.11, 3.12, 3.13, 3.14, 3.16, 3.18, 5.8, 5.11, 12.4, 12.5, 16.7, 16.11_

  - [x] 2.9 Implement the explicit allowlist serializers in `coroner/viewer/model.py`
    - Implement `to_list_item`, `to_detail` and `to_listing` that name every key explicitly, with the nine top-level detail blocks (`run`, `docker_state`, `timing`, `termination_markers`, `evidence_summary`, `chart`, `runner_text`, `artifact_status`, `warnings`), one `ALLOWED_KEYS` per response object (exactly the field tables of the design), the nine capped free-text fields and list caps with `_total` and `_truncated`, `*_display` and `*_clock` siblings (`container`, `collector`, `host`, `docker`; `collector_minus_host` for the offset and uncertainty), `runner_text`, `warnings` (Viewer codes `record_count_mismatch` and `artifact_<name>_<status>`; the `not_read` text is "not read (manifest unusable)"), `artifact_status` (six values, stable reason codes, `observed_size_text` "more than 16,777,216 bytes" and `limit_bytes` only for `too_large`), and `assert_allowlisted` before every `dump_json`. The `timing` block returns `tolerance_ms` exactly as recorded plus `tolerance_status` (`valid`, `not_recorded`, `invalid_type`, `out_of_range`) and, for Experiment A, C and unknown types, `derived` with an empty `intervals` list. No `raw_inspect_path`, `host_copy_path`, `container_path`, `docker_inspect_path` or absolute path is ever returned; the Runs_Directory appears only as `runs_dir_label`.
    - Files: `coroner/viewer/model.py`.
    - Verify: `.\.venv\Scripts\python.exe -c "import coroner.viewer.model as m; print(sorted(m.DETAIL_TOP_LEVEL_KEYS))"` prints the nine block names.
    - Done when: the nine keys print and the module imports.
    - _Requirements: 2.9, 2.11, 3.17, 9.1, 9.2, 9.3, 9.4, 9.6, 9.9, 9.11, 9.12, 9.13, 9.14, 9.15, 12.9_

  - [x] 2.10 Wire the real provider into `coroner/viewer/app.py`
    - Replace the default stub with the real provider built on `loader.py`: `GET /api/runs` returns `to_listing`; `GET /api/runs/{run_id}` validates, calls `find_run`, loads, serializes with `dump_json` and returns `Response(content=bytes, media_type="application/json")` (no `response_model`). The 400, 404, 405 and 500 behaviour from task 1.6 is unchanged. `/compare` is still the static shell (group D).
    - Files: `coroner/viewer/app.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_http_guard.py tests/unit/viewer/test_readonly.py -q -p no:cacheprovider` still passes (record counts).
    - Done when: the group A tests still pass with the real provider as default and the injected stub still works.
    - _Requirements: 2.12, 3.9, 3.18, 9.1, 9.2, 9.10_

  - [x] 2.11 Measure parse memory for a 16 MiB evidence file and record it
    - Create the tool `tests/unit/viewer/measure_parse_memory.py` (not collected by pytest; the name does not start with `test_`). It builds a synthetic evidence file of exactly 16,777,216 bytes in a temporary directory (valid Sample lines in the Collector envelope), reads it through `files.read_artifact`, parses it with `read_evidence(bytes, name=...)` and `summarize`, builds the chart payload and the detail JSON, and reports the peak Python memory with `tracemalloc` and the process peak working set (`psutil` is not pinned, so use `ctypes` only inside this tool, which is outside `coroner/viewer`, or `resource` where available; record which source was used) and the elapsed time. It never touches `runs/`.
    - Run it three times and record the three real numbers, the Python version and the method in `docs/m1-results/parse-memory.md`. State whether the measured peak is consistent with the design estimate of about 100 MB for one run and with the bound of two concurrent loads, and flag the result for the reviewer if the peak is much larger (do not change the semaphore size silently).
    - Files: `tests/unit/viewer/measure_parse_memory.py`, `docs/m1-results/parse-memory.md`.
    - Verify: `.\.venv\Scripts\python.exe tests/unit/viewer/measure_parse_memory.py` (three runs). Also `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer --collect-only -q -p no:cacheprovider` shows the tool is not collected.
    - Done when: three real measurements and the method are recorded.
    - _Requirements: 3.12, 16.5, 16.6, 16.7_

  - [x] 2.12 Write the classification and Docker state tests
    - `tests/unit/viewer/test_classification.py`: A, B, C in lower and upper case and other strings; verdict mapping for `passed`, `failed`, `aborted` and other values; the B (137, OOMKilled false) versus C (137, OOMKilled true) values returned as recorded, and that changing one never changes the other.
    - `tests/unit/viewer/test_docker_state.py`: the five `source` states; each of the six fields discrepant in turn; the nine-digit `FinishedAt` versus the millisecond manifest value (equal); the zero value as not recorded; a field recorded in one source only is `not_compared_*` and never a discrepancy; raw missing, malformed (`invalid_json`), rejected, oversized and unreadable; memory limit 0 gives `none_configured`; the Verdict still comes from the Stored_Outcome during a discrepancy.
    - Files: `tests/unit/viewer/test_classification.py`, `tests/unit/viewer/test_docker_state.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_classification.py tests/unit/viewer/test_docker_state.py --collect-only -q -p no:cacheprovider` (record the count), then run both.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 4.11, 5.5, 11.3, 11.4, 11.18, 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8, 14.9, 14.10, 14.11, 14.12_

  - [x] 2.13 Write the evidence assembly tests
    - `tests/unit/viewer/test_evidence_assembly.py`: missing evidence (every evidence-derived value `null`, manifest values kept); Malformed_Lines excluded from every count; an only-malformed file gives Sample count 0, `has_valid_record` false and the "no valid record" state; the manifest record count mismatch warning and no mismatch claimed when `manifest.evidence` is null; `summarize` counts equal the response counts; Last_Persisted_Sample versus Latest_Capture_Sample on a fixture whose last record does not have the latest Capture_Time; `last_receive_time_summary` returned separately and `"none received"` unchanged with `null` display; `unobserved_interval_note` equals `UNOBSERVED_NOTE`; an RSS above the memory limit returned unchanged; null RSS counted as unplottable and never plotted; a `ts` fixture that cannot reach the reader is not required.
    - Files: `tests/unit/viewer/test_evidence_assembly.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_evidence_assembly.py --collect-only -q -p no:cacheprovider` (record the count), then run it.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 3.6, 3.7, 3.8, 5.8, 5.9, 5.10, 5.11, 5.12, 6.12, 6.20, 7.3, 11.5, 11.16, 11.29, 12.5, 12.6_

  - [x] 2.14 Write the timeline, segment and tick tests
    - `tests/unit/viewer/test_timeline.py`: fixtures with 4,999, 5,000, 5,001 and 20,000 Plottable_Samples; selection includes the first, last and peak, keeps Capture order, contains only fixture Samples and is identical across calls; `total_samples`, first, peak and last RSS, Missing_Sample_Count and Malformed_Line count equal the full fixture values; `partial_view` and the full-run statistics for a smaller `chart_cap` passed to `build_chart`; `gap_before` for a sequence discontinuity and for a `silence_timeout` in full view and in Partial_View; the unknown-range drop sets `unknown_range_drop_present` and never `gap_before`.
    - `tests/unit/viewer/test_segments_ticks.py`: `segment_before` `solid`, `dashed` and `none` cases in full view (a null-RSS Sample between points) and in Partial_View (stride skips); the legend condition (at least one `dashed`); `skipped_before` and `omitted_before`; the `x_axis` domain contains every point and the SIGKILL `x_ms`; tick count 1 to 7, step list, ticks multiples of one step; every tick `label` equals `format_utc_ms` of the tick's own `x_ms`; a single-point and a zero-width domain; a tick that coincides with no Sample.
    - Files: `tests/unit/viewer/test_timeline.py`, `tests/unit/viewer/test_segments_ticks.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_timeline.py tests/unit/viewer/test_segments_ticks.py --collect-only -q -p no:cacheprovider` (record the count), then run both.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.7, 6.8, 6.9, 6.10, 6.11, 6.22, 6.23, 6.25, 9.3, 9.4, 9.5, 11.12, 11.13, 11.14, 11.38, 11.41_

  - [x] 2.15 Write the clock and timing tests
    - `tests/unit/viewer/test_clocks.py`: Experiment B with a usable offset (`corrected_capture_time` equals the raw Capture_Time minus the offset in milliseconds, `x_ms` is the corrected epoch, `receive_time` unchanged, SIGKILL marker drawn with `x_ms`), without an offset (raw uncorrected axis, no marker, reason `offset_not_recorded`), with conflicting offsets (both values returned, every `corrected_capture_time` `null`, raw axis label code, no SIGKILL marker, `derived.suppressed` true and empty `intervals`), with a missing `sample_timing`, with a missing uncertainty (no difference, no ordering); A and C have `null` corrected values, `raw_capture`, no marker, an EMPTY `derived.intervals` list (no `sample_to_finished_at`, no interval to `crash_request_time`), the raw Capture_Time values and the Docker `FinishedAt` returned as separate recorded values with their clock labels (`text_times` rows for `last_persisted_capture`, `latest_capture` and `docker_finished_at`), and no numeric difference and no ordering anywhere between them (assert that no field of the response holds a Capture_Time-to-`FinishedAt` difference); recorded `sample_timing` counts and Capture_Time lists returned unchanged and never recomputed; `after_container_stop_analysis` `unavailable` gives a `null` count and never 0; ordering suppression within `max(uncertainty, tolerance)`; no ordering for `FinishedAt` versus SIGKILL; a nine-digit `FinishedAt` returned unchanged with the ms display; Experiment B Last_Persisted and Latest_Capture intervals with distinct `sample` values, each labelled derived and not independently proven; Receive_Time never shifted; B without correction produces no `sample_to_*` interval (only `finished_at_to_sigkill` when both times are recorded).
    - **Tolerance matrix.** For Experiment B fixtures with `sample_timing.tolerance_ms` set to `0` and `250` (valid; ordering stated per the threshold rule), `-1`, `251` (`out_of_range`), `true`, `false` (`invalid_type`, bool excluded), `250.0` and `12.5` (`invalid_type`), `"250"` (`invalid_type`), `null` and a missing key (`not_recorded`): assert `tolerance_status`, that `ordering` is `None` with the matching `tolerance_*` reason for every non-valid case, that the recorded value in the response equals the stored value (never clamped to 250, never replaced by 0 or a default), that `difference_ms` is still present, and that a valid value still gives `before_sigkill`, `after_sigkill` or `not_established` as before.
    - Create `tests/unit/viewer/test_tolerance_parity.py`: imports `CLOCK_TOLERANCE_MS` from `coroner.runner.evaluate` (this test file is the only Viewer test that imports it) and asserts `VIEWER_CLOCK_TOLERANCE_MAX_MS == CLOCK_TOLERANCE_MS`; a source-inspection test asserts that no module in `coroner/viewer` imports `coroner.runner.evaluate` or names `CLOCK_TOLERANCE_MS`; M0 is not modified.
    - Files: `tests/unit/viewer/test_clocks.py`, `tests/unit/viewer/test_tolerance_parity.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_clocks.py tests/unit/viewer/test_tolerance_parity.py --collect-only -q -p no:cacheprovider` (record the count), then run both.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 5.13, 6.15, 6.16, 6.17, 6.18, 11.17, 11.25, 11.26, 11.27, 11.28, 11.40, 11.44, 12.10, 15.5, 15.6, 15.7, 15.10, 15.11, 15.12, 15.13, 15.17, 15.19, 15.20, 15.21, 15.22, 15.23, 15.24, 15.26, 15.27, 15.28, 15.29, 15.30_

  - [x] 2.16 Write the discovery, Run_Order and `not_read` tests
    - `tests/unit/viewer/test_discovery.py`: loose files and directories without `manifest.json` are not listed; a directory whose `manifest.json` is unparseable, invalid, oversized, unreadable or a link is listed as an UNKNOWN run with the stable `reason`; an empty Runs_Directory returns an empty list with the label; a manifest `run_id` that differs from the directory name is listed UNKNOWN with `run_id_mismatch`; the 500-run cap fields; valid names that are not runs (like `t31`) are skipped silently.
    - `tests/unit/viewer/test_run_order.py`: different `start_time` descending; equal `start_time` by `run_id` descending as plain strings; missing or unparseable `start_time` after all others and by `run_id` descending; the first run of each type in Run_Order is the overview card and default compare run; identical order on repeated calls.
    - `tests/unit/viewer/test_not_read.py`: for each unusable-manifest cause (too large, unreadable, invalid, missing by containment, `run_id` mismatch) `evidence` and `inspect` are `not_read` with `manifest_unusable`, a spy IO records no open for them, derived values are `null`, `not_read` differs from `missing`, every status is one of the six values, the `artifact_<name>_not_read` warning text is "not read (manifest unusable)", and a usable manifest never produces `not_read`.
    - Files: `tests/unit/viewer/test_discovery.py`, `tests/unit/viewer/test_run_order.py`, `tests/unit/viewer/test_not_read.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_discovery.py tests/unit/viewer/test_run_order.py tests/unit/viewer/test_not_read.py --collect-only -q -p no:cacheprovider` (record the count), then run all three.
    - Done when: exit code 0 and passed (plus recorded skips) equals collected.
    - _Requirements: 3.1, 3.2, 3.4, 3.5, 3.11, 3.12, 3.13, 3.16, 3.17, 4.3, 8.9, 9.9, 11.2, 11.35, 11.36_

  - [x] 2.17 Write the cache, error path and logging tests
    - `tests/unit/viewer/test_cache.py`: a created, rewritten (size, mtime or identity changed), grown, shrunk, deleted or replaced artifact changes the list result compared with an empty cache; a result is not stored when the signature changes during the load or when `mtime_ns` is within 2 seconds of the injected clock; an example test pins the documented limitation (a same-signature rewrite is served from the cache) and the test docstring states it is a documented limitation, not a guarantee.
    - `tests/unit/viewer/test_error_paths.py`: an injected unexpected exception in the detail path returns HTTP 500 with `error.code` `internal_error`, a fixed message, no host path and no traceback, and the next request succeeds; an injected exception for one run in the list leaves the other entries intact, lists that run UNKNOWN with `reason` `internal_error`, and the response is HTTP 200.
    - `tests/unit/viewer/test_logging.py`: `caplog` while serving hostile fixtures (path-like strings, the temporary directory path, HTML) contains no fixture string and no temporary path, and WARNING lines hold only run id, artifact name and reason code.
    - Files: `tests/unit/viewer/test_cache.py`, `tests/unit/viewer/test_error_paths.py`, `tests/unit/viewer/test_logging.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_cache.py tests/unit/viewer/test_error_paths.py tests/unit/viewer/test_logging.py --collect-only -q -p no:cacheprovider` (record the count), then run all three.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 2.12, 3.18, 9.10, 11.40, 16.10, 16.11_

  - [x] 2.18 Write the API contract, truncation, null policy and end-to-end read-only tests
    - `tests/unit/viewer/test_api_contract.py`: exact key sets of the list envelope, the list item and the detail response (exactly the nine top-level blocks); every field of Requirement 9 criteria 1 to 13 present; no field on no allowlist; an extra fixture manifest key (and the never-read keys `phases`, `leak`, `collector.readiness`, `memory`, `highest_rss_bytes`, `evidence_summary`, `container_left_running`, `container_state_problems`) does not change the response; `plotted_samples == len(points)`; `partial_view`; no NaN or Infinity; missing values are `null` and never 0, false or an empty string (missing evidence, missing inspect, missing optional field, invalid manifest, Malformed_Lines); HTML markup in an artifact string returned as JSON data unchanged; no absolute path anywhere even though the fixture manifests contain absolute `raw_inspect_path`, `host_copy_path` and `docker_inspect_path`, including an absolute `--runs-dir` (label is the final component only) and a relative one (label as supplied); an original timestamp with nine fractional digits returned unchanged with the `_display` value from `format_utc_ms` and `null` display with the original intact for an unparseable value; the Verdict still comes from the Stored_Outcome.
    - `tests/unit/viewer/test_truncation.py`: a capped field longer than its cap returns the first `cap` code points with `<field>_truncated` true and the original length; a field within the cap has `truncated` false and an unchanged value; an uncapped string longer than every cap is returned unchanged; list caps with `_total` and `_truncated`.
    - Also add to `test_api_contract.py` the end-to-end read-only check against the real loader (snapshot identical before and after list and detail requests on fixtures, no file created).
    - Files: `tests/unit/viewer/test_api_contract.py`, `tests/unit/viewer/test_truncation.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_api_contract.py tests/unit/viewer/test_truncation.py --collect-only -q -p no:cacheprovider` (record the count), then run both.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 2.9, 2.11, 3.10, 5.10, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.11, 9.12, 9.13, 9.14, 9.15, 10.5, 11.5, 11.7, 11.9, 11.16, 11.28, 11.30, 11.37, 11.39, 12.2, 12.3, 12.7, 16.10_

  - [ ]* 2.19 Write property test for deterministic selection
    - **Property 1: Deterministic selection preserves what matters**
    - **Validates: Requirements 6.2, 6.3, 6.4, 6.5, 9.4, 11.13**
    - File: `tests/unit/viewer/test_prop_p01_selection.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p01_selection.py -q -p no:cacheprovider`.

  - [ ]* 2.20 Write property test for statistics independent of the chart cap
    - **Property 2: Full-run statistics are independent of the chart cap, and `partial_view` follows the counts**
    - **Validates: Requirements 6.7, 6.8, 6.22, 9.5, 9.6, 11.12, 11.38, 12.8**
    - File: `tests/unit/viewer/test_prop_p02_cap_independence.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p02_cap_independence.py -q -p no:cacheprovider`.

  - [ ]* 2.21 Write property test for the response allowlist and truncation metadata
    - **Property 5: Responses use an allowlist, expose no path, preserve uncapped values, and mark every truncation**
    - **Validates: Requirements 2.9, 2.11, 3.14, 9.8, 9.14, 9.15, 11.9, 11.37, 11.39, 16.9, 16.10**
    - File: `tests/unit/viewer/test_prop_p05_allowlist.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p05_allowlist.py -q -p no:cacheprovider`.

  - [ ]* 2.22 Write property test that not recorded is always null
    - **Property 6: Not recorded is always null**
    - **Validates: Requirements 3.6, 3.10, 5.10, 9.7, 12.2, 12.3, 12.7, 14.11**
    - File: `tests/unit/viewer/test_prop_p06_null.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p06_null.py -q -p no:cacheprovider`.

  - [ ]* 2.23 Write property test for classification and verdict
    - **Property 7: Classification and verdict are functions of the recorded values only**
    - **Validates: Requirements 11.3, 11.4, 11.18, 14.9, 14.12**
    - File: `tests/unit/viewer/test_prop_p07_classification.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p07_classification.py -q -p no:cacheprovider`.

  - [ ]* 2.24 Write property test for timestamp preservation and display
    - **Property 8: Recorded timestamps are preserved and displayed by one formatter**
    - **Validates: Requirements 9.12, 9.13, 11.28, 12.9, 15.3, 15.25**
    - File: `tests/unit/viewer/test_prop_p08_timestamps.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p08_timestamps.py -q -p no:cacheprovider`.

  - [ ]* 2.25 Write property test for Experiment_B correction and conflicting offsets
    - **Property 9: Correction applies to Experiment_B Capture_Time only, and conflicting offsets suppress it**
    - **Validates: Requirements 6.16, 11.25, 11.26, 11.40, 15.5, 15.6, 15.7, 15.10, 15.11, 15.12, 15.13, 15.17, 15.26**
    - File: `tests/unit/viewer/test_prop_p09_correction.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p09_correction.py -q -p no:cacheprovider`.

  - [ ]* 2.26 Write property test for ordering statements
    - **Property 10: Ordering is stated only outside the recorded uncertainty, and Docker-clock intervals never carry an ordering**
    - **Validates: Requirements 5.13, 7.3, 11.27, 11.44, 15.18, 15.19, 15.20, 15.21, 15.27, 15.28, 15.29, 15.30**
    - File: `tests/unit/viewer/test_prop_p10_ordering.py`. The generators draw `tolerance_ms` from any JSON type (ints inside and outside 0..250, bools, floats, strings, `None`, missing) and assert the Property 10 statement, including that no value is clamped and that Experiment A, C and unknown types have an empty `derived.intervals`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p10_ordering.py -q -p no:cacheprovider`.

  - [ ]* 2.27 Write property test for `gap_before`
    - **Property 11: `gap_before` agrees with a reference implementation**
    - **Validates: Requirements 6.9, 6.10, 11.14**
    - File: `tests/unit/viewer/test_prop_p11_gap_before.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p11_gap_before.py -q -p no:cacheprovider`.

  - [ ]* 2.28 Write property test for last persisted, latest capture and RSS statistics
    - **Property 12: Last persisted, latest capture, and RSS statistics follow their definitions**
    - **Validates: Requirements 5.12, 9.11, 11.29, 12.5, 12.6, 12.8**
    - File: `tests/unit/viewer/test_prop_p12_last_persisted.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p12_last_persisted.py -q -p no:cacheprovider`.

  - [ ]* 2.29 Write property test for Docker state source and discrepancy rules
    - **Property 13: Docker state source and discrepancy rules**
    - **Validates: Requirements 4.11, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8, 14.10**
    - File: `tests/unit/viewer/test_prop_p13_docker_state.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p13_docker_state.py -q -p no:cacheprovider`.

  - [ ]* 2.30 Write property test for discovery and Run_Order
    - **Property 14: Discovery lists exactly the Run_Directories in Run_Order**
    - **Validates: Requirements 3.1, 3.2, 3.4, 3.5, 3.11, 3.13, 4.3, 8.9, 11.2, 11.36**
    - File: `tests/unit/viewer/test_prop_p14_discovery_order.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p14_discovery_order.py -q -p no:cacheprovider`.

  - [ ]* 2.31 Write property test for the cache
    - **Property 15: The cache never changes a result for a changed signature**
    - **Validates: Requirements 3.1, 9.1, 16.11**
    - File: `tests/unit/viewer/test_prop_p15_cache.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p15_cache.py -q -p no:cacheprovider`.

  - [ ]* 2.32 Write property test for segment styles
    - **Property 20: Segment styles are solid only between consecutive Samples**
    - **Validates: Requirements 6.9, 6.25, 11.41**
    - File: `tests/unit/viewer/test_prop_p20_segments.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p20_segments.py -q -p no:cacheprovider`.

  - [ ]* 2.33 Write property test for tick labels
    - **Property 21: Tick labels are the exact time at the tick position**
    - **Validates: Requirements 6.11, 6.23, 11.41**
    - File: `tests/unit/viewer/test_prop_p21_ticks.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p21_ticks.py -q -p no:cacheprovider`.

  - [ ]* 2.34 Write property test for Artifact_Status and `not_read`
    - **Property 23: Artifact_Status is one of six values, and an unusable manifest yields `not_read`**
    - **Validates: Requirements 3.12, 3.16, 3.17, 9.9, 11.35**
    - File: `tests/unit/viewer/test_prop_p23_not_read.py`. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p23_not_read.py -q -p no:cacheprovider`.

  - [x] 2.35 Checkpoint - review before continuing (M1.1-B)
    - Run `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py -q -p no:cacheprovider` (collected count first; exit code 5 is a FAILURE). This includes the group A tests, so the static read-only scan and the import boundary test now cover the new modules. Record `$LASTEXITCODE` and the real collected, passed, failed, skipped and deselected counts, which optional property tests were and were not run, and every skip with its reason (for example Windows reparse-point tests).
    - Run a read-only smoke check of the API against a fixture only (not `runs/`): the tests of tasks 2.16 and 2.18 already do this; do not start a server in this checkpoint.
    - Report: files created and modified (`git status --short`), test results, the real parse-memory measurements of `docs/m1-results/parse-memory.md`, and the open items below, in particular (R-A) to (R-E) and the parse-memory result.
    - **Independent review by the user is required before M1.1-C starts. STOP here and wait for the user's decision. Report real results, never predicted results. Do not commit.**

- [x] 3. M1.1-C Investigation UI
  - [x] 3.1 Build the static shell, design tokens, shared modules and page router
    - Finish `coroner/viewer/static/index.html`: skip link, header with "Coroner", `<main id="main">`, a `<noscript>` message, one `<script type="module" src="/static/app.js">`, a stylesheet link to `/static/style.css`, no inline script, no inline style, no external URL.
    - Create `style.css` with the CSS custom property tokens of the design (`--bg #14161a`, `--surface #1c1f25`, `--surface-2 #252932`, `--border #4a5261`, `--text #e8eaee`, `--muted #a6aebb`, `--amber #f5b041`, `--red #ff7b72`, `--purple #c4a3ff`), system font stacks (no font files), 12/14/16/20/28 px sizes, CSS grid layout (three cards at 960 px and wider, one column below; compare slots side by side at 900 px and wider), 44 px targets, one 120 ms colour transition removed under `prefers-reduced-motion: reduce`, and no `style=""` dependence.
    - Create `copy.js` (frozen object, every user-visible string grouped by page, including the fixed phrases: "Coroner", "Container Crash Forensics", "What survived after the process died?", "controlled fatal process exit via os._exit(1)", "not recorded", "no saved run found", "DISCREPANCY", "shown to ms; original in details", "derived, not independently proven", "Partial view", the axis labels, the SIGKILL label, KNOWN and UNKNOWN OR LIMITED, the Docker state source labels, "not read (manifest unusable)", and the compare strings used by group D), `dom.js` (`el`, `text`, `clear`, and the constant `SVG_NS` equal to `http://www.w3.org/2000/svg`; `createElement`/`createElementNS`, `textContent`, an attribute-name allowlist), `api.js` (`fetchJson` returning `{ok, status, data}` or `{ok: false, status, code}`; status 0 for a network failure; the API error `message` is ignored), and `app.js` (router for `/`, `/runs/{id}`, `/compare`; loading state with `aria-live="polite"`; error panel with a retry button mapping 400, 404, 0 and other statuses to fixed copy; empty state).
    - Create `compare.js` as a clearly labelled placeholder that renders the fixed text "Compare page: implemented in checkpoint M1.1-D" and exports `renderCompare`; task 4.5 replaces it.
    - Nothing in `static/*.js` uses `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `new Function`, `setAttribute("style", ...)`, `Date.parse`, `new Date(` or `Intl` on artifact values, or any `http://`, `https://` or protocol-relative `//` URL; the single allowed exception is the exact standard SVG namespace string `http://www.w3.org/2000/svg` as the namespace argument of `createElementNS` (an identifier, not a request).
    - Files: `coroner/viewer/static/index.html`, `style.css`, `copy.js`, `dom.js`, `api.js`, `app.js`, `compare.js`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_http_guard.py -q -p no:cacheprovider` still passes (the static allowlist now serves the new files; record counts). `node --check` is not available without Node; if `node` exists, run `node --check` on each file and record it, otherwise record "not run".
    - Done when: the files exist, the allowlist serves them, and the group A guard tests pass.
    - _Requirements: 2.10, 3.9, 3.17, 4.8, 4.10, 13.14_

  - [x] 3.2 Build the overview page (`overview.js`)
    - Render the brand "Coroner", the heading "Container Crash Forensics" and the subtitle "What survived after the process died?"; three incident cards labelled A, B and C, each populated from the first run of that type in the `GET /api/runs` order, showing the run id, the Verdict as a text badge plus colour, Docker ExitCode, OOMKilled, the preserved Sample count, the Collector survival result when recorded, a link built with `"/runs/" + encodeURIComponent(run_id)`, and the red text marker "DISCREPANCY" when `docker_state_discrepancy` is true; "no saved run found" with every value "not recorded" when a type has no run; Experiment_A description "controlled fatal process exit via os._exit(1)" and never "exception"; the run list of every other run in the API order with run id, experiment type, Verdict and link, including runs whose experiment is not A, B or C and UNKNOWN runs with their reason; the "showing N of M runs" notice when `truncated`; the empty state that names `runs_dir_label` and states the Viewer does not generate runs. All text goes through `copy.js`, all artifact values through `textContent`.
    - Files: `coroner/viewer/static/overview.js`.
    - Verify: static tests of task 3.6; manual review in task 3.8.
    - Done when: the file exists and imports only the shared modules.
    - _Requirements: 3.9, 3.17, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.11_

  - [x] 3.3 Build the SVG RSS timeline (`chart.js`)
    - Build the chart with `createElementNS`, whose namespace argument is the exact standard SVG namespace string `http://www.w3.org/2000/svg` defined once as a constant in `dom.js` (no other URL appears in any static file) (frame `viewBox="0 0 960 380"`, margins left 72, right 24, top 24, bottom 84; `role="img"` with `<title>` and `<desc>`). The x position of points, the SIGKILL line and ticks is `left + (x_ms - domain_min_ms) / (domain_max_ms - domain_min_ms) * plot_width` from the server-supplied domain (a zero-width domain centres the single tick and point); one tick per `x_axis.ticks[]` entry labelled with that entry's `label` only; the axis title keyed by `chart.time_axis`; y axis in MiB from 0 to `max(y_data_max_bytes, memory limit) * 1.05` with 4 to 6 nice ticks and the title "RSS (MiB)", so a value above the limit is drawn at its true height; segments drawn from `segment_before` (`solid`, `dashed` with `stroke-dasharray`, `none` with a dotted vertical "gap" mark when `gap_before`); one legend entry "dashed: omitted observations between these points" when any dashed segment exists and, in a Partial_View, the "Partial view" label with the total and plotted counts next to the chart; circles up to 600 points, polyline plus the active point beyond that; a dashed amber memory-limit line when the limit is greater than 0; for Experiment B with `sigkill.drawn` one dashed purple vertical line with the exact label "SIGKILL sent (Host_Clock; position derived, not independently proven)"; no marker for `FinishedAt`, crash request or any Sample time.
    - Roving focus: the plot is one tab stop (`tabindex="0"`, `role="application"`, `aria-label` naming chart and keys, `aria-activedescendant`); Left and Right move the active point, Home and End jump to the ends; pointer hover selects the nearest `x_ms` by binary search; each point has `role="img"` and an `aria-label`; `describePoint(point, context)` builds the readout from API fields only (seq; original Capture_Time with "(Container_Clock)"; "corrected (derived)" only when `corrected_capture_time` is non-null; original Receive_Time with "(Collector_Clock)"; RSS in MiB and bytes; "shown to ms; original in details") and feeds the visible readout, the `aria-label` and an `aria-live="polite"` region.
    - Below the chart: the Sample table in a `<details>` titled "Sample table (accessible equivalent of the chart)" with a `<caption>`, rows in chunks of 200 with a "Show more" button; the count of unplottable Samples; the statement about an unknown-position drop when `unknown_range_drop_present`; the empty state "No Sample with a valid RSS was preserved" when nothing is plottable; the `text_times` rows with clock labels; the experiment text rows (A: "crash requested (Host_Clock)", the raw Last_Persisted and Latest_Capture Capture_Times (Container_Clock) and `FinishedAt` (Docker_Clock) each as its own separately labelled row, with the "position not established" sentence; B: SIGKILL send time, uncorrected `FinishedAt`, Last_Persisted and Latest_Capture Capture_Times, Last_Persisted Receive_Time, and "correction unavailable" with its reason when applicable; C: the raw Capture_Times and `FinishedAt` as separate labelled rows, the "position not established" sentence and the recorded Sampling_Limitations). For A and C the page shows NO interval, NO difference and NO ordering statement between a Capture_Time and `FinishedAt` or the crash request time (Requirement 15 criterion 29).
    - Files: `coroner/viewer/static/chart.js`.
    - Verify: static tests of task 3.6; manual review in task 3.8.
    - Done when: the file exists and uses no timestamp parsing.
    - _Requirements: 6.1, 6.10, 6.11, 6.12, 6.13, 6.14, 6.15, 6.16, 6.17, 6.18, 6.19, 6.20, 6.21, 6.23, 6.24, 6.25, 15.11, 15.12, 15.13, 15.14, 15.15, 15.16, 15.17, 15.29, 11.43_

  - [x] 3.4 Build the Evidence vs Uncertainty panel (`evidence-panel.js`)
    - Two labelled sections, KNOWN and UNKNOWN OR LIMITED, each entry prefixed with its text label in addition to colour. KNOWN: Docker-observed status, ExitCode, OOMKilled with the state source label, the persisted evidence record count, the preserved Sample count, first, highest and last-persisted RSS with their definitions. UNKNOWN OR LIMITED: the `unobserved_interval_note` text unchanged; every Experiment_B Derived_Interval labelled "derived, not independently proven" with the statement that it does not establish the order of two events (Experiment A and C have no Derived_Interval: the panel lists the raw Capture_Time and the Docker `FinishedAt` as separate recorded values and states that their order is not established); a `tolerance_status` other than `valid` listed as "ordering unavailable: recorded tolerance not recorded / out of range / of invalid type" with the recorded value shown as recorded; Experiment C growth after the Last_Persisted_Sample may be unsampled plus the recorded Sampling_Limitations; unreconciled drops with uncertainty and the Malformed_Line count; the sentence that a root cause is not established (no diagnosis, no confidence, no probability); A and C: the ordering of Capture_Time against Host_Clock and Docker_Clock times is not established; B: every comparison of a Corrected_Capture_Time or `FinishedAt` with the SIGKILL send time is "derived, not independently proven" and the Clock_Uncertainty does not bound the Docker_Clock; conflicting offsets as a Discrepancy with both values and the "derived values are suppressed" statement; Discrepancies with field name and both values; every Not_Recorded entry as "not recorded". Runner text (`termination_description`, `cause_label`, `limitations`, `statements`) in a visually separate block labelled "recorded by the Runner". The panel never states or implies that the Collector_Clock to Host_Clock uncertainty proves the Container_Clock and Docker_Clock are equivalent.
    - Files: `coroner/viewer/static/evidence-panel.js`.
    - Verify: static tests of task 3.6; manual review in task 3.8.
    - Done when: the file exists and all strings come from `copy.js`.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 7.11, 7.12, 7.13, 15.29, 15.30_

  - [x] 3.5 Build the incident detail page (`incident.js`)
    - Render the run id, experiment type, start and end time with clock labels and the "shown to ms; original in details" label with the original in a details view; the Verdict with the Stored_Outcome and the failure code and message; the Docker status, ExitCode and OOMKilled each labelled with the state source (Requirement 14 labels, "DISCREPANCY" with both values side by side, "not compared: not recorded in <source>", the raw-unavailable reason); the memory limit in MiB with bytes, "0 bytes (no memory limit configured)" for 0 and "not recorded" for `null`; the Runner `termination_description` and `cause_label` labelled "recorded by the Runner"; Durability_Level as Crash_Durable for `crash`, Process_Durable for `process`, other values as text; the manifest record count and the valid record count with a flag on a difference; the preserved Sample count, Missing_Sample_Count, Unreconciled_Drop_Count with its uncertainty flag and the Malformed_Line count; the Last_Persisted_Sample, the Latest_Capture_Sample and "last Receive_Time" as three separately labelled facts; the clock offset with the uncertainty ("uncertainty not recorded" when `null`) and the scope note; the recorded `sample_timing` values exactly as stored ("sample timing not recorded", the unavailable analysis statement), including `tolerance_ms` exactly as recorded with the note "out of range" or "not recorded" when `tolerance_status` is not `valid` (never clamped); each Experiment_B Derived_Interval (none for A and C, whose raw Capture_Time and `FinishedAt` are shown separately) with the fixed statements ("derived, not independently proven", clock labels, "the four clocks ... are not recorded as synchronised", "the recorded uncertainty does not bound the Docker_Clock or the Container_Clock", "does not establish the order of two events", "order not established within the recorded clock uncertainty and tolerance"); the artifact status of each artifact ("not read (manifest unusable)" for `not_read`, "missing" for `missing`, the too-large text with size and limit); the warnings list. It composes `chart.js` and `evidence-panel.js`. All artifact values go through `textContent`.
    - Files: `coroner/viewer/static/incident.js`.
    - Verify: static tests of task 3.6; manual review in task 3.8.
    - Done when: the file exists and imports only the shared modules, `chart.js` and `evidence-panel.js`.
    - _Requirements: 3.6, 3.8, 3.12, 3.17, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 5.11, 5.12, 5.13, 6.17, 6.18, 6.19, 14.2, 14.3, 14.4, 14.5, 14.7, 14.8, 15.1, 15.4, 15.9, 15.18, 15.22, 15.23, 15.24, 15.27, 15.29, 15.30_

  - [x] 3.6 Write the static UI tests
    - `tests/unit/viewer/test_static_ui.py`: every file in `coroner/viewer/static/` matches the allowlist pattern and is served by `/static/<name>` with the right content type; every `import` in a static module resolves to an existing file; `index.html` has no inline `<script>` body, no `style=""` attribute and no external URL; no static file contains `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval(`, `new Function`, `setAttribute('style'`/`setAttribute("style"`, `javascript:`, `Date.parse`, `new Date(` or `Intl` applied to artifact values; no `fetch(` to a non-relative URL;
    - **URL scan with the SVG namespace whitelist.** Scan every file in `coroner/viewer/static/` (HTML, JS, CSS, including `url(...)` and `@import` in CSS and `src`/`href` attributes in HTML) for `http://`, `https://` and protocol-relative `//` URLs. The ONLY allowed URL string is exactly `http://www.w3.org/2000/svg` (compared as an exact whole-URL match, so `http://www.w3.org/2000/svg.evil.test`, `http://www.w3.org/2000/svg/x`, `https://www.w3.org/2000/svg` and `http://www.w3.org/1999/xlink` are flagged), and it is permitted only as the namespace constant of `dom.js` / the `createElementNS` argument. Every other URL fails the test; external fetches, CDN dependencies, external fonts, scripts and stylesheets stay prohibited. Negative controls on synthetic files: a file containing `https://cdn.jsdelivr.net/npm/x.js` is flagged; a file containing `//cdn.example.com/x.js` is flagged; a file containing only `createElementNS("http://www.w3.org/2000/svg", "svg")` is NOT flagged; a CSS file containing `@import url(https://fonts.googleapis.com/x)` is flagged.
    - Other checks in the same file: `copy.js` contains every fixed phrase listed in task 3.1 and "controlled fatal process exit via os._exit(1)" and no "exception" in the Experiment_A description; CSP compatibility: the CSP header value from the app forbids inline script and style and every static file is compatible with it (no inline handlers `onclick=` and similar); the HTML response carries the CSP and `nosniff`; an artifact value containing HTML markup returns from the API unchanged (the UI side is checked by the scans above and by the manual review). The test reports that it does not execute JavaScript.
    - Files: `tests/unit/viewer/test_static_ui.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_static_ui.py --collect-only -q -p no:cacheprovider` (record the count), then run it, then run `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_static_rules.py -q -p no:cacheprovider`.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 2.10, 4.8, 4.10, 11.9, 11.41, 11.43, 13.6, 13.7, 13.14_

  - [x] 3.7 Create the acceptance record skeleton and the visual review procedure
    - Create `docs/m1-results/m1.1-acceptance.md` with the sections Environment (date, operating system, Python version, branch and commit, Viewer command line, browser name and version, `node --version` or "node not available"), Run ids used, Procedure and observed results (one row per step: step, expected, observed text, pass or fail), Tested / not tested ledger (`ran` or `not run` with evidence for each claim), and Deviations. Every result cell starts as `PENDING` and is filled only with real observations.
    - Add the "Visual review of checkpoint M1.1-C" procedure against the real `runs/` (read-only, browser on the same machine): start `python -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port 8765`; overview (`/`) three cards with run id, Verdict, ExitCode (A 1, B 137, C 137), OOMKilled (false, false, true), Sample count, Collector survival, the full list in Run_Order, no "DISCREPANCY" unless a run really disagrees; the Experiment B incident (`b-20261120T211311Z-ce313da1`): axis label "host-equivalent time (corrected ..., derived)", the SIGKILL line labelled derived, text rows below the chart, the Derived_Intervals with fixed statements, `sample_timing` as recorded; an Experiment A and an Experiment C incident: raw axis label, no markers, crash request (A), the raw Capture_Time values and `FinishedAt` as separate labelled text, and NO interval, difference or ordering statement between them; tick labels differ from the nearest point's time; the Sample table; empty, error and loading states by stopping the server and pressing retry; DevTools console shows no CSP violation and no script error; hostile text check only on a scratch copy of a run directory outside `runs/` (never the real one); record the browser name and version. The visual check is not a keyboard, contrast or reduced-motion check; those are in task 4.7.
    - Files: `docs/m1-results/m1.1-acceptance.md`.
    - Verify: `Select-String -Path docs\m1-results\m1.1-acceptance.md -Pattern 'PENDING'` shows the pending cells.
    - Done when: the skeleton and the procedure exist and no result is pre-filled.
    - _Requirements: 11.33, 11.34, 11.41_

  - [x] 3.8 Perform the manual visual review on the real runs (read-only) and record the real results
    - Start the Viewer on the real `runs/` with the process tool (or ask the user to start it), on port 8765, host `127.0.0.1`. The user (or the agent with the user's browser) walks the procedure of task 3.7. The agent records only what the user reports or what it observed itself, with the browser name and version, the run ids and the observed texts, and fills the rows of `docs/m1-results/m1.1-acceptance.md`. Anything not performed stays `PENDING` or `not run`. Stop the server when the review ends.
    - Do not modify `runs/`. No Docker command.
    - Files: `docs/m1-results/m1.1-acceptance.md`.
    - Verify: record the Viewer start line, the browser name and version, and the observed results in the file; `git status --short runs` shows no change (runs/ is ignored, so also compare the aggregate hash with task 4.8 later).
    - Done when: each procedure row has a real result or an explicit "not run", and any defect found is listed under Deviations (fix defects in the group C files only after reporting them).
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 6.11, 6.13, 6.14, 6.15, 6.16, 6.23, 6.24, 6.25, 7.1, 11.33, 11.41_

  - [x] 3.9 Checkpoint - review before continuing (M1.1-C)
    - Run `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py -q -p no:cacheprovider` (collected count first; exit code 5 is a FAILURE). Record real counts, skips and the optional tests run or not run.
    - Report: files created and modified (`git status --short`), test results, the real manual visual review results and what was not reviewed, the state of the acceptance skeleton, and the open items below. State explicitly that pytest did not execute any JavaScript and that nothing about browser behaviour is claimed beyond what the recorded manual review shows.
    - **Independent review by the user is required before M1.1-D starts. STOP here and wait for the user's decision. Report real results, never predicted results. Do not commit.**

- [x] 4. M1.1-D Comparison and acceptance
  - [x] 4.1 Implement `coroner/viewer/compare.py` and the `/compare` query validation
    - Implement `CompareQuery` and `parse_compare_query(items)` over `request.query_params.multi_items()`: only `a`, `b`, `c`, each at most once; an unknown key, a repeated key and an empty value raise `QueryError` (HTTP 400, code `invalid_query`); a value rejected by `validate_run_id_for_path` raises `QueryError` (code `invalid_run_id`). All values are validated before any file access. In `app.py` the `/compare` handler reads `Request` directly (no FastAPI query parameters, so FastAPI never produces a 422), returns the fixed one-line `text/plain` 400 body ("Invalid compare parameter.") on error, and otherwise serves the static shell without touching the file system. A valid id that names no run is 200.
    - Create `tests/unit/viewer/test_compare_query.py`: an invalid `a`, `b` or `c` is 400 with zero IO calls (spy IO); an unknown parameter, a repeated parameter and an empty value are 400 with zero IO calls; a valid nonexistent id is 200; `/runs/{id}` and `/api/runs/{id}` with an invalid id are 400 and with a valid nonexistent id are 404.
    - Files: `coroner/viewer/compare.py`, `coroner/viewer/app.py`, `tests/unit/viewer/test_compare_query.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_compare_query.py tests/unit/viewer/test_http_guard.py --collect-only -q -p no:cacheprovider` (record the count), then run both.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 8.10, 8.11, 11.24, 13.1, 13.8, 13.16_

  - [x] 4.2 Create the comparison reference vectors and the Python reference model
    - Create `tests/unit/viewer/compare_vectors.json`: input slot sets with expected statement ids S1 to S4, texts, omitted reasons and the headline. Cover at least: the recorded A/B/C case (A exit 1, B 137 false, C 137 true: S1, S2, S3, S4 and the headline); B and C with equal OOMKilled (no S3, no headline); an Experiment B run supplied as slot `a` (excluded with a warning, S1 and S4 omitted naming the missing slot, no headline use); a missing slot (unavailable); a missing ExitCode or OOMKilled value (statement omitted naming the value); a discrepant `exit_code` or `oom_killed` ("Docker state sources disagree", dependent statements omitted); a slot with zero Samples (S4 omitted); no saved run for a type; exit code 137 for B only.
    - Create `tests/unit/viewer/compare_reference.py`: the Python reference model written from Requirement 8, not from the JavaScript. Create `tests/unit/viewer/test_compare_logic.py` (Python layer): the reference model agrees with every vector; the test reports that it executes no JavaScript; a vector asserts the statements for B (137, false) and C (137, true) differ.
    - Files: `tests/unit/viewer/compare_vectors.json`, `tests/unit/viewer/compare_reference.py`, `tests/unit/viewer/test_compare_logic.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_compare_logic.py --collect-only -q -p no:cacheprovider` (record the count), then run it.
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.12, 8.13, 11.4, 11.32_

  - [x] 4.3 Implement `compare-logic.js` and the Node vector test
    - Create `coroner/viewer/static/compare-logic.js`: a pure ES module exporting `evaluateComparison(slots) -> {statements, headline, omitted}` with no DOM access, no `import`, and no `document`, `window`, `fetch` or browser global; statements S1 to S4 and the headline computed from the stored values exactly as in the design table (the headline only when B and C are present, not excluded, not unavailable, not discrepant, both ExitCode 137, different OOMKilled); omitted statements name the missing run or value.
    - Create `tests/unit/viewer/compare_vectors_runner.mjs` (loads `compare-logic.js` as text through `import("data:text/javascript;base64," + ...)`, evaluates each vector, prints a JSON result; no npm package, no `package.json`) and `tests/unit/viewer/test_compare_logic_node.py` (finds `node` with `shutil.which`; runs the runner with `subprocess.run([node, runner, vectors])`; compares the output with the expected values; `pytest.skip("node not on PATH")` when `node` is missing so the report shows skipped, never passed). Note: the unit-test guards block only the Docker CLI, so running `node` is allowed.
    - Record whether `node` was available. If it was, record `node --version` and the real passed count of the Node test. If it was not, record the test as skipped and the ledger row "compare-logic.js returns the vector results" as `not run`.
    - Files: `coroner/viewer/static/compare-logic.js`, `tests/unit/viewer/compare_vectors_runner.mjs`, `tests/unit/viewer/test_compare_logic_node.py`.
    - Verify: `node --version` (or "not available"); `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_compare_logic_node.py tests/unit/viewer/test_compare_logic.py -q -p no:cacheprovider -rs` (the `-rs` flag prints skip reasons).
    - Done when: exit code 0, and the Node test is recorded as either passed (with the Node version) or skipped (with the reason).
    - _Requirements: 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.12, 8.13, 8.15, 11.32, 11.34_

  - [ ]* 4.4 Write property test for comparison statements
    - **Property 17: Comparison statements are computed from the values**
    - **Validates: Requirements 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.12, 8.13, 8.15, 11.4, 11.32**
    - File: `tests/unit/viewer/test_prop_p17_compare.py`. The property runs against the Python reference model, and against `compare-logic.js` through Node only when `node` ran; otherwise the JavaScript half is reported as not run. Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_prop_p17_compare.py -q -p no:cacheprovider -rs`.

  - [x] 4.5 Implement the compare page (`compare.js`)
    - Replace the placeholder `coroner/viewer/static/compare.js`. Slot resolution: for each slot without a query parameter, the default is the first run in `GET /api/runs` order whose `experiment_type` equals the slot type (`a`: A, `b`: B, `c`: C); for each supplied parameter fetch `GET /api/runs/{run_id}` (HTTP 404 gives the `unavailable` state naming the run id with "no saved run found"; other failures give an `error` state with the HTTP status); a loaded run whose `experiment_type` differs from the slot type is `excluded` with a visible warning naming the run id, its actual type and the expected type, still shown with its values labelled with its actual type, and excluded from every statement and from the headline. The page uses only the two existing API routes and adds none. A selector lets the user choose which saved run of each type is compared (a link or a form of `GET` links to `/compare?a=...&b=...&c=...`; no JavaScript timestamp parsing). The table shows run id, Verdict, ExitCode, OOMKilled, memory limit, preserved Sample count and highest RSS per slot. Statements and the headline come only from `evaluateComparison`; the headline text "The same exit code does not always mean the same failure" appears only when the logic returns it. The Verdict stays the Runner's.
    - Update `copy.js` and `style.css` with the compare strings and layout (side by side at 900 px and wider, stacked below, horizontal scroll for the table). No other module is changed.
    - Files: `coroner/viewer/static/compare.js`, `copy.js`, `style.css`.
    - Verify: static tests of task 4.6; the real-browser procedure of task 4.7.
    - Done when: the placeholder text is gone and the page imports only the shared modules and `compare-logic.js`.
    - _Requirements: 8.1, 8.2, 8.7, 8.8, 8.9, 8.11, 8.12, 8.13, 8.14, 8.15, 13.16_

  - [x] 4.6 Extend the static tests for the compare files
    - Extend `tests/unit/viewer/test_static_ui.py`: `compare-logic.js` has no `document`, `window`, `fetch` or `import` reference; `compare.js` imports `compare-logic.js` and no other data source, calls `fetch` only for `/api/runs` and `/api/runs/` URLs; the placeholder text "implemented in checkpoint M1.1-D" is gone; the headline phrase is in `copy.js` and used only by `compare.js`; the registered route set is unchanged (no new route); the forbidden-pattern scans of task 3.6 still pass on the new files.
    - Files: `tests/unit/viewer/test_static_ui.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_static_ui.py tests/unit/viewer/test_static_rules.py tests/unit/viewer/test_http_guard.py -q -p no:cacheprovider` (record the collected count first).
    - Done when: exit code 0 and passed equals collected.
    - _Requirements: 8.14, 8.15, 11.32, 13.1, 13.2_

  - [x] 4.7 Perform the mandatory real-browser smoke test and accessibility checks and record the real results
    - The real-browser comparison smoke test is mandatory before M1.1 acceptance. The user (or the agent using the user's browser) performs the nine-step procedure of the design (start the Viewer on real `runs/`; `/compare` defaults and the S1 to S4 statements and headline; `/compare?a=<a B run id>&b=<that B run id>&c=<a C run id>` shows the warning and excludes the run; `/compare?a=<valid id naming no run>` shows 200 and an "unavailable" slot; `/compare?a=..`, `/compare?x=1`, `/compare?a=`, `/compare?a=<id>&a=<id>` each show a plain 400 text; a scratch copy of the runs folder (outside `runs/`) where B and C have equal OOMKilled shows no headline and no S3; DevTools console shows no CSP violation or script error on each page; keyboard on an incident page: Tab reaches the chart as one tab stop, Left, Right, Home and End move the active point and the readout changes, the Sample table opens from its summary; an Experiment B incident: tick labels are the times at the tick positions and a Partial_View fixture shows dashed segments and the legend entry). Create the scratch copies only outside `runs/`.
    - Add the accessibility checks: keyboard-only navigation of all three pages with a visible focus ring and a sensible focus order; the skip link; contrast of the token colours spot-checked against the design table (record the tool or method used and the measured values, or "not measured"); `prefers-reduced-motion: reduce` removes the 120 ms transition; 200% zoom and a 360 px window; a screen reader check only if the user performs one (otherwise "not run").
    - Record in `docs/m1-results/m1.1-acceptance.md`: the browser name and version, the Viewer command line, the run ids used and which were defaults, each step's observed text, pass or fail, and any deviation. Steps not performed are marked "not run". Do not write an expected result as an observed result.
    - Files: `docs/m1-results/m1.1-acceptance.md`.
    - Verify: the recorded file; no automated command can substitute for this task.
    - Done when: every row has a real observation or "not run", and the browser name and version are recorded. M1.1 is not accepted without this record.
    - _Requirements: 6.24, 11.33, 11.34, 11.41_

  - [x] 4.8 Launch the Viewer on the real runs (read-only), verify the localhost bind manually and confirm the artifacts are unchanged
    - With the before-snapshot of task 1.1 (`$env:TEMP\coroner-m1\runs-before.snapshot.txt`, outside the repository, read-only, and the count, bytes and aggregate digest in `docs/m1-results/runs-baseline.md`), first confirm the snapshot still exists, is read-only and its SHA-256 equals the digest recorded in `docs/m1-results/runs-baseline.md` (if it is missing or differs, STOP and report; never recreate or overwrite it). Then start the Viewer on `runs/` with the process tool on port 8765 and, using a browser or `Invoke-WebRequest`/`curl.exe` with an explicit `Host: 127.0.0.1:8765` header, perform only GET requests against `/`, `/api/runs`, `/api/runs/<each real run id>` and `/compare`. Record: the start line, HTTP status of each request, the number of runs listed, the first run of each type in Run_Order, the ExitCode and OOMKilled of the three card runs (expected A 1, B 137/false, C 137/true; record the real values and flag any difference), no absolute host path in any response body (`Select-String` the saved bodies for the repository path and for a drive letter), and no Viewer error.
    - **Manual launch verification (real socket; this is where the localhost launch is verified, not in the unit suite).** While the server runs, observe and record as MANUAL: the start line; HTTP 200 for GET `http://127.0.0.1:8765/` and `/api/runs` with the `Host: 127.0.0.1:8765` header and with the `Host: localhost:8765` header; HTTP 421 for a request with `Host: evil.example:8765` (and for `Host: 127.0.0.1` without a port); the response for `POST /api/runs` is 405 (a read-only probe of the method guard); and, optionally, that `netstat -ano | Select-String ':8765'` shows a LISTENING entry on `127.0.0.1:8765` only and none on `0.0.0.0:8765` or `[::1]:8765` (record "not run" if netstat was not used). Use `curl.exe` or `Invoke-WebRequest` from the machine itself; no Docker command. Stop the server afterwards.
    - **After-snapshot and comparison.** Write the final state of `runs/` with the command of task 1.1 but into a NEW file `$env:TEMP\coroner-m1\runs-after.snapshot.txt` (create-new semantics; STOP if it already exists; never reuse or overwrite the before-snapshot). Compare the final state with the before-snapshot: file count, total bytes and aggregate digest must be identical to the values in `docs/m1-results/runs-baseline.md`, `(Get-FileHash -LiteralPath "$env:TEMP\coroner-m1\runs-after.snapshot.txt").Hash` must equal `(Get-FileHash -LiteralPath "$env:TEMP\coroner-m1\runs-before.snapshot.txt").Hash`, and `Compare-Object (Get-Content "$env:TEMP\coroner-m1\runs-before.snapshot.txt") (Get-Content "$env:TEMP\coroner-m1\runs-after.snapshot.txt")` must print nothing. Record only counts, bytes, digests and "identical" or the NUMBER of differing lines in the repository documents; never copy per-file lines or scratch file names into the repository. If anything differs, STOP and report (Execution Rule 11).
    - **The snapshots stay.** Neither snapshot file is deleted, moved or edited in this task. They are deleted only after the user accepts task 4.8 and approves the deletion (asked in checkpoint 4.14).
    - Files: `docs/m1-results/m1.1-acceptance.md`, `docs/m1-results/runs-baseline.md`.
    - Verify: the commands above; `git status --short runs` and `git ls-files runs` (no output).
    - Done when: the before and after counts, bytes and digests are recorded as identical (or the difference is reported), the requests, statuses and manual launch observations are recorded as manual, and both snapshot files still exist outside the repository.
    - _Requirements: 1.1, 1.9, 1.10, 2.2, 3.9, 4.3, 10.5, 11.7, 11.45, 13.9, 16.10_

  - [x] 4.9 Add the README "Viewer" section
    - Read `README.md` and `tests/unit/test_docs.py` first (the README is checked for LF line endings, no BOM and required M0 phrases). Add a section "Viewer (M1.1)" before "License" (and update the layout list in section 16 if it lists packages) with: what the Viewer is (read-only, localhost-only, no Docker, no database), the launch command `python -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port 8765` and its defaults, the pages and API routes, the bind and Host rules (literal `127.0.0.1` bind, `localhost` accepted as a Host header, port 80 may need privileges), the artifact size limit and `too_large`, the not-recorded policy, the clock and derived-value rules in one short paragraph, the testing commands (the Docker-free commands of this plan), the Node-optional note and the real-browser record location, and the cache limitation text: "The Viewer caches parsed results keyed by the size, modification time and file identity of each artifact. An artifact rewritten with the same size, modification time and file identity (deliberate metadata forgery) can be served from the cache. Restart the Viewer to discard the cache." The README does not claim the cache can never be stale and does not claim JavaScript was tested.
    - Files: `README.md`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_docs.py --collect-only -q -p no:cacheprovider` (record the count), then run it. Check no BOM and no CRLF: `$b = [System.IO.File]::ReadAllBytes("README.md"); ($b[0..2] -join ','); ([System.Text.Encoding]::UTF8.GetString($b) -match "`r")`.
    - Done when: exit code 0, the README has LF endings and no BOM, and the section contains the limitation text (open item R-F is then satisfied by this section and the `loader.py` docstring).
    - _Requirements: 1.1, 1.2, 11.34, 16.11_

  - [x] 4.10 Run the focused regression of directly relevant existing tests
    - Run `.\.venv\Scripts\python.exe -m pytest tests/unit/test_structure.py tests/unit/common/test_timeutil.py tests/unit/evidence/test_reader.py tests/unit/evidence/test_summary.py tests/unit/evidence/test_prop_summary.py tests/unit/evidence/test_prop_reader_quarantine.py tests/unit/runner/test_prop_manifest.py tests/unit/runner/test_cli_summary.py -q -p no:cacheprovider` (collected count first; exit code 5 is a FAILURE). These are the tests that touch the manifest, reader, summary and time functions the Viewer reuses, and the structure test updated in task 1.8. Record the real counts. A failure in an M0 test is reported, not fixed in M0 source (Execution Rule 6).
    - Files: none modified.
    - Verify: the command above.
    - Done when: the real counts and exit code are recorded.
    - _Requirements: 1.7, 10.3, 11.11_

  - [ ]* 4.11 Optional: run the full default test suite (needs the user's approval first)
    - Ask the user for approval before running. If approved, run `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` (never `--run-docker`, never `CORONER_RUN_DOCKER`) with an extended timeout, and record the real collected, passed, failed, skipped and deselected counts and the elapsed time next to the previous baseline of about 4,510 tests. If the user declines, record "not run (declined)".
    - Files: `docs/m1-results/m1.1-acceptance.md`.
    - Verify: the command above.
    - Done when: the result or the decline is recorded.
    - _Requirements: 10.3, 10.6_

  - [x] 4.12 Complete the acceptance record and the tested / not tested ledger
    - Fill `docs/m1-results/m1.1-acceptance.md` from the real results of tasks 1.1, 2.11, 3.8, 4.3, 4.7, 4.8 and 4.10 (and 4.11 when run): environment, run ids, procedure results, the ledger, and deviations. The ledger has one row per claim, each `ran` or `not run` with its evidence: comparison rules match the Python reference model (Python vectors); `compare-logic.js` returns the vector results (Node vector test, only when `node` ran); the page renders, links, warnings, "unavailable" slots, keyboard focus, SVG ticks, dashed segments and CSP cleanliness (real-browser procedure, only when executed and recorded); parse memory measured (task 2.11); launch on real runs with unchanged `runs/` counts, bytes and digest against the external before-snapshot (task 4.8); the literal `127.0.0.1` bind configuration without a socket (automated, task 1.11) and, as a separate MANUAL row, the real localhost launch, HTTP 200 on `http://127.0.0.1:8765`, the Host-header 200/421 observations and the optional `netstat` listener check (task 4.8, `ran` or `not run`, recorded as manual); property tests run or not run; Windows reparse-point tests passed or skipped. Every claim of JavaScript testing names which of the two layers ran (Node test or real browser). No claim of JavaScript or browser behaviour appears for a layer that did not run.
    - Files: `docs/m1-results/m1.1-acceptance.md`.
    - Verify: `Select-String -Path docs\m1-results\m1.1-acceptance.md -Pattern 'PENDING'` prints nothing, or every remaining `PENDING` is listed under Deviations with the reason.
    - Done when: the ledger is complete and honest.
    - _Requirements: 11.33, 11.34_

  - [x] 4.13 Final read-only verification
    - Run and record: `git branch --show-current`; `git status --short` (list every changed and untracked path; each is under `coroner/viewer/`, `tests/unit/`, `docs/m1-results/`, `README.md` or `.kiro/specs/coroner-m1-ui/`); `git ls-files runs` (no output); `git diff --cached --name-only` (nothing from `runs/`; if nothing is staged, say so); `git check-ignore -v runs/x/manifest.json` (a rule is shown); M0 source unchanged: `git diff --stat master -- coroner ':(exclude)coroner/viewer'` (no output) and `git status --short coroner` shows only `coroner/viewer/` paths; schemas, images and compose unchanged: `git diff --stat master -- compose.yaml docker requirements coroner/evidence coroner/common coroner/runner` (no output); only the allowed edits outside the new files: `git diff --stat master -- tests README.md` lists `tests/unit/test_structure.py` and `README.md` as the only modified existing files; line endings and BOM of every new file: scan `coroner/viewer`, `tests/unit/viewer`, `docs/m1-results` for a BOM or CR bytes; no new dependency pin (`requirements/` unchanged); no snapshot or per-file listing of `runs/` exists inside the repository (`git status --short` shows none, and `Select-String -Path docs\m1-results\*.md -Pattern 'snapshot'` shows only the file-name label, counts and digests); the Viewer source has no Docker client import, no subprocess or process execution, no Docker CLI invocation and no container-control operation (the static scan test of task 1.12 passes; the word `docker` and names such as `docker_state` are allowed); protected volumes untouched and no Docker command used in any task (state this from the task records; do not run a Docker command to check it).
    - Files: none modified.
    - Verify: the commands above.
    - Done when: every check has a real recorded result. Any difference is reported, not corrected silently.
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

  - [x] 4.14 Final checkpoint - review before M1.1 acceptance (M1.1-D)
    - Run `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py tests/unit/test_docs.py -q -p no:cacheprovider` (collected count first; exit code 5 is a FAILURE). Record the real counts, the skipped tests with reasons (including the Node test when `node` is missing), and which optional property tests were and were not run.
    - Report: the files created and modified, the real results of tasks 4.7, 4.8, 4.10, 4.12 and 4.13, the tested / not tested ledger, every open item below with its current state, and a clear statement of what is not established (JavaScript execution, browser behaviour, real Windows reparse points, port 80 bind).
    - **Snapshot deletion needs approval.** Ask the user whether the external snapshot files `runs-before.snapshot.txt` and `runs-after.snapshot.txt` in `$env:TEMP\coroner-m1\` may be deleted. Delete them only after the user has accepted the task 4.8 result and approved the deletion; until then they stay in place, unchanged. Record the user's decision and, if deleted, that only those two files in that folder were removed.
    - **Independent review by the user is required before M1.1 is accepted. M1.1 is not accepted without the real-browser record of task 4.7. STOP here and wait. Report real results, never predicted results. Do not commit, push or merge unless the user explicitly instructs it.**

## Open items for review

These are decisions the design made where the requirements are silent or loose, plus risks the design left open. They are listed for the user to confirm. No task resolves them silently; each is presented again in the review of checkpoint 1.18 (R-A to R-G) and 2.35 (the parse-memory result).

| ID | Item | Design choice that the tasks implement | Where it is handled |
|---|---|---|---|
| R-A | Requirement 3.16 treats a manifest `run_id` that differs from the directory name as an unusable manifest but does not state the manifest entry's own Artifact_Status. | Status `invalid` with reason `run_id_mismatch`; `evidence` and `inspect` are `not_read` / `manifest_unusable`. | 2.8, 2.9, 2.16, 2.34 |
| R-B | Requirement 9.13 says each timing value has "a `clock` string". | A `<field>_clock` sibling beside every timestamp, and invented codes `collector_minus_host` (offset and uncertainty) and `host` (tolerance); a literal `clock` field per value would need each value to become an object. | 2.9, 2.18 |
| R-C | **RESOLVED (review correction 3).** Derived_Interval set was open ("for example"). | Experiment B only: `sample_to_sigkill`, `sample_to_finished_at`, `finished_at_to_sigkill`, each labelled "derived, not independently proven". No interval to `crash_request_time`. Experiment A and C: NO interval; raw Capture_Time and `FinishedAt` are shown separately with no numeric cross-clock difference and no ordering claim. | 2.6, 2.15, 3.3, 3.4, 3.5 |
| R-D | Capped free-text fields and caps are delegated to the design. | Nine fields, caps of 200 and 2,000 code points; list caps of 20, 50 and 100 with `_total` and `_truncated`. | 2.3, 2.9, 2.18 |
| R-E | **RESOLVED (review correction 2).** Requirement 15.20 uses the Timing_Tolerance, which M0 fixes at a maximum of 250 ms. | Valid only as an `int` (bool excluded) from 0 to 250 inclusive, checked against the Viewer constant `VIEWER_CLOCK_TOLERANCE_MAX_MS = 250` (documented as mirroring M0; a parity test imports `CLOCK_TOLERANCE_MS` in test code only; no M0 change; `evaluate.py` is not imported). Any other value makes derived ordering unavailable for that run; never clamped; shown as recorded with an "out of range" or "not recorded" note. | 2.6, 2.15 |
| R-F | Requirement 16.11 requires the cache limitation to be documented. | The limitation text goes in the README Viewer section and the `loader.py` module docstring. | 2.8, 4.9 |
| R-G | **PARTLY RESOLVED (review correction 5).** Requirement 1.11 allows `--port 80`. | No automated test binds port 80 or any port (resolved: only `host_allowed`, a stub guard and the spied server configuration are tested). Binding port 80 may need privileges and a failed bind exits non-zero through uvicorn; a real port-80 bind stays unverified unless the maintainer does it manually. | 1.5, 1.10, 1.11, 4.8 |
| P-1 | Parse memory for a 16 MiB evidence file is estimated (about 100 MB) in the design, not measured. | Measured and recorded in task 2.11; the reviewer decides whether the semaphore size of 2 stays. | 2.11 |
| P-2 | Windows reparse-point, junction and symlink tests may be skipped when the test process cannot create the link. | Fake-IO tests cover the logic; skipped tests are reported as skipped. | 1.9, 2.16 |
| P-3 | The post-open path comparison on Windows is path-based; the handle identity check is the primary guard. | No `GetFinalPathNameByHandle`; the residual risk is documented. | 1.4, 1.9 |
| P-4 | Plan-level: group A uses an injected `DataProvider` stub for API success bodies, and `/compare` serves the shell without query validation until task 4.1. | Group B wires the real loader (2.10); group D adds the `/compare` validation (4.1). The stub never reaches a release. | 1.6, 2.10, 4.1 |
| P-5 | Plan-level: JavaScript behaviour (chart, focus, ticks, compare rendering) has no automated execution in the pinned stack. | Static scans always; Node vectors for `compare-logic.js` only when `node` exists; everything else only by the recorded real-browser procedure. | 3.6, 3.8, 4.3, 4.7 |

The review item "Unresolved question 1" (a real-socket test of the `127.0.0.1` bind in the guarded unit suite) is RESOLVED by review correction 5: the unit suite opens no socket, and the real launch is a manual check in task 4.8. (The ID "Unresolved question 1" comes from the review conversation, not from these three files.)

## Review corrections applied

The user approved these corrections after review; they were applied to `requirements.md`, `design.md` and this file together.

1. **Static scan by operations, not by the word `docker`** (tasks 1.12, 4.13; requirements 11.42; design "Launch and Safety", `test_static_rules.py`, V27). The Viewer source may contain `docker`, `docker_state`, `docker_inspect.json`, `container_state`. Docker client imports, subprocess and process execution, `shutil.which("docker")`, Docker CLI strings, container-control operations, `/var/run/docker.sock` and `DOCKER_HOST` are still forbidden. Negative controls are required.
2. **Tolerance validity 0 to 250** (tasks 2.6, 2.15, 2.26, 3.4, 3.5; requirements glossary, 12.10, 15.19, 15.20, 15.30, 11.27, 11.44, Source Field Map; design `timing`, `clocks.py`, Property 10, V26, R-E). Local constant, parity test, no clamping, ordering unavailable when not valid.
3. **No A/C derived interval** (tasks 2.6, 2.15, 3.3, 3.4, 3.5, 3.7; requirements glossary Derived_Interval, 5.13, 7.11, 9.13, 15.27, 15.29, 11.25; design `timing.derived`, Clock Handling table, Property 10, V23, V30, Q7, R-C). Raw Capture_Time and `FinishedAt` shown separately; Experiment B intervals stay "derived, not independently proven".
4. **SVG namespace whitelist** (tasks 3.1, 3.3, 3.6; requirements 4.10, 11.43; design SVG timeline, V28). Exactly `http://www.w3.org/2000/svg` is allowed; every other `http(s)://` or `//` URL is flagged.
5. **No real-socket unit test** (tasks 1.11, 4.8, 4.12; requirements 11.23, 11.45; design CLI, `test_cli.py`, "What is not tested automatically", manual check 3, V29). The bind is tested through a spy on `uvicorn.Server`; the real launch and Host-header checks are manual in task 4.8 and recorded as manual.
6. **External `runs/` before-snapshot** (tasks 1.1, 4.8, 4.13, 4.14). Created once with create-new semantics outside the repository, never overwritten, only the count, bytes, aggregate digest and file-name label are recorded in `docs/m1-results/runs-baseline.md`, compared by task 4.8, and deleted only after task 4.8 acceptance with the user's approval.

## Notes

- Tasks marked with `*` are optional and are not implemented unless the user opts in. Each checkpoint report states which optional property tests ran.
- Each task references specific requirements for traceability. Property tests reference the design's Correctness Property numbers (1 to 23).
- Every test builds its fixtures in `tmp_path`; the real `runs/` directory is read only by the manual tasks 3.8, 4.7 and 4.8 and by the hash commands of tasks 1.1 and 4.8.
- Docker is never used. The six protected Docker evidence volumes are never referenced.
- Pytest exit code 5 (nothing collected) is a failure. Skipped tests are reported as skipped.
- Checkpoints (1.18, 2.35, 3.9, 4.14) stop for independent user review and are not part of the technical dependency order except as waves of their own.
- Task 1.8 updates `tests/unit/test_structure.py` (a test update, allowed); M0 production code is never edited.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3"] },
    { "id": 2, "tasks": ["1.4", "1.5"] },
    { "id": 3, "tasks": ["1.6"] },
    { "id": 4, "tasks": ["1.7"] },
    { "id": 5, "tasks": ["1.8", "1.9", "1.10", "1.11", "1.12", "1.13", "1.14", "1.15", "1.16", "1.17"] },
    { "id": 6, "tasks": ["1.18"] },
    { "id": 7, "tasks": ["2.1", "2.3"] },
    { "id": 8, "tasks": ["2.2", "2.4", "2.7"] },
    { "id": 9, "tasks": ["2.5", "2.6", "2.12", "2.23", "2.29"] },
    { "id": 10, "tasks": ["2.8", "2.14", "2.19", "2.26", "2.27", "2.32", "2.33"] },
    { "id": 11, "tasks": ["2.9"] },
    { "id": 12, "tasks": ["2.10", "2.11"] },
    { "id": 13, "tasks": ["2.13", "2.15", "2.16", "2.17", "2.18", "2.20", "2.21", "2.22", "2.24", "2.25", "2.28", "2.30", "2.31", "2.34"] },
    { "id": 14, "tasks": ["2.35"] },
    { "id": 15, "tasks": ["3.1"] },
    { "id": 16, "tasks": ["3.2", "3.3", "3.4"] },
    { "id": 17, "tasks": ["3.5"] },
    { "id": 18, "tasks": ["3.6", "3.7"] },
    { "id": 19, "tasks": ["3.8"] },
    { "id": 20, "tasks": ["3.9"] },
    { "id": 21, "tasks": ["4.1", "4.2"] },
    { "id": 22, "tasks": ["4.3", "4.4"] },
    { "id": 23, "tasks": ["4.5"] },
    { "id": 24, "tasks": ["4.6"] },
    { "id": 25, "tasks": ["4.7"] },
    { "id": 26, "tasks": ["4.8"] },
    { "id": 27, "tasks": ["4.9", "4.10"] },
    { "id": 28, "tasks": ["4.11"] },
    { "id": 29, "tasks": ["4.12"] },
    { "id": 30, "tasks": ["4.13"] },
    { "id": 31, "tasks": ["4.14"] }
  ]
}
```
