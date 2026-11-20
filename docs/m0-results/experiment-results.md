# Experiment results (M0)

## Task 7.2: ordinary HTTP 500 does not terminate the Demo App

Test: `tests/docker_integration/test_ordinary_500.py`, run with `--run-docker`. All values below come from real output saved in `runs/m07-72/` (gitignored): `pytest_run.txt`, `observations.json`, `independent.txt`, `evidence.ndjson`, `teardown.txt`.

### Result

| Item | Value |
|---|---|
| Integration result | **PASS**: `2 passed in 13.17s` (`test_01_infrastructure_as_running`, `test_02_ordinary_500_does_not_terminate_demo_app`) |
| Run window (host) | 2026-11-20T10:48:03Z to 10:48:17Z |
| Git | branch `feat/coroner-m0`, HEAD `95594cf0b29c1825d49f82685076c18798aa891c` |
| Docker / Compose | Engine and client 29.9.3 / Compose v5.7.0 (Docker Desktop, Linux containers) |
| Compose project / file | `coroner-m07`, `compose.yaml`, `up -d --no-build collector demo_app` (images `coroner-collector:m0`, `coroner-demo-app:m0`, not rebuilt) |
| Run id | `m07-500-76640824` (unique; file `m07-500-76640824.jsonl` in volume `coroner-m07_coroner_evidence`) |
| Demo App container | `d6403f3f1f4a54c72803ded3e3faf1b750ebbb540f7879724319afc656dbbb41` (`coroner-m07-demo_app-1`) |
| Collector container | `8d750d887a7c...` (`coroner-m07-collector-1`), full id `8d750d887a7c296d2aa1b8efcf9eee0ccdf406b9963459af4b38e9a4a54f0df1` |
| `GET /error` | HTTP **500**, body `Internal Server Error`, answered in 0.015 s |
| `GET /health` after the 500 | HTTP **200**, 0.016 s after the 500 response was received (limit 1 s) |
| PID before / after | `pid` 1 / 1 (PID inside the container). Host `State.Pid` 3164 / 3164 |
| Probe `next_seq` | before the request 1; in the `/health` right after the 500, 2; after 4 more Samples, 6 |
| Persisted Samples | 2 before the request, 6 after waiting 2.094 s for 4 new Samples (4 new). `dropped` 0 |
| Sequence continuity | persisted `seq` 0..5 consecutive (+1 each, no gap, reset or repeat); all Samples `pid` 1 and the run id |
| `docker inspect` before | `Status=running`, `RestartCount=0`, `StartedAt=2026-11-20T10:48:11.870189803Z`, `Pid=3164` |
| `docker inspect` after | identical `Id`, `Status=running`, `Running=true`, `Restarting=false`, `Dead=false`, `OOMKilled=false`, `ExitCode=0`, `FinishedAt=0001-01-01T00:00:00Z`, `RestartCount=0`, same `StartedAt`, same `Pid` |
| Collector health | `/healthz` HTTP 200, `storage_ok` JSON `true`, `durability` `crash`; container healthy, `RestartCount=0` |

### Infrastructure as running (`docker inspect`, not compose.yaml)

- Ports: Collector `8081/tcp` to `127.0.0.1:8081`, Demo App `8080/tcp` to `127.0.0.1:8080`, no other bindings.
- Demo App: user `10001:10001`, `Memory` and `MemorySwap` both 134217728, restart policy `no`, not privileged, destructive flag env `1` (set only for this test), `CORONER_RUN_ID` as above, no mounts.
- Collector: user `10001:10001`, restart policy `on-failure`, not privileged, exactly one mount, `coroner-m07_coroner_evidence` at `/data/evidence` (rw). Neither protected volume was mounted.
- Both containers on network `coroner-m07_default`. `docker top` showed one process each: `python -m coroner.collector.main` (host PID 3077) and `python -m coroner.demo_app.main` (host PID 3164).

### Evidence read back from the Collector

`GET /v1/runs/m07-500-76640824/evidence` after the test (about 2 minutes later, stack kept running for inspection) held one `run_metadata` record and 254 `sample` records, `seq` 0 to 253, all `pid` 1, last receive time 10:50:20.540Z. The 500 occurred between `seq` 5 and 6 (`request_count` rose from 5 to 7 at `seq` 6, the `/error` plus the `/health` of the test, plus Docker healthchecks).

### Limitations and notes

- The `pid` the app reports is its PID inside the container namespace, which is 1 because Python is the container's entrypoint. A restart would also give PID 1, so `pid` alone cannot prove "no restart". Continuity is shown by the unchanged host `State.Pid` (3164), `StartedAt`, container `Id`, `RestartCount` 0, and the unbroken `seq` (a restart would reset the Probe counter).
- Only one `GET /error` was sent. `/crash`, `/leak`, kills and OOM were not used.
- Latency figures are client-side on Windows through Docker Desktop port forwarding, single request.
- The stack was kept running after the pytest run (`CORONER_KEEP_STACK=1`) only to take independent `docker inspect` / `docker top` readings, then removed with `docker compose -p coroner-m07 -f compose.yaml down` (no `-v`). Evidence file `m07-500-76640824.jsonl` (117534 bytes) remains in `coroner-m07_coroner_evidence`.
- No unexpected behaviour was observed.

## Experiment A (Task 7.3): fatal process exit via `os._exit(1)`

Real Runner runs, an independent `docker inspect` of each Demo App container, and the integration test `tests/docker_integration/test_experiment_a.py`. All values come from real output saved in `runs/m07-73/` (gitignored) and the run directories `runs/<run_id>/` (`manifest.json`, `docker_inspect.json`, `evidence.jsonl`). Host UTC dates are 2026-11-20. Git: branch `feat/coroner-m0`, HEAD `95594cf0b29c1825d49f82685076c18798aa891c`. Docker Engine / Compose as in `clock-offset.md` (29.9.3 / v5.7.0, Docker Desktop, Linux containers).

### Setup and isolation

- Compose project `coroner-m07`, file `compose.yaml`. The Runner got the project from `CORONER_COMPOSE_PROJECT=coroner-m07`, set in the launching Python process and printed right before launch (`runs/m07-73/p4_run_a.txt`). Images `coroner-collector:m0` and `coroner-demo-app:m0` (IDs unchanged, no build). Compose images were never rebuilt or retagged.
- The Runner does not start the Collector (its preamble only checks readiness, then `compose up -d --no-deps --force-recreate demo_app`). The Collector was started separately: `docker compose -p coroner-m07 -f compose.yaml up -d --no-deps --no-build collector`. Inspect: project label `coroner-m07`, single mount `coroner-m07_coroner_evidence` at `/data/evidence` (rw), healthy, `8081/tcp` bound to `127.0.0.1` only, `GET /healthz` HTTP 200 `{"status":"ok","durability":"crash",...,"storage_ok":true}` (`p2_collector_inspect.json`, `p2_healthz.txt`).
- `docker ps -a` during the runs listed only `coroner-m07-collector-1` and `coroner-m07-demo_app-1`, both with label `com.docker.compose.project=coroner-m07`. No container or network of project `coroner` was created, no new volume appeared, and neither protected volume was mounted.
- Pre-run clock check (`runs/m07-73/p3_clock_check.txt`, exit 0): 20 of 20 usable, best offset +1 ms +/- 3.0 ms, median +7 ms, range +1 to +37 ms (spread 36 ms), round trip min 6.0 / median 17.1 / max 70.5 ms. Conservative bound: 7 + 70.5 / 2 = about 42 ms, below the 125 ms limit. `CLOCK_TOLERANCE_MS` stays 250.

### Runs

| Run | How | Run id | Demo App container (full id) | Offset used by the Runner |
|---|---|---|---|---|
| 1 | direct `python -m coroner.runner run a`, exit 0, 13:39:33.8Z to 13:39:44.5Z | `a-20261120T133934Z-6d2f2350` | `78f8382504a0ae01b8eedd74c065f1db183cd666066775f9d3276b3ad6bc4e0c` | +6 ms +/- 7.9855 |
| 2 | pytest attempt 2 (see below) | `a-20261120T134213Z-06deaa12` | `0255044e7f395a7013d1b75b995600378987876adc4df0b86de10c46709c1514` | +8 ms +/- 9.388 |
| 3 | pytest attempt 3 | `a-20261120T134252Z-b51dcf11` | `48ca9b6b9f178f0d12497d0ea7f4c8743f740be35d1dee12d4ab96592122d729` | +3 ms +/- 5.148 |
| 4 | pytest attempt 4 (final, 4 passed) | `a-20261120T134321Z-a5e5a30e` | `e5dcb355e4d6d47ea1a2a3068db3c3f0a28ed8e39f3e1642d6c3aee96ea3363a` | +12 ms +/- 14.7435 |

All four runs ended `outcome: passed`, Runner exit code 0, `failure: null`, `finished: true`, `container_left_running: false`. The stack was removed with `docker compose -p coroner-m07 -f compose.yaml down` (no `-v`) between runs, after the independent inspect for each run was saved.

### Run 1 detail (Runner manifest compared with an independent `docker inspect`)

| Item | Runner manifest / raw inspect saved by the Runner | Independent `docker inspect <full id>` (`run1_inspect_raw.json`) |
|---|---|---|
| Container id | `78f8382504a0...c4e0c` (64 hex) | same (`Id`), name `/coroner-m07-demo_app-1`, label `coroner-m07` |
| `State.Status` | `exited` | `exited` |
| `State.ExitCode` | 1 | 1 |
| `State.OOMKilled` | false | false |
| `HostConfig.Memory` | 134217728 | 134217728 (`MemorySwap` 134217728) |
| `StartedAt` / `FinishedAt` | `FinishedAt` 2026-11-20T13:39:44.071608017Z (raw copy) | `StartedAt` 2026-11-20T13:39:34.926908681Z, `FinishedAt` 2026-11-20T13:39:44.071608017Z |
| `RestartCount`, host `State.Pid` | not recorded | 0, 0 (the container is stopped) |
| Image, user, mounts | not recorded | `coroner-demo-app:m0` (`sha256:23571625...c2c52`), `10001:10001`, no mounts |
| Inspect time | `inspect_time` 2026-11-20T13:39:44.385Z | taken afterwards, about 13:40 |
| Termination description | `controlled fatal process exit via os._exit(1)` (exact) | n/a |

The same comparison for runs 2, 3 and 4 (`run2_verify.txt`, `run3_verify.txt`, `run4_verify.txt`, raw inspect JSON in `run<N>_inspect_raw.json`) matched on every field: `Status` `exited`, `ExitCode` 1, `OOMKilled` false, `Memory` 134217728, `RestartCount` 0, same container id as the manifest, and the Runner's saved raw inspect equal to the independent one (including `StartedAt` and `FinishedAt`). Run 4: `StartedAt` 2026-11-20T13:43:21.634913082Z, `FinishedAt` 2026-11-20T13:43:31.052973113Z, manifest `inspect_time` 13:43:31.374Z.

### Timing, Samples and memory

| Item | Run 1 | Run 4 |
|---|---|---|
| Leak | 6 requests of 4194304 bytes, 25165824 bytes acknowledged, 5.031 s | same shape (checks passed) |
| `crash_request_time` | 2026-11-20T13:39:43.988Z | 2026-11-20T13:43:30.930Z |
| Container `FinishedAt` | 13:39:44.071608017Z (83 ms after the request) | 13:43:31.052973113Z (123 ms after the request) |
| Samples in evidence | 14 (`seq` 0 to 13, consecutive, `pid` 1, 0 malformed lines, 0 gaps) | 15 (`seq` 0 to 14, consecutive, `pid` 1, 0 malformed) |
| Samples before `crash_request_time` after clock correction (`ts` minus offset), recomputed independently | 14 of 14 | 15 of 15 |
| Last Sample (corrected host time) | 13:39:43.624Z, 364 ms before the request; Collector receive time 13:39:43.632Z | 13:43:30.664Z, 266 ms before the request |
| RSS first / peak (`rss_bytes`, no null values) | 46518272 / 74285056 bytes (+27766784, ratio 1.60; check needed at least 12582912) | 46891008 / 74387456 bytes (ratio 1.59) |
| Collector survival | `GET /healthz` HTTP 200, `storage_ok` true, checked 13:39:44.464Z, 17.413 ms, within 0.032 s of the check start (limit 5 s) | HTTP 200, `storage_ok` true, 5.477 ms, 0.016 s |
| Collector health read independently after the run | HTTP 200, `storage_ok` true, `durability` `crash` | same |
| Durability | manifest `crash`, `run_metadata` `crash` | same |
| Evidence records | 16: 1 `run_metadata`, 14 `sample`, 1 `termination` (6883 bytes) | 17: 1, 15, 1 (7324 bytes) |
| Termination record | `controlled_fatal_process_exit`, container state `exited` / 1 / false | same |
| `summary <run_id>` (read-only) | exit 0: Outcome passed, Samples 14, Gaps 0, Malformed 0, Durability `crash` | exit 0: Samples 15, Gaps 0 |

Runs 2 and 3 had 15 Samples each (RSS 46706688 to 74268672 bytes, and 46710784 to 74272768 bytes), all before the crash request after correction. The evidence read back from the Collector was byte-identical to the Runner's host copy (the copy is a prefix of, and equal in length to, the fresh read).

The word "exception" appears nowhere in the manifest or evidence of any run (checked by case-insensitive search). The Demo App log shows the last HTTP access line is `GET /health`; the `POST /crash` request is not logged because the process ends inside the handler.

### Integration test

`.\.venv\Scripts\python.exe -m pytest tests/docker_integration/test_experiment_a.py --run-docker -q -s -p no:cacheprovider -rA` (with `CORONER_KEEP_STACK=1`, so the stack could be inspected before teardown). The test starts its own Collector, launches the real Runner as a subprocess (project only in the subprocess env), and asserts: Runner exit 0, manifest fields and outcome, termination description exactly as above and no text containing "exception", `docker inspect` of the manifest's full container id (`exited`, 1, OOMKilled false, `Memory` 134217728, no mounts, label `coroner-m07`), manifest equals inspect, Collector healthy with `storage_ok` true and mounting only `coroner-m07_coroner_evidence`, nothing in project `coroner`, no new volume, at least one Sample before the crash request, consecutive `seq`, RSS growth, durability equal to `run_metadata`, one termination record.

Four executions were needed because the first three had defects in the test itself, not in the code under test (the Runner passed on every execution that reached it; outputs are kept in `runs/m07-73/p7_pytest_attempt*.txt`):

1. Setup error, 4 errors: my guard treated the persistent labelled evidence volume as an existing project resource. Nothing was started.
2. `3 passed, 1 failed`: `test_03` asserted that no resource carries the label of project `coroner`, but the existing volume `coroner_coroner_evidence` carries it (untouched, never mounted). Run 2 above.
3. `3 passed, 1 failed`: `test_03` used the name prefix `coroner-`, which also matches `coroner-m07-...`. Replaced by exact equality with the two `coroner-m07` container names. Run 3 above.
4. **`4 passed in 15.94s`** (run 4 above).

No production code, acceptance criterion or tolerance was changed to obtain the pass.

### Limitations

- Docker Desktop on Windows (Linux VM). Four runs on one host in about 4 minutes. Not a statement about other hosts or Docker versions.
- The Samples are 500 ms apart, so the interval between the last Sample and the actual termination is unobserved (shown in the manifest `limitations` and in the termination note). Experiment A's pass does not claim what the process did between its last Sample and `os._exit(1)`.
- Run 1 is the only run inspected with the Runner's own start and end timestamps in this note; runs 2 to 4 were launched by the test.
- The manifest records no `StartedAt`, `RestartCount`, host `Pid`, image or user; those values come only from the independent inspect. The host `State.Pid` is 0 because the container had already exited.
- The Demo App container was inspected after the Runner finished, once per run, from a container that still existed (exited). It was removed only after the inspect output was saved.
- Evidence files of the four runs remain in `coroner-m07_coroner_evidence` next to `m07-500-76640824.jsonl` (117534 bytes, unchanged). The volume and the `m07-verify` images were left in place.
- Experiments B and C, SIGKILL and OOM were not run.

## Experiment B (Task 7.4): SIGKILL sent by the Runner

One real Runner run, an independent `docker inspect` of the Demo App container, and the integration test `tests/docker_integration/test_experiment_b.py`. All values come from real output saved in `runs/m07-74/` (gitignored) and the run directory `runs/b-20261120T173541Z-3fa52342/` (`manifest.json`, `docker_inspect.json`, `evidence.jsonl`). Host UTC date 2026-11-20. Git: branch `feat/coroner-m0`, HEAD `c6404ced5b06a93be8f3968d5bc3bbacd492ce1a`. Docker Engine 29.9.3, Compose v5.7.0, Docker Desktop, Linux containers. Exactly one SIGKILL was sent, only by the Runner through `DockerPort.kill` (`docker kill --signal KILL <full 64-hex id>`). Nothing else sent a signal, and no `/crash` request was made.

### Result

| Item | Value |
|---|---|
| Run id | `b-20261120T173541Z-3fa52342` |
| Command | `.\.venv\Scripts\python.exe -m pytest tests/docker_integration/test_experiment_b.py --run-docker -q -s -p no:cacheprovider -rA` (env `CORONER_KEEP_STACK=1`; no `COMPOSE_*` or other `CORONER_*` variables; `DOCKER_HOST` and `DOCKER_CONTEXT` unset), 2026-11-20T17:35:36.146Z to 17:35:53.604Z |
| Runner command line started by the test | `python -m coroner.runner run b --runs-dir <repo>/runs`, with `CORONER_COMPOSE_PROJECT=coroner-m07` in the subprocess env only |
| pytest exit code | 0 (`6 passed in 16.51s`) |
| Runner process exit code | 0 (distinct from the Docker exit code). Output: `Experiment B b-20261120T173541Z-3fa52342: PASSED - Experiment B: all 7 mandatory checks passed`. Manifest `outcome` `passed`, `failure` null, `finished` true, `container_left_running` false |
| Container id (full) | `d8f19b8abc9191dd5aaf34f272e08d079cb68bc61c5e677d0c690050b07dc917`, name `coroner-m07-demo_app-1` |
| `sigkill_sent_time` (manifest, host clock, taken immediately before the kill call) | 2026-11-20T17:35:51.702Z |
| `State.FinishedAt` (real `docker inspect`) | 2026-11-20T17:35:51.865093887Z (manifest `container_finished_at` 17:35:51.865Z) |
| Docker state | `Status` `exited`, `ExitCode` 137, `OOMKilled` false, `RestartCount` 0 |
| Clock offset used | +6 ms, uncertainty 8.469 ms (Runner preamble; positive means the Collector clock is ahead of the host) |
| Tolerance | `CLOCK_TOLERANCE_MS` 250 (unchanged) |

### Setup, isolation and clock gate

- Compose project `coroner-m07`, file `compose.yaml`, explicit `-p coroner-m07 -f compose.yaml` on every compose command. Images `coroner-collector:m0` (`cd9126e56533...`) and `coroner-demo-app:m0` (`23571625f731...`) were not built, tagged or removed. No container, network or volume of project `coroner` was created, and the two protected volumes were not mounted.
- Single-run gate (Docker-free, `runs/m07-74/gate_output.txt`): default `--collect-only` shows `no tests collected (6 deselected)`; with `--run-docker` 6 tests are collected. An AST check found exactly one `subprocess.run` that starts `coroner.runner`, inside the module-scoped fixture `experiment` (line 236); `evidence` and `demo_info` are module-scoped and read only; no test calls the Runner, a kill, or `/crash`; no parametrization, rerun or retry; `pytest-rerunfailures` and `xdist` are not installed. `DockerPort.kill` is called from one place (`exp_b.py`, after the leak), and the Runner has no retry around it. The fixture fails early if any `coroner-m07` container or network exists, so the clock-gate Collector was removed with `down` (no `-v`) before pytest, and the test started its own Collector.
- Clock gate (`runs/m07-74/clock_check_out.txt`, exit 0): 20 of 20 usable, best offset +0 ms with uncertainty 2.6 ms, median +9 ms, range +0 to +31 ms (spread 31 ms), round trip min 5.1 / median 24.1 / max 65.2 ms. Conservative bound: 9 + 65.2 / 2 = 41.6 ms, below the 125 ms limit.
- Collector (gate instance): healthy, label `coroner-m07`, single mount `coroner-m07_coroner_evidence` at `/data/evidence` (rw), user 10001:10001, `8081/tcp` bound to `127.0.0.1` only, `GET /healthz` HTTP 200 `{"status":"ok","durability":"crash",...,"storage_ok":true}`.

### Independent inspect compared with the Runner (`runs/m07-74/demo_inspect_independent_bytes.json`, taken 2026-11-20T17:37:30.268Z)

| Field | Independent `docker inspect <full id>` | Runner raw inspect / manifest |
|---|---|---|
| `Id` | `d8f19b8a...7dc917` (64 hex) | same (`container_id`) |
| `Name`, project label, service | `/coroner-m07-demo_app-1`, `coroner-m07`, `demo_app` | same raw inspect |
| `State.Status` / `Running` | `exited` / false | `exited` |
| `State.ExitCode` / `OOMKilled` | 137 / false | 137 / false |
| `State.StartedAt` | 2026-11-20T17:35:42.119258226Z | same (raw inspect) |
| `State.FinishedAt` | 2026-11-20T17:35:51.865093887Z | same (raw inspect); manifest 17:35:51.865Z (0.093 ms rounding) |
| `RestartCount` | 0 | same |
| `HostConfig.Memory` / `MemorySwap` | 134217728 / 134217728 | `host_config_memory_bytes` 134217728 |
| Image, user, mounts | `coroner-demo-app:m0` (`sha256:23571625...c2c52`), `10001:10001`, none | same |

The independent inspect output is byte-identical to the Runner's saved `docker_inspect.json` (9853 bytes). A first independent capture through PowerShell redirection (`demo_inspect_independent.json`, taken 17:36:47.82Z) was UTF-16 encoded and was kept unchanged next to the byte-exact capture.

SIGKILL timeline, from real values: `sigkill_sent_time` 17:35:51.702Z; `State.FinishedAt` 17:35:51.865093887Z, which is 163.093 ms later; the Runner recorded phase `sigkill_sent` (the `docker kill` call had returned) at 17:35:52.116Z, 414 ms after `sigkill_sent_time`; first inspect showing `exited` at 17:35:52.226Z. So `FinishedAt` is not before `sigkill_sent_time` (difference +163.093 ms, larger than the measured offsets of +6 ms used by the Runner and +0 to +31 ms in the clock gate). The time between the timestamp and the exit is the `docker kill` CLI start-up and daemon latency, which this run did not separate from clock offset. `FinishedAt` is a Docker daemon time, `sigkill_sent_time` is a host time; the comparison assumes the daemon (VM) clock and the host clock agree within the measured offset.

### Samples, timing and memory

Evidence read back three ways was byte-identical (7319 bytes): the Runner's `evidence.jsonl`, the Collector's stored file read (read-only) from `coroner-m07_coroner_evidence` (`b-20261120T173541Z-3fa52342.jsonl`, sha256 `00e8f5df59de237c4c4c95951917d5cd5b9a855a0e7e2d52840de5dd18150b94`), and `GET /v1/runs/<run_id>/evidence`. 17 records: 1 `run_metadata`, 15 `sample`, 1 `termination`; no gap, anomaly, rejection or repair records, no record type outside the known set (no shutdown-handler records), 0 malformed lines.

| Item | Value |
|---|---|
| Sample `seq` | 0 to 14, consecutive; `ts` ascending, spacing about 500 ms |
| Capture time (`ts` minus 6 ms) before the kill | 15 of 15 (independent recomputation equals the manifest) |
| Late-received (pre-kill capture, received after the kill) | 0 |
| Within tolerance (kill to kill + 250 ms inclusive) | 0 |
| Beyond tolerance (must be 0) | 0 |
| After container stop (informational, `FinishedAt` available) | 0 |
| Last Sample | `seq` 14, `ts` 17:35:51.393Z, corrected capture 17:35:51.387Z, 315 ms before `sigkill_sent_time` and 478 ms before `FinishedAt`; received at 17:35:51.401Z (307 ms before the kill) |
| RSS first / peak | 46903296 / 74399744 bytes (+27496448; leaked 25165824 in 6 requests of 4194304 bytes over 5.031 s; check needed at least 12582912); manifest `highest_rss_bytes` 74399744 |
| Termination record | present, `cause_label` `sigkill_sent_by_runner`, container state `exited` / 137 / false, `observed_time` 17:35:52.226Z, note that the interval to the real termination is unobserved |
| Durability | `crash` in `run_metadata` and in the manifest |
| `summary <run_id> --runs-dir runs` | exit 0: Outcome passed, Samples 15, Gaps 0, Unreconciled 0, Malformed 0, `sample timing: pre_kill=15 (late_received=0) within_tolerance=0 beyond_tolerance=0 after_container_stop=0 tolerance_ms=250 clock_offset_ms=6` |

All 10 manifest checks passed: `exit_code_137`, `oom_killed_false`, `stopped_within_10s`, `collector_survival`, `at_least_one_sample_captured_before_kill`, `no_sample_captured_after_kill_beyond_tolerance`, `no_shutdown_handler_records`, `termination_recorded`, `durability_matches_run_metadata`, `memory_growth_matches_leak`.

### Collector survival

Runner check at 17:35:52.308Z: HTTP 200, `storage_ok` true, 20.796 ms. Test check at 17:35:52.584Z: HTTP 200, `storage_ok` true, `durability` `crash`. Independent read after the run (2026-11-20T17:36:58Z): HTTP 200, `storage_ok` true, uptime 77 s. The Collector container was running with `RestartCount` 0.

### Integration test

`6 passed in 16.51s`: `test_01` Runner exit 0 and manifest outcome, `test_02` independent inspect equals manifest, `test_03` pre-kill Samples and none beyond tolerance, `test_04` memory growth and no shutdown records, `test_05` termination record, `test_06` Collector healthy and isolated. The test ran once, with no retry. Output: `runs/m07-74/pytest_stdout.txt`.

### Limitations and notes

- Docker Desktop on Windows (Linux VM). One run in one Docker Desktop session. Not a statement about other hosts or Docker versions.
- The Samples are 500 ms apart, so the interval between the last Sample (478 ms before `FinishedAt`) and the actual termination is unobserved. A process that was killed between Samples leaves no data about that interval.
- `sigkill_sent_time` is taken before the `docker kill` CLI starts; the 163 ms to `FinishedAt` includes CLI start-up and daemon latency. The clock domains (host, Docker daemon VM, Collector) are assumed to agree within the measured offset; the daemon-versus-host offset was not measured separately.
- The manifest field `memory` is null in this run (memory is recorded in `container_state.host_config_memory_bytes` and `highest_rss_bytes`).
- The evidence volume `coroner-m07_coroner_evidence` now also holds `b-20261120T173541Z-3fa52342.jsonl` (7319 bytes). The four Experiment A files and `m07-500-76640824.jsonl` (117534 bytes) have unchanged sizes and sha256 hashes (compared before and after the run).
- Experiment C and any OOM were not run.

## Experiment C (Task 7.5): genuine Docker OOM kill

### Result

PASS. Run id `c-20261120T174958Z-94f4225c`. The Demo App (limit 128 MiB, no swap) was killed by the kernel cgroup memory limit while the Runner leaked 4 MiB chunks through `POST /leak`. Docker reports `State.Status` `exited`, `ExitCode` 137, `OOMKilled` true. The Runner never sent a signal, never called `docker kill` and never called `/crash`.

- Runner command (executed by the test fixture, once): `python -m coroner.runner run c --runs-dir <repo>/runs`, exit code 0, output `Experiment C c-20261120T174958Z-94f4225c: PASSED - Experiment C: all 6 mandatory checks passed`. Runner duration 26.72 s.
- Test command (run once, with `CORONER_KEEP_STACK=1`): `.\.venv\Scripts\python.exe -m pytest tests/docker_integration/test_experiment_c.py --run-docker -q -s -p no:cacheprovider -rA`. Result: `7 passed in 31.81s`, pytest exit code 0.
- Container id: `42a89ff3f2a6f7907cd12913feae448f541ecefc67703e613ec94ff5c20fed44`, name `coroner-m07-demo_app-1`, image `coroner-demo-app:m0` (`sha256:23571625f7319525505816774be12f752854b04608b9b0912ef9d1e1b5ca2c52`), user `10001:10001`, restart policy `no`, `RestartCount` 0, no mounts (`HostConfig.Tmpfs` is `/tmp` size 8m), label project `coroner-m07`.
- `StartedAt` 2026-11-20T17:49:58.687986556Z, `FinishedAt` 2026-11-20T17:50:23.82152454Z (about 25.13 s of run time).
- Memory: `HostConfig.Memory` 134217728 and `HostConfig.MemorySwap` 134217728 (equal, no extra swap). `docker compose config` printed `mem_limit` and `memswap_limit` as `"134217728"`.

### Setup, isolation and clock gate

- Host: Docker Desktop 4.99.0 on Windows, Docker Engine 29.9.3, Compose v5.7.0, `OSType` linux, Cgroup Version 2, cgroup driver `cgroupfs`, kernel `6.18.58.1-microsoft-standard-WSL2`, 8 CPUs, `Total Memory` 7.687GiB (Docker VM), host free physical memory 4270836 KiB of 16630520 KiB before the run. No `COMPOSE_*`, `CORONER_*` or `DOCKER_*` variables were set in the process.
- Before the run: no container, only the networks `bridge`, `host`, `none`, three volumes, ports 8080 and 8081 free. Compose project `coroner-m07` only (`-p coroner-m07 -f compose.yaml`); no container, network or volume of project `coroner` was created. Neither protected volume was mounted; their `CreatedAt` values are unchanged (`coroner_coroner_evidence` 2026-11-18T17:19:06Z, `coroner-spike_coroner_spike_evidence` 2026-11-14T10:56:17Z).
- Images were not built, tagged or removed. Image ids before and after: `coroner-collector:m0` `cd9126e5...bf0420e`, `coroner-demo-app:m0` `23571625...c2c52`, plus the unchanged `:m07-verify` images and the Python base image.
- Single-run check of the test: one module-scoped fixture, one Runner subprocess call site (AST count 1), no parametrize, no retry, no xdist or rerunfailures installed (`runs/m07-75/venv_pip_list.txt`). Default collection deselects all 7 tests.
- Clock gate (fresh Collector, 20 of 20 usable): best-measurement offset +1 ms, best uncertainty 2.4 ms, median offset +7 ms, range +1 to +24 ms, round trip min 4.8 / median 19.8 / max 53.0 ms (worst single uncertainty 26.5 ms), exit code 0. The Runner's own clock estimate during the run was +5 ms with uncertainty 6.731 ms. Stack was torn down (no `-v`) before the run, as the fixture requires.

### Independent inspect compared with the Runner (`runs/m07-75/demo_inspect_independent_bytes.json`, taken 2026-11-20T17:50:55.097Z)

The independent `docker inspect` output is byte-identical to the Runner's `runs/c-20261120T174958Z-94f4225c/docker_inspect.json` (10358 bytes, a JSON diff reported no difference).

| Item | Runner manifest | Independent inspect |
|---|---|---|
| Container id | `42a89ff3...0fed44` (64 hex) | same |
| `State.Status` | `exited` | `exited` |
| `State.ExitCode` | 137 | 137 |
| `State.OOMKilled` | true | true |
| `HostConfig.Memory` | 134217728 | 134217728 |
| `HostConfig.MemorySwap` | not recorded in the manifest | 134217728 |
| Finished time | `container_finished_at` 2026-11-20T17:50:23.821Z | `FinishedAt` 2026-11-20T17:50:23.82152454Z (0.524 ms rounding difference) |
| Image, user, mounts | not recorded | `coroner-demo-app:m0`, `10001:10001`, none |

`docker logs` of the Demo App: 36 lines, 22 `POST /leak` lines (equal to `successful_requests`), no `/crash` text. uvicorn does not log `/crash` because the handler would end the process with `os._exit`, so the verdict (exit 137 plus OOMKilled true, where `/crash` would give exit 1) is the real protection.

### Leak, Samples and memory

- Leak: 23 requests, 22 successful, 92274688 cumulative bytes (22 x 4194304), first call 17:50:01.742Z, duration 22.156 s. The 23rd call failed with `RemoteDisconnected: Remote end closed connection without response`, which is the kill itself. The Runner phase `leak_failed` (17:50:23.817Z) is 4 ms before `FinishedAt`.
- Evidence read three ways was byte-identical (21031 bytes, sha256 `a5e8de221f7da1e9f51d77b0f7103892be97d890fb39dd1fca8e01756403f146`): the Runner's `runs/c-20261120T174958Z-94f4225c/evidence.jsonl`, `GET /v1/runs/<run_id>/evidence`, and the Collector's stored file (read-only) `c-20261120T174958Z-94f4225c.jsonl` in `coroner-m07_coroner_evidence`. 48 records: 1 `run_metadata`, 46 `sample`, 1 `termination`.
- Sample `seq` 0 to 45, consecutive, 0 gaps, 0 malformed lines, 0 null RSS. All 46 Samples were received before `FinishedAt` (host clock after the +5 ms offset). `container_id` on every Sample is the full id (source `mountinfo`).
- RSS (capture order): first 46362624, highest 138522624 bytes (+92160000), 46 readable values. Series: 46362624, 48664576, 52862976, 52862976, 57061376, 57061376, 61259776, 61259776, 65458176, 65458176, 69656576, 69656576, 73854976, 73854976, 78053376, 78053376, 82251776, 82251776, 86450176, 86450176, 90648576, 90648576, 94846976, 94846976, 99045376, 99045376, 103243776, 103243776, 107442176, 107442176, 111640576, 111640576, 115838976, 115838976, 120037376, 120037376, 124235776, 124235776, 128434176, 128434176, 132308992, 129662976, 134324224, 134324224, 138522624, 138522624. The series is not strictly non-decreasing (one dip, 132308992 to 129662976, near the end).
- Last Sample: `seq` 45, `ts` 17:50:23.586Z, RSS 138522624; 235.5 ms before `FinishedAt`.
- Termination record: one, `cause_label` `docker_oom_kill`, container state `exited` / 137 / true, `observed_time` 17:50:24.147Z. No `sigkill_sent_by_runner`; manifest `times` are both null.
- Durability: `crash` in `run_metadata` and in the manifest.

### Collector survival

Runner check at 17:50:24.220Z: HTTP 200, `storage_ok` true, 15.913 ms. Test check: HTTP 200, `storage_ok` true, `durability` `crash`. Independent read at about 17:50:56Z: HTTP 200, `storage_ok` true, uptime 59 s. The Collector mounted only `coroner-m07_coroner_evidence` at `/data/evidence`, `RestartCount` 0, port 8081 bound to `127.0.0.1` only.

### Manifest checks, diagnostics and limitations

- All 9 checks passed: the six mandatory (`oom_killed_true`, `exit_code_137`, `collector_survival`, `preserved_precrash_evidence` (46 Samples), `measurable_memory_growth` (peak 138522624 vs first 46362624), `docker_inspect_recorded`) and `memory_limit_matches_host_config`, `durability_matches_run_metadata`, `termination_recorded`. Manifest `outcome` `passed`, `container_left_running` false, `container_state_problems` empty.
- Diagnostics (informational only): `rss_monotonic_tail` true, `peak_rss_ratio_of_limit` 1.032073974609375, `sample_count` 46.
- `sampling_limitations` (interval 500 ms, approximate leak rate 4164771 bytes/s): (1) Sample_Interval is 500 ms while the leak adds about 4.0 MiB per second, so up to about 2.0 MiB of growth can occur between two Samples; (2) the final seconds before the out-of-memory kill may be unobserved: the last Sample can predate the kill; (3) the peak RSS in the Samples may understate the container memory limit that triggered the kill.
- `summary c-20261120T174958Z-94f4225c --runs-dir runs`: exit 0, Outcome passed, Samples 46, Gaps 0, Unreconciled 0, Malformed 0, `docker_oom_kill`, durability `crash` (`runs/m07-75/summary_cli.txt`).

### Integration test

`7 passed in 31.81s`: `test_01` Runner exit 0, manifest outcome passed, `test_02` independent inspect equals manifest and is a genuine OOM, `test_03` no Runner kill or crash call, `test_04` pre-termination Samples and RSS growth, `test_05` verdict checks, diagnostics and limitations, `test_06` termination record and durability, `test_07` Collector survival and isolation. The OOM run was executed once, with no retry. Output and observations: `runs/m07-75/pytest_stdout.bin`, `runs/m07-75/observations.json`.

### Evidence preservation and teardown

Evidence is kept in `runs/c-20261120T174958Z-94f4225c/` (`manifest.json`, `docker_inspect.json`, `evidence.jsonl`) and `runs/m07-75/`. After the run, `docker compose -p coroner-m07 -f compose.yaml down` (no `-v`) removed the two containers and the network `coroner-m07_default`. The three volumes remain. The evidence files present before the run (`m07-500-76640824.jsonl` 117534 bytes, four `a-*.jsonl`, `b-20261120T173541Z-3fa52342.jsonl` 7319 bytes) have unchanged sizes and sha256 hashes (`runs/m07-75/volume_pre.txt` against `volume_post.txt`). The new `c-20261120T174958Z-94f4225c.jsonl` (21031 bytes) was added.

### Limitations and notes

- Docker Desktop on Windows (WSL2 Linux VM). One run in one Docker Desktop session. This is not a statement about other hosts or Docker versions.
- Peak Sample RSS (138522624 bytes) is 1.032 times the cgroup limit (134217728). The Samples report process RSS read inside the container, while the kernel enforces the limit on cgroup memory accounting, so the two are different measures and the ratio is not a threshold. It is recorded as a diagnostic only.
- The last Sample precedes `FinishedAt` by 235.5 ms, and Samples are 500 ms apart, so the interval between the last Sample and the exact kill is unobserved. The leak adds about 4 MiB per second.
- One RSS decrease occurred near the end of the series (132308992 to 129662976), while the manifest diagnostic `rss_monotonic_tail` is true; the diagnostic does not look at the whole series.
- The clock domains (host, Docker daemon VM, Collector) are assumed to agree within the measured offset; the daemon-versus-host offset was not measured separately.
- The first attempt of the clock-gate helper script was interrupted by the tooling shell before it wrote its output; the Collector it had started was reused by the second attempt (`up -d` reported it already running). No OOM run or Docker side effect other than that Collector start occurred.

## Cross-validation (Task 7.6)

Existing real run evidence was re-read with independent code. No experiment, kill, OOM, `/crash`, teardown, rebuild or volume operation was performed for this task. The only Docker command used was `docker ps -a`, which listed no containers. All checks read the saved files under `runs/` (gitignored): the run directories, `runs/m07-72` to `runs/m07-75`, and the scratch output in `runs/m07-76/` (`xval.py`, `cli.py`, `out_main.txt`, `cli_out.txt`, `out_hash_start.txt`, `out_hash_end.txt`, `hashes_*.json`). No fresh `docker inspect` was taken after teardown; "independent inspect" below means the independent `docker inspect` output saved at run time (`runs/m07-73/run<N>_inspect_raw.json`, `runs/m07-74/demo_inspect_independent_bytes.json`, `runs/m07-75/demo_inspect_independent_bytes.json`). Git: branch `feat/coroner-m0`, HEAD `490d79f5722157695fecdf1f148cb4a6d48440b3`.

Method: `coroner.runner.manifest.read_manifest` for each manifest, plain `json.loads` per line for each `evidence.jsonl`, `CLOCK_TOLERANCE_MS` imported from `coroner.runner.evaluate` (250, asserted), sample classification recomputed from `ts` minus `clock_offset_ms`.

### Observed results

| Item | A (primary run 1) | B | C | Ordinary 500 (7.2) |
|---|---|---|---|---|
| Run id | `a-20261120T133934Z-6d2f2350` | `b-20261120T173541Z-3fa52342` | `c-20261120T174958Z-94f4225c` | `m07-500-76640824` |
| Container id (full 64 hex, equals manifest) | `78f8382504a0ae01b8eedd74c065f1db183cd666066775f9d3276b3ad6bc4e0c` | `d8f19b8abc9191dd5aaf34f272e08d079cb68bc61c5e677d0c690050b07dc917` | `42a89ff3f2a6f7907cd12913feae448f541ecefc67703e613ec94ff5c20fed44` | `d6403f3f1f4a54c72803ded3e3faf1b750ebbb540f7879724319afc656dbbb41` |
| Docker status / ExitCode / OOMKilled | `exited` / 1 / false | `exited` / 137 / false | `exited` / 137 / **true** | `running` / 0 / false, `RestartCount` 0 |
| Runner exit, manifest outcome | 0 (documented), `passed` | 0 (documented), `passed` | 0 (documented), `passed` | not applicable (pytest `2 passed`) |
| Manifest checks | 8 of 8 passed | 10 of 10 passed | 9 of 9 passed (six mandatory plus three) | not applicable |
| Collector survival (manifest) | HTTP 200, `storage_ok` true, 0.032 s | HTTP 200, `storage_ok` true, 0.015 s | HTTP 200, `storage_ok` true, 0.015 s | `/healthz` 200, `storage_ok` true (`observations.json`) |
| Evidence records (manifest = file, 0 malformed) | 16 (1 `run_metadata`, 14 `sample`, 1 `termination`) | 17 (1, 15, 1) | 48 (1, 46, 1) | 1 `run_metadata` plus 254 `sample` read back in 7.2 (file 117534 bytes) |
| Samples, `seq` | 14, 0..13 consecutive | 15, 0..14 consecutive | 46, 0..45 consecutive | 6 persisted `seq` 0..5 consecutive, all `pid` 1 |
| Every record `run_id` equals the run id, `ts` ascending, every Sample `container_id` is the full id | yes | yes | yes | Samples: run id and `pid` 1 (from the saved observations) |
| Termination record | `controlled_fatal_process_exit`, `exited` / 1 / false | `sigkill_sent_by_runner`, `exited` / 137 / false | `docker_oom_kill`, `exited` / 137 / true | none (process kept running) |
| Durability, manifest vs `run_metadata` | `crash` = `crash` | `crash` = `crash` | `crash` = `crash` | `crash` (Collector `/healthz`) |
| Clock offset used by the Runner | +6 ms | +6 ms | +5 ms | not used |
| Manifest vs independent inspect (status, ExitCode, OOMKilled, `Memory`, `Id`) | equal | equal | equal (`Memory` and `MemorySwap` 134217728) | before and after observations agree (same `Id`, `StartedAt`, host `Pid` 3164) |
| Saved `docker_inspect.json` vs independent inspect | byte-identical | byte-identical | byte-identical | not applicable |
| `summary <run_id> --runs-dir runs` | exit 0 | exit 0 (`Sample timing` line) | exit 0 (`Diagnostics` and `Limitations` lines) | not applicable |
| `Get-Content ... -TotalCount 3` and `Select-String` for `"record_type":"termination"` | exit 0; termination at line 16 | exit 0; line 17 | exit 0; line 48 | not applicable |

Run-specific checks:

- **A.** `crash_request_time` 2026-11-20T13:39:43.988Z; `FinishedAt` 13:39:44.071608017Z (83 ms later). Samples before `crash_request_time` after correction (`ts` minus 6 ms): 14 of 14. RSS first 46518272, peak 74285056 (+27766784, ratio 1.60; manifest `highest_rss_bytes` 74285056). Termination description is exactly `controlled fatal process exit via os._exit(1)`. The word "exception" is absent (case-insensitive) from the manifest and evidence of all four A runs. Independent inspect: `StartedAt` 13:39:34.926908681Z, `RestartCount` 0.
- **A, three additional runs** (same checks, same results): `a-20261120T134213Z-06deaa12` (container `0255044e...c1514`, offset +8 ms, 15 Samples, 15 of 15 before the request, `FinishedAt` 27 ms after it), `a-20261120T134252Z-b51dcf11` (`48ca9b6b...2d729`, +3 ms, 15, 15 of 15, 56 ms), `a-20261120T134321Z-a5e5a30e` (`e5dcb355...3363a`, +12 ms, 15, 15 of 15, 122 ms). All exit 1, `OOMKilled` false, `exited`, manifest equal to the saved independent inspect, termination description exact. The Docker figures agree with the Experiment A sections above.
- **B.** `sigkill_sent_time` 2026-11-20T17:35:51.702Z; real `FinishedAt` 17:35:51.865093887Z (163 ms later). Independent recomputation with `ts` minus 6 ms and tolerance 250: pre-kill 15, late-received 0, within tolerance 0, beyond tolerance 0, after container stop 0, equal to the manifest `sample_timing`. Last Sample `seq` 14. RSS first 46903296, peak 74399744 (manifest `highest_rss_bytes` equal). `cause_label` `sigkill_sent_by_runner`.
- **C.** `HostConfig.Memory` 134217728 and `MemorySwap` 134217728. First RSS 46362624, highest 138522624, null RSS values 0. Termination cause `docker_oom_kill`; manifest `times` both null and no phase containing `kill` or `crash`. Saved Demo App log (`runs/m07-75/demo_logs.txt`, 38 lines): 22 `POST /leak` lines, 0 lines containing `/crash`. Manifest `diagnostics` `rss_monotonic_tail` true, `peak_rss_ratio_of_limit` 1.032073974609375, `sample_count` 46. All three `sampling_limitations` statements are present (interval 500 ms, approximate leak rate 4164771 bytes/s), and the `summary` output prints them.
- **Ordinary 500.** From `runs/m07-72/observations.json`: `GET /error` 500 in 0.015 s; `GET /health` 200 0.016 s after the 500 response (limit 1 s); `pid` 1 before and after, host `State.Pid` 3164 before and after, `RestartCount` 0, `Status` `running`, `next_seq` 1 before and 6 after, `dropped` 0. The saved volume listings (`runs/m07-74/evidence_vol_after_teardown.txt`, `runs/m07-75/volume_post.txt`) show `m07-500-76640824.jsonl` at 117534 bytes with sha256 `3adab6c46307e0cf9ef870ef32d19491bc99d541969072ffe9ec5e09e8211984`. The 254-Sample read-back is documented in 7.2; the file itself was not re-read, only the saved listings.

### Evidence integrity

sha256 of `evidence.jsonl`, `manifest.json` and `docker_inspect.json` for the six run directories (18 files) was computed before and after this work (`runs/m07-76/hashes_hash_start.json`, `hashes_hash_end.json`). All 18 hashes are unchanged. The scratch drivers only read these files.

### RSS diagnostic (`rss_monotonic_tail`) for Experiment C

`diagnostic_metrics` in `coroner/runner/evaluate.py` sorts Samples by capture time (`ts`), takes the last `min(5, n)` Samples (`DIAGNOSTIC_TAIL_SIZE` 5), and returns `None` when there are no Samples or any tail RSS is null. Otherwise it returns `all(a <= b)` over consecutive pairs, so equal values count as non-decreasing and only a decrease gives false.

Last five readable RSS values in capture order (capture order equals `seq` order):

| `seq` | `ts` | `rss_bytes` |
|---|---|---|
| 41 | 2026-11-20T17:50:21.589Z | 129662976 |
| 42 | 2026-11-20T17:50:22.086Z | 134324224 |
| 43 | 2026-11-20T17:50:22.587Z | 134324224 |
| 44 | 2026-11-20T17:50:23.088Z | 138522624 |
| 45 | 2026-11-20T17:50:23.586Z | 138522624 |

The independent recomputation gives true, equal to the manifest. The series has exactly one decrease, 132308992 at `seq` 40 to 129662976 at `seq` 41. The tail starts at `seq` 41, so the pair (40, 41) is not compared: the dip lies **outside** the evaluated tail (the lower value is the first element of the tail, the higher one precedes it). The diagnostic covers only its defined tail, not the full series. No defect was found in the evaluated tail, and no code was changed.

Peak RSS 138522624 against the limit 134217728 gives 1.032073974609375 (the same as the manifest). This is informational. RSS is the process-resident memory the Probe samples; the kernel enforces the limit on cgroup accounting, which is a different measure. The verdict is unchanged: it rests on `OOMKilled` true and exit code 137.

### Unresolved anomalies and limitations

- No unresolved anomaly. Typos corrected in earlier sections: none.
- A discrepancy that is explained, not new: `rss_monotonic_tail` is true while the full series has one dip (above).
- Single Docker Desktop (WSL2) session on one Windows host. Experiments B and C were each executed once and A four times; no repeat-consistency claim is made, and nothing here covers a Linux host or other Docker versions.
- After teardown there is no live container to inspect, so inspect-based checks rely on the files saved at run time. The independent inspect for A, B and C is byte-identical to the Runner's saved inspect, so it confirms consistency of the two captures, not a second observation of a live container.
- The Samples are 500 ms apart: the interval between the last Sample and the actual termination is unobserved in every experiment.
- The Docker `FinishedAt` and host clock domains are assumed to agree within the measured offset.
