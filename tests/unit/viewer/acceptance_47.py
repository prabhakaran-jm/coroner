"""Task 4.7 browser acceptance checks, run in real Chrome through the DevTools Protocol.

Command line (from the repository root, with the repository's virtual environment):

    .\\.venv\\Scripts\\python.exe -m tests.unit.viewer.acceptance_47 --out docs/m1-results/browser-4.7

The real `runs/` directory is only read by a Viewer subprocess started with `python -m coroner.viewer`; scratch
fixture runs are built under the system temporary directory, never inside the repository. Every result in
the JSON comes from JavaScript that ran in Chrome (DOM text, computed styles, key events, responses). The
process exits nonzero when a required check fails. NOT TESTED is a separate status and is never a pass.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from tests.unit.viewer import cdp_harness as cdp
from tests.unit.viewer.builder import InspectSpec, RunsBuilder, SampleSpec, StateSpec, sample_specs

REAL = {
    "A": "a-20261120T211146Z-4300f395",
    "B": "b-20261120T211311Z-ce313da1",
    "C": "c-20261120T211433Z-09b81ca1",
}
ALT = {"A": "a-20261120T134321Z-a5e5a30e", "B": "b-20261120T173541Z-3fa52342", "C": "c-20261120T174958Z-94f4225c"}
READY = "document.querySelector('#main .page, #main .state-error') !== null && !document.querySelector('.state-loading')"
HEADLINE = "The same exit code does not always mean the same failure"
FAVICON = "/favicon.ico"
BPM = 1048576

# ------------------------------------------------------------------ page scripts

DESCRIBE_ACTIVE = r"""
(() => {
  const e = document.activeElement;
  if (!e) { return null; }
  const all = Array.from(document.querySelectorAll('*'));
  const r = e.getBoundingClientRect();
  const cs = getComputedStyle(e);
  return {
    tag: e.tagName, id: e.id, cls: String(e.getAttribute('class') || ''),
    text: (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 50),
    href: e.getAttribute('href'), idx: all.indexOf(e),
    rect: [r.left, r.top, r.width, r.height].map(Math.round),
    inViewport: r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth,
    outlineStyle: cs.outlineStyle, outlineWidth: cs.outlineWidth, outlineColor: cs.outlineColor,
    outlineOffset: cs.outlineOffset, focusVisible: e.matches(':focus-visible'),
    insideChart: !!e.closest('.chart-plot'),
  };
})()
"""

COMPARE_FACTS = r"""
(() => {
  const all = (s) => Array.from(document.querySelectorAll(s));
  const head = document.querySelector('.compare-headline');
  return {
    headline: head ? head.textContent : null,
    statements: all('.statements li').map((e) => e.textContent),
    omitted: all('.omitted > li').map((e) => e.innerText.replace(/\s+/g, ' ')),
    slotRuns: all('.slot-run .mono').map((e) => e.textContent),
    slotStates: all('.slot-state').map((e) => e.textContent),
    warnings: all('.warning').map((e) => e.textContent),
    slotTitles: all('.compare-slots h3').map((e) => e.textContent),
    rows: all('.compare-table tbody tr').map((tr) => Array.from(tr.children).map((c) => c.textContent)),
    mainText: document.getElementById('main').innerText,
    h1: all('#main h1').length,
    showMore: all('button').filter((b) => b.textContent.trim() === 'Show more').length,
    chooserLinks: all('.chooser a').map((a) => a.getAttribute('href')),
  };
})()
"""

LAYOUT_JS = r"""
(() => {
  const root = document.documentElement;
  const vw = root.clientWidth;
  const vis = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && n.checkVisibility({ checkVisibilityCSS: true, contentVisibilityAuto: true }); };
  const inScroller = (n) => !!n.closest('.table-wrap, .chart-plot');
  const outside = [], clipped = [], small = [];
  for (const n of document.querySelectorAll('#main *, header *')) {
    if (!vis(n) || n.closest('svg') || n.classList.contains('visually-hidden')) { continue; }
    const r = n.getBoundingClientRect();
    if (!inScroller(n) && (r.right > vw + 1 || r.left < -1)) {
      outside.push(n.tagName + '.' + (n.getAttribute('class') || '') + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
    }
    const cs = getComputedStyle(n);
    if ((cs.overflowX === 'hidden' || cs.overflowX === 'clip') && n.scrollWidth > n.clientWidth + 1) {
      clipped.push(n.tagName + '.' + (n.getAttribute('class') || ''));
    }
    if ((n.matches('button, summary, a.button, nav a, header a') || (n.matches('a') && !n.closest('p, li, td, th'))) && r.height < 43.5) {
      small.push(n.tagName + ' "' + n.textContent.trim().slice(0, 30) + '" h=' + Math.round(r.height));
    }
  }
  // overlapping text: the real line boxes of every visible text node (Range.getClientRects), pairwise
  const boxes = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement;
    if (!parent || node.textContent.trim() === '' || parent.closest('svg, script, style, .visually-hidden')) { continue; }
    if (!parent.checkVisibility({ checkVisibilityCSS: true, contentVisibilityAuto: true })) { continue; }
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const r of range.getClientRects()) {
      if (r.width > 1 && r.height > 1) { boxes.push({ r: r, node: node }); }
    }
  }
  const overlaps = [];
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      if (boxes[i].node === boxes[j].node) { continue; }
      const a = boxes[i].r, b = boxes[j].r;
      const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (w > 2 && h > 2) { overlaps.push(boxes[i].node.textContent.trim().slice(0, 25) + ' | ' + boxes[j].node.textContent.trim().slice(0, 25)); }
    }
  }
  const wraps = Array.from(document.querySelectorAll('.table-wrap')).filter(vis).map((w) => ({
    scrollW: w.scrollWidth, clientW: w.clientWidth, scrollable: w.scrollWidth > w.clientWidth + 1,
  }));
  const plot = document.querySelector('.chart-plot');
  return {
    innerWidth, clientWidth: vw, docScrollWidth: root.scrollWidth, bodyScrollWidth: document.body.scrollWidth,
    pageHorizontalOverflow: root.scrollWidth > vw + 1,
    outside, clipped, smallTargets: small, overlapCount: overlaps.length, overlaps: overlaps.slice(0, 5),
    tableWraps: wraps,
    plot: plot ? { scrollW: plot.scrollWidth, clientW: plot.clientWidth, scrollable: plot.scrollWidth > plot.clientWidth + 1 } : null,
    cardColumns: (() => { const g = document.querySelector('.cards, .compare-slots'); return g ? getComputedStyle(g).gridTemplateColumns.split(' ').length : 0; })(),
  };
})()
"""

CONTRAST_JS = r"""
(() => {
  const parse = (v) => { const m = /rgba?\(([^)]+)\)/.exec(v); if (!m) { return null; }
    const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(parseFloat);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
  const over = (f, b) => ({ r: f.r * f.a + b.r * (1 - f.a), g: f.g * f.a + b.g * (1 - f.a), b: f.b * f.a + b.b * (1 - f.a), a: 1 });
  const lin = (v) => { const s = v / 255; return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4); };
  const lum = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const backdrop = (n) => {
    const layers = [];
    for (let e = n; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c && c.a > 0) { layers.push(c); if (c.a >= 0.999) { break; } } }
    let base = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = layers.length - 1; i >= 0; i -= 1) { base = over(layers[i], base); }
    return base;
  };
  const items = [];
  for (const n of document.querySelectorAll('body *')) {
    if (n.classList.contains('visually-hidden') || n.matches('script, style, title, desc')) { continue; }
    const own = Array.from(n.childNodes).some((c) => c.nodeType === 3 && c.textContent.trim() !== '');
    if (!own) { continue; }
    const r = n.getBoundingClientRect(); const cs = getComputedStyle(n);
    if (r.width === 0 || r.height === 0 || cs.visibility === 'hidden') { continue; }
    const isSvg = n instanceof SVGElement;
    const fg = parse(isSvg ? cs.fill : cs.color);
    if (!fg) { continue; }
    const bg = backdrop(n);
    items.push({ ratio: ratio(over(fg, bg), bg), where: n.tagName + '.' + (n.getAttribute('class') || '') + ' "' + n.textContent.trim().slice(0, 25) + '"',
      fg: cs.color, size: cs.fontSize });
  }
  items.sort((a, b) => a.ratio - b.ratio);
  const root = getComputedStyle(document.documentElement);
  const tok = (name) => parse(root.getPropertyValue(name).trim().startsWith('#') ? (() => { const h = root.getPropertyValue(name).trim();
    return 'rgb(' + parseInt(h.slice(1, 3), 16) + ',' + parseInt(h.slice(3, 5), 16) + ',' + parseInt(h.slice(5, 7), 16) + ')'; })() : root.getPropertyValue(name));
  const tokens = {};
  for (const name of ['--text', '--muted', '--amber', '--red', '--purple', '--border']) {
    tokens[name] = ['--bg', '--surface', '--surface-2'].map((b) => Math.round(ratio(tok(name), tok(b)) * 100) / 100);
  }
  return { count: items.length, min: items.length ? Math.round(items[0].ratio * 100) / 100 : null,
    lowest: items.slice(0, 5).map((i) => ({ ratio: Math.round(i.ratio * 100) / 100, where: i.where, fg: i.fg, size: i.size })),
    below45: items.filter((i) => i.ratio < 4.5).length, tokens };
})()
"""

READOUT_JS = r"""
(() => {
  const r = document.querySelector('.chart-readout');
  const items = r ? Array.from(r.querySelectorAll('.readout-lines li')).map((l) => l.textContent) : [];
  const a = document.activeElement;
  return { lines: items, text: r ? r.innerText : '', activeIsPlot: !!(a && a.classList && a.classList.contains('chart-plot')),
    activedescendant: a && a.getAttribute ? a.getAttribute('aria-activedescendant') : null,
    activePointClass: (() => { const p = document.querySelector('.chart-point.point-active'); return p ? p.id : null; })() };
})()
"""

SAMPLE_ROWS_JS = r"""
(() => {
  const d = document.querySelector('details.sample-table');
  const rows = Array.from(document.querySelectorAll('details.sample-table tbody tr'));
  const more = document.querySelector('details.sample-table button');
  const tr = rows[0] ? rows[0].getBoundingClientRect() : null;
  return { open: d ? d.open : null, rowCount: rows.length,
    firstRowVisible: !!(tr && tr.height > 0 && tr.width > 0 && rows[0].checkVisibility({ checkVisibilityCSS: true, contentVisibilityAuto: true })),
    rows: rows.map((r) => Array.from(r.children).map((c) => c.textContent)),
    status: (d && d.querySelector('p.muted')) ? d.querySelector('p.muted').textContent : null,
    moreHidden: more ? (more.hidden || getComputedStyle(more).display === 'none') : null,
    moreVisible: more ? (!more.hidden && more.getBoundingClientRect().height > 0 && more.checkVisibility({ checkVisibilityCSS: true, contentVisibilityAuto: true })) : false };
})()
"""


# ------------------------------------------------------------------ recording


class Recorder:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.results: list[dict[str, Any]] = []
        self.chrome_version = ""

    def add(self, test_id: str, description: str, route: str, expected: str, actual: str, status: str, *,
            screenshot: str | None = None, console_errors: int | None = None, source: str = "real saved runs",
            viewport: str | None = None, extra: Any = None, warnings: list[str] | None = None) -> None:
        assert status in ("PASS", "FAIL", "NOT TESTED")
        self.results.append({
            "id": test_id, "description": description, "route": route, "expected": expected, "actual": actual,
            "status": status, "screenshot": screenshot, "console_error_count": console_errors,
            "evidence_source": source, "javascript_executed_in_chrome": status != "NOT TESTED",
            "viewport": viewport, "chrome": self.chrome_version, "warnings": warnings or [], "evidence": extra,
        })
        print(f"[{status}] {test_id} {description}", flush=True)

    def expect(self, ok: bool) -> str:
        return "PASS" if ok else "FAIL"


def unexpected(report: dict[str, Any], allow_http: tuple[str, ...] = (), allow_log_status: tuple[int, ...] = ()) -> list[str]:
    """Problems in a capture report. A favicon 404 (and, if allowed, named expected HTTP errors) is not one."""
    problems: list[str] = []
    problems += ["console.error: " + m for m in report["console_errors"]]
    problems += ["uncaught: " + m for m in report["uncaught_exceptions"]]
    problems += ["csp: " + m for m in report["csp_violations"]]
    problems += ["failed request: " + json.dumps(m) for m in report["failed_requests"] if not m.get("canceled")]
    for e in report["http_errors"]:
        if e["url"].endswith(FAVICON) or any(e["url"].endswith(a) or a in e["url"] for a in allow_http):
            continue
        problems.append(f"http {e['status']}: {e['url']}")
    for m in report["log_errors"]:
        if FAVICON in m or any(a in m for a in allow_http):
            continue
        problems.append("log error: " + m)
    if report["fallback_page_shown"]:
        problems.append("fallback 'The page could not be displayed.' shown")
    return problems


def expected_warnings(report: dict[str, Any]) -> list[str]:
    out = [f"favicon {e['status']}: {e['url']}" for e in report["http_errors"] if e["url"].endswith(FAVICON)]
    out += ["console.warn: " + m for m in report["console_warnings"]]
    out += ["log warning: " + m for m in report["log_warnings"]]
    return out


class Env:
    """Everything a scenario needs: the real page, a recorder, the base URL and the screenshot directory."""

    def __init__(self, page: cdp.Page, rec: Recorder, base: str, shots: Path, source: str = "real saved runs") -> None:
        self.page = page
        self.rec = rec
        self.base = base
        self.shots = shots
        self.source = source

    def api(self, path: str) -> Any:
        """GET a Viewer API path with fetch() inside Chrome (same origin), so the test process opens no socket."""
        page = self.page
        if page.eval("location.origin") != self.base:
            page.navigate(self.base + "/api/runs")
        r = page.eval(f"fetch({json.dumps(path)}).then(async r => ({{status: r.status, body: await r.text()}}))", await_promise=True)
        assert r["status"] == 200, (path, r["status"])
        return json.loads(r["body"])

    def visit(self, path: str, viewport: int = 1280, ready: str = READY) -> dict[str, Any]:
        self.page.set_viewport(viewport, 900)
        return self.page.navigate(self.base + path, ready)

    def shot(self, name: str, full_page: bool = True, element: str | None = None) -> str:
        if element:
            self.page.screenshot_element(self.shots / name, element)
        else:
            self.page.screenshot(self.shots / name, full_page)
        return name


def compare_facts(page: cdp.Page) -> dict[str, Any]:
    return page.eval(COMPARE_FACTS)


def rows_by_label(facts: dict[str, Any]) -> dict[str, list[str]]:
    return {row[0]: row[1:] for row in facts["rows"]}


def statement_texts(ids: dict[str, str], values: dict[str, tuple[Any, Any, Any]]) -> dict[str, str]:
    """The four statements, built from API values only: values[slot] = (exit_code, oom_killed, samples)."""
    a, b, c = values["A"], values["B"], values["C"]
    return {
        "S1": f"Experiment A differs from Experiment B and Experiment C by exit code (A={a[0]}, B={b[0]}, C={c[0]})",
        "S2": "Experiment B and Experiment C share ExitCode 137",
        "S3": f"OOMKilled distinguishes Experiment B ({str(b[1]).lower()}) from Experiment C ({str(c[1]).lower()})",
        "S4": f"Evidence survived all three deaths in the recorded runs (Samples: A={a[2]}, B={b[2]}, C={c[2]})",
    }


def api_values(env: Env, run_id: str) -> tuple[Any, Any, Any, Any, Any]:
    d = env.api("/api/runs/" + run_id)
    eff = d["docker_state"]["effective"]
    es = d["evidence_summary"]
    return eff["exit_code"], eff["oom_killed"], es["sample_count"], (es.get("highest_rss") or {}).get("rss_bytes"), eff["memory_limit_bytes"]


# ------------------------------------------------------------------ keyboard helpers


def tab_walk(page: cdp.Page, limit: int = 90, shift: bool = False) -> list[dict[str, Any]]:
    stops: list[dict[str, Any]] = []
    for _ in range(limit):
        page.key("Tab", shift=shift)
        d = page.eval(DESCRIBE_ACTIVE)
        if d is None or d["tag"] == "BODY" or d["tag"] == "HTML":
            stops.append({"tag": "BODY", "left_document": True})
            break
        if stops and d["idx"] == stops[0].get("idx"):
            stops.append({**d, "wrapped": True})
            break
        stops.append(d)
    return stops


def focus_top(page: cdp.Page) -> None:
    page.eval("(() => { if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur(); window.scrollTo(0, 0); })()")


def stop_label(s: dict[str, Any]) -> str:
    return f"{s['tag']}.{s['cls']}:{s['text'][:28]}" if "cls" in s else s["tag"]


# ------------------------------------------------------------------ scenarios: real runs (Phases 0, 3, 5)


def sc_static_fresh(env: Env) -> None:
    import hashlib
    static = Path(cdp.REPO) / "coroner" / "viewer" / "static"
    diffs = []
    names = sorted(p.name for p in static.iterdir() if p.is_file())
    for name in names:
        status, _, body = cdp.get(f"{env.base}/static/{name}")
        disk = (static / name).read_bytes()
        if status != 200 or hashlib.sha256(body).hexdigest() != hashlib.sha256(disk).hexdigest():
            diffs.append(f"{name}: HTTP {status}")
    env.rec.add("4.7-00", "Viewer serves the current static files (SHA-256 of each served file equals the file on disk)",
                "/static/*", f"{len(names)} files identical", f"{len(names) - len(diffs)} identical; differing: {diffs}",
                env.rec.expect(not diffs), extra={"files": names})


def sc_real_pages(env: Env) -> None:
    """Phase 3: HTTP status and rendered values of the five real routes."""
    rec = env.rec
    vals = {k: api_values(env, v) for k, v in REAL.items()}
    exp = {"A": (1, False, 15, 74326016, 134217728), "B": (137, False, 15, 74174464, 134217728), "C": (137, True, 45, 141090816, 134217728)}
    rec.add("4.7-R00", "Live API values equal the expected values of the task", "/api/runs/<A|B|C>",
            json.dumps(exp), json.dumps({k: list(v) for k, v in vals.items()}), rec.expect(
                all(tuple(vals[k]) == exp[k] for k in exp)), source=env.source, extra={"mib": {k: round(v[3] / BPM, 2) for k, v in vals.items()}})

    # overview
    resp = env.visit("/")
    text = env.page.eval("document.querySelector('.cards').innerText")
    cards = env.page.eval("Array.from(document.querySelectorAll('.cards > *')).map(c => c.innerText.replace(/\\s+/g,' '))")
    checks = []
    for key, card in zip("ABC", cards):
        e = vals[key]
        flat = re.sub(r"\s+", " ", card)
        checks.append(REAL[key] in flat and re.search(r"Docker ExitCode\s+%d\b" % e[0], flat, re.I) is not None
                      and re.search(r"OOMKilled\s+%s\b" % str(e[1]).lower(), flat, re.I) is not None
                      and re.search(r"Preserved Samples\s+%d\b" % e[2], flat, re.I) is not None)
    others = env.page.eval("document.querySelectorAll('.table tbody tr').length")
    rep = env.page.report()
    rec.add("4.7-R01", "Overview: three cards with the live ExitCode, OOMKilled, Samples and run id", "/",
            "HTTP 200; cards A,B,C match the API", f"HTTP {resp.get('status')}; cards={cards}; other-runs rows={others}",
            rec.expect(resp.get("status") == 200 and len(cards) == 3 and all(checks)), console_errors=len(unexpected(rep)), screenshot=env.shot("01-overview-desktop.png"),
            viewport="1280x900", source=env.source, warnings=expected_warnings(rep))

    # incident pages
    for key in "ABC":
        path = "/runs/" + REAL[key]
        resp = env.visit(path)
        d = env.api("/api" + path)
        facts = env.page.eval("Array.from(document.querySelectorAll('dl .fact')).map(f => [f.querySelector('dt').textContent, f.querySelector('dd').innerText.replace(/\\s+/g,' ')])")
        fm = {k: v for k, v in facts}
        table = env.page.eval(SAMPLE_ROWS_JS)
        peaks = [int(r[4]) for r in table["rows"]]
        e = vals[key]
        ok = (resp.get("status") == 200 and fm.get("Docker ExitCode", "").startswith(str(e[0]))
              and fm.get("Docker OOMKilled", "").startswith(str(e[1]).lower())
              and fm.get("Preserved Samples", "").strip() == str(e[2])
              and fm.get("Memory limit", "").startswith("128.00 MiB (134217728 bytes)")
              and len(peaks) == e[2] and max(peaks) == e[3])
        rep = env.page.report()
        shot = None
        if key in "BC":
            shot = env.shot(f"0{6 if key == 'B' else 7}-experiment-{key.lower()}-chart.png", element=".chart-section")
        rec.add(f"4.7-R0{2 + 'ABC'.index(key)}", f"Incident {key}: ExitCode, OOMKilled, Samples, limit and highest sampled RSS from the live page", path,
                f"HTTP 200; ExitCode {e[0]}, OOMKilled {str(e[1]).lower()}, Samples {e[2]}, peak {e[3]} B, limit 134217728 B",
                f"HTTP {resp.get('status')}; ExitCode={fm.get('Docker ExitCode')}; OOMKilled={fm.get('Docker OOMKilled')}; Samples={fm.get('Preserved Samples')}; limit={fm.get('Memory limit')}; table rows={len(peaks)}; max table RSS={max(peaks) if peaks else None}; peak above limit={max(peaks) > 134217728 if peaks else None}",
                rec.expect(ok), console_errors=len(unexpected(rep)), screenshot=shot, viewport="1280x900", source=env.source,
                warnings=expected_warnings(rep), extra={"api_chart_points": len(d["chart"]["points"])})

    # compare defaults
    resp = env.visit("/compare")
    f = compare_facts(env.page)
    rows = rows_by_label(f)
    st = statement_texts(REAL, {k: (vals[k][0], vals[k][1], vals[k][2]) for k in "ABC"})
    ok = (resp.get("status") == 200 and f["slotRuns"] == [REAL["A"], REAL["B"], REAL["C"]]
          and all(st[s] in f["statements"] for s in ("S1", "S2", "S3", "S4")) and len(f["statements"]) == 4
          and f["headline"] == HEADLINE and f["omitted"] == []
          and rows["Docker ExitCode"] == ["1", "137", "137"] and rows["Docker OOMKilled"] == ["false", "false", "true"]
          and rows["Preserved Samples"] == ["15", "15", "45"]
          and rows["Highest sampled RSS"] == [f"{round(vals[k][3] / BPM, 2):.2f} MiB ({vals[k][3]} bytes)" for k in "ABC"]
          and all(v == "128.00 MiB (134217728 bytes)" for v in rows["Memory limit"]))
    rep = env.page.report()
    env.shot("02-compare-desktop.png")
    env.page.eval("document.querySelector('.compare-facts').scrollIntoView()")
    env.shot("03-compare-values.png", full_page=False)
    env.page.eval("document.querySelector('.compare-statements').scrollIntoView()")
    env.shot("04-compare-statements.png", full_page=False)
    rec.add("4.7-01", "Default /compare: slots A,B,C, values, all four statements and the headline", "/compare",
            f"HTTP 200; slots {list(REAL.values())}; four statements; headline visible",
            f"HTTP {resp.get('status')}; slots={f['slotRuns']}; statements={f['statements']}; headline={f['headline']!r}; omitted={f['omitted']}; rows={json.dumps({k: rows[k] for k in ('Docker ExitCode', 'Docker OOMKilled', 'Highest sampled RSS', 'Preserved Samples', 'Memory limit')})}",
            rec.expect(ok), console_errors=len(unexpected(rep)), screenshot="02-compare-desktop.png; 03-compare-values.png; 04-compare-statements.png",
            viewport="1280x900", source=env.source, warnings=expected_warnings(rep))
    c_above = vals["C"][3] > vals["C"][4]
    b_distinct = (vals["B"][0], vals["B"][1]) == (137, False) and (vals["C"][0], vals["C"][1]) == (137, True)
    rec.add("4.7-R05", "C's RSS peak is above its limit; B and C are distinct although both exit 137", "/compare",
            "C peak 141090816 > 134217728; B (137,false) vs C (137,true)",
            f"C peak={vals['C'][3]} limit={vals['C'][4]}; B oom={rows['Docker OOMKilled'][1]} C oom={rows['Docker OOMKilled'][2]}; rendered peak C={rows['Highest sampled RSS'][2]}",
            rec.expect(c_above and b_distinct and rows["Docker OOMKilled"][1:] == ["false", "true"]), console_errors=0,
            viewport="1280x900", source=env.source)


def sc_select_run(env: Env) -> None:
    """4.7-02: change a slot through the real UI (summary click, then link click)."""
    rec = env.rec
    page = env.page
    for slot, alt in (("A", ALT["A"]), ("B", ALT["B"]), ("C", ALT["C"])):
        env.visit("/compare")
        page.click(f".compare-slots article:nth-of-type({'ABC'.index(slot) + 1}) .chooser summary")
        opened = page.eval(f"document.querySelectorAll('.compare-slots article')[{'ABC'.index(slot)}].querySelector('details.chooser').open")
        link_sel = f".compare-slots article:nth-of-type({'ABC'.index(slot) + 1}) .chooser a[href*='{alt}']"
        page.capture = page._fresh_capture()
        page.click(link_sel)
        page.wait_for(f"location.search.includes('{alt}')", 20, "navigation by the chooser link")
        page.wait_for("document.readyState === 'complete' && " + READY, 20, "re-render")
        f = compare_facts(page)
        vals = {k: api_values(env, (alt if k == slot else REAL[k])) for k in "ABC"}
        ids = {k: (alt if k == slot else REAL[k]) for k in "ABC"}
        st = statement_texts(ids, {k: vals[k][:3] for k in "ABC"})
        expect_statements = [st["S1"], st["S2"], st["S3"], st["S4"]]
        rows = rows_by_label(f)
        idx = "ABC".index(slot)
        ok = (opened is True and f["slotRuns"] == [ids["A"], ids["B"], ids["C"]]
              and rows["Docker ExitCode"][idx] == str(vals[slot][0]) and rows["Preserved Samples"][idx] == str(vals[slot][2])
              and rows["Highest sampled RSS"][idx] == f"{vals[slot][3] / BPM:.2f} MiB ({vals[slot][3]} bytes)"
              and f["statements"] == expect_statements and f["headline"] == HEADLINE
              and "chosen in the address" in f["mainText"])
        rep = page.report()
        shot = env.shot(f"13-compare-chose-{slot.lower()}.png")
        rec.add(f"4.7-02{slot.lower()}", f"Change slot {slot} to another saved run through the chooser (real clicks)", "/compare -> /compare?" + slot.lower() + "=<run>",
                f"slot {slot} = {alt}; values and the four statements recomputed from that run's API response",
                f"slots={f['slotRuns']}; statements={f['statements']}; S4 sample counts from API={[vals[k][2] for k in 'ABC']}; headline={f['headline']!r}",
                rec.expect(ok), console_errors=len(unexpected(rep)), screenshot=shot, viewport="1280x900", source=env.source,
                warnings=expected_warnings(rep))


def sc_compare_errors(env: Env) -> None:
    rec = env.rec
    page = env.page
    vals = {k: api_values(env, v) for k, v in REAL.items()}
    # 4.7-03 wrong type
    path = f"/compare?a={REAL['B']}&b={REAL['B']}&c={REAL['C']}"
    resp = env.visit(path)
    f = compare_facts(page)
    rows = rows_by_label(f)
    joined = " | ".join(f["statements"])
    omitted = " | ".join(f["omitted"])
    ok = (resp.get("status") == 200 and len(f["warnings"]) == 1 and REAL["B"] in f["warnings"][0] and "Experiment B" in f["warnings"][0]
          and "expects Experiment A" in f["warnings"][0] and rows["Experiment type (recorded)"][0] == "B"
          and "differs from" not in joined and "Evidence survived" not in joined and "A=" not in joined
          and "slot A" in omitted and "expected Experiment A" in omitted
          and "Experiment B and Experiment C share ExitCode 137" in f["statements"])
    rep = page.report()
    rec.add("4.7-03", "Wrong experiment type: a=<B run> is excluded from slot A with a warning", path,
            "slot A warning names run, actual type B, expected A; S1 and S4 omitted; no statement uses A",
            f"HTTP {resp.get('status')}; warning={f['warnings']}; statements={f['statements']}; omitted={f['omitted']}; slot A recorded type={rows['Experiment type (recorded)'][0]}; headline={f['headline']!r}",
            rec.expect(ok), console_errors=len(unexpected(rep)), screenshot=env.shot("10-wrong-slot-warning.png"), viewport="1280x900", source=env.source,
            warnings=expected_warnings(rep))

    # 4.7-04 unavailable
    missing = "a-20261120T211146Z-deadbeef"
    path = f"/compare?a={missing}"
    resp = env.visit(path)
    f = compare_facts(page)
    rows = rows_by_label(f)
    joined = " | ".join(f["statements"])
    ok = (resp.get("status") == 200 and any(missing in s and "no saved run found" in s for s in f["slotStates"])
          and all(v == "no run" for v in [rows[k][0] for k in rows if k != "Recorded value"])
          and "differs from" not in joined and "Evidence survived" not in joined
          and any("slot A: no saved run found for run id " + missing in o for o in f["omitted"])
          and "Experiment B and Experiment C share ExitCode 137" in f["statements"] and REAL["A"] not in f["mainText"])
    rep = page.report()
    rec.add("4.7-04", "Syntactically valid but nonexistent run id: HTTP 200, slot unavailable, nothing borrowed", path,
            "HTTP 200; slot A unavailable naming the id; every slot-A cell 'no run'; S1 and S4 omitted",
            f"HTTP {resp.get('status')}; slotStates={f['slotStates']}; slot A column={[rows[k][0] for k in rows]}; statements={f['statements']}; omitted={f['omitted']}",
            rec.expect(ok), console_errors=len(unexpected(rep, allow_http=("/api/runs/" + missing,))), screenshot=env.shot("11-unavailable-slot.png"),
            viewport="1280x900", source=env.source, warnings=expected_warnings(rep) + [f"expected HTTP 404 for /api/runs/{missing}: {[e for e in rep['http_errors'] if 'deadbeef' in e['url']]}"])

    # 4.7-05 invalid queries: raw HTTP, the page's own fetch, and the Chrome-rendered page
    queries = ["a=..", "x=1", "a=", f"a={REAL['A']}&a={REAL['A']}"]
    for i, q in enumerate(queries, 1):
        url = f"{env.base}/compare?{q}"
        status, headers, body = cdp.get(url)
        resp = env.visit(f"/compare?{q}", ready="document.readyState === 'complete'")
        rendered = page.eval("document.body.innerText")
        fetched = page.eval(f"fetch({json.dumps('/compare?' + q)}).then(async r => ({{status: r.status, type: r.headers.get('content-type'), body: await r.text()}}))", await_promise=True)
        shot = env.shot("12-invalid-query.png") if i == 1 else None
        rep = page.report()
        ok = (status == 400 and headers.get("content-type", "").startswith("text/plain") and body == b"Invalid compare parameter.\n"
              and resp.get("status") == 400 and resp.get("mime") == "text/plain" and rendered.strip() == "Invalid compare parameter."
              and fetched["status"] == 400 and fetched["type"].startswith("text/plain") and fetched["body"] == "Invalid compare parameter.\n")
        rec.add(f"4.7-05.{i}", f"Invalid compare query /compare?{q}", f"/compare?{q}",
                "HTTP 400, Content-Type text/plain, body = the message 'Invalid compare parameter.' plus one line feed (the form pinned by test_compare_query.py FIXED_400)",
                f"HTTP GET: {status} {headers.get('content-type')} {body!r}; Chrome navigation: {resp.get('status')} {resp.get('mime')}; Chrome in-page fetch: {fetched}; Chrome rendered text={rendered!r}",
                rec.expect(ok), console_errors=len(unexpected(rep, allow_http=("/compare?",))), screenshot=shot, viewport="1280x900", source=env.source,
                warnings=expected_warnings(rep) + [f"expected browser log for the 400: {rep['log_errors']}"])


# ------------------------------------------------------------------ scenarios: console and CSP (Phase 5)


def sc_console_csp(env: Env) -> None:
    rec = env.rec
    page = env.page
    targets = [("/", "overview"), ("/runs/" + REAL["A"], "incident A"), ("/runs/" + REAL["B"], "incident B"),
               ("/runs/" + REAL["C"], "incident C"), ("/compare", "compare")]
    for path, name in targets:
        resp = env.visit(path)
        before = page.report()
        # interact: keyboard walk, open every details, open the table, use the chart, click the nav link
        tab_walk(page, 40)
        page.eval("document.querySelectorAll('details').forEach(d => { d.open = true; })")
        if page.eval("!!document.querySelector('.chart-plot')"):
            page.eval("document.querySelector('.chart-plot').focus()")
            for k in ("ArrowRight", "ArrowRight", "End", "Home"):
                page.key(k)
        if page.eval("!!document.querySelector('.sample-table button:not([hidden])')"):
            page.click(".sample-table button")
        after = page.report()
        problems = unexpected(after)
        rec.add(f"4.7-CSP.{name.replace(' ', '-')}", f"Console, exceptions, CSP and local requests: {name} (before and after interaction)", path,
                "0 console errors, 0 uncaught exceptions, 0 CSP violations, 0 failed or HTTP-error local requests, no fallback page",
                f"HTTP {resp.get('status')}; before: {unexpected(before)}; after interaction: {problems}; expected warnings: {expected_warnings(after)}; responses={len(after.get('http_errors', []))} HTTP errors",
                rec.expect(resp.get("status") == 200 and not problems), console_errors=len(problems), viewport="1280x900", source=env.source,
                warnings=expected_warnings(after), extra={"before": before, "after": after})


def sc_capture_controls(env: Env) -> None:
    """Positive controls: the capture machinery does record a CSP violation, a console error and an exception."""
    page = env.page
    env.visit("/compare")
    page.eval("(() => { const s = document.createElement('script'); s.textContent = 'window.__ran = true'; document.body.append(s); })()")
    page.eval("console.error('control-error'); setTimeout(() => { throw new Error('control-exception'); }, 0); true")
    page.wait_for("window.__csp.length > 0", 5, "control CSP violation")
    deadline = time.time() + 5
    while time.time() < deadline and not page.capture["exceptions"]:
        page.pump(0.05)
    rep = page.report()
    ran = page.eval("window.__ran === true")
    ok = len(rep["csp_violations"]) >= 1 and "control-error" in " ".join(rep["console_errors"]) and any("control-exception" in e for e in rep["uncaught_exceptions"]) and not ran
    env.rec.add("4.7-CSP.control", "Capture controls: a deliberate inline script is blocked and reported as a CSP violation, a console error and an uncaught exception are captured", "/compare",
                "violation, console.error and exception are recorded; the inline script did not run",
                f"csp={rep['csp_violations']}; console_errors={rep['console_errors']}; exceptions={[e[:60] for e in rep['uncaught_exceptions']]}; inline script ran={ran}",
                env.rec.expect(ok), console_errors=0, viewport="1280x900", source=env.source,
                warnings=["control check: the errors counted here are deliberate and are not page defects"])

# ------------------------------------------------------------------ scenarios: keyboard (Phase 6)


def sc_tab_order(env: Env) -> None:
    rec = env.rec
    page = env.page
    for path, name, must_contain in [
        ("/", "overview", ["Coroner", "Overview", "Compare", "Open incident detail"]),
        ("/runs/" + REAL["B"], "incident B", ["Coroner", "Overview", "Compare", "Back to overview"]),
        ("/compare", "compare", ["Coroner", "Overview", "Compare", "Choose another run for slot", "Open incident detail"]),
    ]:
        env.visit(path)
        focus_top(page)
        fwd = tab_walk(page, 90)
        rev = tab_walk(page, 90, shift=True)
        labels = [stop_label(s) for s in fwd]
        real = [s for s in fwd if "cls" in s]
        distinct = len({s["idx"] for s in real if not s.get("wrapped")}) == len([s for s in real if not s.get("wrapped")])
        terminated = bool(fwd) and (fwd[-1].get("left_document") or fwd[-1].get("wrapped"))
        first_is_skip = bool(real) and real[0]["cls"] == "skip-link"
        reachable = all(any(m in l for l in labels) for m in must_contain)
        visible_focus = all(s["outlineStyle"] != "none" and float(s["outlineWidth"][:-2]) >= 2 and s["inViewport"] for s in real)
        idxs = [s["idx"] for s in real if not s.get("wrapped")]
        document_order = idxs == sorted(idxs)
        rev_real = [s for s in rev if "cls" in s and not s.get("wrapped")]
        rev_back = [s["idx"] for s in rev_real]
        reverse_ok = len(rev_back) > 0 and rev_back[: len(idxs)] == list(reversed(idxs))[: len(rev_back)]
        if path == "/runs/" + REAL["B"]:
            page.eval("window.scrollTo(0,0)")
        ok = terminated and distinct and first_is_skip and reachable and visible_focus and document_order and reverse_ok
        rec.add(f"4.7-09.{name.replace(' ', '-')}", f"Tab and Shift+Tab order, visible focus, no trap: {name}", path,
                "skip link first; nav links and page links reachable in document order; every stop has a visible outline >= 2px; the walk ends; Shift+Tab walks back in reverse order",
                f"{len(real)} stops; first={labels[:1]}; order={labels}; terminated={terminated} distinct={distinct} document_order={document_order} reverse_ok={reverse_ok}; outlines={sorted({(s['outlineStyle'], s['outlineWidth'], s['outlineColor']) for s in real})}",
                rec.expect(ok), console_errors=len(unexpected(page.report())), viewport="1280x900", source=env.source,
                extra={"forward": fwd, "reverse": rev})

    # skip link
    env.visit("/")
    focus_top(page)
    page.key("Tab")
    d = page.eval(DESCRIBE_ACTIVE)
    shot = env.shot("08-keyboard-focus.png", full_page=False)
    page.key("Enter")
    page.wait_for("location.hash === '#main'", 5, "skip link hash")
    after = page.eval("({hash: location.hash, active: document.activeElement.id, tag: document.activeElement.tagName})")
    ok = d["cls"] == "skip-link" and d["inViewport"] and d["rect"][1] >= 0 and after["hash"] == "#main" and after["active"] == "main"
    rec.add("4.7-09.skip", "Skip link: appears on first Tab, Enter moves focus to <main>", "/",
            "skip link focused, on screen (top >= 0), Enter -> #main and focus on main",
            f"focused={d['text']!r} rect={d['rect']} outline={d['outlineStyle']} {d['outlineWidth']} {d['outlineColor']}; after Enter {after}",
            rec.expect(ok), console_errors=len(unexpected(page.report())), screenshot=shot, viewport="1280x900", source=env.source)


def point_matches(lines: list[str], p: dict[str, Any]) -> bool:
    joined = " | ".join(lines)
    return (re.search(r"\bseq %d\b|\b%d\b" % (p["seq"], p["seq"]), lines[0]) is not None
            and p["capture_time_display"] in joined and f"{p['rss_bytes'] / BPM:.2f}" in joined and str(p["rss_bytes"]) in joined)


def sc_chart_keyboard(env: Env) -> None:
    rec = env.rec
    page = env.page
    for key in "BC":
        path = "/runs/" + REAL[key]
        env.visit(path)
        pts = env.api("/api" + path)["chart"]["points"]
        focus_top(page)
        presses = 0
        for _ in range(80):
            page.key("Tab")
            presses += 1
            if page.eval("document.activeElement.classList.contains('chart-plot')"):
                break
        on_plot = page.eval("document.activeElement.classList.contains('chart-plot')")
        initial = page.eval(READOUT_JS)
        steps: list[dict[str, Any]] = []

        def step(name: str, expect_index: int) -> None:
            if name != "focus":
                page.key(name)
            r = page.eval(READOUT_JS)
            ok = point_matches(r["lines"], pts[expect_index]) and r["activePointClass"] == f"chart-pt-{expect_index}"
            steps.append({"key": name, "expected_index": expect_index, "seq": pts[expect_index]["seq"], "lines": r["lines"], "ok": ok})

        step("focus", 0)
        step("ArrowRight", 1)
        step("ArrowRight", 2)
        step("ArrowLeft", 1)
        step("End", len(pts) - 1)
        step("ArrowRight", len(pts) - 1)
        step("Home", 0)
        step("ArrowLeft", 0)
        mid = steps[1]["lines"]
        page.key("Tab")
        leave = page.eval(DESCRIBE_ACTIVE)
        page.key("Tab", shift=True)
        back = page.eval(DESCRIBE_ACTIVE)
        one_stop = not leave["insideChart"] and leave["tag"] != "svg" and "chart-plot" in back["cls"]
        stops_in_chart = page.eval("document.querySelectorAll('.chart-plot [tabindex]:not([tabindex=\"-1\"]), .chart-plot a, .chart-plot button').length")
        ok = on_plot and all(s["ok"] for s in steps) and one_stop and stops_in_chart == 0
        shot = None
        if key == "B":
            page.key("End")
            page.eval("document.querySelector('.chart-plot').scrollIntoView({block:'center'})")
            shot = env.shot("08b-chart-keyboard-end-b.png", full_page=False)
        rec.add(f"4.7-10.{key}", f"RSS chart keyboard on Experiment {key}: one tab stop; Right, Left, Home, End; readout matches the API", path,
                "chart-plot focused by Tab; Right/Left move one Sample; Home first, End last; readout seq, time and RSS equal the API Sample; Tab leaves the chart (one stop)",
                f"Tab presses to reach chart={presses}; steps={[(s['key'], s['seq'], s['ok']) for s in steps]}; sample1 lines={mid}; Tab leaves to {stop_label(leave)}; Shift+Tab returns to {stop_label(back)}; extra tab stops inside chart={stops_in_chart}",
                rec.expect(ok), console_errors=len(unexpected(page.report())), screenshot=shot, viewport="1280x900", source=env.source,
                extra={"initial": initial, "steps": steps})


def sc_sample_table(env: Env) -> None:
    rec = env.rec
    page = env.page
    for key in "BC":
        path = "/runs/" + REAL[key]
        env.visit(path)
        pts = env.api("/api" + path)["chart"]["points"]
        focus_top(page)
        for _ in range(80):
            page.key("Tab")
            if page.eval("document.activeElement.tagName === 'SUMMARY' && !!document.activeElement.closest('details.sample-table')"):
                break
        on_summary = page.eval("document.activeElement.tagName === 'SUMMARY' && !!document.activeElement.closest('details.sample-table')")
        d = page.eval(DESCRIBE_ACTIVE)
        closed = page.eval(SAMPLE_ROWS_JS)["firstRowVisible"]
        page.key("Enter")
        page.wait_for("document.querySelector('details.sample-table').open === true", 5, "table open")
        t = page.eval(SAMPLE_ROWS_JS)
        values_ok = len(t["rows"]) == len(pts) and all(
            r[0] == str(p["seq"]) and r[4] == str(p["rss_bytes"]) and r[3] == f"{p['rss_bytes'] / BPM:.2f}" and r[2] == p["capture_time"]
            for r, p in zip(t["rows"], pts))
        shot = env.shot("09-expanded-sample-table.png", element="details.sample-table") if key == "B" else None
        if key == "B":
            page.eval("document.querySelector('details.sample-table').scrollIntoView()")
            env.shot("09b-expanded-sample-table-viewport.png", full_page=False)
        page.key(" ")
        page.wait_for("document.querySelector('details.sample-table').open === false", 5, "table closed by Space")
        after_close = page.eval(SAMPLE_ROWS_JS)
        ok = (on_summary and not closed and t["open"] and t["firstRowVisible"] and values_ok and t["moreHidden"] is True
              and after_close["open"] is False and not after_close["firstRowVisible"] and d["outlineStyle"] != "none")
        rec.add(f"4.7-11.{key}", f"Sample table opens from its summary with Enter, rows equal the API, Space closes it ({key})", path,
                "summary focused by keyboard; Enter opens; rows visible; seq/time/RSS equal API; Space closes",
                f"summary focused={on_summary} outline={d['outlineStyle']} {d['outlineWidth']}; rows visible when closed={closed}; after Enter open={t['open']} rows={len(t['rows'])}/{len(pts)} visible={t['firstRowVisible']} values match API={values_ok}; status={t['status']!r}; Show more hidden={t['moreHidden']}; after Space open={after_close['open']}",
                rec.expect(ok), console_errors=len(unexpected(page.report())), screenshot=shot, viewport="1280x900", source=env.source)


def sc_tick_labels(env: Env) -> None:
    rec = env.rec
    page = env.page
    path = "/runs/" + REAL["B"]
    env.visit(path)
    api = env.api("/api" + path)["chart"]
    labels = page.eval("Array.from(document.querySelectorAll('.chart-x-tick-label')).map(t => t.textContent)")
    ticks = api["x_axis"]["ticks"]
    expect = [re.search(r"T(\d\d:\d\d:\d\d)", t["label"]).group(1) for t in ticks]
    marks = page.eval("Array.from(document.querySelectorAll('line.chart-axis')).filter(l => Number(l.getAttribute('y2')) - Number(l.getAttribute('y1')) === 5).map(l => Number(l.getAttribute('x1')))")
    text_x = page.eval("Array.from(document.querySelectorAll('.chart-x-tick-label')).map(t => Number(t.getAttribute('x')))")
    ok = labels == expect and marks == text_x
    rec.add("4.7-12", "Experiment B x tick labels are the times of the tick positions (clock part of the API tick label), at the tick mark x", path,
            f"labels {expect}; label x equals tick-mark x", f"labels={labels}; tick marks x={marks}; label x={text_x}", rec.expect(ok),
            console_errors=len(unexpected(page.report())), viewport="1280x900", source=env.source)


# ------------------------------------------------------------------ scenarios: responsive and accessibility (Phase 7)


def sc_responsive(env: Env) -> None:
    rec = env.rec
    page = env.page
    small_all: dict[str, set[str]] = {}
    pages = [("/", "overview"), ("/runs/" + REAL["B"], "incident-B"), ("/runs/" + REAL["C"], "incident-C"), ("/compare", "compare")]
    for width in (1280, 480, 360):
        for path, name in pages:
            env.visit(path, viewport=width)
            m = page.eval(LAYOUT_JS)
            extra: dict[str, Any] = {"layout": m}
            sig_ok = True
            if name == "incident-B":
                s = page.eval("""(() => { const p = document.querySelector('.chart-plot'); p.scrollLeft = 0; const before = p.scrollLeft;
                    p.scrollLeft = p.scrollWidth; const after = p.scrollLeft; const pr = p.getBoundingClientRect();
                    const k = document.querySelector('.chart-sigkill').getBoundingClientRect();
                    const svg = p.querySelector('svg').getBoundingClientRect();
                    p.scrollLeft = 0; const k0 = document.querySelector('.chart-sigkill').getBoundingClientRect();
                    return {scrollW: p.scrollWidth, clientW: p.clientWidth, scrolledTo: after, svgW: svg.width,
                      sigkillAtEndVisible: k.left >= pr.left - 1 && k.right <= pr.right + 1,
                      sigkillAtStartVisible: k0.left >= pr.left - 1 && k0.right <= pr.right + 1}; })()""")
                extra["chart_scroll"] = s
                sig_ok = s["sigkillAtEndVisible"] is True and (width == 1280 or (s["scrolledTo"] > 0 and s["svgW"] > s["clientW"]))
                if width == 1280:
                    sig_ok = s["sigkillAtEndVisible"] is True or s["sigkillAtStartVisible"] is True
            if name == "incident-C":
                s = page.eval("""(() => { const p = document.querySelector('.chart-plot'); p.scrollLeft = p.scrollWidth;
                    const r = p.getBoundingClientRect(); const pts = Array.from(document.querySelectorAll('.chart-point'));
                    const last = pts[pts.length - 1].getBoundingClientRect(); const out = {scrollW: p.scrollWidth, clientW: p.clientWidth, scrolledTo: p.scrollLeft,
                      lastPointVisibleAtEnd: last.left >= r.left - 1 && last.right <= r.right + 1}; p.scrollLeft = 0; return out; })()""")
                extra["chart_scroll"] = s
                sig_ok = s["lastPointVisibleAtEnd"] is True and (width == 1280 or s["scrolledTo"] > 0)
            wraps_ok = True
            if name == "compare" and width < 900:
                wraps_ok = bool(m["tableWraps"]) and m["tableWraps"][0]["scrollable"] and m["cardColumns"] == 1
            if name == "compare" and width == 1280:
                wraps_ok = m["cardColumns"] == 3
            ok = (not m["pageHorizontalOverflow"] and not m["outside"] and not m["clipped"] and m["overlapCount"] == 0 and sig_ok and wraps_ok)
            shot = None
            if name == "compare" and width == 480:
                shot = env.shot("05-compare-narrow.png")
            elif width != 1280:
                shot = env.shot(f"14-{name.lower()}-{width}.png", full_page=False)
            rec.add(f"4.7-RSP.{name}.{width}", f"Responsive layout {name} at {width} px (CDP Emulation.setDeviceMetricsOverride)", path,
                    "no page-wide horizontal overflow; nothing outside the viewport except inside scroll containers; no clipped or overlapping text; wide chart/table scrolls",
                    f"clientWidth={m['clientWidth']} docScrollWidth={m['docScrollWidth']} overflow={m['pageHorizontalOverflow']}; outside={m['outside'][:3]}; clipped={m['clipped'][:3]}; overlaps={m['overlapCount']} {m['overlaps'][:2]}; tableWraps={m['tableWraps']}; plot={m['plot']}; columns={m['cardColumns']}; chart_scroll={extra.get('chart_scroll')}; targets<44px={m['smallTargets'][:4]}",
                    rec.expect(ok), console_errors=len(unexpected(page.report())), screenshot=shot, viewport=f"{width}x900", source=env.source, extra=extra,
                    warnings=[])
            small_all.setdefault(f"{name} @{width}", set()).update(m["smallTargets"])
    count = {k: len(v) for k, v in small_all.items() if v}
    sample = sorted({t for v in small_all.values() for t in v})[:6]
    rec.add("4.7-20", "Interactive targets are at least 44 px high (design: Visual system, Layout) on overview, B, C and compare at 1280, 480 and 360 px",
            "/, /runs/<B>, /runs/<C>, /compare", "no visible button, summary, nav link or stand-alone link shorter than 44 px",
            f"pages with shorter targets (count of distinct elements): {count}; examples: {sample}",
            rec.expect(not count), console_errors=0, viewport="1280, 480, 360", source=env.source, extra={"short_targets": {k: sorted(v) for k, v in small_all.items() if v}})


def sc_motion_contrast_ax(env: Env) -> None:
    rec = env.rec
    page = env.page
    env.visit("/")
    probe = "(() => { const a = document.querySelector('a.button, a.text-link'); const b = Array.from(document.querySelectorAll('a')).find(x => x.classList.contains('nav-link')); " \
            "const cs = getComputedStyle(b); return {reduce: matchMedia('(prefers-reduced-motion: reduce)').matches, duration: cs.transitionDuration, property: cs.transitionProperty}; })()"
    normal = page.eval(probe)
    page.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
    reduced = page.eval(probe)
    btn = env.visit("/runs/" + REAL["B"])
    reduced_btn = page.eval("(() => { const b = document.querySelector('.sample-table button'); const cs = getComputedStyle(b); return {d: cs.transitionDuration, p: cs.transitionProperty}; })()")
    page.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "no-preference"}]})
    normal_btn = page.eval("(() => { const b = document.querySelector('.sample-table button'); const cs = getComputedStyle(b); return {d: cs.transitionDuration, p: cs.transitionProperty}; })()")
    page.call("Emulation.setEmulatedMedia", {"features": []})
    ok = (normal["reduce"] is False and normal["duration"].startswith("0.12s") and reduced["reduce"] is True
          and reduced["duration"].startswith("0s") and reduced_btn["d"].startswith("0s") and normal_btn["d"].startswith("0.12s"))
    rec.add("4.7-13", "prefers-reduced-motion: reduce removes the 120 ms transition (Emulation.setEmulatedMedia)", "/ and /runs/<B>",
            "no-preference: 0.12s colour transition; reduce: none / 0s",
            f"no-preference nav link={normal}; reduce nav link={reduced}; reduce button={reduced_btn}; no-preference button={normal_btn}",
            rec.expect(bool(ok)), console_errors=0, viewport="1280x900", source=env.source)

    worst = []
    tokens: dict[str, Any] = {}
    for path, name in [("/", "overview"), ("/runs/" + REAL["B"], "incident-B"), ("/runs/" + REAL["C"], "incident-C"), ("/compare", "compare")]:
        env.visit(path)
        page.eval("document.querySelectorAll('details').forEach(d => { d.open = true; })")
        c = page.eval(CONTRAST_JS)
        tokens = c["tokens"]
        worst.append((name, c["min"], c["below45"], c["count"], c["lowest"][:2]))
    design = {"--text": [15.0, 13.7, 12.1], "--muted": [8.1, 7.4, 6.5], "--amber": [9.6, 8.8, 7.8], "--red": [7.2, 6.6, 5.8], "--purple": [8.7, 7.9, 7.0]}
    diffs = {k: [round(a - b, 2) for a, b in zip(tokens[k], design[k])] for k in design}
    ok = all(w[2] == 0 for w in worst) and all(min(tokens[k]) >= 4.5 for k in design)
    rec.add("4.7-14", "Contrast from computed styles (WCAG 2.x relative luminance, alpha-composited over the real backdrop)", "/, /runs/<B>, /runs/<C>, /compare",
            "every rendered text >= 4.5:1; design tokens >= 4.5:1 on bg, surface, surface-2",
            f"per page (name, min ratio, texts below 4.5, texts measured, two lowest)={worst}; token ratios on bg/surface/surface-2={tokens}; difference from the design table={diffs}",
            rec.expect(ok), console_errors=0, viewport="1280x900", source=env.source,
            extra={"method": "ratio=(L1+0.05)/(L2+0.05); L=0.2126R+0.7152G+0.0722B with sRGB linearisation; fg from getComputedStyle color (fill for SVG text) composited over the stacked opaque ancestor backgrounds", "pages": worst, "tokens": tokens})

    env.visit("/runs/" + REAL["B"])
    tree = page.call("Accessibility.getFullAXTree")["nodes"]
    roles: dict[str, int] = {}
    for n in tree:
        r = (n.get("role") or {}).get("value", "?")
        roles[r] = roles.get(r, 0) + 1
    app = [n for n in tree if (n.get("role") or {}).get("value") == "application"]
    headings = [(n.get("name") or {}).get("value") for n in tree if (n.get("role") or {}).get("value") == "heading"]
    rec.add("4.7-15", "Chrome accessibility tree (Accessibility.getFullAXTree) for the B incident. NOT a screen reader test", "/runs/<B>",
            "recorded for information", f"{len(tree)} AX nodes; roles={dict(sorted(roles.items(), key=lambda kv: -kv[1])[:12])}; application nodes={[(a.get('name') or {}).get('value') for a in app]}; headings={headings}",
            "PASS" if app and headings else "FAIL", console_errors=0, viewport="1280x900", source=env.source,
            warnings=["information only: not equivalent to testing with a screen reader"])


def sc_not_tested(env: Env) -> None:
    rec = env.rec
    rec.add("4.7-16", "200% browser zoom", "all pages", "real browser zoom at 200%", "not performed: DevTools Protocol cannot set the browser zoom level of a page, and "
            "Emulation.setPageScaleFactor, a resize or a device scale factor change are not browser zoom, so none was substituted", "NOT TESTED", viewport="n/a", source=env.source)
    rec.add("4.7-17", "Screen reader check", "all pages", "performed by a person with a screen reader", "not performed; no screen reader was run", "NOT TESTED", source=env.source)
    rec.add("4.7-18", "Human-operated interactive Chrome check (headed, by a person)", "all pages", "performed by a person", "not performed; all checks here are automated headless Chrome", "NOT TESTED", source=env.source)
    if not getattr(env, "rerun", False):
        rec.add("4.7-19", "Partial_View fixture shows dashed segments and legend entry", "/runs/<partial fixture>", "dashed segment and legend entry",
                "not performed in this run: needs a fixture with more Samples than the chart cap (5000); not in the requested scope", "NOT TESTED", source="synthetic")


# ------------------------------------------------------------------ synthetic fixtures (Phases 4.7-06, 07, 08)

ID_A = "a-20261120T211146Z-00000001"
ID_B = "b-20261120T211311Z-00000002"
ID_C = "c-20261120T211433Z-00000003"
EARLY = "2026-11-19T10:00:00.000Z"


def build_compare_fixture(root: Path) -> dict[str, str]:
    b = RunsBuilder(root)
    ids = {
        "A": b.add_run("A", samples=sample_specs(5), run_id=ID_A),
        "B": b.add_run("B", samples=sample_specs(5), run_id=ID_B),
        "C": b.add_run("C", samples=sample_specs(5), run_id=ID_C),
        # B and C both ExitCode 137 and OOMKilled false, no discrepancy
        "C_equal": b.add_run("C", samples=sample_specs(5), run_id="c-20261119T100000Z-00000004", start_time=EARLY,
                             container_state=StateSpec(exit_code=137, oom_killed=False)),
        # ExitCode discrepancy: manifest 137, raw inspect 1 (slot C)
        "C_exit_disc": b.add_run("C", samples=sample_specs(5), run_id="c-20261119T100000Z-00000005", start_time=EARLY,
                                 container_state=StateSpec(exit_code=137, oom_killed=True), inspect=InspectSpec(exit_code=1, oom_killed=True)),
        # OOMKilled discrepancy: manifest true, raw inspect false (slot C)
        "C_oom_disc": b.add_run("C", samples=sample_specs(5), run_id="c-20261119T100000Z-00000006", start_time=EARLY,
                                container_state=StateSpec(exit_code=137, oom_killed=True), inspect=InspectSpec(exit_code=137, oom_killed=False)),
        # unrelated memory-limit discrepancy: only the limit differs (slot C)
        "C_mem_disc": b.add_run("C", samples=sample_specs(5), run_id="c-20261119T100000Z-00000007", start_time=EARLY,
                                container_state=StateSpec(exit_code=137, oom_killed=True, memory=134_217_728),
                                inspect=InspectSpec(exit_code=137, oom_killed=True, memory=268_435_456)),
        # B ExitCode discrepancy
        "B_exit_disc": b.add_run("B", samples=sample_specs(5), run_id="b-20261119T100000Z-00000008", start_time=EARLY,
                                 container_state=StateSpec(exit_code=137, oom_killed=False), inspect=InspectSpec(exit_code=1, oom_killed=False)),
    }
    return ids


def sc_synthetic_compare(env: Env, ids: dict[str, str]) -> None:
    rec = env.rec
    page = env.page
    src = "synthetic fixture (scratch Runs_Directory outside the repository)"

    # 4.7-06 equal OOMKilled
    path = f"/compare?c={ids['C_equal']}"
    env.visit(path)
    f = compare_facts(page)
    rows = rows_by_label(f)
    joined = " | ".join(f["statements"])
    ok = (f["headline"] is None and HEADLINE not in f["mainText"] and "OOMKilled distinguishes" not in f["mainText"]
          and rows["Docker OOMKilled"][1:] == ["false", "false"] and rows["Docker ExitCode"][1:] == ["137", "137"]
          and "share ExitCode 137" in joined and all(d == "no disagreement recorded" for d in rows["Docker state sources"])
          and not re.search(r"\b(because|caused by|due to|memory pressure|leak)\b", " ".join(f["statements"]), re.I))
    rep = page.report()
    rec.add("4.7-06", "Equal OOMKilled (B false, C false, both 137): no headline, no OOMKilled statement, no inferred cause", path,
            "headline absent; 'OOMKilled distinguishes' absent; S2 present; no cause statement",
            f"headline={f['headline']!r}; statements={f['statements']}; omitted={f['omitted']}; OOMKilled row={rows['Docker OOMKilled']}",
            rec.expect(ok), console_errors=len(unexpected(rep)), screenshot=env.shot("15-synth-equal-oomkilled.png"), viewport="1280x900", source=src, warnings=expected_warnings(rep))

    def variant(test_id: str, desc: str, query: str, shot_name: str, check: Callable[[dict[str, Any], str], bool], note: str) -> None:
        env.visit("/compare?" + query)
        f = compare_facts(page)
        rows = rows_by_label(f)
        rep = page.report()
        joined = " | ".join(f["statements"])
        omitted = " | ".join(f["omitted"])
        ok = check(f, omitted) and "DISCREPANCY" in f["mainText"].upper()
        rec.add(test_id, desc, "/compare?" + query, note,
                f"headline={f['headline']!r}; statements={f['statements']}; omitted={f['omitted']}; discrepancy row={rows['Docker state sources']}; ExitCode row={rows['Docker ExitCode']}; OOMKilled row={rows['Docker OOMKilled']}; memory limit row={rows['Memory limit']}",
                rec.expect(ok), console_errors=len(unexpected(rep)), screenshot=env.shot(shot_name), viewport="1280x900", source=src, warnings=expected_warnings(rep))

    variant("4.7-07a", "ExitCode discrepancy on C suppresses the headline and the statements that depend on ExitCode", f"c={ids['C_exit_disc']}", "16-synth-exitcode-discrepancy.png",
            lambda f, om: f["headline"] is None and not any("share ExitCode" in s or "OOMKilled distinguishes" in s or "differs from" in s for s in f["statements"])
            and "Docker state sources disagree (ExitCode)" in om,
            "headline, S1, S2, S3 absent; 'Docker state sources disagree (ExitCode)' listed; discrepancy shown with the recorded values")
    variant("4.7-07b", "OOMKilled discrepancy on C suppresses the headline and the OOMKilled statement only", f"c={ids['C_oom_disc']}", "17-synth-oomkilled-discrepancy.png",
            lambda f, om: f["headline"] is None and not any("OOMKilled distinguishes" in s for s in f["statements"])
            and any("share ExitCode 137" in s for s in f["statements"]) and "Docker state sources disagree (OOMKilled)" in om,
            "headline and S3 absent; S2 still shown; 'Docker state sources disagree (OOMKilled)' listed")
    variant("4.7-07c", "Unrelated memory-limit discrepancy on C does not suppress a valid comparison (Requirement 8.13)", f"c={ids['C_mem_disc']}", "18-synth-memory-limit-discrepancy.png",
            lambda f, om: f["headline"] == HEADLINE and len(f["statements"]) == 4 and f["omitted"] == [],
            "headline and all four statements still shown; the discrepancy is displayed")
    variant("4.7-07d", "ExitCode discrepancy on B suppresses the headline and dependent statements", f"b={ids['B_exit_disc']}", "19-synth-b-exitcode-discrepancy.png",
            lambda f, om: f["headline"] is None and not any("share ExitCode" in s or "OOMKilled distinguishes" in s for s in f["statements"]) and "Docker state sources disagree (ExitCode)" in om,
            "headline, S2, S3 absent; 'Docker state sources disagree (ExitCode)' listed")

    # discrepancy values shown from the recorded sources: compare with the API
    for test_id, rid, field in (("4.7-07e", ids["C_exit_disc"], "exit_code"), ("4.7-07f", ids["C_oom_disc"], "oom_killed"), ("4.7-07g", ids["C_mem_disc"], "memory_limit_bytes")):
        d = env.api("/api/runs/" + rid)
        disc = [x for x in d["docker_state"]["discrepancies"] if x["field"] == field]
        env.visit(f"/runs/{rid}")
        text = page.eval("document.getElementById('main').innerText")
        row = page.eval(f"Array.from(document.querySelectorAll('table tbody tr')).filter(r => r.children[0].textContent.trim() === {json.dumps(field)}).map(r => Array.from(r.children).map(c => c.textContent))")
        ok = bool(disc) and bool(row) and str(disc[0]["manifest_value"]).lower() == row[0][1].strip().lower() and str(disc[0]["raw_value"]).lower() == row[0][2].strip().lower() and "DISCREPANCY" in text.upper()
        rec.add(test_id, f"The incident page shows the actual recorded manifest and raw-inspect values of the {field} discrepancy", f"/runs/{rid}",
                "field row with both recorded values; word DISCREPANCY visible", f"API discrepancy={disc}; rendered row={row}", rec.expect(ok), console_errors=len(unexpected(page.report())), viewport="1280x900", source=src)


def sc_show_more(env: Env, many_id: str, total: int) -> None:
    rec = env.rec
    page = env.page
    src = "synthetic fixture (scratch Runs_Directory outside the repository)"
    path = "/runs/" + many_id
    env.visit(path)
    page.eval("document.querySelector('details.sample-table').open = true")
    t0 = page.eval(SAMPLE_ROWS_JS)
    page.click("details.sample-table button")
    t1 = page.eval(SAMPLE_ROWS_JS)
    page.click("details.sample-table button")
    t2 = page.eval(SAMPLE_ROWS_JS)
    ok = (t0["rowCount"] == 200 and t0["moreVisible"] and t0["status"] == f"Showing 200 of {total} rows"
          and t1["rowCount"] == 400 and t1["moreVisible"] and t1["status"] == f"Showing 400 of {total} rows"
          and t2["rowCount"] == total and not t2["moreVisible"] and t2["status"] == f"Showing {total} of {total} rows")
    rec.add("4.7-08a", "Sample table 'Show more': appears when more rows exist, a real click reveals rows and updates the count, then the control disappears", path,
            f"200 rows + control + 'Showing 200 of {total} rows'; click -> 400; click -> {total}, control hidden",
            f"initial rows={t0['rowCount']} control visible={t0['moreVisible']} status={t0['status']!r}; after click 1 rows={t1['rowCount']} status={t1['status']!r}; after click 2 rows={t2['rowCount']} control visible={t2['moreVisible']} status={t2['status']!r}",
            rec.expect(ok), console_errors=len(unexpected(page.report())), screenshot=env.shot("20-synth-show-more.png", full_page=False), viewport="1280x900", source=src)


def sc_show_more_real(env: Env) -> None:
    rec = env.rec
    page = env.page
    for key in "BC":
        env.visit("/runs/" + REAL[key])
        page.eval("document.querySelector('details.sample-table').open = true")
        t = page.eval(SAMPLE_ROWS_JS)
        ok = t["moreHidden"] is True and not t["moreVisible"] and t["status"] == f"Showing {t['rowCount']} of {t['rowCount']} rows"
        rec.add(f"4.7-08b.{key}", f"Real run {key}: all rows shown, so the 'Show more' control is hidden (design: chunks of 200)", "/runs/" + REAL[key],
                "control hidden when shown >= total", f"rows={t['rowCount']} status={t['status']!r} control hidden={t['moreHidden']} visible={t['moreVisible']}",
                rec.expect(ok), console_errors=len(unexpected(page.report())), viewport="1280x900", source="real saved runs")
    env.visit("/compare")
    f = compare_facts(page)
    rec.add("4.7-08c", "The compare page has no 'Show more' control (the design places it only in the incident Sample table)", "/compare",
            "no Show more button on /compare", f"'Show more' buttons found on /compare: {f['showMore']}", rec.expect(f["showMore"] == 0), console_errors=0, viewport="1280x900", source="real saved runs")


PARTIAL_ID = "c-20261120T211433Z-000000ff"
PARTIAL_SEQ_END = 7000
PARTIAL_MISSING = ((2000, 2049), (5500, 5504))  # known Evidence_Gap ranges (missing seq, inclusive)
PARTIAL_NULL = range(100, 120)  # Samples whose rss_bytes is null (counted in total_samples, not plottable)
PARTIAL_PEAKS = (3305, 4405)  # equal highest RSS; the earliest (3305) is NOT on the uniform stride, so it is forced in (5000 points)
PARTIAL_PEAKS_ON_STRIDE = (3333, 4444)  # the earliest (3333) is already on the stride, so the selection has 4999 points
PARTIAL_PEAK_RSS = 200_000_000


def partial_rss(seq: int, peaks: tuple[int, ...] = PARTIAL_PEAKS) -> int:
    return PARTIAL_PEAK_RSS if seq in peaks else 60_000_000 + (seq * 7919) % 20_000_000


def partial_present_seqs() -> list[int]:
    gone = {s for lo, hi in PARTIAL_MISSING for s in range(lo, hi + 1)}
    return [s for s in range(PARTIAL_SEQ_END) if s not in gone]


def build_partial_fixture(root: Path, peaks: tuple[int, ...] = PARTIAL_PEAKS, run_id: str = PARTIAL_ID) -> str:
    """A valid Experiment C run with 6925 plottable Samples (> the 5000 cap), 20 null-RSS Samples and two known gaps.

    Written with the approved M0 formats through `RunsBuilder`; about 2.5 MB, far below the 16 MiB artifact limit.
    """
    from datetime import timedelta

    from tests.unit.viewer.builder import DEFAULT_START_TIME, GapSpec, format_ts, parse_ts

    start = parse_ts(DEFAULT_START_TIME)
    specs = [SampleSpec(seq=s, rss_bytes=None if s in PARTIAL_NULL else partial_rss(s, peaks)) for s in partial_present_seqs()]
    gaps = [
        GapSpec(kind="sequence_discontinuity", first_missing_seq=lo, last_missing_seq=hi,
                start_time=format_ts(start + timedelta(milliseconds=(lo - 1) * 500)),
                end_time=format_ts(start + timedelta(milliseconds=(hi + 1) * 500)))
        for lo, hi in PARTIAL_MISSING
    ]
    return RunsBuilder(root).add_run("C", samples=specs, gaps=gaps, run_id=run_id)


TARGETS_JS = r"""
(() => {
  const open = %s;
  if (open) { document.querySelectorAll('details').forEach((d) => { d.open = true; }); }
  const vis = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && n.checkVisibility({ checkVisibilityCSS: true, contentVisibilityAuto: true }); };
  const items = [];
  const sel = 'a[href], button, summary, input, select, textarea, [tabindex]:not([tabindex="-1"]), [role=button]';
  for (const n of document.querySelectorAll(sel)) {
    if (!vis(n) || n.closest('svg')) { continue; }
    const r = n.getBoundingClientRect();
    const kind = n.tagName.toLowerCase() + (n.getAttribute('class') ? '.' + n.getAttribute('class').split(' ')[0] : '')
      + (n.tagName === 'SUMMARY' ? '(' + (n.parentElement.getAttribute('class') || 'details') + ')' : '');
    items.push({ kind: kind, text: (n.textContent || '').trim().slice(0, 30), w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10,
      l: r.left, t: r.top, r: r.right, b: r.bottom, inProse: !!n.closest('p') });
  }
  const overlaps = [];
  for (let i = 0; i < items.length; i += 1) {
    for (let j = i + 1; j < items.length; j += 1) {
      const a = items[i], b = items[j];
      const w = Math.min(a.r, b.r) - Math.max(a.l, b.l), h = Math.min(a.b, b.b) - Math.max(a.t, b.t);
      if (w > 1 && h > 1) { overlaps.push(a.kind + ':' + a.text + ' | ' + b.kind + ':' + b.text); }
    }
  }
  const byKind = {};
  for (const it of items) {
    const k = byKind[it.kind] || (byKind[it.kind] = { count: 0, minW: 1e9, minH: 1e9 });
    k.count += 1; k.minW = Math.min(k.minW, it.w); k.minH = Math.min(k.minH, it.h);
  }
  return { total: items.length, byKind: byKind, short: items.filter((i) => i.h < 44 || i.w < 44).map((i) => i.kind + ' "' + i.text + '" ' + i.w + 'x' + i.h).slice(0, 8),
    shortCount: items.filter((i) => i.h < 44 || i.w < 44).length, overlaps: overlaps.slice(0, 5), overlapCount: overlaps.length };
})()
"""


def measure_targets(page: cdp.Page, opened: bool) -> dict[str, Any]:
    return page.eval(TARGETS_JS % ("true" if opened else "false"))


def sc_targets(env: Env, pages: list[tuple[str, str]], label: str = "") -> None:
    """4.7-20 (measured): the real bounding boxes of every interactive target, closed and opened, at 1280, 480 and 360 px."""
    rec = env.rec
    page = env.page
    table: dict[str, Any] = {}
    problems: list[str] = []
    for width in (1280, 480, 360):
        for path, name in pages:
            env.visit(path, viewport=width)
            closed = measure_targets(page, False)
            opened = measure_targets(page, True)
            lay = page.eval(LAYOUT_JS)
            table[f"{name}@{width}"] = {"closed": closed["byKind"], "opened": opened["byKind"], "closed_total": closed["total"], "opened_total": opened["total"]}
            for state, m in (("closed", closed), ("opened", opened)):
                if m["shortCount"] or m["overlapCount"]:
                    problems.append(f"{name}@{width} {state}: short={m['short']} overlaps={m['overlaps']}")
            if lay["pageHorizontalOverflow"] or lay["outside"] or lay["clipped"] or lay["overlapCount"]:
                problems.append(f"{name}@{width} layout: overflow={lay['pageHorizontalOverflow']} outside={lay['outside'][:2]} clipped={lay['clipped'][:2]} overlaps={lay['overlapCount']}")
    summary: dict[str, list[float]] = {}
    for entry in table.values():
        for state in ("closed", "opened"):
            for kind, v in entry[state].items():
                s = summary.setdefault(kind, [1e9, 1e9, 0])
                s[0], s[1], s[2] = min(s[0], v["minW"]), min(s[1], v["minH"]), max(s[2], v["count"])
    dims = {k: {"min_width_px": v[0], "min_height_px": v[1], "max_count_on_a_page": v[2]} for k, v in sorted(summary.items())}
    rec.add("4.7-20.measured" + label, "Every interactive target (links, buttons, summary toggles, focusable elements) has a real bounding box of at least 44 x 44 px, none overlap, and no layout check regresses, at 1280, 480 and 360 px",
            ", ".join(p for p, _ in pages), "min width and height >= 44 px for every target (closed and opened details); 0 overlapping targets; no page overflow, clipped or overlapping text",
            f"smallest box per target kind (width x height px): {json.dumps({k: [v['min_width_px'], v['min_height_px']] for k, v in dims.items()})}; problems: {problems}",
            rec.expect(not problems), console_errors=len(unexpected(page.report())), viewport="1280, 480, 360", source=env.source,
            extra={"dimensions_by_kind": dims, "by_page": table, "problems": problems})


def sc_target_behaviour(env: Env, run_path: str) -> None:
    """The enlarged toggles keep native <details>/<summary> behaviour: real click and keyboard, visible focus, 44 px box."""
    page = env.page
    env.visit(run_path, viewport=1280)
    box = page.eval("(() => { const s = document.querySelector('details.original > summary'); s.scrollIntoView({block:'center'}); const r = s.getBoundingClientRect(); return [r.width, r.height, s.parentElement.open]; })()")
    page.click("details.original > summary")
    opened = page.eval("document.querySelector('details.original').open")
    page.key(" ")
    page.wait_for("document.querySelector('details.original').open === false", 5, "Space closes the original disclosure")
    page.eval("document.querySelector('details.original > summary').focus()")
    page.key("Tab", shift=True)
    page.key("Tab")
    d = page.eval(DESCRIBE_ACTIVE)
    page.key("Enter")
    page.wait_for("document.activeElement.parentElement.open === true", 5, "Enter opens the disclosure")
    ok = (box[1] >= 44 and box[0] >= 44 and box[2] is False and opened is True and d["tag"] == "SUMMARY" and d["outlineStyle"] != "none" and float(d["outlineWidth"][:-2]) >= 2)
    env.shot("21-original-toggle-focus.png", full_page=False)
    env.rec.add("4.7-20.behaviour", "'original' toggle: native details/summary, real mouse click opens, Space closes, Tab focus shows a >= 2 px outline, Enter opens; box >= 44 px", run_path,
                "box >= 44 x 44; click opens; Space closes; focus outline visible; Enter opens",
                f"box={box}; open after click={opened}; focused={d['tag']} outline={d['outlineStyle']} {d['outlineWidth']} {d['outlineColor']} rect={d['rect']}",
                env.rec.expect(ok), console_errors=len(unexpected(page.report())), screenshot="21-original-toggle-focus.png", viewport="1280x900", source=env.source)


# ------------------------------------------------------------------ Partial_View (4.7-19)

PARTIAL_DOM_JS = r"""
(() => {
  const svg = document.querySelector('.chart-svg');
  const num = (v) => Number(v);
  const polys = Array.from(svg.querySelectorAll('polyline')).map((p) => ({
    cls: p.getAttribute('class'), dash: p.getAttribute('stroke-dasharray'), computed: getComputedStyle(p).strokeDasharray,
    pts: p.getAttribute('points').split(' ').map((s) => s.split(',').map(num)),
  }));
  const gaps = Array.from(svg.querySelectorAll('line.chart-gap')).map((l) => num(l.getAttribute('x1')));
  const gapLabels = Array.from(svg.querySelectorAll('.chart-gap-label')).map((t) => t.textContent);
  const marks = Array.from(svg.querySelectorAll('line.chart-axis')).filter((l) => num(l.getAttribute('y2')) - num(l.getAttribute('y1')) === 5).map((l) => num(l.getAttribute('x1')));
  const labels = Array.from(svg.querySelectorAll('.chart-x-tick-label')).map((t) => ({ text: t.textContent, x: num(t.getAttribute('x')) }));
  const q = (s) => { const e = document.querySelector(s); return e ? e.innerText : null; };
  const dashedSwatch = document.querySelector('.chart-legend .swatch-dashed');
  const sw = dashedSwatch ? getComputedStyle(dashedSwatch) : null;
  const desc = svg.querySelector('desc');
  return { polys: polys, gaps: gaps, gapLabels: gapLabels, marks: marks, labels: labels,
    partialLabel: q('.partial-label'), legend: q('.chart-legend'), below: q('.chart-text'), axisNotes: q('.chart-axis-notes'),
    desc: desc ? desc.textContent : null, circles: svg.querySelectorAll('circle.chart-point').length,
    swatchBorderStyle: sw ? sw.borderTopStyle : null, factsText: document.getElementById('main').innerText };
})()
"""


def partial_expectations(peaks: tuple[int, ...]) -> dict[str, Any]:
    """Everything the Viewer must report for the partial fixture, computed from the fixture definition alone."""
    present = partial_present_seqs()
    plottable = [s for s in present if s not in PARTIAL_NULL]
    rss = {s: partial_rss(s, peaks) for s in plottable}
    top = max(rss.values())
    return {"present": present, "plottable": plottable, "total": len(present), "plottable_n": len(plottable),
            "unplottable_n": len(present) - len(plottable), "first": plottable[0], "last": plottable[-1],
            "first_rss": rss[plottable[0]], "last_rss": rss[plottable[-1]], "peak_rss": top,
            "peak_seq": min(s for s in plottable if rss[s] == top)}


def expected_segments(seqs: list[int], present: list[int]) -> list[tuple[str, bool, int]]:
    """(segment_before, gap_before, omitted_before) for each plotted seq pair, from the full sequence, not from the Viewer."""
    pos = {s: i for i, s in enumerate(present)}
    out: list[tuple[str, bool, int]] = [("none", False, 0)]
    for prev, cur in zip(seqs, seqs[1:]):
        omitted = pos[cur] - pos[prev] - 1
        gap = any(prev < lo and hi < cur for lo, hi in PARTIAL_MISSING)
        out.append(("none" if gap else ("solid" if omitted == 0 else "dashed"), gap, omitted))
    return out


def sc_partial_view(env: Env, run_id: str, peaks: tuple[int, ...], plotted_expected: int, shots: bool = True) -> None:
    rec = env.rec
    page = env.page
    ex = partial_expectations(peaks)
    path = "/runs/" + run_id
    resp = env.visit(path)
    page.wait_for("document.querySelector('.chart-svg') !== null", 20, "chart drawn")
    detail = env.api("/api" + path)
    chart = detail["chart"]
    es = detail["evidence_summary"]
    pts = chart["points"]
    seqs = [p["seq"] for p in pts]
    dom = page.eval(PARTIAL_DOM_JS)
    tag = "" if peaks == PARTIAL_PEAKS else ".stride"
    src = env.source
    if shots and not tag:  # before the Sample table is opened, so the chart section is just the chart
        env.shot("22-partial-view-incident.png", full_page=False)
        page.eval("document.querySelector('.chart-section').scrollIntoView()")
        env.shot("23-partial-view-chart.png", element=".chart-section")
    # interact first so the console/CSP check covers real use: focus the chart, Home, End, open the Sample table
    page.eval("document.querySelector('.chart-plot').focus()")
    page.key("Home")
    home = page.eval(READOUT_JS)
    page.key("End")
    end = page.eval(READOUT_JS)
    page.eval("document.querySelector('details.sample-table').open = true")
    caption = page.eval("document.querySelector('details.sample-table caption').textContent")
    rows_shown = page.eval("document.querySelectorAll('details.sample-table tbody tr').length")
    report_after = page.report()
    seg_expect = expected_segments(seqs, ex["present"]) if len(seqs) > 1 else []
    seg_api = [(p["segment_before"], p["gap_before"], p["omitted_before"]) for p in pts]

    def add(n: int, what: str, expected: str, actual: str, ok: bool, shot: str | None = None, extra: Any = None) -> None:
        rec.add(f"4.7-19{tag}.{n}", f"Partial_View{' (peak on the stride)' if tag else ''}: {what}", path, expected, actual, rec.expect(ok), console_errors=len(unexpected(report_after)),
                screenshot=shot, viewport="1280x900", source=src, extra=extra)

    add(1, "Partial_View is true", "chart.partial_view true; the page shows the 'Partial view' label",
        f"HTTP {resp.get('status')}; partial_view={chart['partial_view']}; chart_cap={chart['chart_cap']}; label={dom['partialLabel']!r}; desc has 'Partial view: yes'={'Partial view: yes' in (dom['desc'] or '')}",
        chart["partial_view"] is True and resp.get("status") == 200 and "Partial view" in (dom["partialLabel"] or "") and "Partial view: yes" in (dom["desc"] or ""),
        "22-partial-view-incident.png" if not tag else None)
    add(2, "total_samples and plottable_samples describe the full evidence",
        f"total {ex['total']}, plottable {ex['plottable_n']}, unplottable {ex['unplottable_n']}, evidence_summary.sample_count {ex['total']}",
        f"total={chart['total_samples']} plottable={chart['plottable_samples']} unplottable={chart['unplottable_samples']} sample_count={es['sample_count']}; label={dom['partialLabel']!r}; below chart={dom['below']!r}",
        (chart["total_samples"], chart["plottable_samples"], chart["unplottable_samples"], es["sample_count"]) == (ex["total"], ex["plottable_n"], ex["unplottable_n"], ex["total"])
        and f"valid Samples {ex['total']}" in (dom["partialLabel"] or "") and f"no valid RSS (not drawn): {ex['unplottable_n']}" in (dom["below"] or ""))
    add(3, f"plotted_samples is {plotted_expected} (the cap is 5000)", f"plotted_samples == len(points) == {plotted_expected} <= 5000; the page reports the same count",
        f"plotted_samples={chart['plotted_samples']} len(points)={len(pts)}; label={dom['partialLabel']!r}; desc={dom['desc'][:60] if dom['desc'] else None!r}; Sample table caption={caption!r}; table rows shown in first chunk={rows_shown}",
        chart["plotted_samples"] == len(pts) == plotted_expected and f"plotted {plotted_expected}" in (dom["partialLabel"] or "") and f"Points plotted: {plotted_expected}." in (dom["desc"] or "")
        and f"plotted {plotted_expected}" in caption)
    peak_in = ex["peak_seq"] in seqs
    add(4, "the first, the last and the earliest-peak observations are represented",
        f"seq {ex['first']} first, seq {ex['last']} last, earliest peak seq {ex['peak_seq']} ({ex['peak_rss']} B) among the plotted points; Home and End select the first and last",
        f"first plotted={seqs[0]}; last plotted={seqs[-1]}; earliest peak {ex['peak_seq']} plotted={peak_in}; later equal peak {peaks[1]} plotted={peaks[1] in seqs}; Home readout={home['lines'][:1]}; End readout={end['lines'][:1]}; sequence strictly increasing={all(a < b for a, b in zip(seqs, seqs[1:]))}",
        seqs[0] == ex["first"] and seqs[-1] == ex["last"] and peak_in and all(x < y for x, y in zip(seqs, seqs[1:])) and set(seqs) <= set(ex["plottable"])
        and point_matches(home["lines"], pts[0]) and point_matches(end["lines"], pts[-1]),
        extra={"home": home, "end": end})
    first = es["first_rss"]
    hi = es["highest_rss"]
    last = es["last_rss"]
    add(5, "full-run RSS statistics come from all the evidence, not from the selection",
        f"first RSS {ex['first_rss']} (seq {ex['first']}), highest {ex['peak_rss']} (seq {ex['peak_seq']}), last {ex['last_rss']} (seq {ex['last']}), y_data_max_bytes {ex['peak_rss']}; sample_count {ex['total']}",
        f"first={first['seq']}/{first['rss_bytes']} highest={hi['seq']}/{hi['rss_bytes']} last={last['seq']}/{last['rss_bytes']} y_data_max={chart['y_data_max_bytes']} sample_count={es['sample_count']}; page shows 200000000: {str(ex['peak_rss']) in dom['factsText']}",
        (first["seq"], first["rss_bytes"], hi["seq"], hi["rss_bytes"], last["seq"], last["rss_bytes"], chart["y_data_max_bytes"])
        == (ex["first"], ex["first_rss"], ex["peak_seq"], ex["peak_rss"], ex["last"], ex["last_rss"], ex["peak_rss"]) and str(ex["peak_rss"]) in dom["factsText"],
        extra={"note": "expected values come from the fixture definition (partial_expectations), not from the Viewer"})

    dashed_api = [i for i, s in enumerate(seg_api) if s[0] == "dashed"]
    dashed_dom = [p for p in dom["polys"] if "chart-line-dashed" in (p["cls"] or "")]
    solid_dom = [p for p in dom["polys"] if "chart-line-dashed" not in (p["cls"] or "")]
    dom_dashed_segments = sum(len(p["pts"]) - 1 for p in dashed_dom)
    dom_solid_segments = sum(len(p["pts"]) - 1 for p in solid_dom)
    exp_dashed = sum(1 for s in seg_expect if s[0] == "dashed")
    exp_solid = sum(1 for s in seg_expect if s[0] == "solid")
    add(6, "segments across omitted observations are dashed (and only those)",
        f"API and an independent computation from the full sequence agree for all {len(pts)} points; {exp_dashed} dashed and {exp_solid} solid segments are drawn",
        f"API segment/gap/omitted equals independent computation={seg_api == seg_expect}; dashed pairs={len(dashed_api)} (independent {exp_dashed}); drawn dashed segments={dom_dashed_segments} in {len(dashed_dom)} polylines; drawn solid segments={dom_solid_segments} in {len(solid_dom)} polylines; dasharray attribute values={sorted({p['dash'] for p in dashed_dom})}; computed stroke-dasharray dashed={sorted({p['computed'] for p in dashed_dom})} solid={sorted({p['computed'] for p in solid_dom})}",
        seg_api == seg_expect and exp_dashed > 0 and dom_dashed_segments == exp_dashed and dom_solid_segments == exp_solid
        and all(p["dash"] == "8 5" and p["computed"] not in ("none", "") for p in dashed_dom) and all(p["computed"] == "none" for p in solid_dom))
    legend = dom["legend"] or ""
    add(7, "the legend explains the dashed segments", "legend item 'dashed: omitted observations between these points' with a dashed swatch",
        f"legend={legend!r}; swatch border-top-style={dom['swatchBorderStyle']}", "dashed: omitted observations between these points" in legend and dom["swatchBorderStyle"] == "dashed",
        "23-partial-view-chart.png" if not tag else None)
    gap_idx = [i for i, p in enumerate(pts) if p["gap_before"]]
    exp_gap_idx = [i for i, s in enumerate(seg_expect) if s[1]]
    crossing = [g for g in dom["gaps"] for p in dom["polys"] for a, b in zip(p["pts"], p["pts"][1:]) if min(a[0], b[0]) < g < max(a[0], b[0])]
    pair_ranges = [(seqs[i - 1], seqs[i]) for i in gap_idx]
    add(8, "known evidence gaps are never joined by a solid or dashed line",
        f"{len(PARTIAL_MISSING)} gap marks, one per known missing range {list(PARTIAL_MISSING)}; segment_before 'none' across them; no drawn segment spans a gap mark",
        f"gap_before at points {gap_idx} (independent {exp_gap_idx}), spanning seq pairs {pair_ranges}; segment_before there={[pts[i]['segment_before'] for i in gap_idx]}; gap marks drawn at x={dom['gaps']} labels={dom['gapLabels']}; polyline segments spanning a gap mark={crossing}; chart.gaps={[(g['first_missing_seq'], g['last_missing_seq']) for g in chart['gaps']]}",
        gap_idx == exp_gap_idx and len(gap_idx) == len(PARTIAL_MISSING) and all(pts[i]["segment_before"] == "none" for i in gap_idx) and len(dom["gaps"]) == len(PARTIAL_MISSING)
        and crossing == [] and all(pair_ranges[k][0] < lo and hi < pair_ranges[k][1] for k, (lo, hi) in enumerate(PARTIAL_MISSING)),
        "24-partial-view-gap-closeup.png" if shots and not tag else None, extra={"gap_marks_x": dom["gaps"]})
    if shots and not tag and dom["gaps"]:
        env.page.eval("document.querySelector('.chart-plot').scrollIntoView({block:'center'})")
        clip_box = page.eval("(() => { const l = document.querySelector('line.chart-gap').getBoundingClientRect(); return [l.left + scrollX, l.top + scrollY, l.height]; })()")
        clip = {"x": max(0, clip_box[0] - 90), "y": max(0, clip_box[1] - 30), "width": 180, "height": clip_box[2] + 60, "scale": 3}
        data = page.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True, "clip": clip})["data"]
        import base64
        (env.shots / "24-partial-view-gap-closeup.png").write_bytes(base64.b64decode(data))
    problems = unexpected(report_after)
    add(9, "no JavaScript error, uncaught exception or CSP violation (after Home, End and opening the Sample table)", "0 console errors, 0 exceptions, 0 CSP violations, 0 failed requests",
        f"problems={problems}; csp={report_after['csp_violations']}; warnings={expected_warnings(report_after)}", problems == [])
    ticks = chart["x_axis"]["ticks"]
    xs = [t["x_ms"] for t in ticks]
    step = xs[1] - xs[0]
    clocks = [re.search(r"T(\d\d:\d\d:\d\d)", t["label"]).group(1) for t in ticks]
    shown = [lab["text"] for lab in dom["labels"]]
    # linear x scale fitted on the first and last tick marks, then checked on the other marks and on the plotted line ends
    marks = dom["marks"]
    scale = (marks[-1] - marks[0]) / (xs[-1] - xs[0])
    resid = max(abs(marks[i] - (marks[0] + (xs[i] - xs[0]) * scale)) for i in range(len(xs)))
    all_x = [v[0] for p in dom["polys"] for v in p["pts"]]
    x_first = marks[0] + (pts[0]["x_ms"] - xs[0]) * scale
    x_last = marks[0] + (pts[-1]["x_ms"] - xs[0]) * scale
    ends = (abs(min(all_x) - x_first), abs(max(all_x) - x_last))
    dom_min, dom_max = chart["x_axis"]["domain_min_ms"], chart["x_axis"]["domain_max_ms"]
    exp_first_ms = 1795209180000  # 2026-11-20T21:13:00.000Z
    add(10, "x tick positions and labels are correct",
        f"ticks inside the domain at a constant step on multiples of the step; labels are the clock parts {clocks}; each label is at its tick mark; marks are a linear function of time that also places the first and last plotted points",
        f"ticks x_ms={xs} step={step}; domain={dom_min}..{dom_max}; every tick on a multiple of the step={all(x % step == 0 for x in xs)}; labels shown={shown}; label x equals mark x={[l['x'] for l in dom['labels']] == marks}; max residual of marks to a linear time scale={resid:.4f} px; line end vs expected x (first, last)={tuple(round(e, 3) for e in ends)}; first point x_ms={pts[0]['x_ms']} (expected {exp_first_ms}); axis note={dom['axisNotes']!r}",
        all(dom_min <= x <= dom_max for x in xs) and all(x % step == 0 for x in xs) and shown == clocks and [l["x"] for l in dom["labels"]] == marks
        and resid < 0.05 and max(ends) < 0.05 and pts[0]["x_ms"] == exp_first_ms and "2026-11-20" in (dom["axisNotes"] or ""))


def build_many_fixture(root: Path, total: int) -> str:
    b = RunsBuilder(root)
    return b.add_run("B", samples=sample_specs(total), run_id="b-20261120T211311Z-0000000a")


# ------------------------------------------------------------------ main


def run_partial(out: Path, scratch: Path, rec: Recorder, chrome: cdp.Chrome, python: str | None = None) -> None:
    """4.7-19: two scratch Viewers, one per partial fixture (peak off the stride: 5000 points; peak on the stride: 4999)."""
    off = scratch / "fixture-partial"
    on = scratch / "fixture-partial-stride"
    build_partial_fixture(off)
    on_id = "c-20261120T211433Z-000000fe"
    build_partial_fixture(on, PARTIAL_PEAKS_ON_STRIDE, on_id)
    src = "synthetic partial fixture (6945 Samples, scratch Runs_Directory outside the repository)"
    with cdp.ViewerProcess(off, python) as v:
        env = Env(chrome.page, rec, v.base, out, src)
        sc_partial_view(env, PARTIAL_ID, PARTIAL_PEAKS, 5000)
        sc_targets(env, [("/runs/" + PARTIAL_ID, "partial-incident")], ".partial")
    with cdp.ViewerProcess(on, python) as v:
        env = Env(chrome.page, rec, v.base, out, src)
        sc_partial_view(env, on_id, PARTIAL_PEAKS_ON_STRIDE, 4999)


def run_synthetic_only(out: Path, scratch: Path, rec: Recorder, chrome: cdp.Chrome, python: str | None = None) -> None:
    fx = scratch / "fixture-compare"
    ids = build_compare_fixture(fx)
    many = scratch / "fixture-many"
    many_id = build_many_fixture(many, 450)
    with cdp.ViewerProcess(fx, python) as v1:
        env = Env(chrome.page, rec, v1.base, out, "synthetic fixture")
        sc_synthetic_compare(env, ids)
    with cdp.ViewerProcess(many, python) as v2:
        env = Env(chrome.page, rec, v2.base, out, "synthetic fixture")
        sc_show_more(env, many_id, 450)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/m1-results/browser-4.7")
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--headed", action="store_true", help="run a visible Chrome window instead of headless")
    ap.add_argument("--rerun", default=None, help="label of a rerun (for example \"Rerun 1\"): adds the target and Partial_View checks and writes <out>/task-4.7-rerun-results.json")
    ap.add_argument("--merge-into", default=None, help="results JSON that keeps the original run; the rerun is appended to it (nothing is replaced)")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="coroner-47-scratch-"))
    rec = Recorder(out)
    started = time.time()
    try:
        with cdp.Chrome(headless=not args.headed) as chrome:
            rec.chrome_version = chrome.version
            with cdp.ViewerProcess(args.runs_dir, args.python, args.port) as real:
                env = Env(chrome.page, rec, real.base, out)
                env.rerun = bool(args.rerun)
                viewer_command = " ".join(real.command)
                for scenario in (sc_static_fresh, sc_real_pages, sc_select_run, sc_compare_errors, sc_console_csp, sc_capture_controls, sc_tab_order,
                                 sc_chart_keyboard, sc_sample_table, sc_tick_labels, sc_show_more_real, sc_responsive,
                                 sc_motion_contrast_ax, sc_not_tested):
                    try:
                        scenario(env)
                    except Exception as err:  # a harness error is a failure, never silently skipped
                        rec.add(f"4.7-ERR.{scenario.__name__}", f"scenario {scenario.__name__} raised", "n/a", "no exception", f"{type(err).__name__}: {err}", "FAIL")
                if args.rerun:
                    pages = [("/", "overview"), ("/runs/" + REAL["A"], "incident-A"), ("/runs/" + REAL["B"], "incident-B"),
                             ("/runs/" + REAL["C"], "incident-C"), ("/compare", "compare")]
                    for scenario2 in (lambda e: sc_targets(e, pages, ".real"), lambda e: sc_target_behaviour(e, "/runs/" + REAL["B"])):
                        try:
                            scenario2(env)
                        except Exception as err:
                            rec.add("4.7-ERR.targets", "target scenario raised", "n/a", "no exception", f"{type(err).__name__}: {err}", "FAIL")
            run_synthetic_only(out, scratch, rec, chrome, args.python)
            if args.rerun:
                try:
                    run_partial(out, scratch, rec, chrome, args.python)
                except Exception as err:
                    rec.add("4.7-ERR.partial", "Partial_View scenario raised", "n/a", "no exception", f"{type(err).__name__}: {err}", "FAIL")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    summary = {s: sum(1 for r in rec.results if r["status"] == s) for s in ("PASS", "FAIL", "NOT TESTED")}
    doc = {
        "task": "4.7", "automation": "headless Chrome driven by the DevTools Protocol (tests/unit/viewer/cdp_harness.py)" if not args.headed else "headed Chrome via DevTools Protocol",
        "chrome": rec.chrome_version, "platform": platform.platform(), "python": platform.python_version(), "viewer_command": viewer_command,
        "human_operated_interactive_chrome": "NOT PERFORMED", "screen_reader": "NOT PERFORMED",
        "duration_s": round(time.time() - started, 1), "summary": {"total": len(rec.results), **summary,
        "warnings": sum(1 for r in rec.results if r["warnings"])}, "results": rec.results,
    }
    if args.rerun:
        doc["label"] = args.rerun
        doc["rerun_of"] = "original run (headless Chrome, 64 checks)"
        (out / "task-4.7-rerun-results.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        if args.merge_into:
            target = Path(args.merge_into)
            base = json.loads(target.read_text(encoding="utf-8"))
            base.setdefault("rev_history", [{"rev": "Original run", "summary": base["summary"], "chrome": base["chrome"]}])
            base.setdefault("reruns", [])
            base["reruns"] = [r for r in base["reruns"] if r.get("label") != args.rerun] + [doc]
            base["rev_history"] = [r for r in base["rev_history"] if r["rev"] != args.rerun] + [{"rev": args.rerun, "summary": doc["summary"], "chrome": doc["chrome"], "duration_s": doc["duration_s"]}]
            target.write_text(json.dumps(base, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    else:
        (out / "task-4.7-results.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(doc["summary"]))
    return 1 if summary["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
