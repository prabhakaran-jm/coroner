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
