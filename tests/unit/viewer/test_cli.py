"""CLI tests for the Viewer (task 1.11).

Nothing here opens a network endpoint. The server class is replaced with a spy that records the
`uvicorn.Config` it receives and whose `run()` does nothing, so launch configuration is checked
by reading the constructed configuration. Port 80 is only ever checked as configuration.
Fixtures are synthetic directories under pytest's `tmp_path`; the real `runs/` is never used.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.8, 1.9, 1.11, 3.9, 11.10, 11.23, 11.30, 11.45, 13.17.
"""
from __future__ import annotations

import importlib
import importlib.util
import ntpath
import os
import sys
from pathlib import Path
from typing import Any

import pytest

import coroner.viewer.cli as cli
from coroner.viewer.app import make_runs_dir_label
from coroner.viewer.cli import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_RUNS_DIR,
    ViewerArgs,
    ViewerConfigError,
    build_server_config,
    main,
    parse_args,
    validate_host,
    validate_port,
)

ON_WINDOWS = os.name == "nt"


class SpyServer:
    """Stands in for `uvicorn.Server`: records the configuration, runs nothing."""

    instances: list[SpyServer] = []
    run_error: Exception | None = None

    def __init__(self, config: Any) -> None:
        self.config = config
        self.run_calls = 0
        SpyServer.instances.append(self)

    def run(self) -> None:
        self.run_calls += 1
        if SpyServer.run_error is not None:
            raise SpyServer.run_error


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> type[SpyServer]:
    SpyServer.instances = []
    SpyServer.run_error = None
    monkeypatch.setattr(cli.uvicorn, "Server", SpyServer)
    return SpyServer


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    path = tmp_path / "runs"
    path.mkdir()
    return path


# ----------------------------------------------------------------------------- host


@pytest.mark.parametrize(
    "host",
    ["localhost", "::1", "0.0.0.0", "127.0.0.2", "", "127.0.0.1 ", " 127.0.0.1", "127.0.0.1\n", "LOCALHOST"],
)
def test_validate_host_rejects_everything_but_the_exact_string(host: str) -> None:
    with pytest.raises(ViewerConfigError) as info:
        validate_host(host)
    assert "127.0.0.1" in str(info.value)


def test_validate_host_accepts_only_loopback_literal() -> None:
    assert validate_host("127.0.0.1") == "127.0.0.1"


@pytest.mark.parametrize("host", ["localhost", "::1", "0.0.0.0", "127.0.0.2", "", "127.0.0.1 "])
def test_main_rejects_bad_host_with_exit_2_and_no_server(
    host: str, spy: type[SpyServer], runs_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--runs-dir", str(runs_dir), "--host", host])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("error: ")
    assert "127.0.0.1" in captured.err
    assert captured.out == ""
    assert spy.instances == []


# ----------------------------------------------------------------------------- port


@pytest.mark.parametrize("text, expected", [("1", 1), ("80", 80), ("8765", 8765), ("65535", 65535)])
def test_validate_port_accepts_digits_in_range(text: str, expected: int) -> None:
    assert validate_port(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "0",
        "65536",
        "+80",
        "-80",
        "0x50",
        "8765\n",
        " 80",
        "80 ",
        "8 0",
        "８０８０",
        "٨٠",
        "",
        "abc",
        "80.0",
        "1e2",
        "99999999999999999999",
    ],
)
def test_validate_port_rejects_and_names_the_value(text: str) -> None:
    with pytest.raises(ViewerConfigError) as info:
        validate_port(text)
    assert repr(text[:64]) in str(info.value)
    assert "1 to 65535" in str(info.value)


def test_validate_port_leading_zero_is_decimal_not_octal() -> None:
    # '08' is digits only and in range, so it is a plain decimal 8 (never octal, never an error).
    assert validate_port("08") == 8
    assert validate_port("0080") == 80


def test_validate_port_echo_is_cut_to_64_characters() -> None:
    with pytest.raises(ViewerConfigError) as info:
        validate_port("x" * 500)
    assert repr("x" * 64) in str(info.value)
    assert "x" * 65 not in str(info.value)


@pytest.mark.parametrize("port", ["0", "65536", "+80", "0x50", "8765\n", "８０８０", ""])
def test_main_rejects_bad_port_with_exit_2_and_no_server(
    port: str, spy: type[SpyServer], runs_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--runs-dir", str(runs_dir), "--port", port])
    captured = capsys.readouterr()
    assert code == 2
    assert repr(port) in captured.err
    assert captured.out == ""
    assert spy.instances == []


# ----------------------------------------------------------------------------- parse_args


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args == ViewerArgs(runs_dir="runs", host="127.0.0.1", port=8765)
    assert (DEFAULT_RUNS_DIR, DEFAULT_HOST, DEFAULT_PORT) == ("runs", "127.0.0.1", 8765)


def test_parse_args_overrides() -> None:
    args = parse_args(["--runs-dir", "some/where", "--host", "127.0.0.1", "--port", "80"])
    assert args == ViewerArgs(runs_dir="some/where", host="127.0.0.1", port=80)


def test_parse_args_unknown_option_exits_2() -> None:
    with pytest.raises(SystemExit) as info:
        parse_args(["--bogus"])
    assert info.value.code == 2


def test_viewer_args_is_frozen() -> None:
    args = parse_args([])
    with pytest.raises(AttributeError):
        args.port = 1  # type: ignore[misc]


def test_viewer_config_error_is_an_exception_carrying_its_message() -> None:
    err = ViewerConfigError("text")
    assert isinstance(err, Exception)
    assert str(err) == "text"


# ----------------------------------------------------------------------------- runs directory


def test_missing_runs_dir_exits_2_and_names_the_path(
    spy: type[SpyServer], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-such-runs"
    code = main(["--runs-dir", str(missing)])
    captured = capsys.readouterr()
    assert code == 2
    assert str(missing) in captured.err
    assert captured.out == ""
    assert spy.instances == []


def test_runs_dir_that_is_a_file_exits_2_and_names_the_path(
    spy: type[SpyServer], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_a_dir = tmp_path / "plain.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    code = main(["--runs-dir", str(not_a_dir)])
    captured = capsys.readouterr()
    assert code == 2
    assert str(not_a_dir) in captured.err
    assert spy.instances == []


def test_usage_error_exits_2_without_building_a_server(spy: type[SpyServer]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--no-such-option"])
    assert info.value.code == 2
    assert spy.instances == []


# ----------------------------------------------------------------------------- configuration


@pytest.mark.parametrize("port", [8765, 80, 1, 65535, 8080])
def test_build_server_config_options(port: int) -> None:
    config = build_server_config(object(), ViewerArgs(runs_dir="runs", host="127.0.0.1", port=port))
    assert config.host == "127.0.0.1"
    assert config.port == port
    assert config.workers == 1
    assert config.reload is False
    assert config.log_level == "warning" or config.log_level == 30
    assert config.access_log is False
    assert config.lifespan == "off"
    assert config.server_header is False
    assert config.proxy_headers is False
    assert config.ws == "none"
    assert config.h11_max_incomplete_event_size == 16384
    assert config.timeout_keep_alive == 5
    assert config.limit_concurrency == 64
    assert config.host not in ("localhost", "0.0.0.0", "::1")


# ----------------------------------------------------------------------------- bind configuration via spy


@pytest.mark.parametrize("port", [8765, 80])
def test_main_passes_literal_loopback_configuration_to_the_server(
    port: int, spy: type[SpyServer], runs_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--runs-dir", str(runs_dir), "--host", "127.0.0.1", "--port", str(port)])
    assert code == 0
    assert len(spy.instances) == 1
    server = spy.instances[0]
    assert server.run_calls == 1
    config = server.config
    assert config.host == "127.0.0.1"
    assert config.host not in ("localhost", "0.0.0.0", "::1")
    assert config.port == port
    assert config.workers == 1
    assert config.reload is False
    assert config.log_level == "warning" or config.log_level == 30
    assert config.access_log is False
    assert config.lifespan == "off"
    assert config.server_header is False
    assert config.proxy_headers is False
    assert config.ws == "none"
    assert config.h11_max_incomplete_event_size == 16384
    assert config.timeout_keep_alive == 5
    assert config.limit_concurrency == 64


def test_main_with_default_host_uses_literal_loopback(
    spy: type[SpyServer], runs_dir: Path
) -> None:
    assert main(["--runs-dir", str(runs_dir)]) == 0
    assert spy.instances[0].config.host == "127.0.0.1"
    assert spy.instances[0].config.port == 8765


def test_rejected_host_localhost_creates_no_server_and_never_runs(
    spy: type[SpyServer], runs_dir: Path
) -> None:
    assert main(["--runs-dir", str(runs_dir), "--host", "localhost"]) == 2
    assert spy.instances == []
    assert sum(s.run_calls for s in spy.instances) == 0


def test_start_line_text(
    spy: type[SpyServer], runs_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--runs-dir", str(runs_dir), "--port", "9001"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Coroner Viewer: http://127.0.0.1:9001/  (runs: runs, read-only)\n"
    assert captured.err == ""


# ----------------------------------------------------------------------------- exit code 1


def test_unexpected_startup_failure_exits_1_with_fixed_message(
    spy: type[SpyServer],
    runs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(f"secret detail {runs_dir}")

    monkeypatch.setattr("coroner.viewer.app.create_app", boom)
    code = main(["--runs-dir", str(runs_dir)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == "error: the Viewer could not start\n"
    assert captured.out == ""
    assert "secret detail" not in captured.err
    assert spy.instances == []


def test_server_construction_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, runs_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_config: Any) -> Any:
        raise OSError("nope")

    monkeypatch.setattr(cli.uvicorn, "Server", boom)
    assert main(["--runs-dir", str(runs_dir)]) == 1
    captured = capsys.readouterr()
    assert captured.err == "error: the Viewer could not start\n"
    assert captured.out == ""


def test_failure_while_running_exits_1_with_fixed_message(
    spy: type[SpyServer], runs_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spy.run_error = OSError("address in use")
    code = main(["--runs-dir", str(runs_dir)])
    captured = capsys.readouterr()
    assert code == 1
    assert spy.instances[0].run_calls == 1
    assert captured.err == "error: the Viewer stopped unexpectedly\n"
    assert "address in use" not in captured.err


# ----------------------------------------------------------------------------- label


def test_relative_label_is_as_supplied() -> None:
    assert make_runs_dir_label("runs") == "runs"
    assert make_runs_dir_label(os.path.join("data", "my-runs")) == os.path.join("data", "my-runs")


def test_absolute_label_is_final_component_only(tmp_path: Path) -> None:
    target = tmp_path / "outer" / "inner-runs"
    assert make_runs_dir_label(str(target)) == "inner-runs"
    assert make_runs_dir_label(str(target) + os.sep) == "inner-runs"
    assert str(tmp_path) not in make_runs_dir_label(str(target))


@pytest.mark.skipif(not ON_WINDOWS, reason="drive and UNC forms are Windows path syntax")
@pytest.mark.parametrize(
    "supplied, expected",
    [
        ("C:\\Users\\someone\\runs", "runs"),
        ("C:\\Users\\someone\\runs\\", "runs"),
        ("D:\\data\\archive-1", "archive-1"),
        ("\\\\server\\share\\team\\runs", "runs"),
        ("\\\\server\\share\\team\\runs\\", "runs"),
    ],
)
def test_absolute_label_drive_and_unc_forms(supplied: str, expected: str) -> None:
    assert ntpath.isabs(supplied)
    assert make_runs_dir_label(supplied) == expected


@pytest.mark.skipif(not ON_WINDOWS, reason="drive and UNC roots are Windows path syntax")
@pytest.mark.parametrize("supplied", ["C:\\", "\\\\server\\share\\", "\\\\server\\share"])
def test_root_label_is_the_placeholder(supplied: str) -> None:
    assert make_runs_dir_label(supplied) == "(runs directory)"


def test_start_line_label_for_absolute_runs_dir_is_final_component_only(
    spy: type[SpyServer], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "evidence-runs"
    target.mkdir()
    assert main(["--runs-dir", str(target)]) == 0
    out = capsys.readouterr().out
    assert "(runs: evidence-runs, read-only)" in out
    assert str(tmp_path) not in out


def test_start_line_label_for_relative_runs_dir_is_as_supplied(
    spy: type[SpyServer], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "rel-runs").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["--runs-dir", "rel-runs"]) == 0
    assert "(runs: rel-runs, read-only)" in capsys.readouterr().out


# ----------------------------------------------------------------------------- import safety


def test_importing_cli_parses_no_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prog", "--definitely-not-an-option", "--port", "zzz"])
    spec = importlib.util.spec_from_file_location("coroner_viewer_cli_fresh_copy", cli.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)  # would raise SystemExit if argparse ran at import time
    assert callable(module.main)


def test_module_entry_point_imports_without_running_main(
    spy: type[SpyServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["prog", "--definitely-not-an-option"])
    sys.modules.pop("coroner.viewer.__main__", None)
    entry = importlib.import_module("coroner.viewer.__main__")
    assert entry.main is main
    assert spy.instances == []
