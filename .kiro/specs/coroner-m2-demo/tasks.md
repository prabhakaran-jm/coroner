# Implementation Plan: Coroner M2 (Demo Readiness)

## Overview

This task list is derived from `docs/m2-plan/m2-demo-readiness-plan.md` (the source of truth) plus the reviewer decisions recorded below. It adds no new planning content. There is no `requirements.md` or `design.md` for this spec; `_Plan: §N_` references point to sections of the plan. The implementation language of the one code deliverable (the preflight script) is PowerShell; its test is Python 3.12 with pytest.

| Group | Top-level task | Content |
|---|---|---|
| M1.1 Git preparation | 1 | Review, scan, regression run, stage, commit, push, PR; merge as a separate approval-gated task |
| M2.1 Preflight and run sheet | 2 | Preflight script, its test, startup and shutdown docs, canonical run verification, run sheet, judge test-build plan |
| M2.2 Rehearsal and backup recording | 3 | Screen sequence, fallback list, human rehearsal and recording |
| M2.3 Human usability | 4 | Normal-Chrome checklist, 200% zoom, readability, honest records |
| M2.4 Defect fixes | 5 | Only defects found in rehearsal |
| M2.5 Dress rehearsal and freeze | 6 | Final rehearsal, freeze, submission checklist |

Conventions:

- **Files** lists files a task creates or modifies (paths from the repository root `<repo root>`). **Verify** gives PowerShell commands run from the repository root. **Safety** lists what the task must not do.
- Tasks marked **HUMAN-ONLY** are performed and observed by a person. An agent never records an expected result as an observed result and never performs them.
- Only the NVDA task (4.3) is optional (`*`), as stated in the plan (§10, open decision 4).
- Documents created by this spec live in `docs/m2-plan/`. Recordings are stored outside `runs/` and outside the repository.

## Reviewer Decisions Encoded

1. Git: group 1 comes before any M2.1 work. The merge is its own task and needs explicit user approval at execution time.
2. Evidence: saved A/B/C runs only. No Docker experiments (plan open decision 2).
3. Recording: Clipchamp for screen recording and editing, ElevenLabs optional narration. Target 2:45, strictly under 3:00.
4. NVDA is optional. Never claim it was tested without actual human verification.
5. Preflight: a read-only PowerShell script is approved (plan open decision 5), suggested path `scripts/demo-preflight.ps1`. The plan names no path, so the path is a suggestion; task 2.1 confirms it with the user before creating the file.
6. Demo content: A briefly, then B and C and the comparison (plan open decision 7).
7. Canonical runs (plan open decision 8, newest set): A `a-20261120T211146Z-4300f395`, B `b-20261120T211311Z-ce313da1`, C `c-20261120T211433Z-09b81ca1`.

## Non-Negotiable Demo Facts

These statements are used verbatim in the run sheet (task 2.6) and in every script and write-up.

- A: ExitCode 1, OOMKilled false.
- B: ExitCode 137, OOMKilled false. SIGKILL was sent by the Runner. Do not attribute B to Docker OOM.
- C: ExitCode 137, OOMKilled true. Docker recorded an OOM kill. Do not claim the underlying cause of memory growth is established.
- B's clock correction is derived and not independently verified. A and C use raw Probe Capture_Time. Never fabricate ordering across clocks.

## Global Rules

These rules apply to every task and are part of the completion criteria.

1. **M1.1 implementation is frozen** except for defects approved by the user (group 5).
2. **Preserve the real `runs/` directory and its baseline.** Never modify, rename, delete or add a file under `runs/`. Never write a baseline, report or log into `runs/`. The existing M1 snapshot (`$env:TEMP\coroner-m1\runs-before.snapshot.txt`) is never overwritten, appended to or deleted.
3. **No Docker, of any kind.** No `docker`, `docker compose`, image build, container start or stop, SIGKILL, `/crash`, `/leak` or volume command. The six protected volumes named in `.kiro/specs/coroner-m1-ui/tasks.md` Execution Rule 4 are never touched.
4. **No M0 production changes.** No change to `coroner/` outside `coroner/viewer/` and none to the evidence schema, Dockerfiles, `docker/`, `compose.yaml` or `requirements/*.txt`.
5. **No new dependencies without review.** Stop and ask.
6. **No claim of human testing without observations.** A human check counts only if a named person observed it. Otherwise it stays NOT TESTED.
7. **No commit or push beyond group 1 without approval.** Task 6.6 is the only later commit task and is approval-gated. Never run `git add -A` or `git add .`. Never modify git config. Never force push, `reset --hard`, `clean` or `stash -u`.
8. **File edits use the file-edit tools** (LF endings, no BOM). PowerShell may write scratch output only outside the repository.
9. **Pytest rule.** Use `.\.venv\Scripts\python.exe -m pytest <paths> -q -p no:cacheprovider -rs`, run `--collect-only -q` first, record `$LASTEXITCODE` and the real collected, passed, failed, skipped and deselected counts. Exit code 5 is a failure. A skip is never reported as a pass. Never pass `--run-docker`.
10. **No unverified claims.** A task is ticked only after its Verify commands ran and real output was recorded. Checkpoints report real results.
11. **Never make the local viewer publicly reachable.** It binds only `127.0.0.1`. No tunnel, port forward or public host.
12. **Long-running processes** (the Viewer) are started with the process tool or by the user, never in a blocking shell call, and are stopped when the check ends.
13. **Checkpoints stop for review.** The next group does not start until the user says to continue.

## Tasks

- [x] 1. M1.1 Git preparation
  - [x] 1.1 Record the starting state
    - Run `git branch --show-current` (expected `feat/coroner-forensic-ui`), `git rev-parse --short HEAD` (expected `618be05`), `git status --short`, `git remote -v`, `git log --oneline -3`. Record the output.
    - Safety: read-only git commands only. If branch or HEAD differs, STOP and ask.
    - Verify: the recorded output matches the expected branch and HEAD.
    - _Plan: §13 decision 1_

  - [x] 1.2 Final diff review of the uncommitted M1.1 work
    - Review `git diff --stat` and `git diff` for `coroner/viewer/`, `tests/unit/viewer/`, `tests/unit/test_structure.py`, `README.md`, `.gitattributes`, `.kiro/specs/coroner-m1-ui/`, `docs/m1-results/`, `docs/m2-plan/`. Review untracked files with `git status --short` and read each new file name against the intended list.
    - List any changed or untracked path outside the intended list and ask the user before including it.
    - Confirm no M0 file changed (`coroner/` outside `coroner/viewer/`, `docker/`, `compose.yaml`, `requirements/`).
    - Safety: do not edit any file in this task; findings go to the user.
    - Verify: a written list of reviewed paths, with "intended" or "ask" per path.
    - _Plan: §3, §13 decision 1_

  - [x] 1.3 Secrets and privacy scan
    - Scan the intended files for tokens and keys (patterns such as `ghp_`, `github_pat_`, `AKIA`, `sk-`, `-----BEGIN`, `api_key`, `secret`, `password`, `token`), for absolute user paths (`C:\\Users\\<username>`, `C:/Users/<username>`, `<username>`) in committed docs, and for email addresses. Use `grep_search` or `Select-String`.
    - Inspect PNG screenshots under `docs/m1-results/browser-4.7/` and `docs/m1-results/browser-4.14/` for embedded text metadata containing user paths (read the text chunks with `.\.venv\Scripts\python.exe -c` and the standard library only) and visually check for visible paths or emails.
    - Report each hit with file and line. Do not redact or edit files without the user's decision. Existing M1 documents may reference the repository root; list them for the user to approve or fix.
    - Safety: read-only. Do not print secret values in full; show only the location and the first 4 characters.
    - Verify: scan output recorded; every hit is classified "accepted by user" or "fix required".
    - _Plan: §9_

  - [x] 1.4 Ignored-file review
    - Run `git check-ignore -v runs/x/manifest.json .hypothesis/x __pycache__/x.pyc` and confirm each is ignored. Run `git ls-files runs` (no output expected). Run `git status --short --ignored` and review the ignored list.
    - Confirm that `runs/`, `%TEMP%` artefacts, `.hypothesis/`, `__pycache__`, Chrome profiles and the M1 snapshot are never staged and are not under the repository.
    - Safety: no deletion of ignored files; do not edit `.gitignore` without asking.
    - Verify: `git ls-files runs` prints nothing and `git check-ignore` prints a rule for each path.
    - _Plan: §11_

  - [x] 1.5 Focused regression run
    - Run `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py tests/unit/test_docs.py -q -p no:cacheprovider -rs --collect-only` and record the collected count. Then run the same command without `--collect-only`.
    - Record `$LASTEXITCODE`, the counts and every skip reason (for example the Node vector test when `node` is missing). Do not run the full suite and never pass `--run-docker`.
    - If a test fails, STOP and report. Do not fix M1.1 code in this task.
    - Verify: exit code 0, passed plus skipped equals collected, no exit code 5.
    - _Plan: §11 (CRLF regression risk)_

  - [x] 1.6 Stage only the intended files by explicit path
    - Stage with explicit paths only: `git add -- coroner/viewer/ tests/unit/viewer/ tests/unit/test_structure.py README.md .gitattributes .kiro/specs/coroner-m1-ui/ docs/m1-results/ docs/m2-plan/`. Add `.kiro/specs/coroner-m2-demo/` only if the user approves it in this task.
    - Never run `git add -A` or `git add .`.
    - Run `git diff --cached --stat` and `git diff --cached --name-only`. Confirm that no path starts with `runs/`, and that no `.hypothesis/`, `__pycache__`, snapshot or profile path is staged: `git diff --cached --name-only | Select-String '^runs/|\.hypothesis|__pycache__'` prints nothing.
    - Safety: if an unexpected path is staged, unstage it with `git restore --staged -- <path>` (this does not touch the working tree).
    - Verify: the staged list equals the intended list.
    - _Plan: §11_

  - [x] 1.7 Commit
    - Run `git commit -m "feat(viewer): M1.1 forensic viewer with acceptance records and M2 demo-readiness plan"`. Propose the message to the user first if they have a preferred style (check `git log --oneline -10`).
    - Safety: no `--amend`, no `--no-verify`, no git config change. If a hook fails, fix the cause and create a new commit.
    - Verify: `git status --short` shows no intended file left uncommitted and `git log --oneline -1` shows the new commit.
    - _Plan: §13 decision 1_

  - [x] 1.8 Push to origin
    - Run `git push origin feat/coroner-forensic-ui` (no force flag, no `--force-with-lease`). Before pushing, show the user `git log origin/feat/coroner-forensic-ui..HEAD --oneline` if the remote branch exists.
    - Safety: if the push is rejected, STOP and report; do not force.
    - Verify: push output recorded; `git status -sb` shows the branch up to date with origin.
    - _Plan: §13 decision 1_

  - [x] 1.9 Prepare the pull request to master
    - Check `gh --version` and `gh auth status`. If both succeed, create the PR with `gh pr create --base master --head feat/coroner-forensic-ui --title "<title>" --body "<body>"` only after the user approves the title and body.
    - If `gh` is missing or not authenticated, record the exact PR title, the exact PR body (what M1.1 adds, test results from task 1.5, the preserved limitations in `docs/m1-results/m1.1-acceptance.md`, and a note that human checks H1 to H7 remain NOT TESTED) and the compare URL `https://github.com/<owner>/<repo>/compare/master...feat/coroner-forensic-ui` (derive owner and repository from `git remote -v`). Give them to the user to open manually.
    - Safety: no login or token handling by the agent; no merge in this task.
    - Verify: a PR URL, or the title, body and compare URL, recorded.
    - _Plan: §13 decision 1_

  - [x] 1.10 Merge to master after a separate PR review (requires explicit user approval at execution time)
    - Review the PR diff separately from task 1.2 (for example `gh pr diff` or the compare page) and confirm the PR checks passed (`gh pr checks`, or the user's confirmation if checks are not available). Report the result.
    - Ask the user for explicit approval to merge now. Merge only after that approval, using the merge method the user names. Without approval at execution time, leave this task unticked.
    - Safety: no force push, no branch deletion, no git config change. Do not merge if any check failed or was not run.
    - Verify: the merged state is shown on the PR page or by `git log origin/master --oneline -3`.
    - _Plan: §13 decision 1_

  - [x] 1.11 Checkpoint - stop for review (M1.1 Git preparation)
    - Report real results: commit id, push result, PR link or text, merge status, scan findings, regression counts.
    - Stop and wait for the user's decision before group 2.

- [x] 2. M2.1 Preflight and run sheet
  - [x] 2.1 Create the read-only preflight script `scripts/demo-preflight.ps1`
    - Confirm the path with the user (suggested `scripts/demo-preflight.ps1`; the plan names no path). Run on Windows PowerShell 5.1 and PowerShell 7.
    - Parameters: `-RunsDir` (default `runs`), `-RunIds` (default the three canonical ids), `-Port` (default 8765), `-FallbackPort` (default 8766), `-BaselinePath` (required for the integrity check, must resolve outside `runs/` and outside the repository), `-Json` (JSON output), `-Offline` (skip every HTTP check and the Viewer launch, for the test), `-LaunchViewer` (switch, off by default).
    - Checks, each reported as PASS or FAIL with a message: (1) `.venv\Scripts\python.exe --version` is 3.12.x; (2) `import coroner.viewer` succeeds; (3) each canonical run directory exists and holds `manifest.json`, `evidence.jsonl` and `docker_inspect.json`; (4) port 8765 is free, otherwise 8766 is free, and the process owning an occupied port is reported by name and id but never stopped; (5) with `-LaunchViewer` only, start `python -m coroner.viewer --runs-dir <RunsDir> --host 127.0.0.1 --port <port>` and remember its process id, and stop only that process at the end; (6) HTTP checks with `Host: 127.0.0.1:<port>` for `/`, `/api/runs`, `/runs/<id>` for the three ids and `/compare?a=<A>&b=<B>&c=<C>`, each expecting 200; (7) from `/api/runs/<id>` read the recorded ExitCode and OOMKilled and compare with the Non-Negotiable Demo Facts (A 1/false, B 137/false, C 137/true); (8) evidence integrity: SHA-256 of every file in the three canonical run directories compared with the baseline file.
    - The script never writes under `runs/`, never writes the baseline file, never runs Docker, never stops a process it did not start, and adds no dependency (built-in cmdlets and `System.Net` only). It exits 0 only if every check passed and exits nonzero on any failed check. Output is plain text by default and a JSON object with a `checks` array and an `ok` boolean with `-Json`.
    - Files: `scripts/demo-preflight.ps1`.
    - Verify: `powershell -NoProfile -File scripts\demo-preflight.ps1 -Offline -BaselinePath <path outside runs> -Json` runs; a deliberately wrong `-RunsDir` returns a nonzero exit code (check `$LASTEXITCODE`). Do not start a server in this task.
    - Safety: read-only; no Docker; no process termination other than a Viewer the script itself launched.
    - _Plan: §5, §12 M2.1, §13 decision 5_

  - [x] 2.2 Create the evidence baseline outside `runs/`
    - Create one baseline file with the SHA-256 and size of every file in the three canonical run directories (relative path, hash, size), at a location outside `runs/` and outside the repository (suggested `$env:TEMP\coroner-m2\demo-baseline.txt`). Use create-new semantics: refuse if the file exists, then mark it read-only. Copy it to a second location outside the repository and note the location.
    - Cross-check the values against the existing M1 snapshot (`$env:TEMP\coroner-m1\runs-before.snapshot.txt`) for the same files; report any difference and STOP. Read the M1 snapshot only.
    - Do not commit the baseline. Record in the task note only the file name, the file count and the aggregate SHA-256, not the per-file listing.
    - Safety: no write under `runs/`; never overwrite an existing baseline.
    - Verify: `Test-Path` is true, `IsReadOnly` is true, `git status --short` shows no baseline file.
    - _Plan: §12 M2.4 (`runs/` unchanged against the baseline)_

  - [x] 2.3 Write a test for the preflight script
    - Create `tests/unit/test_demo_preflight.py`. The test builds a synthetic runs directory in `tmp_path` with the existing fixture builders in `tests/unit/viewer/builder.py`, builds a baseline file in `tmp_path`, and runs the script with `-Offline -Json -RunsDir <tmp> -RunIds <synthetic ids> -BaselinePath <tmp baseline>`. Cases: all checks pass (exit code 0, `ok` true); a modified evidence file fails the integrity check (nonzero); a missing run directory fails (nonzero); the script leaves `runs`-style directory contents unchanged (hash before equals hash after).
    - The test skips cleanly with a stated reason when neither `pwsh` nor `powershell` is on `PATH` (`shutil.which`). It never reads the real `runs/`, never starts a server and never calls Docker.
    - Check `tests/unit/test_structure.py` for a top-level directory or file allowlist and update it only if the new `scripts/` directory breaks it (report the change to the user first).
    - Files: `tests/unit/test_demo_preflight.py`.
    - Verify: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_demo_preflight.py --collect-only -q -p no:cacheprovider`, then the run with `-rs`. Record counts and `$LASTEXITCODE`; skips are reported as skips.
    - _Plan: §5, §12 M2.1_

  - [x] 2.4 Document startup and shutdown
    - Create `docs/m2-plan/startup-shutdown.md`: exact start command `.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port 8765` in its own PowerShell window, the expected start line `Coroner Viewer: http://127.0.0.1:8765/  (runs: runs, read-only)`, fallback port 8766 (and that every URL changes), Ctrl+F5 after any code change, stop with Ctrl+C in the Viewer window, how to identify a stale process on 8765 without stopping anything you cannot identify as your own Viewer, how to run the preflight script (with and without `-LaunchViewer`, with `-Json`), and the statement that no Docker is used.
    - Files: `docs/m2-plan/startup-shutdown.md`.
    - Verify: every command in the document is copied from the plan, `coroner/viewer/cli.py` or the script's own `-?` output; list each command checked.
    - Safety: documentation only; no server is started.
    - _Plan: §5_

  - [x] 2.5 Verify the canonical runs against the real `runs/` (read-only)
    - With the user's agreement, start the Viewer once on port 8765 (or 8766 if occupied) via the process tool, run `scripts/demo-preflight.ps1 -BaselinePath <baseline from 2.2>` and then with `-Json`, and stop the Viewer.
    - Record the real output. Expected: all checks PASS, the facts of the Non-Negotiable Demo Facts match, the integrity check shows no difference. Any mismatch is reported to the user and is not worked around.
    - Safety: no Docker; no process stopped other than the Viewer started in this task; no write under `runs/`.
    - Verify: exit code 0 and the recorded output; `git status --short` shows no change under `runs/`.
    - _Plan: §5, §12 M2.1_

  - [x] 2.6 Write the concise operator run sheet
    - Create `docs/m2-plan/run-sheet.md`: one page with the preflight checklist (script command first, then the manual fallbacks from plan §5), exact commands, fallback port, the three canonical run ids and URLs (`/`, the three `/runs/<id>` URLs, `/compare?a=...&b=...&c=...`), and the demo order A briefly, then B and C and the comparison.
    - Include the Non-Negotiable Demo Facts section verbatim from this file. Include what Coroner refuses to claim (plan §7 and §8) and the headline condition (shown only when B and C both have ExitCode 137 and OOMKilled differs; suppressed on an ExitCode or OOMKilled source discrepancy).
    - Include the rule that a recording or screenshot is never presented as the live Viewer.
    - Files: `docs/m2-plan/run-sheet.md`.
    - Verify: `Select-String` finds each Non-Negotiable Demo Fact sentence in the file; `.\.venv\Scripts\python.exe -m pytest tests/unit/test_docs.py -q -p no:cacheprovider -rs` still passes (record counts).
    - Safety: no claim beyond the recorded evidence; no new facts.
    - _Plan: §4, §5, §6, §7, §8, §12 M2.1_

  - [x] 2.7 Write the reproducible judge test-build plan
    - Create `docs/m2-plan/judge-test-build.md`: install steps (create the venv, install `requirements/dev.txt`), the test command (`.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py tests/unit/test_docs.py -q -p no:cacheprovider -rs`), and a way to see the Viewer using safe synthetic sample evidence generated with the fixture builders in `tests/unit/viewer/builder.py` into a temporary directory outside the repository, then `python -m coroner.viewer --runs-dir <that directory> --host 127.0.0.1 --port 8765`.
    - State that the real `runs/` is not published, that the local viewer is never exposed publicly (no tunnel, no hosting), and that judges run it on their own machine. State what the synthetic sample proves and does not prove.
    - If generating the synthetic sample needs a new code file, STOP and ask the user for approval before creating it; do not add one silently.
    - Files: `docs/m2-plan/judge-test-build.md`.
    - Verify: run the documented commands once on this machine against a temporary directory (read-only for `runs/`), record the real output, and confirm `/api/runs` lists the synthetic runs.
    - Safety: no real run data in the sample; no Docker.
    - _Plan: §1, §2_

  - [x] 2.8 Checkpoint - stop for review (M2.1)
    - Report real results: script path and behaviour, test counts and skips, baseline location (name only), preflight output on the real runs, documents created.
    - Stop and wait for the user's review. No commit.

- [ ] 3. M2.2 Rehearsal and backup recording
  - [-] 3.1 Write the screen sequence and transitions
    - Create `docs/m2-plan/recording-script.md`: the ordered screen sequence with URLs, spoken lines, the pointer targets and the transition between pages, for a target of 2:45 and a hard limit of under 3:00. Order: `/` (three cards), A briefly (`/runs/a-20261120T211146Z-4300f395`, ExitCode 1, OOMKilled false), B (`/runs/b-20261120T211311Z-ce313da1`), C (`/runs/c-20261120T211433Z-09b81ca1`), then `/compare?a=...&b=...&c=...` and the closing statement on what Coroner does not claim.
    - Every spoken line is checked against the Non-Negotiable Demo Facts and against the text on the page; no statement goes beyond the page text. Do not say who sent the SIGKILL beyond "the Runner" and do not say "the sample came before the kill".
    - Add per-segment time budgets that sum to at most 2:45. Mark Clipchamp as the recording and editing tool and ElevenLabs as optional narration (a synthetic voice must be disclosed in the video description).
    - Files: `docs/m2-plan/recording-script.md`.
    - Verify: sum of the budgets is at most 2:45 (show the arithmetic); each Non-Negotiable Demo Fact appears unchanged.
    - _Plan: §4, §6, §7, §8, §12 M2.2_

  - [-] 3.2 List the fallback screenshots and clips
    - Add a section to `docs/m2-plan/recording-script.md` listing, per screen, the fallback file: existing headless screenshots in `docs/m1-results/browser-4.7/` (for example `01-overview-desktop.png`, `02-compare-desktop.png`, `06-experiment-b-chart.png`, `07-experiment-c-chart.png`) and `docs/m1-results/browser-4.14/`, plus any new human-recorded clip. Verify each listed file exists (`Test-Path`).
    - State that a fallback is announced as "a recording / a saved screenshot, not the live Viewer" and that a second copy of the recording is kept on another device or drive.
    - Safety: do not change or regenerate any screenshot; no evidence changes.
    - Files: `docs/m2-plan/recording-script.md`.
    - Verify: `Test-Path` is true for every listed file.
    - _Plan: §9_

  - [~] 3.3 HUMAN-ONLY: timed rehearsal
    - The presenter runs the preflight (task 2.6), then rehearses the script with a timer in Chrome. Record the real time per segment and total. A listener who has not seen Coroner says what B and C differ in.
    - Record observations in `docs/m2-plan/rehearsal-record.md` (create it if absent) in a section "M2.2 timed rehearsal" with presenter, date (from the person), Chrome version (from `chrome://version`) and the measured times. Any problem becomes an entry in a defect list for group 5.
    - Safety: a person performs and observes this; the agent only writes down what the person reports. No Docker.
    - Verify: the section exists with the person's name, date and measured times, or the task stays open.
    - _Plan: §12 M2.2_

  - [~] 3.4 HUMAN-ONLY: record the backup video in Clipchamp
    - The presenter records the screen in Clipchamp (and optionally adds ElevenLabs narration), keeps the total under 3:00 (target 2:45), and stores the file outside `runs/` and outside the repository, with the date and Chrome version in the file name or an adjacent note. A copy goes to a second device or drive.
    - Record file name, duration as shown by the player, and storage locations (names only) in `docs/m2-plan/rehearsal-record.md`.
    - Safety: do not commit the video; do not upload it anywhere in this task.
    - Verify: the recorded duration is strictly less than 3:00, as read by the person from the player.
    - _Plan: §9, §12 M2.2_

  - [~] 3.5 Confirm that evidence is unchanged after rehearsal
    - Run `scripts/demo-preflight.ps1 -Offline -BaselinePath <baseline>` (no server needed) and record the result. Run `git status --short` and confirm nothing under `runs/` is staged or changed.
    - Safety: read-only.
    - Verify: exit code 0 and an integrity PASS.
    - _Plan: §12 M2.4_

  - [~] 3.6 Checkpoint - stop for review (M2.2)
    - Report the measured times, the recording duration and location (names only), the defect list, and the preflight result. Stop and wait for the user's decision.

- [ ] 4. M2.3 Human usability
  - [~] 4.1 Prepare the normal-Chrome checklist
    - Create `docs/m2-plan/rehearsal-record.md` section "M2.3 usability checklist" (or extend the file from task 3.3) with one row per check: H1 to H7 as defined in `docs/m1-results/m1.1-acceptance.md` (read them first and copy their wording, do not invent new ones), one pass at 200% browser zoom, and V28 readability of the overview, B, C and compare pages. Each row has empty fields: Observer name, date, Chrome version (`chrome://version`), operating system, URL visited, display resolution and zoom, result (PASS / FAIL / NOT TESTED), observation. Every result starts as NOT TESTED.
    - Safety: pre-filled rows contain no PASS and no observations.
    - Files: `docs/m2-plan/rehearsal-record.md`.
    - Verify: `Select-String -Pattern 'PASS' docs/m2-plan/rehearsal-record.md` finds only the legend line, none in a result column.
    - _Plan: §10_

  - [~] 4.2 HUMAN-ONLY: perform the checklist in normal Chrome
    - The presenter operates normal Chrome (not headless). A second person observes and writes the observations; the presenter does not fill in the observer's notes. Cover H1 to H7, the 200% zoom pass and V28 readability.
    - The agent transcribes only what the observer provides, with the observer's name and date as given by the person. A check not performed stays NOT TESTED. Earlier mis-attributed PASS entries stay withdrawn.
    - Verify: each recorded result has an observer, date, Chrome version and observation.
    - _Plan: §10, §12 M2.3_

  - [ ]* 4.3 HUMAN-ONLY (optional): NVDA screen reader pass
    - If the user chooses to do it, a person runs NVDA over the overview and a run page and reports what was heard. Name the product and version. If not performed, the record says "NOT TESTED".
    - Safety: never claim NVDA was tested without a human's actual observations.
    - Verify: the record has the product name, the observer and the observations, or the row reads NOT TESTED.
    - _Plan: §10, §13 decision 4_

  - [~] 4.4 Append the results to the M1.1 acceptance record
    - After task 4.2, append a new section at the end of `docs/m1-results/m1.1-acceptance.md` that links to `docs/m2-plan/rehearsal-record.md` and summarizes the PASS / FAIL / NOT TESTED results as recorded. Do not edit earlier sections.
    - Failures become the defect list for group 5.
    - Files: `docs/m1-results/m1.1-acceptance.md`.
    - Verify: `git diff -- docs/m1-results/m1.1-acceptance.md` shows only appended lines; run `.\.venv\Scripts\python.exe -m pytest tests/unit/test_docs.py -q -p no:cacheprovider -rs` and record counts.
    - _Plan: §10_

  - [~] 4.5 Checkpoint - stop for review (M2.3)
    - Report the real PASS / FAIL / NOT TESTED counts, the defect list, and which checks remain NOT TESTED. Stop and wait for the user's decision.

- [ ] 5. M2.4 Defect fixes
  - [~] 5.1 Triage the defect list
    - List every defect from tasks 3.3, 4.2 and 4.4. For each: demo-blocking or not, the affected check, and the proposed minimal fix. Defects that would need a new feature, an M0 change or a new dependency are reported to the user and are not fixed here. If the list is empty, record "no defects found" and tick tasks 5.2 and 5.3 as not applicable with that note.
    - Safety: no code change in this task; no feature expansion without review.
    - Verify: a triage table exists and the user approves each fix to be made.
    - _Plan: §12 M2.4_

  - [~] 5.2 Fix each approved defect (only defects found in rehearsal)
    - For each approved defect: a minimal fix in `coroner/viewer/` only, with a test; no M0 change. Run `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py tests/unit/test_docs.py -q -p no:cacheprovider -rs` (collect-only first) and record the counts.
    - Safety: M1.1 implementation is otherwise frozen; no commit; the reviewer approves each fix before group 6.
    - Verify: the new test fails before the fix and passes after, with real output recorded.
    - _Plan: §12 M2.4_

  - [~] 5.3 HUMAN-ONLY: repeat the affected checks
    - A person repeats each affected check in Chrome after restarting the Viewer and pressing Ctrl+F5. Record the new result with observer, date and Chrome version in `docs/m2-plan/rehearsal-record.md`. Run `scripts/demo-preflight.ps1 -Offline -BaselinePath <baseline>` and record that `runs/` is unchanged.
    - Verify: the repeated check has a person's observation and the preflight integrity check passes.
    - _Plan: §12 M2.4_

  - [~] 5.4 Checkpoint - stop for review (M2.4)
    - Report fixes made, tests run with counts, repeated human checks, and the integrity result. Stop and wait for the user's approval before group 6.

- [ ] 6. M2.5 Dress rehearsal and freeze
  - [~] 6.1 HUMAN-ONLY: dress rehearsal on the demo display and machine
    - The presenter runs the preflight, starts the Viewer, and performs the full script on the actual demo display and machine with the network off. Record the result in `docs/m2-plan/rehearsal-record.md` under "M2.5 dress rehearsal": date, display resolution, zoom, Chrome version, network state, total time, and any deviation from the script.
    - Safety: no Docker; the Viewer binds `127.0.0.1` only.
    - Verify: the live run matches the script, as stated by the presenter and observer.
    - _Plan: §5, §12 M2.5_

  - [~] 6.2 Confirm that the recording matches the committed app
    - Compare the recording's pages and texts against the current committed app (`git status --short` clean for `coroner/viewer/`, `git log --oneline -1` recorded). If the app changed in group 5 after the recording was made, a HUMAN re-records it. Note the commit id and the recording file name in `docs/m2-plan/rehearsal-record.md`.
    - Verify: the recorded commit id equals `git rev-parse --short HEAD` for the frozen app.
    - _Plan: §9, §12 M2.5_

  - [~] 6.3 Write the judge questions and factual answers
    - Add a section to `docs/m2-plan/run-sheet.md` with a short list of questions judges may ask and answers drawn only from `README.md`, `docs/m0-results/`, `docs/m1-results/` and the Non-Negotiable Demo Facts (for example why the clocks are not synchronised, who sent the SIGKILL in B, whether the cause of memory growth in C is established). Cite the source document for each answer.
    - Verify: each answer names its source file.
    - Files: `docs/m2-plan/run-sheet.md`.
    - _Plan: §12 M2.5_

  - [~] 6.4 Write the Kiroween 2026 submission checklist
    - Create `docs/m2-plan/submission-checklist.md` with these items, each with a status field starting as NOT DONE and an evidence field: deadline 4 December 2026 22:00 UK time; public GitHub repository; approved open-source license (check that a LICENSE file exists and is on the approved list, otherwise STOP and ask the user); root `.kiro` directory included in the repository (check it is not ignored: `git check-ignore -v .kiro`); working project or reproducible test build (`docs/m2-plan/judge-test-build.md`); install and test instructions in `README.md`; public YouTube video under 3 minutes; Kiro usage write-up; an accurate description of what Coroner proves and what it does not prove, consistent with the Non-Negotiable Demo Facts; and the statement that the local viewer is never made publicly reachable.
    - Do not tick any item that needs an external action (upload, form submission). Those are HUMAN-ONLY and are recorded by the person.
    - Files: `docs/m2-plan/submission-checklist.md`.
    - Verify: each item has a status; `git check-ignore -v .kiro` prints nothing.
    - _Plan: §1, §9_

  - [~] 6.5 Freeze check
    - Run `scripts/demo-preflight.ps1 -BaselinePath <baseline>` with the Viewer launched by the script (`-LaunchViewer`) and record the output. Run the focused regression command from task 1.5 plus `tests/unit/test_demo_preflight.py` and record counts. Confirm that the preflight passes, the run sheet, recording script and checklist exist, and the backup recording exists and plays (a person confirms).
    - Report to the user, who decides whether the demo is frozen. After the freeze, no change is made except by a new decision.
    - Safety: no Docker; no write under `runs/`.
    - Verify: exit code 0 for the preflight and tests, with counts recorded.
    - _Plan: §12 M2.5_

  - [~] 6.6 Commit and push the M2 deliverables (requires explicit user approval at execution time)
    - After the user approves, stage by explicit path only: `scripts/demo-preflight.ps1`, `tests/unit/test_demo_preflight.py`, `docs/m2-plan/`, `docs/m1-results/m1.1-acceptance.md`, `.kiro/specs/coroner-m2-demo/`, and any approved defect-fix files from task 5.2. Never `git add -A` or `git add .`. Check `git diff --cached --name-only` has no `runs/` path, no video, no baseline and no scratch file. Commit, then push without force.
    - Run the secrets and privacy scan of task 1.3 over the staged files before committing.
    - Safety: no commit without approval; no git config change.
    - Verify: `git status --short` clean for the intended files and the push output recorded.
    - _Plan: §13 decision 1_

  - [~] 6.7 Final checkpoint - stop for review (M2.5)
    - Report the dress rehearsal result, freeze status, the submission checklist status per item, and every item that is still HUMAN-ONLY and open (video upload, write-up, submission form). Stop.

## Notes

- Human-only tasks: 3.3, 3.4, 4.2, 4.3 (optional), 5.3, 6.1. Part of 6.4 (external actions) is also human-only.
- Approval-gated tasks: 1.10 (merge), 6.6 (commit and push).
- The plan's open decisions are encoded as reviewer decisions above. Plan open decision 6 (spec text updates for the `/compare` 400 body and chart label wording) was not part of the approved scope and is not addressed here.
- No property-based tests: the only new code is a PowerShell script with environment and file-system checks, so a design Correctness Properties section does not exist. The script is covered by example tests (task 2.3).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4"] },
    { "id": 2, "tasks": ["1.5"] },
    { "id": 3, "tasks": ["1.6"] },
    { "id": 4, "tasks": ["1.7"] },
    { "id": 5, "tasks": ["1.8"] },
    { "id": 6, "tasks": ["1.9"] },
    { "id": 7, "tasks": ["1.10"] },
    { "id": 8, "tasks": ["1.11"] },
    { "id": 9, "tasks": ["2.1"] },
    { "id": 10, "tasks": ["2.2", "2.4"] },
    { "id": 11, "tasks": ["2.3"] },
    { "id": 12, "tasks": ["2.5"] },
    { "id": 13, "tasks": ["2.6", "2.7"] },
    { "id": 14, "tasks": ["2.8"] },
    { "id": 15, "tasks": ["3.1"] },
    { "id": 16, "tasks": ["3.2"] },
    { "id": 17, "tasks": ["3.3"] },
    { "id": 18, "tasks": ["3.4"] },
    { "id": 19, "tasks": ["3.5"] },
    { "id": 20, "tasks": ["3.6"] },
    { "id": 21, "tasks": ["4.1"] },
    { "id": 22, "tasks": ["4.2"] },
    { "id": 23, "tasks": ["4.3"] },
    { "id": 24, "tasks": ["4.4"] },
    { "id": 25, "tasks": ["4.5"] },
    { "id": 26, "tasks": ["5.1"] },
    { "id": 27, "tasks": ["5.2"] },
    { "id": 28, "tasks": ["5.3"] },
    { "id": 29, "tasks": ["5.4"] },
    { "id": 30, "tasks": ["6.1"] },
    { "id": 31, "tasks": ["6.2", "6.3", "6.4"] },
    { "id": 32, "tasks": ["6.5"] },
    { "id": 33, "tasks": ["6.6"] },
    { "id": 34, "tasks": ["6.7"] }
  ]
}
```
