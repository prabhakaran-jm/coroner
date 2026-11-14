# Docker Prerequisites and Base Image Verification (Task 1.4)

Recorded on the host (Windows 11, PowerShell). All values are from commands that were run. Raw outputs are in the gitignored `runs/t14_*.txt` files.

## Tool versions

| Item | Observed |
|---|---|
| Docker client | 29.9.3 (API 1.56), windows/amd64, context `desktop-linux` |
| Docker Desktop (server) | 4.99.0 (245822) |
| Docker Engine | 29.9.3 (API 1.56, minimum 1.40), linux/amd64 |
| containerd / runc | v2.4.2 / 1.5.2 |
| Docker Compose | v5.7.0 |
| Buildx | v0.38.1 |

`docker version`, `docker compose version`, and `docker info` all exited 0.

## Platform

| Item | Observed |
|---|---|
| `OSType` | linux (Linux containers) |
| Architecture | x86_64 |
| Kernel | 6.18.58.1-microsoft-standard-WSL2 (WSL2 backend) |
| Cgroup | version 2, driver cgroupfs |
| Storage driver | overlayfs (containerd snapshotter) |
| Docker VM CPUs / memory | 8 CPUs / 7.687 GiB (8254025728 bytes) |
| Security options | seccomp (builtin profile), cgroupns |
| Containers, volumes before checks | 0 containers, no volumes (`docker volume ls` listed none). No existing evidence was present. |

The VM memory (7.687 GiB) is far above the 128 MiB Demo_App limit that Experiment C uses, so the limit, not the VM, is the binding constraint.

## Base image: `python:3.12.8-slim-bookworm`

| Check | Observed |
|---|---|
| `docker manifest inspect` | exit 0. OCI image index with a `linux/amd64` manifest (`sha256:8859bd6c...a6cc9`), plus arm and other platforms. The tag exists and covers this host's platform. |
| `docker pull` | exit 0. `Digest: sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0`. Status `Image is up to date`. |
| `docker image inspect` (Id, Os/Arch) | `sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 linux/amd64` |
| RepoDigests | `python@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0` |
| Created / size | 2025-01-17T12:35:53Z / 186016211 bytes (about 188 MB on disk) |
| Python inside the container | `Python 3.12.8` (`3.12.8 (main, Feb  4 2025, 04:56:13) [GCC 12.2.0]`) |
| Container OS | `Linux-6.18.58.1-microsoft-standard-WSL2-x86_64-with-glibc2.36` |

The first `docker manifest inspect ... ; docker pull ...` call printed nothing to my terminal, so the pull happened there and the later explicit pull reported the image as up to date. Both exited 0 and the digest above is the one in the local image store.

Digest note: the image index digest and the local image Id both read `sha256:2199a628...`, because the containerd snapshotter stores the index digest as the image Id. The manifest digest for linux/amd64 is `sha256:8859bd6c...`. The tag was not changed. Pinning by digest is not required by the approved design and was not added.

## Memory-limit test

| Command | Exit | Output |
|---|---|---|
| `docker run --rm --memory 64m --memory-swap 64m python:3.12.8-slim-bookworm python -c "print(1)"` | 0 | `1` |
| same limits, `python -c "print(open('/sys/fs/cgroup/memory.max').read().strip())"` | 0 | `67108864` (64 MiB) |

The cgroup file inside the container shows the 64 MiB limit was applied, so memory limits are enforced by this Docker Desktop and WSL2 setup. This shows the limit is set. It does not show an OOM kill, which only the spike (task 1.10) and Experiment C test.

After the checks, `docker ps -a` listed no containers (the `--rm` containers were removed), and `docker volume ls` listed no volumes. No Dockerfile, Compose file, volume, or application code was created.

## Limitations

- Linux containers run in the Docker Desktop WSL2 VM, so the VM clock and kernel differ from the Windows host. The clock offset is measured in task 7.1.
- This check does not prove `State.OOMKilled` is reported correctly on this Docker Desktop version. The spike (task 1.10) tests that.
- Python 3.12.8 in the image differs from the host's 3.12.10. Dependency compatibility is verified against both in task 1.5.
- The tag is a candidate from the design. It exists and works, so it is confirmed for this host. Tag drift is possible if the tag is later re-pushed. The digest above is recorded to detect that.

## Result

PASS. The tag exists for linux/amd64, the pull succeeded, the container reports Python 3.12.8, and the 64 MiB memory-limited container ran and exited 0.