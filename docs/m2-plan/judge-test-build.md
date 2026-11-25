# Judge test-build plan

Status: plan, with one approved code file. It describes how a judge installs Coroner, runs the tests and sees the Viewer on their own machine. The user approved synthetic sample evidence (reviewer decision 3), so section 4 uses `scripts/make-sample-runs.py` (standard library only, no new dependency). The user did not approve publishing the real saved runs. Sources: `README.md` (sections 3.2, 3.3, 12 and the Viewer section), `requirements/dev.txt`, `coroner/viewer/cli.py`, `scripts/make-sample-runs.py`, `tests/unit/test_make_sample_runs.py`, `docs/m2-plan/m2-demo-readiness-plan.md`. Commands marked "run on the author's machine" or "TESTED" were executed while writing this plan, and the output shown is real. Commands marked "UNTESTED" or "not verified" were not run.

This plan supports two Kiroween submission items: a working project or reproducible test build, and clear install and test instructions.

## 1. What a judge gets and what they do not

- The repository contains the code, the tests, the pinned requirements and the docs. It does not contain `runs/`. `.gitignore` lists `runs/`, so the real A, B and C evidence is not in the repository.
- The Viewer is a local program. It binds only to `127.0.0.1`, accepts no other `--host`, and is never hosted, tunnelled or port forwarded for the submission. Every judge runs it on their own machine.
- No Docker is needed to install, test or view the sample in sections 2 to 4.

## 2. Install

Prerequisites: Python 3.12 or later (`pyproject.toml` has `requires-python = ">=3.12"`; 3.10 and 3.11 are not tested) and Git. Node and a Chrome-family browser are optional; see section 3.

Windows PowerShell, from the repository root (README section 3.2):

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# if script execution is blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
python -m pip install -r requirements/dev.txt
```

Linux and macOS, from the repository root (not verified: the README documents only Windows, and says native Linux hosts are not verified for M0; these are the standard equivalents):

```bash
python3.12 --version
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/dev.txt
```

Dependencies: `requirements/dev.txt` has 24 lines, every one an exact `==` pin (checked by `tests/unit/test_structure.py`). Nothing is added by this plan. `colorama` is a Windows-only transitive dependency of pytest and is harmless on Linux and macOS (comment in the file).

Expected result: `pip` ends with a "Successfully installed ..." line and exit code 0. A pip index is needed for this one step; the rest works offline.

## 3. Run the tests

Docker-free, network-free, does not start a server and does not read the real `runs/`. Collect first, then run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py tests/unit/test_docs.py -q -p no:cacheprovider -rs --collect-only
.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py tests/unit/test_docs.py -q -p no:cacheprovider -rs
```

Linux and macOS (not verified): the same commands with `python` from the activated venv in place of `.\.venv\Scripts\python.exe`, for example `python -m pytest tests/unit/viewer ... -rs`. Add `$LASTEXITCODE` (PowerShell) or `$?` and `echo $?` (shell) to check the exit code.

Expected output, run on the author's machine (Windows 11, Python 3.12.10, Chrome-family browser present, Node absent):

```text
1571 tests collected          (collect-only, exit code 0)
1568 passed, 3 skipped, 1 warning in 106.30s (0:01:46)     (exit code 0)
SKIPPED [1] tests\unit\viewer\test_compare_logic_node.py:65: node not on PATH ...
SKIPPED [2] tests\unit\viewer\test_files_containment.py:308: this account/platform cannot create symlinks ...
```

How to read a result on another machine:

- Passed plus skipped must equal collected. Exit code 0 is required. Exit code 5 (nothing collected) is a failure.
- The count of skips depends on the machine. The Node vector test skips without `node` on `PATH`. The symlink tests skip where the account cannot create symlinks (common on Windows without Developer Mode). The real-browser tests (`*_browser.py`) skip with the reason "no Chrome, Chromium or Edge found" unless a browser is found; `CORONER_BROWSER` points them at a specific executable. Every skip prints a reason with `-rs`. A skip is not a pass.
- The one warning is a Starlette `anyio.abc.BlockingPortal` DeprecationWarning from the pinned `starlette==0.41.3`. It is known and harmless.
- The run takes about two minutes on the author's machine with browser tests included. It will be shorter without a browser.
- Behaviour on Linux and macOS, and behaviour without any browser, were not verified here. If a test fails on a judge's platform, that is a finding to report, not a reason to edit the tests.

The default M0 suite (`python -m pytest -q`, README section 12) is also Docker-free and network-free. It was last recorded as 4402 passed, 5 skipped, 53 deselected at commit `c3c9428`, about 424 s (README section 12, historical). It was not rerun for this plan. Run it only if a judge wants the whole suite; counts change as tests are added. Never pass `--run-docker` and never set `CORONER_RUN_DOCKER=1`: those start containers and, for B and C, kill them (README section 12).

## 4. Judge quick-start: see the Viewer on synthetic sample runs

**The runs generated here are synthetic.** `scripts/make-sample-runs.py` writes three invented runs (A, B, C). No Docker container ran and nothing was measured. They are not real Docker experimental evidence, and they are not the author's saved runs. The real saved runs are not published: they stay on the author's machine (section 7 lists why). The user approved the synthetic sample only.

The generator uses only the Python standard library, so it needs no `pip install` beyond what the Viewer already needs. It is deterministic: the same script writes byte-identical files every time, with fixed run ids, fixed times and plainly patterned fake container ids.

| Run | Run id | ExitCode | OOMKilled | What it contains |
|---|---|---|---|---|
| A | `a-20260101T000000Z-sample01` | 1 | false | 15 Samples, controlled fatal exit |
| B | `b-20260101T000100Z-sample02` | 137 | false | 15 Samples, a Runner SIGKILL record, clock offset 9 ms (uncertainty 12.5 ms), so the corrected time axis and the derived intervals apply |
| C | `c-20260101T000200Z-sample03` | 137 | true | 45 Samples, RSS rising from about 44 MiB to about 134.5 MiB (peak 140,996,608 bytes, above the 128 MiB limit of 134,217,728 bytes) |

Each run directory holds `manifest.json`, `evidence.jsonl` and `docker_inspect.json`. The output root also holds `README-SYNTHETIC.txt`, and every manifest has a top-level `"synthetic"` block and a `termination_description` that starts with "SYNTHETIC". Path fields are placeholders (`/synthetic/...`).

Safety rules of the script:

- `--out` is required. There is no default and no overwrite option.
- The target must not exist or must be an empty directory. Otherwise it exits with code 2 and writes nothing.
- It refuses the repository's `runs/`, anything inside it, `<current directory>/runs`, a target that is a symlink or junction, and any path that resolves into `runs/`.
- It writes only ten files (the README and three per run), opened exclusively, and removes only its own files if a write fails midway.

Prerequisite: section 2 is done (venv with `requirements/dev.txt` installed), and the commands run from the repository root.

### Windows PowerShell (TESTED on the author's machine)

```powershell
$dir = Join-Path ([IO.Path]::GetTempPath()) ("coroner-judge-sample-" + [guid]::NewGuid().ToString('N').Substring(0,8))
.\.venv\Scripts\python.exe scripts\make-sample-runs.py --out $dir
.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir $dir --host 127.0.0.1 --port 8765
```

Run the third command in its own terminal window, or in the background. Output of the first command, as run on the author's machine (exit code 0; the temporary folder name differs on every machine and run):

```text
SYNTHETIC sample runs written (not real Docker evidence):
  a-20260101T000000Z-sample01
  b-20260101T000100Z-sample02
  c-20260101T000200Z-sample03
directory: <the temporary directory>
```

Expected first line of the Viewer (the label is the last folder name of the runs directory):

```text
Coroner Viewer: http://127.0.0.1:8765/  (runs: coroner-judge-sample-xxxxxxxx, read-only)
```

Then open these in a browser on the same machine:

- `http://127.0.0.1:8765/` (overview)
- `http://127.0.0.1:8765/runs/b-20260101T000100Z-sample02` (and the A and C ids)
- `http://127.0.0.1:8765/compare?a=a-20260101T000000Z-sample01&b=b-20260101T000100Z-sample02&c=c-20260101T000200Z-sample03`
- `http://127.0.0.1:8765/api/runs` (JSON list)

If port 8765 is taken, use another port and change every URL. Stop the Viewer with Ctrl+C, then delete the temporary directory.

Verification run on the author's machine (Windows 11, Python 3.12.10). The Viewer was started with `--port 51611` (a free port) from a process tool, then checked with `Invoke-WebRequest` and `Host: 127.0.0.1:51611`:

```text
/                                  -> 200
/api/runs                          -> 200
/compare?a=a-...-sample01&b=...    -> 200
/runs/b-20260101T000100Z-sample02  -> 200
/api/runs/c-20260101T000200Z-sample03 -> 200
total_found=3 listed=3 truncated=False
c-20260101T000200Z-sample03 verdict=PASS exit=137 oom=True  samples=45
b-20260101T000100Z-sample02 verdict=PASS exit=137 oom=False samples=15
a-20260101T000000Z-sample01 verdict=PASS exit=1   oom=False samples=15
Host: evil.example                 -> 421
```

Only the Viewer process that was started for this check was stopped afterwards, and its temporary directory was deleted. A headless-Chrome test (`tests/unit/test_make_sample_runs.py`) also shows the compare headline "The same exit code does not always mean the same failure" for these runs, with no console or CSP errors.

### Linux and macOS (UNTESTED: not run on these systems)

```bash
DIR="$(mktemp -d -t coroner-judge-sample-XXXXXXXX)"
python scripts/make-sample-runs.py --out "$DIR"
python -m coroner.viewer --runs-dir "$DIR" --host 127.0.0.1 --port 8765
```

`mktemp -d` creates an empty directory, which the script accepts. Use the `python` of the activated venv. The script and the tests use only portable standard-library calls, but no run was made on Linux or macOS.

### What the synthetic sample proves and does not prove

It proves that a judge can install the pinned dependencies, start the Viewer on loopback, open every page and API route, and see the exit-code and OOMKilled values the manifests and inspect files record, rendered by the committed code. The B and C values match the finding the project is about (both ExitCode 137; only C has OOMKilled true).

It does not prove anything about real failures. Every value is written by the generator, so the sample says nothing about Docker, the Runner, the Collector or the Probe. It does not show the real B versus C evidence, real RSS curves, real clock-offset values or the real wording from M0 runs. Do not present the sample as the experiments. Label it synthetic wherever it is shown.

The earlier inline builder snippet (`tests/unit/viewer/builder.py`) is no longer needed for this route. It stays in the repository as test code.

## 5. What the tests prove and do not prove

Prove:

- The Viewer's parsing, classification, clock labelling, HTTP guards (Host header, read-only, loopback only), cache rules, limits and static rules behave as the tests state, on synthetic inputs built by `builder.py`.
- The repository structure, pins (every requirement line is `==`) and README claims match what `test_structure.py` and `test_docs.py` check.
- With a browser present, the chart, compare and wording checks run in headless Chrome against synthetic runs.

Do not prove:

- Anything about real Docker behaviour. No test in this command reads the real `runs/`, starts Docker or uses the network. The real A, B and C results are recorded in `docs/m0-results/` and `README.md` section 13, from one Windows and Docker Desktop host. They are not reproduced by this command.
- Repeatability on other hosts, Linux or macOS (README status block).
- Human use. Normal-Chrome checks H1 to H7, 200% zoom and readability are NOT TESTED until a person records them (`docs/m1-results/m1.1-acceptance.md`, `docs/m2-plan/rehearsal-record.md` when it exists).
- JavaScript outside Chrome. The Node vector test skips without Node.

## 6. How judges can see the real A, B and C evidence: options

The real runs are `a-20261120T211146Z-4300f395`, `b-20261120T211311Z-ce313da1` and `c-20261120T211433Z-09b81ca1`. They are not in the repository.

| Option | What it is | For | Against |
|---|---|---|---|
| (a) Synthetic sample (approved) | Section 4. Generated on demand by the committed script `scripts/make-sample-runs.py`. Not committed as static sample files. | No privacy exposure. No Docker. Works on any host that runs the tests. Needs no release. | Not the real evidence. A reader could mistake it for it unless labelled. Committing generated files adds fixture data to the repository; generating on demand avoids that. |
| (b) Release asset with the real saved runs | A zip of the three run directories attached to a GitHub release. | Judges see the real B versus C result in the real Viewer. | The files contain personal data (section 7). Publishing the originals needs a privacy review. Redacting changes the bytes, so the copy is no longer the original evidence and its hashes differ from the local baseline; it would need an explicit "redacted copy" label. Public, permanent once released. |
| (c) Judge reruns the M0 experiments | README sections 3 to 8: Docker Desktop, build images, `CORONER_COMPOSE_PROJECT`, `python -m coroner.runner run a`, `b`, `c`. | Real evidence, produced by the judge, with no data from the author's machine. | Needs Docker Desktop, Linux containers and WSL2, a one-time image build, free ports 8080 and 8081, and Windows (native Linux is not verified). B sends a real SIGKILL and C causes a real OOM kill. New run ids and times, so values like RSS differ. Slow and fragile for a judge. Not the author's recorded evidence. |
| (d) Video and screenshots (supporting only) | The demo video, plus the existing headless screenshots in `docs/m1-results/browser-4.7/` and `browser-4.14/`. | Shows the real evidence with no new publication of raw files. | A recording is not the live Viewer and must be labelled so (plan section 9). Screenshots and video can also show paths; checked in task 1.3 for the committed screenshots. The video needs the same review. |

All four keep the Viewer local. None needs a public host.

## 7. Privacy findings from one real manifest (read-only)

Checked: `runs/b-20261120T211311Z-ce313da1/` (manifest 6,026 bytes, evidence 7,322 bytes, inspect 9,852 bytes). Read only; nothing under `runs/` was changed. Values are not reproduced here, only the field names and kinds. The other real runs were not opened and are expected to have the same shape.

Fields that could be sensitive, with the file where they appear:

| Kind | Field | File | Why it matters |
|---|---|---|---|
| Absolute local path with the Windows account name | `evidence.host_copy_path` | `manifest.json` | Reveals the user name and the repository location. |
| Same | `container_state.raw_inspect_path` and `docker_inspect_path` | `manifest.json` | Same. A line search found 3 manifest lines and 2 inspect lines containing the account name. |
| Same | `Config.Labels["com.docker.compose.project.config_files"]` and `["com.docker.compose.project.working_dir"]` | `docker_inspect.json` | Compose records the host paths it ran from. |
| Container id (64 hex) | `container_id`, `container_state.container_id`, `Id` | manifest, inspect, and evidence `sample.container_id` | Identifies one container on the author's Docker. Low risk once it is gone, but it is a machine identifier. |
| Container name and hostname | `Name` (`/coroner-t83-demo_app-1`), `Config.Hostname` (12 hex), `HostnamePath`, `ResolvConfPath`, `HostsPath`, `LogPath` | `docker_inspect.json` | Docker-internal names and paths under `/var/lib/docker/containers/...`. The Compose project name in the labels (`coroner-t83`) shows how the run was made. |
| Environment of the container | `Config.Env` | `docker_inspect.json` | In this run: Coroner variables, run id, collector URL, Python version and hash, the Python image's public GPG key id, `PATH`. No secrets were seen. A different environment could hold secrets; check each file before release. |
| Network | `NetworkSettings.Networks` (project network name; IP and MAC were empty in this stopped container) | `docker_inspect.json` | Empty here, but present as fields. |
| Mounts | `Mounts` (empty list here) | `docker_inspect.json` | Would show host paths if bind mounts were used. |
| Timestamps | start, end, clock offset, `server_time` | all | Not personal, but show when the experiment was run. |

Not found: tokens, keys, passwords, email addresses, or a host name of the author's computer (the container hostname is the Docker container's, not the Windows host's). The checks were by field inspection and a string search for the account name and `Users`; they were not a complete secret scan.

The committed repository is not affected by these files because `runs/` is ignored. It is affected by the docs and screenshots already committed, which task 1.3 scanned.

## 8. Recommendation and decisions

1. Use option (a) for the judge test build, through scripts/make-sample-runs.py (section 4). Label the sample as synthetic everywhere it is mentioned.
2. Use option (d) to show the real evidence: the demo video and the committed screenshots, which are already public after the push. Say in the README or submission text that the real `runs/` is not published and that the video shows the author's saved runs.
3. Do not publish the real runs (option (b)) unless you want judges to open the originals. If you do, redact or review first, and call the result a redacted copy.
4. Offer option (c) as an optional path for a judge who has Docker Desktop on Windows, with the README safety gates. Do not recommend it as the main route.

Decided by the user: option (a) with `scripts/make-sample-runs.py` is approved (reviewer decision 3). Publishing the real saved runs (option (b)) is not approved.

## 9. Rules that apply to every route

- The Viewer runs only on `127.0.0.1`. No tunnel, port forward, public host or hosted demo.
- No Docker for sections 2 to 4.
- Nothing is written under the repository's `runs/`. Sample and scratch output goes to a temporary directory outside the repository.
- No claim beyond the evidence: B has ExitCode 137 with OOMKilled false and C has ExitCode 137 with OOMKilled true, as recorded for the real runs; the synthetic sample does not establish either.
