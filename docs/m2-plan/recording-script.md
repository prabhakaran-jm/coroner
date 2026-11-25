# Coroner demo recording: rehearsal sequence and narration (task 3.1)

**NOT YET REHEARSED. The narration draft in section B has not been read aloud or timed by a person.**

Status: written, not rehearsed. No timing below is a measurement. Every time is a budget (see section 1). Nothing in this document records a human observation. The recording (task 3.4) and the timed rehearsal (task 3.3) are human-only.

Sources read: `docs/m2-plan/m2-demo-readiness-plan.md`, `run-sheet.md`, `canonical-run-verification.md` (including "B SIGKILL timestamp investigation"), `startup-shutdown.md`, `coroner/viewer/static/copy.js`, `.kiro/specs/`. Page text in section 4 was read from the real saved runs in headless Chrome at 1920x1080 on 2026-11-20 run data (section 6). Real saved runs only: A `a-20261120T211146Z-4300f395`, B `b-20261120T211311Z-ce313da1`, C `c-20261120T211433Z-09b81ca1`. No reruns, no Docker.

Tool roles: Clipchamp records and edits. ElevenLabs narration is optional; if used, say in the video description that the voice is synthetic.

## 1. Screen order and time budget (target 2:45, hard limit under 3:00)

Segments run in the order the reviewer required. Budgets sum to 2:42, which leaves 18 s under 3:00 and 8 s under the 2:50 ceiling.

| # | Segment | Screen | Start | End | Seconds |
|---|---|---|---|---|---|
| 1 | Problem statement | Overview `/` | 0:00 | 0:15 | 15 |
| 2 | A baseline | `/runs/<A>` | 0:15 | 0:27 | 12 |
| 3 | B SIGKILL evidence | `/runs/<B>` | 0:27 | 1:12 | 45 |
| 4 | C recorded OOM kill | `/runs/<C>` | 1:12 | 1:52 | 40 |
| 5 | A/B/C comparison | `/compare?a=...&b=...&c=...` | 1:52 | 2:22 | 30 |
| 6 | Kiro's role | stays on the compare page (no new screen) | 2:22 | 2:42 | 20 |

Arithmetic: 15 + 12 + 45 + 40 + 30 + 20 = 162 s = 2:42. If the spoken audio runs long, shorten segment 2 first, never the closing statements in segments 5 and 6.

URLs (viewer on port 8765; if the fallback port 8766 is used, every URL changes):

- Overview: `http://127.0.0.1:8765/`
- A: `http://127.0.0.1:8765/runs/a-20261120T211146Z-4300f395`
- B: `http://127.0.0.1:8765/runs/b-20261120T211311Z-ce313da1`
- C: `http://127.0.0.1:8765/runs/c-20261120T211433Z-09b81ca1`
- Compare: `http://127.0.0.1:8765/compare?a=a-20261120T211146Z-4300f395&b=b-20261120T211311Z-ce313da1&c=c-20261120T211433Z-09b81ca1`

Recommended setup: five tabs opened before recording, in this order: Overview, A, B, C, Compare. Switch with Ctrl+Tab (no typing on camera, scroll position of each tab is kept). Each tab is loaded and at the top before recording starts.

## A. Screen-by-screen rehearsal sequence

Scroll distances are approximate pixel offsets of the page top, read from 1920x1080 full-page renders (reference images in `docs/m2-plan/rehearsal-shots/`). They can differ by about 100 px. Confirm them once in rehearsal.

### Segment 1: Overview, 0:00 to 0:15

- URL: `http://127.0.0.1:8765/`. Tab 1. Page at top, no scroll.
- On screen: heading "Container Crash Forensics", subtitle "What survived after the process died?", cards for Experiment A, B, C. The "Other saved runs" list starts near the bottom edge. Do not scroll to it.
- Action: 0:00 to 0:06 pointer rests near the heading and subtitle. 0:06 to 0:15 pointer moves slowly across the three card titles, left to right.
- Transition: Ctrl+Tab to tab 2 (A). A hard cut in Clipchamp is fine.

### Segment 2: A, 0:15 to 0:27

- URL: A. Tab 2, page at top.
- Action: scroll to about 800 px so the "Docker state" block is in the middle of the screen (Docker ExitCode `1`, Docker OOMKilled `false`). Hold. Pointer rests on the ExitCode line, then the OOMKilled line. Do not scroll further.
- Transition: Ctrl+Tab to tab 3 (B).

### Segment 3: B, 0:27 to 1:12

- URL: B. Tab 3, page at top.
- 0:27 to 0:36: scroll to about 800 px. "Docker state" in the middle: Docker ExitCode `137`, Docker OOMKilled `false`. Pointer on each line in turn.
- 0:36 to 0:42: scroll to about 1,300 px. The block "Termination as described by the Runner" (dashed border, "recorded by the Runner") shows `SIGKILL sent by the runner (docker kill --signal=KILL)`. Pointer on that line.
- 0:42 to 0:46: one fast, steady scroll down to about 6,300 px (one flick of the mouse wheel; do not stop on the "Derived intervals (Experiment B)" cards, see defect D2). Say the first words of the chart sentence while scrolling.
- 0:46 to 1:12: hold on the "RSS timeline" (heading at about 6,500 px; the plot is about 6,550 to 6,950 px; its axis title, the date line, the axis note and the legend lines are below it, to about 7,050 px). Pointer sequence: the axis title "Derived host-equivalent time", then the axis note line, then the dotted purple "SIGKILL" line, then the legend line for it. Keep the whole chart, axis title and both legend lines in one frame (scroll to about 6,350 px if the legend is cut off).
- Do not scroll down to "Evidence vs Uncertainty" (about 7,800 px) in this segment.
- Transition: Ctrl+Tab to tab 4 (C).

### Segment 4: C, 1:12 to 1:52

- URL: C. Tab 4, page at top.
- 1:12 to 1:24: scroll to about 800 px. "Docker state": ExitCode `137`, OOMKilled `true`, Memory limit `128.00 MiB (134217728 bytes)`. Below it (about 1,320 px page position) is the amber banner "Docker records OOMKilled true and ExitCode 137 for this run. This establishes the immediate termination mechanism: an out-of-memory kill." and the line "The underlying reason for the memory growth is not established by the recorded evidence." Scroll so both are visible (about 900 px). Pointer on the banner sentence, then on the second sentence.
- 1:24 to 1:44: scroll to about 3,150 px. "RSS timeline": plot, dashed amber "128 MiB limit" line, points above it at the right end, axis title "Raw Probe Capture_Time", axis note "Capture_Time as recorded (Container_Clock). No clock correction is applied." Pointer: limit line, then the highest points, then the axis title and note.
- 1:44 to 1:52: scroll down only as far as needed to show the text "Sampling can miss memory growth between preserved Samples and after the last preserved Sample." directly under the chart (about 3,300 px; the earlier figure of 3,700 px risks bringing the Sampling_Limitations box into frame). Pointer on that line. Before recording, confirm in the frame that the dashed "recorded by the Runner: Sampling_Limitations" box is not visible; if any part of it shows, scroll back up until it is gone, even if the line is then partly cut (the narration does not depend on the line being on screen).
- Transition: Ctrl+Tab to tab 5 (Compare).

### Segment 5: Compare, 1:52 to 2:22

- URL: Compare. Tab 5, page at top. At 1080 px height the top frame shows the amber headline, the three slot cards and the whole "Recorded values side by side" table down to "Docker state sources". The "Comparison statements" start at the bottom edge.
- 1:52 to 2:00: pointer on the headline "The same exit code does not always mean the same failure".
- 2:00 to 2:12: pointer on the table row "Docker ExitCode" (B 137, C 137), then the row "Docker OOMKilled" (B false, C true). Pointer to the A column ExitCode (1) once.
- 2:12 to 2:22: scroll to about 900 px so the statements "Experiment B and Experiment C share ExitCode 137" and "OOMKilled distinguishes Experiment B (false) from Experiment C (true)" are visible, and the sentence under "What these values do not establish" ("ExitCode alone does not establish a cause...") comes into view if scrolled slightly further (about 1,300 px). Pointer on that sentence.
- Transition: none. The same screen stays for segment 6.

### Segment 6: Kiro's role, 2:22 to 2:42

- Screen: stay on the compare page, scrolled to the "What these values do not establish" block. No new screen. No code editor, terminal or file explorer is shown (they would show the user profile path; see section 5).
- Optional closing visual (last 8 s): a sanitized view of the real `.kiro` spec directory, captured by the operator during rehearsal (no image is supplied with this document). It must show no local path: use the GitHub repository page for the `.kiro/specs` folder, or an editor file tree with the path bar, title bar and account hidden. Real structure on branch `feat/coroner-m2-demo` (from `git ls-files .kiro`):

```text
.kiro/
  specs/
    coroner-m0/
      .config.kiro
      design.md
      requirements.md
      tasks.md
      tasks.meta.json
    coroner-m1-ui/
      .config.kiro
      design.md
      requirements.md
      tasks.md
      tasks.meta.json
    coroner-m2-demo/
      .config.kiro
      tasks.md
      tasks.meta.json
```

  Show only this tree. Do not open a file, a chat or an execution log, and do not describe sub-agents or any workflow that is not recorded in these files. If no clean capture is possible, skip the visual and stay on the compare page.
- End: stop the recording at 2:42. Add no outro.

## B. Narration draft per screen

Read the words in quotes. The run sheet's say / do-not-say table is binding. The pace target is about 2.3 to 2.6 words per second (about 140 to 155 words per minute) for a calm voice. The drafts below run at 1.4 to 2.53 words per second, with pointer pauses, which leaves room for pointer pauses. All word counts are exact counts of the quoted text (whitespace-separated tokens) and are not measurements of speech.

| Segment | Seconds | Words | Words per second |
|---|---|---|---|
| 1 Problem statement | 15 | 38 | 2.53 |
| 2 A baseline | 12 | 17 | 1.42 |
| 3 B (3a 29 words, 3b 73 words) | 45 | 102 | 2.27 |
| 4 C (4a 21 words, 4b 51 words) | 40 | 72 | 1.80 |
| 5 Comparison | 30 | 57 | 1.90 |
| 6 Kiro's role | 20 | 39 | 1.95 |
| Total | 162 | 325 | 2.01 |

No segment is above 2.6 words per second (the highest is segment 1 at 2.53), so no trim was needed. Split rates: 3a 1.93 (15 s), 3b 2.43 (30 s), 4a 1.75 (12 s), 4b 1.82 (28 s). The total stays at 162 s (2:42), at most 2:50. If a segment runs long when read, cut the pointer pauses first, not the "not established" sentences.

### 1. Problem statement (38 words, 15 s)

"Two containers can die with the same exit code. Coroner preserves and analyses evidence from container failures. This read-only Viewer starts no containers and does not modify saved runs. Here are three recorded experiments: A, B and C."

### 2. A baseline (17 words, 12 s)

"Experiment A exited with code 1, and Docker recorded OOMKilled false. That is the baseline. Now B."

Say nothing about why A exited. The page shows no cause statement that this line should add to.

### 3. B (102 words, 45 s)

0:27 to 0:42 (3a, 29 words):
"Experiment B. Docker recorded ExitCode 137 and OOMKilled false. The Runner's own record says it sent SIGKILL with docker kill. Docker did not record an out-of-memory kill for B."

0:42 to 1:12 (3b, 73 words, read in this order):
"The chart plots preserved memory samples on a derived host-equivalent time axis, not independently proven." (15 words, derived-time uncertainty, kept)
"One timestamp was recorded immediately before the Runner issued the kill command. Another was written after the command returned. They are 425 milliseconds apart, but that does not measure when the signal reached the container." (35 words, required wording, read exactly)
"The dotted line marks the first timestamp. Arrival in the container is not recorded. The page does not establish an order across clocks." (23 words, kept uncertainty)

What the two B records are (from `canonical-run-verification.md`, "B SIGKILL timestamp investigation", code read in `exp_b.py` lines 147 to 153):

- `times.sigkill_sent_time` is a Host_Clock stamp taken immediately before the Runner issued `docker kill --signal=KILL`. The dotted line sits at this stamp.
- The phase-log entry `sigkill_sent` was written after that command returned.
- The two stamps are 425 ms apart in the canonical B run. That gap does not measure when the signal reached the container. (`canonical-run-verification.md` labels the gap as an inferred command duration; do not say that on camera.)
- The record does not say when the signal arrived in the container.
- The dotted line's position on the derived axis is derived, not independently proven.

Do not say: any exact SIGKILL time or clock reading, "the sample came before the kill", the -10 ms or -80 ms values or any reading of them as an order of events, "B's time is corrected and verified", that the gap is the command duration or kill latency, or that ExitCode 137 means a Docker OOM kill. Never call B an out-of-memory kill.

### 4. C (72 words, 40 s)

1:12 to 1:24 (4a, 21 words):
"Experiment C. Docker recorded ExitCode 137 and OOMKilled true. The page says this establishes the immediate termination mechanism: an out-of-memory kill."

1:24 to 1:52 (4b, 51 words):
"The memory limit is 128 MiB. The highest sampled memory is about 134.5 MiB, above that limit. The axis is raw probe capture time, with no correction applied. The page says the underlying reason for the memory growth is not established, and sampling can miss growth after the last preserved sample."

Do not say: any cause of the growth (leak, bug, application), "C proves a memory leak", that the sampled peak is the real peak, or anything read from the Runner block or the Sampling_Limitations box.

Sampling_Limitations box (C): the dashed "recorded by the Runner: Sampling_Limitations" box under the C chart contains the sentence "The peak RSS in the Samples may understate the container memory limit that triggered the kill." That wording is inaccurate (defect D6, section 9). It must not be read aloud or shown in the recording. The saved C evidence keeps the old wording even after the approved M0 wording fix, because a Runner change does not alter manifests that are already saved and M2 forbids rerunning experiments. So the box stays out of the recording in every case: keep it below the frame, never scroll to it, and use only the stills that do not show it (section 8).

### 5. Comparison (57 words, 30 s)

"Side by side, B and C share ExitCode 137. OOMKilled is false for B and true for C. That is what separates them. The page headline says: the same exit code does not always mean the same failure. The page also says ExitCode and OOMKilled are two separate recorded facts, and neither is inferred from the other."

### 6. Kiro's role (39 words, 20 s)

"Coroner was built with Kiro's spec workflow. The first two milestones have requirements, design and task lists with review checkpoints, saved in the .kiro folder. Each task's executions are logged there, and the repository has unit and property-based tests."

Project records behind each sentence (checked on the branch `feat/coroner-m2-demo`):

- "Built with Kiro": `README.md` line 3 ("built with Kiro for Kiroween 2026").
- Spec workflow: `.kiro/specs/coroner-m0/` and `.kiro/specs/coroner-m1-ui/` each hold `requirements.md`, `design.md` and `tasks.md`. `.kiro/specs/coroner-m2-demo/` holds `tasks.md` only (it states there is no requirements.md or design.md), which is why the narration says "the first two milestones".
- Review checkpoints: the three `tasks.md` files contain tasks named "Checkpoint" (48 lines match, for example "2.8 Checkpoint - stop for review (M2.1)" in `coroner-m2-demo/tasks.md`).
- Executions logged: each spec has a `tasks.meta.json` with an `executionHistory` of task names, each entry holding an `executionId`, a `chatSessionId` and a timestamp. Tasks with entries: `coroner-m0` 103, `coroner-m1-ui` 56, `coroner-m2-demo` 42. Entries exist for queued-but-unexecuted M2 tasks too, so they show task status activity, not proof that a task finished; that is why the narration says "logged" and nothing more.
- Tests: `requirements/dev.txt` pins `hypothesis` (6.122.3) and `pytest`; `README.md` line 103 lists both.
- Sub-agent claim: REMOVED. The old draft said tasks were executed by Kiro sub-agents. No project record supports that. `tasks.meta.json` has `executionHistory` entries with execution and chat-session ids but no field that says an entry was a sub-agent, and a search of the three files for "sub-agent", "subagent" and "parent" found nothing. Do not say "sub-agent", "autonomous", or any specific automated workflow unless the owner shows a record that supports it.
- Browser automation (headless Chrome harness under `tests/unit/viewer/`) was also dropped from the narration to keep the line short; it is not a Kiro claim.
- Optional closing line, only if the statuses are still true on the day of recording: "Human browser checks are recorded separately and are still marked not tested." `docs/m2-plan/rehearsal-record.md` does not exist at the time of this edit; look in the task 3.3 and 4.x records instead, and if a person has since recorded results, drop or change the line. Say nothing that implies a person tested the Viewer unless a record shows it.

## C. Expected visible evidence per screen

The operator confirms each line on screen before recording. Strings are from `copy.js` and the pages read on the saved runs. Values come from `canonical-run-verification.md` and were matched on screen in the headless check.

### Overview `/`

- "Container Crash Forensics", "What survived after the process died?".
- Card A: "Experiment A", "controlled fatal process exit via os._exit(1)", Run id `a-20261120T211146Z-4300f395`, Verdict PASS, Docker ExitCode 1, OOMKilled false, Preserved Samples 15, Collector survival "recorded: Collector survived", Docker state source "Docker inspect (raw file and manifest agree)", button "Open incident detail".
- Card B: Run id `b-20261120T211311Z-ce313da1`, ExitCode 137, OOMKilled false, Preserved Samples 15.
- Card C: Run id `c-20261120T211433Z-09b81ca1`, ExitCode 137, OOMKilled true, Preserved Samples 45.
- Below the cards: "Other saved runs" with six further run ids (the run list has nine runs).

### A `/runs/<A>`

- "Incident detail", Run id, Experiment type A, Docker state: Docker status `exited`, Docker ExitCode `1`, Docker OOMKilled `false`, Memory limit `128.00 MiB (134217728 bytes)`, each with "(state source: Docker inspect (raw file and manifest agree))".
- "Termination as described by the Runner": `controlled fatal process exit via os._exit(1)`, cause_label `controlled_fatal_process_exit`.
- Chart title text: "X axis: Raw Probe Capture_Time. Points plotted: 15. Highest plotted RSS: 70.88 MiB." (not shown in this segment).

### B `/runs/<B>`

- Docker ExitCode `137`, Docker OOMKilled `false`, Memory limit `128.00 MiB (134217728 bytes)`.
- "Termination as described by the Runner": `SIGKILL sent by the runner (docker kill --signal=KILL)`, cause_label `sigkill_sent_by_runner`.
- "Derived intervals (Experiment B)": four cards, each "derived: derived, not independently proven", with "difference -10 ms" and "difference -80 ms" and "this interval does not establish the order of two events". Not narrated.
- "RSS timeline": "X axis: Derived host-equivalent time. Points plotted: 15. Partial view: no. Highest plotted RSS: 70.74 MiB." Axis title "Derived host-equivalent time", ticks `21:13:16`, `21:13:18`, `21:13:20`, "128 MiB limit" label, "SIGKILL" label, "Date: 2026-11-20 UTC. Tick labels show the UTC clock time, HH:MM:SS.", axis note "Capture_Time − recorded clock_offset_ms. Derived, not independently proven.", legend lines `dashed amber line, "128 MiB limit": recorded memory limit (134217728 bytes)` and `dotted purple line, "SIGKILL": SIGKILL sent (Host_Clock; position derived, not independently proven)`.
- Below the chart (not shown in the recording): "SIGKILL send time (Host_Clock)" `2026-11-20T21:13:21.690Z`.
- Verified facts behind this page: Peak RSS 74174464 bytes, 15 Samples, B uses the derived axis (clock offset 9 ms, uncertainty 12.5375 ms).

### C `/runs/<C>`

- Docker ExitCode `137`, Docker OOMKilled `true`, Memory limit `128.00 MiB (134217728 bytes)`.
- Amber banner: "Docker records OOMKilled true and ExitCode 137 for this run. This establishes the immediate termination mechanism: an out-of-memory kill."
- Line: "The underlying reason for the memory growth is not established by the recorded evidence."
- Runner block: `genuine Docker OOM kill (kernel cgroup memory limit)`, cause_label `docker_oom_kill`.
- "RSS timeline": "X axis: Raw Probe Capture_Time. Points plotted: 45. Partial view: no. Highest plotted RSS: 134.55 MiB." Axis ticks `21:14:40`, `21:14:45`, `21:14:50`, `21:14:55`. Axis note "Capture_Time as recorded (Container_Clock). No clock correction is applied." "128 MiB limit" dashed line, with the final points above it.
- Under the chart: "Sampling can miss memory growth between preserved Samples and after the last preserved Sample."

### Compare

- Heading "Compare incidents", amber headline "The same exit code does not always mean the same failure".
- Slot cards: "Slot A: Experiment A", "Slot B: Experiment B", "Slot C: Experiment C", each "chosen in the address".
- Table "Recorded values side by side": Docker ExitCode 1 / 137 / 137, Docker OOMKilled false / false / true, Highest sampled RSS `70.88 MiB (74326016 bytes)` / `70.74 MiB (74174464 bytes)` / `134.55 MiB (141090816 bytes)`, Memory limit `128.00 MiB (134217728 bytes)` in all three, Preserved Samples 15 / 15 / 45, Docker state sources "no disagreement recorded".
- Statements: "Experiment A differs from Experiment B and Experiment C by exit code (A=1, B=137, C=137)", "Experiment B and Experiment C share ExitCode 137", "OOMKilled distinguishes Experiment B (false) from Experiment C (true)", "Evidence survived all three deaths in the recorded runs (Samples: A=15, B=15, C=45)".
- "Statements not shown, and why": "Every statement could be computed."
- "What these values do not establish": the three limit sentences, starting "ExitCode alone does not establish a cause. ExitCode and OOMKilled are two separate recorded facts, and neither is inferred from the other."
- If the headline is missing, stop: it is suppressed only on a source discrepancy (see run sheet section 5). Do not improvise it.

## D. Defects and risks found while checking

Checked by reading `copy.js` and rendering the five pages against the real saved runs (read-only) in headless Chrome at 1920x1080. No code was changed. None of these blocks the demo; D1 and D3 need a decision from the reviewer.

| ID | Where | Finding | Effect on the recording | Suggested handling |
|---|---|---|---|---|
| D1 | `/runs/<B>`, `/runs/<C>` | Incident pages are long. At 1920x1080 the first frame shows only Run, Verdict and the start of Docker state. ExitCode and OOMKilled are at about 1,100 px (below the fold), the B chart at about 6,500 px, the C chart at about 3,250 px. | B needs a long scroll in a 45 s segment. A slow scroll looks busy and reads as hunting. | Use the scroll plan in section A, rehearse it, and keep the fast flick past the derived-interval cards. Reviewer may decide on 80% browser zoom; this document assumes 100%. |
| D2 | `/runs/<B>`, "Derived intervals (Experiment B)" | Four cards between Docker state and the chart show "difference -10 ms" (corrected last Sample against the SIGKILL send time) and "-80 ms" (against Docker FinishedAt), each labelled "order not established". A viewer can read "-10 ms" as "the sample was just before the kill". | Contradicts the rule against cross-clock ordering if lingered on or read aloud. | Scroll past without stopping. Never narrate the numbers. |
| D3 | `/runs/<B>`, legend and "Recorded times" | The legend says "SIGKILL sent (Host_Clock; ...)" and the row says "SIGKILL send time (Host_Clock)". The saved value is a stamp taken just before the `docker kill` call (investigation in `canonical-run-verification.md`). The wording "send time" slightly overstates. | The spoken line says "recorded just before it issued the kill command", which is more exact than the page. A viewer may notice the difference. | Keep the narration as drafted. The `copy.js` wording is frozen in M1.1; any change is a group 5 decision, not made here. |
| D4 | Overview cards, incident pages, compare table | "Verdict PASS" appears for A, B and C. It is the Runner's Stored_Outcome (the experiment behaved as designed), and the page says so only in a small note on the incident page. | A viewer can read PASS as "the container was healthy". | Do not mention the Verdict. If asked: it is the Runner's stored outcome. |
| D5 | `/runs/<C>`, Runner block | The Runner's text reads "genuine Docker OOM kill (kernel cgroup memory limit)". "genuine" and "kernel cgroup" are the Runner's wording. The page labels it "recorded by the Runner". | Reading it aloud would claim more than Docker's own record. | Do not read the Runner block of C. Read only the Docker-based banner sentence. |
| D6 | `/runs/<C>`, Sampling_Limitations list | The third statement reads "The peak RSS in the Samples may understate the container memory limit that triggered the kill." The wording looks reversed (the peak is more likely to understate the real peak, not the limit). | Confusing if the panel is on screen. | Do not scroll to that block. Report to the reviewer as a wording defect (group 5), not changed here. |
| D7 | `/compare`, 1080 px frame | The statements start at the bottom edge of the first frame and are visible only after a scroll (about 900 px). The table and headline fit in the first frame. | None if the scroll in segment 5 is rehearsed. | As in segment 5. |
| D8 | Overview | "Other saved runs" lists six older runs (the Viewer holds nine). Their ids include the same dates. | A viewer may ask why there are six more. | Do not scroll. The cards are the canonical three. |
| D9 | Terminal output (not on the page) | The start line is `Coroner Viewer: http://127.0.0.1:8765/  (runs: runs, read-only)`, but the PowerShell prompt shows the user profile path. | A terminal in frame leaks the path. | Do not record a terminal (see section 5). |
| D10 | Console text only | A text dump showed "âˆ’" for the minus sign in the B axis note. `index.html` declares `charset utf-8`; the artefact is in the PowerShell console, not on the page. The screenshot shows the correct sign. | None. | None. |

Viewing the overview, B, C and compare top frames at 1920x1080 showed no clipped headline and no overlapping text (the harness hides scrollbars, so scrollbar appearance in normal Chrome is unchecked). The chart bodies of B and C were checked only through the full-page renders (1920x9000), not at viewport level, so chart legibility at the recording resolution is still a human check in rehearsal.

## E. Operator checklist

### Before recording

1. Preflight (read-only, safe at any time), from the repository root:
   `powershell -NoProfile -File scripts\demo-preflight.ps1 -Offline -BaselinePath "$env:TEMP\coroner-m2\demo-baseline.txt"`. Expect exit code 0 and an integrity PASS.
2. Check port 8765 is free or its owner is your own earlier Viewer (`startup-shutdown.md` section 3). Do not stop PID 6940 or any process you cannot identify as yours. If 8765 is taken, use 8766 and change every URL.
3. Start the Viewer in its own PowerShell window: `.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port 8765`. Keep that window out of the recorded area. Run the full preflight (without `-Offline`) once; expect `RESULT: PASS`.
4. Browser: a fresh Chrome profile or an incognito window with no extensions visible, no personal bookmarks bar, no profile avatar with a name, no sync prompt. Bookmarks bar off (Ctrl+Shift+B). Zoom 100% (Ctrl+0). Window maximised at 1920x1080 (if the display is larger, size the window to 1920x1080 or record a 1920x1080 region). Hard-reload (Ctrl+F5) each tab. Open the five tabs in the order of section 1; close all others.
5. Notifications off: turn on Windows Focus assist (Do not disturb), close mail, chat and the Kiro window, disable Chrome notifications. Hide the taskbar or record the browser window only. Wi-Fi off once during a rehearsal to confirm the Viewer is local.
6. Clipchamp: new screen recording, 1080p, microphone selected only if recording voice live, system audio off. Do a 10 s test, play it back and check the pointer is visible and no private path or window shows.
7. ElevenLabs (optional): generate the narration from section B, one file per segment, and mark the video as synthetic voice in the description. If the voice is live, read the script instead.
8. Dry run with a timer: record the six segment end times and the total. Total must be under 3:00 and should be at or under 2:50.

### During recording

1. Start the recording on tab 1 with the page at top. Do not show the terminal, the file explorer, the Kiro window or the address bar's autocomplete list.
2. Follow the pointer and scroll plan in section A. Do not hover over run-id links that open a status-bar URL near the bottom of the window (the status bar shows only 127.0.0.1 URLs, which is fine).
3. Do not open "Sample table" or any `original` disclosure. Do not show an error state. If the Viewer misbehaves, stop the recording and restart; do not rerun experiments.
4. Stop at about 2:42.

### After recording

1. Player check: the duration shown by the player is strictly under 3:00. Write the value down (task 3.4).
2. Cut timing in Clipchamp: trim dead air at the start and end, use hard cuts between tabs, speed up nothing on the chart segments, add no fades that hide the screen content. Add captions only if their text matches section B.
3. Watch the whole export once for: wrong run id, any visible path or profile name, notification toasts, text cut off at the window edge.
4. Save the export outside `runs/` and outside the repository. Put the date and the Chrome version (from `chrome://version`) in the file name or in a note beside it. Copy it to a second device or drive.
5. Stop the Viewer with Ctrl+C in its window. Confirm the port is free.
6. Evidence check: `powershell -NoProfile -File scripts\demo-preflight.ps1 -Offline -BaselinePath "$env:TEMP\coroner-m2\demo-baseline.txt"` (exit code 0, integrity PASS) and `git status --short` (nothing under `runs/`).

## 5. Where a sensitive local path could appear, and what to avoid

Read from `copy.js`, `overview.js`, `app.py`, `cli.py`, `model.py` and the rendered pages:

| Screen or element | Can it reveal a path? | Handling |
|---|---|---|
| Browser address bar | Shows `127.0.0.1:8765` and the run id only. | Fine to show. |
| Overview page | The "Runs_Directory_Label" is shown only in the empty state ("No saved runs were found in ..."). It is the string given to `--runs-dir` (`runs`), or only the last component when absolute, or "(runs directory)" when empty. The real runs exist, so the empty state does not appear. | No path appears with saved runs. Never start the Viewer on an empty or wrong directory before recording. |
| Incident pages, compare page | No filesystem path. `raw_inspect_path`, `host_copy_path` and `docker_inspect_path` in the manifests are not sent by the Viewer API (read in `model.py`; the rendered text of all five pages was scanned for `C:\`, `Users`, the profile name, `AppData` and `Temp`, with no hit). | Safe. |
| PowerShell window or prompt | Yes. The prompt shows the user profile path. The Viewer's start line shows only `runs`. | Do not record any terminal. Keep the Viewer window off screen. |
| Windows taskbar, notifications, file explorer, Kiro window | May show the profile name, project path or private chats. | Record the browser window only, notifications off, close other windows. |
| Chrome window | Profile avatar, tab titles of other tabs, bookmarks, extensions. | Fresh profile or incognito, only the five demo tabs. |
| Clipchamp export dialog | Shows the save location. | Do not show it on camera; it is not part of the recorded area. |

## 6. Reference screenshots and checks performed

Produced by this task (headless Chrome 1920x1080 against the real saved runs, read-only, a scratch Viewer on port 8777, stopped afterwards): `docs/m2-plan/rehearsal-shots/`

- `overview-1920x1080-top.png`, `a-1920x1080-top.png`, `b-1920x1080-top.png`, `c-1920x1080-top.png`, `compare-1920x1080-top.png` (the first 1080 px of each page)
- `b-1920x9000-full-page.png`, `c-1920x9000-full-page.png` (full-page renders used to derive the scroll offsets; reference renders only. `c-1920x9000-full-page.png`: not for use in the video, it shows the stored Runner sentence superseded by D6)
- `b-chart-1920x1080-viewport.png`, `c-chart-1920x1080-viewport.png` (chart-region viewport captures, added in task 3.2, see section 8)

They are automated reference renders, not recorded evidence and not human observation. The overview, B, C and compare top frames and the two full-page renders were read as images and show no filesystem path or profile name. The A top frame's text was scanned. A byte scan of the PNGs for the profile name, `Users`, `AppData` and `C:\` found nothing, and no text chunks. The offsets in section A were read from these images at reduced scale and can differ by about 100 px. The B and C chart-region viewport captures were added in task 3.2.

## 7. Verification of this document against the facts

- Each Non-Negotiable Demo Fact is used unchanged in meaning: A exited with code 1 and OOMKilled false; B ExitCode 137, OOMKilled false, SIGKILL by the Runner, not attributed to Docker OOM; C ExitCode 137, OOMKilled true, Docker recorded an OOM kill, growth cause not established; B's clock correction derived and not independently verified; A and C raw Probe Capture_Time; no cross-clock ordering.
- No exact B SIGKILL timestamp is narrated, and the narration describes the two B timestamp records as `canonical-run-verification.md` does (pre-call stamp, post-call phase stamp, 425 ms apart, not a measure of when the signal reached the container, arrival not recorded).
- The Kiro narration states only what project records support (section B, segment 6); the sub-agent claim was removed.
- ExitCode 137 of B is never used to infer a Docker OOM.
- Section 1 sums to 162 s (2:42), strictly under 3:00 and under 2:50.
- No fallback is presented as the live Viewer. If a fallback is needed, announce "a recording / a saved screenshot, not the live Viewer" (`run-sheet.md` section 8).

## 8. Fallback screenshots and clips (task 3.2)

Purpose: if the live Viewer fails during the demo, show one of these instead. Nothing here is a live run and nothing here is a human observation.

Every still in this section is an automated headless Chrome screenshot of the real saved runs, not live. The synthetic judge fixtures generated by `scripts/make-sample-runs.py` are NOT used in the recording or in any of its stills.

Stale images (historical only, not a current fallback): `docs/m1-results/browser-4.7/07-experiment-c-chart.png` and `docs/m1-results/browser-4.7/rerun-1/07-experiment-c-chart.png`. They predate the current copy (old C sampling sentence). Use `c-chart-1920x1080-viewport.png` and `c-1920x1080-top.png` for C instead.

`c-1920x9000-full-page.png` is a reference render only and is NOT a demo fallback: it shows the C Sampling_Limitations box with the stored Runner sentence superseded by D6 (section 9). Not for use in the video. For C use `c-1920x1080-top.png` and `c-chart-1920x1080-viewport.png`. Neither shows the D6 sentence. `c-1920x1080-top.png` covers only the first 1080 px, so it does not show Docker ExitCode or OOMKilled; for those values use `compare-1920x1080-top.png` (C column).

### Rules for every fallback

- Announce it before showing it. A still: say "This is a saved screenshot, not the live Viewer." A clip: say "This is a recording, not the live Viewer." (`m2-demo-readiness-plan.md` section 9, `run-sheet.md` section 8).
- Stills carry the on-screen label (a caption in Clipchamp or a text box over the slide, outside the screenshot area): **automated headless Chrome screenshot, not live**.
- Clips carry the on-screen label: **pre-recorded rehearsal clip**.
- Keep a second copy of the backup recording (task 3.4) on another device or drive. Neither the recording nor any copy goes into `runs/` or the repository.
- Do not edit, crop or regenerate the committed screenshots. If a different crop is needed, take a new capture and keep the old one.
- All stills below show the real saved runs A `a-20261120T211146Z-4300f395`, B `b-20261120T211311Z-ce313da1` and C `c-20261120T211433Z-09b81ca1`, unless a row says otherwise.

### Per segment

Paths are relative to the repository root. "Exists" was checked with `Test-Path` (section 8, end).

| # | Segment | Fallback still or clip | Source | Exists | What it shows | Label on screen |
|---|---|---|---|---|---|---|
| 1 | Overview | `docs/m1-results/browser-4.7/01-overview-desktop.png` (1280 wide, full page) | Committed automated screenshot, task 4.7 R01 | yes | Heading, three cards for A, B, C with ExitCode, OOMKilled, Preserved Samples, and "Other saved runs" | automated headless Chrome screenshot, not live |
| 1 | Overview (alternate) | `docs/m2-plan/rehearsal-shots/overview-1920x1080-top.png` | Created in task 3.1, headless Chrome 1920x1080 | yes | First 1080 px of the overview as framed in the recording | automated headless Chrome screenshot, not live |
| 2 | A baseline | `docs/m1-results/browser-4.7/03-compare-values.png` (viewport capture of the compare table) | Committed automated screenshot, task 4.7 test 01 | yes | A's recorded values (ExitCode 1, OOMKilled false) in the A column of the comparison table. It is not the A incident page. | automated headless Chrome screenshot, not live |
| 2 | A baseline (gap) | A incident page "Docker state" block | **Human clip**: 12 s of segment 2 from the rehearsal recording (task 3.4), cut as its own file | **no** | A page scrolled to "Docker state" showing Docker ExitCode `1` and OOMKilled `false`. No committed still of the A incident page shows these lines: `rehearsal-shots/a-1920x1080-top.png` is the page top only (ExitCode lies at about 1,100 px). | pre-recorded rehearsal clip |
| 3 | B SIGKILL evidence | `docs/m1-results/browser-4.14/V9-B-incident-1280.png` (1280x6000, top of the page) | Committed automated screenshot, task 4.14 | yes | Real B incident page from the top: Docker state ExitCode 137, OOMKilled false, the Runner block "SIGKILL sent by the runner (docker kill --signal=KILL)", clock offset, derived-interval cards. The image stops at 6,000 px, above the chart. | automated headless Chrome screenshot, not live |
| 3 | B chart | `docs/m1-results/browser-4.7/06-experiment-b-chart.png` (1176x1156, chart section) | Committed automated screenshot, task 4.7 R03 | yes | B "RSS timeline": derived host-equivalent axis, 128 MiB line, dotted SIGKILL line, both legend lines, "Recorded times" rows | automated headless Chrome screenshot, not live |
| 3 | B chart at recording size | `docs/m2-plan/rehearsal-shots/b-chart-1920x1080-viewport.png` | **New in this task**, headless Chrome 1920x1080, viewport capture with the "RSS timeline" heading near the top (about 6,500 px page offset) | yes | The same view the presenter sees in segment 3 at 0:46 to 1:12: whole chart, axis title `Derived host-equivalent time`, axis note, both legend lines, the first "Recorded times" rows | automated headless Chrome screenshot, not live |
| 3 | B full page | `docs/m2-plan/rehearsal-shots/b-1920x9000-full-page.png` | Created in task 3.1 | yes | Whole B page at 1920 wide | automated headless Chrome screenshot, not live |
| 4 | C recorded OOM kill (STALE: historical only, not a current fallback; same for `rerun-1/07-experiment-c-chart.png`) | `docs/m1-results/browser-4.7/07-experiment-c-chart.png` (1176x1281, chart section) | Committed automated screenshot, task 4.7 R04 | yes | C "RSS timeline": raw Capture_Time axis, 128 MiB line, final points above it, and the Runner's Sampling_Limitations box. This render predates the current wording: it shows "Sampling can miss growth in the final interval before the out-of-memory kill." where the current `copy.js` reads "Sampling can miss memory growth between preserved Samples and after the last preserved Sample." (line 342 to 343). It also shows the D6 sentence, see section 9. | automated headless Chrome screenshot, not live |
| 4 | C chart at recording size | `docs/m2-plan/rehearsal-shots/c-chart-1920x1080-viewport.png` | **New in this task**, same method as the B still | yes | Whole C chart: axis title `Raw Probe Capture_Time`, ticks 21:14:40 to 21:14:55, final points above the dashed 128 MiB line, the axis note and the legend line. The "Sampling can miss growth..." line and the Runner's Sampling_Limitations box are below the frame, so D6 text is not visible in this still. | automated headless Chrome screenshot, not live |
| 4 | C page top | `docs/m2-plan/rehearsal-shots/c-1920x1080-top.png` | Created in task 3.1 | yes | First 1080 px only; ExitCode and OOMKilled are below it. Use together with `compare-1920x1080-top.png` (C column shows ExitCode 137, OOMKilled true). | automated headless Chrome screenshot, not live |
| 4 | C reference render (NOT a fallback) | `docs/m2-plan/rehearsal-shots/c-1920x9000-full-page.png` | Created in task 3.1 | yes | Not for use in the video: shows the stored Runner sentence superseded by D6. Listed only so nobody picks it up by mistake. | do not show |
| 5 | A/B/C comparison | `docs/m1-results/browser-4.7/02-compare-desktop.png` (1280, full page) | Committed automated screenshot, task 4.7 test 01 | yes | Headline, three slot cards, the side-by-side table, the four statements, "What these values do not establish" | automated headless Chrome screenshot, not live |
| 5 | Comparison statements | `docs/m1-results/browser-4.7/04-compare-statements.png` | Committed automated screenshot, task 4.7 test 01 | yes | Viewport capture of the statements ("Experiment B and Experiment C share ExitCode 137", "OOMKilled distinguishes ...") | automated headless Chrome screenshot, not live |
| 5 | Comparison at recording size | `docs/m2-plan/rehearsal-shots/compare-1920x1080-top.png` | Created in task 3.1 | yes | Headline, slot cards and the whole table in the first frame | automated headless Chrome screenshot, not live |
| 6 | Kiro's role | No Viewer screen is shown in this segment, so no still is needed. | Optional sanitized `.kiro` spec tree captured by the operator (section A, segment 6) | no (human capture, not yet made) | Directory tree only, no local path | If a Viewer image has to stay up, use `04-compare-statements.png` with the compare label above. |
| all | Whole demo | The backup recording of the full script (task 3.4) | **Human clip**: Clipchamp export, under 3:00, stored outside `runs/` and outside the repository, second copy on another device or drive | **no** (human-only, task 3.4) | Segments 1 to 6 | pre-recorded rehearsal clip |

The two new 1920x1080 viewport stills close the gap noted in section 6 (no B or C chart-region viewport capture). Method: the existing harness `tests/unit/viewer/cdp_harness.py` (`ViewerProcess`, `Chrome`, `Page`), real saved runs read-only, viewport set with `Emulation.setDeviceMetricsOverride` to 1920x1080, "RSS timeline" heading brought into view with `window.scrollTo` from a DOM query, `Page.captureScreenshot` of the viewport without `captureBeyondViewport`. The scratch Viewer ran on an automatically chosen free port and was stopped by the harness when the script ended. The capture script is a scratch file outside the repository. Both images were read: no filesystem path, profile name or email is visible.

### What still needs a human

- A baseline clip (segment 2), as listed.
- The full backup recording (task 3.4) and its second copy.
- A check in rehearsal that the offsets of the stills match what the presenter sees in normal Chrome (the stills are headless, scrollbars hidden).
- Whether the stills are acceptable as a fallback is a presenter decision; they are automated renders and carry no human observation.

## 9. Defect D6: Sampling_Limitations wording (read-only investigation)

Result: the sentence is wrong as written, it comes from the M0 Runner and is stored in the saved C manifest, and the Viewer only displays it. Nothing was changed.

- Text: "The peak RSS in the Samples may understate the container memory limit that triggered the kill."
- Where the Viewer shows it: the dashed "recorded by the Runner: Sampling_Limitations" box under the C chart (third bullet). The Viewer builds the list from the manifest (`coroner/viewer/model.py` lines 1691 to 1720, `sampling_limitations.statements`) and renders it in `coroner/viewer/static/evidence-panel.js` (lines 541 to 544, label `COPY.evidence.statements` in `copy.js` line 451). `copy.js` and the Viewer contain no copy of the sentence. The Viewer prints the string as recorded.
- Where it is generated: `coroner/runner/evaluate.py` line 572, in `sampling_limitations()` (starts line 548), a hard-coded third statement.
- Where it is stored: `runs/c-20261120T211433Z-09b81ca1/manifest.json` line 205 (the same text is in the other saved C run; not changed). Every saved C run carries this string, so changing the Runner would not alter saved runs.
- Where it is specified: `.kiro/specs/coroner-m0/design.md` line 848 (the same string), `.kiro/specs/coroner-m0/requirements.md` lines 43 and 264 ("the peak RSS may understate the memory limit"), `README.md` line 541 ("the peak RSS may understate the memory limit"), `docs/m0-results/experiment-results.md` line 269 and `docs/m0-results/m0.6-runner-batch3.md` line 356. `scripts/make-sample-runs.py` line 396 repeats it in the sample data generator.
- Is it wrong: yes. The memory limit is a fixed configured value (134217728 bytes for the saved runs); sampling cannot understate it. What sampling can understate is the highest memory the container actually reached, because the last Sample can predate the kill. In run C the sampled peak (134.55 MiB) is already above the limit, so the sentence also reads oddly beside the chart.
- Proposed fix: replace the sentence with "The peak RSS in the Samples may understate the highest memory the container reached before the kill." The change belongs in `coroner/runner/evaluate.py` line 572, with the M0 design, requirements, README, `scripts/make-sample-runs.py` and any test that asserts the string updated to match.
- Constraints: M1.1 is frozen and the Runner is M0, so this needs reviewer approval and falls outside the Viewer-only scope of M2.4 (Global Rule 4). A Viewer-side rewrite is not appropriate, because the box is labelled "recorded by the Runner" and shows stored text. Saved runs would keep the old sentence unless a run is repeated, which M2 forbids (no Docker).
- Handling for the demo, as in section D: do not scroll to or read that box. The new C chart still does not show it; `07-experiment-c-chart.png` (stale, historical) and `c-1920x9000-full-page.png` (reference render, not for the video) do. If a viewer asks, say the page shows the Runner's stored text as recorded and that the wording is under review.

Check run for section 8: `Test-Path` returned True for every file listed in the table (the two rows marked "no" and "n/a" are human clips and the text card, not files).

## 10. Operator instructions for tasks 3.3 and 3.4

Tasks 3.3 (timed rehearsal) and 3.4 (backup recording) are human-only. Nothing here has been done by an agent, and no observation is filled in.

### Recommended method

Use real saved runs only (A `a-20261120T211146Z-4300f395`, B `b-20261120T211311Z-ce313da1`, C `c-20261120T211433Z-09b81ca1`). Long scrolling on the B and C pages (defect D1) is the main timing risk, so do one of these:

1. Pre-positioned tabs. Open one browser tab per screen below (nine tabs, see the table), load each URL from section 1, scroll each to its start position, and leave it there. Switch with Ctrl+Tab. A tab keeps its scroll position, so no scrolling happens on camera. Use a fresh or incognito Chrome window with only these tabs.
2. Separate short clips. Record each screen as its own Clipchamp clip (pointer movement only, no scrolling), then join the clips on the timeline with hard cuts. Narration is then laid over the joined clips (read live or with ElevenLabs, per segment, per section B).

Either method works with the segment budgets in section 1. Do not use slow scrolling to fill time.

### Clip plan

Tabs or clips, in recording order. "Start" is the heading to scroll to so it sits near the top third of the 1920x1080 frame; the page offsets in section A are approximate (about 100 px), so scroll by heading, not by number.

| Clip | Screen | Start position | Target (s) | Segment |
|---|---|---|---|---|
| 1 | Overview `/` | Page top | 15 | 1 |
| 2 | A incident | Heading "Docker state" | 12 | 2 |
| 3 | B evidence | Heading "Docker state", with the block "Termination as described by the Runner" below it | 15 | 3 (3a) |
| 4 | B RSS timeline | Heading "RSS timeline" | 30 | 3 (3b) |
| 5 | C Docker state | Heading "Docker state", amber banner and the line "The underlying reason for the memory growth is not established by the recorded evidence." in the same frame | 12 | 4 (4a) |
| 6 | C RSS timeline | Heading "RSS timeline" (chart, axis title, axis note, limit line) | 28 | 4 (4b) |
| 7 | A/B/C comparison, headline and table | Page top | 20 | 5 |
| 8 | A/B/C comparison, statements | Heading "Comparison statements" scrolled into the top third; "What these values do not establish" sentence visible if it fits | 10 | 5, then 6 stays here |
| 9 | Kiro's role | Same tab as clip 8 (no new screen), or the optional sanitized `.kiro` spec tree (section A, segment 6) for the last 8 s | 20 | 6 |

Clip 3 target: segment 3a is 15 s. Clips 7 and 8 split the 30 s of segment 5 into about 20 s and 10 s. Clip 9 reuses clip 8's screen for 20 s, so clip 8 plus 9 is 30 s on screen.

### Per clip: what must be visible, what must not

| Clip | Must be visible | Must NOT be visible |
|---|---|---|
| 1 Overview | Heading "Container Crash Forensics", cards for A, B, C | The "Other saved runs" list (do not scroll) |
| 2 A | Docker ExitCode `1` and OOMKilled `false` | Nothing on cause; no other page area is needed |
| 3 B | Docker ExitCode `137`, OOMKilled `false`, the Runner block text `SIGKILL sent by the runner (docker kill --signal=KILL)` | The "Derived intervals (Experiment B)" cards with -10 ms and -80 ms (they must not be paused on or narrated); any exact SIGKILL time such as the "SIGKILL send time (Host_Clock)" row |
| 4 B RSS | Whole chart, axis title "Derived host-equivalent time", axis note, dotted SIGKILL line, both legend lines | The "Recorded times" rows with an exact SIGKILL time, if they fit in the frame (scroll up until they are out) |
| 5 C Docker state | ExitCode `137`, OOMKilled `true`, the amber banner sentence about the out-of-memory kill, and "The underlying reason for the memory growth is not established by the recorded evidence." | The Runner block text ("genuine Docker OOM kill (kernel cgroup memory limit)", defect D5) |
| 6 C RSS | Whole chart, dashed "128 MiB limit" line, final points above it, axis title "Raw Probe Capture_Time", axis note | The dashed "recorded by the Runner: Sampling_Limitations" box, and in particular its sentence "may understate the container memory limit" (defect D6). If any part shows, scroll up until it is gone. |
| 7 Compare, headline | Headline "The same exit code does not always mean the same failure", the three slot cards, the table rows Docker ExitCode and Docker OOMKilled | Nothing special; no cause statements are on this page |
| 8 Compare, statements | The statements "Experiment B and Experiment C share ExitCode 137" and "OOMKilled distinguishes Experiment B (false) from Experiment C (true)" | Nothing special |
| 9 Kiro | Compare page, or the `.kiro` spec tree (GitHub folder view or editor tree with path bar hidden) | A local path, a terminal, an open file, a chat or execution log, the Kiro window |

For every clip, none of these may appear anywhere in the frame: a local path (terminal prompt, file explorer, Clipchamp save dialog), the Windows taskbar, a notification toast, the Chrome profile avatar or a name or email, other browser tabs with private titles, the bookmarks bar, or an error state. Check each clip on playback. If the Sample table or an `original` disclosure opened, redo the clip.

### Backup per screen

If a clip fails or the live Viewer is unavailable, use the matching still from section 8 (full table there). Show the label on screen and say "This is a saved screenshot, not the live Viewer."

| Clip | Backup still | Label on screen |
|---|---|---|
| 1 | `docs/m2-plan/rehearsal-shots/overview-1920x1080-top.png` | automated headless Chrome screenshot, not live |
| 2 | No still exists for the A Docker state (section 8 gap). Use `docs/m1-results/browser-4.7/03-compare-values.png` (the A column of the comparison table), or the A clip from the rehearsal recording. | automated headless Chrome screenshot, not live; or pre-recorded rehearsal clip |
| 3 | `docs/m1-results/browser-4.14/V9-B-incident-1280.png` (top of the B page, includes Docker state and the Runner block, and also the derived-interval cards: do not narrate them) | automated headless Chrome screenshot, not live |
| 4 | `docs/m2-plan/rehearsal-shots/b-chart-1920x1080-viewport.png` (or `docs/m1-results/browser-4.7/06-experiment-b-chart.png`) | automated headless Chrome screenshot, not live |
| 5 | `docs/m2-plan/rehearsal-shots/c-1920x1080-top.png` (page top only; it does not show ExitCode or OOMKilled), then `docs/m2-plan/rehearsal-shots/compare-1920x1080-top.png` (C column: ExitCode 137, OOMKilled true). Do not use `c-1920x9000-full-page.png` (shows the D6 sentence). | automated headless Chrome screenshot, not live |
| 6 | `docs/m2-plan/rehearsal-shots/c-chart-1920x1080-viewport.png` (does not show the Sampling_Limitations box). Do not use `docs/m1-results/browser-4.7/07-experiment-c-chart.png`: it shows the D6 sentence. | automated headless Chrome screenshot, not live |
| 7 | `docs/m2-plan/rehearsal-shots/compare-1920x1080-top.png` | automated headless Chrome screenshot, not live |
| 8 | `docs/m1-results/browser-4.7/04-compare-statements.png` | automated headless Chrome screenshot, not live |
| 9 | The same still as clip 8; the `.kiro` tree has no backup (skip it) | as above |

Do not edit, crop or regenerate the committed stills. To show only the upper part of a tall still, scale and position it in Clipchamp.

### Preparing the positions (before each take)

1. Run section E, "Before recording", steps 1 to 5.
2. In each tab, press Ctrl+F5, scroll to the start position in the table, and check the "must be visible" and "must not be visible" columns in that frame at 100% zoom.
3. Write down the final scroll offsets that worked (the numbers in section A are only a guide).
4. For method 2, record each clip once as a test, play it back, then record it again for the take.

### Evidence check after recording

Run from the repository root, read-only:

`powershell -NoProfile -File scripts\demo-preflight.ps1 -Offline -BaselinePath "$env:TEMP\coroner-m2\demo-baseline.txt"`

Expect exit code 0 and an integrity PASS. Then run `git status --short` and confirm nothing under `runs/`. Write the result in the task 3.3 or 3.4 note by hand. If the baseline file is missing or the check fails, do not recreate or overwrite the baseline to make it pass; report it.

### Recording observations honestly (task 3.3)

The person who rehearses fills these in, from the actual rehearsal, in the task 3.3 record. The agent must not pre-fill, estimate or copy any value from this document.

- Date of the rehearsal.
- Chrome version, from `chrome://version`, and the display resolution and zoom used.
- Method used (pre-positioned tabs or separate clips) and whether the narration was live or synthetic.
- Actual time per segment (1 to 6), measured on the rehearsal recording or with a timer, next to the budget from section 1, and the total.
- Whether the total was under 3:00 (and at or under 2:50).
- Issues seen: any clip redone and why, any place where the frame showed something from the "must NOT be visible" column, any narration line that was hard to say or too long, any scroll position that drifted.
- Any Viewer defect seen on screen, described with the page and the visible text. Record it as a defect to triage in task 5.1. Do not change the Viewer for it during rehearsal.
- Anything that was not tested.

Report only what happened. A segment that ran over its budget is recorded as over budget; it is not rounded to the budget.

### Viewer defects relevant here

No new Viewer defect was found in this review, and no Viewer change is proposed. The existing items remain as listed in section D and section 9: D2 (derived-interval numbers, handled by not narrating or pausing), D3 (the "send time" wording on B), D5 (Runner wording on C), D6 (the Sampling_Limitations sentence, a Runner-stored text that stays in saved C evidence even after the approved M0 fix).

## 11. Final rehearsal checklist (updated narration)

**NOT YET REHEARSED.** Task status: 3.3 NOT DONE, 3.4 NOT DONE, 3.5 NOT STARTED, 3.6 NOT STARTED. Every field marked "human" below is blank on purpose. An agent must not fill it in.

The narration below is the current wording (task 3.1 follow-up). Word counts are exact; seconds are budgets, not measurements. Read each line exactly as written. The total budget is 162 s (2:42), at most 2:50.

### Per clip

| Clip | Narration to read (exact) | Words | Target (s) | Screen and start heading | Must be visible | Must NOT be visible | Backup still |
|---|---|---|---|---|---|---|---|
| 1 | "Two containers can die with the same exit code. Coroner preserves and analyses evidence from container failures. This read-only Viewer starts no containers and does not modify saved runs. Here are three recorded experiments: A, B and C." | 38 | 15 | Overview `/`, page top | Heading, cards for A, B, C | "Other saved runs" list | `rehearsal-shots/overview-1920x1080-top.png` |
| 2 | "Experiment A exited with code 1, and Docker recorded OOMKilled false. That is the baseline. Now B." | 17 | 12 | A incident, heading "Docker state" | ExitCode `1`, OOMKilled `false` | Any cause for A's exit | `m1-results/browser-4.7/03-compare-values.png`, or the A clip |
| 3 | "Experiment B. Docker recorded ExitCode 137 and OOMKilled false. The Runner's own record says it sent SIGKILL with docker kill. Docker did not record an out-of-memory kill for B." | 29 | 15 | B incident, heading "Docker state", Runner block below | ExitCode `137`, OOMKilled `false`, `SIGKILL sent by the runner (docker kill --signal=KILL)` | "Derived intervals" cards (-10 ms, -80 ms); the "SIGKILL send time" row | `m1-results/browser-4.14/V9-B-incident-1280.png` |
| 4 | "The chart plots preserved memory samples on a derived host-equivalent time axis, not independently proven. One timestamp was recorded immediately before the Runner issued the kill command. Another was written after the command returned. They are 425 milliseconds apart, but that does not measure when the signal reached the container. The dotted line marks the first timestamp. Arrival in the container is not recorded. The page does not establish an order across clocks." | 73 | 30 | B incident, heading "RSS timeline" | Whole chart, axis title "Derived host-equivalent time", axis note, dotted SIGKILL line, both legend lines | "Recorded times" rows with an exact SIGKILL time | `rehearsal-shots/b-chart-1920x1080-viewport.png` |
| 5 | "Experiment C. Docker recorded ExitCode 137 and OOMKilled true. The page says this establishes the immediate termination mechanism: an out-of-memory kill." | 21 | 12 | C incident, heading "Docker state" | ExitCode `137`, OOMKilled `true`, amber banner, "The underlying reason for the memory growth is not established by the recorded evidence." | Runner block text (D5) | `rehearsal-shots/c-1920x1080-top.png` then `rehearsal-shots/compare-1920x1080-top.png` (C column) |
| 6 | "The memory limit is 128 MiB. The highest sampled memory is about 134.5 MiB, above that limit. The axis is raw probe capture time, with no correction applied. The page says the underlying reason for the memory growth is not established, and sampling can miss growth after the last preserved sample." | 51 | 28 | C incident, heading "RSS timeline" | Whole chart, dashed "128 MiB limit" line, final points above it, axis title "Raw Probe Capture_Time", axis note | The "Sampling_Limitations" box and its D6 sentence | `rehearsal-shots/c-chart-1920x1080-viewport.png` |
| 7 | "Side by side, B and C share ExitCode 137. OOMKilled is false for B and true for C. That is what separates them. The page headline says: the same exit code does not always mean the same failure." | 38 | 20 | Compare, page top | Headline, slot cards, rows Docker ExitCode and Docker OOMKilled | Nothing special | `rehearsal-shots/compare-1920x1080-top.png` |
| 8 | "The page also says ExitCode and OOMKilled are two separate recorded facts, and neither is inferred from the other." | 19 | 10 | Compare, heading "Comparison statements" in the top third | The two B/C statements; "What these values do not establish" if it fits | Nothing special | `m1-results/browser-4.7/04-compare-statements.png` |
| 9 | "Coroner was built with Kiro's spec workflow. The first two milestones have requirements, design and task lists with review checkpoints, saved in the .kiro folder. Each task's executions are logged there, and the repository has unit and property-based tests." | 39 | 20 | Same tab as clip 8; optional sanitized `.kiro` tree for the last 8 s (section A, segment 6) | Compare page, or the tree | Any local path, terminal, open file, Kiro window | Same as clip 8 (tree has no backup) |

Totals: clips 1 to 9 are 15 + 12 + 15 + 30 + 12 + 28 + 20 + 10 + 20 = 162 s, 325 words (clips 7 and 8 together are segment 5, 57 words in 30 s).

| Segment | Seconds | Words | Words per second |
|---|---|---|---|
| 1 | 15 | 38 | 2.53 |
| 2 | 12 | 17 | 1.42 |
| 3 (clips 3 and 4) | 45 | 102 | 2.27 |
| 4 (clips 5 and 6) | 40 | 72 | 1.80 |
| 5 (clips 7 and 8) | 30 | 57 | 1.90 |
| 6 (clip 9) | 20 | 39 | 1.95 |
| Total | 162 | 325 | 2.01 |

Fallback stills are labelled "automated headless Chrome screenshot, not live" and announced: "This is a saved screenshot, not the live Viewer." Do not use `c-1920x9000-full-page.png` or either `07-experiment-c-chart.png` (stale, historical).

### Before the rehearsal or recording

1. Preflight, from the repository root: `powershell -NoProfile -File scripts\demo-preflight.ps1 -Offline -BaselinePath "$env:TEMP\coroner-m2\demo-baseline.txt"`. Expect exit code 0 and an integrity PASS. If the baseline file is missing or the check fails, do not recreate it; report it.
2. Restart the Viewer in its own window (Ctrl+C the old one, confirm the port is free, start it again as in section E).
3. Hard reload every demo tab (Ctrl+F5) at 100% zoom, in the order of section 1, each at its start heading.
4. Confirm each clip's "must be visible" and "must NOT be visible" columns in the frame.

### After the rehearsal or recording

1. Stop the Viewer (Ctrl+C) and confirm the port is free.
2. Run the same offline preflight again with the same baseline path. Expect exit code 0 and an integrity PASS.
3. `git status --short`: nothing under `runs/`.

### Human results (leave blank until a person fills them in)

| Field | Value |
|---|---|
| Rehearsal date | |
| Chrome version (`chrome://version`) | |
| Display resolution and zoom | |
| Method and voice (live or synthetic) | |
| Actual time per segment 1 to 6 | |
| Actual total | |
| Video duration (player) | |
| Preflight before (exit code, integrity) | |
| Preflight after (exit code, integrity) | |
| Issues seen | |
| Sign-off (name, date) | |
