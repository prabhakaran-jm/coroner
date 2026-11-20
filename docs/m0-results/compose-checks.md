# M0.5 Compose Isolation and Health Checks (Task 5.7)
Recorded 2026-11-18 on the host (Windows, PowerShell, Docker Desktop, Compose v5.7.0), branch `feat/coroner-m0`, HEAD `400e592`. Project `coroner`, file `compose.yaml`, images `coroner-collector:m0` and `coroner-demo-app:m0` (prebuilt in task 5.5; tests use `up -d --no-build`). No `/crash` request was sent and no OOM experiment was run. Teardown is plain `docker compose down` (no `-v`).

## How it was checked
- Live test module `tests/docker_integration/test_compose_isolation.py`, run with `--run-docker`. Assertions read the running containers through `docker inspect`, `docker exec` and `docker top`, not `compose.yaml`.
- Run 1 (this module alone): `18 passed in 21.07s`, exit 0, no skips (`runs/m05-docker/compose-isolation-run.txt`, gitignored).
- Run 2 (whole `tests/docker_integration` directory in one process, before task 5.8): `27 passed in 67.43s`, exit 0 = 18 (5.7) + 8 (5.6, `test_volume_persistence.py`) + 1 (`test_placeholder.py`). The `test_placeholder.py` test is `assert True` and is NOT runtime proof (see "Which tests prove something" below). In the task 5.8 acceptance run the directory has 34 tests: 7 (read-only Collector root, `test_collector_readonly_rootfs.py`) + 18 + 8 + 1. The two Docker modules did not conflict: the 5.7 module runs first and ends with `compose down`, the 5.6 module then starts the Collector on the same surviving volume with unique run ids (`runs/m05-docker/docker-integration-combined.txt`).
- Independent manual collection of the same facts from the running stack, saved in `runs/m05-docker/`: `inspect-coroner-collector-1.json`, `inspect-coroner-demo_app-1.json`, `top-collector.txt`, `top-demo.txt`, `compose-ls.txt`, `compose-ps.txt`, `network-inspect.json`, `final-state.txt`.

## 16-point table
Each test number is the test function in the module. Tests 14 and 16 are parametrized over both images and each case passed (18 test cases for 16 points).

| # | Check | Result | Source (live) |
|---|---|---|---|
| 1 | Both services reach `running` within 60 s of `up -d --no-build` (not restarting, not dead) | PASS | test_01; manual `up` showed both `Up`, collector and demo healthy about 19 s after creation |
| 2 | Collector becomes `healthy` | PASS | test_02; `State.Health.Status` = `healthy` |
| 3 | Collector health test requires HTTP 200 and `storage_ok is True`; live `/healthz` returns 200 with `storage_ok: true` | PASS | test_03; `Config.Healthcheck.Test` from inspect; GET `http://127.0.0.1:8081/healthz` |
| 4 | Demo_App starts only after the Collector is healthy (`depends_on: service_healthy`; start time is at least one health interval after the Collector's) | PASS | test_04; `docker compose config --format json`, `State.StartedAt` of both |
| 5 | Both ports published on `127.0.0.1` only (8081 collector, 8080 demo); nothing on `0.0.0.0` or `::` | PASS | test_05; `HostConfig.PortBindings` and `NetworkSettings.Ports`; `docker compose port` printed `127.0.0.1:8081` and `127.0.0.1:8080` |
| 6 | Demo_App `HostConfig.Memory` = 134217728 | PASS | test_06; manual inspect `Mem=134217728` |
| 7 | Demo_App `MemorySwap` equals `Memory` | PASS | test_07; manual inspect `Swap=134217728` |
| 8 | Demo_App not privileged; `CapDrop` = ALL, no `CapAdd`, `no-new-privileges:true`, user `10001:10001` | PASS | test_08; manual inspect `Priv=false`, `CapDrop=["ALL"]`, `SecOpt=["no-new-privileges:true"]` |
| 9 | Demo_App has no mounts, no evidence volume, no binds | PASS | test_09; manual inspect `Mounts=[]` |
| 10 | Demo_App root filesystem is read-only | PASS | test_10; manual inspect `RO=true` |
| 11 | Demo_App `/tmp` is a tmpfs (`size=8m`) | PASS | test_11; `HostConfig.Tmpfs={"/tmp":"size=8m"}` and `/proc/mounts` inside the container |
| 12 | Restart policies as designed: Collector `on-failure` max 5, Demo_App `no` | PASS | test_12; manual inspect `{"Name":"on-failure","MaximumRetryCount":5}` and `{"Name":"no","MaximumRetryCount":0}` |
| 13 | With the flag at its default `0`, `/crash`, `/leak`, `/error` return 404 (and `/health` 200) | PASS | test_13; the test asserts the container env flag is exactly `0` first and sends GET requests only, so `/crash` could not reach a handler |
| 14 | PID 1 is `python -m coroner.<service>.main` in both containers with no other process (single worker, no supervisor) | PASS | test_14 (2 cases); `/proc/1/cmdline` printed `python -m coroner.collector.main` and `python -m coroner.demo_app.main`; the test's `docker top <id> -eo pid,args` listed exactly one process in each container (the in-container `/proc` scan found only PID 1). The manual `top-collector.txt` and `top-demo.txt` also list one short-lived `python -c ...` row each (host PIDs 6326 and 6344). Those are the healthcheck commands that Docker ran with `exec` at that moment, not workers; PID 1 is host PID 5708 (collector) and 5795 (demo) |
| 15 | `CORONER_DEMO_MEM_LIMIT=bogus` is rejected; Collector (same container id, same `StartedAt`, `RestartCount` 0) and volume are untouched; evidence files did not shrink; a marker file written before is intact | PASS | test_15; `docker compose config` exits 1 with `services[demo_app].mem_limit invalid size: 'bogus'`; `up -d --no-deps --no-build demo_app` also exits non-zero |
| 16 | Image source and dependency boundaries: collector image holds `common`, `evidence`, `collector`; demo image holds `probe`, `demo_app`; no tests, docs, spike, runner, compose file; no pytest, hypothesis, docker or typer packages | PASS | test_16 (2 cases) via `docker run --rm --entrypoint python <image>` |

## Observed runtime values (manual collection, running stack)
| Item | Collector `coroner-collector-1` | Demo_App `coroner-demo_app-1` |
|---|---|---|
| `Config.User` | `10001:10001` | `10001:10001` |
| `HostConfig.Memory` / `MemorySwap` | 0 / 0 (no limit, by design) | 134217728 / 134217728 |
| `Privileged` | false | false |
| `ReadonlyRootfs` | false (not configured, see below) | true |
| `CapDrop` / `SecurityOpt` | `["ALL"]` / `["no-new-privileges:true"]` | `["ALL"]` / `["no-new-privileges:true"]` |
| `Tmpfs` | none | `{"/tmp":"size=8m"}` |
| `RestartPolicy` | `on-failure`, max 5 | `no` |
| `PortBindings` | `8081/tcp` to `127.0.0.1:8081` | `8080/tcp` to `127.0.0.1:8080` |
| `Mounts` | volume `coroner_coroner_evidence` at `/data/evidence`, rw | none |
| Healthcheck | python one-liner, interval 2s, timeout 3s, start 5s, retries 25; status `healthy`, last 5 probes exit 0 | python one-liner, interval 2s, timeout 3s, retries 10; status `healthy`, last 5 probes exit 0 |
| PID 1 | `python -m coroner.collector.main` | `python -m coroner.demo_app.main` |

`docker compose ls` showed `coroner  running(2)  C:\Users\prabh\Projects\coroner\compose.yaml`. `docker network inspect coroner_default` showed a plain bridge (`internal=false`) with exactly the two project containers (172.18.0.2 collector, 172.18.0.3 demo). Only 8080 and 8081 are published, both on 127.0.0.1; the Collector is reached by the demo over the bridge at `collector:8081`.

## Test-file review notes
The module was reviewed before the first live run. Assumptions that were untested and are now confirmed by real output: `docker top <id> -eo pid,args` works on Docker Desktop (returns a `PID COMMAND` table); `docker compose config` rejects `CORONER_DEMO_MEM_LIMIT=bogus` itself (exit 1), so the `up --no-deps` fallback was not needed for rejection. One fix was made to the test: test 15 now always asserts that both `config` and `up -d --no-deps --no-build demo_app` fail and name `bogus`, instead of running `up` only when `config` succeeded. This stays safe because Compose fails at parse time, before touching any container.

## Which tests prove something
All 33 tests in `test_compose_isolation.py` (18), `test_volume_persistence.py` (8) and `test_collector_readonly_rootfs.py` (7) call Docker and assert on real containers, volumes and HTTP responses. The only non-substantive test in `tests/docker_integration` is `test_placeholder.py::test_placeholder_runs_only_when_opted_in`, from task 1.3. Its whole body is:

```python
def test_placeholder_runs_only_when_opted_in() -> None:
    assert True
```

It only shows that the opt-in mechanism collects and runs Docker-marked tests. It is NOT runtime proof of anything and is meant to be replaced in M0.7. Counts such as "27 passed" therefore mean 26 substantive tests plus 1 placeholder (18 + 8 + 1), and "34 passed" means 33 substantive plus 1 placeholder.

`test_14` could in principle fail if its `docker top` call lands on a healthcheck `exec` (see the table). It did not in any run here, and was not changed.

## Collector read-only root filesystem (task 5.8)
Compose does not make the Collector root read-only, and nothing here changes that. The behavior was checked separately with one uniquely named non-Compose container (`--read-only`, no tmpfs, UID/GID 10001, all capabilities dropped, no published port). All 7 checks passed. Details are in [volume-ownership.md](volume-ownership.md).

## Limits
- The Collector root filesystem in `compose.yaml` is not read-only; compose.yaml sets `read_only` only for the Demo_App. The read-only Collector was verified only in a separate container (see above).
- Docker Desktop on Windows was used. Verification on a Linux host was not performed.
- No crash or OOM experiment has been run, and the Runner (M0.6) does not exist yet. Whether the 128 MiB limit produces an OOM kill is for the later experiments.
- Every Demo_App start without `CORONER_RUN_ID` writes a new `manual-<8 hex>` evidence file in the volume (4 such files exist after the 5.8 runs; see the provenance table in volume-ownership.md).
- The checks prove the configuration Docker applies, not behavior under load.

## Cleanup
After collection, `docker compose -p coroner -f compose.yaml down` (no `-v`) removed both containers and `coroner_default`. Volume `coroner_coroner_evidence`, both images and the preserved `coroner-spike_coroner_spike_evidence` volume remain (`runs/m05-docker/final-state.txt`).

## Related
[volume-ownership.md](volume-ownership.md), [dependency-verification.md](dependency-verification.md), [m0.5-docker.md](m0.5-docker.md).
