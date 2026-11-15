"""Requirement 13.13 as end-to-end examples: damage, repair, append, read (task 2.14; design D12, D19, D26, D27).

Real `EvidenceWriter` (Crash_Durable, with an fsync double that records what it saw), real `repair_file`, real
`read_evidence` and `summarize`, on temporary files. These are the canonical walk-throughs; the details of
`repair_file` and `read_evidence` alone are in test_repair.py and test_reader.py, and the generated version of the
reader contract is Property 2 in test_prop_reader_quarantine.py.
**Validates: Requirements 5.5, 5.7, 5.10, 13.1, 13.13**
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from coroner.evidence.reader import read_evidence, summarize
from coroner.evidence.repair import repair_file
from coroner.evidence.writer import Durability, EvidenceWriter, TailRepairRequired, serialize_record

RUN = "r1"
NAME = "r1.jsonl"
NOW = "2025-01-01T12:05:00.004Z"
RECV = "2025-01-01T12:00:06.530Z"


def sample(seq: int) -> dict:
    body = {
        "run_id": RUN, "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 71303168, "request_count": 9, "loop_delay_ms": 0.84,
        "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo", "interval_ms": 500, "dropped": 0,
    }
    return {"record_type": "sample", "schema_version": 1, "run_id": RUN, "recv_time": RECV, "sample": body}


class Recorder:
    """fsync double: records (kind, file size at that moment) so the order of persistence steps can be checked."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []
        self.path: Path | None = None

    def fsync(self, fd: int) -> None:
        self.events.append(("fsync_file", os.fstat(fd).st_size))

    def fsync_dir(self, path: Path) -> None:
        self.events.append(("fsync_dir", self.path.stat().st_size if self.path else -1))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return NOW


FRAGMENTS = {
    "partial": b'{"record_type":"sample","sche',
    "validsample": serialize_record(sample(2)).rstrip(b"\n"),  # a complete, valid Sample that lost only its newline
    "badutf8": b'{"a":"\xff',
    "validobj": b'{"a":1}',
}


class Scenario:
    """Two acknowledged Samples, then a damaged last line, then repair, then one more Sample."""

    def __init__(self, tmp_path: Path, fragment: bytes, terminated: bool = False) -> None:
        directory = tmp_path / "evidence"
        directory.mkdir()
        self.rec = Recorder()
        self.writer = EvidenceWriter(directory, Durability.CRASH, fsync=self.rec.fsync, fsync_dir=self.rec.fsync_dir)
        self.fragment = fragment
        self.writer.append(RUN, [sample(0), sample(1)])  # creates the file: one file fsync and one directory fsync
        self.path = self.writer.path_for(RUN)
        self.rec.path = self.path
        self.good = self.path.read_bytes()
        with open(self.path, "ab") as handle:  # the crash: damaged bytes land after the acknowledged lines
            handle.write(fragment + (b"\n" if terminated else b""))
        self.damaged = self.path.read_bytes()
        self.events_before_repair = list(self.rec.events)
        self.repair = repair_file(self.path, self.writer, now)
        self.repaired = self.path.read_bytes()
        self.events_after_repair = list(self.rec.events)

    def append_sample(self) -> bytes:
        self.writer.append(RUN, [sample(3)])
        return self.path.read_bytes()


@pytest.fixture(params=sorted(FRAGMENTS), ids=sorted(FRAGMENTS))
def scenario(request: pytest.FixtureRequest, tmp_path: Path) -> Scenario:
    return Scenario(tmp_path, FRAGMENTS[request.param])


def test_the_original_damaged_fragment_is_preserved_byte_for_byte(scenario: Scenario) -> None:
    assert scenario.damaged == scenario.good + scenario.fragment
    assert scenario.repaired.startswith(scenario.damaged)  # the file only grew; the damaged bytes are an unchanged prefix
    assert sha(scenario.repaired[: len(scenario.damaged)]) == sha(scenario.damaged)
    # D30: the NUL invalidating marker, then the isolating newline
    assert scenario.repaired[len(scenario.damaged) : len(scenario.damaged) + 2] == b"\x00\n"
    start = len(scenario.good)  # the fragment is still at its original offset, as line 3
    assert scenario.repaired[start : start + len(scenario.fragment)] == scenario.fragment


def test_the_repair_record_holds_the_fragment_its_length_file_and_line_number(scenario: Scenario) -> None:
    record = scenario.repair
    assert record is not None
    assert record["record_type"] == "repair" and record["run_id"] == RUN and record["action"] == "appended_newline"
    assert record["file"] == NAME and record["line_no"] == 3
    assert record["fragment_len"] == len(scenario.fragment) and record["fragment_truncated"] is False
    assert base64.b64decode(record["fragment_b64"]) == scenario.fragment
    written = json.loads(scenario.repaired.splitlines()[-1])  # what is on disk is what was returned
    assert written == record


def test_the_repair_is_durable_before_the_next_append_and_append_before_repair_is_refused(tmp_path: Path) -> None:
    # Built by hand (not with Scenario) so that an append can be attempted BEFORE the repair.
    directory = tmp_path / "evidence"
    directory.mkdir()
    rec = Recorder()
    writer = EvidenceWriter(directory, Durability.CRASH, fsync=rec.fsync, fsync_dir=rec.fsync_dir)
    writer.append(RUN, [sample(0), sample(1)])
    path = writer.path_for(RUN)
    rec.path = path
    assert [kind for kind, _ in rec.events] == ["fsync_file", "fsync_dir"]  # creation: file, then directory
    with open(path, "ab") as handle:
        handle.write(FRAGMENTS["partial"])
    damaged, events = path.read_bytes(), list(rec.events)
    with pytest.raises(TailRepairRequired):
        writer.append(RUN, [sample(3)])
    assert path.read_bytes() == damaged and rec.events == events  # nothing written, nothing fsynced
    repair_file(path, writer, now)
    repaired = path.read_bytes()
    assert rec.events[len(events):] == [("fsync_file", len(repaired))]  # one fsync, taken when the whole repair (newline + record) was in the file
    writer.append(RUN, [sample(3)])
    final = path.read_bytes()
    assert rec.events[-1] == ("fsync_file", len(final)) and len(final) > len(repaired)  # the append is a later, separate persistence step
    assert [size for kind, size in rec.events if kind == "fsync_file"] == sorted(size for kind, size in rec.events if kind == "fsync_file")


def test_new_evidence_starts_on_a_fresh_line(scenario: Scenario) -> None:
    final = scenario.append_sample()
    assert final.startswith(scenario.repaired)
    new_line = serialize_record(sample(3))
    assert final.endswith(new_line) and final[-len(new_line) - 1 : -len(new_line)] == b"\n"
    assert final.count(b"\n") == scenario.good.count(b"\n") + 3  # fragment line, repair line, new line
    assert read_evidence(scenario.path).line_numbers == [1, 2, 4, 5]


def test_the_reader_reports_the_damaged_line_accurately(scenario: Scenario) -> None:
    result = read_evidence(scenario.path)
    assert len(result.errors) == 1
    (error,) = result.errors
    # the physical line is the fragment plus the one-byte D30 marker
    assert (error.file, error.line_no, error.byte_length) == (NAME, 3, len(scenario.fragment) + 1)
    assert error.kind == "isolated_by_repair" and error.has_repair_record is True
    assert fragment_not_in_records(result, scenario.fragment)


def fragment_not_in_records(result, fragment: bytes) -> bool:
    return all(serialize_record(r).rstrip(b"\n") != fragment for r in result.records)


def test_a_fragment_that_is_a_valid_sample_is_still_quarantined_and_not_counted(tmp_path: Path) -> None:
    s = Scenario(tmp_path, FRAGMENTS["validsample"])
    assert json.loads(s.fragment)["record_type"] == "sample"  # it parses and would be a valid record on its own
    result = read_evidence(s.path)
    assert [(e.line_no, e.kind, e.has_repair_record) for e in result.errors] == [(3, "isolated_by_repair", True)]
    assert result.line_numbers == [1, 2, 4]
    s.append_sample()
    summary = summarize(read_evidence(s.path))
    assert summary.sample_count == 3  # seq 0, 1 and 3; the isolated seq 2 is not counted
    assert summary.malformed_line_count == 1


def test_the_repair_record_is_not_counted_as_a_sample(scenario: Scenario) -> None:
    result = read_evidence(scenario.path)
    assert [r["record_type"] for r in result.records] == ["sample", "sample", "repair"]
    assert summarize(result).sample_count == 2 and summarize(result).malformed_line_count == 1
    scenario.append_sample()
    assert summarize(read_evidence(scenario.path)).sample_count == 3


def test_the_file_is_never_truncated_across_every_step(scenario: Scenario) -> None:
    sizes = [len(scenario.good), len(scenario.damaged), len(scenario.repaired)]
    assert repair_file(scenario.path, scenario.writer, now) is None  # a second repair changes nothing
    sizes.append(scenario.path.stat().st_size)
    final = scenario.append_sample()
    sizes.append(len(final))
    read_evidence(scenario.path)  # reading changes nothing either
    sizes.append(scenario.path.stat().st_size)
    assert sizes == sorted(sizes) and sizes[2] > sizes[1] and sizes[3] == sizes[2] and sizes[4] > sizes[3] and sizes[5] == sizes[4]
    assert final.startswith(scenario.good + scenario.fragment)


def test_a_terminated_invalid_last_line_keeps_its_own_kind_with_a_repair_record(tmp_path: Path) -> None:
    s = Scenario(tmp_path, b"{not json", terminated=True)
    assert s.repair is not None and s.repair["action"] == "none_needed_already_terminated" and s.repair["line_no"] == 3
    assert s.repaired == s.damaged + serialize_record(s.repair)  # no isolating newline was needed
    result = read_evidence(s.path)
    assert [(e.file, e.line_no, e.byte_length, e.kind, e.has_repair_record) for e in result.errors] == [(NAME, 3, 9, "invalid_json", True)]
    assert result.line_numbers == [1, 2, 4]
    s.append_sample()
    assert summarize(read_evidence(s.path)).sample_count == 3


def test_a_fragment_without_a_repair_record_is_just_an_incomplete_last_line(tmp_path: Path) -> None:
    """Contrast: before repair the damaged tail is `incomplete_last_line` with no repair record, and is not counted."""
    s = Scenario(tmp_path, FRAGMENTS["validsample"])
    result = read_evidence(s.damaged, name=NAME)
    assert [(e.line_no, e.kind, e.has_repair_record) for e in result.errors] == [(3, "incomplete_last_line", False)]
    assert summarize(result).sample_count == 2
