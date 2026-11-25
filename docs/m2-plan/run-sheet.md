# Coroner demo run sheet

Target 2:45, strictly under 3:00. Live Viewer on `127.0.0.1` only, saved runs only, no Docker, nothing written under `runs/`. Sources: `docs/m2-plan/m2-demo-readiness-plan.md` (sections 4 to 8), `docs/m2-plan/startup-shutdown.md`, `docs/m2-plan/canonical-run-verification.md`. Wording below matches `coroner/viewer/static/copy.js`; read values from the screen and do not memorise numbers.

## Non-negotiable demo facts

- A: ExitCode 1, OOMKilled false.
- B: ExitCode 137, OOMKilled false. SIGKILL was sent by the Runner. Do not attribute B to Docker OOM.
- C: ExitCode 137, OOMKilled true. Docker recorded an OOM kill. Do not claim the underlying cause of memory growth is established.
- B's clock correction is derived and not independently verified. A and C use raw Probe Capture_Time. Never fabricate ordering across clocks.

## 1. Preflight (before every rehearsal and the live demo)

Script first (repository root, offline check, safe at any time):

```powershell
powershell -NoProfile -File scripts\demo-preflight.ps1 -Offline -BaselinePath "$env:TEMP\coroner-m2\demo-baseline.txt"
```

Expect exit code 0 (`$LASTEXITCODE`). With the Viewer running, run the same command without `-Offline` (full check: expect `RESULT: PASS`, 20 checks, as verified in `canonical-run-verification.md`).

Manual fallbacks (plan section 5):

1. `.\.venv\Scripts\python.exe --version` prints 3.12.x.
2. `Test-Path runs` is true and the three run directories below exist.
3. `Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue`. If something listens, identify it first. Stop only your own earlier `coroner.viewer`. At verification time another python process (PID 6940) held 8766; it is not ours, never stop it.
4. Start, in its own PowerShell window: `.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port 8765`. Expect `Coroner Viewer: http://127.0.0.1:8765/  (runs: runs, read-only)`.
5. Health: `/` and `/api/runs` return 200 and `/api/runs` lists nine runs.
6. After any code change: restart the Viewer and hard-reload (Ctrl+F5). Use a fresh Chrome profile or incognito window.
7. Stop with Ctrl+C in the Viewer window. Wi-Fi off once in rehearsal.

Fallback port: **8766** (`--port 8766`). Every URL below then uses 8766. Decide before the demo.

## 2. Canonical runs and URLs (port 8765)

| | Run id | URL |
|---|---|---|
| Overview | | `http://127.0.0.1:8765/` |
| A | `a-20261120T211146Z-4300f395` | `http://127.0.0.1:8765/runs/a-20261120T211146Z-4300f395` |
| B | `b-20261120T211311Z-ce313da1` | `http://127.0.0.1:8765/runs/b-20261120T211311Z-ce313da1` |
| C | `c-20261120T211433Z-09b81ca1` | `http://127.0.0.1:8765/runs/c-20261120T211433Z-09b81ca1` |
| Compare | | `http://127.0.0.1:8765/compare?a=a-20261120T211146Z-4300f395&b=b-20261120T211311Z-ce313da1&c=c-20261120T211433Z-09b81ca1` |

## 3. Verified values (`canonical-run-verification.md`)

| | A | B | C |
|---|---|---|---|
| ExitCode | 1 | 137 | 137 |
| OOMKilled | false | false | true |
| Memory limit | 134217728 bytes (128 MiB) | 134217728 | 134217728 |
| Peak RSS (bytes) | 74326016 | 74174464 | 141090816 |
| Preserved Samples | 15 | 15 | 45 |
| Time axis | raw Probe Capture_Time | derived host-equivalent time | raw Probe Capture_Time |

- B: the Runner recorded `SIGKILL sent by the runner (docker kill --signal=KILL)`. The Runner recorded `times.sigkill_sent_time` immediately before issuing `docker kill --signal=KILL`, and the page draws the SIGKILL marker there. The phase log stamp `sigkill_sent` was written after that command returned. Both are Host_Clock. The two stamps are 425 ms apart in the canonical B run; that gap does not measure when the signal reached the container. The signal's arrival in the container is not recorded. **Do not narrate an exact SIGKILL instant; say only what the page shows.**
- C: highest sampled RSS is above the 128 MiB limit (drawn at its true height). Sampling can miss growth after the last preserved Sample.

## 4. Demo order and per-screen timing (total 2:45)

| Clock | Screen | Say (matches the screen) | Point at |
|---|---|---|---|
| 0:00 to 0:15 (15 s) | Overview `/` | "Two containers can die with the same exit code. Coroner preserves and analyses evidence from container failures. This read-only Viewer starts no containers and does not modify saved runs. Here are three recorded experiments: A, B and C." | The three cards. |
| 0:15 to 0:30 (15 s) | A | "Experiment A exited with code 1, and Docker recorded OOMKilled false." Then move on. | ExitCode 1, OOMKilled false. |
| 0:30 to 1:20 (50 s) | B | "B: Docker ExitCode 137, OOMKilled false. The Runner sent SIGKILL; this is the Runner's description, not a Docker OOM." "The time axis reads *Derived host-equivalent time*; it is derived, not independently proven." | ExitCode, OOMKilled, "Termination as described by the Runner", the RSS timeline and its axis title and note, the SIGKILL marker as drawn, "Evidence vs Uncertainty". |
| 1:20 to 2:05 (45 s) | C | "C: Docker ExitCode 137, OOMKilled true. Docker records an out-of-memory kill: the immediate termination mechanism." "The underlying reason for the memory growth is not established by the recorded evidence." "Sampling can miss memory growth between preserved Samples and after the last preserved Sample." "The axis here is *Raw Probe Capture_Time*; no correction is applied." | ExitCode, OOMKilled, memory limit (128 MiB), the highest sampled RSS above the limit, the oomMechanism sentence, the sampling note, the axis title. |
| 2:05 to 2:45 (40 s) | Compare | Headline: "The same exit code does not always mean the same failure." "B and C share ExitCode 137; OOMKilled distinguishes them: false for B, true for C." Close with what Coroner does not claim (section 6). | Slot labels and run ids, ExitCode row, OOMKilled row, Memory limit row, headline, comparison statements, "What these values do not establish". |

Budget: 15 + 15 + 50 + 45 + 40 = 165 s = 2:45. If running long, shorten A first. Never cut the closing statement.

## 5. Headline condition

The headline appears only when B and C both have ExitCode 137 and OOMKilled differs. It is suppressed when an ExitCode or OOMKilled source discrepancy exists in either run (the reason is listed under "Statements not shown, and why"). If it is absent, say so and do not improvise the claim.

## 6. What Coroner refuses to claim (plan sections 7 and 8)

- Cause of B's SIGKILL beyond "the Runner sent it" (as the Runner describes it); no attribution to Docker OOM.
- The underlying reason for C's memory growth.
- Any ordering of Capture_Time (Container_Clock) against Host_Clock or Docker_Clock times: the page states it is not established.
- The exact peak: sampling can miss growth between Samples and after the last Sample.
- Clock synchronisation: the four clocks are not recorded as synchronised; the recorded Clock_Uncertainty does not bound the Docker_Clock or the Container_Clock.

## 7. Say / do not say

| Say | Do not say |
|---|---|
| "Docker recorded an OOM kill." (C) | "C proves a memory leak" or any cause of the growth. |
| "The Runner sent SIGKILL." (B) | "B was an OOM kill", or who sent the SIGKILL beyond the Runner. |
| "The Runner recorded the time just before it issued `docker kill --signal=KILL`; the marker is drawn there." (B) | "The marker shows when the signal arrived", or that the `sigkill_sent` phase time is the send time. |
| "One timestamp was recorded immediately before the Runner issued the kill command. Another was written after the command returned. They are 425 ms apart; that does not measure when the signal reached the container." (B, if asked) | "That the gap is the command duration or kill latency", or that the two recorded times are equal. |
| "Arrival of the signal in the container is not recorded." (B) | Any exact SIGKILL instant. |
| "Experiment A exited with code 1, and Docker recorded OOMKilled false." (A) | Any cause for A's exit, such as "application error". |
| "Derived, not independently proven." (B's axis and marker) | "B's time is corrected and verified." |
| "Raw Probe Capture_Time." (A and C) | "A and C are corrected." |
| "The order across clocks is not established." | "The sample came before the kill." Any cross-clock ordering. |
| "Read from the screen: ... MiB." | An exact SIGKILL instant, or numbers beyond what the page shows. |
| "This is a recording / a saved screenshot, not the live Viewer." (fallback) | Presenting a recording or screenshot as the live Viewer. |

## 8. If the live demo fails

1. Say it plainly and switch to the **backup recording** (Clipchamp, stored outside the repository, second copy on another device or drive). Announce: "This is a recording, not the live Viewer." Never present it as live.
   The synthetic sample (`docs/m2-plan/judge-test-build.md`, generated by `scripts/make-sample-runs.py`) is for judges and must never be shown as the real evidence in the demo.
2. Last resort only: the committed headless screenshots under `docs/m1-results/browser-4.7/` and `docs/m1-results/browser-4.14/`. Label them as automated screenshots, not live evidence.
3. Do not start Docker, re-run experiments or edit `runs/` to recover. Do not demonstrate stopped-server or 400, 404 or 500 states.

## 9. After the demo

Run the offline preflight command from section 1 (integrity check against the baseline) and `git status --short`; nothing under `runs/` may change.
