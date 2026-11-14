# Genuine Docker OOM evidence-survival spike (Task 1.10)

**Disposable spike. This is not an M0 experiment result.** It does not replace Experiment C or any M0 test. It was run once, with the parameters prepared in task 1.9 (victim limit 64 MiB, swap equal to the limit, 2 MiB step, 200 ms interval). No parameter was changed and nothing was retried.

Result: **PASS** (all five conditions met; one anomaly noted below).

Run date: 14 November 2026, host UTC 10:54 to 10:58, Windows 11, Docker Desktop 4.99.0, Docker Engine 29.9.3 (linux/amd64, WSL2 kernel 6.18.58.1), Compose v5.7.0, image `python:3.12.8-slim-bookworm` (id `2199a62885a1`). Branch `feat/coroner-m0`.

Raw outputs are in the gitignored `runs/spike/` (numbered files, listed at the end).

## Commands executed (in order)

```powershell
git branch --show-current
docker version --format '{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}'
docker ps -a --format '{{.ID}} {{.Names}} {{.Status}}'
docker volume ls --format '{{.Name}} {{.Labels}}'
docker network ls --format '{{.Name}}'
docker compose ls -a --format json
docker compose -f spike/compose.spike.yaml -p coroner-spike config
docker compose -f spike/compose.spike.yaml -p coroner-spike up -d collector
docker inspect coroner-spike-collector --format '{{.State.Status}} {{.State.Running}} {{.State.Health.Status}}'
docker exec coroner-spike-collector python -c "<GET http://127.0.0.1:8081/healthz>"
docker compose -f spike/compose.spike.yaml -p coroner-spike up -d victim
# PowerShell loop: docker inspect coroner-spike-victim --format '{{.State.Status}}|{{.State.Running}}|{{.State.ExitCode}}|{{.State.OOMKilled}}'
#   every 500 ms, deadline 120 s (Get-Date based; docker wait was not used)
docker inspect coroner-spike-victim ; docker inspect coroner-spike-collector
docker logs coroner-spike-victim ; docker logs coroner-spike-collector
docker volume inspect coroner-spike_coroner_spike_evidence
docker run --rm -v coroner-spike_coroner_spike_evidence:/data:ro python:3.12.8-slim-bookworm cat /data/spike.jsonl
docker compose -f spike/compose.spike.yaml -p coroner-spike down        # no -v
docker run --rm -v coroner-spike_coroner_spike_evidence:/data:ro python:3.12.8-slim-bookworm  (read, hash)
```

`docker compose down` ran only after the inspect output, logs, and evidence were saved. No `-v`, no volume deletion, no `docker kill`, `docker stop`, or signal was sent to the victim before it exited.

## Observations

**Starting state.** Before the run there were 0 containers, 0 volumes, only the default networks `bridge host none`, and no Compose projects (`runs/spike/03` to `06`). No existing Coroner resource could be affected.

**Collector before the victim started.** Container `ba08e06f8bb0a1519560c49c3df1fb26c4839b550534b5d0964b81e356cbeb87`, `running`, health `healthy`. `GET /healthz` returned `200 {"status": "ok", "storage_ok": true, ...}`. `/data` existed, owned by uid 0, mode `0o40755`, and `spike.jsonl` was present with size 0.

**Victim.** Container `118f31ae291d3c3206433fdcd53bffe3e1e96a954c92bb00146f638e1663cfc0`. Started `2026-11-14T10:56:57.680Z`, finished `2026-11-14T10:57:03.776Z` (about 6.1 s). The polling loop saw `exited|false|137|true` 7.2 s after the `up -d victim` command began (including its start-up time), well inside the 120 s limit (timed out: false). The victim was running on the previous poll and nothing had signalled it.

Raw `docker inspect` of the victim, read before any removal:

| Field | Value |
|---|---|
| `State.OOMKilled` | `true` |
| `State.ExitCode` | `137` |
| `State.Status` | `exited` |
| `State.Error` | empty |
| `HostConfig.Memory` | `67108864` (64 MiB) |
| `HostConfig.MemorySwap` | `67108864` |
| `HostConfig.Privileged` | `false` |

Victim log (`docker logs`): 27 lines `seq=N rss=... retained=... ack=200`, `seq` 0 to 26, every `ack=200`, and no line claims a handled exit, shutdown, or signal. The last line is `seq=26 rss=76582912 retained=54525952 ack=200`.

**Collector after the kill** (state read after the victim exited and before `down`): `status=running running=true health=healthy oom=false exit=0`. `GET /healthz` returned `200` with `"storage_ok": true` at server time `2026-11-14T10:57:15.433Z`.

**Persisted evidence** (`runs/spike/26_spike.jsonl`, read from the collector's named volume):

| Item | Value |
|---|---|
| Lines | 27, all valid JSON |
| `seq` | 0 to 26, contiguous, no gaps or duplicates |
| First RSS | 21,196,800 bytes (seq 0) |
| Maximum RSS | 76,582,912 bytes (seq 26, also the last) |
| RSS growth first to max | +55,386,112 bytes; strictly increasing at every step |
| `retained_bytes` | 0 at seq 0, 54,525,952 at seq 26 |
| RSS minus retained | 21,196,800 at seq 0, 22,056,960 at seq 26 (about constant) |
| Capture interval | 200.0 to 201.0 ms, mean 200.3 ms |
| Capture times | strictly increasing, 10:56:58.499Z to 10:57:03.708Z |
| Receive minus capture | 1 to 12 ms, never negative |
| First capture vs container start | 819 ms after `StartedAt` |
| Last capture vs container finish | 67.5 ms before `FinishedAt` |
| `container` field | `118f31ae291d` for all 27 samples, equal to the first 12 characters of the victim's container ID |
| `pid` | 1 for all samples |
| Stored values vs victim log | identical (27 rows of seq, rss, retained) |

Stored RSS values at selected steps: seq 0 = 21,196,800; seq 1 = 24,051,712; seq 10 = 42,962,944; seq 20 = 63,975,424; seq 21 = 66,076,672; seq 22 = 68,177,920; seq 26 = 76,582,912.

**After `docker compose down` (no `-v`).** The output shows both containers stopped and removed and the network `coroner-spike_default` removed. `docker ps -a` listed no containers. `docker volume ls` listed `coroner-spike_coroner_spike_evidence`, carrying the label `com.docker.compose.project=coroner-spike`. A new read-only helper container read `spike.jsonl` from that volume: 5,082 bytes, 27 newline characters, sha256 `a9286b127d6e947faf5ab51e3a0eeedb4210a5330f8f896084154b8a6c53afd4`. A second read gave the same hash. The 27 lines read after shutdown are identical to the 27 lines read before shutdown. The helper containers used `--rm`, and no volume was deleted.

**Anomaly (observation).** RSS exceeded the 67,108,864-byte limit in samples 22 to 26 (68.2 MB to 76.6 MB), and the container was still alive and acknowledged by the collector. The task's expectation of a highest RSS "below or near the 64 MiB limit" was therefore not met in the literal sense, though it is not part of the PASS rule.

## Conclusions

1. **A genuine Docker OOM kill occurred.** Docker's own state recorded `OOMKilled=true` and `ExitCode=137` for a container that had a 64 MiB memory limit and swap equal to the limit, and the victim code contains no signal, `os._exit`, `sys.exit`, or exit code 137. The kill came before any action by the run. The one exit path in the victim is a safety cap with code 3.
2. **The independent collector survived the victim.** It was running, healthy, and reporting `storage_ok: true` after the kill.
3. **Real pre-crash evidence persisted.** 27 samples, with RSS read by the victim from `/proc/self/statm`, were acknowledged and stored by the collector (write plus `fsync` before the 200 response) on a volume that only the collector mounts. The victim's log and the stored lines match exactly.
4. **Measurable memory growth is shown.** RSS rose by 55.4 MB, and the retained-bytes counter rose by 54.5 MB over the same samples.
5. **The evidence survived container removal.** The file was byte-identical (same hash) after `down`.

The spike thereby supports the M0 premise that an external collector keeps evidence across a genuine OOM kill on this Docker Desktop and WSL2 setup. It does not show that M0's own components do, which is what the M0 experiments test.

## Uncertainty and limits

- **Cause of the RSS-above-limit anomaly is not established.** A likely explanation, not tested: `/proc/self/statm` resident size includes file-backed pages (the interpreter and its libraries, about 21 MB at start) that the cgroup treats differently, while the OOM killer acts on memory it cannot reclaim. The cgroup's own counters (`memory.current`, `memory.stat`) were not read, so this is unconfirmed. For M0, RSS is a process measure and cannot be assumed to be capped by the container limit. This is relevant to the M0 diagnostic metric `peak_rss_ratio_of_limit`, which may exceed 1.0. That is a note for later design review, not a change made here.
- **The final moments are unobserved.** The last sample was captured 67.5 ms before the container finished, but the memory state at the kill is unknown. Whether the kill came during the next 2 MiB allocation is an inference.
- **One run.** Repeatability was not tested, and no claim is made beyond this run. Timings (the 7.2 s observed wait, the 6.1 s lifetime) are from one run.
- **Clocks.** Capture and receive times come from the same Docker VM clock. Comparison with the host clock was not done here (M0 task 7.1 measures that offset). The `StartedAt` and `FinishedAt` values come from the Docker daemon.
- **Durability.** Samples were acknowledged after `fsync` inside the container. Surviving a host or VM crash was not tested.
- **Collector permissions.** The collector ran as root with all capabilities dropped and wrote to a new named volume without trouble. The M0 collector runs as UID 10001, which is tested in task 5.6, not here.
- **Not simulated.** The workload is real memory allocation, the limit is Docker's, and the telemetry is real values read and sent by the victim. Nothing in the evidence was written or edited by hand.

## Saved outputs (`runs/spike/`, gitignored)

| File | Content |
|---|---|
| `18_inspect_victim.json`, `19_inspect_collector.json` | raw `docker inspect` before removal |
| `20_victim_logs.txt`, `21_collector_logs.txt` | container logs |
| `22_volume_inspect.json` | volume identity and labels |
| `24_collector_state_post.txt`, `23_collector_healthz_post.txt`, `25_victim_state.txt` | state after the kill |
| `15_poll.txt`, `16_wait_result.txt` | 500 ms polling record and result |
| `26_spike.jsonl` | evidence read before `down` |
| `35_spike_after_down.jsonl`, `34_volume_file_after.txt`, `36_hash_second_read.txt` | evidence and hashes read after `down` |
| `27_analysis.json`, `analyze.py` | the analysis script and its output |
| `28_down.txt` | `docker compose down` output |
| `29`, `30`, `31`, `37`, `38` | container, volume, and network lists after `down` |

## Remaining state

The volume `coroner-spike_coroner_spike_evidence` still exists with its evidence. Its deletion is outside this task and needs Execution Rule 11 and your explicit approval. The `spike/` directory is left in place.