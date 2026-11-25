# Repository organisation

Counts are tracked files on branch `feat/coroner-m2-demo` at head `fa00edf` (`git ls-files`: 344 files), taken on 2026-11-25 before the files in `JUDGING.md` and `docs/submission/` were added. Those new files are listed separately. Nothing was deleted, moved or renamed. Everything under "Recommendations" is a suggestion for the user to decide.

## Classification

| Class | Paths and globs | Tracked files | Notes |
|---|---|---|---|
| Essential production source | `coroner/**` (Viewer `coroner/viewer/**`: 27 files, of which 13 static files are `.js`, `.css` and `.html`), `docker/*.Dockerfile`, `compose.yaml`, `requirements/*.txt`, `pyproject.toml`, `pytest.ini`, `.dockerignore`, `.gitattributes`, `.gitignore`, `scripts/make-sample-runs.py`, `scripts/demo-preflight.ps1` | 65 in `coroner/`, 13 build and config files, 2 in `scripts/` (80) | The code, pins and packaging. `LICENSE` and `README.md` are also essential (see the next row group) |
| Unit and property tests | `tests/unit/**` (122 `.py` files, of which 28 are `test_prop_*.py`, plus `compare_vectors.json` and `compare_vectors_runner.mjs`), `tests/__init__.py`, `tests/conftest.py` | 124 + 2 (126) | Docker-free and network-free. Default `python -m pytest` runs them |
| Opt-in Docker tests | `tests/docker_integration/**` | 10 | Skipped unless `--run-docker` or `CORONER_RUN_DOCKER=1`. Never run unscoped (`README.md` section 12) |
| Required `.kiro` specs | `.kiro/specs/coroner-m0/`, `.kiro/specs/coroner-m1-ui/`, `.kiro/specs/coroner-m2-demo/` (`.config.kiro`, `requirements.md`, `design.md`, `tasks.md`, `tasks.meta.json`) | 13 | Required at the repository root by the Kiroween rules. Must stay out of `.gitignore` |
| Current judge instructions | `JUDGING.md` (new), `README.md`, `LICENSE`, `docs/m2-plan/judge-test-build.md`, `docs/m2-plan/startup-shutdown.md`, `docs/m2-plan/run-sheet.md`, `docs/m2-plan/recording-script.md`, `docs/m2-plan/canonical-run-verification.md`, `docs/m2-plan/m2-demo-readiness-plan.md` | 8 tracked (`README.md`, `LICENSE`, 6 in `docs/m2-plan/`) plus `JUDGING.md` | `README.md` carries tests that pin its content (`tests/unit/test_docs.py`) |
| Submission text and media | `docs/submission/*.md` (new: `devpost-writeup.md`, `kiro-usage.md`, `judge-testing-instructions.md`, `submission-checklist.md`, `repository-organisation.md`), `docs/m2-plan/rehearsal-shots/*.png` | 9 PNG tracked, 5 new `.md` | No demo video is in the repository, and none is recorded |
| Historical test and acceptance evidence | `docs/m0-results/**` | 22 | Recorded M0 results. Frozen records |
| | `docs/m1-results/**` (`m1.1-acceptance.md`, `m1b-closeout.md`, `parse-memory.md`, `runs-baseline.md`, `browser-4.7/**` 67 files, `browser-4.14/**` 5 files) | 76 | Acceptance record and screenshots, including superseded and withdrawn entries kept as marked history |
| Local temporary files, excluded | `runs/` (real A, B, C evidence), `.venv/`, `.hypothesis/`, `.pytest_cache/`, `**/__pycache__/`, `spike/` | 0 tracked (all in `.gitignore`) | Never staged. `runs/` is deliberately not published |

Images: 75 tracked PNG files (66 under `docs/m1-results/`, 9 under `docs/m2-plan/rehearsal-shots/`), 54 unique by SHA-256. The pixel-level privacy audit inspected all 75 and found nothing.

## Recommendations (nothing was changed)

Do not delete or move any of these without a decision. A move or deletion also needs the references fixed first, because the acceptance records cite file names.

1. **Duplicate screenshots in `docs/m1-results/browser-4.7/rerun-1/`.** The folder has 35 tracked files. 21 PNG files there are byte-identical to the same-named files directly under `docs/m1-results/browser-4.7/` (2,705,133 bytes in total, checked by SHA-256):
   `01-overview-desktop`, `02-compare-desktop`, `03-compare-values`, `04-compare-statements`, `05-compare-narrow`, `08-keyboard-focus`, `10-wrong-slot-warning`, `11-unavailable-slot`, `12-invalid-query`, `13-compare-chose-a`, `13-compare-chose-b`, `13-compare-chose-c`, `14-compare-360`, `14-overview-360`, `14-overview-480`, `15-synth-equal-oomkilled`, `16-synth-exitcode-discrepancy`, `17-synth-oomkilled-discrepancy`, `18-synth-memory-limit-discrepancy`, `19-synth-b-exitcode-discrepancy` and `20-synth-show-more` (each with the extension `.png`).
   The other 14 files in `rerun-1/` differ from the originals and should stay (for example `06-experiment-b-chart.png`, `07-experiment-c-chart.png` and `task-4.7-rerun-results.json`). `docs/m1-results/browser-4.7/task-4.7-report.md` section 11 describes `rerun-1/` as a set of 34 PNG files and cites `rerun-1/14-*-480.png` and `14-*-360.png`, and `m1.1-acceptance.md` cites the folder, so a deletion would make those records inaccurate. The repository is smaller by about 2.7 MB at most. Low value: the cleanup saves little and edits historical evidence, so leaving it as is is reasonable.
2. **Stale `docs/m1-results/browser-4.7/07-experiment-c-chart.png`.** It differs from `rerun-1/07-experiment-c-chart.png`. It is historical evidence of an earlier run and stays. Keep it, and if anyone links to it from outside the acceptance record, point to the `rerun-1` version for the final result.
3. **Nothing else is flagged as redundant.** No other duplicate was searched for beyond the images (SHA-256 on all tracked PNG files). Other possible cleanup, such as merging overlapping M0 result files, was not assessed and is not recommended before submission.
