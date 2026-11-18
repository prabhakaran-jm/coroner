"""Task 4.1: Probe config and real readers (Requirements 2.3, 2.6, 2.8, 2.11, 7.1, 7.3, 7.6)."""
from __future__ import annotations

import dataclasses
import os
import re

import pytest

from coroner.evidence.schema import RUN_ID_RE
from coroner.probe import probe as probe_mod
from coroner.probe.probe import ProbeConfig, load_config, read_container_id, read_rss_bytes

H64 = "3f1c0e0a9d7b4c1e8a6f5d2b9c4e7a1033b8d5f6e2a4c7b9d1e3f5a7c9b1d3e5"
O64 = "a" * 63 + "b"
C64 = "c" * 64


def cfg(env):
    warnings: list[str] = []
    return load_config(env, warnings.append), warnings


# ---- config ----

def test_defaults_and_no_warning():
    c, w = cfg({"CORONER_RUN_ID": "r1"})
    assert (c.interval_ms, c.collector_url, c.send_timeout_s, c.start_deadline_s) == (
        500, "http://collector:8081", 0.200, 0.100)
    assert w == []


@pytest.mark.parametrize("raw,expected", [("100", 100), ("500", 500), ("60000", 60000)])
def test_valid_interval(raw, expected):
    c, w = cfg({"CORONER_SAMPLE_INTERVAL_MS": raw})
    assert c.interval_ms == expected and w == []


@pytest.mark.parametrize(
    "raw", ["99", "60001", "0", "-100", "", " ", "abc", "1.5", "+100", " 100", "100 ", "1e3", "0x64", "٣٠٠", "9" * 40]
)
def test_invalid_interval_gives_500_and_one_warning(raw):
    c, w = cfg({"CORONER_SAMPLE_INTERVAL_MS": raw})
    assert c.interval_ms == 500
    assert len(w) == 1 and "CORONER_SAMPLE_INTERVAL_MS" in w[0] and repr(raw) in w[0]


def test_run_id_generated_valid_and_random():
    ids = {cfg(env)[0].run_id for env in ({}, {"CORONER_RUN_ID": ""}, {})}
    assert len(ids) == 3
    for rid in ids:
        assert re.fullmatch(r"manual-[0-9a-f]{8}", rid) and RUN_ID_RE.fullmatch(rid)


def test_explicit_run_id_and_url_preserved():
    c, _ = cfg({"CORONER_RUN_ID": "a-20250101T120000Z-9f3a1c2e", "CORONER_COLLECTOR_URL": "http://h:1/x"})
    assert c.run_id == "a-20250101T120000Z-9f3a1c2e" and c.collector_url == "http://h:1/x"


@pytest.mark.parametrize("bad", ["bad id", "-x", "a" * 65, "x\n", "../a"])
def test_explicit_invalid_run_id_raises_naming_value(bad):
    with pytest.raises(ValueError, match="CORONER_RUN_ID"):
        cfg({"CORONER_RUN_ID": bad})


def test_run_id_pattern_equals_schema_pattern():
    assert probe_mod._RUN_ID_RE.pattern == RUN_ID_RE.pattern


def test_frozen_env_untouched_and_os_environ_not_read(monkeypatch):
    class Boom(dict):
        def get(self, *a, **k):
            raise AssertionError("os.environ read")
        __getitem__ = get

    monkeypatch.setattr(os, "environ", Boom())
    env = {"CORONER_SAMPLE_INTERVAL_MS": "bad"}
    snapshot = dict(env)
    c, _ = cfg(env)
    assert env == snapshot
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.interval_ms = 1  # type: ignore[misc]
    assert isinstance(c, ProbeConfig)


# ---- RSS ----

def test_rss_is_resident_pages_times_page_size(tmp_path):
    f = tmp_path / "statm"
    f.write_text("12345 678 90 1 0 2 0\n")
    assert read_rss_bytes(f, lambda: 4096) == 678 * 4096


def test_rss_does_not_use_getrusage(tmp_path, monkeypatch):
    resource = pytest.importorskip("resource")
    monkeypatch.setattr(resource, "getrusage", lambda *_: (_ for _ in ()).throw(AssertionError("peak")))
    f = tmp_path / "statm"
    f.write_text("1 2 3\n")
    assert read_rss_bytes(f, lambda: 10) == 20


@pytest.mark.parametrize("content", ["", "5", "5 abc 1", "5 -3 1", "5 1.5 1"])
def test_rss_malformed_raises(tmp_path, content):
    f = tmp_path / "statm"
    f.write_text(content)
    with pytest.raises(ValueError):
        read_rss_bytes(f, lambda: 4096)


def test_rss_missing_file_raises(tmp_path):
    with pytest.raises(OSError):
        read_rss_bytes(tmp_path / "nope", lambda: 4096)


def test_default_page_size_is_real():
    assert probe_mod._page_size() in (4096, 8192, 16384, 65536)


# ---- container id ----

def files(tmp_path, mountinfo=None, cgroup=None):
    m, g = tmp_path / "mountinfo", tmp_path / "cgroup"
    if mountinfo is not None:
        m.write_text(mountinfo)
    if cgroup is not None:
        g.write_text(cgroup)
    return m, g


def mi(cid):
    return f"501 480 8:1 /var/lib/docker/containers/{cid}/hostname /etc/hostname rw - ext4 /dev/sda1 rw\n"


def test_mountinfo_has_priority(tmp_path):
    m, g = files(tmp_path, mi(H64), f"12:memory:/docker/{O64}\n")
    assert read_container_id(m, g, {"HOSTNAME": "abcdef012345"}) == (H64, "mountinfo")


def test_cgroup_fallback_variants(tmp_path):
    m, g = files(tmp_path, "36 35 98:0 / /mnt rw - ext4 /dev/x rw\n", f"12:memory:/docker/{H64}\n")
    assert read_container_id(m, g, {}) == (H64, "cgroup")
    g.write_text(f"0::/system.slice/docker-{O64}.scope\n")
    assert read_container_id(m, g, {}) == (O64, "cgroup")


def test_hostname_fallback_and_none(tmp_path):
    m, g = files(tmp_path, "", "0::/\n")
    assert read_container_id(m, g, {"HOSTNAME": "abcdef012345"}) == ("abcdef012345", "hostname")
    assert read_container_id(m, g, {}) == (None, "none")


@pytest.mark.parametrize("host", ["myhost", "abcdef01234g", "ABCDEF012345", "abcdef01234", "abcdef0123456", H64, ""])
def test_malformed_hostname_is_none(tmp_path, host):
    m, g = files(tmp_path, "", "0::/\n")
    assert read_container_id(m, g, {"HOSTNAME": host}) == (None, "none")


@pytest.mark.parametrize(
    "cid", [H64.upper(), H64[:63], H64 + "a", "g" * 64]
)
def test_invalid_hex_rejected(tmp_path, cid):
    m, g = files(tmp_path, mi(cid), f"1:x:/docker/{cid}\n")
    assert read_container_id(m, g, {}) == (None, "none")


def test_unrelated_64_hex_ignored(tmp_path):
    m, g = files(tmp_path, f"1 2 3:4 /data/{C64}/x /mnt rw - ext4 {C64} rw\n", f"1:x:/other/{C64}\n")
    assert read_container_id(m, g, {}) == (None, "none")


def test_unreadable_sources_fall_through(tmp_path):
    # mountinfo missing, cgroup is a directory (read raises OSError on every platform)
    d = tmp_path / "cgroup"
    d.mkdir()
    assert read_container_id(tmp_path / "missing", d, {"HOSTNAME": "abcdef012345"}) == ("abcdef012345", "hostname")
    assert read_container_id(tmp_path / "missing", d, {}) == (None, "none")
