# Viewer startup and shutdown for the demo

This page covers starting and stopping the Coroner Viewer on the demo machine. All commands run from the repository root in Windows PowerShell or PowerShell 7. Source: `docs/m2-plan/m2-demo-readiness-plan.md` section 5, `coroner/viewer/cli.py` and `scripts/demo-preflight.ps1`.

## Rules for the demo

- No Docker. Do not run `docker`, `docker compose`, any image build, container start or stop, `/crash` or `/leak`. The saved runs are the evidence. Re-running experiments creates new runs and is a separate decision.
- Never write under `runs/`. The Viewer reads it only. Do not save logs, baselines or reports there.
- The Viewer binds only to `127.0.0.1`. `--host` accepts only the exact string `127.0.0.1`; anything else is rejected with exit code 2. Do not use a tunnel, port forward or public host.
- Do not stop any process you cannot identify as your own Viewer.

## 1. Run the preflight first

The baseline file lives outside the repository at `%TEMP%\coroner-m2\demo-baseline.txt`. It is read only and the script never writes it.

Offline check (no server needed, safe at any time):

```powershell
powershell -NoProfile -File scripts\demo-preflight.ps1 -Offline -BaselinePath "$env:TEMP\coroner-m2\demo-baseline.txt"
```

Parameters of `scripts/demo-preflight.ps1`:

| Parameter | Default | Meaning |
|---|---|---|
| `-RunsDir` | `runs` | Runs directory to check. |
| `-RunIds` | the three canonical ids (A `a-20261120T211146Z-4300f395`, B `b-20261120T211311Z-ce313da1`, C `c-20261120T211433Z-09b81ca1`) | Runs to check. |
| `-Port` | `8765` | Preferred port. |
| `-FallbackPort` | `8766` | Used when `-Port` is occupied. |
| `-BaselinePath` | none | Baseline file for the SHA-256 integrity check. Must resolve outside `runs/` and outside the repository. |
| `-Json` | off | Print one JSON object (`ok`, `checks[]`) instead of plain text. |
| `-Offline` | off | Skip every HTTP check and the Viewer launch. |
| `-LaunchViewer` | off | Start a Viewer, check it, then stop only that process. |

Exit code 0 means every check passed. Any failed check gives a nonzero exit code. Check `$LASTEXITCODE`.

Full check against a Viewer you started yourself (section 2): the script finds whichever of 8765 or 8766 is listening and runs the HTTP checks against it. It does not verify who owns that port, so confirm section 3 first.

```powershell
powershell -NoProfile -File scripts\demo-preflight.ps1 -BaselinePath "$env:TEMP\coroner-m2\demo-baseline.txt"
```

Full check where the script launches its own hidden Viewer on the first free port and stops it at the end (use it only when no Viewer of yours is running):

```powershell
powershell -NoProfile -File scripts\demo-preflight.ps1 -LaunchViewer -BaselinePath "$env:TEMP\coroner-m2\demo-baseline.txt"
```

Machine-readable output works with any of the above:

```powershell
powershell -NoProfile -File scripts\demo-preflight.ps1 -LaunchViewer -Json -BaselinePath "$env:TEMP\coroner-m2\demo-baseline.txt"
```

The script never stops a process it did not start. If both ports are occupied it reports the owner by name and PID and fails.

## 2. Start the Viewer

Open a new, separate PowerShell window for the Viewer and keep it open for the whole demo.

```powershell
.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port 8765
```

Defaults in `coroner/viewer/cli.py`: `--runs-dir runs`, `--host 127.0.0.1`, `--port 8765`. The port must be an integer from 1 to 65535. The runs directory must exist or the Viewer exits with code 2.

Expected start line:

```
Coroner Viewer: http://127.0.0.1:8765/  (runs: runs, read-only)
```

The window then stays busy. That is normal. Open `http://127.0.0.1:8765/` in Chrome.

### Fallback port 8766

If 8765 is occupied and you cannot or should not stop its owner (section 3), start on 8766:

```powershell
.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port 8766
```

Expected start line: `Coroner Viewer: http://127.0.0.1:8766/  (runs: runs, read-only)`.

Every URL changes: use `http://127.0.0.1:8766/`, `http://127.0.0.1:8766/runs/<id>` and `http://127.0.0.1:8766/compare?a=<A>&b=<B>&c=<C>`. Choose the port before the demo and write it on the run sheet.

## 3. Find a stale Viewer on the port

In M1.1 a stale process once held 8765. A stale Viewer serves old code and old static files, so check before launching.

1. List the listener on the port:

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
```

No output means the port is free; go to section 2. Otherwise note `OwningProcess` (the PID).

2. Identify that process by its command line:

```powershell
Get-CimInstance Win32_Process -Filter "ProcessId = <PID>" | Select-Object ProcessId, Name, CommandLine
```

(Replace `<PID>` with the `OwningProcess` value.)

3. Decide whether it is yours:

- It is your own earlier Viewer only if `CommandLine` contains `coroner.viewer` (typically `python -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port 8765`) and you started it, or it is from your earlier session of this project. If you are not sure you started it, treat it as not yours.
- If the command line is anything else, is empty, cannot be read, or you do not recognise it, do not stop it. Use the fallback port 8766 instead.
- A Viewer started from a virtual environment may show two python processes (a launcher and a child). Stop only the process you started, as in section 5, or press Ctrl+C in its window.

4. If it is your own `coroner.viewer` process, stop it (section 5), then confirm the port is free by repeating step 1, and start the Viewer again.

Never stop a process by name (for example every `python`). Never stop a process on the basis of the port alone.

## 4. Hard reload and stale static files

After any code change, stop the Viewer and start it again. Then hard-reload the browser with **Ctrl+F5** and confirm the page shows the expected current text. Do this also after starting a new Viewer on a port that Chrome has used before, because Chrome can show cached static files.

## 5. Shut down cleanly

Preferred: press **Ctrl+C** in the PowerShell window that launched the Viewer. Wait for the prompt to return.

If the window is gone or Ctrl+C does not work, and you know the PID of the Viewer you started (section 3, step 2 confirmed it is your `coroner.viewer` process):

```powershell
Stop-Process -Id <PID>
```

Use only the PID of your own Viewer. Do not add `-Force` unless that PID is confirmed as yours and a plain stop failed.

Confirm shutdown:

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
```

No output means the port is free. Repeat for 8766 if you used it.

## 6. Final check after the demo

The Viewer does not write to `runs/`. To confirm the evidence is unchanged, run the offline preflight (section 1, first command). The integrity check compares the SHA-256 and size of every file in the three canonical runs with `%TEMP%\coroner-m2\demo-baseline.txt`. Also run `git status --short` and confirm nothing under `runs/` is listed.
