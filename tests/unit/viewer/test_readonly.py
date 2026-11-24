"""The Viewer never changes the Runs_Directory (task 1.12).

A synthetic Runs_Directory is built under `tmp_path` with `RunsBuilder`. The app runs in process
through starlette's `TestClient` (no socket). A before/after snapshot of every file (name, size,
SHA-256) and of the whole `tmp_path` tree (files and directories) must be identical after a mix of
valid, invalid and hostile requests and after direct calls to `list_run_entries`, `find_run` and
`read_artifact`. The real `runs/` directory is never read.

Requirements: 2.1, 2.2, 3.3, 10.2, 10.5, 11.7, 11.8, 11.11, 11.42.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coroner.viewer.app import create_app
from coroner.viewer.files import (
    ArtifactName,
    find_run,
    list_run_entries,
    open_runs_root,
    read_artifact,
)
from tests.unit.viewer.builder import GapSpec, RunsBuilder, sample_specs

PORT = 8765


class StubProvider:
    def list_runs(self) -> Mapping[str, Any]:
        return {"stub": "list"}

    def load_detail(self, run_id: str) -> Mapping[str, Any] | None:
        return {"stub": "detail", "run_id": run_id}


def tree(root: Path) -> dict[str, tuple[str, int]]:
    """Every entry under `root` (files and directories, links not followed): kind and size."""
    out: dict[str, tuple[str, int]] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            out[(Path(current) / name).relative_to(root).as_posix()] = ("dir", 0)
        for name in files:
            p = Path(current) / name
            out[p.relative_to(root).as_posix()] = ("file", p.lstat().st_size)
    return out


@pytest.fixture
def env(tmp_path: Path):
    runs = tmp_path / "runs"
    builder = RunsBuilder(runs)
    ids = [
        builder.add_run("A", samples=sample_specs(6)),
        builder.add_run("B", samples=sample_specs(8), gaps=[GapSpec(first_missing_seq=3, last_missing_seq=4)]),
        builder.add_run("C", samples=sample_specs(4), inspect=None),
        builder.add_run("B", run_id="b-20261120T211300Z-badmanif", manifest_bytes=b"{not json"),
    ]
    builder.add_loose_file("notes.txt", b"loose file")
    builder.add_empty_dir("not-a-run")
    app = create_app(runs, PORT, provider=StubProvider())
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)
    return tmp_path, runs, builder, ids, client


def _get(client: TestClient, path: str, host: str | None = None, **kw: Any) -> Any:
    headers = {"Host": host if host is not None else f"127.0.0.1:{PORT}"}
    headers.update(kw.pop("headers", {}))
    return client.get(path, headers=headers, **kw)


def test_http_request_mix_leaves_runs_dir_unchanged(env) -> None:
    tmp_path, _runs, builder, ids, client = env
    before_files = builder.snapshot()
    before_tree = tree(tmp_path)
    assert before_files and before_tree

    paths = ["/", "/compare", "/compare?a=x&b=y", "/api/runs", "/runs", "/runs/", "/nope", "/favicon.ico"]
    for rid in ids:
        paths += [f"/runs/{rid}", f"/api/runs/{rid}", f"/api/runs/{rid}?x=1"]
    paths += [
        "/runs/b-20261120T211300Z-00000000", "/api/runs/b-20261120T211300Z-00000000",
        "/runs/..", "/runs/../runs", "/api/runs/..", "/api/runs/../../etc/passwd", "/api/runs/%2e%2e",
        "/api/runs/%2e%2e%2f%2e%2e%2fsecret", "/api/runs/a%5Cb", "/api/runs/a%2Fb", "/api/runs/%00",
        f"/api/runs/{ids[0]}%0A", f"/runs/{ids[0]}%0A", "/api/runs/" + "a" * 5000, "/runs/" + "%41" * 3000,
        "/api/runs/CON", "/api/runs/.", "/api/runs/notes.txt", "/api/runs/not-a-run", "/runs/notes.txt",
        "/static/index.html", "/static/", "/static/..%2f..%2fcoroner%2fviewer%2fapp.py", "/static/missing.js",
        "/static/../app.py", "/static/%2e%2e/app.py", "/api/", "/api", "//", "/runs//x",
    ]
    for path in paths:
        response = _get(client, path)
        assert response.status_code < 600

    # Host-guard rejections, bad methods and odd headers.
    for host in ("evil.example", "localhost:8765", "127.0.0.1:1", "[::1]:8765", "0.0.0.0:8765"):
        _get(client, f"/api/runs/{ids[0]}", host=host)
        _get(client, "/", host=host)
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
        for path in ("/", f"/api/runs/{ids[0]}", "/api/runs", "/static/index.html", "/compare"):
            client.request(method, path, headers={"Host": f"127.0.0.1:{PORT}"}, content=b"x=1")
    _get(client, f"/api/runs/{ids[0]}", headers={"Range": "bytes=0-5", "If-None-Match": "*"})

    assert builder.snapshot() == before_files
    assert tree(tmp_path) == before_tree


def test_direct_file_api_calls_leave_runs_dir_unchanged(env) -> None:
    tmp_path, runs, builder, ids, _client = env
    before_files = builder.snapshot()
    before_tree = tree(tmp_path)

    root = open_runs_root(runs)
    entries = list_run_entries(root)
    assert [e.run_id for e in entries] == sorted(ids)
    for entry in entries:
        assert find_run(root, entry.run_id) == entry
        for name in ArtifactName:
            read_artifact(root, entry, name)
    assert find_run(root, "a-20261120T211300Z-ffffffff") is None
    with pytest.raises(Exception):
        find_run(root, "../x")

    assert builder.snapshot() == before_files
    assert tree(tmp_path) == before_tree


def test_snapshot_detects_a_change(env) -> None:
    """Control: the snapshot comparison is sensitive to an append and to a new file."""
    tmp_path, runs, builder, ids, _client = env
    before_files = builder.snapshot()
    before_tree = tree(tmp_path)
    manifest = runs / ids[0] / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    assert builder.snapshot() != before_files
    (runs / "new.txt").write_bytes(b"x")
    assert tree(tmp_path) != before_tree
