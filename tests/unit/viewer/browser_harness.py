"""Real-browser harness for the Viewer chart checks (headless Chrome or Edge, no extra dependency).

The real Viewer app (``create_app``) is served by uvicorn on 127.0.0.1 in a background thread, on a runs
directory the caller owns (a temporary one in the tests). A thin ASGI wrapper changes nothing the app
serves except that it adds ``/__measure.js`` and inserts one ``<script type="module" src="/__measure.js">``
into the HTML of ``/runs/<id>``. That script runs after the real page has drawn its chart, measures the
real DOM and writes the result into a hidden ``<pre id="__result">``. The browser is driven with
``--dump-dom`` (measurements) and ``--screenshot``, so no WebSocket or driver library is needed.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from coroner.viewer.app import create_app

MEASURE_JS = r"""
import { COPY } from "/static/copy.js";
import {
  splitUtcText, distinctDates, limitMibText, placeLabels, thinTickLabels, tickAnchor,
} from "/static/chart.js";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const VIEW_W = 960;
const VIEW_H = 360;

function units(rect, svgRect) {
  const k = VIEW_W / svgRect.width;
  return {
    l: (rect.left - svgRect.left) * k,
    r: (rect.right - svgRect.left) * k,
    t: (rect.top - svgRect.top) * k,
    b: (rect.bottom - svgRect.top) * k,
  };
}

function unitChecks() {
  const out = {};
  out.split = splitUtcText("2026-11-20T21:13:16.000Z");
  out.splitMs = splitUtcText("2026-11-20T21:13:16.500Z");
  out.splitNone = splitUtcText("not a time");
  out.splitNotString = splitUtcText(5);
  out.dates = distinctDates(["2026-11-21T00:00:01.000Z", "2026-11-20T23:59:59.000Z", "x", null]);
  out.mib = [limitMibText(134217728), limitMibText(105381888)];
  out.anchors = [tickAnchor(10, 50, 960), tickAnchor(950, 50, 960), tickAnchor(500, 50, 960)];
  out.thin = Array.from(thinTickLabels([
    { x: 100, width: 50, anchor: "middle" },
    { x: 105, width: 50, anchor: "middle" },
    { x: 300, width: 50, anchor: "middle" },
  ], 10)).sort();
  const bounds = { l: 0, r: 200, t: 0, b: 100 };
  const a = { x: 10, y: 20, anchor: "start" };
  const placed = placeLabels(
    [
      { id: "one", width: 60, candidates: [a] },
      { id: "two", width: 60, candidates: [a, { x: -50, y: 20, anchor: "start" }, { x: 10, y: 40, anchor: "start" }] },
    ],
    bounds, [], [],
  );
  out.placed = { one: [placed.one.x, placed.one.y, placed.one.fits], two: [placed.two.x, placed.two.y, placed.two.fits] };
  return out;
}

async function main() {
  const result = { unit: unitChecks() };
  for (let i = 0; i < 150 && !document.querySelector(".chart-svg, .chart-empty, .state-error"); i += 1) {
    await wait(100);
  }
  await wait(300);
  result.innerWidth = window.innerWidth;
  result.innerHeight = window.innerHeight;
  result.docScrollWidth = document.documentElement.scrollWidth;
  const svg = document.querySelector(".chart-svg");
  result.hasChart = svg !== null;
  result.pageError = document.querySelector(".state-error") ? document.querySelector(".state-error").textContent : null;
  if (svg) {
    const s = svg.getBoundingClientRect();
    result.svgWidth = s.width;
    result.svgHeight = s.height;
    result.viewBox = svg.getAttribute("viewBox");
    const frame = svg.parentElement.getBoundingClientRect();
    result.frameWidth = frame.width;
    result.frameScrollable = svg.parentElement.scrollWidth > svg.parentElement.clientWidth + 1;
    result.texts = Array.from(svg.querySelectorAll("text")).map((t) => ({
      cls: t.getAttribute("class") || "",
      text: t.textContent,
      anchor: t.getAttribute("text-anchor"),
      box: units(t.getBoundingClientRect(), s),
    }));
    result.tickMarks = Array.from(svg.querySelectorAll("line.chart-axis"))
      .filter((l) => l.getAttribute("y2") !== l.getAttribute("y1") && l.getAttribute("x1") === l.getAttribute("x2")
        && Number(l.getAttribute("y2")) - Number(l.getAttribute("y1")) === 5)
      .map((l) => Number(l.getAttribute("x1")));
    const lim = svg.querySelector("line.chart-limit");
    result.limitY = lim ? Number(lim.getAttribute("y1")) : null;
    const sk = svg.querySelector("line.chart-sigkill");
    result.sigkillX = sk ? Number(sk.getAttribute("x1")) : null;
    result.circles = Array.from(svg.querySelectorAll("circle.chart-point")).map((c) => [
      Number(c.getAttribute("cx")), Number(c.getAttribute("cy")),
    ]);
    result.limitDash = lim ? lim.getAttribute("stroke-dasharray") : null;
    result.sigkillDash = sk ? sk.getAttribute("stroke-dasharray") : null;
  }
  const notes = document.querySelector(".chart-axis-notes");
  result.dateNote = notes && notes.querySelector(".chart-date") ? notes.querySelector(".chart-date").textContent : null;
  result.explain = notes && notes.querySelector(".chart-axis-explain") ? notes.querySelector(".chart-axis-explain").textContent : null;
  const legend = document.querySelector(".chart-legend");
  result.legend = legend ? Array.from(legend.querySelectorAll("li")).map((li) => li.textContent) : [];
  result.legendBelowSvg = !!(legend && svg && (svg.compareDocumentPosition(legend) & Node.DOCUMENT_POSITION_FOLLOWING));
  result.tableRows = document.querySelectorAll(".sample-table tbody tr").length;
  result.tableText = (document.querySelector(".sample-table") || { textContent: "" }).textContent;
  const pre = document.createElement("pre");
  pre.id = "__result";
  pre.hidden = true;
  pre.textContent = JSON.stringify(result);
  document.body.append(pre);
  const section = document.querySelector(".chart-section");
  if (section && location.search.indexOf("shot") >= 0) {
    // Screenshots only: hide everything on the page except the chart section, so the picture is the chart.
    let node = section;
    while (node && node.parentElement && node.parentElement !== document.body) {
      for (const sib of Array.from(node.parentElement.children)) {
        if (sib !== node) {
          sib.style.display = "none";
        }
      }
      node = node.parentElement;
    }
  }
}

main().catch((err) => {
  const pre = document.createElement("pre");
  pre.id = "__result";
  pre.hidden = true;
  pre.textContent = JSON.stringify({ scriptError: String(err && err.stack ? err.stack : err) });
  document.body.append(pre);
});
"""


CONSOLE_JS = r"""
window.__errors = [];
const note = (kind, detail) => { window.__errors.push(kind + ": " + String(detail).slice(0, 300)); };
window.addEventListener("error", (e) => note("error", e.message || (e.target && (e.target.src || e.target.href)) || "resource"), true);
window.addEventListener("unhandledrejection", (e) => note("unhandledrejection", e.reason));
document.addEventListener("securitypolicyviolation", (e) => note("csp", e.violatedDirective + " " + e.blockedURI));
for (const name of ["error", "warn"]) {
  const original = console[name].bind(console);
  console[name] = (...args) => { note("console." + name, args.join(" ")); original(...args); };
}
"""

COMPARE_MEASURE_JS = r"""
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function parseColor(value) {
  const m = /rgba?\(([^)]+)\)/.exec(value);
  if (!m) { return null; }
  const p = m[1].split(",").map((s) => parseFloat(s));
  return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
}
function lum(c) {
  const f = (v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
}
function backdrop(node) {
  for (let n = node; n; n = n.parentElement) {
    const c = parseColor(getComputedStyle(n).backgroundColor);
    if (c && c.a > 0.99) { return c; }
  }
  return { r: 20, g: 22, b: 26, a: 1 };
}
function ratio(a, b) {
  const x = lum(a), y = lum(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

async function main() {
  for (let i = 0; i < 150 && !document.querySelector(".compare-page, .state-error"); i += 1) { await wait(100); }
  await wait(300);
  const r = {};
  r.innerWidth = window.innerWidth;
  r.docScrollWidth = document.documentElement.scrollWidth;
  r.errors = window.__errors || [];
  r.pageError = document.querySelector(".state-error") ? document.querySelector(".state-error").textContent : null;
  const main = document.getElementById("main");
  r.mainText = main.innerText;
  r.h1 = main.querySelectorAll("h1").length;
  r.headings = Array.from(main.querySelectorAll("h1,h2,h3")).map((h) => h.tagName + " " + h.textContent);
  r.headline = (document.querySelector(".compare-headline") || { textContent: null }).textContent;
  r.statements = Array.from(document.querySelectorAll(".statements li")).map((li) => li.textContent);
  r.omitted = Array.from(document.querySelectorAll(".omitted > li")).map((li) => li.innerText);
  r.warnings = Array.from(document.querySelectorAll(".warning")).map((w) => w.textContent);
  r.slotStates = Array.from(document.querySelectorAll(".slot-state")).map((w) => w.textContent);
  r.slotRuns = Array.from(document.querySelectorAll(".slot-run .mono")).map((w) => w.textContent);
  r.tableRows = Array.from(document.querySelectorAll(".compare-table tbody tr")).map((tr) =>
    Array.from(tr.children).map((c) => c.textContent));
  r.tableHead = Array.from(document.querySelectorAll(".compare-table thead th")).map((c) => c.textContent);
  r.runnerBlocks = Array.from(document.querySelectorAll(".compare-runner .runner-block")).map((b) => b.innerText);
  r.limits = Array.from(document.querySelectorAll(".limit-list li")).map((li) => li.textContent);
  const slots = document.querySelector(".compare-slots");
  r.slotColumns = slots ? getComputedStyle(slots).gridTemplateColumns.split(" ").length : 0;
  r.chooserLinks = Array.from(document.querySelectorAll(".chooser a")).map((a) => a.getAttribute("href"));
  // horizontal overflow: elements poking out of the viewport that are not inside a scroll container
  const vw = document.documentElement.clientWidth;
  const outside = [];
  const clipped = [];
  const scrollers = [];
  for (const node of main.querySelectorAll("*")) {
    const rect = node.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) { continue; }
    let scrolled = false;
    for (let p = node.parentElement; p && p !== document.body; p = p.parentElement) {
      if (p.classList.contains("table-wrap")) { scrolled = true; break; }
    }
    if (!scrolled && (rect.right > vw + 1 || rect.left < -1)) {
      outside.push(node.tagName + "." + node.className + " " + Math.round(rect.left) + ".." + Math.round(rect.right));
    }
    const cs = getComputedStyle(node);
    if (!node.classList.contains("visually-hidden") && cs.overflowX !== "visible" && node.scrollWidth > node.clientWidth + 1) {
      (node.classList.contains("table-wrap") ? scrollers : clipped).push(node.tagName + "." + node.className);
    }
  }
  r.outside = outside;
  r.clipped = clipped;
  r.scrollers = scrollers;
  // controls: every link/button/summary has a name and a 44px target
  const small = [];
  const unnamed = [];
  for (const node of main.querySelectorAll("a,button,summary")) {
    const rect = node.getBoundingClientRect();
    if (rect.height > 0 && rect.height < 43.5) { small.push(node.textContent.slice(0, 40) + " h=" + Math.round(rect.height)); }
    if (!node.textContent.trim()) { unnamed.push(node.tagName); }
  }
  r.smallTargets = small;
  r.unnamed = unnamed;
  // contrast of every element that directly holds text
  let minRatio = 99;
  let minWhere = "";
  for (const node of main.querySelectorAll("*")) {
    const own = Array.from(node.childNodes).some((c) => c.nodeType === 3 && c.textContent.trim() !== "");
    if (!own) { continue; }
    const rect = node.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) { continue; }
    const fg = parseColor(getComputedStyle(node).color);
    const v = ratio(fg, backdrop(node));
    if (v < minRatio) { minRatio = v; minWhere = node.tagName + "." + node.className + " " + node.textContent.slice(0, 30); }
  }
  r.minContrast = minRatio;
  r.minContrastWhere = minWhere;
  const pre = document.createElement("pre");
  pre.id = "__result";
  pre.hidden = true;
  pre.textContent = JSON.stringify(r);
  document.body.append(pre);
}

main().catch((err) => {
  const pre = document.createElement("pre");
  pre.id = "__result";
  pre.hidden = true;
  pre.textContent = JSON.stringify({ scriptError: String(err && err.stack ? err.stack : err) });
  document.body.append(pre);
});
"""


def find_browser() -> str | None:
    """Path of a Chrome, Chromium or Edge executable, or None. CORONER_BROWSER overrides the search."""
    override = os.environ.get("CORONER_BROWSER")
    if override and Path(override).is_file():
        return override
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


class _InjectMeasure:
    """ASGI wrapper: serves /__measure.js and adds its script tag to the HTML of /runs/<id>."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        assets = {
            "/__measure.js": MEASURE_JS,
            "/__console.js": CONSOLE_JS,
            "/__compare_measure.js": COMPARE_MEASURE_JS,
        }
        if path in assets:
            body = assets[path].encode("utf-8")
            await send({"type": "http.response.start", "status": 200, "headers": [
                (b"content-type", b"text/javascript; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ]})
            await send({"type": "http.response.body", "body": body})
            return
        is_compare = path == "/compare"
        if not (path.startswith("/runs/") or is_compare):
            await self.app(scope, receive, send)
            return
        start: dict[str, Any] = {}
        chunks: list[bytes] = []

        async def capture(message: Any) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, capture)
        body = b"".join(chunks)
        if start.get("status") == 200 and is_compare:
            body = body.replace(b"<head>", b'<head><script src="/__console.js"></script>', 1)
            body = body.replace(b"</head>", b'<script type="module" src="/__compare_measure.js"></script></head>', 1)
        elif start.get("status") == 200:
            body = body.replace(b"</head>", b'<script type="module" src="/__measure.js"></script></head>', 1)
        headers = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": start["status"], "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ViewerServer:
    """The Viewer on a free 127.0.0.1 port; stopped on exit."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir
        self.port = _free_port()
        app = _InjectMeasure(create_app(runs_dir, self.port))
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "ViewerServer":
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError("the Viewer test server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)

    def url(self, run_id: str) -> str:
        return f"http://127.0.0.1:{self.port}/runs/{run_id}"

    def compare_url(self, query: str = "") -> str:
        return f"http://127.0.0.1:{self.port}/compare" + (f"?{query}" if query else "")


def _run_browser(browser: str, args: list[str], timeout: int = 90) -> subprocess.CompletedProcess[bytes]:
    with tempfile.TemporaryDirectory(prefix="coroner-chrome-") as profile:
        cmd = [
            browser, "--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
            "--disable-background-networking", "--hide-scrollbars", f"--user-data-dir={profile}",
            *args,
        ]
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


def measure(browser: str, url: str, width: int, height: int = 1700) -> dict[str, Any]:
    """Load the page in headless Chrome at width x height and return the measurement object."""
    proc = _run_browser(
        browser, [f"--window-size={width},{height}", "--virtual-time-budget=20000", "--dump-dom", url]
    )
    dom = proc.stdout.decode("utf-8", errors="replace")
    match = re.search(r'<pre id="__result"[^>]*>(.*?)</pre>', dom, re.DOTALL)
    if not match:
        raise RuntimeError("no measurement in the page DOM (exit %s): %s" % (proc.returncode, proc.stderr[-400:]))
    return json.loads(html.unescape(match.group(1)))


def screenshot(browser: str, url: str, width: int, height: int, path: Path, shot_param: bool = True) -> None:
    """Save a screenshot. `shot_param` adds the chart-only `shot=1` marker; /compare rejects unknown parameters."""
    path.parent.mkdir(parents=True, exist_ok=True)
    target = url + ("&shot=1" if "?" in url else "?shot=1") if shot_param else url
    _run_browser(browser, [f"--window-size={width},{height}", "--virtual-time-budget=20000", f"--screenshot={path}", target])
