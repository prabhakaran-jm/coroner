# Judging and testing Coroner

This page lets a judge install Coroner, view it on sample runs and run the tests on one machine. It needs no Docker and
no hosted service. The Viewer runs on your own computer and listens on `127.0.0.1` only.

**Read this first: the sample runs are synthetic.** `scripts/make-sample-runs.py` writes three invented runs (A, B, C).
No container ran and nothing was measured. They are not the real experiments and do not reproduce them. The real A, B and C
runs were recorded on one Windows host with Docker Desktop (see `README.md` section 13 and `docs/m0-results/`). The real
`runs/` directory is not published in this repository. The demo video is planned to show the real saved runs and is not yet
recorded.

Verified on Windows on 2026-11-25 with Python 3.12.10 (commands and output in the section "What was verified"). Linux and
macOS are UNTESTED. The rationale for this build is in [`docs/m2-plan/judge-test-build.md`](docs/m2-plan/judge-test-build.md).

## 1. Prerequisites

- Python 3.12 or later. The version used for every check here is 3.12.10. Python 3.10 and 3.11 are not tested
  (`requires-python = ">=3.12"` in `pyproject.toml`).
- Git, to clone the repository, and a pip index for the one install step.
- A browser on the same machine. Chrome was the only browser used in project checks.
- Not needed: Docker, Node, a hosted site.

## 2. Install (Windows PowerShell)

From the repository root:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# if script execution is blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
python -m pip install -r requirements/dev.txt
```

`requirements/dev.txt` pins every package with `==`. The Viewer and the sample generator use only the packages in that file
and the Python standard library.

## 3. Generate the synthetic sample runs

Pick a new or empty folder outside the repository. The script refuses the repository's `runs/` directory and refuses to
overwrite anything.

```powershell
$dir = Join-Path ([IO.Path]::GetTempPath()) ("coroner-judge-sample-" + [guid]::NewGuid().ToString('N').Substring(0,8))
.\.venv\Scripts\python.exe scripts\make-sample-runs.py --out $dir
```

Expected: exit code 0, three run ids and the directory path:

```text
SYNTHETIC sample runs written (not real Docker evidence):
  a-20260101T000000Z-sample01
  b-20260101T000100Z-sample02
  c-20260101T000200Z-sample03
```

## 4. Start the Viewer and open it

In a second PowerShell window (or as a background process), from the repository root:

```powershell
.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir $dir --host 127.0.0.1 --port 8765
```

(Use the same `$dir` value; in a new window, paste the folder path printed in step 3.) The first output line is
`Coroner Viewer: http://127.0.0.1:8765/  (runs: <folder name>, read-only)`.

Open `http://127.0.0.1:8765/` in a browser on the same machine. If port 8765 is taken, use another port with `--port`, for
example `8766`, and change every URL below.

| Page | URL |
|---|---|
| Overview | `http://127.0.0.1:8765/` |
| Run A | `http://127.0.0.1:8765/runs/a-20260101T000000Z-sample01` |
| Run B | `http://127.0.0.1:8765/runs/b-20260101T000100Z-sample02` |
| Run C | `http://127.0.0.1:8765/runs/c-20260101T000200Z-sample03` |
| Compare A, B and C | `http://127.0.0.1:8765/compare?a=a-20260101T000000Z-sample01&b=b-20260101T000100Z-sample02&c=c-20260101T000200Z-sample03` |
| JSON list | `http://127.0.0.1:8765/api/runs` |

## 5. Expected values

| Run | ExitCode | OOMKilled | Samples | Notes |
|---|---|---|---|---|
| A (`a-20260101T000000Z-sample01`) | 1 | false | 15 | Controlled fatal exit |
| B (`b-20260101T000100Z-sample02`) | 137 | false | 15 | A Runner SIGKILL record. Clock offset 9 ms, uncertainty 12.5 ms |
| C (`c-20260101T000200Z-sample03`) | 137 | true | 45 | Peak RSS 140,996,608 bytes, above the 128 MiB limit of 134,217,728 bytes |

`/api/runs` lists three runs (`total_found` 3, `truncated` false), each with verdict PASS.

The Compare page shows the headline "The same exit code does not always mean the same failure", with B and C both at
ExitCode 137 and different OOMKilled values. The page is drawn by JavaScript in the browser, so the server returns only an
HTML shell. The headline check was not repeated by hand in this verification. It is covered by the headless-Chrome test in
`tests/unit/test_make_sample_runs.py` (it skips when no Chrome-family browser is found).

## 6. Safe shutdown

- Press `Ctrl+C` in the window that runs the Viewer. The prompt returns.
- If you started it as a background process, stop only that process. Do not stop processes you cannot identify as your own
  Viewer.
- The Viewer never writes to the runs folder. Delete the temporary sample folder when you are done, if you wish.

## 7. Troubleshooting

| Symptom | What to do |
|---|---|
| Port already in use | Choose another port: `--port 8766`. Then change the port in every URL. This case was not tested here. |
| HTTP 421 | The `Host` header was not `127.0.0.1:<port>` or `localhost:<port>`. Open the page as `http://127.0.0.1:<port>/`, not through another name or a tunnel. A request with `Host: evil.example` returned 421 in the verification below. |
| `--host` rejected (exit code 2) | The Viewer accepts only `--host 127.0.0.1`. |
| Python version | Run `python --version`. It must print 3.12 or later. |
| PowerShell blocks `Activate.ps1` | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that window, then activate again. This changes only the current process. |
| `--out` refused (exit code 2) | The target must not exist, or must be an empty folder, and must not be inside `runs/`. Use a new folder. |
| Page shows "needs JavaScript" | Turn JavaScript on. The Viewer draws every page in the browser. |

## 8. Tests

All commands are Docker-free and network-free. Run them from the repository root with the venv's Python.

Focused judge tests (Viewer, structure, docs, preflight script, sample generator). Collect first:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py tests/unit/test_docs.py tests/unit/test_demo_preflight.py tests/unit/test_make_sample_runs.py -q -p no:cacheprovider -rs --collect-only
.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py tests/unit/test_docs.py tests/unit/test_demo_preflight.py tests/unit/test_make_sample_runs.py -q -p no:cacheprovider -rs
```

Whole Docker-free unit suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit -q -p no:cacheprovider -rs
```

Recorded results on Windows with Python 3.12.10:

- `tests/unit`: 5,962 collected, 5,950 passed, 0 failed, 12 skipped, exit code 0. This was a run on the PR #4 worktree,
  before the PR #3 additions.
- Viewer, structure, docs, preflight and generator tests (the five-path command above): 1,604 collected, 1,601 passed, 3
  skipped, exit code 0.
- A later whole-tree run in the author's main working copy had 1 failure caused by local CRLF line endings in that copy.
  No count from that run is cited.
- Clean-checkout whole-tree run on 2026-11-25 at commit `e2307a3e900766f62d175b9d07351cc9050ec85e` (a fresh detached git worktree with its own `.venv`, Windows, Python 3.12.10, pinned `requirements/dev.txt`): `tests/unit` 5,995 collected, 5,982 passed, 0 failed, 13 skipped, exit code 0, about 10 minutes. The 13 skips are not passes: 2 need POSIX `fcntl.flock`, 3 need symlinks this account cannot create, 1 needs directory fsync, 1 needs the POSIX `resource` module, 1 needs `node` (not installed), and 5 need the real `runs/` folder, which is not published. Docs-only commits after that commit change no code.
- Historical: an earlier clean checkout at `f076d6e` had 2 failures, caused by a CRLF `scripts/demo-preflight.ps1` and a worktree with no `.venv`. Commit `e2307a3` fixes the line endings through `.gitattributes`.

Read a result as follows: passed plus skipped equals collected, and the exit code is 0 (exit code 5 means nothing was
collected, which is a failure). Skips print a reason with `-rs`. A skip is not a pass. Typical skip reasons are `node` not
on `PATH`, an account that cannot create symlinks, or no Chrome-family browser for the browser tests (set `CORONER_BROWSER`
to point at one). Counts change as tests are added.

Do not pass `--run-docker` and do not set `CORONER_RUN_DOCKER=1`. Those options start containers and, for Experiments B and
C, kill them (`README.md` section 12).

What the tests do not show: anything about real Docker behaviour, repeat results on other hosts, or human use of the
Viewer. Human-operated Chrome, real 200% browser zoom and a screen reader are NOT TESTED
(`docs/m1-results/m1.1-acceptance.md`).

## 9. Platform status

| Platform | Status |
|---|---|
| Windows 11, PowerShell, Python 3.12.10 | TESTED with the commands above (2026-11-25) |
| Linux | UNTESTED |
| macOS | UNTESTED |

Linux and macOS equivalents (UNTESTED; standard venv commands, the same Python modules):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/dev.txt
DIR="$(mktemp -d -t coroner-judge-sample-XXXXXXXX)"
python scripts/make-sample-runs.py --out "$DIR"
python -m coroner.viewer --runs-dir "$DIR" --host 127.0.0.1 --port 8765
```

## 10. What was verified

Verified on Windows on 2026-11-25 with Python 3.12.10 (PowerShell, project virtual environment). The generator wrote to a
temporary folder outside the repository. The Viewer ran on port 8765 as a background process. Each URL was requested with
`Host: 127.0.0.1:8765`.

```text
generator exit code: 0 (10 files written: README-SYNTHETIC.txt and 3 files per run)
Viewer line: Coroner Viewer: http://127.0.0.1:8765/  (runs: coroner-judge-sample-<8 hex>, read-only)
GET /                                        -> 200 (HTML shell)
GET /api/runs                                -> 200
GET /compare?a=...&b=...&c=... (sample ids)  -> 200 (HTML shell)
GET /runs/b-20260101T000100Z-sample02        -> 200 (HTML shell)
GET /api/runs/c-20260101T000200Z-sample03    -> 200
/api/runs: total_found 3, listed 3, truncated false
c-20260101T000200Z-sample03  verdict PASS  exit_code 137  oom_killed true   sample_count 45
b-20260101T000100Z-sample02  verdict PASS  exit_code 137  oom_killed false  sample_count 15
a-20260101T000000Z-sample01  verdict PASS  exit_code 1    oom_killed false  sample_count 15
C highest RSS in evidence.jsonl and manifest: 140996608 bytes (limit 134217728)
GET / with Host: evil.example                -> 421
```

Only the Viewer started for this check was stopped. Afterwards nothing was listening on port 8765.
