"""JavaScript execution of the /compare logic vectors (task 4.3, Requirement 8, Requirement 11 criterion 32).

Two independent executors run ``coroner/viewer/static/compare-logic.js`` over every vector in
``compare_vectors.json`` and compare the result with ``expected``:

* Node (``compare_vectors_runner.mjs``). Skipped, never passed, when ``node`` is not found.
* Headless Chrome or Edge (a throwaway local HTML page that loads the same file through a
  ``data:`` URL and writes the results into the page). Skipped when no browser is found.

Each test reports which engine ran. A skipped Node test means Node did NOT run.
"""

from __future__ import annotations

import base64
import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.viewer import browser_harness

HERE = Path(__file__).resolve().parent
VECTORS_PATH = HERE / "compare_vectors.json"
RUNNER = HERE / "compare_vectors_runner.mjs"
LOGIC_JS = HERE.parents[2] / "coroner" / "viewer" / "static" / "compare-logic.js"

VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]
EXPECTED = {v["id"]: v["expected"] for v in VECTORS}


def _node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for path in (r"C:\Program Files\nodejs\node.exe", r"C:\Program Files (x86)\nodejs\node.exe"):
        if Path(path).is_file():
            return path
    return None


def _mismatches(results: dict) -> list[str]:
    bad = [vid for vid in EXPECTED if results.get(vid) != EXPECTED[vid]]
    bad += [f"unexpected result id {vid}" for vid in results if vid not in EXPECTED]
    return bad


def test_logic_module_is_pure_and_self_contained():
    text = LOGIC_JS.read_text(encoding="utf-8")
    assert re.search(r"export\s+function\s+evaluateComparison\b", text)
    assert re.search(r"^\s*import\b", text, re.MULTILINE) is None
    assert re.search(r"\bimport\s*\(", text) is None
    for word in ("document", "window", "fetch", "innerHTML", "XMLHttpRequest", "localStorage", "globalThis"):
        assert re.search(rf"\b{word}\b", text) is None, word


def test_node_executes_every_vector(record_property):
    node = _node()
    if node is None:
        record_property("engine", "node: not available")
        pytest.skip("node not on PATH (and not in the common install directories); Node did not run")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    record_property("engine", f"node {version}")
    proc = subprocess.run(
        [node, str(RUNNER), str(VECTORS_PATH)], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    assert _mismatches(results) == []
    assert len(results) == len(VECTORS)


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>compare-logic vectors</title></head>
<body><pre id="__result"></pre>
<script>
const SOURCE_B64 = "@@SOURCE@@";
const VECTORS_B64 = "@@VECTORS@@";
const decode = (b64) => new TextDecoder().decode(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)));
const out = document.getElementById("__result");
(async () => {
  try {
    const mod = await import("data:text/javascript;base64," + SOURCE_B64);
    const vectors = JSON.parse(decode(VECTORS_B64)).vectors;
    const results = {};
    for (const vector of vectors) {
      results[vector.id] = mod.evaluateComparison(JSON.parse(JSON.stringify(vector.slots)));
    }
    out.textContent = JSON.stringify({ ok: true, results });
  } catch (err) {
    out.textContent = JSON.stringify({ ok: false, error: String(err && err.stack ? err.stack : err) });
  }
})();
</script></body></html>
"""


def test_headless_browser_executes_every_vector(tmp_path, record_property):
    browser = browser_harness.find_browser()
    if browser is None:
        record_property("engine", "browser: not available")
        pytest.skip("no headless Chrome or Edge found; the browser did not run")
    page = tmp_path / "vectors.html"
    page.write_text(
        _PAGE.replace("@@SOURCE@@", base64.b64encode(LOGIC_JS.read_bytes()).decode("ascii")).replace(
            "@@VECTORS@@", base64.b64encode(VECTORS_PATH.read_bytes()).decode("ascii")
        ),
        encoding="utf-8",
    )
    proc = browser_harness._run_browser(
        browser, ["--virtual-time-budget=20000", "--dump-dom", page.as_uri()]
    )
    dom = proc.stdout.decode("utf-8", errors="replace")
    match = re.search(r'<pre id="__result"[^>]*>(.*?)</pre>', dom, re.DOTALL)
    assert match, f"no result in the page DOM (exit {proc.returncode}): {proc.stderr[-400:]!r}"
    payload = json.loads(html.unescape(match.group(1)))
    assert payload.get("ok") is True, payload
    record_property("engine", f"browser {Path(browser).name}")
    assert _mismatches(payload["results"]) == []
    assert len(payload["results"]) == len(VECTORS)
