// RSS timeline (inline SVG, no library). The browser only does arithmetic on
// numbers the API supplied: x positions come from the server-supplied x_axis
// domain and tick x_ms values, and the tick text is the clock part of the tick's own
// label (the date is shown below the chart). No recorded timestamp is parsed into a Date. Every artifact value is set with
// textContent (or a text node), never as markup.

import { COPY, fill } from "/static/copy.js";
import { SVG_NS, clear, el, textEl } from "/static/dom.js";

const VIEW_W = 960;
const VIEW_H = 360;
const M_LEFT = 72;
const M_RIGHT = 24;
const M_TOP = 24;
const M_BOTTOM = 64;
const PLOT_W = VIEW_W - M_LEFT - M_RIGHT;
const PLOT_H = VIEW_H - M_TOP - M_BOTTOM;
const PLOT_BOTTOM = M_TOP + PLOT_H;
const MAX_CIRCLES = 600;
const TABLE_CHUNK = 200;
const BYTES_PER_MIB = 1048576;
const Y_HEADROOM = 1.05;
const TICK_LABEL_DY = 18; // tick label baseline below the x axis
const AXIS_TITLE_DY = 42; // x axis title baseline below the x axis
const CHAR_W = 6.2; // monospace glyph advance at the 10px chart text, in viewBox units
const TEXT_H = 10;
const LABEL_ROW_STEP = 14;
const LABEL_ROWS = 8;

function isNum(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function r2(value) {
  return String(Math.round(value * 100) / 100);
}

function svgEl(tag, className, attrs, content) {
  const node = document.createElementNS(SVG_NS, tag);
  if (className) {
    node.setAttribute("class", className);
  }
  if (attrs) {
    for (const name of Object.keys(attrs)) {
      node.setAttribute(name, String(attrs[name]));
    }
  }
  if (content !== undefined && content !== null) {
    node.textContent = String(content);
  }
  return node;
}

// ---------------------------------------------------------------- scales

// x = left + (x_ms - domain_min_ms) / (domain_max_ms - domain_min_ms) * plot_width.
// A zero-width (or missing) domain centres the value.
export function xPosition(xMs, axis) {
  if (!axis || !isNum(axis.domain_min_ms) || !isNum(axis.domain_max_ms) || !isNum(xMs)) {
    return M_LEFT + PLOT_W / 2;
  }
  const span = axis.domain_max_ms - axis.domain_min_ms;
  if (span === 0) {
    return M_LEFT + PLOT_W / 2;
  }
  return M_LEFT + ((xMs - axis.domain_min_ms) / span) * PLOT_W;
}

// Inverse of xPosition, used for pointer hover. Returns null for a zero-width domain.
function xMsAt(svgX, axis) {
  const span = axis.domain_max_ms - axis.domain_min_ms;
  if (span === 0) {
    return null;
  }
  return axis.domain_min_ms + ((svgX - M_LEFT) / PLOT_W) * span;
}

// Nice y ticks (1, 2 or 5 times a power of ten, in MiB) from 0 up to maxMiB. Four to six ticks are
// chosen when a nice step allows it; otherwise the step whose tick count is closest to that range.
export function niceYTicks(maxMiB) {
  const hi = isNum(maxMiB) && maxMiB > 0 ? maxMiB : 1;
  let best = null;
  for (let exp = -4; exp <= 14; exp += 1) {
    for (const mult of [1, 2, 5]) {
      const step = mult * Math.pow(10, exp);
      const count = Math.floor(hi / step + 1e-9) + 1;
      if (count > 40) {
        continue;
      }
      const distance = count < 4 ? 4 - count : count > 6 ? count - 6 : 0;
      if (best === null || distance < best.distance) {
        best = { step: step, count: count, distance: distance, digits: exp < 0 ? -exp : 0 };
      }
    }
  }
  const ticks = [];
  for (let i = 0; i < best.count; i += 1) {
    ticks.push({ value: i * best.step, text: (i * best.step).toFixed(best.digits) });
  }
  return ticks;
}

// Index of the entry of the ascending array xs nearest to target (binary search).
export function nearestIndex(xs, target) {
  if (xs.length === 0) {
    return -1;
  }
  let lo = 0;
  let hi = xs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] < target) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  if (lo > 0 && Math.abs(xs[lo - 1] - target) <= Math.abs(xs[lo] - target)) {
    return lo - 1;
  }
  return lo;
}

// ---------------------------------------------------------------- labels and layout (pure)

const UTC_TEXT_RE = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z$/;

// Splits a UTC timestamp text as the API returns it (YYYY-MM-DDTHH:MM:SS[.fff]Z) into its date and its
// clock text. The clock is HH:MM:SS, plus the milliseconds only when they are not 000. Nothing is
// parsed into a Date and nothing is invented: a text of another shape gives null.
export function splitUtcText(text) {
  if (typeof text !== "string") {
    return null;
  }
  const m = UTC_TEXT_RE.exec(text);
  if (m === null) {
    return null;
  }
  let clock = m[2];
  if (m[3] !== undefined) {
    const ms = (m[3] + "000").slice(0, 3);
    if (ms !== "000") {
      clock += "." + ms;
    }
  }
  return { date: m[1], clock: clock };
}

// Distinct dates (YYYY-MM-DD) of the given timestamp texts, ascending.
export function distinctDates(texts) {
  const seen = new Set();
  for (const text of texts) {
    const parts = splitUtcText(text);
    if (parts !== null) {
      seen.add(parts.date);
    }
  }
  return Array.from(seen).sort();
}

export function textWidth(text) {
  return String(text).length * CHAR_W;
}

// Anchor of an x tick label so that its text stays inside [0, viewW].
export function tickAnchor(x, width, viewW) {
  if (x - width / 2 < 0) {
    return "start";
  }
  if (x + width / 2 > viewW) {
    return "end";
  }
  return "middle";
}

function labelBox(x, y, anchor, width) {
  const left = anchor === "end" ? x - width : anchor === "middle" ? x - width / 2 : x;
  return { l: left, r: left + width, t: y - TEXT_H * 0.8, b: y + TEXT_H * 0.25 };
}

function boxesOverlap(a, b, pad) {
  return a.l < b.r + pad && b.l < a.r + pad && a.t < b.b + pad && b.t < a.b + pad;
}

function boxInside(box, bounds) {
  return box.l >= bounds.l && box.r <= bounds.r && box.t >= bounds.t && box.b <= bounds.b;
}

// Which of the x ticks get a displayed label. items: [{x, width, anchor}] in any order. Marks are not
// affected: this only decides the label text. Greedy from the left: a label is shown when its box clears
// the previous shown one by gap units. Returns a Set of indexes into items.
export function thinTickLabels(items, gap) {
  const order = items.map((_, i) => i).sort((a, b) => items[a].x - items[b].x);
  const shown = new Set();
  let lastRight = -Infinity;
  for (const i of order) {
    const box = labelBox(items[i].x, 0, items[i].anchor, items[i].width);
    if (box.l >= lastRight + gap) {
      shown.add(i);
      lastRight = box.r;
    }
  }
  return shown;
}

// Places annotation labels without overlap. requests: [{id, width, candidates: [{x, y, anchor}]}] in
// priority order. fixed: boxes no label may touch (lines, earlier labels are added automatically).
// soft: boxes to avoid when possible (plotted points). bounds: {l, r, t, b}. The first candidate that is
// inside the bounds and clear of every other box wins; if none is clear of the soft boxes, the first
// that is clear of the hard boxes; otherwise the first candidate (reported with fits: false).
export function placeLabels(requests, bounds, fixed, soft) {
  const hard = fixed.slice();
  const placed = {};
  for (const req of requests) {
    let chosen = null;
    for (const passSoft of [true, false]) {
      for (const c of req.candidates) {
        const box = labelBox(c.x, c.y, c.anchor, req.width);
        if (!boxInside(box, bounds)) {
          continue;
        }
        if (hard.some((h) => boxesOverlap(box, h, 2))) {
          continue;
        }
        if (passSoft && soft.some((s) => boxesOverlap(box, s, 1))) {
          continue;
        }
        chosen = { x: c.x, y: c.y, anchor: c.anchor, box: box, fits: true };
        break;
      }
      if (chosen !== null) {
        break;
      }
    }
    if (chosen === null) {
      const c = req.candidates[0];
      chosen = { x: c.x, y: c.y, anchor: c.anchor, box: labelBox(c.x, c.y, c.anchor, req.width), fits: false };
    }
    hard.push(chosen.box);
    placed[req.id] = chosen;
  }
  return placed;
}

// "128" for 134217728 bytes, "100.5" for a limit that is not a whole number of MiB.
export function limitMibText(bytes) {
  const mib = bytes / BYTES_PER_MIB;
  return Number.isInteger(mib) ? String(mib) : String(Math.round(mib * 100) / 100);
}

// ---------------------------------------------------------------- point text

function shownTime(display, original) {
  if (typeof display === "string") {
    return display;
  }
  if (typeof original === "string") {
    return original;
  }
  return COPY.shared.notRecorded;
}

function mibText(bytes) {
  return (bytes / BYTES_PER_MIB).toFixed(2);
}

// Readout lines for one point, from API fields only. context.limitBytes (optional) is the recorded
// memory limit used for the "above the limit" note.
export function describePoint(point, context) {
  const ctx = context || {};
  const lines = [];
  lines.push(fill(COPY.chart.pointSeq, { seq: isNum(point.seq) ? point.seq : COPY.shared.notRecorded }));
  lines.push(
    COPY.chart.pointCapture + ": " + shownTime(point.capture_time_display, point.capture_time),
  );
  if (typeof point.corrected_capture_time === "string") {
    lines.push(COPY.chart.pointCorrected + ": " + point.corrected_capture_time);
  }
  lines.push(
    COPY.chart.pointReceive + ": " + shownTime(point.receive_time_display, point.receive_time),
  );
  if (isNum(point.rss_bytes)) {
    let rss =
      COPY.chart.pointRss +
      ": " +
      fill(COPY.chart.pointRssValue, { mib: mibText(point.rss_bytes), bytes: point.rss_bytes });
    if (isNum(ctx.limitBytes) && ctx.limitBytes > 0 && point.rss_bytes > ctx.limitBytes) {
      rss += " (" + COPY.chart.pointAboveLimit + ")";
    }
    lines.push(rss);
  } else {
    lines.push(COPY.chart.pointRss + ": " + COPY.shared.notRecorded);
  }
  lines.push(COPY.shared.shownToMs);
  return lines;
}

// Original strings of a point, for the details view of the readout.
function pointOriginals(point) {
  const rows = [];
  if (typeof point.capture_time === "string") {
    rows.push([COPY.chart.originalCapture, point.capture_time]);
  }
  if (typeof point.corrected_capture_time === "string") {
    rows.push([COPY.chart.originalCorrected, point.corrected_capture_time]);
  }
  if (typeof point.receive_time === "string") {
    rows.push([COPY.chart.originalReceive, point.receive_time]);
  }
  return rows;
}

// ---------------------------------------------------------------- time values

function timeValue(display, original) {
  const shown = typeof display === "string" ? display : typeof original === "string" ? original : null;
  if (shown === null) {
    return textEl("span", "", COPY.shared.notRecorded);
  }
  const wrap = el("span", { class: "time-value" }, textEl("span", "mono", shown));
  if (typeof display === "string") {
    wrap.append(textEl("span", "muted time-note", " " + COPY.shared.shownToMs));
  } else {
    wrap.append(textEl("span", "muted time-note", " " + COPY.times.displayUnavailable));
  }
  if (typeof original === "string") {
    wrap.append(
      el(
        "details",
        { class: "original" },
        textEl("summary", "", COPY.times.originalSummary),
        textEl("span", "mono", original),
      ),
    );
  }
  return wrap;
}

function factRow(label, valueNode) {
  return el(
    "div",
    { class: "fact" },
    textEl("dt", "fact-label", label),
    el("dd", { class: "fact-value" }, valueNode),
  );
}

// ---------------------------------------------------------------- text below the chart

function runnerList(title, items) {
  const list = el("ul", { class: "runner-list" });
  for (const item of items) {
    const li = el("li", {}, textEl("span", "runner-text", item.text === null ? COPY.shared.notRecorded : item.text));
    if (item.truncated === true) {
      li.append(
        textEl(
          "span",
          "muted",
          " (" + fill(COPY.evidence.truncatedNote, { n: item.originalLength }) + ")",
        ),
      );
    }
    list.append(li);
  }
  return el(
    "div",
    { class: "runner-block" },
    textEl("p", "runner-block-title", COPY.evidence.runnerBlockTitle + ": " + title),
    list,
  );
}

function textTimesBlock(detail) {
  const markers = detail.termination_markers;
  const rows = markers && Array.isArray(markers.text_times) ? markers.text_times : [];
  const list = el("dl", { class: "facts" });
  for (const row of rows) {
    if (!row || typeof row.kind !== "string" || !Object.prototype.hasOwnProperty.call(COPY.times, row.kind)) {
      continue;
    }
    list.append(factRow(COPY.times[row.kind], timeValue(row.display, row.value)));
  }
  return list;
}

function belowChart(detail, chart) {
  const expType = detail.run ? detail.run.experiment_type : null;
  const box = el("div", { class: "chart-text" });

  if (isNum(chart.unplottable_samples)) {
    box.append(textEl("p", "chart-note", fill(COPY.chart.unplottableLine, { n: chart.unplottable_samples })));
  }
  if (chart.unknown_range_drop_present === true) {
    box.append(textEl("p", "chart-note", COPY.chart.unknownDrop));
  }

  box.append(textEl("h4", "subsection-title", COPY.chart.timesHeading));
  box.append(textTimesBlock(detail));

  if (expType === "A") {
    box.append(textEl("p", "chart-note", COPY.chart.positionNotEstablishedA));
  } else if (expType === "C") {
    box.append(textEl("p", "chart-note", COPY.chart.positionNotEstablishedC));
    box.append(textEl("p", "chart-note", COPY.chart.samplingMissGrowth));
    const sl = detail.runner_text ? detail.runner_text.sampling_limitations : null;
    if (sl && Array.isArray(sl.statements) && sl.statements.length > 0) {
      box.append(
        runnerList(
          COPY.chart.samplingLimitationsLabel,
          sl.statements.map((s) => ({
            text: s.text,
            truncated: s.text_truncated,
            originalLength: s.text_original_length,
          })),
        ),
      );
    }
  } else if (expType === "B") {
    const timing = detail.timing || {};
    if (timing.correction_available === false) {
      const reasonKey = "correctionReason_" + String(timing.correction_unavailable_reason);
      let line = COPY.chart.correctionUnavailable;
      if (Object.prototype.hasOwnProperty.call(COPY.chart, reasonKey)) {
        line += ": " + COPY.chart[reasonKey];
      }
      box.append(textEl("p", "chart-note", line));
    }
    const bMarkers = detail.termination_markers;
    const sigkill = bMarkers ? bMarkers.sigkill : null;
    if (sigkill && sigkill.drawn !== true) {
      const key = "sigkillReason_" + String(sigkill.not_drawn_reason);
      if (Object.prototype.hasOwnProperty.call(COPY.chart, key)) {
        box.append(textEl("p", "chart-note", fill(COPY.chart.sigkillNotDrawn, { reason: COPY.chart[key] })));
      }
    }
  }
  return box;
}

// ---------------------------------------------------------------- sample table

function sampleTable(points, chart) {
  const wrap = el("details", { class: "sample-table" }, textEl("summary", "", COPY.chart.tableTitle));
  const caption = textEl("caption", "", COPY.chart.tableCaption);
  if (chart.partial_view === true) {
    caption.textContent =
      COPY.chart.tableCaption +
      ". " +
      fill(COPY.chart.partialLine, {
        label: COPY.shared.partialView,
        total: isNum(chart.total_samples) ? chart.total_samples : COPY.shared.notRecorded,
        plotted: points.length,
      });
  }
  const head = el(
    "tr",
    {},
    textEl("th", "", COPY.chart.colSeq, { scope: "col" }),
    textEl("th", "", COPY.chart.colCapture, { scope: "col" }),
    textEl("th", "", COPY.chart.colOriginal, { scope: "col" }),
    textEl("th", "", COPY.chart.colRssMib, { scope: "col" }),
    textEl("th", "", COPY.chart.colRssBytes, { scope: "col" }),
  );
  const body = el("tbody");
  const table = el("table", { class: "table" }, caption, el("thead", {}, head), body);
  const status = textEl("p", "muted", "");
  const more = textEl("button", "button", COPY.chart.showMore, { type: "button" });
  let shown = 0;

  function addChunk() {
    const end = Math.min(points.length, shown + TABLE_CHUNK);
    for (let i = shown; i < end; i += 1) {
      const p = points[i];
      body.append(
        el(
          "tr",
          {},
          textEl("td", "mono", isNum(p.seq) ? p.seq : COPY.shared.notRecorded),
          textEl("td", "mono", shownTime(p.capture_time_display, p.capture_time)),
          textEl("td", "mono", typeof p.capture_time === "string" ? p.capture_time : COPY.shared.notRecorded),
          textEl("td", "mono", isNum(p.rss_bytes) ? mibText(p.rss_bytes) : COPY.shared.notRecorded),
          textEl("td", "mono", isNum(p.rss_bytes) ? p.rss_bytes : COPY.shared.notRecorded),
        ),
      );
    }
    shown = end;
    status.textContent = fill(COPY.chart.rowsShown, { shown: shown, total: points.length });
    if (shown >= points.length) {
      more.setAttribute("hidden", "");
    }
  }

  more.addEventListener("click", addChunk);
  addChunk();
  wrap.append(el("div", { class: "table-wrap" }, table), status, more);
  return wrap;
}

// ---------------------------------------------------------------- the plot

function buildPlot(detail, chart, points) {
  const axis = chart.x_axis && isNum(chart.x_axis.domain_min_ms) && isNum(chart.x_axis.domain_max_ms)
    ? chart.x_axis
    : {
        domain_min_ms: points[0].x_ms,
        domain_max_ms: points[points.length - 1].x_ms,
        ticks: [],
      };
  const eff = detail.docker_state && detail.docker_state.effective ? detail.docker_state.effective : null;
  const limitBytes = eff && isNum(eff.memory_limit_bytes) && eff.memory_limit_bytes > 0 ? eff.memory_limit_bytes : 0;
  const ctx = { limitBytes: limitBytes };
  let maxPoint = 0;
  for (const p of points) {
    if (p.rss_bytes > maxPoint) {
      maxPoint = p.rss_bytes;
    }
  }
  const dataMax = isNum(chart.y_data_max_bytes) ? chart.y_data_max_bytes : maxPoint;
  let yMaxBytes = Math.max(dataMax, limitBytes) * Y_HEADROOM;
  if (!(yMaxBytes > 0)) {
    yMaxBytes = BYTES_PER_MIB;
  }
  const yOf = (bytes) => PLOT_BOTTOM - (bytes / yMaxBytes) * PLOT_H;
  const xOf = (xMs) => xPosition(xMs, axis);

  const axisLabel = Object.prototype.hasOwnProperty.call(COPY.axisTitle, chart.time_axis)
    ? COPY.axisTitle[chart.time_axis]
    : COPY.shared.notRecorded;
  const runId = detail.run && typeof detail.run.run_id === "string" ? detail.run.run_id : COPY.shared.notRecorded;

  const svg = svgEl("svg", "chart-svg", {
    viewBox: "0 0 " + VIEW_W + " " + VIEW_H,
    role: "img",
    "aria-labelledby": "chart-title chart-desc",
    focusable: "false",
  });
  svg.append(
    svgEl("title", "", { id: "chart-title" }, fill(COPY.chart.titleTemplate, { run: runId, axis: axisLabel })),
    svgEl(
      "desc",
      "",
      { id: "chart-desc" },
      [
        fill(COPY.chart.descPlotted, { n: points.length }),
        chart.partial_view === true ? COPY.chart.descPartial : COPY.chart.descFull,
        fill(COPY.chart.descHighest, { mib: mibText(maxPoint) }),
      ].join(" "),
    ),
  );

  // y grid, ticks and labels
  const yTicks = niceYTicks(yMaxBytes / BYTES_PER_MIB);
  for (const tick of yTicks) {
    const y = yOf(tick.value * BYTES_PER_MIB);
    svg.append(
      svgEl("line", "chart-grid", { x1: M_LEFT, x2: M_LEFT + PLOT_W, y1: r2(y), y2: r2(y) }),
      svgEl("text", "chart-tick-label", { x: M_LEFT - 8, y: r2(y + 4), "text-anchor": "end" }, tick.text),
    );
  }
  // axes
  svg.append(
    svgEl("line", "chart-axis", { x1: M_LEFT, x2: M_LEFT, y1: M_TOP, y2: PLOT_BOTTOM }),
    svgEl("line", "chart-axis", { x1: M_LEFT, x2: M_LEFT + PLOT_W, y1: PLOT_BOTTOM, y2: PLOT_BOTTOM }),
  );
  // x ticks: the mark position is the tick's own x_ms. The label text is the clock part (HH:MM:SS, UTC)
  // of the tick's own label; the date is shown separately below the chart. A label that is not a UTC
  // timestamp text is shown as it is. Labels that would collide are thinned out; the marks stay.
  const ticks = Array.isArray(axis.ticks) ? axis.ticks : [];
  const tickItems = [];
  for (const tick of ticks) {
    if (!tick || !isNum(tick.x_ms)) {
      continue;
    }
    const x = xOf(tick.x_ms);
    svg.append(svgEl("line", "chart-axis", { x1: r2(x), x2: r2(x), y1: PLOT_BOTTOM, y2: PLOT_BOTTOM + 5 }));
    if (typeof tick.label === "string") {
      const parts = splitUtcText(tick.label);
      const text = parts !== null ? parts.clock : tick.label;
      const width = textWidth(text);
      tickItems.push({ x: x, text: text, width: width, anchor: tickAnchor(x, width, VIEW_W) });
    }
  }
  const shownTicks = thinTickLabels(tickItems, 10);
  tickItems.forEach((item, i) => {
    if (shownTicks.has(i)) {
      svg.append(
        svgEl(
          "text",
          "chart-tick-label chart-x-tick-label",
          { x: r2(item.x), y: PLOT_BOTTOM + TICK_LABEL_DY, "text-anchor": item.anchor },
          item.text,
        ),
      );
    }
  });
  // axis titles
  svg.append(
    svgEl(
      "text",
      "chart-axis-title chart-x-title",
      { x: M_LEFT + PLOT_W / 2, y: PLOT_BOTTOM + AXIS_TITLE_DY, "text-anchor": "middle" },
      axisLabel,
    ),
    svgEl(
      "text",
      "chart-axis-title",
      { x: 14, y: M_TOP + PLOT_H / 2, "text-anchor": "middle", transform: "rotate(-90 14 " + (M_TOP + PLOT_H / 2) + ")" },
      COPY.axis.yTitle,
    ),
  );

  // Memory limit line and SIGKILL marker, with short in-plot labels. The SIGKILL marker is drawn for
  // Experiment B with sigkill.drawn only. The full descriptions are in the DOM legend below the chart.
  const expType = detail.run ? detail.run.experiment_type : null;
  const sigkill = detail.termination_markers ? detail.termination_markers.sigkill : null;
  const sigkillDrawn = expType === "B" && !!sigkill && sigkill.drawn === true && isNum(sigkill.x_ms);
  const bounds = { l: M_LEFT, r: M_LEFT + PLOT_W, t: M_TOP, b: PLOT_BOTTOM };
  const fixedBoxes = [];
  const sigkillX = sigkillDrawn ? xOf(sigkill.x_ms) : 0;
  if (sigkillDrawn) {
    fixedBoxes.push({ l: sigkillX, r: sigkillX, t: M_TOP, b: PLOT_BOTTOM });
  }
  const pointBoxes = points.map((p) => {
    const px = xOf(p.x_ms);
    const py = yOf(p.rss_bytes);
    return { l: px - 5, r: px + 5, t: py - 5, b: py + 5 };
  });
  const limitY = limitBytes > 0 ? yOf(limitBytes) : 0;
  if (limitBytes > 0) {
    // no label may sit on the limit line
    fixedBoxes.push({ l: M_LEFT, r: M_LEFT + PLOT_W, t: limitY, b: limitY });
  }
  const limitText = fill(COPY.chart.limitLabel, { mib: limitMibText(limitBytes) });
  const requests = [];
  if (limitBytes > 0) {
    const left = M_LEFT + 4;
    const right = M_LEFT + PLOT_W - 4;
    requests.push({
      id: "limit",
      width: textWidth(limitText),
      candidates: [
        { x: left, y: limitY - 5, anchor: "start" },
        { x: left, y: limitY + 13, anchor: "start" },
        { x: right, y: limitY - 5, anchor: "end" },
        { x: right, y: limitY + 13, anchor: "end" },
      ],
    });
  }
  if (sigkillDrawn) {
    const onRight = sigkillX > M_LEFT + PLOT_W / 2;
    const near = { x: onRight ? sigkillX - 5 : sigkillX + 5, anchor: onRight ? "end" : "start" };
    const far = { x: onRight ? sigkillX + 5 : sigkillX - 5, anchor: onRight ? "start" : "end" };
    const candidates = [];
    for (const side of [near, far]) {
      for (let row = 0; row < LABEL_ROWS; row += 1) {
        candidates.push({ x: side.x, y: M_TOP + 12 + row * LABEL_ROW_STEP, anchor: side.anchor });
      }
    }
    requests.push({ id: "sigkill", width: textWidth(COPY.axis.sigkillLabel), candidates: candidates });
  }
  const placed = placeLabels(requests, bounds, fixedBoxes, pointBoxes);

  if (limitBytes > 0) {
    const at = placed.limit;
    svg.append(
      svgEl("line", "chart-limit", {
        x1: M_LEFT,
        x2: M_LEFT + PLOT_W,
        y1: r2(limitY),
        y2: r2(limitY),
        "stroke-dasharray": "8 5",
      }),
      svgEl(
        "text",
        "chart-limit-label chart-annotation",
        { x: r2(at.x), y: r2(at.y), "text-anchor": at.anchor },
        limitText,
      ),
    );
  }
  if (sigkillDrawn) {
    const at = placed.sigkill;
    svg.append(
      svgEl("line", "chart-sigkill", {
        x1: r2(sigkillX),
        x2: r2(sigkillX),
        y1: M_TOP,
        y2: PLOT_BOTTOM,
        "stroke-dasharray": "2 3",
      }),
      svgEl(
        "text",
        "chart-sigkill-label chart-annotation",
        { x: r2(at.x), y: r2(at.y), "text-anchor": at.anchor },
        COPY.axis.sigkillLabel,
      ),
    );
  }

  // Dates for the line below the chart: from the tick texts, the first and last plotted Sample and the
  // drawn SIGKILL time, so a span over midnight shows both dates.
  const timeKey = chart.time_axis === "corrected" ? "corrected_capture_time" : "capture_time";
  const dateTexts = ticks.map((t) => (t ? t.label : null));
  dateTexts.push(points[0][timeKey], points[points.length - 1][timeKey]);
  if (sigkillDrawn) {
    dateTexts.push(sigkill.time_display, sigkill.time);
  }
  const dates = distinctDates(dateTexts);
  // segments from segment_before; gap marks where gap_before is true
  const runs = [];
  const gapIndexes = [];
  let anyDashed = false;
  let current = null;
  for (let i = 1; i < points.length; i += 1) {
    const style = points[i].segment_before;
    if (points[i].gap_before === true) {
      current = null;
      gapIndexes.push(i);
    } else if (style === "solid" || style === "dashed") {
      if (current !== null && current.segment === style && current.last === i - 1) {
        current.indexes.push(i);
        current.last = i;
      } else {
        current = { segment: style, indexes: [i - 1, i], last: i };
        runs.push(current);
      }
      if (style === "dashed") {
        anyDashed = true;
      }
    } else {
      current = null;
    }
  }
  for (const run of runs) {
    const coords = run.indexes.map((i) => r2(xOf(points[i].x_ms)) + "," + r2(yOf(points[i].rss_bytes)));
    const attrs = { points: coords.join(" "), fill: "none" };
    if (run.segment === "dashed") {
      attrs["stroke-dasharray"] = "8 5";
    }
    svg.append(svgEl("polyline", run.segment === "dashed" ? "chart-line chart-line-dashed" : "chart-line", attrs));
  }
  for (const i of gapIndexes) {
    const x = (xOf(points[i - 1].x_ms) + xOf(points[i].x_ms)) / 2;
    svg.append(
      svgEl("line", "chart-gap", {
        x1: r2(x),
        x2: r2(x),
        y1: M_TOP,
        y2: PLOT_BOTTOM,
        "stroke-dasharray": "2 4",
      }),
      svgEl("text", "chart-gap-label", { x: r2(x), y: M_TOP - 6, "text-anchor": "middle" }, COPY.chart.gapLabel),
    );
  }

  // points
  const drawAll = points.length <= MAX_CIRCLES;
  const circles = [];
  const texts = points.map((p) => describePoint(p, ctx));
  if (drawAll) {
    points.forEach((p, i) => {
      const c = svgEl("circle", "chart-point", {
        id: "chart-pt-" + i,
        cx: r2(xOf(p.x_ms)),
        cy: r2(yOf(p.rss_bytes)),
        r: 3.5,
        role: "img",
        "aria-label": texts[i].join("; "),
      });
      circles.push(c);
      svg.append(c);
    });
  }
  const ring = svgEl("circle", "chart-active is-hidden", { id: "chart-active-point", cx: 0, cy: 0, r: 7 });
  svg.append(ring);

  return {
    svg: svg,
    axis: axis,
    ctx: ctx,
    circles: circles,
    ring: ring,
    drawAll: drawAll,
    texts: texts,
    anyDashed: anyDashed,
    limitBytes: limitBytes,
    limitMib: limitMibText(limitBytes),
    sigkillDrawn: sigkillDrawn,
    dates: dates,
    timeAxis: chart.time_axis,
    xOf: xOf,
    yOf: yOf,
  };
}

function plotInteractive(plot, points) {
  const xs = points.map((p) => p.x_ms);
  const readout = el("div", { class: "chart-readout" });
  const live = el("div", {
    class: "visually-hidden",
    attrs: { "aria-live": "polite", "aria-atomic": "true" },
  });
  const frame = el(
    "div",
    {
      class: "chart-plot",
      attrs: {
        tabindex: "0",
        role: "application",
        "aria-label": COPY.chart.plotLabel,
      },
    },
    plot.svg,
  );
  let active = -1;

  function showHint() {
    clear(readout);
    readout.append(textEl("p", "muted", COPY.chart.hint));
  }

  function setActive(index) {
    if (index < 0 || index >= points.length || index === active) {
      return;
    }
    if (plot.drawAll && active >= 0) {
      plot.circles[active].setAttribute("class", "chart-point");
    }
    active = index;
    const p = points[index];
    const lines = plot.texts[index];
    if (plot.drawAll) {
      plot.circles[index].setAttribute("class", "chart-point point-active");
      plot.ring.setAttribute("aria-hidden", "true");
      frame.setAttribute("aria-activedescendant", "chart-pt-" + index);
    } else {
      plot.ring.removeAttribute("aria-hidden");
      plot.ring.setAttribute("role", "img");
      plot.ring.setAttribute("aria-label", lines.join("; "));
      plot.ring.setAttribute("tabindex", "-1");
      frame.setAttribute("aria-activedescendant", "chart-active-point");
    }
    plot.ring.setAttribute("cx", r2(plot.xOf(p.x_ms)));
    plot.ring.setAttribute("cy", r2(plot.yOf(p.rss_bytes)));
    plot.ring.setAttribute("class", "chart-active");

    clear(readout);
    readout.append(textEl("p", "readout-title", COPY.chart.readoutTitle));
    const list = el("ul", { class: "readout-lines" });
    for (const line of lines) {
      list.append(textEl("li", "", line));
    }
    readout.append(list);
    const originals = pointOriginals(p);
    if (originals.length > 0) {
      const details = el("details", { class: "original" }, textEl("summary", "", COPY.times.originalSummary));
      for (const pair of originals) {
        details.append(el("p", {}, textEl("span", "muted", pair[0] + ": "), textEl("span", "mono", pair[1])));
      }
      readout.append(details);
    }
    live.textContent = lines.join(". ");
  }

  frame.addEventListener("keydown", (event) => {
    let target = -1;
    const from = active < 0 ? 0 : active;
    if (event.key === "ArrowRight") {
      target = active < 0 ? 0 : Math.min(points.length - 1, from + 1);
    } else if (event.key === "ArrowLeft") {
      target = active < 0 ? 0 : Math.max(0, from - 1);
    } else if (event.key === "Home") {
      target = 0;
    } else if (event.key === "End") {
      target = points.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    setActive(target);
  });
  frame.addEventListener("focus", () => {
    if (active < 0) {
      setActive(0);
    }
  });
  plot.svg.addEventListener("pointermove", (event) => {
    const rect = plot.svg.getBoundingClientRect();
    if (!(rect.width > 0)) {
      return;
    }
    const svgX = ((event.clientX - rect.left) / rect.width) * VIEW_W;
    const xMs = xMsAt(svgX, plot.axis);
    setActive(xMs === null ? 0 : nearestIndex(xs, xMs));
  });

  showHint();
  return { frame: frame, readout: readout, live: live };
}

function legend(plot) {
  const items = [];
  if (plot.anyDashed) {
    items.push(
      el(
        "li",
        {},
        el("span", { class: "swatch swatch-dashed", attrs: { "aria-hidden": "true" } }),
        textEl("span", "", COPY.axis.dashedLegend),
      ),
    );
  }
  if (plot.limitBytes > 0) {
    items.push(
      el(
        "li",
        {},
        el("span", { class: "swatch swatch-limit", attrs: { "aria-hidden": "true" } }),
        textEl(
          "span",
          "",
          fill(COPY.chart.legendLimit, {
            label: fill(COPY.chart.limitLabel, { mib: plot.limitMib }),
            bytes: plot.limitBytes,
          }),
        ),
      ),
    );
  }
  if (plot.sigkillDrawn) {
    items.push(
      el(
        "li",
        {},
        el("span", { class: "swatch swatch-sigkill", attrs: { "aria-hidden": "true" } }),
        textEl("span", "", fill(COPY.axis.sigkillLegend, { label: COPY.axis.sigkillLabel })),
      ),
    );
  }
  if (items.length === 0) {
    return null;
  }
  return el("ul", { class: "chart-legend", attrs: { "aria-label": COPY.chart.legendLabel } }, items);
}

// Date line and time-axis explanation, below the chart (kept out of the SVG so they never crowd the axis).
function axisNotes(plot) {
  const box = el("div", { class: "chart-axis-notes" });
  if (plot.dates.length === 1) {
    box.append(textEl("p", "chart-axis-note chart-date", fill(COPY.chart.axisDateOne, { date: plot.dates[0] })));
  } else if (plot.dates.length > 1) {
    box.append(
      textEl(
        "p",
        "chart-axis-note chart-date",
        fill(COPY.chart.axisDateMany, { dates: plot.dates.join(COPY.chart.axisDateJoin) }),
      ),
    );
  }
  const note = plot.timeAxis === "corrected" ? COPY.axisNote.corrected : COPY.axisNote.raw;
  box.append(textEl("p", "chart-axis-note chart-axis-explain", note));
  return box;
}

// Builds the timeline section for one run detail (the GET /api/runs/{id} object).
export function renderChart(detail) {
  const chart = detail && detail.chart ? detail.chart : {};
  const section = el(
    "section",
    { class: "chart-section", attrs: { "aria-labelledby": "chart-heading" } },
    textEl("h3", "section-title", COPY.chart.heading, { id: "chart-heading" }),
  );
  const rawPoints = Array.isArray(chart.points) ? chart.points : [];
  // A point without a numeric x_ms or rss_bytes is never drawn (no invented points).
  const points = rawPoints.filter((p) => p && isNum(p.x_ms) && isNum(p.rss_bytes));

  if (points.length === 0) {
    section.append(el("div", { class: "state chart-empty", attrs: { role: "status" } }, COPY.chart.empty));
  } else {
    if (chart.partial_view === true) {
      section.append(
        textEl(
          "p",
          "partial-label",
          fill(COPY.chart.partialLine, {
            label: COPY.shared.partialView,
            total: isNum(chart.total_samples) ? chart.total_samples : COPY.shared.notRecorded,
            plotted: points.length,
          }),
        ),
      );
    }
    const plot = buildPlot(detail, chart, points);
    const ui = plotInteractive(plot, points);
    section.append(el("div", { class: "chart-wrap" }, ui.frame), axisNotes(plot));
    const leg = legend(plot);
    if (leg !== null) {
      section.append(leg);
    }
    section.append(ui.live, ui.readout);
    section.append(sampleTable(points, chart));
  }
  section.append(belowChart(detail || {}, chart));
  return section;
}
