# Task 4.7 automated Chrome acceptance report

**Revision history.** Original run: sections 1 to 10 below, unchanged (64 checks: 59 PASS, 1 FAIL 4.7-20, 4 NOT TESTED). Rerun 1: section 11 at the end (86 checks: 83 PASS, 0 FAIL, 3 NOT TESTED). Task 4.7 stays in progress. **Second provenance correction (section 11.8): the resubmitted human report, like the first, is NOT a valid human observation. H1 to H7, real 200% browser zoom and the screen reader are all NOT TESTED; both human entries are SUPERSEDED/WITHDRAWN and kept only as history.**

Scope: automated checks in real Google Chrome driven over the DevTools Protocol. **Headless automation only.**
Not performed: human-operated interactive Chrome, headed Chrome, any screen reader, real 200% browser zoom.
Machine-readable results: `task-4.7-results.json` (64 checks, each with expected, actual, status, screenshot, evidence source).

## 1 Environment

| Item | Value |
| --- | --- |
| OS | Microsoft Windows 11 Pro 10.0.26300 |
| Python | 3.12.10 (`.venv`) |
| Chrome | `C:\Program Files\Google\Chrome\Application\chrome.exe`, Chrome/158.0.8127.36, `--headless=new`, isolated temporary profile outside the repository, `--hide-scrollbars` |
| Branch / HEAD | `feat/coroner-forensic-ui` / `618be05e01629df32de8c99c8181dc88c3b59e8e` (viewer work uncommitted on top) |
| Viewer command (real runs) | `.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port <free port>` (final run: 62939; an earlier manual instance used 8791). Port 8765 was already held by a process from an earlier session (PID 2720, global Python, started 7:05 PM). It was not started by this task, so it was left running and not used. |
| Automation | `tests/unit/viewer/cdp_harness.py` (stdlib WebSocket client, Chrome and Viewer launchers, real `Input.dispatchKeyEvent` and mouse events, `Emulation.setDeviceMetricsOverride`, `Page.captureScreenshot`) and `tests/unit/viewer/acceptance_47.py` (scenarios, JSON, nonzero exit on FAIL). Command: `.\.venv\Scripts\python.exe -m tests.unit.viewer.acceptance_47 --out docs/m1-results/browser-4.7` |
| Static files fresh | 4.7-00: SHA-256 of every file under `/static/` served by the launched Viewer equals the file on disk. |
| Viewport | Set with CDP device metrics override (not window size): 1280x900, 480x900, 360x900, DPR 1. |

## 2 Execution summary

| Measure | Value |
| --- | --- |
| Checks | 64 |
| PASS | 59 |
| FAIL | 1 (4.7-20, see below) |
| NOT TESTED | 4 (4.7-16 zoom, 4.7-17 screen reader, 4.7-18 human check, 4.7-19 Partial_View fixture) |
| Checks carrying notes (expected 400/404 logs, favicon 404, control, AX info) | 8 |
| Acceptance command exit code | 1 (because of 4.7-20) |
| `pytest tests/unit/viewer/test_acceptance_47_browser.py` | 12 collected, 12 passed, 0 skipped |
| `pytest tests/unit/viewer tests/unit/test_structure.py -q -rs -p no:cacheprovider` | 1458 collected (1455 + 3 skipped), 1455 passed, 0 failed, 3 skipped, exit 0. Skips: node not on PATH (1), symlink creation (2). A skip is not a pass. |
| `runs/` vs `%TEMP%\coroner-m1\runs-before.snapshot.txt` | 1217 vs 1217 entries, 0 differences |
| Earlier failures this task | The first harness runs had failures caused by harness defects, fixed before the final run: body compared without the trailing line feed (4.7-05), a truncated label match (4.7-09 compare), closed `<details>` rows counted as visible, and text-overlap detection using element boxes. No Viewer file was changed. |

## 3 Test matrix

Source is "real" (saved runs under `runs/`, read-only) unless marked synthetic. JS ran in Chrome for every row except the NOT TESTED rows. Console error count is the number of unexpected console errors, uncaught exceptions, CSP violations, failed or HTTP-error local requests (0 everywhere in the final run). Full expected/actual text for each is in the JSON.

| ID | What | Route | Result | Observed (short) | Screenshot |
| --- | --- | --- | --- | --- | --- |
| 4.7-R00 | Live API values | `/api/runs/<A,B,C>` | PASS | A: ExitCode 1, OOMKilled false, 15 Samples, peak 74326016 B (70.88 MiB). B: 137, false, 15, 74174464 B (70.74). C: 137, true, 45, 141090816 B (134.55). Limit 134217728 B for all. | |
| 4.7-R01 | Overview | `/` HTTP 200 | PASS | three cards with the values above; six other runs listed | 01-overview-desktop |
| 4.7-R02..R04 | Incident A, B, C | `/runs/<id>` HTTP 200 | PASS | Docker ExitCode, OOMKilled, Preserved Samples and "128.00 MiB (134217728 bytes)" as above; Sample table row count equals Samples, maximum row RSS equals the API peak | 06-experiment-b-chart, 07-experiment-c-chart |
| 4.7-01 | Default compare | `/compare` HTTP 200 | PASS | slots A,B,C = the three real ids; four statements (S1 A=1,B=137,C=137; S2; S3 B=false, C=true; S4 Samples 15,15,45); headline shown; "Statements not shown" empty | 02, 03, 04 |
| 4.7-R05 | C above limit, B and C distinct | `/compare` | PASS | C peak 141090816 > 134217728; B (137,false) vs C (137,true) | |
| 4.7-02a,b,c | Change slot A, B, C with real clicks (open "Choose another run", click link) | `/compare` then `?a=`, `?b=`, `?c=` | PASS | slot shows the other real run (`a-...134321Z-a5e5a30e`, `b-...173541Z-3fa52342`, `c-...174958Z-94f4225c`); values equal that run's API; statements equal those built from API values | 13-compare-chose-a/b/c |
| 4.7-03 | Wrong type `a=<B>&b=<B>&c=<C>` | HTTP 200 | PASS | warning names run, "Experiment B", "expects Experiment A"; slot A recorded type B; S1 and S4 listed under "not shown"; S2, S3, headline come from the real B and C | 10-wrong-slot-warning |
| 4.7-04 | Absent valid id `a=a-20261120T211146Z-deadbeef` | HTTP 200 | PASS | "no saved run found for run id ... (no saved run found)"; every slot A cell "no run"; S1 and S4 omitted with the reason; expected API 404 logged by the browser | 11-unavailable-slot |
| 4.7-05.1-4 | `?a=..`, `?x=1`, `?a=`, `?a=<A>&a=<A>` | HTTP 400 | PASS | raw GET, Chrome navigation (status 400, mime text/plain) and in-page `fetch`: `text/plain; charset=utf-8`, body `Invalid compare parameter.` followed by one line feed; rendered text is the message | 12-invalid-query |
| 4.7-06 | Equal OOMKilled (synthetic B false, C false, both 137) | `/compare?c=<fixture>` | PASS | headline absent, no "OOMKilled distinguishes", S2 present, no cause wording | 15-synth-equal-oomkilled |
| 4.7-07a | ExitCode discrepancy on C (synthetic) | | PASS | headline, S1, S2, S3 omitted; "Docker state sources disagree (ExitCode)"; S4 kept | 16 |
| 4.7-07b | OOMKilled discrepancy on C | | PASS | headline and S3 omitted; S1, S2, S4 kept | 17 |
| 4.7-07c | Memory-limit discrepancy on C | | PASS | headline and all four statements kept; discrepancy marker shown; matches Requirement 8.13 (only statements depending on the discrepant field are omitted) | 18 |
| 4.7-07d | ExitCode discrepancy on B | | PASS | headline, S2, S3 omitted | 19 |
| 4.7-07e,f,g | Incident page shows both recorded values | `/runs/<fixture>` | PASS | exit_code 137 vs 1, oom_killed true vs false, memory_limit_bytes 134217728 vs 268435456, each with DISCREPANCY | |
| 4.7-08a | Show more (synthetic, 450 Samples) | `/runs/<fixture>` | PASS | 200 rows + control + "Showing 200 of 450 rows"; click gives 400; click gives 450 and the control is hidden | 20-synth-show-more |
| 4.7-08b.B,C | Show more on real runs | | PASS | all rows shown (15, 45), control hidden |  |
| 4.7-08c | `/compare` | | PASS | the compare page has no Show more control; the design places it only in the incident Sample table |  |
| 4.7-12 | B tick labels | `/runs/<B>` | PASS | `21:13:16`, `21:13:18`, `21:13:20`, equal to the clock part of the API tick labels, at the tick mark x | |
| 4.7-13 | Reduced motion | | PASS | no-preference: transition 0.12s on color, background-color, border-color; reduce: `none` / 0s | |
| 4.7-14 | Contrast | | PASS | see section 6 | |
| 4.7-15 | AX tree (information only) | `/runs/<B>` | PASS | see section 6 | |
| 4.7-20 | Interactive targets at least 44 px high | incident B and C | **FAIL** | `summary` "original" toggles 18 px high; "Sample table (accessible equivalent of the chart)" summary 24 px; overview and compare have none below 44 px | |
| 4.7-16 | 200% browser zoom | | NOT TESTED | DevTools Protocol cannot set browser zoom; page scale, resize and DPR changes are not zoom and were not substituted | |
| 4.7-17 | Screen reader | | NOT TESTED | not performed | |
| 4.7-18 | Human-operated interactive Chrome | | NOT TESTED | not performed | |
| 4.7-19 | Partial_View fixture (dashed segments, legend entry) | | NOT TESTED | needs more than 5000 Samples; outside the requested scope | |

Checks of sections 5 to 7 (4.7-CSP.*, 4.7-09.*, 4.7-10.*, 4.7-11.*, 4.7-RSP.*) are tabulated in their sections.

## 4 Comparison logic verification

Executed in Chrome (real `compare.js` and `compare-logic.js`): 4.7-01 to 4.7-08. All statements and omissions were compared with values fetched from the live API, not with the Python reference model. Observations beyond the matrix:

- The headline appears only when B and C share 137 and OOMKilled differs (real default and 4.7-02, 4.7-03, 4.7-04, 4.7-07c) and is absent in 4.7-06, 07a, 07b, 07d.
- A discrepancy on ExitCode or OOMKilled removes exactly the statements that use the field; an unrelated memory-limit discrepancy removes none. No spec conflict was found.
- On `/compare` a discrepant slot shows the effective value and "disagree on: <field>"; both source values are shown on the incident page (4.7-07e,f,g).
- Invalid-query body: the message plus one line feed. `test_compare_query.py` pins this (`FIXED_400`); it is recorded as an observation, not a defect.

## 5 Console and CSP verification

For `/`, the three incident pages and `/compare`: capture was attached before navigation (`Runtime`, `Log`, `Network`, and a `securitypolicyviolation` listener installed with `Page.addScriptToEvaluateOnNewDocument`), the page rendered, then interactions were made (Tab walk, all `<details>` opened, chart keys, Show more) and the capture read again. Result for all five pages: 0 console errors, 0 uncaught exceptions, 0 CSP violations, 0 failed or HTTP-error local requests, no "The page could not be displayed." fallback (4.7-CSP.overview, incident-A, incident-B, incident-C, compare).

Positive control 4.7-CSP.control: a deliberately injected inline script was blocked and reported as a CSP violation, and a deliberate `console.error` and uncaught exception were captured, so the zero counts are not an artefact of the capture.

Expected browser messages, kept apart from errors: the 404 for `/api/runs/<absent id>` (4.7-04), the Chrome log lines for the four 400 responses (4.7-05), and `/favicon.ico` 404 (seen on the synthetic 4.7-06 page; the Viewer ships no favicon). The favicon 404 is a cosmetic warning and is not counted as an error.

## 6 Keyboard and accessibility verification

All with real `Input.dispatchKeyEvent`.

- 4.7-09 Tab and Shift+Tab (overview, B incident, compare): the skip link is the first stop; Overview, Compare, page links ("Open incident detail", "Back to overview", "Choose another run for slot") are reached in document order; every stop has a solid 3 px outline `rgb(245, 176, 65)` and is inside the viewport; the walk ends after the last element (no trap); Shift+Tab visits the same stops in reverse. Skip link: on first Tab it is on screen (top >= 0), Enter sets `#main` and focus moves to `<main>` (`08-keyboard-focus.png`).
- 4.7-10 chart (B: 15 Samples, C: 45): Tab reaches `.chart-plot` as one stop (19 presses on B); focusing selects Sample 0; Right, Right, Left, End, Right (stays at last), Home, Left (stays at first) each gave the readout of the expected Sample, whose seq, Capture_Time, RSS MiB and bytes equal the API point; the next Tab leaves the chart, Shift+Tab returns to it; 0 extra tab stops inside the chart. `08b-chart-keyboard-end-b.png` shows B at the last Sample.
- 4.7-11 Sample table (B and C): focused summary shows the outline; Enter opens it, rows (15 and 45) are visible and equal the API (seq, original Capture_Time, RSS MiB, RSS bytes); the Show more control is hidden; Space closes it. `09-expanded-sample-table.png`.
- 4.7-13 reduced motion: confirmed (Emulation.setEmulatedMedia).
- 4.7-14 contrast, method: WCAG 2.x, ratio = (L1+0.05)/(L2+0.05), L = 0.2126 R + 0.7152 G + 0.0722 B after sRGB linearisation; foreground from `getComputedStyle` (`fill` for SVG text) composited over the stacked ancestor backgrounds. Lowest text ratio per page: overview 6.51, incident B 6.51, incident C 6.51, compare 7.38 (0 of 79, 530, 506 and 121 texts below 4.5). Token ratios on bg / surface / surface-2: text 15.04 / 13.71 / 12.09; muted 8.10 / 7.38 / 6.51; amber 9.63 / 8.78 / 7.75; red 7.18 / 6.55 / 5.78; purple 8.65 / 7.88 / 6.96; border 2.30 / 2.10 / 1.85. These agree with the design table to within rounding.
- 4.7-15 accessibility tree (`Accessibility.getFullAXTree`, B incident): 1609 nodes; one `application` node named "RSS timeline chart. Use the Left and Right arrow keys ..."; 26 headings. **This is not a screen reader test.**
- Finding 4.7-20 (FAIL): the design (Visual system, Layout) says interactive targets are at least 44 px high. The `original` disclosure summaries and the Sample table summary on incident pages are 18 to 24 px high. Overview and compare pass. No fix was made: enlarging inline disclosure toggles changes the reviewed visual design, so it needs a decision.

## 7 Responsive layout measurements

CDP `Emulation.setDeviceMetricsOverride` gave true 1280, 480 and 360 px layout viewports (`clientWidth` read back as 1280, 480, 360). In every case (overview, B, C, compare; 12 checks, all PASS): `documentElement.scrollWidth` equals `clientWidth` (no page-wide horizontal overflow), 0 elements outside the viewport except inside scroll containers, 0 clipped elements, 0 overlapping text line boxes (measured with `Range.getClientRects`).

| Page | Width | Observed |
| --- | --- | --- |
| Compare | 1280 | 3 slot columns, table not scrolling |
| Compare | 480 / 360 | 1 slot column; table wrapper scrolls (scrollWidth 640 in 312 px at 360) |
| B incident | 480 / 360 | chart frame scrolls (scrollWidth 640; client 422 / 302); scrolled to the end, the SIGKILL line lies inside the frame |
| C incident | 480 / 360 | chart frame scrolls; the last point is inside the frame at full scroll |
| B, C | 1280 | chart frame does not scroll (1142 px) |

Narrow screenshots: `05-compare-narrow.png` (480), `14-*-480.png`, `14-*-360.png` (viewport captures).

## 8 Screenshots and evidence index

All in this folder, real Chrome 158 renders, headless, DPR 1. Real = saved runs A `a-20261120T211146Z-4300f395`, B `b-20261120T211311Z-ce313da1`, C `c-20261120T211433Z-09b81ca1` (and the alternates named in 4.7-02). Synthetic = scratch Runs_Directory under `%TEMP%`, removed after the run.

| File | Test | Route | Viewport | Source |
| --- | --- | --- | --- | --- |
| 01-overview-desktop.png | R01 | `/` | 1280 | real |
| 02-compare-desktop.png | 01 | `/compare` | 1280 | real |
| 03-compare-values.png, 04-compare-statements.png | 01 | `/compare` | 1280 (viewport) | real |
| 05-compare-narrow.png | RSP.compare.480 | `/compare` | 480 | real |
| 06-experiment-b-chart.png, 07-experiment-c-chart.png | R03, R04 | incident B, C | 1280 (chart section) | real |
| 08-keyboard-focus.png | 09.skip | `/` | 1280 | real |
| 08b-chart-keyboard-end-b.png | 10.B | B incident | 1280 | real |
| 09-expanded-sample-table.png, 09b-...-viewport.png | 11.B | B incident | 1280 | real |
| 10-wrong-slot-warning.png | 03 | `/compare?a=<B>&b=<B>&c=<C>` | 1280 | real |
| 11-unavailable-slot.png | 04 | `/compare?a=<absent>` | 1280 | real |
| 12-invalid-query.png | 05.1 | `/compare?a=..` | 1280 | real |
| 13-compare-chose-a/b/c.png | 02a,b,c | `/compare?...` | 1280 | real |
| 14-overview/incident-b/incident-c/compare-{480,360}.png | RSP | | 480, 360 | real |
| 15-synth-equal-oomkilled.png | 06 | | 1280 | synthetic |
| 16 to 19-synth-*.png | 07a to 07d | | 1280 | synthetic |
| 20-synth-show-more.png | 08a | | 1280 | synthetic |
| task-4.7-results.json | all | | | |

## 9 Unverified items and limitations

- Human-operated interactive Chrome: not performed. Headed Chrome: not performed (the `--headed` option exists, unused).
- Screen reader: not performed. The AX tree record is not equivalent.
- 200% browser zoom: NOT TESTED (not controllable over CDP).
- Partial_View fixture dashed segments and legend: NOT TESTED.
- Hostile text on a scratch copy (V27) and the error/Retry/empty states (V22 to V25) were outside this request and are untouched.
- Headless Chrome with hidden scrollbars; visual judgements (readability, overlap beyond measured boxes) come from measurements and the screenshots only.
- Failing 4.7-20 means the design's 44 px target rule is not met on incident pages.
- The Viewer on 8765 that already existed was not tested and may not serve the current static files.
- Tasks 4.8 to 4.14 and optional 4.4/4.11 were not touched; `tasks.md` and the spec files were not edited.

## 10 Final Task 4.7 recommendation

Automated Chrome acceptance is complete for the requested scope: 59 PASS, 1 FAIL, 4 NOT TESTED, with no console, CSP, request or fallback problem and no change to `runs/`. **Do not mark Task 4.7 complete yet.** The task requires a user-performed (or user-browser) smoke test and a 200% zoom check, which remain:

1. A person runs the nine-step smoke test in interactive Chrome and records the browser version and observed text.
2. 200% browser zoom (and a 360 px real window) by a person.
3. Screen reader check only if the user performs one; otherwise record "not run".
4. Decision on 4.7-20: accept the small `summary` toggles as a documented deviation, or enlarge them (a CSS-only change plus a browser regression test) and rerun this suite.
5. Optional: Partial_View fixture check.

## 11 Rerun 1 (after the 4.7-20 fix; Partial_View added)

Sections 1 to 10 are the original run and are kept as written, including the failed 4.7-20. Rerun 1 is recorded here and in `task-4.7-results.json` (`rev_history`, `reruns[0]`; the original `results` array is untouched). Rerun files: `rerun-1/task-4.7-rerun-results.json` and the screenshots in `rerun-1/` (34 PNG files; the original screenshots in this folder are unchanged).

### 11.1 Run facts

| Item | Value |
| --- | --- |
| Chrome | Chrome/158.0.8127.36, `--headless=new`, isolated temporary profile outside the repository (started and stopped by the harness; the user's own Chrome was not touched) |
| Command | `.\.venv\Scripts\python.exe -m tests.unit.viewer.acceptance_47 --out docs/m1-results/browser-4.7/rerun-1 --rerun "Rerun 1" --merge-into docs/m1-results/browser-4.7/task-4.7-results.json` |
| Acceptance exit code | 0 |
| Checks | 86: 83 PASS, 0 FAIL, 3 NOT TESTED (4.7-16 zoom, 4.7-17 screen reader, 4.7-18 human check). 4.7-19 is no longer NOT TESTED (section 11.3). |
| Console errors, uncaught exceptions, CSP violations, failed requests | 0 in every check (sum of `console_error_count` = 0). 4.7-CSP.overview, incident-A, incident-B, incident-C, compare: PASS; positive control 4.7-CSP.control: PASS. Expected notes only: favicon 404, 400/404 logs of the error-path checks. |
| Static files fresh | 4.7-00 PASS (SHA-256 of every served file equals the file on disk, including the changed `style.css`) |
| Ports | Viewer on free ports chosen by the harness. Port 8765 (PID 2720, earlier session) was not used and not stopped. |
| Focused pytest | `pytest tests/unit/viewer tests/unit/test_structure.py --collect-only`: 1460 tests collected (exit 0). Run `-q -rs -p no:cacheprovider`: 1457 passed, 0 failed, 3 skipped (node not on PATH 1, symlink creation 2), 1 warning (starlette deprecation), about 80 to 93 s. The pytest summary shows no failure; the process exit code is therefore 0 (the tool output did not echo the code of the last run; the first run and the collect run were exit 0). Before this correction: 1458 collected, 1455 passed, 3 skipped. The two new tests are `test_every_interactive_target_is_at_least_44_px_in_chrome` and `test_partial_view_fixture_in_chrome`. |
| `runs/` unchanged | 1217 entries now, 1217 in `%TEMP%\coroner-m1\runs-before.snapshot.txt`, 0 differences (path, SHA-256, size). |

### 11.2 Decision 1: touch targets (4.7-20), fixed

Cause: `.original summary` and `.sample-table summary` had no height rule (the other target classes already had `min-height: 44px`).

CSS change in `coroner/viewer/static/style.css` (no JS, no API, no recorded value changed; native `<details>`/`<summary>`, the disclosure marker and the existing 3 px amber `:focus-visible` outline are kept):

- `.original`: `vertical-align: middle`. `.original summary`: `box-sizing: border-box; min-block-size: 44px; min-inline-size: 44px; padding-block: 13px; padding-inline: 4px`.
- `.sample-table summary`: `box-sizing: border-box; min-block-size: 44px; padding-block: 10px`.
- `.table .text-link { white-space: nowrap }`: found by the width check below. At 360 px the run id link in the overview "Other saved runs" table wrapped character by character into a 38 x 168 px box. Now the table scrolls inside `.table-wrap` instead.
- `.page > * { min-width: 0 }`: my first version of the `nowrap` rule made the overview wider than the viewport at 480 and 360 px (a defect I introduced; the new browser test caught it before the rerun). This rule lets the grid item shrink so the table scrolls inside its wrapper. Result: no page-wide overflow at any width (4.7-RSP.* 12 of 12 PASS).

Measured bounding boxes (`getBoundingClientRect` in Chrome, details closed and opened, 1280, 480 and 360 px, pages overview, incident A, B, C and compare):

| Target | Before (original run) | After (Rerun 1, smallest over all pages and widths) |
| --- | --- | --- |
| `summary` "original" (incident pages, 6 to 19 per page) | 168.8 x 18 px (opened) | 60.6 x 44 px |
| `summary` "Sample table (accessible equivalent of the chart)" | 24 px high | 286 x 44 px |
| run id link in "Other saved runs" at 360 px | 38.2 x 168 px | 118.7 x 44 px (text-link) |
| `summary` chooser (compare) | 44 px | 278 x 44 px |
| `a.brand`, `a.nav-link`, `a.skip-link`, `a.button`, `button.button` | at least 44 px high | 109.6 x 44, 80.5 x 44, 183.1 x 44, 163 x 44, 105.8 x 44 |
| chart plot (focusable `div.chart-plot`) | not listed | 304 x 242 px |

Results: 4.7-20 (original check, inline height rule) PASS; 4.7-20.measured.real PASS; 4.7-20.measured.partial PASS (0 targets under 44 x 44 px, 0 overlapping target boxes, no page overflow, clipped or overlapping text); 4.7-20.behaviour PASS (real mouse click opens, Space closes, Tab focus shows a solid 3 px `rgb(245, 176, 65)` outline on a 61 x 44 px box, Enter opens). Screenshots: `rerun-1/21-original-toggle-focus.png`, `rerun-1/14-*-480.png`, `14-*-360.png`. Visual change: the "original" line is 26 px taller (padding), text and marker unchanged.

The chart, compare, layout and no-overflow browser tests (`test_chart_browser.py`, `test_compare_browser.py`, `test_acceptance_47_browser.py`, `test_static_chart_regression.py`) all pass in the full run. No existing static test pinned the old CSS, so none was changed.

New regression tests (skip with a reason when no Chrome is found): in `tests/unit/viewer/test_acceptance_47_browser.py`, `test_every_interactive_target_is_at_least_44_px_in_chrome` measures every target at 1280, 480 and 360 px, requires at least 44 x 44 px, no overlaps and no overflow, and checks the toggle's native behaviour; `test_partial_view_fixture_in_chrome` runs the ten Partial_View checks.

### 11.3 Decision 2: Partial_View (4.7-19), mandatory, now measured

Fixture (generated by `build_partial_fixture` with `tests/unit/viewer/builder.py`, approved M0 formats, in a temporary folder outside the repository and outside `runs/`; deleted after the run): Experiment C run `c-20261120T211433Z-000000ff`, 6945 Sample records (seq 0 to 6999 minus two known gaps), 2.6 MB (limit 16 MiB), 20 Samples with null RSS (seq 100 to 119), 6925 plottable Samples, two `sequence_discontinuity` Gap_Records (missing seq 2000 to 2049 and 5500 to 5504). Two equal highest RSS values (200000000 B at seq 3305 and 4405); the earliest, 3305, is not on the uniform stride. A second fixture (`...000000fe`) has the equal peaks at seq 3333 and 4444, where 3333 is on the stride. A separate Viewer was started on a free port for each; all checks ran in Chrome. Expected values come from the fixture definition, not from the Viewer.

| Check | Result | Observed (main fixture) |
| --- | --- | --- |
| 1 Partial_View is true | PASS | `partial_view` true, `chart_cap` 5000; label "Partial view: valid Samples 6945, plotted 5000"; chart description "Partial view: yes" |
| 2 totals describe the full evidence | PASS | total_samples 6945, plottable_samples 6925, unplottable 20, `sample_count` 6945; page shows "Samples with no valid RSS (not drawn): 20" |
| 3 plotted_samples | PASS | 5000 (= `len(points)`, label, chart description and Sample table caption agree) |
| 4 first, last, earliest peak represented | PASS | first plotted seq 0, last 6999, earliest peak 3305 plotted; Home and End show seq 0 and 6999 in the readout; strictly increasing; only fixture Samples |
| 5 full-run RSS statistics unchanged | PASS | first 60000000 (seq 0), highest 200000000 (seq 3305), last 75425081 (seq 6999), y_data_max 200000000, equal to the fixture definition; the page shows 200000000 |
| 6 dashed segments across omitted observations | PASS | 1924 dashed and 3073 solid segments drawn; API values equal an independent computation from the full sequence for all 5000 points; dashed polylines have `stroke-dasharray` 8 5 (computed `8px, 5px`), solid ones `none` |
| 7 legend | PASS | "dashed: omitted observations between these points" with a dashed swatch |
| 8 known gaps never joined | PASS | `gap_before` at the two pairs seq (1999, 2051) and (5499, 5505), `segment_before` none, two "gap" marks drawn, 0 drawn segments span a gap mark; null-RSS omissions (seq 100 to 119) are dashed, not gaps |
| 9 no JS errors or CSP violations | PASS | 0 console errors, 0 exceptions, 0 CSP violations, 0 failed requests after Home, End and opening the Sample table (favicon 404 only, a warning) |
| 10 tick positions and labels | PASS | ticks 21:20:00 to 22:10:00 every 10 min inside the domain; labels equal the clock part of the API labels and sit at their tick marks; tick marks are linear in time (max residual 0.008 px) and place the first and last plotted points (0.005 and 0.002 px) |

Observation, not a defect: with the peak already on the uniform stride (second fixture) the selection has 4999 points, not 5000, because the design adds the peak only when it is missing (`select_points`: "at most cap"; Property 1 says at most `cap`). All ten checks pass for that fixture too (ids `4.7-19.stride.1` to `.10`: plotted 4999). No defect was found in Partial_View, so no Viewer change was made for it.

Screenshots (`rerun-1/`): `22-partial-view-incident.png` (viewport), `23-partial-view-chart.png` (chart, notes, legend), `24-partial-view-gap-closeup.png` (3x close-up: the line ends before the dotted gap mark and restarts after it; a dashed run next to the gap mark is visible on the right).

Defects found in this rerun, with fixes: (a) the two summary controls were under 44 px (fixed, section 11.2); (b) the run id link in the overview table at 360 px was 38 px wide (fixed); (c) harness only: the first Partial_View chart screenshot included the opened Sample table, so the screenshot step was moved before the table is opened. No other defects.

### 11.4 Deviations and limits

- No permanent deviation is accepted for 4.7-20.
- Headless Chrome only. 4.7-16 (real 200% zoom) stays NOT TESTED by automation: the DevTools Protocol cannot set browser zoom, and a resize, page scale or device pixel ratio change was not substituted. 4.7-17 (screen reader) stays NOT TESTED: no screen reader was run, and the accessibility-tree record (4.7-15) is not a substitute. 4.7-18 (a person in an interactive Chrome) is NOT TESTED.
- The Viewer already on port 8765 (PID 2720) may still be serving the old `style.css`; restart it before the human check (section 11.5).

### 11.5 Human checklist (to be performed by the user in their normal Chrome)

**Provenance correction (see section 11.7): every H1 to H7 result below is NOT TESTED. The PASS entries recorded earlier in this table were withdrawn because they were not human observations of the user's interactive Chrome.** They are kept in the cells only as history, marked SUPERSEDED/WITHDRAWN. No Chrome version, date, URL, screenshots or observations from a person were ever provided, and none are recorded. Headless automation (sections 1 to 11.4) does not equal human-operated verification.

First restart the Viewer so that it serves the current static files. If the process on port 8765 is the old one from this project, stop it yourself (it was not stopped here), then run from the repository root:

```
.\.venv\Scripts\python.exe -m coroner.viewer --runs-dir runs --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/` and hard-reload (Ctrl+Shift+R).

| # | Step | Expected | Result |
| --- | --- | --- | --- |
| H1 | Open Overview | three cards A, B, C with ExitCode 1, 137, 137, OOMKilled false, false, true and Samples 15, 15, 45; "Other saved runs" table | NOT TESTED. SUPERSEDED/WITHDRAWN earlier entry: "PASS (user report: Overview shows A, B and C runs)"; not a human observation, see 11.7 |
| H2 | Open Experiment B (card "Open incident detail") and look at the RSS chart | RSS line, dashed amber memory limit line at 128 MiB, SIGKILL marker, readable tick labels | NOT TESTED. SUPERSEDED/WITHDRAWN earlier entry: "PASS (user report: chart renders with the SIGKILL marker)"; not a human observation, see 11.7 |
| H3 | Open Experiment C | the RSS peak 134.55 MiB (141090816 bytes) is drawn above the 128 MiB limit line | NOT TESTED. SUPERSEDED/WITHDRAWN earlier entry: "PASS (user report: RSS shown above the 128 MiB limit)"; not a human observation, see 11.7 |
| H4 | Open Compare | B and C are visibly distinct: both ExitCode 137, OOMKilled false for B and true for C; headline and four statements | NOT TESTED. SUPERSEDED/WITHDRAWN earlier entry: "PASS (user report: ExitCode 137 for B and C, distinct OOMKilled values)"; not a human observation, see 11.7 |
| H5 | Check the "original" controls and the "Sample table" control on an incident page | each is easy to click (tall hit area), opens and closes, focus ring visible with the keyboard | NOT TESTED (never performed by a person; includes how easy the "original" and Sample table controls are to click; the automated 44 px bounding-box measurement of Rerun 1 stays automated evidence only and does not substitute for H5) |
| H6 | In Chrome use Zoom 200% (Ctrl and +, or the menu) on Overview, an incident page and Compare | text readable; navigation works; nothing overlaps; the chart scrolls sideways inside its frame by design and the page does not | NOT TESTED. SUPERSEDED/WITHDRAWN earlier entry: "PASS (user report: at actual 200% Chrome zoom, content remains usable and the chart scrolls sideways; real browser zoom, human-operated)"; not a human observation, see 11.7. Real 200% browser zoom (4.7-16) is NOT TESTED again |
| H7 | Open DevTools (F12) Console on `/`, B, C and `/compare` | no red errors and no "Refused to ..." CSP messages (a `/favicon.ico` 404 is expected and cosmetic) | NOT TESTED. SUPERSEDED/WITHDRAWN earlier entry: "PASS (user report: DevTools Console has no JavaScript errors or CSP violations)"; not a human observation, see 11.7 |

Never provided: Chrome version, date, URL, screenshots or observations from a person. Screen reader: NOT TESTED (reason: no screen reader was operated).

**SUPERSEDED/WITHDRAWN (see section 11.8): every PASS in this resubmission table is invalid and not counted. Current status of H1 to H7 is NOT TESTED.** The text and table below are kept as history only.

**Human report resubmitted after the provenance correction (new entry; the table above and section 11.7 are history and are kept unchanged).** Source: the user's own report, submitted after the provenance correction, stated by the user to be human-operated in normal interactive Chrome. The assistant did not observe it and cannot independently verify it. Chrome version as given by the user: 158.0.8127.36. This equals the version of the headless Chrome used by the automation; nothing is inferred from that. Test date: not provided. URL: not provided. Screenshots: not provided. Free-text observations: not provided.

| # | Step | Status as originally recorded (SUPERSEDED/WITHDRAWN, not valid; current status NOT TESTED) | User's wording |
| --- | --- | --- | --- |
| H1 | Open Overview | PASS (WITHDRAWN; current status NOT TESTED) | "H1 PASS Overview" |
| H2 | Open Experiment B | PASS (WITHDRAWN; current status NOT TESTED) | "H2 PASS Experiment B" |
| H3 | Open Experiment C | PASS (WITHDRAWN; current status NOT TESTED) | "H3 PASS Experiment C" |
| H4 | Open Compare | PASS (WITHDRAWN; current status NOT TESTED) | "H4 PASS Compare" |
| H5 | Touch controls | PASS (WITHDRAWN; current status NOT TESTED) | "H5 PASS Touch controls" |
| H6 | Chrome 200% zoom | PASS (WITHDRAWN; current status NOT TESTED) | "H6 PASS Chrome 200% zoom" |
| H7 | DevTools Console | PASS (WITHDRAWN; current status NOT TESTED) | "H7 PASS DevTools Console" |
| - | Screen reader | NOT TESTED | no screen reader was operated |

These PASS entries are withdrawn (section 11.8) and are not valid human observations; they are not counted as passed. They rested on the user's statement alone. The automated results, the rerun history, the automated 44 px measurements and the Partial_View verification remain automated evidence and are kept separate from these human results.

### 11.6 Remaining before Task 4.7 can be marked complete, and recommendation

**Current state (corrected by section 11.8): H1 to H7 NOT TESTED for human-operated Chrome, real 200% browser zoom NOT TESTED, screen reader NOT TESTED. Task 4.7 stays IN PROGRESS. The text in this subsection from here to the end of 11.6 is SUPERSEDED/WITHDRAWN history where it presents H1 to H7 as PASS.**

Remaining limitations: screen reader NOT TESTED; human screenshots, URL and test date not provided; no human free-text observations.

SUPERSEDED/WITHDRAWN recommendation (invalid, see 11.8): automated acceptance is complete (original run 59 PASS, 1 FAIL 4.7-20 later fixed; Rerun 1: 86 checks, 83 PASS, 0 FAIL, 3 NOT TESTED). Human checks H1 to H7 were reported PASS by the user (withdrawn: not valid human observations). The earlier conclusion that Task 4.7 can be closed by the orchestrator is withdrawn, with the limitation list above. This agent did not mark it complete; `tasks.md` and the spec files were not edited.

History (before the resubmission, kept unchanged):

Human checks H1, H2, H3, H4, H5, H6 and H7: all NOT TESTED. Real 200% browser zoom (H6, 4.7-16): NOT TESTED. Screen reader: NOT TESTED (no screen reader was operated).

Outstanding, to be performed by a person in their normal interactive Chrome: H1 to H7, including real 200% browser zoom (H6), the DevTools Console check (H7), and how easy the "original" and Sample table controls are to click (H5). The screen reader check remains NOT TESTED (optional). The automated 44 px measurements and the Partial_View verification are automated evidence only. The headless automation does not equal human-operated verification. Chrome version, date, URL, screenshots and observations from a person were never provided.

Recommendation: Task 4.7 stays IN PROGRESS. Automated Chrome acceptance (Rerun 1: 86 checks, 83 PASS, 0 FAIL, 3 NOT TESTED) is unchanged. Do not mark Task 4.7 complete until a person has performed H1 to H7 and recorded the Chrome version, date and observed text. `tasks.md` and the spec files were not edited.

### 11.7 Provenance correction (history)

- What was recorded: after Rerun 1, the entries H1, H2, H3, H4, H6 and H7 were written as PASS ("user report"), H5 as NOT TESTED, and 4.7-16 (200% zoom) as covered by H6. This was relayed from a user chat message.
- Why it was invalid: independent review established that the message was not a set of human observations of the user's interactive Chrome. It was incorrectly interpreted from a suggested checklist. No person's observations, Chrome version, date, URL or screenshots exist.
- Correction: those entries were withdrawn. H1 to H7 are NOT TESTED, real 200% zoom is NOT TESTED, the screen reader stays NOT TESTED. The withdrawn entries remain in the table above as marked history and are not counted anywhere as passed.
- Unchanged: the original 64-check run (59 PASS, 1 FAIL 4.7-20, 4 NOT TESTED) and Rerun 1 (86 checks, 83 PASS, 0 FAIL, 3 NOT TESTED) automated results, the automated 44 px measurements and the Partial_View verification.

#### Resubmission after the correction (SUPERSEDED/WITHDRAWN by section 11.8; kept as history, the correction above is not changed)

**This resubmission is not a valid human observation. Its PASS results are withdrawn and not counted.**

- Source: the user's own report, submitted after the provenance correction. The user states it is human-operated, in normal interactive Chrome. The assistant did not observe it and cannot independently verify it.
- Content (WITHDRAWN, not valid), verbatim from the user: "H1 PASS Overview; H2 PASS Experiment B; H3 PASS Experiment C; H4 PASS Compare; H5 PASS Touch controls; H6 PASS Chrome 200% zoom; H7 PASS DevTools Console. Chrome version: 158.0.8127.36."
- Recorded (WITHDRAWN, see 11.8): H1 to H7 each PASS (user-reported), with the user's short wording only. Chrome version 158.0.8127.36 as given by the user; it equals the version of the headless Chrome used by the automation, and nothing is inferred from that.
- Not provided by the user, so recorded as "not provided": test date, URL, screenshots, free-text observations.
- Screen reader: stays NOT TESTED (no screen reader was operated).
- Unchanged: the withdrawn earlier entries, the provenance correction, the automated results (original 64 checks; Rerun 1: 86 checks, 83 PASS, 0 FAIL, 3 NOT TESTED), the automated 44 px measurements and the Partial_View evidence, which stay automated evidence separate from the human results.
- Remaining limitations: screen reader NOT TESTED; human screenshots, URL and date not provided; no human free-text observations.

### 11.8 Second provenance correction (new entry; 11.5 to 11.7 are history and are kept)

- What was recorded: the resubmission "Coroner human Chrome verification" recorded H1 to H7 as PASS (user-reported, human-operated), with Chrome 158.0.8127.36 as user-reported. This is the second set of PASS entries, after the first set that was already withdrawn.
- Why it is invalid: the user states that these H1 to H7 PASS entries are NOT valid human observations. They were not produced by a person operating interactive Chrome.
- Correction: the resubmission is SUPERSEDED/WITHDRAWN together with the first set. Current status: H1 to H7 NOT TESTED for human-operated Chrome; actual browser zoom at 200% (4.7-16, H6) NOT TESTED; screen reader NOT TESTED. The Chrome version 158.0.8127.36 given in the resubmission is not recorded as a human observation. No human date, URL, screenshot, version or observation exists.
- Unchanged valid automated evidence (headless Chrome 158.0.8127.36 over the DevTools Protocol): the original 64-check run (59 PASS, 1 FAIL 4.7-20, 4 NOT TESTED); Rerun 1 (86 checks, 83 PASS, 0 FAIL, 3 NOT TESTED); automated touch targets at least 44 px; Partial_View verification on a synthetic fixture; no console errors, exceptions or CSP violations reported by the automation.
- Task 4.7 stays IN PROGRESS. Do not mark it complete until a person has performed H1 to H7 in interactive Chrome and recorded the Chrome version, date and observed text. `tasks.md` and the spec files were not edited.
