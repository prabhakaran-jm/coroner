# Parse memory for a 16 MiB evidence file (task 2.11, redone)

## Method

- Tool: `tests/unit/viewer/measure_parse_memory.py` (not collected by pytest). Run from PowerShell, outside pytest, one fresh process per run:
  `.\.venv\Scripts\python.exe tests/unit/viewer/measure_parse_memory.py` (tracemalloc on) and
  `.\.venv\Scripts\python.exe tests/unit/viewer/measure_parse_memory.py --no-tracemalloc` (working set only).
- Python 3.12.10 (MSC v.1943 64 bit, AMD64), Windows.
- Input: one synthetic evidence file of exactly 16,777,216 bytes (46,894 Samples, run id `mem-test-00000001`) plus a synthetic manifest and `docker_inspect.json`, all in a temporary directory. The real `runs/` directory is never touched.
- Measured section: `files.find_run` and `files.read_artifact`, then `loader.load_detail` (bounded read, `read_evidence`, `summarize`, Sample extraction, chart payload, Docker state), then **`to_detail` and `dump_json`**. The earlier measurement could not include the last two because of the x-axis serializer defect. That defect is fixed, and every run below reports `to_detail/dump_json: ok` with a 1,773,593-byte JSON response.
- Python-side peak: `tracemalloc` peak. Process peak: `PeakWorkingSetSize` from `K32GetProcessMemoryInfo` through `ctypes` (stdlib). The process peak also contains the tool's own build of the synthetic file (a list of lines joined into one 16 MiB bytes object, so about two copies exist briefly). That part is not Viewer work, so the increment below is an upper bound.

## Baseline (idle process)

Peak working set of a fresh process after `import coroner.viewer.app`, read with the same ctypes call (three runs; the import was run from a small script file, not `python -c`, because PowerShell mangles the quotes in a multi-line `-c` string):

| Run | PeakWorkingSetSize |
|---|---|
| 1 | 45,142,016 bytes (43.1 MiB) |
| 2 | 45,072,384 bytes (43.0 MiB) |
| 3 | 45,121,536 bytes (43.0 MiB) |

## Load results (including to_detail and dump_json)

With tracemalloc on:

| Run | tracemalloc peak | PeakWorkingSetSize | Elapsed |
|---|---|---|---|
| 1 | 130,520,966 bytes (124.5 MiB) | 358,625,280 bytes (342.0 MiB) | 11,734 ms |
| 2 | 130,520,991 bytes (124.5 MiB) | 357,986,304 bytes (341.4 MiB) | 11,741 ms |
| 3 | 130,520,991 bytes (124.5 MiB) | 357,695,488 bytes (341.1 MiB) | 12,154 ms |

With `--no-tracemalloc` (working set only; tracemalloc inflates both memory and time):

| Run | PeakWorkingSetSize | Elapsed |
|---|---|---|
| 1 | 183,414,784 bytes (174.9 MiB) | 2,917 ms |
| 2 | 183,447,552 bytes (174.9 MiB) | 2,249 ms |
| 3 | 183,160,832 bytes (174.7 MiB) | 2,445 ms |

All runs exited with code 0.

## Increment over the baseline

Without tracemalloc: 174.8 MiB peak minus 43.0 MiB baseline is about **132 MiB** per load. This is an upper bound for the Viewer pipeline, because it includes the tool's own synthetic-file build (tens of MiB, not separated here). The tracemalloc-on working set (about 341 MiB) is dominated by tracemalloc overhead and is not a usable estimate of real memory.

Adding `to_detail` and `dump_json` did not change the process peak measurably (about 175 MiB before and after). The peak occurs earlier, during parsing, and serializing at most 5,000 chart points allocates less than that.

## Assessment (for the reviewer)

- **Design estimate of about 100 MB per run:** the Python-side peak is 124.5 MiB (tracemalloc), and the process increment is about 132 MiB. Both are 25 to 30 percent above the estimate, so the measurement is not consistent with "about 100 MB" strictly, though it is the same order of magnitude. Part of the increment is the measuring tool's own file build, so the true per-request figure may be closer to 100 MB. That share was not isolated.
- **Two concurrent loads (semaphore of 2):** an arithmetic estimate is baseline plus 2 times the increment, about 43 + 2 x 132 = **307 MiB**. This figure is an UNMEASURED estimate, not a measurement of two concurrent loads and not a guaranteed maximum. The semaphore and all bounds are unchanged.
- The file has 46,894 Samples, so the 5,000-point chart cap is exercised.
