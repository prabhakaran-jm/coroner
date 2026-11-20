"""Property 25: Run_Manifest and inspect excerpt fidelity (task 6.10).

*For any* Run_Manifest, writing it and reading it back yields an equal manifest, and for any
`docker inspect` JSON the extracted excerpt contains `State.OOMKilled`, `State.ExitCode`,
`State.Status`, the full 64-character `Id`, and `HostConfig.Memory` equal to the source values,
with the raw output saved unchanged.

**Validates: Requirements 11.13, 11.14, 12.2, 12.7**

Oracles are plain `json` and byte comparisons on what the tests generated; they do not call the
module's serializer. All files live under temporary directories, never the repository `runs/`.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path, PureWindowsPath

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coroner.runner.manifest import (
    ContainerState,
    EvidenceRef,
    Expected,
    Failure,
    InspectError,
    InspectExcerpt,
    ManifestConflictError,
    ManifestError,
    RunIdError,
    RunManifest,
    atomic_write_bytes,
    deserialize_manifest,
    extract_inspect_excerpt,
    manifest_path,
    posix_path,
    read_manifest,
    save_raw_inspect,
    serialize_manifest,
    write_manifest,
)

CORE_KEYS = {
    "schema_version", "experiment", "run_id", "outcome", "failure", "start_time", "end_time",
    "durability_level", "termination_description", "expected", "container_state", "evidence",
}

text = st.text(max_size=20)  # includes non-ASCII; surrogates excluded by default
run_ids = st.from_regex(r"[a-z0-9]{1,8}-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", fullmatch=True)
cids = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
ints = st.integers(min_value=-(2**40), max_value=2**40)
json_leaf = st.none() | st.booleans() | ints | text
json_values = st.recursive(
    json_leaf,
    lambda c: st.lists(c, max_size=3) | st.dictionaries(text, c, max_size=3),
    max_leaves=8,
)
posix_paths = st.from_regex(r"runs/[a-z0-9-]{1,10}/[a-z_.]{1,12}", fullmatch=True)


@st.composite
def inspect_objs(draw):
    obj = {
        "Id": draw(cids),
        "State": {
            "Status": draw(st.sampled_from(["running", "exited", "dead", "créé"])),
            "OOMKilled": draw(st.booleans()),
            "ExitCode": draw(st.integers(-1, 255)),
            "FinishedAt": draw(text),
        },
        "HostConfig": {"Memory": draw(st.integers(0, 2**40)), "MemorySwap": draw(ints)},
        "Name": draw(text),
        "Config": draw(json_values),
    }
    return obj


@st.composite
def manifests(draw):
    state = None
    if draw(st.booleans()):
        ex = InspectExcerpt(
            id=draw(cids), status=draw(text), oom_killed=draw(st.booleans()),
            exit_code=draw(ints), memory=draw(st.integers(0, 2**40)),
        )
        state = ContainerState(
            status=ex.status, exit_code=ex.exit_code, oom_killed=ex.oom_killed, container_id=ex.id,
            host_config_memory_bytes=ex.memory, inspect_time=draw(text),
            raw_inspect_path=draw(posix_paths), raw_inspect_excerpt=ex,
        )
    evidence = None
    if draw(st.booleans()):
        evidence = EvidenceRef(draw(posix_paths), draw(posix_paths), draw(st.integers(0, 10**6)))
    failure = Failure(draw(text), draw(text)) if draw(st.booleans()) else None
    extra = draw(st.dictionaries(text.filter(lambda k: k not in CORE_KEYS), json_values, max_size=4))
    return RunManifest(
        experiment=draw(st.sampled_from(["A", "B", "C"])),
        run_id=draw(run_ids),
        outcome=draw(st.sampled_from(["passed", "failed", "aborted"])),
        start_time=draw(text), end_time=draw(text), durability_level=draw(text),
        termination_description=draw(text),
        expected=Expected(draw(ints), draw(st.booleans())),
        failure=failure, container_state=state, evidence=evidence, extra=extra,
    )


def make_manifest(run_id="c-20250101T121500Z-4be07a11", **kw) -> RunManifest:
    base = dict(
        experiment="C", run_id=run_id, outcome="passed", start_time="s", end_time="e",
        durability_level="crash", termination_description="d", expected=Expected(137, True),
    )
    base.update(kw)
    return RunManifest(**base)


# ---------------------------------------------------------------- Property 25: manifest round trip


@settings(max_examples=150, deadline=None)
@given(m=manifests())
def test_write_then_read_yields_equal_manifest(m):
    with tempfile.TemporaryDirectory() as tmp:
        path = write_manifest(tmp, m)
        assert path == Path(tmp) / m.run_id / "manifest.json"
        raw = path.read_bytes()
        assert read_manifest(path) == m
        assert deserialize_manifest(serialize_manifest(m)) == m
        # Oracle on bytes: UTF-8, no BOM, LF only, trailing newline, no leftover temp files.
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in raw
        assert raw.endswith(b"\n")
        parsed = json.loads(raw.decode("utf-8"))
        assert parsed["run_id"] == m.run_id and parsed["schema_version"] == 1
        for k, v in m.extra.items():
            assert parsed[k] == v
        assert [p.name for p in path.parent.iterdir()] == ["manifest.json"]
        # Rewriting the same run is allowed and stays equal.
        write_manifest(tmp, m)
        assert read_manifest(path) == m


@settings(max_examples=100, deadline=None)
@given(obj=inspect_objs(), wrap=st.booleans(), ascii_only=st.booleans())
def test_excerpt_matches_source_and_has_only_specified_fields(obj, wrap, ascii_only):
    raw = json.dumps([obj] if wrap else obj, ensure_ascii=ascii_only, indent=2).encode("utf-8")
    ex = extract_inspect_excerpt(raw)
    d = ex.to_dict()
    assert d["Id"] == obj["Id"] and len(d["Id"]) == 64
    assert d["State"] == {
        "Status": obj["State"]["Status"],
        "OOMKilled": obj["State"]["OOMKilled"],
        "ExitCode": obj["State"]["ExitCode"],
    }
    assert d["HostConfig"] == {"Memory": obj["HostConfig"]["Memory"]}
    assert set(d) == {"Id", "State", "HostConfig"}
    cs = ContainerState.from_inspect(raw, "t", "runs/x/docker_inspect.json")
    assert (cs.status, cs.exit_code, cs.oom_killed, cs.container_id, cs.host_config_memory_bytes) == (
        obj["State"]["Status"], obj["State"]["ExitCode"], obj["State"]["OOMKilled"],
        obj["Id"], obj["HostConfig"]["Memory"],
    )
    assert cs.raw_inspect_excerpt == ex


@settings(max_examples=100, deadline=None)
@given(raw=st.binary(max_size=300) | st.builds(lambda s: s.encode("utf-8"), st.text(max_size=100)))
def test_raw_inspect_saved_byte_for_byte(raw):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "r" / "docker_inspect.json"
        save_raw_inspect(p, raw)
        assert p.read_bytes() == raw
        assert [x.name for x in p.parent.iterdir()] == ["docker_inspect.json"]


def test_raw_inspect_crlf_and_non_ascii_preserved(tmp_path):
    raw = b'[{"Id":"' + b"a" * 64 + b'",\r\n "Name":"caf\xc3\xa9 \xe2\x98\x83"}]\r\n\r\n'
    p = tmp_path / "docker_inspect.json"
    save_raw_inspect(p, raw)
    assert p.read_bytes() == raw
    assert b"\r\n" in p.read_bytes()


def test_raw_inspect_rejects_str(tmp_path):
    with pytest.raises(TypeError):
        save_raw_inspect(tmp_path / "x", "text")  # type: ignore[arg-type]


# ---------------------------------------------------------------- inspect extraction errors


def good_inspect() -> dict:
    return {
        "Id": "a" * 64,
        "State": {"Status": "exited", "OOMKilled": True, "ExitCode": 137},
        "HostConfig": {"Memory": 134217728},
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: o.update(Id="a" * 12),
        lambda o: o.pop("Id"),
        lambda o: o.pop("State"),
        lambda o: o["State"].update(OOMKilled="true"),
        lambda o: o["State"].update(OOMKilled=1),
        lambda o: o["State"].update(ExitCode=True),
        lambda o: o["State"].update(ExitCode="137"),
        lambda o: o["State"].pop("Status"),
        lambda o: o.pop("HostConfig"),
        lambda o: o["HostConfig"].update(Memory="128m"),
        lambda o: o["HostConfig"].update(Memory=None),
    ],
)
def test_excerpt_rejects_missing_or_mistyped_fields(mutate):
    obj = good_inspect()
    mutate(obj)
    with pytest.raises(InspectError):
        extract_inspect_excerpt(json.dumps(obj))


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"[{}, {}]", b"3", b"\xff\xfe\x00"])
def test_excerpt_rejects_bad_documents(raw):
    with pytest.raises(InspectError):
        extract_inspect_excerpt(raw)


# ---------------------------------------------------------------- paths


@pytest.mark.parametrize(
    "given_path,expected",
    [
        ("runs\\c-1\\docker_inspect.json", "runs/c-1/docker_inspect.json"),
        (PureWindowsPath("runs\\c-1\\evidence.jsonl"), "runs/c-1/evidence.jsonl"),
        ("runs/c-1/x", "runs/c-1/x"),
        ("C:\\work\\runs\\x.json", "C:/work/runs/x.json"),
    ],
)
def test_posix_path_normalizes_backslashes(given_path, expected):
    assert posix_path(given_path) == expected


def test_backslash_paths_are_stored_with_forward_slashes(tmp_path):
    ex = InspectExcerpt("a" * 64, "exited", True, 137, 1)
    cs = ContainerState("exited", 137, True, "a" * 64, 1, "t", "runs\\r\\docker_inspect.json", ex)
    ev = EvidenceRef("/data/evidence/r.jsonl", PureWindowsPath("runs\\r\\evidence.jsonl"), 3)  # type: ignore[arg-type]
    m = make_manifest(container_state=cs, evidence=ev)
    path = write_manifest(tmp_path, m)
    data = json.loads(path.read_bytes())
    assert data["container_state"]["raw_inspect_path"] == "runs/r/docker_inspect.json"
    assert data["evidence"]["host_copy_path"] == "runs/r/evidence.jsonl"
    assert b"\\\\" not in path.read_bytes()
    assert read_manifest(path) == m


# ---------------------------------------------------------------- atomicity and errors


def test_fsync_failure_keeps_previous_manifest_and_cleans_temp(tmp_path):
    old = make_manifest(outcome="passed")
    path = write_manifest(tmp_path, old)
    before = path.read_bytes()

    def boom(fd):
        raise OSError(5, "fsync failed")

    with pytest.raises(OSError):
        write_manifest(tmp_path, make_manifest(outcome="failed"), fsync=boom)
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == ["manifest.json"]


def test_replace_failure_keeps_previous_manifest_and_cleans_temp(tmp_path):
    path = write_manifest(tmp_path, make_manifest(outcome="passed"))
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError(13, "denied")

    with pytest.raises(OSError):
        write_manifest(tmp_path, make_manifest(outcome="failed"), replace=boom)
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == ["manifest.json"]


def test_fsync_is_called_on_temp_before_replace(tmp_path):
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fs(fd):
        events.append("fsync")
        real_fsync(fd)

    def rp(src, dst):
        events.append("replace")
        assert Path(src).exists() and Path(src).name.endswith(".tmp")
        real_replace(src, dst)

    atomic_write_bytes(tmp_path / "f.bin", b"x", fsync=fs, replace=rp)
    assert events == ["fsync", "replace"]
    assert (tmp_path / "f.bin").read_bytes() == b"x"


def test_atomic_replacement_overwrites_same_run(tmp_path):
    write_manifest(tmp_path, make_manifest(outcome="aborted"))
    path = write_manifest(tmp_path, make_manifest(outcome="passed"))
    assert read_manifest(path).outcome == "passed"


def test_unrelated_manifest_is_not_overwritten(tmp_path):
    a = make_manifest(run_id="a-20250101T000000Z-aaaaaaaa")
    path = write_manifest(tmp_path, a)
    other = make_manifest(run_id="b-20250101T000000Z-bbbbbbbb")
    # Plant a's manifest where b's belongs.
    target = manifest_path(tmp_path, other.run_id)
    target.parent.mkdir()
    target.write_bytes(path.read_bytes())
    before = target.read_bytes()
    with pytest.raises(ManifestConflictError):
        write_manifest(tmp_path, other)
    assert target.read_bytes() == before


def test_unreadable_existing_manifest_is_not_overwritten(tmp_path):
    m = make_manifest()
    target = manifest_path(tmp_path, m.run_id)
    target.parent.mkdir()
    target.write_bytes(b"{ not json")
    with pytest.raises(ManifestConflictError):
        write_manifest(tmp_path, m)
    assert target.read_bytes() == b"{ not json"


def test_invalid_manifest_raises_before_touching_disk(tmp_path):
    m = make_manifest()
    path = write_manifest(tmp_path, m)
    before = path.read_bytes()
    bad = make_manifest(outcome="exploded")
    with pytest.raises(ManifestError):
        write_manifest(tmp_path, bad)
    clash = make_manifest(extra={"run_id": "other"})
    with pytest.raises(ManifestError):
        write_manifest(tmp_path, clash)
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == [m.run_id]


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a\\b", "_x", ".x", "a" * 65, "a\n", "x y"])
def test_invalid_run_ids_rejected_for_paths(tmp_path, bad):
    with pytest.raises(RunIdError):
        manifest_path(tmp_path, bad)
    with pytest.raises(RunIdError):
        write_manifest(tmp_path, make_manifest(run_id=bad))
    assert list(tmp_path.iterdir()) == []


def test_corrupt_documents_raise_manifest_error():
    good = json.loads(serialize_manifest(make_manifest()))
    for mutate in (
        lambda d: d.pop("run_id"),
        lambda d: d.update(schema_version=2),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(run_id="../x"),
        lambda d: d.update(outcome="nope"),
        lambda d: d.update(expected={"exit_code": "137", "oom_killed": True}),
        lambda d: d.update(container_state={"status": "x"}),
    ):
        d = json.loads(json.dumps(good))
        mutate(d)
        with pytest.raises(ManifestError):
            RunManifest.from_dict(d)
    for blob in (b"\xff\xfe", b"[1]", b"", b"{"):
        with pytest.raises(ManifestError):
            deserialize_manifest(blob)


def test_unknown_blocks_survive_round_trip(tmp_path):
    extra = {"memory": {"configured_limit_bytes": 1}, "checks": [{"name": "n", "passed": True}], "limitations": ["é"]}
    m = make_manifest(extra=extra)
    assert read_manifest(write_manifest(tmp_path, m)).extra == extra


def test_manifest_with_null_state_and_failure_round_trips(tmp_path):
    m = make_manifest(outcome="aborted", failure=Failure("docker_inspect_failed", "x"), container_state=None)
    assert read_manifest(write_manifest(tmp_path, m)) == m


# ---------------------------------------------------------------- negative controls (fixtures only)


def naive_text_mode_serialize(m: RunManifest) -> bytes:
    """Fixture: translates LF to CRLF, as text mode does on Windows."""
    return serialize_manifest(m).replace(b"\n", b"\r\n")


def naive_in_place_write(path: Path, data: bytes, crash_after: int) -> None:
    """Fixture: non-atomic write that dies partway through."""
    with open(path, "wb") as h:
        h.write(data[:crash_after])
    raise OSError("simulated crash")


def test_negative_control_crlf_translation_is_detected():
    raw = naive_text_mode_serialize(make_manifest())
    assert b"\r" in raw  # the oracle used in the round-trip test would fail on this output


def test_negative_control_non_atomic_write_corrupts_previous(tmp_path):
    path = write_manifest(tmp_path, make_manifest())
    before = path.read_bytes()
    with pytest.raises(OSError):
        naive_in_place_write(path, serialize_manifest(make_manifest(outcome="failed")), 20)
    assert path.read_bytes() != before  # control: the intact-previous assertions would catch this
    with pytest.raises(ManifestError):
        read_manifest(path)


def test_negative_control_excerpt_oracle_detects_wrong_value():
    obj = good_inspect()
    ex = extract_inspect_excerpt(json.dumps(obj))
    tampered = ex.to_dict()
    tampered["State"]["OOMKilled"] = not obj["State"]["OOMKilled"]
    assert tampered["State"]["OOMKilled"] != obj["State"]["OOMKilled"]
    assert ex.to_dict()["State"]["OOMKilled"] == obj["State"]["OOMKilled"]
