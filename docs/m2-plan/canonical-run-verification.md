# Canonical run verification (task 2.5)

Read-only check of the three canonical runs in the real `runs/` directory. Nothing was written under `runs/`. No Docker command was run. No process was stopped other than Viewers started during this check (preflight-launched PID 20792 and the process-tool Viewer on 127.0.0.1:8765, both stopped afterwards). The process listening on port 8766 (python, PID 6940) was reported by the preflight and left running.

Sources: `manifest.json` (M), `docker_inspect.json` (I), the Viewer API `GET /api/runs/<id>` (API) on 127.0.0.1:8765, and `scripts/demo-preflight.ps1`.

Runs: A `a-20261120T211146Z-4300f395`, B `b-20261120T211311Z-ce313da1`, C `c-20261120T211433Z-09b81ca1`.

## Preflight result

`scripts\demo-preflight.ps1 -BaselinePath %TEMP%\coroner-m2\demo-baseline.txt -LaunchViewer`: exit code 0, `RESULT: PASS (20 checks)`. Includes python 3.12.10, `import coroner.viewer`, run files present, port 8765 free (8766 occupied by python PID 6940, not stopped), HTTP 200 for `/`, `/api/runs`, the three `/runs/<id>`, the three `/api/runs/<id>` and `/compare?a=...&b=...&c=...`, the three fact checks, and `integrity: 9 files match the baseline`.

The same command with `-Json`: exit code 0, `ok` true, 20 checks, none failed.

## Facts compared with the Non-Negotiable Demo Facts

| Fact | Source field | A | B | C | Match |
|---|---|---|---|---|---|
| ExitCode | I `State.ExitCode`; M `container_state.exit_code`; API `docker_state.raw.exit_code` | 1 | 137 | 137 | yes |
| OOMKilled | I `State.OOMKilled`; M `container_state.oom_killed`; API `docker_state.raw.oom_killed` | false | false | true | yes |
| Memory limit | I `HostConfig.Memory`; M `container_state.host_config_memory_bytes` | 134217728 | 134217728 | 134217728 | n/a |
| API source agreement | API `docker_state.source` | `inspect_and_manifest_agree` | `inspect_and_manifest_agree` | `inspect_and_manifest_agree` | n/a |
| Container status | I `State.Status` | exited | exited | exited | n/a |
| Sample count | M `evidence_summary.sample_count`; API `evidence_summary.sample_count` | 15 | 15 | 45 | n/a |
| Record count | M `evidence.record_count`; API `valid_record_count` | 17 | 17 | 47 | n/a |
| Missing / unreconciled drops / malformed | M `evidence_summary` | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | n/a |
| Peak RSS (bytes) | M `highest_rss_bytes`; API `evidence_summary.highest_rss.rss_bytes` | 74326016 | 74174464 | 141090816 | n/a |
| Runner termination description | M `termination_description` | `controlled fatal process exit via os._exit(1)` | `SIGKILL sent by the runner (docker kill --signal=KILL)` | `genuine Docker OOM kill (kernel cgroup memory limit)` | n/a |
| Termination cause label | M `termination.cause_label` | `controlled_fatal_process_exit` | `sigkill_sent_by_runner` | `docker_oom_kill` | n/a |

Result: all three runs match the stated facts. No mismatch found.

## B: SIGKILL sent by the Runner

- M `termination_description` = `SIGKILL sent by the runner (docker kill --signal=KILL)`.
- M `termination.cause_label` = `sigkill_sent_by_runner`.
- M `times.sigkill_sent_time` = `2026-11-20T21:13:21.690Z` (host clock). M `phases` contains `sigkill_sent` at `2026-11-20T21:13:22.115Z`. The two times differ; the manifest `times` field is the recorded send time and the phase entry is the phase log time. The API shows the SIGKILL marker at the `times` value (`termination_markers.sigkill.time` = `2026-11-20T21:13:21.690Z`, drawn true).
- I `State.OOMKilled` = false, `State.ExitCode` = 137, `State.FinishedAt` = `2026-11-20T21:13:21.760545826Z` (Docker clock).
- B is not attributed to Docker OOM.

## B: clock correction

- M `clock_offset_ms` = 9, M `clock_offset_uncertainty_ms` = 12.5375. M `sample_timing.clock_offset_ms` = 9.
- API `timing.clock_offset_ms` = 9, `clock_offset_source` = `sample_timing`, `clock_offset_clock` = `collector_minus_host`, `clock_uncertainty_ms` = 12.5375, `clock_offset_conflict` = null. API `chart.time_axis` = `corrected`; Sample points carry `corrected_capture_time` (seq 0: raw `2026-11-20T21:13:14.687Z`, corrected `...14.678Z`).
- The offset is derived from Sample timing by the Runner. Nothing in the saved evidence verifies it independently, so it stays "derived, not independently verified".

## C: Docker OOM record

- I `State.OOMKilled` = true, `State.ExitCode` = 137, `HostConfig.Memory` = 134217728.
- M `termination_description` = `genuine Docker OOM kill (kernel cgroup memory limit)`, `termination.cause_label` = `docker_oom_kill`, M `expected` = exit 137, oom_killed true; M checks `oom_killed_true`, `exit_code_137`, `memory_limit_matches_host_config` passed.
- M `memory`: `configured_limit_bytes` 134217728, `highest_rss_bytes` 141090816, `first_rss_bytes` 46419968. Peak RSS in the Samples is above the configured limit (M `diagnostics.peak_rss_ratio_of_limit` = 1.0512). The cause of the memory growth is not established here; M `sampling_limitations` states that the final seconds before the kill may be unobserved.
- `times.crash_request_time` and `times.sigkill_sent_time` are both null, so no Runner kill was recorded for C.

## Clock basis of A and C

- API `chart.time_axis` = `raw_capture` for A and C, and API `timing.clock_offset_ms` = null for both. Sample points carry `corrected_capture_time` = null. A and C therefore use raw Probe `Capture_Time`.
- Observation: the manifests for A and C also record `clock_offset_ms` (A 14, C 6) with uncertainty (A 15.3935, C 9.449), but the Viewer does not apply them (no `sample_timing` source). This agrees with the demo fact; it is noted so nobody presents A or C as corrected.

## Evidence integrity

- Preflight integrity check: 9 canonical files match `%TEMP%\coroner-m2\demo-baseline.txt`.
- Whole `runs/` against `%TEMP%\coroner-m1\runs-before.snapshot.txt` (path, SHA-256, size): 1,217 files found, 1,217 in the snapshot, 10,210,863 bytes, 0 differences, none missing.
- `git status --short` shows no change under `runs/` (`runs/` is ignored and untracked).

## Notes

- `/compare` returns the HTML shell (705 bytes); the comparison and its headline are rendered client-side, so this check does not assert headline text. The headline condition itself is not exercised by the preflight.
- The manifests contain absolute host paths in `raw_inspect_path`, `host_copy_path` and `docker_inspect_path`. They are not reproduced here.

## B SIGKILL timestamp investigation

Run `b-20261120T211311Z-ce313da1`. Read-only. No evidence, code or timestamp was changed or reconciled. Manifest = `runs/<run>/manifest.json` (M). Line numbers are those of the file as read.

Question: why do `times.sigkill_sent_time` (`21:13:21.690Z`) and the `sigkill_sent` phase entry (`21:13:22.115Z`) differ by 425 ms?

### Findings

| Value | Artifact and field | Writer (code) | Event it records | Clock |
|---|---|---|---|---|
| `2026-11-20T21:13:21.690Z` | M `times.sigkill_sent_time` (M line 131) | `coroner/runner/experiments/exp_b.py` lines 147-148: `sigkill_sent = ctx.now()` then `record_time(ctx, "sigkill_sent_time", sigkill_sent)`. The comment at line 147 and the module docstring step 4 say the time is taken immediately before the kill call. `record_time` is in `base.py` and only sets `extra["times"][key]`; it does not write the file. | Instant read just before `ctx.docker.kill(...)` (exp_b.py line 151). It is a pre-call time, not a delivery time. | Host_Clock: `ExperimentContext.now()` (`base.py` ~766-768) uses `wall_clock`, default `datetime.now(timezone.utc)` (`_utc_now`, `base.py` ~730), truncated to ms. |
| `2026-11-20T21:13:22.115Z` | M `phases[]` entry `name: "sigkill_sent"` (M lines 67-68) | `exp_b.py` line 153: `write_phase(ctx, "sigkill_sent")` after `ctx.docker.kill(...)` returned. `write_phase` (`base.py` ~846-855) sets `now = ctx.stamp()`, appends `{"name", "time": now}` to `extra["phases"]`, sets `end_time`, then writes the manifest. | Instant the Runner logged the phase after `docker kill` returned (the phase name says "sent", the stamp is taken after the call completed). One stamp per phase; there is no start/end pair. | Host_Clock, same `wall_clock` as above, same ms truncation. |

Other related timestamps (same run):

| Timestamp | Where | Value |
|---|---|---|
| Crash request | M `times.crash_request_time` (M line 130) | null (not used in B) |
| Previous phase `leak_finished` | M `phases[]` (before the kill) | `21:13:19.951Z` |
| Last Sample `seq` 14 | `evidence.jsonl` line 16: `sample.ts` (Container_Clock), `recv_time` (Collector receive) | `ts` `21:13:21.689Z`, `recv_time` `21:13:21.691Z` |
| Container `FinishedAt` | `docker_inspect.json` line 21 (Docker_Clock); M `container_finished_at` (M line 97) | `21:13:21.760545826Z`; manifest copy `21:13:21.760Z` |
| Inspect time | M `inspect_time` (M line 21); M `phases` `container_inspected` | `21:13:22.219Z`; `21:13:22.223Z` |
| Termination | `evidence.jsonl` line 17 `observed_time`; M `termination.observed_time` (M line 146) | `21:13:22.219Z` |
| Not recorded | start and finish of the `docker kill` subprocess | no separate field exists |

Order on the Host_Clock: `sigkill_sent_time` 21.690 < phase `sigkill_sent` 22.115 < inspect 22.219. The Docker_Clock `FinishedAt` 21.760 lies between the two disputed values (Docker clock, uncorrected, not directly comparable to the host values; the recorded host/collector offset is +9 ms with uncertainty 12.5375 ms, and no offset between Docker_Clock and Host_Clock is recorded).

### Clock comparability

Both disputed values come from the same `ctx.wall_clock` and the same truncation, so they are directly comparable with each other. `ctx.monotonic` is used for polling and durations only and is not involved in either value. `FinishedAt` is a Docker_Clock value; `Sample.ts` is Container_Clock (corrected by `clock_offset_ms` only in the Viewer's corrected axis).

### Conclusion: expected by construction (with one bounded inference)

Established from code: the two values are two different events on the same clock. One is read before the `docker kill` subprocess starts, the other after `DockerCli.kill` returns (`docker_port.py` lines 207-212, which runs `docker kill --signal KILL <id>` through `subprocess.run` in `_call`, lines 151-168, and waits for it to exit). The 425 ms difference therefore equals the Runner-side duration of the `docker kill` call plus the time to take the next stamp. There is no sleep between them in `exp_b.py` lines 147-153.

Inference (not directly measured): a 425 ms `docker kill` call on Windows with Docker Desktop is plausible, and the earlier recorded run `b-20261120T173541Z-3fa52342` (README, Task 7.4 row) reports 414 ms for the same interval. That second run is read only from the README, not re-checked here. I found no inconsistency; the cause of the exact duration (process start, CLI, daemon, waiting for the container to stop) is not recorded and is not established.

Not established: the instant the signal reached the container. It lies after 21.690 (the pre-call stamp). `FinishedAt` 21.760 (Docker_Clock) is when Docker recorded the exit, and the call returned later at 22.115 (host). This record does not prove the signal was delivered at any particular instant between those values.

### Viewer marker

`coroner/viewer/model.py` lines ~1225-1243: `sigkill_time_str = times.get("sigkill_sent_time")`, `sigkill_ms = epoch_ms_utc(...)`, and `x_ms = sigkill_ms` when drawn. Other uses: `sigkill_ms=_epoch_ms_times(times, "sigkill_sent_time")` (line ~1132) and the `sigkill_sent_time*` timing keys (lines ~1159-1161, clock `host`). The phase list is not read for the marker. The marker is therefore at the pre-call Host_Clock stamp (21.690), which is the earliest possible send instant, not a later phase time.

The legend (`coroner/viewer/static/copy.js` line 39: `SIGKILL sent (Host_Clock; position derived, not independently proven)`) remains supported: Host_Clock is correct, and "derived, not independently proven" is accurate because the value is a pre-call stamp and delivery was not independently observed. No viewer change is needed.

### Wording

Earlier text in this file (section "B: SIGKILL sent by the Runner") calls the `times` value "the recorded send time". More exact wording is "the time the Runner recorded immediately before issuing `docker kill`". This section refines it and does not change any recorded value.

Recommended narration:

- "The Runner recorded the time just before it issued `docker kill --signal=KILL`, and the chart draws the SIGKILL marker there. Docker then recorded the container exit shortly after."
- If asked about the two values: "The manifest holds a time taken before the kill command and a phase entry written after the command returned. The command itself took a few hundred milliseconds."
- Avoid: stating an exact instant as when the signal arrived, saying the phase time is the send time, or saying the two values are equal. Avoid giving the 425 ms as the kill latency; it is the duration of the CLI call, not the time to terminate the process.

Status: expected by construction (code path), exact 425 ms duration not independently explained. Not unexplained and not an inconsistency.
