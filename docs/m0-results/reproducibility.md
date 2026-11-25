# M0.8 — Task 8.4 Clean-Checkout Reproducibility

Date: 20 November 2026

## 1. Verdict

PASS — All three experiments reproduced successfully from a fresh checkout using independently built Docker images.

The verification was performed on the same Windows/Docker Desktop host. It does not establish reproducibility on another host or operating system.

## 2. Source checkout

- Repository: `prabhakaran-jm/coroner`
- Commit: `f87738c1522068046ca1beec63319db857774782`
- Clone: `%USERPROFILE%\Projects\coroner-repro`
- Git state: detached HEAD at the specified commit
- Python: 3.12.10
- Virtual environment: `.venv-repro`
- Dependencies: installed from `requirements/dev.txt`
- `pip check`: no broken requirements

## 3. Docker isolation

Compose project: `coroner-repro`

Environment:

- `COMPOSE_PROJECT_NAME=coroner-repro`
- `CORONER_COMPOSE_PROJECT=coroner-repro`
- `CORONER_COLLECTOR_PORT=18081`
- `CORONER_DEMO_PORT=18080`

The fresh checkout used an untracked `compose.override.yaml` with dedicated `:repro` image tags. Automatic override resolution matched explicit two-file Compose resolution.

The images were built with `docker compose -p coroner-repro build --no-cache`, without `--pull`.

New image IDs:

- Collector `:repro`: `sha256:a144d4b7a4910ca598b8811ac030fcc70ad507fc0ea195296b35d33527238f41`
- Demo App `:repro`: `sha256:16edf1d048f552d07264f333c69fc52da38a0fa71a9f5ad18116ec5cda76066d`

Original `:m0` image IDs remained unchanged:

- Collector: `sha256:cd9126e565333da7b76cb3f16ad494c94e2bcc9102e0cc8980e64eb55bf0420e`
- Demo App: `sha256:23571625f7319525505816774be12f752854b04608b9b0912ef9d1e1b5ca2c52`

The reproduction used Linux containers with cgroup v2 and a 128 MiB Demo App memory and swap limit.

## 4. Experiment results

| Experiment | Run ID | ExitCode | OOMKilled | Runner result |
|---|---|---:|---|---|
| A — Controlled fatal exit | `a-20261120T213011Z-1295a7ac` | 1 | false | PASSED |
| B — SIGKILL | `b-20261120T213157Z-0a283f3e` | 137 | false | PASSED |
| C — Genuine Docker OOM | `c-20261120T213329Z-31e32d02` | 137 | true | PASSED |

All three Runner processes returned exit code 0.

Each experiment was executed once in sequence, with a Collector health check before execution and independent Docker inspection before teardown.

After each termination, the Collector responded successfully with `storage_ok=true`.

## 5. Saved artifact verification

Each reproduction run contains:

- `manifest.json`
- `evidence.jsonl`
- `docker_inspect.json`

The subsequent read-only PowerShell verification confirmed for all three runs:

- Manifest run ID matched the expected ID.
- Manifest outcome was `passed`.
- Manifest failure was null.
- Manifest container ID matched the saved Docker inspection.
- Docker status was `exited`.
- Docker ExitCode and OOMKilled matched the experiment expectations.
- Memory and swap limits were both 134217728 bytes.
- Evidence file existed and was non-empty.

The verification did not independently recount Sample sequences, compute artifact hashes or validate every evidence record.

The raw artifacts remain in the reproduction clone under `runs/`.

## 6. Evidence preservation

The reproduction project created and retained:

`coroner-repro_coroner_evidence`

The five pre-existing evidence volumes also remained present:

- `coroner_coroner_evidence`
- `coroner-spike_coroner_spike_evidence`
- `coroner-m07_coroner_evidence`
- `coroner-t83-m05_coroner_evidence`
- `coroner-t83_coroner_evidence`

After the final experiment, no Docker containers remained.

Compose teardown was performed without `-v`.

The reproduction clone, virtual environment, untracked override, run artifacts, images and named volume were retained.

## 7. Limitations

- Reproduction occurred on the same Windows/Docker Desktop host, not a second machine.
- This establishes successful fresh-checkout reproduction, not a statistical reliability guarantee.
- Process RSS and Docker cgroup memory accounting can differ.
- Sampling at 500 ms can miss memory growth immediately before termination.
- All three runs were independently inspected before teardown, but the transient containers no longer exist.
- The reproduction was not a rerun of the full pytest suite.

## 8. Acceptance

Task 8.4 execution and artifact verification: PASS.

All three experiments reproduced their expected termination states from a fresh checkout using newly built isolated images.

Final task tracking and Git preparation remain separate activities.