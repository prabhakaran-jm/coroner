"""Static checks of the Viewer UI files in `coroner/viewer/static/` (task 3.6).

THIS TEST DOES NOT EXECUTE JAVASCRIPT. No JavaScript engine, no browser and no Node process is
started, so nothing here proves that the UI code runs or renders. The checks read the static files as
text and bytes and check what can be known without running them: the allowlist and the served content
types, the module imports, the absence of forbidden sinks and of external URLs, the CSP compatibility,
and the consistency between the strings and fields the modules use and the files and the API that
provide them. The rendering itself is reviewed by hand in the browser (task 3.8).

Fixtures are synthetic runs under pytest's temporary directory, built with `RunsBuilder`. The real
`runs/` directory is never read and no socket is opened (requests go through `TestClient` in process).

Requirements: 2.10, 4.8, 4.10, 11.9, 11.41, 11.43, 13.6, 13.7, 13.14.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coroner.viewer import app as viewer_app
from coroner.viewer import model as viewer_model
from coroner.viewer.app import create_app
from tests.unit.viewer.builder import RunsBuilder, sample_specs

EXECUTES_JAVASCRIPT = False

PORT = 8765
HOST = {"Host": f"127.0.0.1:{PORT}"}
STATIC_DIR = Path(__file__).resolve().parents[3] / "coroner" / "viewer" / "static"
ALLOWLIST = r"^[a-z0-9][a-z0-9._-]*\.(html|js|css)$"
ALLOWLIST_RE = re.compile(ALLOWLIST)
CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
}
SVG_NS = "http://www.w3.org/2000/svg"
INCIDENT_IMPORTS = {
    "/static/copy.js", "/static/api.js", "/static/dom.js", "/static/chart.js", "/static/evidence-panel.js",
    "/static/oom-state.js",
}


# ---------------------------------------------------------------- file helpers


def static_files() -> list[Path]:
    return sorted(STATIC_DIR.iterdir(), key=lambda p: p.name)


def static_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def js_files() -> list[Path]:
    return [p for p in static_files() if p.suffix == ".js"]


def file_ids(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


# ---------------------------------------------------------------- URL scan with the SVG whitelist

_URL_RE = re.compile(r"""\bhttps?://[^\s"'`<>()\\]+""", re.IGNORECASE)
_PROTOCOL_RELATIVE_RE = re.compile(r"(?<![:\w/*])//(?=[A-Za-z0-9\[])")
_NAMESPACE_CONTEXT_RE = re.compile(r"""(?:createElementNS\(\s*|\bSVG_NS\s*=\s*)["']$""")
_CSS_URL_RE = re.compile(r"url\(\s*([^)]*)\)", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\(\s*)?["']?([^"')\s;]+)""", re.IGNORECASE)


def _relative_reference(value: str) -> bool:
    value = value.strip().strip("\"'")
    return (value.startswith("/") and not value.startswith("//")) or value.startswith("#")


def scan_urls(text: str, name: str = "x.js") -> list[str]:
    """Findings for every http://, https:// and protocol-relative URL, except the exact SVG namespace.

    The namespace is accepted only as an exact whole-URL match in a `createElementNS(` argument or in the
    `SVG_NS = "..."` constant. Anything else (a longer URL, another scheme, another path) is a finding.
    """
    findings: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0)
        if url == SVG_NS and _NAMESPACE_CONTEXT_RE.search(text[max(0, match.start() - 80):match.start()]):
            continue
        findings.append(f"URL {url}")
    for match in _PROTOCOL_RELATIVE_RE.finditer(text):
        findings.append(f"protocol-relative URL at offset {match.start()}")
    if name.endswith(".css"):
        for match in _CSS_URL_RE.finditer(text):
            if not _relative_reference(match.group(1)):
                findings.append(f"CSS url({match.group(1)})")
        for match in _CSS_IMPORT_RE.finditer(text):
            if not _relative_reference(match.group(1)):
                findings.append(f"CSS @import {match.group(1)}")
    return findings


# ---------------------------------------------------------------- forbidden sinks

SINKS = {
    "innerHTML": r"\binnerHTML\b",
    "outerHTML": r"\bouterHTML\b",
    "insertAdjacentHTML": r"\binsertAdjacentHTML\b",
    "document.write": r"\bdocument\s*\.\s*write(?:ln)?\b",
    "eval(": r"\beval\s*\(",
    "new Function": r"\bnew\s+Function\b",
    "Function(": r"(?<![\w.])Function\s*\(",
    "setAttribute style": r"""setAttribute\(\s*["']style["']""",
    "javascript:": r"javascript\s*:",
    "Date.parse": r"\bDate\s*\.\s*parse\b",
    "new Date(": r"\bnew\s+Date\s*\(",
    "Intl": r"\bIntl\b",
    "string timer": r"""\bset(?:Timeout|Interval)\s*\(\s*["'`]""",
    "DOMParser": r"\bDOMParser\b",
    "createContextualFragment": r"\bcreateContextualFragment\b",
    "srcdoc": r"\bsrcdoc\b",
    "XMLHttpRequest": r"\bXMLHttpRequest\b",
    "WebSocket": r"\bWebSocket\b",
    "EventSource": r"\bEventSource\b",
    "sendBeacon": r"\bsendBeacon\b",
    "importScripts": r"\bimportScripts\b",
}
_SINK_RES = {label: re.compile(pattern) for label, pattern in SINKS.items()}
_FETCH_RE = re.compile(r"""\bfetch\s*\(\s*(?P<arg>[^,)]*)""")
_API_CALL_RE = re.compile(r"""\bfetch(?:Json|Run)\s*\(\s*(?P<arg>["'`][^,)]*)""")
_HTML_HANDLER_IN_STRING_RE = re.compile(r"<[A-Za-z][^>]*\bon[a-z]+\s*=", re.IGNORECASE)


def scan_sinks(text: str) -> list[str]:
    return [label for label, rx in _SINK_RES.items() if rx.search(text)]


def _non_relative_literal(arg: str) -> bool:
    arg = arg.strip()
    if not arg or arg[0] not in "\"'`":
        return False  # a variable or expression: no literal URL to judge
    return not (arg[1:2] == "/" and arg[2:3] != "/")


def scan_fetch(text: str) -> list[str]:
    findings = []
    for rx in (_FETCH_RE, _API_CALL_RE):
        for match in rx.finditer(text):
            if _non_relative_literal(match.group("arg")):
                findings.append(f"fetch to non-relative URL: {match.group('arg').strip()}")
    return findings


# ---------------------------------------------------------------- module imports

_STATIC_IMPORT_RE = re.compile(r"""(?:^|\n)[ \t]*(?:import|export)\b[^;"'\n]*?\bfrom\s*["']([^"']+)["']""")
_BARE_IMPORT_RE = re.compile(r"""(?:^|\n)[ \t]*import\s*["']([^"']+)["']""")
_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(\s*(["'])([^"']+)\1\s*\)""")
_DYNAMIC_NON_LITERAL_RE = re.compile(r"""\bimport\s*\(\s*[^"'\s)]""")


def import_specifiers(text: str) -> list[str]:
    found = _STATIC_IMPORT_RE.findall(text) + _BARE_IMPORT_RE.findall(text)
    found += [m.group(2) for m in _DYNAMIC_IMPORT_RE.finditer(text)]
    return found


def unresolved_imports(text: str, existing: set[str]) -> list[str]:
    problems = [f"dynamic import with a non-literal argument at offset {m.start()}" for m in _DYNAMIC_NON_LITERAL_RE.finditer(text)]
    for spec in import_specifiers(text):
        if not spec.startswith("/static/"):
            problems.append(f"{spec}: not an absolute /static/ path")
            continue
        name = spec[len("/static/"):]
        if "/" in name or ALLOWLIST_RE.fullmatch(name) is None or name not in existing:
            problems.append(f"{spec}: no such static file")
    return problems


# ---------------------------------------------------------------- copy.js parser

_COPY_TOKEN_RE = re.compile(r"""\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<id>[A-Za-z_][A-Za-z0-9_]*)|(?P<p>[{}:,+]))""")


def parse_copy(source: str) -> dict[str, Any]:
    """The COPY object literal of copy.js as nested dicts (string leaves). Fails on anything unexpected."""
    source = re.sub(r"(?m)^\s*//.*$", "", source)
    start = source.index("export const COPY = deepFreeze({") + len("export const COPY = deepFreeze(")
    tokens: list[tuple[str, str]] = []
    pos = start
    depth = 0
    while True:
        match = _COPY_TOKEN_RE.match(source, pos)
        assert match is not None, f"copy.js: unexpected text at {source[pos:pos + 40]!r}"
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
        pos = match.end()
        if kind == "p" and match.group(kind) == "{":
            depth += 1
        elif kind == "p" and match.group(kind) == "}":
            depth -= 1
            if depth == 0:
                break
    index = 0

    def value() -> Any:
        nonlocal index
        kind, text = tokens[index]
        if (kind, text) == ("p", "{"):
            index += 1
            obj: dict[str, Any] = {}
            while tokens[index] != ("p", "}"):
                key_kind, key_text = tokens[index]
                key = json.loads(key_text) if key_kind == "str" else key_text
                assert tokens[index + 1] == ("p", ":"), f"copy.js: expected ':' after {key}"
                index += 2
                assert key not in obj, f"copy.js: duplicate key {key}"
                obj[key] = value()
                if tokens[index] == ("p", ","):
                    index += 1
            index += 1
            return obj
        assert kind == "str", f"copy.js: unexpected token {text!r}"
        result = json.loads(text)
        index += 1
        while tokens[index] == ("p", "+"):
            assert tokens[index + 1][0] == "str"
            result += json.loads(tokens[index + 1][1])
            index += 2
        return result

    return value()


def copy_leaves(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    return [leaf for child in node.values() for leaf in copy_leaves(child)]


COPY = parse_copy(static_text(STATIC_DIR / "copy.js"))


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    runs = tmp_path_factory.mktemp("static_ui_runs") / "runs"
    builder = RunsBuilder(runs)
    builder.add_run("A", run_id="a-20261120T211300Z-00000001", samples=sample_specs(3))
    builder.add_run("B", run_id="b-20261120T211300Z-00000002", samples=sample_specs(3))
    builder.add_run("C", run_id="c-20261120T211300Z-00000003", samples=sample_specs(3))
    return TestClient(create_app(runs, PORT), base_url=f"http://127.0.0.1:{PORT}")


# ================================================================ report: no JavaScript is executed


def test_suite_reports_that_it_does_not_execute_javascript(record_property: Any) -> None:
    record_property("executes_javascript", "no")
    record_property("scope", "text and byte checks of coroner/viewer/static/ only; browser review is task 3.8")
    assert EXECUTES_JAVASCRIPT is False
    assert "DOES NOT EXECUTE JAVASCRIPT" in (__doc__ or "")


# ================================================================ allowlist and serving


def test_static_directory_has_the_expected_files() -> None:
    names = {p.name for p in static_files()}
    assert {
        "index.html", "style.css", "app.js", "api.js", "dom.js", "copy.js", "overview.js", "chart.js",
        "evidence-panel.js", "incident.js", "compare.js", "oom-state.js",
    } <= names


def test_test_allowlist_equals_the_application_allowlist() -> None:
    assert ALLOWLIST == viewer_app._ASSET_NAME_RE.pattern
    assert CONTENT_TYPES == viewer_app._CONTENT_TYPES


@pytest.mark.parametrize("path", static_files(), ids=file_ids(static_files()))
def test_every_static_entry_is_a_regular_file_matching_the_allowlist(path: Path) -> None:
    assert path.is_file() and not path.is_symlink(), path.name
    assert ALLOWLIST_RE.fullmatch(path.name) is not None, path.name


@pytest.mark.parametrize("path", static_files(), ids=file_ids(static_files()))
def test_every_static_file_is_served_with_its_content_type(client: TestClient, path: Path) -> None:
    response = client.get(f"/static/{path.name}", headers=HOST)
    assert response.status_code == 200, path.name
    assert response.headers["content-type"].lower() == CONTENT_TYPES[path.suffix[1:]]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content == path.read_bytes()


def test_the_served_set_equals_the_directory_listing(client: TestClient) -> None:
    served = set(client.app.state.static_assets)  # type: ignore[attr-defined]
    assert served == {p.name for p in static_files()}


@pytest.mark.parametrize("path", static_files(), ids=file_ids(static_files()))
def test_static_files_are_utf8_without_a_byte_order_mark(path: Path) -> None:
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), path.name
    data.decode("utf-8")


# ================================================================ module imports


def test_every_import_resolves_to_an_existing_static_file() -> None:
    existing = {p.name for p in static_files()}
    for path in js_files():
        assert unresolved_imports(static_text(path), existing) == [], path.name


def test_app_dynamic_imports_are_found_and_exist() -> None:
    text = static_text(STATIC_DIR / "app.js")
    dynamic = {m.group(2) for m in _DYNAMIC_IMPORT_RE.finditer(text)}
    assert dynamic == {"/static/compare.js", "/static/incident.js"}
    assert all((STATIC_DIR / spec[len("/static/"):]).is_file() for spec in dynamic)


def test_incident_imports_only_shared_modules_chart_and_evidence_panel() -> None:
    specs = set(import_specifiers(static_text(STATIC_DIR / "incident.js")))
    assert specs and specs <= INCIDENT_IMPORTS
    assert {"/static/chart.js", "/static/evidence-panel.js"} <= specs


def test_exports_used_by_the_router_and_the_incident_page_exist() -> None:
    assert re.search(r"export\s+(?:async\s+)?function\s+renderIncident\b", static_text(STATIC_DIR / "incident.js"))
    assert re.search(r"export\s+function\s+renderCompare\b", static_text(STATIC_DIR / "compare.js"))
    assert re.search(r"export\s+function\s+renderChart\b", static_text(STATIC_DIR / "chart.js"))
    assert re.search(r"export\s+function\s+renderEvidencePanel\b", static_text(STATIC_DIR / "evidence-panel.js"))
    assert re.search(r"renderIncident\(\s*root\s*,\s*runId\s*,\s*UI\s*\)", static_text(STATIC_DIR / "app.js"))


def test_imported_names_are_exported_by_the_target_module() -> None:
    name_re = re.compile(r"""(?:^|\n)[ \t]*import\s*\{([^}]*)\}\s*from\s*["']/static/([^"']+)["']""")
    for path in js_files():
        for names, target in name_re.findall(static_text(path)):
            exported = static_text(STATIC_DIR / target)
            for raw in names.split(","):
                name = raw.strip().split(" as ")[0].strip()
                if name:
                    assert re.search(rf"export\s+(?:async\s+)?(?:function|const|class)\s+{name}\b", exported), (
                        f"{path.name} imports {name} which {target} does not export"
                    )


# ================================================================ index.html


@pytest.fixture(scope="module")
def index_html() -> str:
    return static_text(STATIC_DIR / "index.html")


def test_index_has_no_inline_script_body_and_only_same_origin_module_scripts(index_html: str) -> None:
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", index_html, re.DOTALL | re.IGNORECASE)
    assert scripts, "expected the module script"
    for attrs, body in scripts:
        assert body.strip() == "", "inline script body"
        match = re.search(r"""\bsrc\s*=\s*["']([^"']*)["']""", attrs)
        assert match is not None and match.group(1).startswith("/static/"), attrs
        assert 'type="module"' in attrs


def test_index_has_no_style_attribute_no_style_element_no_inline_handler(index_html: str) -> None:
    assert re.search(r"""\sstyle\s*=""", index_html, re.IGNORECASE) is None
    assert re.search(r"<style\b", index_html, re.IGNORECASE) is None
    assert re.search(r"""\son[a-z]+\s*=""", index_html, re.IGNORECASE) is None
    for tag in ("base", "iframe", "object", "embed", "form"):
        assert re.search(rf"<{tag}\b", index_html, re.IGNORECASE) is None, tag


def test_index_references_are_same_origin_and_there_is_no_external_url(index_html: str) -> None:
    assert scan_urls(index_html, "index.html") == []
    for attr, value in re.findall(r"""\b(href|src|action|srcset|poster)\s*=\s*["']([^"']*)["']""", index_html, re.IGNORECASE):
        assert _relative_reference(value), f"{attr}={value}"


def test_index_keeps_the_required_shell_elements(index_html: str) -> None:
    assert '<main id="main"' in index_html
    assert "<noscript>" in index_html
    assert 'href="/static/style.css"' in index_html
    assert 'class="skip-link"' in index_html


# ================================================================ forbidden sinks and URLs in every file


@pytest.mark.parametrize("path", static_files(), ids=file_ids(static_files()))
def test_no_forbidden_sink_in_any_static_file(path: Path) -> None:
    assert scan_sinks(static_text(path)) == [], path.name


@pytest.mark.parametrize("path", js_files(), ids=file_ids(js_files()))
def test_no_fetch_to_a_non_relative_url_and_no_inline_handler_markup(path: Path) -> None:
    text = static_text(path)
    assert scan_fetch(text) == [], path.name
    assert _HTML_HANDLER_IN_STRING_RE.search(text) is None, path.name


@pytest.mark.parametrize("path", static_files(), ids=file_ids(static_files()))
def test_no_external_url_in_any_static_file(path: Path) -> None:
    assert scan_urls(static_text(path), path.name) == [], path.name


def test_the_svg_namespace_appears_only_in_dom_js() -> None:
    holders = {p.name for p in static_files() if SVG_NS in static_text(p)}
    assert holders == {"dom.js"}
    text = static_text(STATIC_DIR / "dom.js")
    assert re.search(rf'export const SVG_NS = "{re.escape(SVG_NS)}";', text)
    assert text.count("http://") == 1


def test_dom_attribute_allowlist_has_no_style_event_or_source_attribute() -> None:
    text = static_text(STATIC_DIR / "dom.js")
    match = re.search(r"ATTR_ALLOWLIST = new Set\(\[(.*?)\]\);", text, re.DOTALL)
    assert match is not None
    names = re.findall(r'"([^"]+)"', match.group(1))
    assert names and "href" in names
    for forbidden in ("style", "src", "srcdoc", "action", "formaction", "xlink:href"):
        assert forbidden not in names
    assert not [n for n in names if n.lower().startswith("on")]
    assert "hrefAllowed" in text


def test_dom_helpers_insert_text_through_text_apis() -> None:
    text = static_text(STATIC_DIR / "dom.js")
    assert "textContent" in text and "createTextNode" in text
    assert "createElementNS(SVG_NS" in text


# ---------------------------------------------------------------- negative controls (scanners must detect)

URL_FLAGGED = {
    "CDN https URL": ("x.js", 'import "https://cdn.jsdelivr.net/npm/x.js";'),
    "CDN https URL bare": ("x.js", "https://cdn.jsdelivr.net/npm/x.js"),
    "protocol-relative URL": ("x.js", "//cdn.example.com/x.js"),
    "protocol-relative in html": ("x.html", '<script src="//cdn.example.com/x.js"></script>'),
    "plain http URL": ("x.js", 'const u = "http://example.com/a";'),
    "css import of an https URL": ("x.css", "@import url(https://fonts.googleapis.com/x);"),
    "css import of a quoted https URL": ("x.css", '@import "https://fonts.googleapis.com/x";'),
    "css url of an https URL": ("x.css", "a { background: url(https://example.com/a.png); }"),
    "css url of a protocol-relative URL": ("x.css", "a { background: url(//example.com/a.png); }"),
    "css url of a data URL": ("x.css", "a { background: url(data:image/png;base64,AAAA); }"),
    "svg namespace with a suffix": ("x.js", 'createElementNS("http://www.w3.org/2000/svg.evil.test", "svg")'),
    "svg namespace with a path": ("x.js", 'createElementNS("http://www.w3.org/2000/svg/x", "svg")'),
    "svg namespace over https": ("x.js", 'createElementNS("https://www.w3.org/2000/svg", "svg")'),
    "xlink namespace": ("x.js", 'createElementNS("http://www.w3.org/1999/xlink", "svg")'),
    "svg namespace outside the namespace argument": ("x.js", 'const u = "http://www.w3.org/2000/svg";'),
    "svg namespace as a fetch URL": ("x.js", 'fetch("http://www.w3.org/2000/svg")'),
}

URL_CLEAN = {
    "createElementNS with the exact namespace": ("x.js", 'createElementNS("http://www.w3.org/2000/svg", "svg")'),
    "createElementNS with single quotes": ("x.js", "document.createElementNS('http://www.w3.org/2000/svg', \"svg\")"),
    "the namespace constant": ("x.js", 'export const SVG_NS = "http://www.w3.org/2000/svg";'),
    "relative paths and comments": ("x.js", '// A comment, not a URL\nimport { a } from "/static/a.js";\nfetch("/api/runs");'),
    "css with a same-origin url": ("x.css", "a { background: url(/static/a.png); }\n/* comment */"),
    "html with same-origin references": ("x.html", '<link rel="stylesheet" href="/static/style.css"><a href="#main">x</a>'),
}


@pytest.mark.parametrize("label", sorted(URL_FLAGGED))
def test_url_scan_negative_control_is_flagged(label: str) -> None:
    name, text = URL_FLAGGED[label]
    assert scan_urls(text, name) != [], label


@pytest.mark.parametrize("label", sorted(URL_CLEAN))
def test_url_scan_allowed_text_is_not_flagged(label: str) -> None:
    name, text = URL_CLEAN[label]
    assert scan_urls(text, name) == [], label


SINK_CONTROLS = {
    "innerHTML": "node.innerHTML = value;",
    "outerHTML": "node.outerHTML = value;",
    "insertAdjacentHTML": "node.insertAdjacentHTML('beforeend', value);",
    "document.write": "document.write(value);",
    "eval(": "eval(value);",
    "new Function": "const f = new Function('return 1');",
    "Function(": "const f = Function('return 1');",
    "setAttribute style": "node.setAttribute('style', value);",
    "javascript:": "const u = 'javascript:alert(1)';",
    "Date.parse": "const t = Date.parse(value);",
    "new Date(": "const d = new Date(value);",
    "Intl": "const f = new Intl.DateTimeFormat();",
    "string timer": "setTimeout('run()', 5);",
    "DOMParser": "new DOMParser();",
    "XMLHttpRequest": "new XMLHttpRequest();",
    "WebSocket": "new WebSocket(x);",
}


@pytest.mark.parametrize("label", sorted(SINK_CONTROLS))
def test_sink_scan_negative_control_is_flagged(label: str) -> None:
    assert label in scan_sinks(SINK_CONTROLS[label])


def test_sink_scan_double_quoted_style_attribute_is_flagged() -> None:
    assert "setAttribute style" in scan_sinks('node.setAttribute("style", value);')


def test_sink_scan_allows_text_apis() -> None:
    clean = "node.textContent = value;\ndocument.createElement('p');\nconst n = Number(value);\nconst evaluated = 1;"
    assert scan_sinks(clean) == []


@pytest.mark.parametrize(
    "text",
    ['fetch("https://example.com/a")', 'fetch("//example.com/a")', "fetch('http://example.com')", 'fetchJson("https://x.test/a")'],
)
def test_fetch_scan_negative_control_is_flagged(text: str) -> None:
    assert scan_fetch(text) != []


@pytest.mark.parametrize("text", ['fetch("/api/runs")', "fetch(url, { method: 'GET' })", 'fetchJson("/api/runs")', "fetch(`/api/${x}`)"])
def test_fetch_scan_allows_relative_calls(text: str) -> None:
    assert scan_fetch(text) == []


def test_import_scan_negative_controls_are_flagged() -> None:
    existing = {"a.js"}
    assert unresolved_imports('import { x } from "/static/missing.js";', existing) != []
    assert unresolved_imports('import { x } from "https://cdn.example.com/a.js";', existing) != []
    assert unresolved_imports('import { x } from "./a.js";', existing) != []
    assert unresolved_imports("const m = await import(name);", existing) != []
    assert unresolved_imports('const m = await import("/static/missing.js");', existing) != []
    assert unresolved_imports('import { x } from "/static/a.js";\nconst m = await import("/static/a.js");', existing) == []


# ================================================================ copy.js


FIXED_PHRASES = {
    ("shared", "brand"): "Coroner",
    ("shared", "heading"): "Container Crash Forensics",
    ("shared", "subtitle"): "What survived after the process died?",
    ("overview", "experimentADescription"): "controlled fatal process exit via os._exit(1)",
    ("shared", "notRecorded"): "not recorded",
    ("shared", "noSavedRun"): "no saved run found",
    ("shared", "discrepancy"): "DISCREPANCY",
    ("shared", "shownToMs"): "shown to ms; original in details",
    ("shared", "derivedNotProven"): "derived, not independently proven",
    ("shared", "partialView"): "Partial view",
    ("shared", "known"): "KNOWN",
    ("shared", "unknownOrLimited"): "UNKNOWN OR LIMITED",
    ("shared", "notReadManifestUnusable"): "not read (manifest unusable)",
    ("axisTitle", "raw_capture"): "Raw Probe Capture_Time",
    ("axisTitle", "corrected"): "Derived host-equivalent time",
    ("axisTitle", "raw_uncorrected"): "Raw Probe Capture_Time",
    ("axisNote", "corrected"): "Capture_Time \u2212 recorded clock_offset_ms. Derived, not independently proven.",
    ("axis", "sigkillLabel"): "SIGKILL",
    ("axis", "sigkillLegend"): "dotted purple line, \"{label}\": SIGKILL sent (Host_Clock; position derived, not independently proven)",
    ("chart", "limitLabel"): "{mib} MiB limit",
    ("dockerSource", "inspect_and_manifest_agree"): "Docker inspect (raw file and manifest agree)",
    ("dockerSource", "manifest_only"): "manifest-derived (raw inspect unavailable)",
    ("dockerSource", "raw_only"): "raw inspect only (manifest has no container_state)",
    ("incident", "memoryZero"): "0 bytes (no memory limit configured)",
    ("incident", "sampleTimingNotRecorded"): "sample timing not recorded",
    ("evidence", "runnerBlockTitle"): "recorded by the Runner",
    ("evidence", "derivedUncertaintyNone"): "uncertainty not recorded",
    ("evidence", "derivedOrderNotEstablished"): "order not established within the recorded clock uncertainty and tolerance",
    ("incident", "durabilityCrash"): "Crash_Durable",
    ("incident", "durabilityProcess"): "Process_Durable",
    ("incident", "lastReceive"): "last Receive_Time",
    ("incident", "toleranceNote_out_of_range"): "out of range",
    ("incident", "toleranceNote_not_recorded"): "not recorded",
    # compare page (task 4.6): the headline phrase and the statements about what the values do not establish
    ("compare", "headline"): "The same exit code does not always mean the same failure",
    ("compare", "rowVerdict"): "Verdict (set by the Runner)",
    ("compare", "limitsHeading"): "What these values do not establish",
    ("compare", "limitExit"): (
        "ExitCode alone does not establish a cause. ExitCode and OOMKilled are two separate recorded facts, "
        "and neither is inferred from the other."
    ),
    ("compare", "limitSlots"): (
        "Each slot shows only its own run. Experiment B is never inferred from Experiment C, "
        "and no cause or confidence is stated by the Viewer."
    ),
    ("compare", "runnerHeading"): "Recorded by the Runner (interpretation, not established by the Viewer)",
    ("compare", "stateUnavailable"): "no saved run found for run id {run}",
}

FIXED_SUBSTRINGS = [
    "does not establish the order of two events",
    "are not recorded as synchronised",
    "does not bound the Docker_Clock or the Container_Clock",
    "correction unavailable",
    "clock offsets conflict",
    "not compared: not recorded in",
    "analysis relative to the container stop is unavailable",
]


@pytest.mark.parametrize("path", sorted(FIXED_PHRASES), ids=lambda p: ".".join(p))
def test_copy_contains_each_fixed_phrase_exactly(path: tuple[str, str]) -> None:
    assert COPY[path[0]][path[1]] == FIXED_PHRASES[path]


@pytest.mark.parametrize("phrase", FIXED_SUBSTRINGS)
def test_copy_contains_each_fixed_statement(phrase: str) -> None:
    assert any(phrase in leaf for leaf in copy_leaves(COPY)), phrase


def test_experiment_a_is_described_without_the_word_exception() -> None:
    description = COPY["overview"]["experimentADescription"]
    assert description == "controlled fatal process exit via os._exit(1)"
    assert "exception" not in description.lower()


def test_copy_is_a_deeply_frozen_object_and_has_the_fill_helper() -> None:
    text = static_text(STATIC_DIR / "copy.js")
    assert "deepFreeze({" in text and "Object.freeze" in text
    assert re.search(r"export function fill\(", text)


_COPY_REF_RE = re.compile(r"\bCOPY\.([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*)|(\[))?")


@pytest.mark.parametrize("path", [p for p in js_files() if p.name != "copy.js"], ids=lambda p: p.name)
def test_every_copy_key_used_by_a_module_exists(path: Path) -> None:
    missing = []
    for group, key, _bracket in _COPY_REF_RE.findall(static_text(path)):
        if group not in COPY:
            missing.append(group)
        elif key and key not in COPY[group]:
            missing.append(f"{group}.{key}")
    assert missing == [], missing


# keys that modules build from a prefix and an API code, and the codes the API can return
DYNAMIC_COPY_KEYS = {
    "dockerSource": ["inspect_and_manifest_agree", "inspect_and_manifest_disagree", "manifest_only", "raw_only", "unavailable"],
    "incident": [
        "rawReason_missing", "rawReason_unreadable", "rawReason_invalid_json", "rawReason_rejected", "rawReason_too_large",
        "toleranceNote_out_of_range", "toleranceNote_not_recorded", "toleranceNote_invalid_type",
        "artifact_manifest", "artifact_evidence", "artifact_inspect",
    ],
    "evidence": [
        "derivedKind_sample_to_sigkill", "derivedKind_sample_to_finished_at", "derivedKind_finished_at_to_sigkill",
        "derivedSample_last_persisted", "derivedSample_latest_capture",
        "toleranceUnavailable_not_recorded", "toleranceUnavailable_out_of_range", "toleranceUnavailable_invalid_type",
    ],
    "chart": [
        "correctionReason_offset_not_recorded", "correctionReason_clock_offsets_conflict",
        "correctionReason_correction_out_of_range", "sigkillReason_offset_not_recorded", "sigkillReason_offset_conflict",
        "sigkillReason_correction_out_of_range", "sigkillReason_sigkill_time_not_recorded", "sigkillReason_not_experiment_b",
    ],
    "times": [
        "crash_requested", "sigkill_sent", "docker_finished_at", "last_persisted_capture", "latest_capture",
        "last_persisted_receive",
    ],
    "clocks": ["container", "collector", "host", "docker"],
    "axisTitle": ["raw_capture", "corrected", "raw_uncorrected"],
    "compare": ["omittedName_S1", "omittedName_S2", "omittedName_S3", "omittedName_S4", "omittedName_headline"],
}


@pytest.mark.parametrize("group", sorted(DYNAMIC_COPY_KEYS))
def test_copy_has_every_key_built_from_an_api_code(group: str) -> None:
    assert [k for k in DYNAMIC_COPY_KEYS[group] if k not in COPY[group]] == []


# ================================================================ API fields read by the incident page


def _model_key_sets() -> set[str]:
    names: set[str] = set()
    for attr, value in vars(viewer_model).items():
        if attr.endswith("_KEYS") and isinstance(value, frozenset):
            names |= {k for k in value if isinstance(k, str)}
    return names


def _incident_property_reads(text: str) -> set[str]:
    text = re.sub(r"(?m)(?<![:\"'])//[^\n]*", "", text)
    text = re.sub(r"\bCOPY\.[A-Za-z]+\.[A-Za-z0-9_]+", "", text)
    return set(re.findall(r"\b[a-z]\w*\??\.([a-z]+(?:_[a-z0-9]+)+)\b", text))


def test_incident_reads_only_fields_that_the_api_can_return() -> None:
    reads = _incident_property_reads(static_text(STATIC_DIR / "incident.js"))
    assert {"valid_record_count", "tolerance_status", "raw_unavailable_reason", "observed_size_text"} <= reads
    assert sorted(reads - _model_key_sets()) == []


# ================================================================ CSP, security headers, markup as data


def _csp(client: TestClient) -> dict[str, list[str]]:
    header = client.get("/", headers=HOST).headers["content-security-policy"]
    directives: dict[str, list[str]] = {}
    for part in header.split(";"):
        tokens = part.split()
        if tokens:
            directives[tokens[0]] = tokens[1:]
    return directives


def test_csp_forbids_inline_script_and_inline_style(client: TestClient) -> None:
    csp = _csp(client)
    assert csp["default-src"] == ["'none'"]
    assert csp["script-src"] == ["'self'"]
    assert csp["style-src"] == ["'self'"]
    for directive, values in csp.items():
        for forbidden in ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "*", "data:", "blob:", "http:", "https:"):
            assert forbidden not in values, f"{directive} allows {forbidden}"
    assert csp["connect-src"] == ["'self'"]
    assert csp["base-uri"] == ["'none'"] and csp["form-action"] == ["'none'"]


def test_static_files_are_compatible_with_the_csp(client: TestClient) -> None:
    """No inline script, inline style, inline handler or external reference in any file the CSP covers."""
    csp = _csp(client)
    assert csp["script-src"] == ["'self'"] and csp["style-src"] == ["'self'"]
    for path in static_files():
        text = static_text(path)
        assert scan_urls(text, path.name) == [], path.name
        assert scan_sinks(text) == [], path.name
        if path.suffix == ".html":
            assert re.search(r"<script\b[^>]*>\s*(?!</script>)\S", text) is None, path.name
            assert re.search(r"<style\b|\sstyle\s*=|\son[a-z]+\s*=", text, re.IGNORECASE) is None, path.name
        if path.suffix == ".css":
            assert "@import" not in text or all(_relative_reference(m) for m in _CSS_IMPORT_RE.findall(text))


@pytest.mark.parametrize("url", ["/", "/compare", "/runs/b-20261120T211300Z-00000002", "/static/index.html"])
def test_html_responses_carry_the_csp_and_nosniff(client: TestClient, url: str) -> None:
    response = client.get(url, headers=HOST)
    assert response.status_code == 200
    assert response.headers["content-type"].lower().startswith("text/html")
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_html_markup_in_an_artifact_value_comes_back_from_the_api_unchanged(tmp_path: Path) -> None:
    markup = '<script>alert("x")</script><img src=x onerror=alert(1)>&amp; <b>bold</b>'
    runs = tmp_path / "runs"
    run_id = RunsBuilder(runs).add_run("B", run_id="b-20261120T211300Z-0000000f", samples=sample_specs(2))
    manifest = runs / run_id / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["termination_description"] = markup
    data["termination"] = {"posted": True, "cause_label": markup}
    manifest.write_text(json.dumps(data), encoding="utf-8")

    local = TestClient(create_app(runs, PORT), base_url=f"http://127.0.0.1:{PORT}")
    response = local.get(f"/api/runs/{run_id}", headers=HOST)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["runner_text"]["termination_description"] == markup
    assert body["runner_text"]["cause_label"] == markup
    assert "&lt;" not in response.text and "&amp;amp;" not in response.text


# ================================================================ compare page files (task 4.6)
#
# THESE CHECKS DO NOT EXECUTE JAVASCRIPT. They read `compare.js`, `compare-logic.js` and `copy.js` as
# text and check what can be known without running them: imports, forbidden sinks and URLs, the data
# sources the page names, where the headline text lives, the wording of the strings that the compare
# files can show, and how the two files are served. That the logic computes the right statements is
# checked by the Python reference model and, when Node is available, by the vector runner. That the page
# renders correctly is checked by the browser tests and the manual procedure of task 4.7.
# Requirements: 8.14, 8.15, 11.32, 13.1, 13.2.

COMPARE_JS = STATIC_DIR / "compare.js"
COMPARE_LOGIC_JS = STATIC_DIR / "compare-logic.js"
COMPARE_FILES = [COMPARE_JS, COMPARE_LOGIC_JS]
HEADLINE_PHRASE = "The same exit code does not always mean the same failure"
PLACEHOLDER_PHRASE = "implemented in checkpoint M1.1-D"
COMPARE_IMPORTS = {"/static/copy.js", "/static/api.js", "/static/compare-logic.js", "/static/dom.js"}
ALLOWED_API_LITERALS = {"/api/runs", "/api/runs/"}


def strip_js_comments_and_strings(text: str) -> tuple[str, list[str]]:
    """The code with comments removed and string literals blanked, and the list of string literal values.

    A small scanner that understands "...", '...' and `...` literals (no nesting) and // and /* */ comments.
    The compare files contain no regular expression literals.
    """
    code: list[str] = []
    strings: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif ch in "\"'`":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            strings.append(text[i + 1:j])
            code.append(ch + ch)
            i = j + 1
        else:
            code.append(ch)
            i += 1
    return "".join(code), strings


def js_code(path: Path) -> str:
    return strip_js_comments_and_strings(static_text(path))[0]


def js_strings(path: Path) -> list[str]:
    return strip_js_comments_and_strings(static_text(path))[1]


PURITY_FORBIDDEN = {
    "document": r"\bdocument\b",
    "window": r"\bwindow\b",
    "fetch": r"\bfetch\w*\b",
    "import": r"\bimport\b",
    "require": r"\brequire\s*\(",
    "browser global": r"\b(?:globalThis|self|location|history|navigator|localStorage|sessionStorage|console|process|performance)\b",
    "timer": r"\bset(?:Timeout|Interval|Immediate)\b|\brequestAnimationFrame\b|\bqueueMicrotask\b",
    "randomness": r"\bMath\s*\.\s*random\b|\bcrypto\b",
    "Date": r"\bDate\b",
    "network or storage": r"\b(?:XMLHttpRequest|WebSocket|EventSource|indexedDB|Worker|Request|Response|URL|URLSearchParams)\b",
}


def purity_findings(source: str) -> list[str]:
    code = strip_js_comments_and_strings(source)[0]
    return [label for label, pattern in PURITY_FORBIDDEN.items() if re.search(pattern, code)]


# Causal wording: anything that states or hints why a process ended. The Viewer states no cause.
CAUSAL_WORDING = re.compile(
    r"\bbecause\b|\bcaus(?:e|es|ed|ing)\s+by\b|\bcaused\b|\bdue\s+to\b|\bas\s+a\s+result\b|\btherefore\b|\bthus\b|"
    r"\bhence\b|\bso\s+that\b|\bleads?\s+to\b|\bresult(?:s|ed|ing)?\s+(?:in|from)\b|\btriggered\b|\bkilled\s+by\b|"
    r"\broot\s+cause\b|\bexplains?\b|\bproves?\b|\bproof\b|\bindicates?\b|\bimplies\b|\bsince\b|\bout\s+of\s+memory\b|"
    r"\bmemory\s+(?:exhaustion|pressure)\b|\bexhaust\w*\b|\bleak\w*\b|\bcrash(?:ed|es)?\s+(?:because|due)\b|"
    r"\bexception\b|\bOOM[-\s]?kill(?:er|ed)\b|\bSIGKILL\b|\bsignal\b",
    re.IGNORECASE,
)
# The only phrases that may contain the word "cause" in a string the compare files can show: two
# statements that the Viewer does not state a cause, and the name of the recorded Runner field.
APPROVED_CAUSE_PHRASES = (
    "does not establish a cause",
    "no cause or confidence is stated by the Viewer",
    "cause_label",
)


def wording_findings(strings: list[str]) -> list[str]:
    findings = []
    for text in strings:
        plain = text.replace("OOMKilled", "")  # the recorded field name is not the word "killed"
        for phrase in APPROVED_CAUSE_PHRASES:
            plain = plain.replace(phrase, "")
        for match in CAUSAL_WORDING.finditer(plain):
            findings.append(f"causal wording {match.group(0)!r} in {text[:60]!r}")
        if re.search(r"\bcaus", plain, re.IGNORECASE):
            findings.append(f"unapproved use of 'cause' in {text[:60]!r}")
    return findings


def compare_copy_leaves() -> list[str]:
    return copy_leaves(COPY["compare"])


# ---------------------------------------------------------------- placeholder, files, allowlist


def test_compare_placeholder_text_is_gone() -> None:
    for path in static_files():
        assert PLACEHOLDER_PHRASE not in static_text(path), path.name
    for path in sorted(VIEWER_PY_DIR.glob("*.py")):
        assert PLACEHOLDER_PHRASE not in path.read_text(encoding="utf-8"), path.name
    assert PLACEHOLDER_PHRASE not in " ".join(copy_leaves(COPY))


VIEWER_PY_DIR = STATIC_DIR.parent


def test_compare_files_exist_and_compare_logic_is_a_js_file_in_the_allowlist() -> None:
    assert {p.name for p in COMPARE_FILES} == {"compare.js", "compare-logic.js"}
    for path in COMPARE_FILES:
        assert path.is_file() and not path.is_symlink()
        assert ALLOWLIST_RE.fullmatch(path.name) is not None


@pytest.mark.parametrize("path", COMPARE_FILES, ids=file_ids(COMPARE_FILES))
def test_compare_files_are_served_from_the_allowlist_with_the_script_content_type(client: TestClient, path: Path) -> None:
    response = client.get(f"/static/{path.name}", headers=HOST)
    assert response.status_code == 200
    assert response.headers["content-type"].lower() == "text/javascript; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content == path.read_bytes()
    assert path.name in client.app.state.static_assets  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ["compare-logic.JS", "Compare.js", "compare-logic.js.map", "compare_logic.mjs", "../compare.js", "compare-logic.js/"])
def test_names_near_the_compare_files_are_not_served(client: TestClient, name: str) -> None:
    assert client.get(f"/static/{name}", headers=HOST).status_code == 404


def test_registered_route_set_is_unchanged_by_the_compare_page(client: TestClient) -> None:
    paths = sorted(route.path for route in client.app.routes)
    assert paths == sorted(
        ["/", "/runs/{run_id:path}", "/compare", "/static/{asset:path}", "/api/runs", "/api/runs/{run_id:path}"]
    )
    assert all(set(route.methods) == {"GET"} for route in client.app.routes)


# ---------------------------------------------------------------- imports


def test_compare_js_imports_only_the_shared_modules_and_compare_logic() -> None:
    specs = import_specifiers(static_text(COMPARE_JS))
    assert set(specs) == COMPARE_IMPORTS
    assert len(specs) == len(set(specs))
    assert "evaluateComparison" in re.search(
        r"import\s*\{([^}]*)\}\s*from\s*\"/static/compare-logic\.js\"", static_text(COMPARE_JS)
    ).group(1)  # type: ignore[union-attr]
    assert re.search(r"\bimport\s*\(", static_text(COMPARE_JS)) is None  # no dynamic import


def test_compare_logic_has_no_import_and_exports_only_evaluate_comparison() -> None:
    text = static_text(COMPARE_LOGIC_JS)
    assert import_specifiers(text) == []
    assert re.search(r"(?m)^\s*import\b", text) is None
    exports = re.findall(r"(?m)^\s*export\s+(?:async\s+)?(?:function|const|let|var|class|default)\s*\*?\s*(\w+)", text)
    assert exports == ["evaluateComparison"]
    assert len(re.findall(r"(?m)^\s*export\b", text)) == 1


def test_only_compare_js_imports_compare_logic() -> None:
    importers = {p.name for p in js_files() if "/static/compare-logic.js" in import_specifiers(static_text(p))}
    assert importers == {"compare.js"}


# ---------------------------------------------------------------- compare-logic.js purity


def test_compare_logic_is_pure_no_dom_no_network_no_clock_no_globals() -> None:
    """Static check on the code with comments and string literals removed. It does not run the logic."""
    assert purity_findings(static_text(COMPARE_LOGIC_JS)) == []


def test_compare_logic_text_references_document_window_fetch_import_nowhere() -> None:
    code = js_code(COMPARE_LOGIC_JS)
    for word in ("document", "window", "fetch", "import"):
        assert re.search(rf"\b{word}\b", code) is None, word


def test_compare_logic_strings_contain_no_markup() -> None:
    for text in js_strings(COMPARE_LOGIC_JS):
        assert "<" not in text and ">" not in text and "&" not in text, text


PURITY_CONTROLS = {
    "document": "const n = document.createElement('p');",
    "window": "const w = window.location;",
    "fetch": "await fetch('/api/runs');",
    "fetchJson": "fetchJson('/api/runs');",
    "import": "import { x } from '/static/dom.js';",
    "dynamic import": "const m = await import('/static/dom.js');",
    "globalThis": "globalThis.x = 1;",
    "console": "console.log(1);",
    "Date": "const t = Date.now();",
    "timer": "setTimeout(f, 1);",
    "random": "const r = Math.random();",
}


@pytest.mark.parametrize("label", sorted(PURITY_CONTROLS))
def test_purity_scan_negative_control_is_flagged(label: str) -> None:
    assert purity_findings(PURITY_CONTROLS[label]) != [], label


def test_purity_scan_ignores_comments_and_strings_and_allows_plain_logic() -> None:
    clean = '// no document, window, fetch or import here\nconst a = "import document";\n/* fetch */\nexport function f(x) { return x + 1; }\n'
    assert purity_findings(clean) == []


# ---------------------------------------------------------------- forbidden patterns on the compare files


@pytest.mark.parametrize("path", COMPARE_FILES, ids=file_ids(COMPARE_FILES))
def test_compare_files_pass_every_forbidden_pattern_scan(path: Path) -> None:
    text = static_text(path)
    assert scan_sinks(text) == []
    assert scan_urls(text, path.name) == []
    assert scan_fetch(text) == []
    assert _HTML_HANDLER_IN_STRING_RE.search(text) is None
    assert unresolved_imports(text, {p.name for p in static_files()}) == []
    code = js_code(path)
    for pattern in (
        r"\.\s*(?:inner|outer)HTML\b", r"\binsertAdjacentHTML\b", r"\bdocument\s*\.\s*write", r"\beval\b",
        r"\bFunction\b", r"\bsetAttribute\b", r"\bsetAttributeNS\b", r"\.\s*style\b", r"\bon[a-z]{3,}\s*(?::|=(?!=))",
        r"\baddEventListener\b", r"\bdangerouslySetInnerHTML\b", r"\bsrcdoc\b",
    ):
        assert re.search(pattern, code) is None, (path.name, pattern)


@pytest.mark.parametrize("path", COMPARE_FILES, ids=file_ids(COMPARE_FILES))
def test_compare_files_have_no_cdn_or_external_reference_even_in_comments(path: Path) -> None:
    text = static_text(path)
    assert "http://" not in text and "https://" not in text and "//cdn" not in text
    assert re.search(r"\b(?:cdn|unpkg|jsdelivr|googleapis|cloudflare)\b", text, re.IGNORECASE) is None


# ---------------------------------------------------------------- compare.js data sources and DOM use


def test_compare_js_makes_no_direct_network_call_and_uses_only_the_two_api_helpers() -> None:
    code = js_code(COMPARE_JS)
    assert re.search(r"\bfetch\s*\(", code) is None
    assert re.search(r"\bfetchJson\b", code) is None
    assert set(re.findall(r"\bfetch\w*\b", code)) == {"fetchRun", "fetchRuns"}
    assert re.search(r"\bfetchRuns\s*\(\s*\)", code) is not None
    assert re.search(r"\bfetchRun\s*\(\s*runId\s*\)", code) is not None


def test_compare_js_names_no_url_other_than_run_links() -> None:
    literals = js_strings(COMPARE_JS)
    slashed = sorted({s for s in literals if s.startswith("/") and s not in COMPARE_IMPORTS})
    assert slashed == ["/compare", "/compare?", "/runs/"]
    assert not [s for s in literals if s.startswith("/api")]
    code = js_code(COMPARE_JS)
    assert re.search(r"\blocation\s*\.\s*(?!search\b)\w+", code) is None  # reads location.search only
    assert re.search(r"\b(?:history|navigator|localStorage|sessionStorage|window\.open)\b", code) is None
    assert re.search(r"""["']form["']""", code) is None  # selector uses links, not a form


def test_api_urls_across_the_static_files_are_still_the_two_existing_routes() -> None:
    """No new endpoint is called by any module, compare.js included."""
    found: dict[str, set[str]] = {}
    for path in js_files():
        urls = {s for s in js_strings(path) if s.startswith("/api")}
        if urls:
            found[path.name] = urls
    assert found == {"api.js": ALLOWED_API_LITERALS}
    api_text = static_text(STATIC_DIR / "api.js")
    assert re.findall(r"method\s*:\s*(\"[A-Z]+\")", api_text) == ['"GET"']
    for path in js_files():
        assert re.findall(r"""method\s*:\s*["']([A-Za-z]+)["']""", static_text(path)) in ([], ["GET"]), path.name


def test_compare_js_builds_every_node_through_the_dom_helpers_with_text_content() -> None:
    """Evidence-derived strings reach the page only through `textEl` / `el`, which use text nodes (dom.js)."""
    code = js_code(COMPARE_JS)
    assert re.search(r"\bcreateElement(?:NS)?\b|\.\s*textContent\b|\bcreateTextNode\b|\bDocumentFragment\b|\bcloneNode\b", code) is None
    assert re.search(r'import\s*\{[^}]*\bel\b[^}]*\}\s*from\s*"/static/dom\.js"', static_text(COMPARE_JS))
    assert re.search(r'import\s*\{[^}]*\btextEl\b[^}]*\}\s*from\s*"/static/dom\.js"', static_text(COMPARE_JS))
    dom = static_text(STATIC_DIR / "dom.js")
    assert "textContent" in dom and "createTextNode" in dom


def test_compare_js_link_targets_are_built_only_from_fixed_prefixes_and_encoded_ids() -> None:
    code = static_text(COMPARE_JS)
    hrefs = re.findall(r"\bhref\s*:\s*(.+?)\s*\}", code)
    assert hrefs, "expected links"
    allowed = {'"/runs/" + encodeURIComponent(slot.run_id)', "compareHref(withSlot(explicit, slot.key, null))",
               "compareHref(withSlot(explicit, slot.key, item.run_id))"}
    assert {h.strip() for h in hrefs} == allowed
    assert 'parts.push(key + "=" + encodeURIComponent(ids[key]))' in code
    assert 'return parts.length === 0 ? "/compare" : "/compare?" + parts.join("&");' in code


def test_compare_js_has_no_sentence_literal_so_user_visible_text_comes_from_copy_js() -> None:
    """Every sentence the page shows is a COPY entry. Only class names, tags and punctuation are literals."""
    literals = js_strings(COMPARE_JS)
    sentences = [s for s in literals if re.match(r"^[A-Z][a-z]+\b.*\s", s) or re.match(r"^[a-z]+(?: [a-z]+){2,}$", s)]
    assert sentences == []
    leaves = [leaf for leaf in copy_leaves(COPY) if len(leaf) >= 8]
    assert [s for s in literals if len(s) >= 8 and s in leaves] == []
    assert "COPY.compare" in static_text(COMPARE_JS)


def test_compare_copy_is_used_only_by_compare_js() -> None:
    for path in js_files():
        if path.name in {"compare.js", "copy.js"}:
            continue
        assert "COPY.compare" not in static_text(path), path.name


def test_compare_fill_calls_pass_exactly_the_placeholders_of_the_template() -> None:
    text = static_text(COMPARE_JS)
    calls = re.findall(r"\bfill\(\s*COPY\.compare\.(\w+)\s*,\s*\{(.*?)\}\s*[,)]", text, re.DOTALL)
    assert len(calls) >= 12
    for key, args in calls:
        given = set(re.findall(r"(?:^|[,{])\s*(\w+)\s*:", args))
        wanted = set(re.findall(r"\{(\w+)\}", COPY["compare"][key]))
        assert given == wanted, (key, given, wanted)


# ---------------------------------------------------------------- the headline phrase


def test_headline_phrase_lives_in_copy_js_and_in_the_logic_only() -> None:
    holders = {p.name: static_text(p).count(HEADLINE_PHRASE) for p in static_files() if HEADLINE_PHRASE in static_text(p)}
    assert holders == {"copy.js": 1, "compare-logic.js": 1}
    assert COPY["compare"]["headline"] == HEADLINE_PHRASE
    assert re.search(r'const HEADLINE_TEXT = "' + re.escape(HEADLINE_PHRASE) + '";', static_text(COMPARE_LOGIC_JS))
    assert HEADLINE_PHRASE not in "".join(js_strings(COMPARE_JS))


def test_headline_copy_key_is_used_only_inside_compare_js_headline_section() -> None:
    for path in js_files():
        if path.name not in {"compare.js", "copy.js"}:
            assert not re.search(r"\bheadline\b", js_code(path)) or path.name == "compare-logic.js", path.name
    code = js_code(COMPARE_JS)
    uses = [m.start() for m in re.finditer(r"COPY\.compare\.headline\b", code)]
    assert len(uses) == 2
    start = code.index("function headlineSection(")
    end = code.index("\n}\n", start)
    assert all(start < u < end for u in uses)
    body = static_text(COMPARE_JS)
    body = body[body.index("function headlineSection("):]
    body = body[:body.index("\n}\n")]
    # the guard: nothing is built unless the logic returned exactly the phrase
    assert re.search(r"if\s*\(\s*result\.headline\s*!==\s*COPY\.compare\.headline\s*\)\s*\{\s*return null;\s*\}", body)
    assert body.index("return null") < body.index("textEl(")
    assert len(re.findall(r"\bheadlineSection\s*\(", code)) == 2  # the definition and one call
    assert re.search(r"const headline = headlineSection\(result\);\s*if \(headline !== null\) \{\s*page\.append\(headline\);", static_text(COMPARE_JS))
    assert "evaluateComparison(facts)" in code and re.search(r"\bconst result = evaluateComparison\(", code)


def test_compare_js_never_computes_a_statement_or_the_headline_itself() -> None:
    code = re.sub(r"typeof\s+[\w.]+\s*===\s*\"\"", "", js_code(COMPARE_JS))  # type checks of recorded values
    assert re.search(r"\b137\b", code) is None
    assert re.search(r"""["'](?:S1|S2|S3|S4)["']""", code) is None
    assert re.search(r"\bexit_code\s*(?:===|!==|==|!=|<|>)", code) is None
    assert re.search(r"\boom_killed\s*(?:===|!==|==|!=)", code) is None
    assert re.search(r"\bresult\s*\.\s*headline\s*=[^=]", code) is None  # the logic result is never modified here
    shown = code[code.index("function statementsSection("):code.index("function limitationsSection(")]
    assert set(re.findall(r"\bresult\.(\w+)", shown)) == {"statements", "omitted", "headline"}


def test_compare_logic_headline_is_set_in_exactly_one_place_after_the_oomkilled_difference() -> None:
    code = js_code(COMPARE_LOGIC_JS)
    assert len(re.findall(r"(?<!\.)\bheadline\s*=[^=]", code)) == 2  # `let headline = null` and one assignment
    assert re.search(r"\bheadline\s*=\s*HEADLINE_TEXT\s*;", code)
    text = static_text(COMPARE_LOGIC_JS)
    assignment = text.index("headline = HEADLINE_TEXT;")
    guard = text.index('oomOf("b") !== oomOf("c")')
    assert guard < assignment
    assert text.index("shared") < guard
    assert 'const SHARED_EXIT_CODE = 137;' in text
    assert 'return { statements, headline, omitted };' in text


# ---------------------------------------------------------------- A, B and C wording: no invented cause


def test_compare_logic_strings_state_no_cause() -> None:
    assert wording_findings(js_strings(COMPARE_LOGIC_JS)) == []


def test_compare_copy_strings_state_no_unapproved_cause() -> None:
    assert wording_findings(compare_copy_leaves()) == []


def test_compare_js_strings_state_no_cause() -> None:
    assert wording_findings(js_strings(COMPARE_JS)) == []


def test_compare_statement_templates_are_the_designed_wording() -> None:
    strings = js_strings(COMPARE_LOGIC_JS)
    joined = "\n".join(strings)
    for fragment in (
        "Experiment A differs from Experiment B and Experiment C by exit code (A=",
        "Experiment B and Experiment C share ExitCode 137",
        "OOMKilled distinguishes Experiment B (",
        ") from Experiment C (",
        "Evidence survived all three deaths in the recorded runs (Samples: A=",
    ):
        assert fragment in joined, fragment
    # every sentence-like literal that is not an omitted reason is one of the four statements
    statement_texts = [s for s in strings if s.startswith(("Experiment ", "OOMKilled ", "Evidence "))]
    assert len(statement_texts) == 4  # the S1, S2, S3 and S4 texts (the S3 tail begins with a parenthesis)


def test_compare_slots_are_described_only_as_the_experiment_types_a_b_and_c() -> None:
    logic = "\n".join(s for s in js_strings(COMPARE_LOGIC_JS) if " " in s and s != HEADLINE_PHRASE)  # sentences, not field names
    for word in ("crash", "kill", "memory", "limit", "fault", "failure", "fatal", "terminat"):
        assert word not in logic.lower().replace("oomkilled", ""), word
    assert "controlled" not in logic.lower()
    copy_text = "\n".join(compare_copy_leaves())
    assert "exception" not in copy_text.lower()
    # the Verdict is the Runner's: the compare copy says so and never derives one
    assert COPY["compare"]["rowVerdict"] == "Verdict (set by the Runner)"
    assert re.search(r"\bverdict\b", js_code(COMPARE_LOGIC_JS), re.IGNORECASE) is None


WORDING_CONTROLS = {
    "because": "The process stopped because the container ran out of memory",
    "caused by": "The exit was caused by the memory limit",
    "due to": "B failed due to the limit",
    "therefore": "ExitCode 137, therefore OOM",
    "root cause": "The root cause is the limit",
    "out of memory": "Out of memory",
    "bare cause": "The cause of the exit is unknown",
    "killed by": "Killed by the kernel",
    "oom killer": "The OOM killer ended it",
    "sigkill": "SIGKILL was sent",
    "exception": "Experiment A raised an exception",
    "leak": "a memory leak",
}


@pytest.mark.parametrize("label", sorted(WORDING_CONTROLS))
def test_wording_scan_negative_control_is_flagged(label: str) -> None:
    assert wording_findings([WORDING_CONTROLS[label]]) != [], label


def test_wording_scan_allows_the_approved_phrases_and_recorded_field_names() -> None:
    allowed = [
        "ExitCode alone does not establish a cause.",
        "no cause or confidence is stated by the Viewer.",
        "cause_label",
        "OOMKilled distinguishes Experiment B (false) from Experiment C (true)",
        "Docker OOMKilled",
    ]
    assert wording_findings(allowed) == []


def test_js_string_scanner_handles_quotes_comments_and_escapes() -> None:
    code, strings = strip_js_comments_and_strings('const a = "x // y"; // c "d"\n/* "e" */ const b = \'it\\\'s\'; const c = `t`;')
    assert strings == ["x // y", "it\\'s", "t"]
    assert "c \"d\"" not in code and '"e"' not in code
