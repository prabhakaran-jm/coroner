# M0.8 Task 8.3 Test Runs

Branch feat/coroner-m0, HEAD 25c912e2e5fc622b87c7ed0935e278279a7b8494.
Each figure below is tagged with its source. See the provenance section for the four source classes.

## (a) Default suite

Source: console-reported by the user, except the baseline row, which has a local artifact.

| Run | Command | Passed | Failed | Skipped | Deselected | Warnings | Duration | Exit |
|---|---|---|---|---|---|---|---|---|
| Default suite after harness tests (console-reported) | `python -m pytest -q` | 4510 | 0 | 5 | 53 | 1 (known Starlette/AnyIO deprecation) | 462.02 s | 0 |
| Baseline before harness tests (agent-measured) | `python -m pytest -q -ra -p no:cacheprovider` | 4451 | 0 | 5 | 53 | 1 | 485.91 s (pytest line); 487.2 s driver wall clock | 0 |

- No Docker integration was enabled for the default suite.
- The 59 new harness tests account for the difference: 4451 + 59 = 4510.
- Baseline artifacts: `runs/t83_baseline_stdout.txt` (pytest summary line `4451 passed, 5 skipped, 53 deselected, 1 warning in 485.91s`), `runs/t83_baseline_summary.txt` (`exit=0`, `duration=487.2`). The user's 485.9 s matches the pytest-reported 485.91 s.

## (b) Docker modules

Source: console-reported by the user. Each module was run separately with `--run-docker` and an explicit file path. There was no single unscoped `--run-docker` run.

| Module | Passed | Failed | Duration | Exit |
|---|---|---|---|---|
| test_placeholder.py | 1 | 0 | 0.86 s | 0 |
| test_compose_isolation.py | 18 | 0 | 26.19 s | 0 |
| test_volume_persistence.py | 8 | 0 | 19.84 s | 0 |
| test_collector_readonly_rootfs.py | 7 | 0 | 9.38 s | 0 |
| test_ordinary_500.py | 2 | 0 | 14.88 s | 0 |
| test_experiment_a.py | 4 | 0 | 17.27 s | 0 |
| test_experiment_b.py | 6 | 0 | 17.59 s | 0 |
| test_experiment_c.py | 7 | 0 | 32.41 s | 0 |
| Total | 53 | 0 | 138.42 s | all 0 |

Arithmetic checked: 1 + 18 + 8 + 7 + 2 + 4 + 6 + 7 = 53 passed. 0.86 + 26.19 + 19.84 + 9.38 + 14.88 + 17.27 + 17.59 + 32.41 = 138.42 s (sum of the eight per-module pytest durations, not wall-clock time).

## (c) Isolation

Source: user-reported, unless noted.

- Compose projects: `coroner-t83-m05` (M0.5 modules) and `coroner-t83` (M0.7 modules).
- New volumes: `coroner-t83-m05_coroner_evidence` and `coroner-t83_coroner_evidence`.
- Five evidence volumes were listed: `coroner_coroner_evidence`, `coroner-spike_coroner_spike_evidence`, `coroner-m07_coroner_evidence`, and the two new volumes above.
- No containers remained after the final experiment, and the original `:m0` image IDs were unchanged (user-reported).
- Earlier read-only observation by the orchestrator at HEAD 25c912e: no containers, and all five volumes present. Image IDs were not re-verified by that check or by this task.
- No local artifact for the isolation statements was found in this task, and none was created.

## (d) Experiment B and C results

### Experiment B, run b-20261120T211311Z-ce313da1

| Item | Console-reported (user) | Saved Runner artifact, read in this task |
|---|---|---|
| Manifest outcome | passed | passed (`RunManifest` via `coroner.runner.manifest.read_manifest`) |
| Container Id | not stated | 764c1468dc071b6c8167d4bb40d4f2de6f7536d46a49c72d71c67546e1ff4fe5 (64 hex); equals manifest `container_id` and `container_state.container_id` |
| State.Status / ExitCode / OOMKilled | exit 137, OOMKilled false | exited / 137 / false |
| HostConfig.Memory / MemorySwap | not stated | 134217728 / 134217728 |
| Collector survival | survived | manifest `collector_health` ok true, http 200, storage_ok true; check `collector_survival` passed |
| Pre-kill Samples | 15 | 15 (manifest `sample_timing.pre_kill_count`); independent recount: 15 |
| Samples beyond 250 ms tolerance | 0 | 0 (manifest); independent recount from `ts - clock_offset_ms` (offset 9) vs `sigkill_sent_time` 2026-11-20T21:13:21.690Z: 0 beyond, 0 within, latest sample 10 ms before the kill |
| Seq continuity | not stated | seq 0..14 contiguous, 0 missing |
| First / peak RSS | not stated | 46678016 / 74174464 bytes |
| Termination cause label | not stated | `sigkill_sent_by_runner` |
| Durability | crash | `crash`, equals `run_metadata` durability (check `durability_matches_run_metadata` passed) |
| Diagnostics / limitations | not stated | `diagnostics` null, `sampling_limitations` null (not applicable to B). Limitation recorded: interval between the last Sample and the actual termination is unobserved |

### Experiment C, run c-20261120T211433Z-09b81ca1

| Item | Console-reported (user) | Saved Runner artifact, read in this task |
|---|---|---|
| Manifest outcome | passed | passed |
| Container Id | not stated | 30df908e7ea68c50469b8bcaab82a7769eef892e19bbf51f931e84c0735cdab1 (64 hex); equals manifest `container_id` and `container_state.container_id` |
| State.Status / ExitCode / OOMKilled | exit 137, OOMKilled true | exited / 137 / true |
| HostConfig.Memory / MemorySwap | 134217728 | 134217728 / 134217728 |
| Collector survival | survived | manifest `collector_health` ok true, http 200, storage_ok true; check `collector_survival` passed |
| Samples | 45 | 45 (manifest and independent recount), seq 0..44 contiguous, 0 missing |
| First RSS / peak RSS | 46419968 / 141090816 | 46419968 / 141090816 (peak is also the last Sample) |
| Termination cause label | not stated | `docker_oom_kill` |
| Durability | crash | `crash`, equals `run_metadata` durability |
| Diagnostics / limitations | not stated | diagnostics present (`rss_monotonic_tail` true, `peak_rss_ratio_of_limit` 1.0512, `sample_count` 45; informational only). `sampling_limitations` present: 500 ms interval, about 4149974 bytes/s leak rate, three statements |

Differences from the user's figures: none found. The only extra detail is that MemorySwap equals Memory (134217728) in both saved inspect files.

### Runner exit code 0

No artifact stores the Runner process exit code directly. The support is indirect: both manifests have `outcome: passed` and `failure: null`, which under the Runner contract corresponds to exit 0. The read-only command `python -m coroner.runner summary <run_id> --runs-dir runs` exited 0 for both runs, but that is the exit code of the summary command, not of the Runner. The user's console report is the only direct source for the Runner exit code.

### Summary command output (read-only)

Both runs: `summary` exit 0, outcome passed.

- B: `Container: status=exited exit_code=137 oom_killed=false`, `Samples: 15`, `Gaps: 0`, `Termination: sigkill_sent_by_runner`, `Durability: crash`, `Sample timing: pre_kill=15 (late_received=0) within_tolerance=0 beyond_tolerance=0 after_container_stop=0 tolerance_ms=250 clock_offset_ms=9`.
- C: `Container: status=exited exit_code=137 oom_killed=true`, `Samples: 45`, `Gaps: 0`, `Termination: docker_oom_kill`, `Durability: crash`, `Diagnostics: rss_monotonic_tail=true peak_rss_ratio_of_limit=1.05 sample_count=45 (informational, not pass/fail)`.

## (e) Provenance

### 1. Console-reported pytest results (user transcripts)

- Sections (a) and (b), except the baseline row, and the Runner exit codes come from the user's console transcripts.
- Separate pytest log files for the default suite run and for the eight Docker module runs were searched for under `runs/` and not found. The only pytest log files from the 8.3 session are the baseline files `runs/t83_baseline_stdout.txt`, `runs/t83_baseline_stderr.txt` (empty) and `runs/t83_baseline_summary.txt`.
- No observation JSON, junit file or `runs/t83*` directory from the 8.3 session was found. The `runs/m07-*` observation files predate Task 8.3 and are not evidence for it.

### 2. Saved Runner artifacts independently read during this task

All files are gitignored and local. Checked read-only. SHA-256 values were identical at the start and the end of the task.

| File | SHA-256 |
|---|---|
| runs/b-20261120T211311Z-ce313da1/manifest.json | 3f8b66a11fbc0e21f0661bd5622bf005886f6d91f155b5bd386cbc810bd09c06 |
| runs/b-20261120T211311Z-ce313da1/evidence.jsonl | c6bc83a93fa56e4832022fa817e2e8efee071c0f7d649f63709376062ded9ba9 |
| runs/b-20261120T211311Z-ce313da1/docker_inspect.json | 2a45f805e8feda7910dfa16b20b8933a5a6bb6f664254f22a033e277d5bc3f0a |
| runs/c-20261120T211433Z-09b81ca1/manifest.json | 0324fc41bc1b1254f2b0906701c3da01c87bb81b096483448f7f788a8e2d1fc9 |
| runs/c-20261120T211433Z-09b81ca1/evidence.jsonl | 3d7ae9cfc73c9af5938481ade226bbcd0ebce56718e16e37fe65edb4f0d9206b |
| runs/c-20261120T211433Z-09b81ca1/docker_inspect.json | 0b4fe819692d93a13b91a1b2545303539f967ee17a9318b0794186334b71bafd |
| runs/t83_baseline_stdout.txt | 984594b5c48495b7802f9e5ccc0f816f4b7047d2f18ecc5c4de37ffe8d599878 |
| runs/t83_baseline_summary.txt | f8fa74d6a08f3243a8b012299a63a9034f56ef5b84c161e5a668919aee591f98 |

Each run directory holds exactly these three files. What was verified is listed in section (d). The Experiment A run from the same session, `a-20261120T211146Z-4300f395`, also exists locally (manifest outcome passed, manifest sha256 22bea488f10fc28632c26387091c8a26ce7ed1ef06a2cc18cd704af76fdd7e1f). It was not otherwise verified. No saved run directories for the ordinary-500 tests were found.

### 3. Previously recorded Docker observations (earlier tasks)

- The orchestrator's read-only check at HEAD 25c912e (no containers, five volumes present) and the earlier M0.5 and M0.7 acceptance records are carried over as recorded then. They were not repeated here.

### 4. Checks that cannot be repeated after container teardown

- Live `docker inspect`, `docker logs`, container mounts and published ports of the B and C containers are no longer available.
- The saved `docker_inspect.json` copy in each run directory is the only inspect evidence for those containers.

## (f) Limits of this record

- This record does not establish fresh-checkout reproducibility. That is Task 8.4.
- In Experiment C, peak RSS (141090816 bytes) is above the 134217728-byte limit. This is informational only, because RSS samples and cgroup accounting differ. The OOM verdict comes from Docker inspect (`OOMKilled` true, exit code 137).
