"""Smoke tests for the Collector HTTP app (Task 3.11). Route-level detail belongs to Task 3.12, persistence to 3.13."""
from __future__ import annotations

import base64
import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coroner.collector import app as app_module
from coroner.collector.app import COLLECTOR_PORT, create_app, main
from coroner.collector.config import CollectorConfig, load_config
from coroner.collector.state import CollectorAlreadyRunning
from coroner.collector.storage_probe import StorageProbe

SAMPLE = {
    "run_id": "r1", "seq": 0, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 1000, "request_count": 1,
    "loop_delay_ms": 0.5, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
    "interval_ms": 500, "dropped": 0,
}
TERMINATION = {
    "source": "experiment_runner", "observed_time": "2025-01-01T12:17:03.221Z",
    "container_state": {"status": "exited", "exit_code": 137, "oom_killed": True},
    "cause_label": "docker_oom_kill", "note": "Observed after the fact via docker inspect.",
}
RAW = json.dumps(SAMPLE).encode()


def config(tmp_path: Path, **env: str) -> CollectorConfig:
    return load_config(env, evidence_dir=tmp_path / "evidence")


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_bytes().splitlines() if line]


def test_routes_are_registered(tmp_path: Path) -> None:
    app = create_app(config(tmp_path))
    routes = {(m, r.path) for r in app.routes for m in getattr(r, "methods", ()) if m != "HEAD"}  # type: ignore[attr-defined]
    assert routes == {
        ("GET", "/healthz"), ("POST", "/v1/samples"),
        ("POST", "/v1/runs/{run_id:path}/termination"), ("GET", "/v1/runs/{run_id:path}/evidence"),
    }


def test_construction_does_no_io_and_requests_before_startup_are_503(tmp_path: Path) -> None:
    app = create_app(config(tmp_path))
    assert not (tmp_path / "evidence").exists()
    client = TestClient(app)  # no `with`: the lifespan has not run
    assert client.post("/v1/samples", content=RAW).status_code == 503
    assert client.get("/v1/runs/r1/evidence").status_code == 503
    assert not (tmp_path / "evidence").exists()


def test_lifecycle_starts_and_stops_watchdog_and_releases_the_lock(tmp_path: Path) -> None:
    app = create_app(config(tmp_path))
    watchdog, registry = app.state.watchdog, app.state.registry
    with TestClient(app):
        assert watchdog.running and registry.started and (tmp_path / "evidence").is_dir()
    assert not watchdog.running and not registry.started
    assert (tmp_path / "evidence").is_dir()
    assert not [t for t in threading.enumerate() if t.name == "coroner-silence-watchdog" and t.is_alive()]


def test_shutdown_never_deletes_evidence(tmp_path: Path) -> None:
    app = create_app(config(tmp_path))
    with TestClient(app) as client:
        assert client.post("/v1/samples", content=RAW).status_code == 200
    assert (tmp_path / "evidence" / "r1.jsonl").is_file()


class _Locked:
    held = False

    def acquire(self) -> None:
        raise CollectorAlreadyRunning("another collector holds the volume")

    def release(self) -> None:
        pass


def test_volume_lock_held_elsewhere_fails_startup_and_serves_nothing(tmp_path: Path) -> None:
    app = create_app(config(tmp_path), volume_lock=_Locked())  # type: ignore[arg-type]
    with pytest.raises(CollectorAlreadyRunning):
        with TestClient(app):
            pass  # pragma: no cover
    assert not app.state.watchdog.running and app.state.ready is False


def test_main_exits_non_zero_on_invalid_config_without_starting_uvicorn(tmp_path: Path) -> None:
    calls: list[Any] = []
    with pytest.raises(SystemExit) as info:
        main({"CORONER_DURABILITY": "sometimes"}, evidence_dir=tmp_path, run=lambda *a, **k: calls.append(k))
    assert info.value.code not in (0, None) and calls == []


def test_main_exits_non_zero_on_a_fatal_startup_error(tmp_path: Path) -> None:
    def run(app: Any, **kwargs: Any) -> None:
        raise CollectorAlreadyRunning("busy")

    with pytest.raises(SystemExit) as info:
        main({}, evidence_dir=tmp_path, run=run)
    assert info.value.code not in (0, None)


def test_main_runs_one_uvicorn_worker_on_port_8081(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    main({}, evidence_dir=tmp_path, run=lambda app, **kwargs: seen.update(kwargs, app=app))
    assert seen["workers"] == 1 and seen["port"] == COLLECTOR_PORT == 8081 and seen["host"] == "0.0.0.0"
    assert not (tmp_path / "r1.jsonl").exists()


def test_healthz_reports_storage_ok(tmp_path: Path) -> None:
    with TestClient(create_app(config(tmp_path))) as client:
        response = client.get("/healthz")
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"status", "durability", "server_time", "uptime_s", "storage_ok"}
    assert body["status"] == "ok" and body["durability"] == "crash" and body["storage_ok"] is True
    assert body["server_time"].endswith("Z") and len(body["server_time"]) == 24 and body["uptime_s"] >= 0


def test_healthz_is_200_with_storage_error_when_the_probe_fails(tmp_path: Path) -> None:
    def boom(fd: int) -> None:
        raise OSError("disk gone")

    cfg = config(tmp_path, CORONER_DURABILITY="process")
    probe = StorageProbe(cfg.evidence_dir, fsync=boom)
    with TestClient(create_app(cfg, probe=probe)) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["storage_ok"] is False and "disk gone" in response.json()["storage_error"]
        assert response.json()["durability"] == "process"
        assert client.post("/v1/samples", content=RAW).status_code == 200  # ingest does not use the probe's fsync


def test_valid_sample_is_stored_and_acknowledged_from_the_service(tmp_path: Path) -> None:
    with TestClient(create_app(config(tmp_path))) as client:
        response = client.post("/v1/samples", content=RAW)
        duplicate = client.post("/v1/samples", content=RAW)
    body = response.json()
    assert response.status_code == 200 and (body["status"], body["run_id"], body["seq"], body["durability"]) == ("stored", "r1", 0, "crash")
    assert duplicate.status_code == 200 and duplicate.json()["status"] == "duplicate"
    kinds = [r["record_type"] for r in records(tmp_path / "evidence" / "r1.jsonl")]
    assert kinds == ["run_metadata", "sample", "sequence_anomaly"]


def test_invalid_sample_bodies_are_422_through_the_service(tmp_path: Path) -> None:
    with TestClient(create_app(config(tmp_path))) as client:
        bad_json = client.post("/v1/samples", content=b"{")
        duplicate_key = client.post("/v1/samples", content=b'{"run_id":"r1","run_id":"r2"}')
        bad_field = client.post("/v1/samples", content=json.dumps({**SAMPLE, "seq": -1}).encode())
    assert [r.status_code for r in (bad_json, duplicate_key, bad_field)] == [422, 422, 422]
    assert bad_json.json()["error"] == "validation_failed" and bad_json.json()["reason_code"] == "invalid_json"
    assert duplicate_key.json()["reason_code"] == "invalid_json"
    assert [r["reason_code"] for r in records(tmp_path / "evidence" / "_rejections.jsonl")][:2] == ["invalid_json", "invalid_json"]


def padded(size: int) -> bytes:
    return RAW + b" " * (size - len(RAW))


def test_body_exactly_at_the_limit_is_processed_and_one_over_is_413(tmp_path: Path) -> None:
    cfg = config(tmp_path, CORONER_MAX_BODY_BYTES=str(len(RAW) + 5))
    with TestClient(create_app(cfg)) as client:
        assert client.post("/v1/samples", content=padded(len(RAW) + 5)).status_code == 200
        over = client.post("/v1/samples", content=padded(len(RAW) + 6))
    assert over.status_code == 413 and over.json()["error"] == "body_too_large"
    (rejection,) = [r for r in records(tmp_path / "evidence" / "_rejections.jsonl") if r["record_type"] == "rejection"]
    assert rejection["http_status"] == 413 and rejection["reason_code"] == "body_too_large"
    assert rejection["body_len"] == len(RAW) + 6 and rejection["body_preview_b64"] == ""  # declared, so never read


def test_declared_content_length_over_the_limit_is_413_without_reading_the_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config(tmp_path, CORONER_MAX_BODY_BYTES="100")
    streamed: list[int] = []
    original = app_module._read_bounded

    async def spy(request: Any, limit: int) -> Any:
        result = await original(request, limit)
        streamed.append(len(result[0]))
        return result

    monkeypatch.setattr(app_module, "_read_bounded", spy)
    with TestClient(create_app(cfg)) as client:
        response = client.post("/v1/samples", content=b"x" * 50, headers={"content-length": "5000"})
    assert response.status_code == 413 and streamed == [0]  # nothing was read from the stream
    (rejection,) = [r for r in records(tmp_path / "evidence" / "_rejections.jsonl") if r["record_type"] == "rejection"]
    assert rejection["body_len"] == 5000 and rejection["body_preview_b64"] == ""


def test_chunked_body_without_content_length_is_bounded_at_the_limit(tmp_path: Path) -> None:
    cfg = config(tmp_path, CORONER_MAX_BODY_BYTES="100")
    pulled: list[int] = []

    def chunks() -> Any:
        for _ in range(50):
            pulled.append(1)
            yield b"y" * 40

    with TestClient(create_app(cfg)) as client:
        response = client.post("/v1/samples", content=chunks())
    assert response.status_code == 413
    (rejection,) = [r for r in records(tmp_path / "evidence" / "_rejections.jsonl") if r["record_type"] == "rejection"]
    assert rejection["body_len"] == 101 and len(base64.b64decode(rejection["body_preview_b64"])) == 101


def test_body_larger_than_declared_is_still_bounded(tmp_path: Path) -> None:
    cfg = config(tmp_path, CORONER_MAX_BODY_BYTES="100")
    with TestClient(create_app(cfg)) as client:
        response = client.post("/v1/samples", content=b"z" * 500, headers={"content-length": "10"})
    assert response.status_code == 413


def test_termination_is_recorded_and_invalid_ones_are_422_or_413(tmp_path: Path) -> None:
    cfg = config(tmp_path, CORONER_MAX_BODY_BYTES="1000")
    with TestClient(create_app(cfg)) as client:
        client.post("/v1/samples", content=RAW)
        ok = client.post("/v1/runs/r1/termination", content=json.dumps(TERMINATION).encode())
        bad = client.post("/v1/runs/r1/termination", content=b"{")
        bad_id = client.post("/v1/runs/bad id/termination", content=json.dumps(TERMINATION).encode())
        big = client.post("/v1/runs/r1/termination", content=b"x" * 2000)
    assert (ok.status_code, ok.json()) == (200, {"status": "recorded"})
    assert (bad.status_code, bad_id.status_code, big.status_code) == (422, 422, 413)
    kinds = [r["record_type"] for r in records(tmp_path / "evidence" / "r1.jsonl")]
    assert kinds.count("termination") == 1 and kinds.count("rejection") == 2


def test_recovery_completes_before_the_first_request(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    path = directory / "r1.jsonl"
    path.write_bytes(b'{"record_type":"sample","sch')  # damaged tail, no newline
    app = create_app(config(tmp_path))
    with TestClient(app) as client:
        assert re.search(rb'"record_type"\s*:\s*"repair"', path.read_bytes())  # repaired before any request
        assert client.post("/v1/samples", content=RAW).status_code == 200


def test_evidence_returns_the_exact_bytes_and_never_modifies_the_file(tmp_path: Path) -> None:
    with TestClient(create_app(config(tmp_path))) as client:
        client.post("/v1/samples", content=RAW)
        path = tmp_path / "evidence" / "r1.jsonl"
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        response = client.get("/v1/runs/r1/evidence")
        assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert response.status_code == 200 and response.content == before
    assert response.headers["content-type"].startswith("application/x-ndjson")


def test_evidence_unknown_run_is_404_and_creates_nothing(tmp_path: Path) -> None:
    with TestClient(create_app(config(tmp_path))) as client:
        before = sorted(p.name for p in (tmp_path / "evidence").iterdir())
        response = client.get("/v1/runs/nope/evidence")
        after = sorted(p.name for p in (tmp_path / "evidence").iterdir())
    assert response.status_code == 404 and before == after
    assert not app_module.create_app(config(tmp_path)).state.registry.states()


@pytest.mark.parametrize("run_id", ["_rejections", "a/b", "..%2Fx", "a%20b", ".hidden", "x" * 65])
def test_evidence_invalid_run_id_is_422(tmp_path: Path, run_id: str) -> None:
    with TestClient(create_app(config(tmp_path))) as client:
        (tmp_path / "evidence" / "_rejections.jsonl").write_bytes(b"secret\n")
        response = client.get(f"/v1/runs/{run_id}/evidence")
    assert response.status_code == 422 and response.json()["error"] == "validation_failed"
    assert b"secret" not in response.content


def test_a_failed_append_is_503_and_not_acknowledged(tmp_path: Path) -> None:
    app = create_app(config(tmp_path))
    with TestClient(app) as client:
        assert client.post("/v1/samples", content=RAW).status_code == 200
        writer = app.state.registry.writer
        original = writer.append

        def failing(*args: Any, **kwargs: Any) -> Any:
            raise OSError("no space")

        writer.append = failing  # type: ignore[method-assign]
        response = client.post("/v1/samples", content=json.dumps({**SAMPLE, "seq": 1}).encode())
        writer.append = original  # type: ignore[method-assign]
        assert response.status_code == 503 and response.json() == {"error": "storage_failure"}
        assert client.get("/healthz").status_code == 200
        assert client.post("/v1/samples", content=json.dumps({**SAMPLE, "seq": 1}).encode()).json()["status"] == "stored"
