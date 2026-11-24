# M1.1-B closeout (Phase 0)

Branch `feat/coroner-forensic-ui`, HEAD `618be05`. All Viewer work is uncommitted (untracked). No Docker, no experiments, no access to the real `runs/` directory, no M0 production edits, no new dependencies, no commit.

## (a) Chart fallback fix

### Defect

When an Experiment B chart could not apply the clock correction (`ts - offset` outside the `datetime` range), `timeline.build_chart` fell back to `raw_uncorrected` with reason `correction_out_of_range`, but:

- it still added the SIGKILL x position to the x-axis domain (the domain and ticks stretched to the SIGKILL time on a raw axis);
- `model._ser_termination_markers_block` still returned `sigkill.drawn: true` with an `x_ms`, because it looked only at the offset resolution;
- `model._ser_timing_block` already reported `time_axis: raw_uncorrected` and `correction_unavailable_reason: correction_out_of_range`, but computed the derived intervals with the usable-offset rule (`sample_to_*` intervals on a correction that could not be applied).

### Fix (production, `coroner/viewer/` only)

`build_chart` already reports the fallback through `Chart.time_axis` and `Chart.correction_unavailable_reason`. The serializers now honour it.

1. `coroner/viewer/timeline.py`, `build_chart`, the fallback `except` branch:

   Before:
   ```python
           except (OverflowError, ValueError, OSError):
               actual_mode = "raw_uncorrected"
               actual_offset = None
               correction_unavailable_reason = "correction_out_of_range"
   ```
   After:
   ```python
           except (OverflowError, ValueError, OSError):
               actual_mode = "raw_uncorrected"
               actual_offset = None
               correction_unavailable_reason = "correction_out_of_range"
               # The SIGKILL marker is not drawn on a raw axis, so its x position
               # must not widen the x-axis domain or add ticks.
               sigkill_x_ms = None
   ```
   A call with a drawn marker and a usable correction is unchanged, so `test_x_axis_domain_contains_every_point_and_the_sigkill_x` keeps its meaning.

2. `coroner/viewer/model.py`, `_ser_termination_markers_block`: a new branch before `sigkill_time_not_recorded`:
   ```python
       elif (
           record.chart_data is not None
           and record.chart_data.correction_unavailable_reason == "correction_out_of_range"
       ):
           # The chart fell back to the raw axis; no corrected position exists.
           not_drawn_reason = "correction_out_of_range"
   ```
   Result: `drawn: false`, `x_ms: null`. `time` and `time_display` keep the recorded string, and `text_times` keeps the `sigkill_sent` row (clock `host`).

3. `coroner/viewer/model.py`, `_ser_timing_block`: when the reason is `correction_out_of_range`, `derived_intervals` receives an `OffsetResolution` with `offset_ms=None` (and the same `source`, no conflict), so the design rule for B without correction applies: only `finished_at_to_sigkill`, no `sample_to_*`. `clock_offset_ms` and `clock_offset_source` still report the recorded offset unchanged. `OffsetResolution` was added to the existing local import from `coroner.viewer.clocks`.

Original evidence is never modified; the raw rows are only read.

Design note for the reviewer: `termination_markers.sigkill.not_drawn_reason` now has a fifth value, `correction_out_of_range` (design lists `not_experiment_b`, `offset_not_recorded`, `offset_conflict`, `sigkill_time_not_recorded`). It reuses the existing `timing.correction_unavailable_reason` code. The UI does not read `not_drawn_reason`. The design text was not edited.

### Regression test

`tests/unit/viewer/test_chart_fallback.py` (new, 5 tests). The fixture builds a synthetic Experiment B run under `tmp_path` with `clock_offset_ms = 2**53 - 1` (a valid `safe_int`; `ts - offset` is far before `datetime.min`), loads it with `load_detail`, and serialises with `to_detail` + `dump_json`. It asserts:

- `build_chart` called directly with `mode="corrected"` and a SIGKILL x: `time_axis == raw_uncorrected`, reason `correction_out_of_range`, every `corrected_capture_time` `None`, `x_ms` equal to the raw Capture_Time epoch ms, domain exactly `[min(x), max(x)]` and every tick inside it (the SIGKILL time is later than every Sample in the fixture);
- the detail response: `chart.time_axis` and `timing.time_axis` `raw_uncorrected`, `correction_available` false, `correction_unavailable_reason == correction_out_of_range`, raw `x_ms`, no tick at or labelled with the SIGKILL time, `sigkill.drawn` false, `sigkill.x_ms` null, `sigkill.time` equal to the recorded string;
- `text_times` `sigkill_sent` value equal to the recorded string with clock `host`, and `timing.sigkill_sent_time` unchanged with clock `host`;
- `derived.intervals` is exactly `[finished_at_to_sigkill]`;
- every block checked against the allowlisted key sets of `test_api_contract.py`, and no absolute path in the response.

Commands and results:

- `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_chart_fallback.py -q -p no:cacheprovider --collect-only`: 5 tests collected, exit 0.
- `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_chart_fallback.py -q -p no:cacheprovider`: 5 passed, exit 0.
- Check that the test detects the defect: with the `sigkill_x_ms = None` line temporarily replaced by `pass`, 2 of the 5 tests failed (domain_max_ms was 1,795,209,200,000, the SIGKILL time, instead of 1,795,209,182,000, the last Sample). The line was restored and the file re-run (5 passed).

## (b) Audit of `coroner/viewer/classify.py`

Read in full (501 lines before the fix, 504 after) against task 2.7, design "Classification and verdict" / "Docker state source and precedence" / "Compared fields and normalisation", and Requirement 14.

Bytes (read-only check, before the fix): 19,095 bytes, 0 CR, 501 LF, no BOM, ends with one LF. The 9 non-ASCII bytes are the three `→` characters (UTF-8, 3 bytes each) in the `verdict_of` docstring. No garbage, no duplicated blocks; the module has exactly 5 dataclasses and 10 functions, each defined once.

Imports: `__future__`, `dataclasses`, `typing`, `coroner.runner.manifest` (`InspectError`, `RunManifest`, `extract_inspect_excerpt` local), `coroner.viewer.timefmt` (`epoch_ms_docker`, `epoch_ms_utc`), `coroner.common.timeutil` (`parse_utc_ms`, local). No file operations, no subprocess, no network, no Docker client.

Findings:

- `experiment_type`, `verdict_of`: as specified; never inferred from ExitCode, OOMKilled or run id.
- Five `source` codes and their conditions: correct.
- Six compared fields: `container_id`, `status`, `exit_code`, `oom_killed`, `memory_limit_bytes`, `finished_at`. Manifest `exit_code` and `memory_limit_bytes` accept only `int` that is not `bool`; `oom_killed` only an exact `bool`. The raw side gets these types from `InspectExcerpt` (M0 enforces them). `0` memory is a value.
- `finished_at`: manifest via `parse_utc_ms`/`epoch_ms_utc`, raw via the local Docker parser `epoch_ms_docker`; equality on floored epoch ms, which equals the design's "format_utc_ms then string equality". Zero date and unparseable values are "not recorded".
- Per-field results: `equal`, `discrepancy`, `not_compared_manifest_missing`, `not_compared_raw_missing`, `not_recorded_both`. A discrepancy needs both sides non-null. The design also lists `unparseable`; the code never emits it (an unparseable side is "not recorded", as the same design row requires). Not changed.
- Effective values: manifest first, then raw, otherwise `None`; no default. `memory_limit_kind`: `none_configured` for 0, `bytes` otherwise, `None` for null. A negative memory value would be `bytes`; M0 does not produce one. Not changed.
- No inference of OOMKilled anywhere.
- `invalid_json` versus `rejected` decided from `isinstance(exc.__cause__, ValueError)`; message text is never read. Correct inside `classify.py`.
- FinishedAt wiring: `loader._raw_finished_at` parses the bounded bytes and checks with `epoch_ms_docker`; both loader paths pass it as the `raw_finished_at` keyword; `_parse_raw` re-checks it and stores it on the raw block; `_compare_fields` compares it. Correct.

Defects found:

1. **Loader wiring of `raw_unavailable_reason` (real defect, fixed).** `loader.py` passed `inspect_bytes=None` for every inspect that failed to parse and `raw_fault=inspect_state.reason`, which is `inspect_invalid_json` or `inspect_rejected`. `classify._parse_raw` accepts only `missing`, `unreadable`, `too_large` and defaulted everything else to `missing`. So an invalid-JSON or rejected inspect was reported as `raw_unavailable_reason: "missing"` in every detail and listing response, and `classify`'s `__cause__` check was never reached from production. Observed before the fix for `{not json`, `{}` and non-JSON bytes: `missing` in all three.
   Fix (`coroner/viewer/loader.py`): new helper `_docker_inputs(ar, state)` used by both `load_detail` and the listing path. It passes the bytes whenever the read succeeded (so `classify` decides `invalid_json`/`rejected` from `__cause__`), passes the artifact status as the fault for a failed read (`missing`, `unreadable`, `too_large`), and extracts `FinishedAt` only from an inspect whose status is `ok`.
   Before (both call sites):
   ```python
       raw_fault: str | None = None
       if inspect_state.status != ArtifactStatus.OK.value:
           raw_fault = inspect_state.reason
       inspect_bytes_for_classify: bytes | None = None
       if inspect_state.status == ArtifactStatus.OK.value and inspect_ar.data is not None:
           inspect_bytes_for_classify = inspect_ar.data
       ... raw_finished_at=_raw_finished_at(inspect_bytes_for_classify),
   ```
   After:
   ```python
       inspect_bytes_for_classify, raw_fault, raw_finished_at = _docker_inputs(inspect_ar, inspect_state)
       ... raw_finished_at=raw_finished_at,
   ```
   The listing's `docker_state_discrepancy` still requires `inspect_state.status == ok`, so it is unchanged.
2. **Deeply nested inspect JSON (related, fixed).** `json.loads` raises `RecursionError` (not an `InspectError`), which `classify` reported as `unreadable` and `loader._parse_inspect` labelled with the artifact reason `manifest_invalid` (a manifest code on the inspect artifact). Fixes: `classify._parse_raw` catches `RecursionError` as `rejected` (a 3-line `except` branch before `except Exception`); `loader._parse_inspect` uses `inspect_rejected` for it.

Regression tests (added to `tests/unit/viewer/test_detail_end_to_end.py`, through `load_detail` + `to_detail` + `dump_json`): `test_loader_derives_invalid_json_and_rejected_from_the_inspect_error_cause` (5 cases: invalid JSON, non-UTF-8 bytes, `{}`, short Id, 100,000 nested `[`) and `test_loader_keeps_missing_as_the_raw_unavailable_reason_when_inspect_is_absent`.

Verdict: `classify.py` itself had no defect from the PowerShell edit (no stray bytes, no duplication, rules as specified). Two defects at its boundary were found and fixed: the loader wiring of `raw_unavailable_reason`, and the `RecursionError` path.

Command: `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer/test_classification.py tests/unit/viewer/test_docker_state.py tests/unit/viewer/test_detail_end_to_end.py -q -p no:cacheprovider`: 95 passed, exit 0.

## (c) Memory

Restated from `docs/m1-results/parse-memory.md`; nothing was rerun.

- Idle baseline (fresh process after `import coroner.viewer.app`): about 43.0 MiB peak working set (45,072,384 to 45,142,016 bytes over three runs).
- One load of a 16,777,216-byte evidence file (46,894 Samples) including `to_detail` and `dump_json`, without tracemalloc: about 174.9 MiB peak working set (183,160,832 to 183,447,552 bytes over three runs).
- tracemalloc peak for the same load: 124.5 MiB (130,520,966 to 130,520,991 bytes).

The increment per load is about 132 MiB (upper bound; it includes the tool's own build of the synthetic file). That is 25 to 30 percent above the design estimate of about 100 MB.

Bounds unchanged: 16 MiB (16,777,216-byte) artifact limit, 5,000-point chart cap, at most two concurrent loads (semaphore of 2).

The 307 MiB figure (43 + 2 x 132) is an UNMEASURED arithmetic two-load estimate, not a measurement and not a guaranteed maximum. `parse-memory.md` called it "worst case" and "an arithmetic bound"; that one sentence was changed to say it is an unmeasured estimate and not a guaranteed maximum.

## (d) Task 2.35 verification deviation

The combined `--collect-only` command over `tests/unit/viewer` and `tests/unit/test_structure.py` did not return in the agent's shell. Counts were collected per file instead (sum 1002), and the full run was executed with `-v -rs` (1000 passed, 2 skipped, exit 0). The exact `-q` command of Task 2.35 was therefore NOT run successfully at that time.

In this closeout, `.\.venv\Scripts\python.exe -m pytest tests/unit/viewer tests/unit/test_structure.py -q -rs -p no:cacheprovider` was run as a background process (`Start-Process ... -RedirectStandardOutput %TEMP%\m1b_full2.log -Wait`) because direct foreground runs that pipe output in this shell were interrupted. Result: 1011 passed, 2 skipped, exit code 0, stderr empty (1013 tests = 1002 + 5 in `test_chart_fallback.py` + 6 new in `test_detail_end_to_end.py`). Both skips: `tests\unit\viewer\test_files_containment.py:308: this account/platform cannot create symlinks (os.symlink failed); covered by fake-IO reparse tests`.

## (e) Process deviation: PowerShell edit of classify.py

One earlier edit of `coroner/viewer/classify.py` used PowerShell `ReadAllText`/`WriteAllText` instead of the file-edit tools. The audit in (b) checked the complete file and its bytes: LF only, no BOM, no garbage or duplicated code. The defects found in (b) were in the loader wiring and the `RecursionError` path, not artefacts of that edit. All closeout edits used the file-edit tools.

## (f) Open edges kept

These were not changed in this closeout:

- The manifest is read before a listing cache hit is decided.
- For a content-invalid manifest, when a cache is present, `lstat` is performed on `evidence.jsonl` and `docker_inspect.json`.
- A too-large manifest result is now cacheable.
