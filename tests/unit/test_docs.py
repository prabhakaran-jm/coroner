"""Documentation phrase tests for README.md (Task 8.2; Requirements 9.12, 14.2, 14.3, 14.8-14.12, 16.4).

Docker-free and network-free: the tests only read README.md. Each check is a pure function
that takes the README text and returns a list of problems, so the negative controls can feed
mutated text to the very same functions and assert that the problems are found.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README_PATH = REPO_ROOT / "README.md"

# --- Constants to update when the recorded run changes ----------------------------------
HISTORICAL_COUNTS = ("4402 passed", "5 skipped", "53 deselected")
HISTORICAL_LABELS = ("historical verified run", "counts change as tests are added")
# ------------------------------------------------------------------------------------------

PROJECT = "coroner-m07"
M05_MODULES = ("test_compose_isolation.py", "test_volume_persistence.py", "test_collector_readonly_rootfs.py")

LIMITATION_KEYWORDS = (
    "The unobserved interval",
    "Cause of termination is unknown",
    "Silence gaps",
    "Sampling resolution",
    "fsync guarantees are only as strong as the stack beneath",
    "Ack-lost duplication",
    "One process incarnation per run id",
    "Clock skew",
    "Container id discovery",
    "Pre-start data is not covered",
    "Experiment C Sampling_Limitations",
    "Experiment B capture versus receive time",
    "Unreconciled drops",
    "Malformed lines remain in evidence files",
    "`storage_ok` is a point-in-time probe",
)

# (label, regex applied to the normalised, lower-cased README)
REQUIRED_PHRASES: tuple[tuple[str, str], ...] = (
    ("Experiment A termination wording", r"controlled fatal process exit via os\._exit\(1\)"),
    ("HTTP 200 is not proof", r"http 200 alone is not proof"),
    ("storage_ok must be true", r"storage_ok is true"),
    ("CORONER_DURABILITY", r"coroner_durability"),
    ("Process_Durable", r"process_durable"),
    ("Crash_Durable", r"crash_durable"),
    ("M0 default is Crash_Durable", r"m0 default is crash_durable"),
    ("fsync caveat", r"fsync guarantees depend on the docker volume and the underlying host filesystem and storage"),
    ("fixed 250 ms tolerance", r"fixed 250 ms tolerance"),
    ("tolerance constant", r"clock_tolerance_ms = 250"),
    ("Capture_Time", r"capture_time"),
    ("Receive_Time", r"receive_time"),
    ("capture versus receive", r"capture time versus receive time"),
    ("destructive endpoint flag", r"coroner_enable_destructive_endpoints=1"),
    ("local testing only", r"local testing only"),
    ("not exposed beyond localhost", r"must not be exposed beyond localhost"),
    ("Windows setup heading", r"windows and docker desktop setup"),
    ("Docker Desktop", r"docker desktop"),
    ("Linux containers", r"linux containers"),
    ("WSL2", r"wsl2"),
    ("PowerShell", r"powershell"),
    ("memory limit check", r"134217728"),
    ("default pytest command", r"python -m pytest -q"),
    ("Docker-free and network-free", r"docker-free and network-free"),
    ("--run-docker opt-in", r"--run-docker"),
    ("CORONER_RUN_DOCKER opt-in", r"coroner_run_docker=1"),
    ("Docker prerequisites for opt-in tests", r"docker prerequisites"),
    ("named evidence volume", r"named volume"),
    ("never down -v", r"never run docker compose down -v"),
    ("CORONER_COMPOSE_PROJECT", r"coroner_compose_project"),
    ("isolated project", PROJECT),
    ("no repeat-consistency", r"no repeat-consistency"),
    ("native Linux not verified", r"native linux hosts? (?:is|are|was|were) not verified"),
    ("no native Linux verification", r"no native linux-host verification"),
)

PROHIBITION = re.compile(r"\b(?:never|do not|must not|no|prohibit\w*|not)\b", re.I)
WARNING = re.compile(r"\b(?:never|do not|must not)\b|without a file path", re.I)
FILE_PATH = re.compile(r"tests/docker_integration/test_\w+\.py")
DANGEROUS_VOLUME = re.compile(r"down -v|volume rm|volume prune|system prune|--volumes")


# --- helpers ----------------------------------------------------------------------------
def load_readme() -> str:
    return README_PATH.read_bytes().decode("utf-8")


def norm(text: str) -> str:
    """Lower-case, markdown emphasis and backticks removed, whitespace collapsed."""
    return re.sub(r"\s+", " ", text.replace("`", "").replace("*", "")).strip().lower()


def section(text: str, number: int) -> str:
    m = re.search(rf"^## {number}\. .*?(?=^## |\Z)", text, re.M | re.S)
    return m.group(0) if m else ""


def subsection(text: str, heading_prefix: str) -> str:
    m = re.search(rf"^### {re.escape(heading_prefix)}.*?(?=^##+ |\Z)", text, re.M | re.S)
    return m.group(0) if m else ""


def fenced_blocks(text: str) -> list[tuple[int, str, str]]:
    """(line number, line, text before the block) for every line inside a fenced code block."""
    out: list[tuple[int, str, str]] = []
    inside = False
    before = ""
    block_start = 0
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            inside = not inside
            if inside:
                before = "\n".join(lines[max(0, i - 6):i])
                block_start = i
            continue
        if inside:
            out.append((i + 1, line, before))
    del block_start
    return out


def mutate(text: str, old: str, new: str) -> str:
    assert old in text, f"mutation target not found: {old!r}"
    return text.replace(old, new)


def remove_phrase(text: str, phrase: str) -> str:
    pattern = r"[`*\s]*".join(re.escape(c) for c in phrase.replace(" ", ""))
    mutated, n = re.subn(pattern, " REDACTED ", text)
    assert n, f"phrase not found: {phrase!r}"
    return mutated


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", norm(text)) if s]


# --- pure checks (return lists of problems) --------------------------------------------
def check_required_phrases(text: str) -> list[str]:
    n = norm(text)
    return [f"missing: {label}" for label, rx in REQUIRED_PHRASES if not re.search(rx, n)]


def check_expected_states(text: str) -> list[str]:
    problems: list[str] = []
    s8 = section(text, 8)
    for exp, code, oom in (("A", "1", "false"), ("B", "137", "false"), ("C", "137", "true")):
        if not re.search(rf"^\|\s*{exp}\s*\|.*\|\s*{code}\s*\|\s*{oom}\s*\|\s*$", s8, re.M):
            problems.append(f"section 8 table lacks Experiment {exp}: {code} / OOMKilled {oom}")
        rows = [l for l in section(text, 13).split("\n") if l.startswith(f"| Experiment {exp}")]
        if not rows or not re.search(rf"exit ?code {code},? oomkilled {oom}", norm(rows[0])):
            problems.append(f"section 13 row for Experiment {exp} lacks exit code {code} / OOMKilled {oom}")
    return problems


def check_experiment_a_has_no_exception(text: str) -> list[str]:
    parts = [subsection(text, "Experiment A:")]
    parts += [l for l in text.split("\n") if re.match(r"\|\s*(?:A|Experiment A\b[^|]*?)\s*\|", l)]
    if not parts[0]:
        return ["Experiment A subsection not found"]
    return [f"'exception' in Experiment A text: {p[:60]!r}" for p in parts if re.search(r"exception", p, re.I)]


def check_limitations(text: str) -> list[str]:
    s14 = section(text, 14)
    n14 = norm(s14)
    problems: list[str] = []
    positions: list[int] = []
    for kw in LIMITATION_KEYWORDS:
        k = norm(kw)
        count = n14.count(k)
        if count != 1:
            problems.append(f"limitation keyword appears {count} times (want 1): {kw!r}")
        else:
            positions.append(n14.index(k))
    if len(positions) == len(LIMITATION_KEYWORDS) and positions != sorted(positions):
        problems.append("limitation keywords are out of order")
    numbers = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", s14, re.M)]
    if numbers != list(range(1, 16)):
        problems.append(f"limitation numbering is {numbers}, want 1..15")
    return problems


def check_run_docker_scoping(text: str) -> list[str]:
    problems: list[str] = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        mentions = "--run-docker" in line or "CORONER_RUN_DOCKER" in line
        if mentions and "pytest" in line and not FILE_PATH.search(line) and not WARNING.search(line):
            problems.append(f"line {i + 1}: pytest with --run-docker lacks a test file path: {line.strip()!r}")
        elif mentions and not (FILE_PATH.search(line) or WARNING.search(line) or re.search(r"opt-in|opts? in", line, re.I)):
            problems.append(f"line {i + 1}: --run-docker mention is neither per-file, a warning nor a description: {line.strip()!r}")
        if mentions and re.search(r"(?:^|[\s`(])tests/(?:docker_integration/?)?(?=[\s`)]|$)", line) and "pytest" in line and not WARNING.search(line):
            problems.append(f"line {i + 1}: --run-docker over a directory: {line.strip()!r}")
        for mod in M05_MODULES:
            if mod in line:
                window = "\n".join(lines[max(0, i - 8):i + 1])
                headings = [j for j, l in enumerate(window.split("\n")) if l.startswith("#")]
                if headings:
                    window = "\n".join(window.split("\n")[headings[-1]:])
                if not re.search(r"\b(?:never|do not|must not)\b|should not|casually", window, re.I):
                    problems.append(f"line {i + 1}: M0.5 module {mod} outside a warning context")
    return problems


def check_no_volume_deletion(text: str) -> list[str]:
    problems: list[str] = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if DANGEROUS_VOLUME.search(line):
            context = line + " " + (lines[i - 1] if i else "")
            if not PROHIBITION.search(context):
                problems.append(f"line {i + 1}: volume deletion without prohibition: {line.strip()!r}")
    for no, line, _ in fenced_blocks(text):
        if re.match(r"\s*docker (?:volume (?:rm|prune)|system prune)", line):
            problems.append(f"line {no}: command deletes volumes: {line.strip()!r}")
    return problems


def check_no_unsupported_claims(text: str) -> list[str]:
    problems: list[str] = []
    for s in sentences(text):
        negated = re.search(r"\b(?:not|no|never|none|without)\b", s)
        if re.search(r"verified on (?:a |an )?(?:native )?linux|native linux (?:host )?(?:is|are|was|were) verified", s) and not negated:
            problems.append(f"positive native Linux claim: {s[:80]!r}")
        if re.search(r"repeat(?:able|ed) (?:runs )?(?:are )?consistent", s) and not negated:
            problems.append(f"repeat consistency claim: {s[:80]!r}")
    return problems


def check_test_counts(text: str) -> list[str]:
    s12 = norm(section(text, 12))
    return [f"section 12 lacks {c!r}" for c in HISTORICAL_COUNTS + HISTORICAL_LABELS if c not in s12]


def check_compose_project_flag(text: str) -> list[str]:
    problems: list[str] = []
    for no, line, before in fenced_blocks(text):
        m = re.search(r"\bdocker compose\b(.*)", line)
        if not m:
            continue
        tokens = m.group(1).split()
        sub = None
        i = 0
        while i < len(tokens):
            if tokens[i] in ("-p", "-f", "--project-name", "--file"):
                i += 2
            elif tokens[i].startswith("-"):
                i += 1
            else:
                sub = tokens[i]
                break
        if sub not in ("up", "down", "ps", "build"):
            continue
        if f"-p {PROJECT}" in line:
            continue
        if line.strip() == "docker compose build" and "fresh clone" in before.lower():
            continue
        problems.append(f"line {no}: docker compose {sub} without -p {PROJECT}: {line.strip()!r}")
    return problems


# --- tests on the real README ----------------------------------------------------------
@pytest.fixture(scope="module")
def readme() -> str:
    return load_readme()


def test_readme_is_lf_only_without_bom() -> None:
    raw = README_PATH.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw


def test_required_phrases_present(readme: str) -> None:
    assert check_required_phrases(readme) == []


def test_expected_docker_states_per_experiment(readme: str) -> None:
    assert check_expected_states(readme) == []


def test_experiment_a_is_not_described_as_an_exception(readme: str) -> None:
    assert check_experiment_a_has_no_exception(readme) == []


def test_experiment_a_subsection_is_found(readme: str) -> None:
    assert "os._exit(1)" in subsection(readme, "Experiment A:")


def test_readme_has_sixteen_numbered_sections(readme: str) -> None:
    assert len(re.findall(r"^## \d+\. ", readme, re.M)) == 16


@pytest.mark.parametrize("keyword", LIMITATION_KEYWORDS)
def test_each_limitation_appears_exactly_once_in_section_14(readme: str, keyword: str) -> None:
    assert norm(section(readme, 14)).count(norm(keyword)) == 1


def test_limitations_are_numbered_and_ordered(readme: str) -> None:
    assert check_limitations(readme) == []


def test_run_docker_is_always_per_file_or_a_warning(readme: str) -> None:
    assert check_run_docker_scoping(readme) == []


def test_readme_never_instructs_deleting_volumes(readme: str) -> None:
    assert check_no_volume_deletion(readme) == []


def test_readme_makes_no_native_linux_or_repeat_claims(readme: str) -> None:
    assert check_no_unsupported_claims(readme) == []


def test_test_counts_are_labelled_historical(readme: str) -> None:
    assert check_test_counts(readme) == []


def test_fenced_docker_compose_commands_carry_the_project_flag(readme: str) -> None:
    assert check_compose_project_flag(readme) == []


def test_fenced_block_parser_sees_the_real_compose_commands(readme: str) -> None:
    lines = [l for _, l, _ in fenced_blocks(readme) if "docker compose" in l]
    assert any(" up " in l for l in lines) and any(l.rstrip().endswith(" down") for l in lines)


# --- negative controls: the same checks must detect mutated text ---------------------
def test_control_missing_os_exit_phrase_is_detected(readme: str) -> None:
    assert check_required_phrases(mutate(readme, "os._exit(1)", "os.exit(0)"))


def test_control_exception_in_experiment_a_is_detected(readme: str) -> None:
    bad = mutate(readme, "It is not an application error", "It is an exception")
    assert check_experiment_a_has_no_exception(bad)


def test_control_exception_in_experiment_a_table_row_is_detected(readme: str) -> None:
    bad = mutate(readme, "| A | controlled fatal", "| A | exception, controlled fatal")
    assert check_experiment_a_has_no_exception(bad)


def test_control_exception_outside_experiment_a_is_allowed(readme: str) -> None:
    ok = mutate(readme, "### Experiment B: SIGKILL", "### Experiment B: SIGKILL (no exception here)")
    assert check_experiment_a_has_no_exception(ok) == []


@pytest.mark.parametrize("keyword", [LIMITATION_KEYWORDS[i] for i in (0, 4, 10, 14)])
def test_control_missing_limitation_keyword_is_detected(readme: str, keyword: str) -> None:
    assert check_limitations(remove_phrase(readme, keyword))


def test_control_unscoped_run_docker_default_is_detected(readme: str) -> None:
    bad = readme + "\nSafe default: `python -m pytest --run-docker -q`\n"
    assert check_run_docker_scoping(bad)


def test_control_m05_module_outside_warning_is_detected(readme: str) -> None:
    bad = readme + "\nRun `python -m pytest tests/docker_integration/test_compose_isolation.py --run-docker -q`\n"
    assert check_run_docker_scoping(bad)


def test_control_down_v_without_prohibition_is_detected(readme: str) -> None:
    assert check_no_volume_deletion(readme + "\nReset with `docker compose down -v` when needed.\n")


def test_control_volume_prune_command_is_detected(readme: str) -> None:
    assert check_no_volume_deletion(readme + "\n```powershell\ndocker volume prune -f\n```\n")


def test_control_native_linux_claim_is_detected(readme: str) -> None:
    assert check_no_unsupported_claims(readme + "\nResults were verified on a native Linux host.\n")


def test_control_repeat_consistency_claim_is_detected(readme: str) -> None:
    assert check_no_unsupported_claims(readme + "\nRepeated runs are consistent.\n")


def test_control_dropping_storage_ok_is_detected(readme: str) -> None:
    assert check_required_phrases(readme.replace("storage_ok", "health_flag"))


def test_control_changed_tolerance_is_detected(readme: str) -> None:
    assert check_required_phrases(readme.replace("250 ms", "500 ms"))


def test_control_missing_crash_durable_is_detected(readme: str) -> None:
    assert check_required_phrases(readme.replace("Crash_Durable", "Fast_Durable"))


def test_control_wrong_expected_state_is_detected(readme: str) -> None:
    assert check_expected_states(mutate(readme, "| C | genuine Docker out-of-memory kill | 137 | true |", "| C | genuine Docker out-of-memory kill | 137 | false |"))


def test_control_changed_test_counts_are_detected(readme: str) -> None:
    assert check_test_counts(readme.replace("4402 passed", "1 passed"))


def test_control_bare_compose_down_in_fenced_block_is_detected(readme: str) -> None:
    bad = readme + "\n```powershell\ndocker compose down\n```\n"
    assert check_compose_project_flag(bad)


def test_control_fresh_clone_build_line_is_allowed() -> None:
    ok = "On a fresh clone only:\n\n```powershell\ndocker compose build\n```\n"
    assert check_compose_project_flag(ok) == []
    assert check_compose_project_flag(ok.replace("fresh clone", "machine")) != []
