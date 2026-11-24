# M2 demo-readiness plan

Status: plan only. Nothing here is implemented, and no result below is new evidence. Facts come from `README.md`, `docs/m0-results/`, `docs/m1-results/m1.1-acceptance.md` and the Viewer routes in `coroner/viewer/app.py`.

## 1. Goal

Present Coroner in a 3 to 5 minute live demonstration to hackathon judges, reliably, offline, from the saved runs in `runs/`. The point to land: three failures that look similar from outside are told apart by recorded evidence, and Coroner states what it observed and what it does not claim.

## 2. Scope and non-goals

In scope: a demo script, a startup preflight, a rehearsal with a human usability check, a backup plan, and fixes only for defects the rehearsal finds.

Non-goals:

- No new forensic features unless the demo cannot be given without them.
- No changes to M0 code (collector, runner, probe, evidence library).
- No new dependencies unless the reviewer approves them.
- No new experiment runs by default (see open decision 2). No Docker during the live demo.
- No claim beyond the recorded evidence.

## 3. What exists (grounding)

- Viewer routes: `/` (overview: cards A, B, C plus "Other saved runs"), `/runs/<run_id>` (incident detail), `/compare?a=<id>&b=<id>&c=<id>`, `/static/...`, `/api/runs`, `/api/runs/<run_id>`.
- Launch (from the repository root): `.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port 8765`. Start line printed: `Coroner Viewer: http://127.0.0.1:8765/  (runs: runs, read-only)`. Default port 8765 (`coroner/viewer/cli.py`).
- Saved runs used in M1.1 (`runs/` has nine runs, all Verdict PASS in the Task 4.8 record):
  - A: `a-20261120T211146Z-4300f395`
  - B: `b-20261120T211311Z-ce313da1`
  - C: `c-20261120T211433Z-09b81ca1`
  - Other runs: `a-20261120T133934Z-6d2f2350`, `a-20261120T134213Z-06deaa12`, `a-20261120T134252Z-b51dcf11`, `a-20261120T134321Z-a5e5a30e`, `b-20261120T173541Z-3fa52342`, `c-20261120T174958Z-94f4225c`.
- M1.1 status: ACCEPTED WITH DOCUMENTED EXCEPTIONS (see the last section of `docs/m1-results/m1.1-acceptance.md`). Human-operated checks are still open.
- Existing fallback images: headless Chrome screenshots in `docs/m1-results/browser-4.7/` (for example `01-overview-desktop.png`, `02-compare-desktop.png`, `06-experiment-b-chart.png`, `07-experiment-c-chart.png`) and `docs/m1-results/browser-4.14/`.

## 4. Judge-facing story (3 to 5 minutes)

The three incidents:

| | Cause | ExitCode | OOMKilled |
| --- | --- | --- | --- |
| A | application error | 1 | not the point of A |
| B | external SIGKILL | 137 | false |
| C | kernel OOM kill | 137 | true |

B and C share ExitCode 137 and differ in OOMKilled. Same exit code, different failure.

Script (times are targets; read the values from the screen, do not memorise numbers):

| Time | Page | Say | Point at |
| --- | --- | --- | --- |
| 0:00 to 0:30 | `/` | "Coroner reads saved runs. It does not run containers and does not write to `runs/`. Three incidents, A, B, C." | The three cards; the "read-only" start line in the terminal if visible. |
| 0:30 to 1:15 | `/runs/a-20261120T211146Z-4300f395` (optional; skip if short of time) | "A is an application error, exit code 1. Different from the other two." | Verdict, ExitCode 1. |
| 1:15 to 2:30 | `/runs/b-20261120T211311Z-ce313da1` | "B was killed from outside with SIGKILL. Exit code 137, OOMKilled false." | ExitCode, OOMKilled, "Termination as described by the Runner", RSS timeline, "Evidence vs Uncertainty". |
| 2:30 to 3:30 | `/runs/c-20261120T211433Z-09b81ca1` | "C was killed by the kernel for running out of memory. Exit code 137, OOMKilled true." | ExitCode, OOMKilled, memory limit, RSS timeline. |
| 3:30 to 4:30 | `/compare?a=a-20261120T211146Z-4300f395&b=b-20261120T211311Z-ce313da1&c=c-20261120T211433Z-09b81ca1` | "Put side by side: B and C have the same exit code, 137. Only OOMKilled differs." | The headline, then the statements below it. |
| 4:30 to 5:00 | stay on `/compare` | Close with what Coroner refuses to claim (section 7). | The Evidence vs Uncertainty text. |

The headline text is "The same exit code does not always mean the same failure" (`coroner/viewer/static/compare-logic.js`). It shows only when B and C both have ExitCode 137 and OOMKilled differs. If an ExitCode or OOMKilled source discrepancy exists in either run, the headline and the statements that depend on it are suppressed. If it does not appear in rehearsal, say so, do not improvise the claim, and check section 8.

## 5. Reliable local startup (preflight checklist)

Do this before every rehearsal and before the live demo. Record each result.

1. Venv: `.\.venv\Scripts\python.exe --version` prints Python 3.12.x.
2. `runs/` present: `Test-Path runs` is true and the three run directories above exist (list names only).
3. Port free: `Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue`. If a listener exists, identify the process before doing anything. Do not stop a process you cannot identify as your own earlier Viewer. In M1.1 a stale process once held 8765.
4. Launch (exact command): `.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port 8765`, in its own PowerShell window. Expect the start line above.
5. Health check: request `http://127.0.0.1:8765/` and `http://127.0.0.1:8765/api/runs`, for example `Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/api/runs | Select StatusCode`. Both must return 200, and `/api/runs` must list nine runs.
6. Fallback port: if 8765 is taken, use `--port 8766` and update every URL. Pick it before the demo and write it on the run sheet.
7. Stale server or static files: after any code change, stop the Viewer and start it again; hard-reload the browser (Ctrl+F5) and confirm the page shows the expected current text.
8. Stop: Ctrl+C in the Viewer window.
9. No Docker during the live demo. The saved runs are the evidence; re-running experiments would create new runs and is a separate decision (open decision 2).
10. Network-free: the Viewer binds 127.0.0.1 and serves its own static files. Confirm with the Wi-Fi off once during rehearsal.

## 6. Live B versus C demonstration

On `/compare`, point at, in order:

1. The three slot labels (A, B, C) and the run ids.
2. ExitCode: B 137, C 137.
3. OOMKilled: B false, C true.
4. The headline, only if the condition in section 4 holds.
5. The memory limit row, to show the OOM is relative to a recorded limit (read the value from the screen).

Then open `/runs/<B>` and `/runs/<C>` (or use the incident links) for the RSS timelines. Say what the chart shows and what the text under it says; do not describe the chart beyond that text.

## 7. Clock uncertainty

The Viewer shows several clocks that are not synchronised. How to say it:

- B shows a derived host-equivalent time for samples. Say: "derived, not independently proven". It is derived from the recorded clock offset and carries the recorded Clock_Uncertainty.
- A and C show raw Capture_Time. Say: "this is the time the probe recorded on its own clock".
- Point at the Clock_Uncertainty disclosure on the incident page and read it, do not paraphrase it away.
- Make no cross-clock ordering claim. The page states that the order of Capture_Time (container clock) against host and Docker times is not established. Do not say "the sample came before the kill".
- If asked "why not synchronise?": the clocks are recorded as separate and the offset is measured, not assumed (see `docs/m0-results/clock-offset.md`).

## 8. Observed evidence versus Runner interpretation

The incident page separates what the Runner recorded from what it concluded (the "Termination as described by the Runner" section, the "Evidence vs Uncertainty" section with KNOWN and UNKNOWN OR LIMITED lists, and the "recorded by the Runner" list).

How to say it aloud:

- "These are values Docker and the probe recorded." (ExitCode, OOMKilled, Samples, times.)
- "This is the Runner's description of the termination." Attribute it: "the Runner says".
- "This is what the evidence does not establish." Read one item from UNKNOWN OR LIMITED.
- Refuse out loud: Coroner does not say who sent the SIGKILL in B, and does not claim exact timing across clocks.

## 9. Backup recording plan

- During rehearsal, record a screen capture of the full script with the user's own tool (open decision 3). Store it outside `runs/` and outside the repository (for example a `Videos` folder), with the rehearsal date and Chrome version in the file name or a note beside it.
- Last resort: the headless screenshots in `docs/m1-results/browser-4.7/` and `browser-4.14/`.
- Say clearly if a fallback is used: "This is a recording / a saved screenshot, not the live Viewer." Fallbacks are not live evidence and must not be presented as a live run.
- Keep a copy of the recording on a second device or drive.

## 10. Final human usability check (during rehearsal)

This check also closes the open M1.1 human exceptions: H1 to H7, human Chrome 200% zoom and V28 readability. Screen reader is optional.

| Item | Detail |
| --- | --- |
| Who | The demo presenter operates Chrome. A second person observes and writes down what is seen. The presenter must not fill in the observer's notes. |
| What | H1 to H7 as defined in `docs/m1-results/m1.1-acceptance.md`; one pass at 200% browser zoom; V28 readability of overview, B, C and compare pages; optionally a screen reader pass (name the product used). |
| Where to record | A new file `docs/m2-plan/rehearsal-record.md`, plus a new appended section in `m1.1-acceptance.md` that links to it. Do not edit the earlier sections. |
| Required fields | Observer name, date (from the person, not inferred), Chrome version (from `chrome://version`), operating system, URL visited, per-check PASS / FAIL / NOT TESTED with a short observation, display resolution and zoom. |
| Rule | A check counts only if a person observed it. Anything else stays NOT TESTED. Earlier mis-attributed PASS sets stay withdrawn. |

## 11. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Port 8765 in use | Preflight step 3; fallback port 8766; identify before stopping any process. |
| Stale Viewer serving old static files | Restart the Viewer after code changes; Ctrl+F5; check a known current text. |
| CRLF regression in `README.md` (`core.autocrlf=true` may convert it on checkout or add) | `.gitattributes` has `README.md text eol=lf`; run `test_docs.py` after any git operation that touches README. |
| Browser cache shows an old page | Hard reload; use a fresh Chrome profile or incognito window for the demo. |
| Projector resolution or zoom | Rehearse on the actual display; test 100% and 200% zoom; narrow layouts were only checked in headless Chrome. |
| Network dependency | None expected; test once with the network off. |
| Headline absent | Check both runs show ExitCode 137 and OOMKilled differs; check for a source discrepancy note. |
| Live failure | Section 9 backup. |
| Unverified browser states (stopped server, 400/404/500 texts) | Do not demonstrate them; they are accepted exceptions. |

## 12. Proposed milestones

### M2.1 Preflight and launch script

- Deliverables: a one-page run sheet (`docs/m2-plan/run-sheet.md`) with the preflight checklist, exact commands, fallback port and URLs. A small PowerShell preflight script only if the reviewer approves it (open decision 5); otherwise manual steps.
- Acceptance: the checklist runs from a cold start on the demo machine and ends with both health checks returning 200 and `/api/runs` listing nine runs.
- Human checkpoint: presenter runs it once, records the result.

### M2.2 Script rehearsal and recording

- Deliverables: the final A/B/C script with URLs and spoken lines; a rehearsal with a timer; a screen recording stored outside `runs/` and outside the repository.
- Acceptance: the script fits 3 to 5 minutes; every page named loads; the headline appears on `/compare`; no statement in the script goes beyond the page text.
- Human checkpoint: presenter and one listener who has not seen Coroner; the listener says what B and C differ in.

### M2.3 Human usability check

- Deliverables: `docs/m2-plan/rehearsal-record.md` and an appended section in `m1.1-acceptance.md`.
- Acceptance: H1 to H7, 200% zoom and V28 each recorded as PASS, FAIL or NOT TESTED with observer, date, Chrome version and observations. Failures become a defect list.
- Human checkpoint: the observer signs off the record; the reviewer reads it.

### M2.4 Defect fixes (only if M2.3 finds any)

- Deliverables: minimal Viewer fixes for demo-blocking defects, with tests; no M0 changes.
- Acceptance: the affected check is repeated by a person and the test suite still passes; `runs/` unchanged against the baseline.
- Human checkpoint: reviewer approves each fix before M2.5.

### M2.5 Dress rehearsal and freeze

- Deliverables: a full run on the demo display and machine with network off; the final backup recording; a short list of questions judges may ask with factual answers drawn from the docs.
- Acceptance: preflight passes; the live run matches the script; the backup exists and plays.
- Human checkpoint: reviewer decides the demo is frozen; no changes after that except by a new decision.

## 13. Open decisions for the reviewer

1. Commit and merge M1.1 before starting M2, or keep M2 work on the same uncommitted branch? (No commit, push or merge has been authorised.)
2. Rerun experiments (new runs, needs Docker) or demo only the saved runs? This plan assumes saved runs only.
3. Which recording tool and where to store the recording.
4. Which screen reader, if any, for the optional check.
5. May a PowerShell preflight script be added to the repository?
6. May the spec text be updated for the `/compare` 400 body and for the chart axis and SIGKILL label wording (open since M1.1)?
7. Does the demo include A on screen, or only B and C plus the compare page?
8. Does the demo use the newest A, B, C runs listed above, or the older B and C pair (`b-20261120T173541Z-3fa52342`, `c-20261120T174958Z-94f4225c`)?
