# Demo_App image (task 5.2). Build context is the repo root (see compose.yaml).
# Base: pinned tag verified in task 1.4 (docs/m0-results/docker-prereqs.md), same as the Collector image.
# Verified digest at that time: sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
# (recorded to detect tag drift; the tag, not the digest, is the pin).
FROM python:3.12.8-slim-bookworm

# No .pyc files: Compose runs this image with a read-only root filesystem. Unbuffered output reaches `docker logs`.
# PYTHONPATH lets `python -m coroner.demo_app.main` resolve without installing the project.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Fixed non-root identity, same as the Collector image.
RUN groupadd -g 10001 coroner \
 && useradd -u 10001 -g 10001 --no-create-home --shell /usr/sbin/nologin coroner

WORKDIR /app

# Dependencies first so this layer is cached until requirements change. pip check fails the build on a conflict.
COPY requirements/demo_app.txt requirements/demo_app.txt
RUN pip install --no-cache-dir -r requirements/demo_app.txt \
 && pip check

# Only what the Demo_App imports: the Probe and the Demo_App. No collector, evidence, common, runner, tests, or docs.
COPY coroner/__init__.py coroner/__init__.py
COPY coroner/probe coroner/probe
COPY coroner/demo_app coroner/demo_app

USER 10001:10001

# Documentation only; Compose publishes the port on 127.0.0.1.
EXPOSE 8080

# Exec form: python is PID 1 and runs uvicorn in-process with workers=1, listening on 0.0.0.0:8080
# (coroner.demo_app.main.main). main() builds the Probe and app from the environment; there is no
# module-level `app` or zero-argument factory, so `uvicorn module:app` cannot be used without code changes.
# The healthcheck is defined in compose.yaml, not here.
CMD ["python", "-m", "coroner.demo_app.main"]
