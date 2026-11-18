# Named-volume ownership and persistence (task 5.6)

Date of run: 2026-11-18 (container clock). Repo HEAD `400e592688e47b03fd8baf583e6c1b975d569a47`, branch `feat/coroner-m0`. All values below come from real Docker output saved in `runs/m05-docker/`.

## Result

The image-baked ownership approach works on this Docker host. A fresh named volume mounted at `/data/evidence` is initialised from the image directory, so it is owned `10001:10001` with mode `750`. The Collector runs as UID/GID 10001 with all capabilities dropped and `no-new-privileges`, writes and fsyncs in the volume, and keeps the data across a container restart. No privileged init step was needed.

## Fresh volume proved by absence

Before the run, `docker volume ls`, `docker ps -a` and `docker network ls` showed no `coroner_*` volume, container or network. The only non-default volume was the preserved spike volume. Nothing was removed to get a fresh volume, so Execution Rule 11 (archive and approval) did not apply.

## Ownership mechanism

`docker/collector.Dockerfile` creates `/data/evidence` in the image (`mkdir -p`, `chown 10001:10001`, `chmod 0750`). Docker copies the image directory's ownership and mode into an empty named volume on first mount. `/data` itself stays `root:root 755` (`ls -lnd /data`: `drwxr-xr-x 3 0 0`). Compose also sets `user: "10001:10001"`.

| Check | Observed |
|---|---|
| `docker exec ... id` | `uid=10001(coroner) gid=10001(coroner) groups=10001(coroner)` |
| `/proc/1/status` | `Uid: 10001 10001 10001 10001`, `Gid: 10001 10001 10001 10001`, `Groups: 10001` |
| `/proc/1/cmdline` | `python -m coroner.collector.main` (python is PID 1) |
| `stat -c '%u:%g %a' /data/evidence` | `10001:10001 750` |
| `ls -lnd /data/evidence` | `drwxr-x--- 3 10001 10001 4096 Nov 18 17:19 /data/evidence` |
| Dockerfile mode | `chmod 0750` (matches 750) |
| `GET /healthz` | `{"status":"ok","durability":"crash",...,"storage_ok":true}` |

`storage_ok: true` comes from the Collector's own write plus fsync probe as UID 10001. The test also creates, writes, fsyncs and unlinks a temporary file in `/data/evidence` with a Python process running in the container (test 04).

## Sample POST and file proof

Run id: `m05-vol-c8c14c62`. The test POSTed a schema-valid Sample (`seq` 0) to `POST /v1/samples` and got `200` with `status: stored`. The file `/data/evidence/m05-vol-c8c14c62.jsonl` then existed, owned `10001:10001`, and held a `sample` record whose `sample` object equals the POSTed Sample. Mode of the file is `644` (`-rw-r--r--`), set by the Collector's open call; the directory mode `750` is what limits access.

## Restart proof

Test 06 restarted the container with `docker compose restart collector`, kept the same container id, waited for `healthy`, and checked that `StartedAt` changed. A second, independent restart was done by the coordinator:

| Item | Before restart | After restart |
|---|---|---|
| Container `StartedAt` | `2026-11-18T17:20:08.593161338Z` | `2026-11-18T17:20:34.470068305Z` |
| Volume `CreatedAt` | `2026-11-18T17:19:06Z` | `2026-11-18T17:19:06Z` (same volume) |
| Health | healthy | healthy |
| `RestartCount` | 0 | 0 |
| Evidence file SHA-256 | `8610D763...34E7` | `8610D763...34E7` (identical) |

SHA-256 in full: `8610D76336BFE39653A6CF924754BF5851FA6FBCE43D0C1327E5BA2A353834E7`. The file as read in the container before the restart, the file after it, and the body of `GET /v1/runs/m05-vol-c8c14c62/evidence` after it are all byte-identical (copies in `runs/m05-docker/evidence_file_before.jsonl`, `evidence_file_after.jsonl`, `evidence_api_after.jsonl`).

## No duplication

The file holds 5 records: `run_metadata created`, sample `seq 0`, `run_metadata resumed`, sample `seq 1` (posted by test 08 after the first restart), and one `gap` record (`silence_timeout`, detected by the watchdog after samples stopped). Sample `seq 0` appears exactly once before and after both restarts, and its record is unchanged. Test 07 also asserts that every line written before the restart appears exactly once afterwards.

## Hardening settings (from `docker inspect coroner-collector-1`)

| Setting | Value |
|---|---|
| `Config.User` | `10001:10001` |
| `HostConfig.CapDrop` | `[ALL]` |
| `HostConfig.SecurityOpt` | `[no-new-privileges:true]` |
| `/proc/1/status` | `CapInh/CapPrm/CapEff/CapBnd/CapAmb` all `0000000000000000`, `NoNewPrivs: 1` |
| Mount | `volume` `coroner_coroner_evidence` at `/data/evidence`, `RW: true` |
| `restart` | `on-failure:5` |
| Port | `127.0.0.1:8081` only |

With these restrictions the Collector still wrote to the volume (test 05 before the restart, test 08 after it, and the storage probe).

## Collector under a read-only root filesystem (task 5.8, separate container)

`compose.yaml` does not set `read_only: true` for the `collector` service (`HostConfig.ReadonlyRootfs` is `false`), and that was left unchanged by design. Only `demo_app` is read-only. The task 5.6 text asks to confirm that a read-only root filesystem does not break writes to the volume. The first 5.6 run did not test it. It is now tested outside Compose by `tests/docker_integration/test_collector_readonly_rootfs.py`. This is evidence about the image and the volume, not about the shipped Compose configuration.

The test starts one container named `coroner-ro-test-<uuid8>` from `coroner-collector:m0` with the image CMD unchanged (`python -m coroner.collector.main`), `--read-only`, no tmpfs at all, `--user 10001:10001`, `--cap-drop ALL`, `--security-opt no-new-privileges:true`, `CORONER_DURABILITY=crash` (the compose value), the volume `coroner_coroner_evidence` mounted read-write at `/data/evidence`, no published port, no privileged mode, and the default bridge network. Compose project `coroner` was down and a check confirmed no other container used the volume.

| Check | Result |
|---|---|
| `HostConfig.ReadonlyRootfs` | `true`; `Tmpfs` empty; `Privileged` false; `CapDrop` `[ALL]`; no `CapAdd`; `PortBindings` empty; one mount (the volume, `RW: true`) |
| Write to `/app/ro-probe.txt`, `/ro-probe.txt`, `/tmp/ro-probe.txt` inside the container | each fails with `errno=30 Read-only file system` |
| Create, write, fsync and unlink a temp file in `/data/evidence` as `uid=10001 gid=10001` | succeeds (`ok`) |
| `GET /healthz` (via `docker exec` and `urllib` to `127.0.0.1:8081`) | 200, `storage_ok: true` |
| `POST /v1/samples` with a valid Sample, run id `m05-ro-<uuid8>` | 200, `stored` |
| File `<run id>.jsonl` in the volume and `GET /v1/runs/<id>/evidence` | both hold the Sample; the two bodies are byte-identical |
| Evidence files that existed before the test | name, size and SHA-256 identical afterwards (read-only snapshot containers); the only new file is the run file |
| `docker stop` then `docker rm` of that container only | done; the volume still exists; no container uses it |

Result: 7 passed in 11.46s (run 1, run id `m05-ro-8c0b2529`), and again as part of the whole directory (34 passed in 50.93s, run id `m05-ro-95e15824`). Each run file has 3 records (`run_metadata`, one `sample`, one `gap` from the silence watchdog after the test stopped sending). The Collector needed neither `/tmp` nor any other writable path outside the volume. Raw output: `runs/m05-docker/ro_rootfs_run1.txt`, `runs/m05-docker/docker_integration_final.txt` (gitignored).

Limits: Docker Desktop on Windows, not a Linux host. If the project later wants `read_only: true` in `compose.yaml`, that is a separate change that needs its own compose test.

## Resources created by this run

| Type | Name | Id or created |
|---|---|---|
| Network | `coroner_default` | `f2bec6770bcb47228643fa51fb17903a24975854cfda9abd1ef801afed011b67` |
| Volume | `coroner_coroner_evidence` | CreatedAt `2026-11-18T17:19:06Z`, labels `com.docker.compose.project=coroner`, `com.docker.compose.volume=coroner_evidence` |
| Container | `coroner-collector-1` | `fffbeb2592755e0e025b570c72159d7790f10d3daa02bb8701864d813615f1dd` |

Images used (built in task 5.5): `coroner-collector:m0` (`sha256:cd9126e565333da7b76cb3f16ad494c94e2bcc9102e0cc8980e64eb55bf0420e`), `coroner-demo-app:m0`. The demo app container was never started.

After the run the Collector container is stopped (exit code 0), not removed. The volume and network were left in place.

## Preserved spike volume

`docker volume ls` after the run:

```
DRIVER    VOLUME NAME
local     coroner-spike_coroner_spike_evidence
local     coroner_coroner_evidence
```

`coroner-spike_coroner_spike_evidence` was not mounted, inspected for changes, or modified.

## Evidence files in the volume (provenance, task 5.8 review)

A read-only inventory (volume mounted `:ro`, no network, no writes) before the task 5.8 test changes found `.collector.lock` (0 bytes), `.coroner_probe/` (directory holding `probe.tmp`) and five run files. The volume `CreatedAt` is `2026-11-18T17:19:06Z`, and the preflight in task 5.5 (`runs/m05-docker/preflight.txt`, written 17:16:23Z) showed the volume absent. So none of the files can predate this phase.

| File | Size | mtime (UTC) | Run id | Records (all valid) | First to last recv_time | Provenance |
|---|---|---|---|---|---|---|
| `m05-vol-c8c14c62.jsonl` | 1634 | 17:19:25.566 | `m05-vol-c8c14c62` | 2 run_metadata, 2 sample, 1 gap | 17:19:15.155 to 17:19:25.571 | task 5.6 test run (volume_test.txt written 17:19:26) |
| `m05-vol-ce5b6291.jsonl` | 1634 | 17:29:53.710 | `m05-vol-ce5b6291` | same | 17:29:43.491 to 17:29:53.711 | task 5.6 module inside the combined run (docker-integration-combined.txt written 17:29:54) |
| `manual-065de53c.jsonl` | 8573 | 17:27:53.474 | `manual-065de53c` | 1 run_metadata, 20 sample | 17:27:43.998 to 17:27:53.479 (sample ts 17:27:43.976 to 17:27:53.476, seq 0 to 19) | Demo_App started by task 5.7 run 1 (compose-isolation-run.txt written 17:27:55) |
| `manual-fa40a358.jsonl` | 25263 | 17:28:40.990 | `manual-fa40a358` | 1 run_metadata, 60 sample | 17:28:11.511 to 17:28:40.992 (seq 0 to 59) | Demo_App of the coordinator's manual 5.7 collection (inspect and top files written 17:28:22 to 17:28:40, run ended when the stack was brought down) |
| `manual-d59bb45a.jsonl` | 8158 | 17:29:06.318 | `manual-d59bb45a` | 1 run_metadata, 19 sample | 17:28:57.337 to 17:29:06.321 (seq 0 to 18) | Demo_App of the 5.7 module in the combined run (that module runs first, then 5.6) |

Why the `manual-` prefix: `coroner/probe/probe.py` (`load_config`) generates `manual-<8 hex>` when `CORONER_RUN_ID` is empty or unset, and `compose.yaml` passes `CORONER_RUN_ID: "${CORONER_RUN_ID:-}"`, which is empty by default. Every `docker compose up` of `demo_app` without that variable therefore writes a fresh `manual-*` run. That is expected behavior, not stray data. Direct confirmation in this phase: the 5.7 module run during the task 5.8 full docker-integration run created a new `manual-71bb514d.jsonl` (20 samples, recv_time 19:11:26.280Z to 19:11:35.751Z).

How firmly this is established: the run ids were not logged by the earlier runs, so the file-to-run mapping rests on matching times and sample counts (20 samples at 500 ms is about 10 s, the demo's life in a 21 s test module; 60 samples is the 30 s manual collection), not on a recorded run id. It is consistent with the record and nothing contradicts it. It is correlation, not a signed proof. All lines in all files validate against the evidence schema, each file ends with a newline, and no file has a second run id.

Baseline for the unchanged check (SHA-256, taken before the read-only test): `m05-vol-c8c14c62` `a659468f...ed0b4`, `m05-vol-ce5b6291` `df953987...433e`, `manual-065de53c` `3cb5675f...63ba`, `manual-d59bb45a` `fd840bcaf...a8c`, `manual-fa40a358` `d275b929...4b39`, `.collector.lock` `e3b0c442...b855` (empty file). After the task 5.8 test runs, size, SHA-256 and mtime of all six were identical. New files from the 5.8 runs: `m05-ro-8c0b2529`, `m05-ro-95e15824`, `m05-vol-6ebb4886`, `manual-71bb514d`. Full listing: `runs/m05-docker/volume_inventory_before.json` and `volume_inventory_after.json`.

The spike volume `coroner-spike_coroner_spike_evidence` was checked by `docker volume ls` and `docker volume inspect` metadata only (CreatedAt `2026-11-14T10:56:17Z`, project `coroner-spike`); it was never mounted.

## Commands used

- Live test: `.\.venv\Scripts\python.exe -m pytest tests/docker_integration/test_volume_persistence.py --run-docker -q -p no:cacheprovider -rA` (driver `runs/m05-docker/run_volume_test.py`, output `runs/m05-docker/volume_test.txt`): 8 passed in 21.47s, none skipped.
- Evidence collection: `runs/m05-docker/volume_evidence.txt`. It contains some repeated sections because the first collection script was re-run in pieces.
- Only `docker compose up -d --no-deps collector`, `restart collector` and `stop collector` were used. No `down`, no `-v`, no `volume rm`, no prune.
