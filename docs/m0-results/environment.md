# M0 Environment Verification (Task 1.1)

Recorded on 2026-11-14 (host clock, Windows, PowerShell). All values below are copied from commands that were actually run.

## Toolchain

| Command | Observed output |
|---|---|
| `git --version` | `git version 2.55.0.windows.5` |
| `python --version` | `Python 3.12.10` |
| `python -m pip --version` | `pip 25.0.1 from C:\Users\prabh\AppData\Local\Programs\Python\Python312\Lib\site-packages\pip (python 3.12)` |
| `where.exe python` | `C:\Users\prabh\AppData\Local\Programs\Python\Python312\python.exe` (first), `C:\Users\prabh\AppData\Local\Microsoft\WindowsApps\python.exe` (Store stub, second) |
| `docker --version` | `Docker version 29.9.3, build 5457199` |
| `docker compose version` | `Docker Compose version v5.7.0` |
| `docker info --format '{{.OSType}} {{.ServerVersion}}'` | `linux 29.9.3` |

Python 3.12.10 meets the design minimum of 3.11. The real interpreter is first on PATH. The Store stub is second and is not used.

An earlier attempt (before the environment was prepared) failed: `python` resolved to the Store stub, and `py` and `docker` were not found. That is superseded by the table above.

## Git

| Check | Observed |
|---|---|
| Branch before | `master` |
| `git branch --list feat/coroner-m0` | no output (branch did not exist) |
| Action | `git switch -c feat/coroner-m0` -> `Switched to a new branch 'feat/coroner-m0'` |
| `git branch --show-current` | `feat/coroner-m0` |
| `git remote -v` | `origin https://github.com/prabhakaran-jm/coroner.git (fetch)` and `(push)` |
| `git status --short` (before edit) | `?? .kiro/` |
| `git status --short` (after edit) | ` M .gitignore` and `?? .kiro/` |

No commit, push, stash, clean, or reset was run. Remote visibility (private) is not checked in this task; task 1.11 does that.

## .gitignore

`.kiro/` was not matched by any rule. Existing patterns that could hide planned files: `build/`, `lib/`, `lib64/`, `*.manifest`, `*.spec`, `*.log`, `.env`, `.envrc`, `.venv`. Consequences for later tasks: run manifests are named `manifest.json`, and no planned directory is named `lib/` or `build/`.

Added at the end of `.gitignore`:

```
# Coroner M0
runs/
.venv*/
spike/
```

## Ignore checks

| Command | Exit code | Result |
|---|---|---|
| `git check-ignore -v .kiro/specs/coroner-m0/tasks.md` | 1 | not ignored (expected) |
| `git check-ignore -v runs/x/manifest.json` | 0 | `.gitignore:223:runs/` |
| `git check-ignore -v .venv-verify/x` | 0 | `.gitignore:224:.venv*/` |
| `git check-ignore -v spike/x` | 0 | `.gitignore:225:spike/` |
| `git check-ignore -v docs/m0-results/environment.md` | 1 | not ignored (expected, this file is tracked) |

## Result

PASS. Docker Desktop was reachable (`docker info` returned `linux 29.9.3`). Base-image availability and the Docker memory-limit behaviour are not checked here; task 1.4 does that.