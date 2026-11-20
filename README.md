# Coroner

Crash forensics workstation for Python containers, built with Kiro for Kiroween 2026.

> **Status.** Verified on Windows with Docker Desktop (Linux containers, WSL2), on a single host. Native Linux hosts are not
> verified. Experiment A, Experiment B and Experiment C are **verified** from real runs (see section 13). Experiment C was
> a real Docker OOM kill (run `c-20261120T174958Z-94f4225c`). Experiment B and Experiment C each have one verified real
> execution and Experiment A has four. There is no repeat-consistency proof. Every result below is taken from
> `docs/m0-results/`.

## 1. What Coroner M0 is

M0 asks one question: when a container dies, does the telemetry sent before the death survive, and can we say
honestly what it does and does not show? It is a crash-evidence telemetry pipeline with four parts:

| Part | Package | Role |
|---|---|---|
| Probe | `coroner/probe` | Runs inside the Demo App. Samples process data (including RSS) every `CORONER_SAMPLE_INTERVAL_MS` (default 500 ms) and posts each Sample to the Collector. A Sample that cannot be delivered is counted as dropped, not retried. |
| Collector | `coroner/collector` | FastAPI service. Stores acknowledged Samples as JSON Lines evidence files on a named volume, detects gaps, serves `GET /healthz` and `GET /v1/runs/<run_id>/evidence`. |
| Demo App | `coroner/demo_app` | Small FastAPI app with the Probe. Destructive endpoints (`POST /leak`, `POST /crash`, `GET /error`) exist only when `CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS` is exactly `1`. For local testing only; must not be exposed beyond localhost. |
| Runner | `coroner/runner` | Host-side CLI. Starts the Demo App with Docker Compose, causes one termination, reads `docker inspect`, fetches evidence, evaluates mandatory checks and writes a run manifest. |

Shared code lives in `coroner/common` and `coroner/evidence` (schema, writer, reader, gap accounting).

M0 does not claim to explain why a process died. It reports what the Runner observed from Docker and what the
Samples show, plus the limitations in section 14.

## 2. Architecture overview

```text
 Windows host (PowerShell)                         Docker Desktop (Linux containers, WSL2)
 +-----------------------------+      docker compose / docker inspect / docker kill
 | python -m coroner.runner    |------------------------------------------------------+
 |  run a|b|c, summary, clock   |                                                      |
 |  writes runs/<run_id>/      |                                                      v
 |   manifest.json             |   HTTP 127.0.0.1:8080        +-----------------------------------+
 |   evidence.jsonl            |----------------------------->| demo_app  (coroner-demo-app:m0)   |
 |   docker_inspect.json       |   /health /leak /crash /error|  Demo App + Probe, 128 MiB limit, |
 |                             |                              |  read-only rootfs, user 10001     |
 |                             |                              +----------------+------------------+
 |                             |                                               | POST /v1/samples
 |                             |   HTTP 127.0.0.1:8081                         | (network: collector:8081)
 |                             |<---------------------------------+            v
 |                             |   /healthz, /v1/runs/<id>/evidence  +-----------------------------------+
 +-----------------------------+                                   | collector (coroner-collector:m0)   |
                                                                    |  user 10001, caps dropped          |
                                                                    +----------------+------------------+
                                                                                     | append (+ fsync)
                                                                                     v
                                                                    named volume <project>_coroner_evidence
                                                                    mounted at /data/evidence
```

Sources: `compose.yaml`, `docs/m0-results/volume-ownership.md`, `coroner/runner/cli.py`.

## 3. Installation

### 3.1 Prerequisites (versions that were actually verified)

| Tool | Verified version | Source |
|---|---|---|
| Windows | Windows 11, PowerShell | `docs/m0-results/docker-prereqs.md` |
| Python (host) | Minimum 3.12; verified 3.12.10 (`requires-python = ">=3.12"` in `pyproject.toml`; 3.10 and 3.11 are not tested and not claimed) | `docs/m0-results/dependency-verification.md`, `docs/m0-results/environment.md` |
| Git | 2.55.0.windows.5 (needed to clone the repository) | `docs/m0-results/environment.md` |
| Docker Desktop | 4.99.0 (245822), Linux containers, WSL2 backend | `docs/m0-results/docker-prereqs.md` |
| Docker Engine / client | 29.9.3 (API 1.56), context `desktop-linux` | same |
| Docker Compose (v2 plugin, `docker compose`) | v5.7.0 | same |
| Buildx | v0.38.1 | same |
| Base image | `python:3.12.8-slim-bookworm`, digest `sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0` (linux/amd64) | same, `docker/*.Dockerfile` |

The digest is recorded to detect tag drift. The Dockerfiles pin the tag, not the digest. Older Docker or Compose
versions are not verified. Native Linux hosts are not verified.

### 3.2 Python environment

From the repository root, in PowerShell:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# if script execution is blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
python -m pip install -r requirements/dev.txt
```

### 3.3 Pinned requirements

Every line uses `==`; there are no ranges. Files are in `requirements/`.

| File | Contents |
|---|---|
| `requirements/collector.txt` and `requirements/demo_app.txt` (identical pin sets) | annotated-types 0.8.0, anyio 4.16.0, click 8.5.1, fastapi 0.115.6, h11 0.16.0, idna 3.20, pydantic 2.14.3, pydantic_core 2.50.4, starlette 0.41.3, typing-inspection 0.4.5, typing_extensions 4.16.0, uvicorn 0.34.0 |
| `requirements/runner.txt` | No third-party packages. The Runner uses only the Python standard library. |
| `requirements/dev.txt` | The 12 image pins plus attrs 26.1.0, certifi 2026.10.23, colorama 0.4.6, httpcore 1.0.9, httpx 0.28.1, hypothesis 6.122.3, iniconfig 2.3.1, packaging 26.3, pluggy 1.7.0, pytest 8.3.4, PyYAML 6.0.2, sortedcontainers 2.4.0 |

`docs/m0-results/clock-offset.md` records that `pip freeze` in each rebuilt image lists exactly the 12 collector and
demo pins and that `pip check` reports no broken requirements.

### 3.4 Windows and Docker Desktop setup

1. Install Docker Desktop and select **Linux containers** with the **WSL2** backend. The Collector, Demo App and Probe run
   only in Linux containers.
2. Verify the tooling:

```powershell
docker version
docker compose version
docker info
```

   The values recorded on the verified host: `OSType linux`, kernel `6.18.58.1-microsoft-standard-WSL2`, cgroup version 2,
   Docker VM 8 CPUs and 7.687 GiB memory (`docs/m0-results/docker-prereqs.md`).
3. Run all commands from the repository root. The Runner does not pass `-f`, so Docker Compose reads `compose.yaml` from
   the working directory (`docs/m0-results/m0.7-runner-project-isolation.md`).
4. Ports 8081 and 8080 on `127.0.0.1` must be free. The Runner uses these ports, or `CORONER_COLLECTOR_PORT` and
   `CORONER_DEMO_PORT` when set.

#### Confirming the memory limit is enforced

After an experiment the Demo App container is stopped, not removed. Check what Docker actually applied:

```powershell
$id = docker compose -p coroner-m07 -f compose.yaml ps -a -q demo_app
docker inspect $id --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.State.OOMKilled}} {{.State.ExitCode}}'
```

(The design's command form, `.kiro/specs/coroner-m0/design.md`, "Windows Setup and Verification".) Do this **before**
`docker compose -p coroner-m07 -f compose.yaml down`, because `down` removes the stopped container and its inspect data.
Use the Compose project that the run used (`coroner-m07` in the examples here). Read the result as follows:

- `HostConfig.Memory` must equal the limit in bytes: `134217728` for `128m`. `0` means no limit, and Experiment C would not
  be a genuine OOM kill.
- `HostConfig.MemorySwap` equal to `Memory` means no extra swap.
- After Experiment C the expected tail is `true 137`.

A 64 MiB cgroup limit was confirmed on this Docker Desktop and WSL2 setup (`memory.max` read `67108864` inside a test
container, `docs/m0-results/docker-prereqs.md`). That shows the limit is applied. It is not an OOM kill.

#### WSL2 caveats (from the design)

- The memory limit is a cgroup limit inside the WSL2 VM. The VM ceiling (`%UserProfile%\.wslconfig`, `memory=`) must be above
  the container limit.
- Docker Desktop's `OOMKilled` reporting can differ across versions. M0 never infers an OOM kill from RSS or timing. It
  relies on `State.OOMKilled` and `State.ExitCode == 137` from `docker inspect`.
- The VM clock can differ from the Windows host clock (section 11).

## 4. Docker Compose usage

`compose.yaml` (project name `coroner`) defines two services. Images are tagged, not pinned by digest.

| Item | `collector` | `demo_app` |
|---|---|---|
| Image | `coroner-collector:m0` | `coroner-demo-app:m0` |
| Host port | `127.0.0.1:8081` (override `CORONER_COLLECTOR_PORT`) | `127.0.0.1:8080` (override `CORONER_DEMO_PORT`) |
| User | `10001:10001` | `10001:10001` |
| Capabilities | `cap_drop: [ALL]`, `no-new-privileges` | same, `privileged: false` |
| Root filesystem | writable (not `read_only`) | `read_only: true`, `tmpfs /tmp:size=8m` |
| Memory | no limit set | `mem_limit` and `memswap_limit` both `${CORONER_DEMO_MEM_LIMIT:-128m}` |
| Restart policy | `on-failure:5` | `no` |
| Storage | named volume `coroner_evidence` at `/data/evidence` | none |
| Health | healthy only when `/healthz` returns 200 **and** `storage_ok` is JSON `true` | `GET /health` returns 200 |
| Environment | `CORONER_DURABILITY` (default `crash`) | `CORONER_RUN_ID`, `CORONER_COLLECTOR_URL=http://collector:8081`, `CORONER_SAMPLE_INTERVAL_MS` (500), `CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS` (default `0`) |

The Demo App depends on the Collector being healthy. If `CORONER_RUN_ID` is empty, the Probe generates `manual-<8 hex>`,
so every manual `docker compose up` of `demo_app` writes a new run file.

Start the Collector from the existing images in the isolated project `coroner-m07`. Always pass `-p` and `-f`. A command
without `-p` targets the project `coroner` and mounts the protected volume `coroner_coroner_evidence`:

```powershell
docker compose -p coroner-m07 -f compose.yaml up -d --no-deps --no-build collector
docker compose -p coroner-m07 -f compose.yaml ps
Invoke-RestMethod http://127.0.0.1:8081/healthz
```

Stop the stack **without** `-v`:

```powershell
docker compose -p coroner-m07 -f compose.yaml down
```

**Building images (one-time step on a fresh clone only).** The images `coroner-collector:m0` and `coroner-demo-app:m0`
must exist before the commands above. On a fresh clone, `docker compose build` creates them. Do not rebuild on a machine
that holds the recorded results: a rebuild retags `coroner-collector:m0` and `coroner-demo-app:m0`, the images the recorded
results refer to. A plain build for `coroner-m07` is not a recorded result. The original image build is recorded in
`docs/m0-results/m0.5-docker.md`. The clean-build verification used separate `:m07-verify` tags and is recorded in
`docs/m0-results/clock-offset.md`.

The Runner starts only the Demo App (`up -d --no-deps --force-recreate demo_app`) and expects the Collector to be ready.
Start the Collector yourself first (command above), in the same Compose project the Runner will use. The Runner sets
`CORONER_RUN_ID`, `CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS=1` and `CORONER_DEMO_MEM_LIMIT` for the Demo App itself.

> The destructive endpoints (the Destructive_Endpoints: `POST /crash`, `POST /leak`, `GET /error`) are for local testing
> only. They are disabled (404) unless `CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS=1` is set, and they must not be exposed beyond
> localhost. Do not publish the Demo App port beyond `127.0.0.1`.

## 5. Evidence persistence

- Evidence lives in the named volume `<project>_coroner_evidence`, mounted at `/data/evidence`. One file per run:
  `<run_id>.jsonl`. Files are append-only.
- **Ownership is image-baked.** `docker/collector.Dockerfile` creates `/data/evidence` with owner `10001:10001` and mode
  `0750`. A fresh named volume inherits that on first mount, so the non-root Collector can write with all capabilities
  dropped. No privileged init step is used. Verified: `10001:10001 750`, and evidence survived `docker compose restart
  collector` byte for byte (`docs/m0-results/volume-ownership.md`).
- The Collector's `storage_ok` comes from a real probe in `.coroner_probe/probe.tmp` inside the evidence directory
  (write, flush, fsync, directory fsync, read-back, compare). It is cached for up to 1 s.
- Evidence files are never deleted by the Runner.

### Rules

- **Never run `docker compose down -v`**, `docker volume rm` or `docker system prune` against an evidence volume.
- **Protected volumes** (recorded as existing and untouched in `docs/m0-results/`): `coroner_coroner_evidence` (M0.5) and
  `coroner-spike_coroner_spike_evidence` (spike). Do not mount, modify or remove them.
- Inspect evidence read-only. Use `python -m coroner.runner summary <run id>`, the host copy `runs/<run_id>/evidence.jsonl`,
  or `GET http://127.0.0.1:8081/v1/runs/<run_id>/evidence` while the Collector runs. Where a volume must be read directly,
  the recorded procedure used a snapshot container with the volume mounted `:ro` and no network
  (`docs/m0-results/volume-ownership.md`).

#### Inspecting evidence in PowerShell

The examples use the recorded Experiment A run id `a-20261120T134321Z-a5e5a30e`. Replace it with your own run id.

```powershell
Get-Content runs\a-20261120T134321Z-a5e5a30e\evidence.jsonl -TotalCount 3
Select-String -Path runs\a-20261120T134321Z-a5e5a30e\evidence.jsonl -Pattern '"record_type":"termination"'
(Invoke-WebRequest http://127.0.0.1:8081/v1/runs/a-20261120T134321Z-a5e5a30e/evidence).Content
python -m coroner.runner summary a-20261120T134321Z-a5e5a30e
```

- `jq` is not assumed on Windows. Use the PowerShell commands above or Python.
- The `Select-String` pattern assumes compact JSON without spaces. If it matches nothing, search for `termination` instead.
- Do not use `>` redirection to copy evidence or `docker inspect` output in Windows PowerShell 5.1. It writes UTF-16. The
  independent capture of the Experiment B inspect output made this mistake once (`docs/m0-results/experiment-results.md`).
  For a scratch copy **outside** the repository evidence, pipe through `Set-Content -Encoding utf8`. When you read a file
  that may be UTF-16, use `Get-Content -Encoding Unicode`.
- Volumes accumulate run files. Each run adds a file. M0 has no cleanup command.

## 6. Compose project isolation

The Runner accepts the Compose project name from `CORONER_COMPOSE_PROJECT` (`docs/m0-results/m0.7-runner-project-isolation.md`).

| Item | Behavior |
|---|---|
| Unset | Project `coroner`. Volume `coroner_coroner_evidence`. |
| Set to a valid name | Every `docker compose` call from the Runner uses `-p <name>`. The volume is `<name>_coroner_evidence`. |
| Set but invalid or empty | Exit code 2, message `error: invalid CORONER_COMPOSE_PROJECT ...`. No Docker call and no run directory. No fallback to `coroner`. |
| Valid name | `^[a-z0-9][a-z0-9_-]*$`, 1 to 63 characters (the 63 cap is the Runner's own choice). |
| Read by | `run` only. `summary` and `clock-check` never read it. |
| Pre-validation order (first error wins) | `--mem-limit`, `--leak-chunk-bytes`, `CORONER_COLLECTOR_PORT`, `CORONER_DEMO_PORT`, `CORONER_COMPOSE_PROJECT` |

The M0.7 Experiment A and the ordinary-500 test used project `coroner-m07`, so their evidence is in
`coroner-m07_coroner_evidence` and does not touch the M0.5 volume. Experiment B also used `coroner-m07` (run `b-20261120T173541Z-3fa52342`, evidence file in the same volume). Experiment C also
used `coroner-m07` (run `c-20261120T174958Z-94f4225c`, evidence file in the same volume). The form for Experiment A:

```powershell
$env:CORONER_COMPOSE_PROJECT = 'coroner-m07'
docker compose -p coroner-m07 -f compose.yaml up -d --no-deps --no-build collector
python -m coroner.runner run a
Remove-Item Env:CORONER_COMPOSE_PROJECT
```

Two projects cannot run at once because the host ports are fixed. The Runner's `compose up` has no `--no-build`, so a
missing image may be built.

## 7. Runner CLI

```text
python -m coroner.runner run {a|b|c} [--runs-dir PATH] [--mem-limit SIZE] [--leak-chunk-bytes N]
python -m coroner.runner summary RUN_ID [--runs-dir PATH]
python -m coroner.runner clock-check [--count N] [--collector-url URL]
```

| Option | Default | Notes |
|---|---|---|
| `run` experiment | none | Lower case `a`, `b` or `c` only. |
| `--runs-dir PATH` | `runs` | Relative to the current directory. Used by `run` and `summary`. |
| `--mem-limit SIZE` | `128m` | `run` only. Digits with optional `b`, `k`, `m`, `g` (powers of 1024). Zero is rejected (exit 2). |
| `--leak-chunk-bytes N` | `4194304` | `run` only. Valid 1 to 104857600. |
| `RUN_ID` | none | `summary` only. Read-only; touches no Docker, network or files other than reading. |
| `--count N` | `20` | `clock-check` only. Valid 1 to 1000. |
| `--collector-url URL` | `http://127.0.0.1:<CORONER_COLLECTOR_PORT or 8081>` | `clock-check` only. |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | `run`: the experiment passed. `summary`: a summary was produced (whatever the stored outcome). `clock-check`: at least one usable measurement. |
| 1 | `run`: the experiment failed or was aborted after a run id existed (a manifest was written). Also Ctrl+C. `summary` never returns 1. |
| 2 | Usage or environment error: bad option or environment value, Docker unavailable (no run id, no manifest), `docker compose` itself failed, `summary` run or evidence not found, `clock-check` with no usable measurement. |

Environment variables read by `run`: `CORONER_COLLECTOR_PORT` (default 8081), `CORONER_DEMO_PORT` (default 8080),
`CORONER_COMPOSE_PROJECT` (default `coroner`). `--repeat K` and `summary --fetch` are deferred (task 6.20, optional);
argparse rejects them with exit 2.

Each `run` creates `runs/<run_id>/` holding `manifest.json`, `evidence.jsonl` and the raw `docker_inspect.json`. Run ids look
like `a-20261120T134321Z-a5e5a30e`. The `runs` directory is gitignored.

## 8. Experiments

Expected container state per experiment (`coroner/runner/evaluate.py`, `EXPECTED_STATE`):

| Experiment | Termination | Expected exit code | Expected `OOMKilled` |
|---|---|---|---|
| A | controlled fatal process exit via `os._exit(1)` | 1 | false |
| B | SIGKILL sent by the Runner | 137 | false |
| C | genuine Docker out-of-memory kill | 137 | true |

```powershell
$env:CORONER_COMPOSE_PROJECT = 'coroner-m07'
python -m coroner.runner run a
python -m coroner.runner summary a-20261120T134321Z-a5e5a30e
Remove-Item Env:CORONER_COMPOSE_PROJECT
```

(Use the run id that `run a` printed instead of the recorded example id.) Experiments B and C use the same form
(`run b`, `run c`); read the safety gates first.

**Safety gates.** Experiment B sends a real SIGKILL and Experiment C causes a real OOM kill of the Demo App container. Run
them only where you accept that. A bare `python -m coroner.runner run a|b|c` without `CORONER_COMPOSE_PROJECT` targets the
default project `coroner` and its protected volume `coroner_coroner_evidence`. Always set the variable first, as in the
example above.

Each run starts from the common preamble: Docker available, run directory created, Collector ready (`/healthz` 200 with
`storage_ok` true, up to 60 s), clock offset measured, Demo App started. A run fails if `/healthz` returns 200 without
`storage_ok is True`.

### Experiment A: controlled fatal process exit

The Runner leaks memory for at least 5 seconds, then calls `POST /crash`. The Demo App ends the process with
`os._exit(1)`. This is a **controlled fatal process exit**. It is not an application error and not an HTTP 500 response. No
Python shutdown or cleanup handlers run and buffered Python output is not flushed. The separate endpoint `GET /error`
returns HTTP 500 while the process keeps running: an ordinary HTTP 500 does not terminate the Demo App (verified, section 13).

Checked: exit code 1, `OOMKilled` false, at least one Sample before the crash request, the Collector healthy within 5 s with
`storage_ok` true. The Runner requires `storage_ok` true both before the experiment and again after the termination
(`collector_survival`; Requirement 14.11). The manifest termination description is exactly `controlled fatal process exit via os._exit(1)`.

### Experiment B: SIGKILL

The Runner sends `docker kill --signal=KILL` through its Docker port. The container id must be the full 64-character
lowercase hex id. The verdict needs all seven mandatory checks: `exit_code_137`, `oom_killed_false`, `stopped_within_10s`,
`collector_survival`, `at_least_one_sample_captured_before_kill`,
`no_sample_captured_after_kill_beyond_tolerance`, `no_shutdown_handler_records`.

**Capture time versus receive time.** Each Sample has a Capture_Time (`ts`, set by the Probe) and a Receive_Time
(`recv_time`, set by the Collector). They are stored separately. A Sample captured before the kill may be received after it;
that is valid and is reported (`pre_kill_late_received_count`). Classification uses Capture_Time, corrected by the measured
clock offset, with a fixed **250 ms tolerance** (`CLOCK_TOLERANCE_MS`). A Sample captured more than 250 ms after the kill
fails the run. A Sample captured after the container's `FinishedAt` is reported as an anomaly. Receive_Time alone never
changes the category.

### Experiment C: genuine Docker OOM kill

The Runner leaks 4 MiB chunks (or `--leak-chunk-bytes`) at intervals of at most 1 s until the container stops or 120 s pass.
The verdict comes from exactly six mandatory checks: `oom_killed_true`, `exit_code_137`, `collector_survival`,
`preserved_precrash_evidence`, `measurable_memory_growth`, `docker_inspect_recorded`.

The Diagnostic_Metrics (`rss_monotonic_tail`, `peak_rss_ratio_of_limit`, Sample count) are recorded and printed, and are
**informational**: they never decide pass or fail. The Sampling_Limitations statements are always recorded and printed
(section 14, item 11). Use only the default 128 MiB limit to limit host impact. The OOM kill is taken from Docker
inspection (`OOMKilled` true, exit code 137), never inferred from RSS or timing. `storage_ok` must also be true after the
termination (`collector_survival`), so the Collector survives all three failure modes: A, B and C.

## 9. Durability semantics

`CORONER_DURABILITY` is read by the Collector. Unset or empty means `crash`. Valid values are `process` and `crash`; anything
else stops the Collector with a configuration error (exit 2). `compose.yaml` passes `${CORONER_DURABILITY:-crash}`.

There are two Durability_Levels, `Process_Durable` and `Crash_Durable`. The M0 default is `Crash_Durable`.

| Level | What the Collector does before acknowledging a Sample |
|---|---|
| `Process_Durable` (`CORONER_DURABILITY=process`) | Write and flush to the operating system. Survives a Collector or Demo App crash. Not necessarily an OS crash. |
| `Crash_Durable` (`CORONER_DURABILITY=crash`, the M0 default) | Write, flush, `fsync` the file, and `fsync` the directory when the file was created. |

The `fsync` caveat: `Crash_Durable` means `fsync` was performed. The `fsync` guarantees depend on the Docker volume and the
underlying host filesystem and storage. Whether the data survives a host or OS crash depends on the Docker
volume driver, the host filesystem, the Docker Desktop VM layer and the storage hardware. M0 never tests a host crash and
makes no claim beyond "fsync was performed". On Windows, the writer's directory `fsync` is a documented no-op, but the
Collector always runs in a Linux container. No atomicity is claimed: after a failed write a partial line can remain, and
the writer refuses to append until the file is repaired.

The level is recorded in `GET /healthz`, in each `run_metadata` record (`durability`, `fsync_before_ack`) and in the
manifest. A mismatch between `/healthz` and the evidence fails the `durability_matches_run_metadata` check.

### `storage_ok`

`GET /healthz` returns `status`, `durability`, `server_time`, `uptime_s` and `storage_ok`. **HTTP 200 alone is not proof of
healthy storage.** The Collector stays at 200 and reports `storage_ok: false` when the volume is unusable. Every consumer
(the Compose healthcheck, the Runner's readiness check and the survival checks) requires the JSON value `storage_ok is true`;
a missing field counts as unhealthy. The Runner requires `storage_ok` true before and after each experiment. `storage_ok` is a point-in-time probe (cached up to 1 s). It does not prove that later
writes succeed or that data survives a host crash.

## 10. Clock tolerance

The Experiment B tolerance is a fixed named constant, `CLOCK_TOLERANCE_MS = 250`, never above 250 ms and never changed
automatically. The Runner measures the Collector's clock offset against the host from `GET /healthz` `server_time` and
records `clock_offset_ms` per run.

Sign convention: a positive offset means the Collector clock is ahead of the host (`host time = collector time - offset`).

## 11. Measured clock offset (Task 7.1)

Measured on 2026-11-20 against the Collector in project `coroner-m07` (`docs/m0-results/clock-offset.md`):

| Measurement | Result |
|---|---|
| `clock-check --count 20` | 20 of 20 usable. Median +10 ms, range +0 to +28 ms, round trip min 7.2 / median 25.6 / max 59.6 ms |
| `clock-check --count 50` | 50 of 50 usable. Median +8 ms, range -1 to +37 ms, round trip min 4.2 / median 22.3 / max 79.9 ms |
| Best single measurement | +1 ms +/- 3.6 ms (20 runs); +0 ms +/- 2.1 ms (50 runs) |
| Conservative bound (50 runs) | abs(median) + max uncertainty = 8 + 39.95, about 48 ms |
| Pre-run check before Experiment A (Task 7.3) | 20 of 20 usable, best +1 ms +/- 3.0 ms, median +7 ms, range +1 to +37 ms |

The decision recorded was PASS with the 250 ms tolerance unchanged. Caveats, as recorded:

- One Docker Desktop VM session and about 30 seconds of data. Drift over hours is not measured.
- Each measurement is only as accurate as its own uncertainty (2 to 40 ms). Long round trips show larger positive offsets,
  which fits asymmetric delay, so the true offset is probably nearer 0 to +1 ms, but the median is kept as the conservative
  figure.
- A separate `docker exec date` comparison gave about +62 ms with an uncertainty of 73 to 156 ms. That method is too coarse to
  resolve small offsets and is not used to infer drift.
- No Linux-host check was done. The VM and host clock relationship may differ after sleep, resume or long uptime.

Check the current offset yourself:

```powershell
python -m coroner.runner clock-check --count 20
```

The command prints `Offset`, `Uncertainty`, `Offset median`, `Offset range`, `Round trip` and `Tolerance`, and a `Warning` line
when the offset plus uncertainty exceeds the 250 ms tolerance.

## 12. Tests

The default suite is Docker-free and network-free:

```powershell
python -m pytest -q
```

Latest recorded full default run (a historical verified run: Task 7.10, `docs/m0-results/m0.7-acceptance.md`, 2026-11-20,
at commit `c3c9428`): **4402 passed, 0 failed, 5 skipped, 53 deselected, 1 known warning**, exit code 0, about 424 s
(423.70 s by pytest). The warning is the starlette `testclient` anyio `BlockingPortal` DeprecationWarning. The 53 deselected
tests are the Docker integration tests, which the default run skips. This is not a permanently current count: counts change
as tests are added, so rerun `python -m pytest -q` to confirm.

Earlier runs, kept for history only: before `test_experiment_b.py` and `test_experiment_c.py` existed, a full default run
gave 4402 passed, 5 skipped, 40 deselected; before `test_experiment_a.py` existed it gave 36 deselected
(`docs/m0-results/m0.7-runner-project-isolation.md`). The history adds up: 36 + 4 (`test_experiment_a.py`) = 40, then
+ 6 (`test_experiment_b.py`) + 7 (`test_experiment_c.py`) = 53.

The default `python -m pytest` command runs only Docker-free, network-free unit tests.

Docker integration tests are opt-in (`--run-docker` or `CORONER_RUN_DOCKER=1`). Their Docker prerequisites are the ones in
section 3.4 (Docker Desktop running with Linux containers) and the two built images named below. Only the exact value `1` of the environment variable (or the `--run-docker` flag) opts in.

**WARNING: never run `--run-docker` (or `CORONER_RUN_DOCKER=1`) without a file path.** An unscoped run would execute every
Docker test, including the M0.5 tests and the destructive B and C tests. These files belong to the M0.5 project `coroner` and
the protected volume `coroner_coroner_evidence`, and must not be rerun casually (a rerun writes to that volume):

- `tests/docker_integration/test_volume_persistence.py`
- `tests/docker_integration/test_compose_isolation.py`
- `tests/docker_integration/test_collector_readonly_rootfs.py`

Also never run `--run-docker` over the whole `tests/docker_integration` directory.

The per-file tests for `test_ordinary_500.py`, `test_experiment_a.py`, `test_experiment_b.py` and `test_experiment_c.py`
use the project `coroner-m07` internally. They hard-code it and pass it to every `docker compose` call, and
`CORONER_COMPOSE_PROJECT` in your shell has no effect on them (the tests set it only for the Runner subprocess). They tear the
stack down with `docker compose -p coroner-m07 -f compose.yaml down` (no `-v`), unless `CORONER_KEEP_STACK=1` is set. They need
the images `coroner-collector:m0` and `coroner-demo-app:m0` to exist and do not build.

The fixtures of `test_experiment_b.py` and `test_experiment_c.py` fail early, without changing anything, if any `coroner-m07`
container already exists. Run this first:

```powershell
docker compose -p coroner-m07 -f compose.yaml down
```

Run one named file per command. Experiment B sends a real SIGKILL and Experiment C causes a real OOM kill of the Demo App
container, so run those two only where you accept that:

```powershell
python -m pytest tests/docker_integration/test_experiment_b.py --run-docker -q
```

The environment variable form works for one explicitly named file as well:

```powershell
$env:CORONER_RUN_DOCKER='1'; python -m pytest tests/docker_integration/test_experiment_c.py -q; Remove-Item Env:CORONER_RUN_DOCKER
```

These tests start real containers and, for the experiment tests, destroy the Demo App container as designed. Read each test
file before running it. If Docker is unavailable, opted-in integration tests are skipped or fail with a message that Docker is
unavailable, and the unit tests still run.

## 13. Verified results

Every row below comes from `docs/m0-results/experiment-results.md` (host UTC 2026-11-20, branch `feat/coroner-m0`).

| Item | Result | Evidence |
|---|---|---|
| Experiment A (Task 7.3), real Runner run | **PASS**. Exit 0, `outcome: passed`. Final run `a-20261120T134321Z-a5e5a30e`, container `e5dcb355e4d6d47ea1a2a3068db3c3f0a28ed8e39f3e1642d6c3aee96ea3363a`: `docker inspect` `exited`, exit code 1, `OOMKilled` false, `Memory` 134217728, `RestartCount` 0. 15 Samples (seq 0 to 14, consecutive), 17 evidence records, one `termination` record. First run `a-20261120T133934Z-6d2f2350`: 14 Samples, `FinishedAt` 83 ms after the crash request. For all four runs the Runner's manifest matched an independent `docker inspect` (captured at run time and compared byte for byte with the Runner's saved copy). Integration test `test_experiment_a.py`, historical result recorded in Task 7.3: `4 passed in 15.94s` (after three executions with defects in the test itself, not the code under test). | `experiment-results.md` |
| HTTP 500 does not terminate the Demo App (Task 7.2) | **PASS**. `GET /error` returned 500, `GET /health` returned 200 0.016 s later, host `State.Pid` unchanged (3164), `RestartCount` 0, `seq` 0..5 consecutive. Run `m07-500-76640824`. Historical result recorded in Task 7.2: `2 passed in 13.17s`. | `experiment-results.md` |
| Experiment B (Task 7.4), real SIGKILL sent by the Runner | **VERIFIED**. Run `b-20261120T173541Z-3fa52342`, container `d8f19b8abc9191dd5aaf34f272e08d079cb68bc61c5e677d0c690050b07dc917`, project `coroner-m07`. Runner exit 0, `outcome: passed`, all 7 mandatory checks passed. `docker inspect`: `Status` `exited`, `ExitCode` 137, `OOMKilled` false, `RestartCount` 0. 15 Samples (seq 0 to 14): pre-kill 15, late-received 0, within-tolerance 0, beyond-tolerance 0, after-container-stop 0. Clock offset used +6 ms (uncertainty 8.469 ms), tolerance 250 ms unchanged. `FinishedAt` was 163.093 ms after `sigkill_sent_time`; the `docker kill` call returned 414 ms after it. Collector `storage_ok` true after the kill. Integration test `test_experiment_b.py`, historical result recorded in Task 7.4: `6 passed in 16.51s`. | `experiment-results.md` |
| Experiment C (Task 7.5), real Docker OOM kill | **VERIFIED**. Run `c-20261120T174958Z-94f4225c`, container `42a89ff3f2a6f7907cd12913feae448f541ecefc67703e613ec94ff5c20fed44`, project `coroner-m07`. Runner exit 0, `outcome: passed`. `docker inspect`: `Status` `exited`, `ExitCode` 137, `OOMKilled` true, `HostConfig.Memory` 134217728, `MemorySwap` 134217728. The Runner sent no signal and made no `/crash` call. 46 Samples (seq 0 to 45, consecutive), first RSS 46362624, highest RSS 138522624. The peak Sample RSS is 1.032 times the cgroup limit; RSS inside the container and cgroup accounting are different measures, so the verdict rests on `OOMKilled` true and exit code 137 only. The leak made 23 requests; the 23rd ended in `RemoteDisconnected`, which is the kill itself. The RSS series has one dip (132308992 to 129662976, `seq` 40 to 41). It lies outside the 5-Sample diagnostic tail, so `rss_monotonic_tail` is true. Collector `storage_ok` true after the kill. Single Docker Desktop (WSL2) session on one Windows host. Integration test `test_experiment_c.py`, historical result recorded in Task 7.5: `7 passed in 31.81s`. | `experiment-results.md` |

Experiment B and Experiment C each have one verified real execution (Experiment A has four). This is not a statement about
repeat consistency, other Docker versions or a Linux host, and there is no native Linux-host verification. The Experiment C
diagnostics are informational only (section 8). The `N passed in X s` figures above are historical results recorded in the
named tasks, not a current run. The latest full default run is in section 12.

## 14. Known limitations

1. **The unobserved interval.** The time between the last received Sample and the actual termination is unobserved. A Sample
   captured just before death may never have been sent. M0 reports that the interval exists, not what happened in it.
2. **Cause of termination is unknown unless a termination record exists**, and the record reflects what the Runner observed
   from Docker, not an inference from telemetry.
3. **Silence gaps have an unknown Sample count** and never add to the missing-Sample count.
4. **Sampling resolution.** A leak or spike between two Samples is invisible. RSS is measured every Sample_Interval (default
   500 ms).
5. **fsync guarantees are only as strong as the stack beneath** (section 9).
6. **Ack-lost duplication.** A persisted Sample whose acknowledgement is lost is counted as dropped by the Probe; stored
   evidence is then a superset and the drop is reported as unreconciled.
7. **One process incarnation per run id.** A restart inside a run shows up as duplicates or resets, not as a new incarnation.
8. **Clock skew.** The Runner estimates the offset; the correction is approximate (section 11).
9. **Container id discovery.** The Probe parses `/proc` and may fall back to the 12-character `HOSTNAME`;
   `container_id_source` says which.
10. **Pre-start data is not covered.** Nothing before the Probe starts, and nothing between ticks, is captured.
11. **Experiment C Sampling_Limitations** (always recorded): the Sample_Interval compared with the leak speed bounds how much
    growth can happen unseen between Samples; the final seconds before the OOM kill may be unobserved; the peak RSS may
    understate the memory limit. Diagnostic_Metrics never decide pass or fail.
12. **Experiment B capture versus receive time** (section 8). Classification uses an estimated offset and the 250 ms tolerance.
13. **Unreconciled drops.** A Probe-reported drop whose range cannot be established is reported with an uncertainty flag.
14. **Malformed lines remain in evidence files.** Append-only repair keeps the fragment for audit, so standard text tools show
    it. Only the Evidence_Reader quarantines them.
15. **`storage_ok` is a point-in-time probe** (section 9). It shows the volume was writable at probe time (cached up to 1 s).
    It does not prove that later writes succeed or that data survives a host crash.

(Items 1 to 15 are the design's list, `.kiro/specs/coroner-m0/design.md`, "Limitations and uncertainty reporting".) Further
limits recorded in the results:

- **Windows and Docker Desktop only.** All verification ran on Windows 11 with Docker Desktop (Linux containers, WSL2).
  Native Linux hosts were not verified.
- **Probe DNS deadline.** DNS resolution (`getaddrinfo`) ignores socket timeouts, so one delivery attempt can exceed the
  nominal 200 ms deadline. There is no unconditional wall-clock bound, and a stalled resolver keeps the single sender thread
  busy so new Samples are dropped (counted in `dropped`). A late result is never acknowledged. See
  `docs/m0-results/m0.4-probe-demo.md`.
- **Deferred.** The optional CLI `--repeat K` and `summary --fetch` (task 6.20) are not implemented.
- **Clock accuracy** is limited to the measured uncertainty, from one session of about 30 seconds.
- **Evidence volumes accumulate** run files; M0 does not delete them.
- **Fixed host ports.** The Runner uses ports 8081 and 8080 (or the `CORONER_*_PORT` variables) and `compose.yaml` from the
  repository root.
- The Collector root filesystem is not read-only in `compose.yaml`; only the Demo App is. A read-only Collector was tested
  outside Compose only (`docs/m0-results/volume-ownership.md`).
- In Experiment A, the Demo App's reported `pid` is its PID inside the container (1), so `pid` alone cannot prove "no
  restart"; continuity is shown by host `State.Pid`, `StartedAt`, `RestartCount` and an unbroken `seq`.

## 15. Safety rules

- Never use `docker compose down -v`, `docker volume rm`, `docker volume prune` or `docker system prune` on this project.
- Never mount or modify `coroner_coroner_evidence` or `coroner-spike_coroner_spike_evidence`. Use `CORONER_COMPOSE_PROJECT` to
  keep new evidence in a separate volume.
- Destructive Demo App endpoints stay disabled unless `CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS=1`. They are for local testing only and must not be exposed beyond localhost.
- Do not change `CLOCK_TOLERANCE_MS` (250) without the project owner's approval.
- Publish ports on `127.0.0.1` only (as `compose.yaml` does).
- The `summary` command is read-only.

## 16. Repository layout

```text
coroner/            Python packages: probe, collector, demo_app, runner, evidence, common
docker/             collector.Dockerfile, demo_app.Dockerfile (base python:3.12.8-slim-bookworm)
compose.yaml        Collector and Demo App services, named volume coroner_evidence
requirements/       Exact-pinned requirements: collector, demo_app, runner, dev
tests/unit/         Docker-free, network-free tests (default pytest)
tests/docker_integration/   Opt-in Docker tests (--run-docker)
docs/m0-results/    Recorded results for each M0 task
.kiro/specs/coroner-m0/     requirements.md, design.md, tasks.md
spike/              Earlier OOM spike (see docs/m0-results/oom-spike.md)
runs/               Local run output (gitignored)
```

## License

See `LICENSE`.
