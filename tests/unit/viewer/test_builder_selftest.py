"""Self-test of the fixture builder (task 1.3; requirements 11.1, 11.5, 11.7).

Everything is written under `tmp_path`; the real `runs/` directory is never read.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest

from coroner.evidence import schema
from coroner.evidence.reader import read_evidence
from coroner.runner.manifest import deserialize_manifest, extract_inspect_excerpt
from tests.unit.viewer.builder import (
    GapSpec,
    InspectSpec,
    RunsBuilder,
    SampleSpec,
    StateSpec,
    sample_specs,
    serialize_line,
)

TERMINATION_TEMPLATE = {
    "record_type": "termination", "schema_version": 1, "run_id": None, "recv_time": "2026-11-20T21:13:21.800Z",
    "source": "experiment_runner", "observed_time": "2026-11-20T21:13:21.800Z",
    "container_state": {"status": "exited", "exit_code": 137, "oom_killed": False},
    "cause_label": "sigkill_sent_by_runner", "note": "Observed after the fact.",
}


def full_run(builder: RunsBuilder, experiment: str = "B") -> str:
    samples = sample_specs(4) + [SampleSpec(seq=4, rss_bytes=None)]
    gaps = [GapSpec(first_missing_seq=5, last_missing_seq=6), GapSpec(kind="silence_timeout")]
    run_id = f"{experiment.lower()}-20261120T211300Z-feedc0de"
    term = dict(TERMINATION_TEMPLATE, run_id=run_id)
    return builder.add_run(experiment, run_id=run_id, samples=samples, gaps=gaps, extra_records=[term])


def test_every_evidence_line_is_schema_valid_and_read_back(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path)
    run_id = full_run(builder)
    data = (tmp_path / run_id / "evidence.jsonl").read_bytes()
    assert data.endswith(b"\n") and b"\r" not in data
    lines = data.splitlines()
    assert len(lines) == 1 + 5 + 2 + 1  # run_metadata, samples, gaps, termination
    for line in lines:
        assert schema.validate_record(json.loads(line)) == []
    result = read_evidence(data, name=f"{run_id}.jsonl")
    assert result.errors == []
    assert len(result.records) == len(lines)
    assert [r["record_type"] for r in result.records][:2] == ["run_metadata", "sample"]
    assert result.records[5]["sample"]["rss_bytes"] is None


@pytest.mark.parametrize("experiment", ["A", "B", "C", "b"])
def test_manifests_pass_deserialize_and_count_matches(tmp_path: Path, experiment: str) -> None:
    builder = RunsBuilder(tmp_path)
    run_id = builder.add_run(experiment, samples=sample_specs(3))
    manifest = deserialize_manifest((tmp_path / run_id / "manifest.json").read_bytes())
    assert manifest.run_id == run_id and manifest.experiment == experiment
    assert manifest.evidence is not None and manifest.evidence.record_count == 4  # metadata + 3 samples
    assert manifest.container_state is not None and len(manifest.container_state.container_id) == 64
    assert "times" in manifest.extra
    raw = (tmp_path / run_id / "manifest.json").read_bytes()
    assert raw.endswith(b"\n") and not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw


def test_manifest_options_null_state_absent_inspect_and_overrides(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path)
    no_state = builder.add_run("A", container_state=None)
    assert deserialize_manifest((tmp_path / no_state / "manifest.json").read_bytes()).container_state is None
    assert not (tmp_path / no_state / "docker_inspect.json").exists()

    no_inspect = builder.add_run("C", inspect=None, container_state=StateSpec(oom_killed=True))
    assert not (tmp_path / no_inspect / "docker_inspect.json").exists()

    b = builder.add_run("B", clock_offset_ms=None, uncertainty_ms=None, sample_timing=None, outcome="failed",
                        manifest_extra={"checks": [{"name": "x"}]})
    manifest = deserialize_manifest((tmp_path / b / "manifest.json").read_bytes())
    assert manifest.outcome == "failed" and manifest.failure is not None
    assert "clock_offset_ms" not in manifest.extra and "sample_timing" not in manifest.extra
    assert manifest.extra["checks"] == [{"name": "x"}]

    raw = builder.add_run("B", manifest_bytes=b"{not json", evidence_bytes=b"garbage\n")
    assert (tmp_path / raw / "manifest.json").read_bytes() == b"{not json"
    assert (tmp_path / raw / "evidence.jsonl").read_bytes() == b"garbage\n"

    with pytest.raises(FileExistsError):
        builder.add_run("B", run_id=raw)


def test_inspect_document_is_a_one_element_template_list(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path)
    run_id = builder.add_run("B", inspect=InspectSpec(exit_code=137, oom_killed=False))
    doc = json.loads((tmp_path / run_id / "docker_inspect.json").read_bytes())
    assert isinstance(doc, list) and len(doc) == 1
    entry = doc[0]
    assert re.fullmatch(r"[0-9a-f]{64}", entry["Id"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{9}Z", entry["State"]["FinishedAt"])
    assert set(entry) == {"Id", "State", "HostConfig"}
    excerpt = extract_inspect_excerpt((tmp_path / run_id / "docker_inspect.json").read_bytes())
    assert excerpt.exit_code == 137 and excerpt.id == entry["Id"]
    manifest = deserialize_manifest((tmp_path / run_id / "manifest.json").read_bytes())
    assert manifest.container_state is not None and manifest.container_state.container_id == entry["Id"]
    assert manifest.extra["container_finished_at"].endswith(".760Z")  # truncated to milliseconds


def test_snapshot_is_stable_and_detects_changes(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path)
    run_id = builder.add_run("B", samples=sample_specs(2))
    builder.add_loose_file("stray.txt", b"abc")
    builder.add_empty_dir("empty")
    first = builder.snapshot()
    assert first == builder.snapshot()
    assert set(first) == {
        f"{run_id}/manifest.json", f"{run_id}/evidence.jsonl", f"{run_id}/docker_inspect.json", "stray.txt",
    }
    assert first["stray.txt"][0] == 3
    (tmp_path / "stray.txt").write_bytes(b"abd")
    assert builder.snapshot()["stray.txt"] != first["stray.txt"]


def test_two_builders_in_one_root_do_not_collide_and_serialize_line_is_ascii(tmp_path: Path) -> None:
    a = RunsBuilder(tmp_path).add_run("B")
    b = RunsBuilder(tmp_path).add_run("B", run_id="b-other")
    assert a != b
    assert serialize_line({"k": "é"}) == b'{"k":"\\u00e9"}\n'


def test_try_symlink_reports_platform_support(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    target = tmp_path / "outside"
    target.mkdir()
    link_path = tmp_path / "runs" / "link"
    created = builder.try_symlink("link", target)
    assert isinstance(created, bool)
    if created:
        assert os.path.islink(link_path)
        assert stat.S_ISLNK(os.lstat(link_path).st_mode)
        assert "link" not in builder.snapshot()
    else:
        assert not os.path.lexists(link_path)
