// Evidence vs Uncertainty panel. Two sections (KNOWN, UNKNOWN OR LIMITED), each
// entry prefixed with its text label, and a visually separate block for text the
// Runner recorded. Everything comes from the API detail object; every artifact
// value is shown as text only, and no recorded timestamp is parsed here.

import { COPY, fill } from "/static/copy.js";
import { el, textEl } from "/static/dom.js";
import { oomState, oomStatements } from "/static/oom-state.js";

const BYTES_PER_MIB = 1048576;

function isNum(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

// A value exactly as recorded: strings in quotes, null as "not recorded".
function recordedText(value) {
  if (value === null || value === undefined) {
    return COPY.shared.notRecorded;
  }
  return JSON.stringify(value);
}

function plainText(value) {
  if (value === null || value === undefined) {
    return COPY.shared.notRecorded;
  }
  return String(value);
}

function boolText(value) {
  if (value === true) {
    return COPY.shared.boolTrue;
  }
  if (value === false) {
    return COPY.shared.boolFalse;
  }
  return COPY.shared.notRecorded;
}

function timeValue(display, original) {
  const shown = typeof display === "string" ? display : typeof original === "string" ? original : null;
  if (shown === null) {
    return textEl("span", "", COPY.shared.notRecorded);
  }
  const wrap = el("span", { class: "time-value" }, textEl("span", "mono", shown));
  wrap.append(
    textEl(
      "span",
      "muted time-note",
      " " + (typeof display === "string" ? COPY.shared.shownToMs : COPY.times.displayUnavailable),
    ),
  );
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

// ---------------------------------------------------------------- entries

// One entry: text label first (in addition to colour), then the content.
function entry(kind, label, ...content) {
  const known = kind === "known";
  return el(
    "li",
    { class: known ? "ev-entry ev-known" : "ev-entry ev-unknown" },
    textEl(
      "span",
      known ? "ev-tag ev-tag-known" : "ev-tag ev-tag-unknown",
      known ? COPY.shared.known : COPY.shared.unknownOrLimited,
    ),
    " ",
    label === null ? null : textEl("span", "ev-label", label + ": "),
    content,
  );
}

function sentence(kind, text) {
  return entry(kind, null, textEl("span", "ev-value", text));
}

function note(text) {
  return textEl("span", "muted ev-note", " " + text);
}

function rssText(row) {
  return fill(COPY.evidence.rssValue, {
    mib: isNum(row.rss_bytes) ? (row.rss_bytes / BYTES_PER_MIB).toFixed(2) : COPY.shared.notRecorded,
    bytes: plainText(row.rss_bytes),
    seq: plainText(row.seq),
  });
}

// ---------------------------------------------------------------- KNOWN

function buildKnown(detail, known, unknown) {
  const ds = detail.docker_state || {};
  const eff = ds.effective || null;
  const es = detail.evidence_summary || {};
  const source = has(COPY.dockerSource, ds.source) ? COPY.dockerSource[ds.source] : COPY.shared.notRecorded;
  const sourceNote = () => note("(" + fill(COPY.evidence.stateSource, { source: source }) + ")");

  function simple(label, value, text, definition, extra) {
    if (value === null || value === undefined) {
      unknown.push(entry("unknown", label, textEl("span", "ev-value", COPY.shared.notRecorded)));
      return;
    }
    known.push(
      entry(
        "known",
        label,
        textEl("span", "ev-value mono", text),
        extra ? extra() : null,
        definition ? note("(" + definition + ")") : null,
      ),
    );
  }

  simple(COPY.evidence.dockerStatus, eff ? eff.status : null, eff ? String(eff.status) : "", null, sourceNote);
  simple(COPY.evidence.exitCode, eff ? eff.exit_code : null, eff ? String(eff.exit_code) : "", null, sourceNote);
  // The shared rule (oom-state.js) also drives the incident Docker state section.
  const oom = oomStatements(oomState(ds), (detail.run || {}).experiment_type === "C", eff ? eff.exit_code : null);
  if (oom.rowValue !== null) {
    // conflicting sources: neither value is KNOWN; both are shown
    unknown.push(entry("unknown", COPY.evidence.oomKilled, textEl("span", "ev-value mono", oom.rowValue)));
  } else {
    simple(COPY.evidence.oomKilled, eff ? eff.oom_killed : null, eff ? boolText(eff.oom_killed) : "", null, sourceNote);
  }
  simple(
    COPY.evidence.recordCount,
    es.valid_record_count,
    String(es.valid_record_count),
    COPY.evidence.recordCountDef,
  );
  simple(COPY.evidence.sampleCount, es.sample_count, String(es.sample_count), COPY.evidence.sampleCountDef);

  function rss(label, row, definition) {
    if (!row || !isNum(row.rss_bytes)) {
      unknown.push(entry("unknown", label, textEl("span", "ev-value", COPY.shared.notRecorded)));
      return;
    }
    known.push(
      entry(
        "known",
        label,
        textEl("span", "ev-value mono", rssText(row) + " "),
        timeValue(row.capture_time_display, row.capture_time),
        note("(" + definition + ")"),
      ),
    );
  }
  rss(COPY.evidence.firstRss, es.first_rss, COPY.evidence.firstRssDef);
  rss(COPY.evidence.highestRss, es.highest_rss, COPY.evidence.highestRssDef);
  rss(
    COPY.evidence.lastRss,
    es.last_rss,
    es.last_rss && es.last_rss.basis === "last_persisted"
      ? COPY.evidence.lastRssDefPersisted
      : COPY.evidence.lastRssDefFallback,
  );

  // Experiment C: the termination mechanism only when the shared rule allows it.
  if (oom.mechanism !== null) {
    known.push(sentence("known", oom.mechanism));
  }
  if (oom.conflict !== null) {
    unknown.push(sentence("unknown", oom.conflict));
  }
  if (oom.singleNote !== null) {
    unknown.push(sentence("unknown", oom.singleNote));
  }
}

// ---------------------------------------------------------------- UNKNOWN OR LIMITED

function textTime(detail, kind) {
  const markers = detail.termination_markers;
  const rows = markers && Array.isArray(markers.text_times) ? markers.text_times : [];
  for (const row of rows) {
    if (row && row.kind === kind) {
      return row;
    }
  }
  return null;
}

function derivedIntervalEntry(detail, di) {
  const timing = detail.timing || {};
  const derived = timing.derived || {};
  const kindKey = "derivedKind_" + String(di.kind);
  const kindLabel = has(COPY.evidence, kindKey) ? COPY.evidence[kindKey] : COPY.shared.notRecorded;
  const sampleKey = "derivedSample_" + String(di.sample);
  const sampleLabel = has(COPY.evidence, sampleKey) ? COPY.evidence[sampleKey] : null;

  const lines = [];
  lines.push(el("li", {}, textEl("strong", "", COPY.shared.derivedNotProven)));

  // from
  if (di.kind === "finished_at_to_sigkill") {
    lines.push(
      el(
        "li",
        {},
        textEl("span", "muted", fill(COPY.evidence.derivedFrom, { time: COPY.times.docker_finished_at }) + " "),
        timeValue(di.from_time_display, di.from_time),
      ),
    );
  } else {
    lines.push(
      el(
        "li",
        {},
        textEl(
          "span",
          "muted",
          fill(COPY.evidence.derivedFrom, { time: COPY.chart.pointCapture }) + " ",
        ),
        timeValue(di.from_time_display, di.from_time),
      ),
    );
    if (typeof di.from_corrected_time === "string") {
      lines.push(
        el(
          "li",
          {},
          textEl("span", "muted", COPY.chart.pointCorrected + ": "),
          textEl("span", "mono", di.from_corrected_time),
        ),
      );
    }
  }

  // to
  let toLabel = COPY.shared.notRecorded;
  let toDisplay = di.to_time_display;
  let toOriginal = di.to_time;
  if (di.to_kind === "sigkill_sent") {
    toLabel = COPY.times.sigkill_sent;
    if (typeof toDisplay !== "string") {
      toDisplay = timing.sigkill_sent_time_display;
    }
    if (typeof toOriginal !== "string") {
      toOriginal = timing.sigkill_sent_time;
    }
  } else if (di.to_kind === "docker_finished_at") {
    toLabel = COPY.times.docker_finished_at;
    if (typeof toOriginal !== "string") {
      toOriginal = derived.finished_at_source === "manifest" ? timing.container_finished_at : timing.state_finished_at;
    }
  }
  lines.push(
    el(
      "li",
      {},
      textEl("span", "muted", fill(COPY.evidence.derivedTo, { time: toLabel }) + " "),
      timeValue(toDisplay, toOriginal),
    ),
  );

  lines.push(
    el(
      "li",
      {},
      isNum(di.difference_ms)
        ? fill(COPY.evidence.derivedDifference, { ms: di.difference_ms })
        : COPY.evidence.derivedDifferenceNone,
    ),
  );
  lines.push(
    el(
      "li",
      {},
      isNum(di.uncertainty_ms)
        ? fill(COPY.evidence.derivedUncertainty, { ms: di.uncertainty_ms })
        : COPY.evidence.derivedUncertaintyNone,
    ),
  );
  if (di.kind === "sample_to_sigkill") {
    lines.push(el("li", {}, fill(COPY.evidence.derivedTolerance, { ms: recordedText(di.tolerance_ms) })));
    if (di.reason === "uncertainty_not_recorded") {
      lines.push(el("li", {}, COPY.evidence.derivedReasonUncertainty));
    }
    if (di.ordering === "not_established") {
      lines.push(el("li", {}, COPY.evidence.derivedOrderNotEstablished));
    }
  } else {
    lines.push(el("li", {}, COPY.evidence.derivedDockerNoBound));
  }
  lines.push(el("li", {}, COPY.evidence.derivedNoOrder));

  return entry(
    "unknown",
    sampleLabel === null ? kindLabel : kindLabel + " (" + sampleLabel + ")",
    el("ul", { class: "ev-sub" }, lines),
  );
}

function buildUnknown(detail, unknown) {
  const run = detail.run || {};
  const expType = run.experiment_type;
  const ds = detail.docker_state || {};
  const es = detail.evidence_summary || {};
  const timing = detail.timing || {};
  const runnerText = detail.runner_text || {};

  // the unobserved interval note, unchanged
  unknown.push(
    entry(
      "unknown",
      COPY.evidence.unobservedLabel,
      textEl("span", "ev-value", typeof es.unobserved_interval_note === "string" ? es.unobserved_interval_note : COPY.shared.notRecorded),
    ),
  );

  // Docker discrepancies: field name and both values
  const discrepancies = Array.isArray(ds.discrepancies) ? ds.discrepancies : [];
  for (const d of discrepancies) {
    unknown.push(
      entry(
        "unknown",
        COPY.shared.discrepancy,
        textEl(
          "span",
          "ev-value",
          fill(COPY.evidence.discrepancyLine, {
            field: plainText(d.field),
            manifest: plainText(d.manifest_value),
            raw: plainText(d.raw_value),
          }),
        ),
      ),
    );
  }

  // conflicting clock offsets
  const conflict = timing.clock_offset_conflict;
  if (conflict && typeof conflict === "object") {
    unknown.push(
      entry(
        "unknown",
        COPY.shared.discrepancy,
        textEl(
          "span",
          "ev-value",
          fill(COPY.evidence.clockConflictLine, { a: plainText(conflict.sample_timing), b: plainText(conflict.manifest) }),
        ),
      ),
    );
    unknown.push(sentence("unknown", COPY.evidence.derivedSuppressed));
  }

  if (es.record_count_mismatch === true) {
    unknown.push(
      sentence(
        "unknown",
        fill(COPY.evidence.recordCountMismatch, {
          manifest: plainText(es.manifest_record_count),
          valid: plainText(es.valid_record_count),
        }),
      ),
    );
  }

  if (expType === "B") {
    // correction and clock statements
    if (timing.correction_available === false) {
      unknown.push(sentence("unknown", COPY.evidence.orderNotEstablishedB));
    }
    if (timing.clock_offset_ms === null || timing.clock_offset_ms === undefined) {
      unknown.push(
        entry("unknown", COPY.evidence.clockOffset, textEl("span", "ev-value", COPY.shared.notRecorded)),
      );
    }
    if (timing.clock_uncertainty_ms === null || timing.clock_uncertainty_ms === undefined) {
      unknown.push(
        entry("unknown", COPY.evidence.clockUncertainty, textEl("span", "ev-value", COPY.evidence.derivedUncertaintyNone)),
      );
    }
    unknown.push(sentence("unknown", fill(COPY.evidence.bComparisons, { label: COPY.shared.derivedNotProven })));
    unknown.push(sentence("unknown", COPY.evidence.bNotBounded));
    unknown.push(sentence("unknown", COPY.evidence.clocksNotSynchronised));
    unknown.push(sentence("unknown", COPY.evidence.clockScope));

    const intervals = timing.derived && Array.isArray(timing.derived.intervals) ? timing.derived.intervals : [];
    for (const di of intervals) {
      if (di && typeof di === "object") {
        unknown.push(derivedIntervalEntry(detail, di));
      }
    }

    // tolerance: ordering unavailable, value as recorded
    if (typeof timing.tolerance_status === "string" && timing.tolerance_status !== "valid") {
      const key = "toleranceUnavailable_" + timing.tolerance_status;
      if (has(COPY.evidence, key)) {
        unknown.push(
          entry(
            "unknown",
            null,
            textEl("span", "ev-value", COPY.evidence[key]),
            note(fill(COPY.evidence.toleranceRecorded, { value: recordedText(timing.tolerance_ms) })),
          ),
        );
      }
    }
  } else {
    // Experiment A, C or unknown: raw values stay separate, no interval, no order
    const kinds = ["crash_requested", "last_persisted_capture", "latest_capture", "docker_finished_at"];
    for (const kind of kinds) {
      const row = textTime(detail, kind);
      if (row !== null) {
        unknown.push(entry("unknown", COPY.times[kind], timeValue(row.display, row.value)));
      }
    }
    unknown.push(sentence("unknown", COPY.evidence.rawValuesSeparate));
    unknown.push(sentence("unknown", COPY.evidence.orderNotEstablishedAC));
  }

  if (expType === "C") {
    unknown.push(sentence("unknown", COPY.evidence.growthReasonNotEstablished));
    unknown.push(sentence("unknown", COPY.evidence.samplingGrowth));
    unknown.push(
      sentence("unknown", fill(COPY.evidence.samplingNote, { block: COPY.evidence.runnerBlockTitle })),
    );
    const sl = runnerText.sampling_limitations;
    if (sl && isNum(sl.sample_interval_ms)) {
      unknown.push(sentence("unknown", fill(COPY.evidence.samplingInterval, { ms: sl.sample_interval_ms })));
    }
    if (sl && isNum(sl.approx_leak_rate_bytes_per_s)) {
      unknown.push(sentence("unknown", fill(COPY.evidence.samplingLeakRate, { n: sl.approx_leak_rate_bytes_per_s })));
    }
  }

  // unreconciled drops and Malformed_Lines
  const dropCount = es.unreconciled_drop_count;
  const malformed = es.malformed_line_count;
  if (dropCount === null || dropCount === undefined) {
    unknown.push(entry("unknown", COPY.evidence.dropCountLabel, textEl("span", "ev-value", COPY.shared.notRecorded)));
  } else if (dropCount > 0) {
    unknown.push(
      entry(
        "unknown",
        null,
        textEl("span", "ev-value", fill(COPY.evidence.dropCount, { n: dropCount })),
        note(fill(COPY.evidence.dropUncertain, { value: boolText(es.unreconciled_drop_uncertain) })),
      ),
    );
    const drops = Array.isArray(es.unreconciled_drops) ? es.unreconciled_drops : [];
    for (const d of drops) {
      unknown.push(
        sentence(
          "unknown",
          fill(COPY.evidence.dropItem, {
            count: plainText(d.count),
            uncertain: boolText(d.uncertain),
            reason: plainText(d.reason),
          }),
        ),
      );
    }
    if (es.unreconciled_drops_truncated === true) {
      unknown.push(
        sentence(
          "unknown",
          fill(COPY.evidence.listTruncated, { shown: drops.length, total: plainText(es.unreconciled_drops_total) }),
        ),
      );
    }
  }
  if (malformed === null || malformed === undefined) {
    unknown.push(entry("unknown", COPY.evidence.malformedLabel, textEl("span", "ev-value", COPY.shared.notRecorded)));
  } else if (malformed > 0 || (isNum(dropCount) && dropCount > 0)) {
    unknown.push(sentence("unknown", fill(COPY.evidence.malformedCount, { n: malformed })));
  }

  unknown.push(sentence("unknown", COPY.evidence.rootCause));
}

// ---------------------------------------------------------------- Runner block

function runnerEntry(label, value, truncated, originalLength) {
  const body = el(
    "div",
    { class: "runner-item" },
    textEl("p", "runner-item-label", label),
    textEl("p", "runner-text", typeof value === "string" ? value : COPY.shared.notRecorded),
  );
  if (typeof value === "string" && truncated === true) {
    body.append(textEl("p", "muted", fill(COPY.evidence.truncatedNote, { n: plainText(originalLength) })));
  }
  return body;
}

function runnerList(label, items, total, truncated) {
  const body = el("div", { class: "runner-item" }, textEl("p", "runner-item-label", label));
  if (!Array.isArray(items) || items.length === 0) {
    body.append(textEl("p", "runner-text", COPY.evidence.runnerNone));
    return body;
  }
  const list = el("ul", { class: "runner-list" });
  for (const item of items) {
    const li = el("li", {}, textEl("span", "runner-text", typeof item.text === "string" ? item.text : COPY.shared.notRecorded));
    if (item.text_truncated === true) {
      li.append(textEl("span", "muted", " (" + fill(COPY.evidence.truncatedNote, { n: plainText(item.text_original_length) }) + ")"));
    }
    list.append(li);
  }
  body.append(list);
  if (truncated === true) {
    body.append(textEl("p", "muted", fill(COPY.evidence.listTruncated, { shown: items.length, total: plainText(total) })));
  }
  return body;
}

function buildRunnerBlock(detail) {
  const rt = detail.runner_text || {};
  const block = el(
    "section",
    { class: "runner-block", attrs: { "aria-labelledby": "runner-block-title" } },
    textEl("h4", "runner-block-title", COPY.evidence.runnerBlockTitle, { id: "runner-block-title" }),
    runnerEntry(
      COPY.evidence.terminationDescription,
      rt.termination_description,
      rt.termination_description_truncated,
      rt.termination_description_original_length,
    ),
    runnerEntry(COPY.evidence.causeLabel, rt.cause_label, rt.cause_label_truncated, rt.cause_label_original_length),
    runnerList(COPY.evidence.limitations, rt.limitations, rt.limitations_total, rt.limitations_truncated),
  );
  const sl = rt.sampling_limitations;
  if (sl && typeof sl === "object") {
    block.append(runnerList(COPY.evidence.statements, sl.statements, sl.statements_total, sl.statements_truncated));
  }
  return block;
}

// ---------------------------------------------------------------- public

// Builds the panel for one run detail (the GET /api/runs/{id} object).
export function renderEvidencePanel(detail) {
  const data = detail || {};
  const known = [];
  const unknown = [];
  buildKnown(data, known, unknown);
  buildUnknown(data, unknown);

  return el(
    "section",
    { class: "evidence-panel", attrs: { "aria-labelledby": "evidence-heading" } },
    textEl("h3", "section-title", COPY.evidence.heading, { id: "evidence-heading" }),
    el(
      "section",
      { class: "ev-section ev-section-known", attrs: { "aria-labelledby": "ev-known-title" } },
      textEl("h4", "ev-section-title", COPY.shared.known, { id: "ev-known-title" }),
      el("ul", { class: "ev-list" }, known),
    ),
    el(
      "section",
      { class: "ev-section ev-section-unknown", attrs: { "aria-labelledby": "ev-unknown-title" } },
      textEl("h4", "ev-section-title", COPY.shared.unknownOrLimited, { id: "ev-unknown-title" }),
      el("ul", { class: "ev-list" }, unknown),
    ),
    buildRunnerBlock(data),
  );
}
