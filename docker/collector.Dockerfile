# Collector image (task 5.1). Build context is the repo root (see compose.yaml).
# Base: pinned tag verified in task 1.4 (docs/m0-results/docker-prereqs.md).
# Verified digest at that time: sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
# (recorded to detect tag drift; the tag, not the digest, is the pin).
FROM python:3.12.8-slim-bookworm

# No .pyc files in the image layer; unbuffered stdout/stderr so logs reach `docker logs` immediately.
# PYTHONPATH lets `python -m coroner.collector.main` resolve without installing the project.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Fixed non-root identity. Compose volume ownership depends on UID/GID 10001.
RUN groupadd -g 10001 coroner \
 && useradd -u 10001 -g 10001 --no-create-home --shell /usr/sbin/nologin coroner

# Evidence directory baked into the image. A fresh named volume mounted at /data/evidence is initialised
# from this directory, so it inherits owner 10001:10001 and mode 0750 on first use. /data stays root-owned 0755.
RUN mkdir -p /data/evidence \
 && chown 10001:10001 /data/evidence \
 && chmod 0750 /data/evidence

WORKDIR /app

# Dependencies first so this layer is cached until requirements change. pip check fails the build on a conflict.
COPY requirements/collector.txt requirements/collector.txt
RUN pip install --no-cache-dir -r requirements/collector.txt \
 && pip check

# Only the packages the Collector imports. No demo_app, probe, runner, tests, docs, or spike.
COPY coroner/__init__.py coroner/__init__.py
COPY coroner/common coroner/common
COPY coroner/evidence coroner/evidence
COPY coroner/collector coroner/collector

USER 10001:10001

# Documentation only; Compose publishes the port on 127.0.0.1.
EXPOSE 8081

# Exec form: python is PID 1 and runs uvicorn in-process with workers=1 (coroner.collector.app.main).
# main() loads config from the environment and builds the app; there is no module-level `app` object.
CMD ["python", "-m", "coroner.collector.main"]
