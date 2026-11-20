# Host vs Collector clock offset (task 7.1)

Measured 2026-11-20 (host UTC, about 10:35 to 10:42) on branch `feat/coroner-m0`, HEAD `95594cf0b29c1825d49f82685076c18798aa891c` (remote `origin/feat/coroner-m0` `dcd47c52807f0ce0ce4e89cf7ab3323571b9875a`). All values come from real output saved in `runs/m07-71/` (gitignored). Nothing was POSTed to the Collector (its log holds only `GET /healthz`). No experiment, `/crash`, kill or OOM was run.

## Result

| Item | Value |
|---|---|
| Sign convention | positive offset = Collector clock ahead of host (`host time = collector time - offset`) |
| `clock-check --count 20` | exit 0, 20 of 20 usable, median offset +10 ms, range +0 to +28 ms (spread 28 ms), round trip min 7.2 / median 25.6 / max 59.6 ms |
| `clock-check --count 50` | exit 0, 50 of 50 usable, median offset +8 ms, range -1 to +37 ms (spread 38 ms), round trip min 4.2 / median 22.3 / max 79.9 ms |
| Best single measurement (lowest round trip, the CLI "Offset" line) | +1 ms +/- 3.6 ms (20 runs); +0 ms +/- 2.1 ms (50 runs) |
| Conservative bound (50 runs): `abs(median)` + max single uncertainty | 8 + 39.95 = about 48 ms (max uncertainty is half of the 79.9 ms max round trip) |
| Same with the best (min) uncertainty | 8 + 2.1 = about 10 ms |
| Conservative bound (20 runs) | 10 + 29.8 = about 40 ms |
| Largest single HTTP offset seen | +37 ms (CLI, 50 runs); worst single `abs(offset)` + own uncertainty in the independent series: 75.1 ms |
| Acceptance limit used | `abs(median HTTP offset)` + uncertainty < 125 ms (half of the 250 ms tolerance) |
| Decision | **PASS**. The tolerance stays at 250 ms (`CLOCK_TOLERANCE_MS`, unchanged). |

The median is above the best-round-trip value because measurements with a long round trip show a positive offset (see "Reading the numbers"). The honest statement is: the Collector clock is ahead of the host by a small amount, between about 0 and 10 ms, and no HTTP measurement shows an offset above 37 ms or a bound above 75 ms. Accuracy is not claimed beyond the uncertainty of each measurement (2 to 40 ms per measurement).

## Environment

| Item | Value |
|---|---|
| Host | Windows, PowerShell, Docker Desktop 4.99.0 (245822), Linux containers (VM), context `desktop-linux` |
| Docker Engine / Client | 29.9.3 / 29.9.3 (API 1.56), containerd v2.4.2, runc 1.5.2 |
| Compose | v5.7.0 |
| Host Python | `.venv` (Python 3.12.x), runner `python -m coroner.runner clock-check` |
| Base image | `python:3.12.8-slim-bookworm`, `sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0` |
| Isolated Compose project | `coroner-m07` (container `coroner-m07-collector-1`, volume `coroner-m07_coroner_evidence`, network `coroner-m07_default`, port `127.0.0.1:8081`) |

Before the run: no containers, no Compose projects, networks only `bridge`, `host`, `none`, ports 8080 and 8081 free (`runs/m07-71/preflight.txt`). Existing volumes `coroner_coroner_evidence` and `coroner-spike_coroner_spike_evidence` and the images below were only inspected, never mounted or changed.

## Images

| Image | ID | Note |
|---|---|---|
| `coroner-collector:m0` | `sha256:cd9126e565333da7b76cb3f16ad494c94e2bcc9102e0cc8980e64eb55bf0420e` | created `2026-11-18T17:16:43Z`; used for the measurement; unchanged afterwards |
| `coroner-demo-app:m0` | `sha256:23571625f7319525505816774be12f752854b04608b9b0912ef9d1e1b5ca2c52` | never started; unchanged |
| `coroner-collector:m07-verify` | `sha256:c441b58b39cd70593adf92ecb5d07a987fd7f07385909823bbe22715aa0eb121` | clean rebuild (`docker build --no-cache`, no `--pull`), created `2026-11-20T10:36:22Z` |
| `coroner-demo-app:m07-verify` | `sha256:d8b7e062185930bf647b562c13cc36812581a9de18fbf85a442db6e1902c8240` | clean rebuild |

The new IDs differ from the `:m0` IDs. They are identifiers of an image index that includes a BuildKit attestation manifest, and a `--no-cache` rebuild produces new layer timestamps and a new attestation, so identical IDs were not expected. Identical content is not claimed. The checks below show the rebuilt images match the pinned inputs.

Clean-build proof (`runs/m07-71/build_collector.txt`, `build_demo.txt`, `verify_images.txt`):
- Both builds exited 0. Both logs show `FROM docker.io/library/python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0`, the digest recorded above. No pull happened.
- `docker run --rm --entrypoint python <image> -m pip check` printed `No broken requirements found.` for both. (pip also prints a harmless warning that `/home/coroner/.cache/pip` is not writable, because the user has no home directory.)
- `pip freeze` in each image lists exactly the 12 pins in `requirements/collector.txt` and `requirements/demo_app.txt` (which are identical): annotated-types 0.8.0, anyio 4.16.0, click 8.5.1, fastapi 0.115.6, h11 0.16.0, idna 3.20, pydantic 2.14.3, pydantic_core 2.50.4, starlette 0.41.3, typing-inspection 0.4.5, typing_extensions 4.16.0, uvicorn 0.34.0. No pin was changed.
- `Config.User` is `10001:10001` in both. Collector CMD `python -m coroner.collector.main`, demo CMD `python -m coroner.demo_app.main`.
- Image contents. Collector `/app/coroner`: `__init__.py`, `collector`, `common`, `evidence`. Demo `/app/coroner`: `__init__.py`, `demo_app`, `probe`. `/app` also holds only `requirements/<own>.txt`. No tests, docs, `.git`, `compose.yaml`, `docker/` or spike files.

## Startup and isolation proof

Command: `docker compose -p coroner-m07 -f compose.yaml up -d --no-deps --no-build collector`, exit 0. Because of `--no-build` the container ran the existing `coroner-collector:m0` image (inspect `Config.Image` `coroner-collector:m0`, `Image` `sha256:cd9126e5...0e`). The `m07-verify` images were not started.

| Check | Observed |
|---|---|
| Container name, project label | `/coroner-m07-collector-1`, `com.docker.compose.project=coroner-m07` |
| Mounts | exactly one: `volume coroner-m07_coroner_evidence` at `/data/evidence`, `RW: true`; `Binds` `coroner-m07_coroner_evidence:/data/evidence:rw`. Neither protected volume is mounted |
| `id` in container | `uid=10001(coroner) gid=10001(coroner) groups=10001(coroner)` |
| `stat` / `ls -ldn` | `/data` `0:0 755`; `/data/evidence` `10001:10001 750`; `drwxr-x--- 3 10001 10001 4096 Nov 20 10:37 /data/evidence` |
| Ports | `PortBindings` and `NetworkSettings.Ports`: `8081/tcp` to `127.0.0.1:8081` only. Host listener (`Get-NetTCPConnection -State Listen`): `127.0.0.1  8081  Listen` |
| Hardening | `CapDrop [ALL]`, `SecurityOpt [no-new-privileges:true]`, `Privileged false`, `RestartCount 0` |
| Health | `healthy` on the first poll after about 25 s of uptime (5 consecutive healthcheck exits 0) |
| Single process | `docker top ... -eo pid,ppid,user,args` listed one row: `1469 1446 10001 python -m coroner.collector.main`. `/proc/1/cmdline` = `python -m coroner.collector.main`. A `/proc` scan inside the container showed PID 1, plus the `runc init` and `python -c` processes of the scan's own `docker exec` (not workers). No healthcheck row was present at that instant |

## Storage health (readiness)

`GET http://127.0.0.1:8081/healthz`, host UTC before `2026-11-20T10:38:01.880878+00:00`, after `2026-11-20T10:38:01.963564+00:00` (`runs/m07-71/healthz.json`):

```
HTTP 200
{"status":"ok","durability":"crash","server_time":"2026-11-20T10:38:01.961Z","uptime_s":27,"storage_ok":true}
```

`storage_ok` is the JSON boolean `true`, `durability` is `crash`, container health `healthy`. The acceptance condition (200 and `storage_ok is true`) is met. This is the real write plus fsync probe on the isolated volume.

## HTTP clock-check output (primary evidence)

`clock-check --help` showed only `--count N` and `--collector-url URL`; only those flags were used.

```
$ .\.venv\Scripts\python.exe -m coroner.runner clock-check --count 20 --collector-url http://127.0.0.1:8081
exit=0
clock-check:    collector=http://127.0.0.1:8081
Measured at:    2026-11-20T10:38:08.987Z
Measurements:   20 of 20 usable
Offset:         +1 ms (positive = Collector clock ahead of host; host time = collector time - offset)
Uncertainty:    3.6 ms (half the round trip of the best measurement)
Offset median:  +10 ms
Offset range:   +0 ms to +28 ms (spread 28 ms)
Round trip:     min 7.2 ms, median 25.6 ms, max 59.6 ms
Tolerance:      250 ms (documented Experiment B clock tolerance; never changed automatically)
```

```
$ .\.venv\Scripts\python.exe -m coroner.runner clock-check --count 50 --collector-url http://127.0.0.1:8081
exit=0
clock-check:    collector=http://127.0.0.1:8081
Measured at:    2026-11-20T10:38:11.890Z
Measurements:   50 of 50 usable
Offset:         +0 ms (positive = Collector clock ahead of host; host time = collector time - offset)
Uncertainty:    2.1 ms (half the round trip of the best measurement)
Offset median:  +8 ms
Offset range:   -1 ms to +37 ms (spread 38 ms)
Round trip:     min 4.2 ms, median 22.3 ms, max 79.9 ms
Tolerance:      250 ms (documented Experiment B clock tolerance; never changed automatically)
```

No warning line was printed and no failures line. The CLI prints only summary statistics, so the maximum single-measurement uncertainty is derived here as half of the printed maximum round trip (59.6 / 2 = 29.8 ms and 79.9 / 2 = 39.95 ms), the same definition the CLI uses for the best measurement.

Supplemental independent series: a separate driver (`runs/m07-71/phase6.py`, `urllib`, `time.time_ns`, same midpoint method) took 50 more HTTP measurements to see per-measurement values (`direct_clock.json`, key `http_supplemental`). All 50 were HTTP 200 with `storage_ok` true. Median offset +8.13 ms, mean +10.05 ms, range +0.44 to +35.53 ms (spread 35.1 ms), standard deviation 7.07 ms, uncertainty min 2.58 / median 11.69 / max 39.56 ms, best measurement +1.02 ms +/- 2.58 ms, worst `abs(offset)` + its own uncertainty 75.09 ms. It agrees with the CLI runs.

## Direct host vs container comparison (secondary evidence)

`docker exec coroner-m07-collector-1 date -u +%s.%N` with host `time.time_ns()` immediately before and after each call, 30 pairs, 0.2 s apart, no errors (`runs/m07-71/direct_clock.json`). Offset = container time minus host midpoint; uncertainty = half the host round trip.

| Statistic | Value |
|---|---|
| Usable pairs | 30 of 30 |
| Median / mean offset | +62.0 ms / +62.0 ms |
| Range | +37.4 to +82.5 ms (spread 45.1 ms), standard deviation 10.2 ms |
| Round trip | 145.6 to 312.5 ms (uncertainty min 72.8 / median 87.8 / max 156.2 ms) |
| Best pair (lowest round trip) | +53.0 ms +/- 72.8 ms |
| Largest `abs(offset)` + own uncertainty | 200.2 ms |

Sign agrees with the HTTP result (Collector ahead). The magnitude does not match: +62 ms versus +8 ms. This is expected from the method, not evidence of a larger clock offset. A `docker exec` spends most of its 145 to 312 ms round trip on the client, daemon and runc process start before `date` reads the clock, so the container timestamp sits late in the window and the midpoint is a poor estimate. Every pair has an uncertainty (73 to 156 ms) larger than the gap to the HTTP median (about 54 ms), and the lower bound `offset - uncertainty` of every pair is below zero (-118.9 to -19.8 ms), so no pair excludes the small HTTP offset. The direct method cannot resolve below about 70 ms, and slow `docker exec` is not used to infer drift. I treat the two methods as consistent within their uncertainties, with the HTTP method (round trip 4 to 80 ms, same code path as the runner) as the primary result. If the direct median is read at face value, its worst single bound of 200.2 ms is still under 250 ms.

## Reading the numbers

- Round trip and offset move together: long round trips give larger positive offsets, and the best (shortest round trip) measurements give +0 to +1 ms. That fits an asymmetric delay, where the Collector reads its clock closer to the end of the round trip than the midpoint assumes, rather than a real +10 ms clock difference. The true offset is therefore likely nearer the best measurements (about 0 to +1 ms, +/- 2 to 4 ms), but the median of +8 to +10 ms is kept as the conservative figure.
- `server_time` has millisecond resolution (truncated), which adds under 1 ms of bias.
- Spread (28 to 38 ms) is explained by round-trip jitter (max round trips 59.6 and 79.9 ms) and shows no drift: runs taken seconds apart agree, and no measurement is outside the range implied by the round trip.
- Measurements span about 1 to 3 seconds per run and about 30 seconds in total, so slow drift over longer periods is not measured.

## Acceptance decision

| Criterion | Result |
|---|---|
| At least 20 valid HTTP measurements | 20 of 20 and 50 of 50 |
| `clock-check` exit code 0 | both runs exit 0 |
| `/healthz` 200 and `storage_ok` true | yes |
| `abs(median HTTP offset)` + max uncertainty below 125 ms | 8 + 39.95 = about 48 ms (min-uncertainty variant about 10 ms) |
| Spread without unexplained instability | 28 and 38 ms, explained by round-trip jitter |
| No HTTP measurement establishing an offset above 250 ms | largest +37 ms; worst offset plus uncertainty 75.1 ms |
| Host vs container results not materially disagreeing | same sign; magnitude difference explained by `docker exec` latency, within uncertainty |

Decision: **PASS**.

## Tolerance decision

`CLOCK_TOLERANCE_MS` stays at 250 ms. It was not edited. Measured offset plus worst uncertainty is about 48 ms (75 ms for the worst single measurement), so 250 ms has a margin of more than 3 times.

Proposal for the user (not applied): if a tighter bound is wanted, 125 ms is justified by these measurements on this host (worst single `abs(offset)` + uncertainty 75.1 ms). It rests on one Docker Desktop VM session and about 30 seconds of data, so I recommend keeping 250 ms. Experiment B should use the per-run `clock_offset_ms` the runner measures, as designed.

## Limitations

- Docker Desktop on Windows (Linux VM). The VM clock and host clock relationship may differ after sleep/resume, long uptime or time sync events. No Linux-host check was done.
- Only about 30 seconds of measurements. Drift over hours is not measured.
- HTTP uncertainty includes request-path latency (host to VM port forwarding), and the direct `docker exec` method includes large process-start overhead, so the direct figures are weak.
- `clock-check` prints summary statistics only; per-measurement data come from the supplemental driver.
- The container ran `coroner-collector:m0`; the `m07-verify` images were built and verified but not started, because `compose.yaml` names the `:m0` image and `--no-build` was required.
- The first `--no-cache` collector build ran twice (the first run's log capture failed on a Python decoding error in my driver; its build succeeded). The `m07-verify` tag was rebuilt; the logs above are from the second build.
- Experiments A, B, C and the HTTP 500 test were not run, and no sample or termination was POSTed.

## Teardown

`docker compose -p coroner-m07 -f compose.yaml down` (no `-v`), exit 0: container `coroner-m07-collector-1` and network `coroner-m07_default` removed. After teardown (`runs/m07-71/phase9.txt`): `docker ps -a` empty, `compose ls -a` empty, volumes `coroner-m07_coroner_evidence` (kept, CreatedAt `2026-11-20T10:37:31Z`), `coroner_coroner_evidence` and `coroner-spike_coroner_spike_evidence`; the `docker volume inspect` output of both protected volumes is identical to the preflight (checked by JSON comparison: True). Image IDs of `:m0` (both), `python:3.12.8-slim-bookworm` and the two `m07-verify` tags are as listed above and unchanged. The verification images and the `coroner-m07_coroner_evidence` volume were left in place for the user to decide on removal.
