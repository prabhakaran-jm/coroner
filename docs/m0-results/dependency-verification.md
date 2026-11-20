# Dependency Verification (Task 1.5)

Recorded on the Windows host (PowerShell). Every value is from a command that was run. Raw outputs are in the gitignored `runs/t15_*.txt` files. The temporary `.venv-verify` was removed at the end of the task.

## Result

PASS. The five candidate pins from the design exist on PyPI, are not yanked, install together without conflicts, and pass `pip check` on the Windows host (Python 3.12.10) and in a throwaway `python:3.12.8-slim-bookworm` container. No candidate was substituted. One package was added that is not in the design's candidate list: `PyYAML==6.0.2` (see "Additions").

## Candidate pins (from design D17)

| Package | Pin | On PyPI | Yanked files | Declared Requires-Python |
|---|---|---|---|---|
| fastapi | 0.115.6 | yes | 0 | >=3.8 |
| uvicorn | 0.34.0 | yes | 0 | >=3.9 |
| pytest | 8.3.4 | yes | 0 | >=3.8 |
| hypothesis | 6.122.3 | yes | 0 | >=3.9 |
| httpx | 0.28.1 | yes | 0 | >=3.8 |

Checked with `https://pypi.org/pypi/<name>/<version>/json`.

## Installed versions in a fresh `.venv-verify`

Procedure: `python -m venv .venv-verify`, then `.\.venv-verify\Scripts\python.exe -m pip install -r requirements/dev.txt` (a fresh environment, built after an earlier incremental install was discarded).

Interpreter: Python 3.12.10, pip 25.0.1.

| Direct | Version |
|---|---|
| fastapi | 0.115.6 |
| uvicorn | 0.34.0 |
| httpx | 0.28.1 |
| hypothesis | 6.122.3 |
| pytest | 8.3.4 |
| PyYAML | 6.0.2 |

| Transitive | Version |
|---|---|
| starlette | 0.41.3 |
| pydantic / pydantic_core | 2.14.3 / 2.50.4 |
| annotated-types | 0.8.0 |
| typing_extensions / typing-inspection | 4.16.0 / 0.4.5 |
| anyio / idna / h11 / click | 4.16.0 / 3.20 / 0.16.0 / 8.5.1 |
| httpcore / certifi | 1.0.9 / 2026.10.23 |
| attrs / sortedcontainers | 26.1.0 / 2.4.0 |
| pytest support: pluggy / iniconfig / packaging | 1.7.0 / 2.3.1 / 26.3 |
| colorama (Windows-only, required by pytest and click on win32) | 0.4.6 |

## Checks

| Check | Exit | Observed |
|---|---|---|
| `pip install -r requirements/dev.txt` (fresh venv) | 0 | 24 packages installed |
| `pip check` | 0 | `No broken requirements found.` |
| `pip freeze` vs `requirements/dev.txt` | n/a | 24 lines each, 0 differences (case and `_`/`-` normalised) |
| `python -m pytest --version` | 0 | `pytest 8.3.4` |
| Imports of fastapi, uvicorn, httpx, hypothesis, pytest, yaml, starlette, pydantic | 0 | `imports ok` |
| `importlib.metadata` versions | 0 | fastapi=0.115.6 uvicorn=0.34.0 httpx=0.28.1 hypothesis=6.122.3 pytest=8.3.4 PyYAML=6.0.2 starlette=0.41.3 pydantic=2.14.3 |

The test suite was not run (no tests exist yet).

## Check against the image Python (3.12.8)

For each requirements file, a throwaway `python:3.12.8-slim-bookworm` container (`docker run --rm`, `requirements/` mounted read-only) ran `pip install -r`, `pip check`, and `pip freeze`. No Dockerfile was written and nothing was built.

| File | Result |
|---|---|
| `requirements/dev.txt` | installed, `No broken requirements found.`, Python 3.12.8; the freeze matches the host freeze (24 packages, 0 differences, excluding pip and setuptools) |
| `requirements/collector.txt` | installed, `No broken requirements found.` |
| `requirements/demo_app.txt` | installed, `No broken requirements found.`; the freeze equals the file's 12 pins |
| `requirements/runner.txt` | installed (empty of packages), `No broken requirements found.` |

The `pip check` inside the real built images is still pending for task 5.5. This check shows the pins resolve and are consistent on the image's Python, but it is not the image build itself.

## Setuptools package discovery

`setuptools` is not installed by default in a Python 3.12 venv, and it is not in `requirements/*.txt`. It was installed into `.venv-verify` only (setuptools 84.2.1) for this check.

| Check | Result |
|---|---|
| `find_packages(include=['coroner*'])` | `['coroner', 'coroner.collector', 'coroner.common', 'coroner.demo_app', 'coroner.evidence', 'coroner.probe', 'coroner.runner', 'coroner.runner.experiments']` |
| `pip wheel . --no-deps --no-build-isolation` | exit 0, built `coroner-0.0.0-py3-none-any.whl` |
| `__init__.py` files inside the wheel | the same eight packages; no `tests`, `docs`, or `docker` content |

The wheel and the `build/` and `coroner.egg-info` directories it created were deleted. `[build-system] requires = ["setuptools"]` is unpinned. That only affects building a wheel, which M0 does not do; the images copy source and install pinned requirements.

## requires-python

`pyproject.toml` now says `requires-python = ">=3.12"`.

- The highest `Requires-Python` among the 24 pinned packages is `>=3.10` (annotated-types, anyio, click, iniconfig, pluggy, pydantic, pydantic_core, typing-inspection). So the metadata alone permits 3.10 and later.
- Only 3.12.10 (host) and 3.12.8 (image) were actually run. Python 3.10 and 3.11 were not tested, so they are not claimed.
- This is stricter than the design's "3.11 minimum" note for the host. To claim 3.11, the pins would need an install, `pip check`, and the test suite on a real 3.11 interpreter, for example in a throwaway `python:3.11` container. That needs your approval because it pulls a second image.

## Additions to the design's candidate list

- `PyYAML==6.0.2`: needed by the Docker-free Compose parse test (task 5.4). It is in `dev.txt` only. Version 6.0.3 was the latest on PyPI at the time; 6.0.2 was installed and checked, so it is the pin.
- `colorama==0.4.6`: a Windows-only transitive dependency, kept in `dev.txt` because the host runs Windows. It installs harmlessly on Linux (shown in the 3.12.8 container).

## Files

- `requirements/demo_app.txt` and `requirements/collector.txt`: identical 12 exact pins (fastapi, uvicorn, and their transitive packages).
- `requirements/runner.txt`: no packages. The runner uses the standard library only.
- `requirements/dev.txt`: all 24 pins, self-contained (no `-r` lines), so every non-comment line matches the exact-pin pattern used by the task 1.6 structure test.
- `requirements/.gitkeep` was removed because the directory now has files.

## Limitations

- Pins were resolved on Windows. A Linux-only transitive dependency would not appear in the freeze; the container install on 3.12.8 produced the same 24 packages, so none was found.
- Pip index availability can change; pins reproduce only while these files remain on PyPI.

## M0.5 image build verification (task 5.5)
Real output from `docker build` and `docker run --rm --entrypoint python <image> -m pip ...` on 2026-11-18. Raw files are in the gitignored `runs/m05-docker/` (`build_collector.txt`, `build_demo.txt`, `freeze_collector.txt`, `freeze_demo_app.txt`, `verify.txt`, `preflight.txt`).
- Base: `python:3.12.8-slim-bookworm`, resolved to `sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0`. This equals the digest recorded in `docker-prereqs.md` (no tag drift). Both build logs show `FROM ...python:3.12.8-slim-bookworm@sha256:2199a628...`.
- Builds: `docker build -f docker/collector.Dockerfile -t coroner-collector:m0 .` and `docker build -f docker/demo_app.Dockerfile -t coroner-demo-app:m0 .` both exited 0 (same files and context as `compose.yaml`).
- Image IDs (containerd image-index digests): `coroner-collector:m0` = `sha256:cd9126e565333da7b76cb3f16ad494c94e2bcc9102e0cc8980e64eb55bf0420e`; `coroner-demo-app:m0` = `sha256:23571625f7319525505816774be12f752854b04608b9b0912ef9d1e1b5ca2c52`.
- `pip check` during build (Dockerfile step): "No broken requirements found." for both. Re-run in each built image: exit 0, "No broken requirements found." (pip also printed a harmless cache-directory warning because user 10001 has no writable home).
- `pip freeze` in each image: 12 packages. Compared with `requirements/collector.txt` and `requirements/demo_app.txt` (12 exact pins each): identical names and versions, no extras, none missing. No pin change is needed.
- Python in both images: 3.12.8 (GCC 12.2.0), Debian 12.9.

| Package | Version in both images |
| --- | --- |
| annotated-types | 0.8.0 |
| anyio | 4.16.0 |
| click | 8.5.1 |
| fastapi | 0.115.6 |
| h11 | 0.16.0 |
| idna | 3.20 |
| pydantic | 2.14.3 |
| pydantic_core | 2.50.4 |
| starlette | 0.41.3 |
| typing-inspection | 0.4.5 |
| typing_extensions | 4.16.0 |
| uvicorn | 0.34.0 |

The "image-build `pip check` pending" note in the earlier sections is now resolved by this section.

## M0.5 acceptance re-check (task 5.8)
On 2026-11-18 the images were re-checked by listing, not rebuilt. `docker image ls` still shows `coroner-collector:m0` (`cd9126e56533`), `coroner-demo-app:m0` (`23571625f731`) and `python:3.12.8-slim-bookworm` (`2199a62885a1`), which are the IDs and base recorded above, so the `pip check` and `pip freeze` results in the image build section still describe the images that the Docker integration tests ran against (34 passed). No file under `requirements/` changed (`git diff --stat HEAD -- requirements/` is empty), no pin changed, and no new dependency was installed. The image-build `pip check` was run once, in task 5.5; it was not repeated in 5.8. Docker Desktop on Windows only; a Linux host was not used.
