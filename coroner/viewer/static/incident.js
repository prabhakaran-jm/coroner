// Incident detail page (GET /api/runs/{id}). Composes the RSS timeline
// (chart.js) and the Evidence vs Uncertainty panel (evidence-panel.js) with
// the recorded facts of one run. Every value comes from the API detail object
// and reaches the page as text only (text nodes and textContent). No recorded
// timestamp is parsed here: the API supplies the display strings, and the
// original strings are shown in a details view. Null is shown as "not
// recorded", never as 0, false or an inferred value.

import { COPY, fill } from "/static/copy.js";
import { fetchRun } from "/static/api.js";
import { clear, el, textEl } from "/static/dom.js";
import { renderChart } from "/static/chart.js";
import { renderEvidencePanel } from "/static/evidence-panel.js";

const BYTES_PER_MIB = 1048576;
const ARTIFACTS = ["manifest", "evidence", "inspect"];

// ---------------------------------------------------------------- small helpers

function isNum(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function has(object, key) {
  return object !== null && typeof object === "object" && Object.prototype.hasOwnProperty.call(object, key);
}

function plain(value) {
  if (value === null || value === undefined || value === "") {
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

// A value of any recorded type for a table cell.
function cellText(value) {
  if (value === null || value === undefined) {
    return COPY.shared.notRecorded;
  }
  if (typeof value === "boolean") {
    return boolText(value);
  }
  return String(value);
}

// A value exactly as recorded: strings in quotes, null as "not recorded".
function recordedText(value) {
  if (value === null || value === undefined) {
    return COPY.shared.notRecorded;
  }
  return JSON.stringify(value);
}

function mono(value) {
  return textEl("span", "mono", value);
}

function muted(value) {
  return textEl("span", "muted", value);
}

function clockName(key) {
  return typeof key === "string" && has(COPY.clocks, key) ? COPY.clocks[key] : null;
}

function withClock(label, clockKey) {
  const name = clockName(clockKey);
  return name === null ? label : label + " (" + name + ")";
}

function truncatedNote(truncated, originalLength) {
  if (truncated !== true) {
    return null;
  }
  return muted(" (" + fill(COPY.evidence.truncatedNote, { n: plain(originalLength) }) + ")");
}

// A formatted time with its "shown to ms" label and the original in a details view.
function timeValue(display, original) {
  const shown = typeof display === "string" ? display : typeof original === "string" ? original : null;
  if (shown === null) {
    return textEl("span", "", COPY.shared.notRecorded);
  }
  const wrap = el("span", { class: "time-value" }, mono(shown));
  wrap.append(
    textEl(
      "span",
      "muted time-note",
      " " + (typeof display === "string" ? COPY.shared.shownToMs : COPY.times.displayUnavailable),
    ),
  );
  if (typeof original === "string") {
    wrap.append(
      el("details", { class: "original" }, textEl("summary", "", COPY.times.originalSummary), mono(original)),
    );
  }
  return wrap;
}

function fact(label, ...content) {
  return el(
    "div",
    { class: "fact" },
    textEl("dt", "fact-label", label),
    el("dd", { class: "fact-value" }, content),
  );
}

function facts(rows) {
  return el("dl", { class: "facts" }, rows);
}

function section(id, title, ...children) {
  return el(
    "section",
    { class: "incident-section", attrs: { "aria-labelledby": id } },
    textEl("h3", "section-title", title, { id: id }),
    children,
  );
}

function memoryText(bytes) {
  if (typeof bytes !== "number" || !Number.isInteger(bytes) || bytes < 0) {
    return COPY.shared.notRecorded;
  }
  if (bytes === 0) {
    return COPY.incident.memoryZero;
  }
  return fill(COPY.incident.memoryValue, { mib: (bytes / BYTES_PER_MIB).toFixed(2), bytes: bytes });
}

// ---------------------------------------------------------------- run and Verdict

function runSection(detail) {
  const run = detail.run || {};
  const type = run.experiment_type !== null && run.experiment_type !== undefined ? run.experiment_type : run.experiment;
  return section(
    "inc-run-title",
    COPY.incident.sectionRun,
    facts([
      fact(COPY.overview.runIdLabel, mono(plain(run.run_id))),
      fact(COPY.incident.experimentType, mono(plain(type))),
      fact(withClock(COPY.incident.startTime, run.start_time_clock), timeValue(run.start_time_display, run.start_time)),
      fact(withClock(COPY.incident.endTime, run.end_time_clock), timeValue(run.end_time_display, run.end_time)),
    ]),
  );
}

function verdictBadge(verdict) {
  const label = verdict === "PASS" || verdict === "FAIL" || verdict === "UNKNOWN" ? verdict : COPY.shared.verdictUnknown;
  const className = label === "PASS" ? "badge badge-pass" : label === "FAIL" ? "badge badge-fail" : "badge badge-unknown";
  return textEl("span", className, label);
}

function durabilityText(value) {
  if (value === "crash") {
    return COPY.incident.durabilityCrash + " (" + COPY.incident.durabilityStored + ": crash)";
  }
  if (value === "process") {
    return COPY.incident.durabilityProcess + " (" + COPY.incident.durabilityStored + ": process)";
  }
  return plain(value);
}

function verdictSection(detail) {
  const run = detail.run || {};
  const rows = [
    fact(COPY.shared.verdictLabel, verdictBadge(run.verdict)),
    fact(COPY.incident.storedOutcome, mono(plain(run.outcome))),
  ];
  const failure = run.failure;
  if (failure && typeof failure === "object") {
    rows.push(
      fact(
        COPY.incident.failureCode,
        mono(plain(failure.code)),
        truncatedNote(failure.code_truncated, failure.code_original_length),
      ),
      fact(
        COPY.incident.failureMessage,
        textEl("span", "runner-text", plain(failure.message)),
        truncatedNote(failure.message_truncated, failure.message_original_length),
      ),
    );
  } else {
    rows.push(fact(COPY.incident.failureLabel, textEl("span", "", COPY.incident.failureNone)));
  }
  rows.push(fact(COPY.incident.durability, mono(durabilityText(run.durability_level))));
  const children = [facts(rows), textEl("p", "muted", COPY.incident.verdictNote)];
  if (run.error && typeof run.error === "object") {
    children.push(
      el("p", { class: "notice" }, COPY.incident.unreadableReason, mono(plain(run.error.reason))),
    );
  }
  return section("inc-verdict-title", COPY.incident.sectionVerdict, children);
}

// ---------------------------------------------------------------- Docker state

function resultText(result) {
  switch (result) {
    case "equal":
      return COPY.incident.resultEqual;
    case "discrepancy":
      return COPY.shared.discrepancy;
    case "not_compared_manifest_missing":
      return COPY.incident.resultNotComparedManifest;
    case "not_compared_raw_missing":
      return COPY.incident.resultNotComparedRaw;
    case "not_recorded_both":
      return COPY.incident.resultNotRecordedBoth;
    default:
      return COPY.shared.notRecorded;
  }
}

function comparisonTable(fields) {
  const rows = fields.map((f) =>
    el(
      "tr",
      {},
      el("td", {}, mono(plain(f.field))),
      el("td", {}, mono(cellText(f.manifest_value))),
      el("td", {}, mono(cellText(f.raw_value))),
      el(
        "td",
        {},
        f.result === "discrepancy"
          ? textEl("span", "discrepancy", resultText(f.result))
          : textEl("span", "", resultText(f.result)),
      ),
    ),
  );
  return el(
    "div",
    { class: "table-wrap" },
    el(
      "table",
      { class: "table" },
      textEl("caption", "visually-hidden", COPY.incident.compareCaption),
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          textEl("th", "", COPY.incident.colField, { scope: "col" }),
          textEl("th", "", COPY.incident.colManifest, { scope: "col" }),
          textEl("th", "", COPY.incident.colRaw, { scope: "col" }),
          textEl("th", "", COPY.incident.colResult, { scope: "col" }),
        ),
      ),
      el("tbody", {}, rows),
    ),
  );
}

function dockerSection(detail) {
  const ds = detail.docker_state || {};
  const eff = ds.effective && typeof ds.effective === "object" ? ds.effective : {};
  const sourceText = has(COPY.dockerSource, ds.source) ? COPY.dockerSource[ds.source] : COPY.shared.notRecorded;
  const sourceNote = () => muted(" (" + fill(COPY.evidence.stateSource, { source: sourceText }) + ")");

  const rows = [
    fact(COPY.incident.stateSource, textEl("span", "", sourceText)),
    fact(COPY.evidence.dockerStatus, mono(plain(eff.status)), sourceNote()),
    fact(COPY.evidence.exitCode, mono(plain(eff.exit_code)), sourceNote()),
    fact(COPY.evidence.oomKilled, mono(boolText(eff.oom_killed)), sourceNote()),
    fact(COPY.incident.memoryLimit, mono(memoryText(eff.memory_limit_bytes)), sourceNote()),
  ];
  const children = [facts(rows)];

  // Experiment C: the recorded OOMKilled value establishes the termination mechanism only.
  if ((detail.run || {}).experiment_type === "C") {
    if (eff.oom_killed === true) {
      children.push(
        textEl(
          "p",
          "notice",
          fill(COPY.evidence.oomMechanism, { oom: boolText(eff.oom_killed), code: plain(eff.exit_code) }),
        ),
      );
    }
    children.push(textEl("p", "muted", COPY.evidence.growthReasonNotEstablished));
  }

  if (typeof ds.raw_unavailable_reason === "string") {
    const key = "rawReason_" + ds.raw_unavailable_reason;
    const reason = has(COPY.incident, key) ? COPY.incident[key] : ds.raw_unavailable_reason;
    children.push(textEl("p", "notice", COPY.incident.rawUnavailable + reason));
  }

  const discrepancies = Array.isArray(ds.discrepancies) ? ds.discrepancies : [];
  for (const d of discrepancies) {
    children.push(
      textEl(
        "p",
        "discrepancy",
        COPY.shared.discrepancy +
          ": " +
          fill(COPY.evidence.discrepancyLine, {
            field: plain(d.field),
            manifest: cellText(d.manifest_value),
            raw: cellText(d.raw_value),
          }),
      ),
    );
  }

  const fields = Array.isArray(ds.fields) ? ds.fields : [];
  if (fields.length > 0) {
    children.push(textEl("h4", "subsection-title", COPY.incident.compareHeading), comparisonTable(fields));
  }
  return section("inc-docker-title", COPY.incident.sectionDocker, children);
}

// ---------------------------------------------------------------- Runner text

function runnerItem(label, value, truncated, originalLength) {
  return el(
    "div",
    { class: "runner-item" },
    textEl("p", "runner-item-label", label),
    textEl("p", "runner-text", typeof value === "string" ? value : COPY.shared.notRecorded),
    typeof value === "string" ? truncatedNote(truncated, originalLength) : null,
  );
}

function runnerSection(detail) {
  const rt = detail.runner_text || {};
  return section(
    "inc-runner-title",
    COPY.incident.sectionRunner,
    el(
      "div",
      { class: "runner-block" },
      textEl("p", "runner-block-title", COPY.evidence.runnerBlockTitle),
      runnerItem(
        COPY.evidence.terminationDescription,
        rt.termination_description,
        rt.termination_description_truncated,
        rt.termination_description_original_length,
      ),
      runnerItem(COPY.evidence.causeLabel, rt.cause_label, rt.cause_label_truncated, rt.cause_label_original_length),
    ),
  );
}

// ---------------------------------------------------------------- evidence counts

function recordsSection(detail) {
  const run = detail.run || {};
  const es = detail.evidence_summary || {};
  const manifestCount = es.manifest_record_count !== undefined ? es.manifest_record_count : run.record_count_manifest;
  const children = [
    facts([
      fact(COPY.incident.manifestRecordCount, mono(plain(manifestCount))),
      fact(COPY.incident.validRecordCount, mono(plain(es.valid_record_count))),
    ]),
  ];
  if (es.record_count_mismatch === true) {
    children.push(
      textEl(
        "p",
        "notice",
        fill(COPY.evidence.recordCountMismatch, {
          manifest: plain(manifestCount),
          valid: plain(es.valid_record_count),
        }),
      ),
    );
  }
  return section("inc-records-title", COPY.incident.sectionRecords, children);
}

function samplesSection(detail) {
  const es = detail.evidence_summary || {};
  const dropRow =
    es.unreconciled_drop_count === null || es.unreconciled_drop_count === undefined
      ? fact(COPY.evidence.dropCountLabel, mono(COPY.shared.notRecorded))
      : fact(
          COPY.evidence.dropCountLabel,
          mono(plain(es.unreconciled_drop_count)),
          muted(" (" + fill(COPY.evidence.dropUncertain, { value: boolText(es.unreconciled_drop_uncertain) }) + ")"),
        );
  return section(
    "inc-samples-title",
    COPY.incident.sectionSamples,
    facts([
      fact(COPY.evidence.sampleCount, mono(plain(es.sample_count))),
      fact(COPY.incident.missingSampleCount, mono(plain(es.missing_sample_count))),
      dropRow,
      fact(COPY.evidence.malformedLabel, mono(plain(es.malformed_line_count))),
    ]),
  );
}

// Three separately labelled facts. They stay distinct even when two refer to the same Sample.
function lastFactsSection(detail) {
  const es = detail.evidence_summary || {};
  const lp = es.last_persisted_sample && typeof es.last_persisted_sample === "object" ? es.last_persisted_sample : null;
  const lc = es.latest_capture_sample && typeof es.latest_capture_sample === "object" ? es.latest_capture_sample : null;

  const persisted =
    lp === null
      ? textEl("p", "muted", COPY.shared.notRecorded)
      : facts([
          fact(COPY.incident.seq, mono(plain(lp.seq))),
          fact(COPY.incident.captureTime, timeValue(lp.capture_time_display, lp.capture_time)),
          fact(COPY.incident.receiveTime, timeValue(lp.receive_time_display, lp.receive_time)),
        ]);
  const latest =
    lc === null
      ? textEl("p", "muted", COPY.shared.notRecorded)
      : facts([
          fact(COPY.incident.seq, mono(plain(lc.seq))),
          fact(COPY.incident.captureTime, timeValue(lc.capture_time_display, lc.capture_time)),
        ]);
  const receive = facts([
    fact(
      COPY.incident.lastReceive,
      timeValue(es.last_receive_time_summary_display, es.last_receive_time_summary),
    ),
  ]);

  // One block when both facts are the same recorded Sample (same seq and same Capture_Time).
  const sameSample =
    lp !== null && lc !== null && isNum(lp.seq) && lp.seq === lc.seq &&
    typeof lp.capture_time === "string" && lp.capture_time === lc.capture_time;
  const sampleBlocks = sameSample
    ? [
        el(
          "div",
          { class: "last-fact" },
          textEl("h4", "subsection-title", COPY.incident.sameSampleHeading),
          textEl("p", "muted", fill(COPY.incident.sameSampleNote, { seq: lp.seq })),
          persisted,
        ),
      ]
    : [
        el("div", { class: "last-fact" }, textEl("h4", "subsection-title", COPY.incident.lastPersisted), persisted),
        el("div", { class: "last-fact" }, textEl("h4", "subsection-title", COPY.incident.latestCapture), latest),
      ];

  return section(
    "inc-last-title",
    COPY.incident.sectionLastFacts,
    sampleBlocks,
    el(
      "div",
      { class: "last-fact" },
      textEl("h4", "subsection-title", COPY.incident.lastReceive),
      receive,
      textEl("p", "muted", COPY.incident.lastReceiveDef),
    ),
  );
}

// ---------------------------------------------------------------- Experiment B clocks

function clockSection(detail) {
  const timing = detail.timing || {};
  const rows = [];
  rows.push(
    fact(
      COPY.evidence.clockOffset,
      mono(isNum(timing.clock_offset_ms) ? fill(COPY.incident.offsetValue, { ms: timing.clock_offset_ms }) : COPY.shared.notRecorded),
      typeof timing.clock_offset_source === "string"
        ? muted(" (" + fill(COPY.incident.offsetSource, { source: timing.clock_offset_source }) + ")")
        : null,
    ),
    fact(
      COPY.evidence.clockUncertainty,
      mono(
        isNum(timing.clock_uncertainty_ms)
          ? fill(COPY.incident.offsetValue, { ms: timing.clock_uncertainty_ms })
          : COPY.evidence.derivedUncertaintyNone,
      ),
    ),
  );
  const children = [facts(rows), textEl("p", "muted", COPY.evidence.clockScope)];

  const conflict = timing.clock_offset_conflict;
  if (conflict && typeof conflict === "object") {
    children.push(
      textEl(
        "p",
        "discrepancy",
        COPY.shared.discrepancy +
          ": " +
          fill(COPY.evidence.clockConflictLine, { a: cellText(conflict.sample_timing), b: cellText(conflict.manifest) }),
      ),
      textEl("p", "notice", COPY.evidence.derivedSuppressed),
    );
  }
  if (timing.correction_available === false) {
    const key = "correctionReason_" + String(timing.correction_unavailable_reason);
    let line = COPY.chart.correctionUnavailable;
    if (has(COPY.chart, key)) {
      line += ": " + COPY.chart[key];
    }
    children.push(textEl("p", "notice", line));
  }
  return section("inc-clock-title", COPY.incident.sectionClock, children);
}

function groupRows(label, group) {
  const rows = [fact(label + " " + COPY.incident.groupCount, mono(plain(group.count)))];
  const items = Array.isArray(group.items) ? group.items : [];
  const list =
    items.length === 0
      ? [textEl("span", "muted", COPY.incident.groupNoItems)]
      : [
          el(
            "ul",
            { class: "ev-sub" },
            items.map((item) =>
              el("li", {}, timeValue(item ? item.capture_time_display : null, item ? item.capture_time : null)),
            ),
          ),
        ];
  if (group.items_truncated === true) {
    list.push(
      muted(" (" + fill(COPY.evidence.listTruncated, { shown: items.length, total: plain(group.items_total) }) + ")"),
    );
  }
  rows.push(fact(label + " " + COPY.incident.groupTimes, list));
  return rows;
}

function timingSection(detail) {
  const timing = detail.timing || {};
  const st = timing.sample_timing;
  if (!st || typeof st !== "object") {
    return section("inc-timing-title", COPY.incident.sectionTiming, textEl("p", "", COPY.incident.sampleTimingNotRecorded));
  }
  const rows = [
    fact(COPY.incident.preKillCount, mono(plain(st.pre_kill_count))),
    fact(COPY.incident.preKillLate, mono(plain(st.pre_kill_late_received_count))),
    fact(COPY.incident.withinTolerance, mono(plain(st.within_tolerance_count))),
  ];
  rows.push(...groupRows(COPY.incident.postKill, st.post_kill_beyond_tolerance || {}));
  rows.push(fact(COPY.incident.afterStopAnalysis, mono(plain(st.after_container_stop_analysis))));
  const children = [];
  if (st.after_container_stop_analysis === "unavailable") {
    children.push(textEl("p", "notice", COPY.incident.afterStopUnavailable));
  } else {
    rows.push(...groupRows(COPY.incident.afterStop, st.after_container_stop || {}));
  }

  // tolerance_ms exactly as recorded; never clamped
  const value = recordedText(timing.tolerance_ms);
  const noteKey = "toleranceNote_" + String(timing.tolerance_status);
  const note =
    timing.tolerance_status !== "valid" && has(COPY.incident, noteKey) && COPY.incident[noteKey] !== value
      ? muted(" (" + COPY.incident[noteKey] + ")")
      : null;
  rows.push(fact(COPY.incident.toleranceMs, mono(value), note));

  return section("inc-timing-title", COPY.incident.sectionTiming, facts(rows), children);
}

// ---------------------------------------------------------------- Derived_Intervals (Experiment B)

function derivedTimes(detail, di) {
  const timing = detail.timing || {};
  const derived = timing.derived || {};
  const from = { label: COPY.chart.pointCapture, display: di.from_time_display, original: di.from_time };
  if (di.kind === "finished_at_to_sigkill") {
    from.label = COPY.times.docker_finished_at;
  }
  // The API returns no to_time for these kinds; use the recorded timing fields.
  const to = { label: COPY.incident.toKindUnknown, display: di.to_time_display, original: di.to_time };
  if (di.to_kind === "sigkill_sent") {
    to.label = COPY.times.sigkill_sent;
    if (typeof to.display !== "string") {
      to.display = timing.sigkill_sent_time_display;
    }
    if (typeof to.original !== "string") {
      to.original = timing.sigkill_sent_time;
    }
  } else if (di.to_kind === "docker_finished_at") {
    to.label = COPY.times.docker_finished_at;
    if (typeof to.original !== "string") {
      to.original = derived.finished_at_source === "manifest" ? timing.container_finished_at : timing.state_finished_at;
    }
  }
  return { from: from, to: to };
}

function intervalBlock(detail, di) {
  const timing = detail.timing || {};
  const kindKey = "derivedKind_" + String(di.kind);
  const kindLabel = has(COPY.evidence, kindKey) ? COPY.evidence[kindKey] : COPY.shared.notRecorded;
  const sampleKey = "derivedSample_" + String(di.sample);
  const sampleLabel = has(COPY.evidence, sampleKey) ? COPY.evidence[sampleKey] : null;
  const times = derivedTimes(detail, di);
  const sigkillKind = di.kind === "sample_to_sigkill";

  const rows = [
    fact(
      fill(COPY.evidence.derivedFrom, { time: times.from.label }),
      timeValue(times.from.display, times.from.original),
      muted(" " + (clockName(di.from_clock) === null ? "" : COPY.incident.clockName + clockName(di.from_clock))),
    ),
  ];
  if (typeof di.from_corrected_time === "string") {
    rows.push(fact(COPY.chart.pointCorrected, mono(di.from_corrected_time)));
  }
  rows.push(
    fact(
      fill(COPY.evidence.derivedTo, { time: times.to.label }),
      timeValue(times.to.display, times.to.original),
      muted(" " + (clockName(di.to_clock) === null ? "" : COPY.incident.clockName + clockName(di.to_clock))),
    ),
  );

  // Without a recorded Clock_Uncertainty no interval is shown for the SIGKILL comparison.
  const showDifference = isNum(di.difference_ms) && !(sigkillKind && !isNum(di.uncertainty_ms));
  rows.push(
    fact(
      COPY.incident.differenceLabel,
      textEl(
        "span",
        "",
        showDifference ? fill(COPY.evidence.derivedDifference, { ms: di.difference_ms }) : COPY.evidence.derivedDifferenceNone,
      ),
    ),
    fact(
      COPY.evidence.clockUncertainty,
      textEl(
        "span",
        "",
        isNum(di.uncertainty_ms)
          ? fill(COPY.evidence.derivedUncertainty, { ms: di.uncertainty_ms })
          : COPY.evidence.derivedUncertaintyNone,
      ),
    ),
  );

  const statements = [];
  if (sigkillKind) {
    const tolValue = recordedText(di.tolerance_ms);
    const noteKey = "toleranceNote_" + String(timing.tolerance_status);
    const tolNote =
      timing.tolerance_status !== "valid" && has(COPY.incident, noteKey) && COPY.incident[noteKey] !== tolValue
        ? " (" + COPY.incident[noteKey] + ")"
        : "";
    rows.push(fact(COPY.incident.toleranceLabel, mono(tolValue + tolNote)));
    if (di.reason === "uncertainty_not_recorded") {
      statements.push(COPY.evidence.derivedReasonUncertainty);
    }
    if (di.ordering === "not_established") {
      statements.push(COPY.evidence.derivedOrderNotEstablished);
    }
  } else {
    statements.push(COPY.evidence.derivedDockerNoBound);
  }
  statements.push(
    COPY.evidence.clocksNotSynchronised,
    COPY.incident.uncertaintyNoBound,
    COPY.evidence.derivedNoOrder,
  );

  return el(
    "article",
    { class: "interval notice" },
    textEl("h4", "subsection-title", sampleLabel === null ? kindLabel : kindLabel + " (" + sampleLabel + ")"),
    textEl("p", "", COPY.incident.derivedLabel + ": " + COPY.shared.derivedNotProven),
    facts(rows),
    el("ul", { class: "ev-sub" }, statements.map((s) => el("li", {}, s))),
  );
}

function derivedSection(detail) {
  const run = detail.run || {};
  const timing = detail.timing || {};
  if (run.experiment_type !== "B") {
    return section(
      "inc-derived-title",
      COPY.incident.sectionDerived,
      textEl("p", "", COPY.incident.derivedNoneForRun),
      textEl("p", "muted", COPY.evidence.orderNotEstablishedAC),
    );
  }
  const derived = timing.derived && typeof timing.derived === "object" ? timing.derived : {};
  if (derived.suppressed === true) {
    return section("inc-derived-title", COPY.incident.sectionDerived, textEl("p", "notice", COPY.evidence.derivedSuppressed));
  }
  const intervals = Array.isArray(derived.intervals) ? derived.intervals.filter((di) => di && typeof di === "object") : [];
  if (intervals.length === 0) {
    return section("inc-derived-title", COPY.incident.sectionDerived, textEl("p", "", COPY.incident.derivedNoneListed));
  }
  return section(
    "inc-derived-title",
    COPY.incident.sectionDerived,
    intervals.map((di) => intervalBlock(detail, di)),
  );
}

// ---------------------------------------------------------------- artifact status and warnings

function artifactText(entry) {
  const status = entry && typeof entry.status === "string" ? entry.status : null;
  switch (status) {
    case "ok":
      return [textEl("span", "", COPY.incident.statusOk)];
    case "not_read":
      return [textEl("span", "", COPY.shared.notReadManifestUnusable)];
    case "missing":
      return [textEl("span", "", COPY.incident.statusMissing), reasonNote(entry)];
    case "unreadable":
      return [textEl("span", "", COPY.incident.statusUnreadable), reasonNote(entry)];
    case "invalid":
      return [textEl("span", "", COPY.incident.statusInvalid), reasonNote(entry)];
    case "too_large":
      return [
        textEl(
          "span",
          "",
          fill(COPY.incident.statusTooLarge, {
            size: plain(entry.observed_size_text),
            limit: plain(entry.limit_bytes),
          }),
        ),
      ];
    default:
      return [textEl("span", "", COPY.incident.statusUnknown)];
  }
}

function reasonNote(entry) {
  if (typeof entry.reason !== "string" || entry.reason === "") {
    return null;
  }
  return muted(" (" + COPY.incident.reasonPrefix + entry.reason + ")");
}

function artifactsSection(detail) {
  const status = detail.artifact_status || {};
  return section(
    "inc-artifacts-title",
    COPY.incident.sectionArtifacts,
    facts(ARTIFACTS.map((name) => fact(COPY.incident["artifact_" + name], artifactText(status[name])))),
  );
}

function warningsSection(detail) {
  const warnings = Array.isArray(detail.warnings) ? detail.warnings : [];
  if (warnings.length === 0) {
    return section("inc-warnings-title", COPY.incident.sectionWarnings, textEl("p", "muted", COPY.incident.warningsNone));
  }
  const items = warnings.map((w) => {
    const entry = w && typeof w === "object" ? w : {};
    const meta = [];
    if (typeof entry.source === "string") {
      meta.push(COPY.incident.warningSource + entry.source);
    }
    if (typeof entry.code === "string") {
      meta.push(COPY.incident.warningCode + entry.code);
    }
    return el(
      "li",
      {},
      textEl("span", "runner-text", plain(entry.text)),
      truncatedNote(entry.text_truncated, entry.text_original_length),
      meta.length > 0 ? muted(" [" + meta.join("; ") + "]") : null,
    );
  });
  return section("inc-warnings-title", COPY.incident.sectionWarnings, el("ul", { class: "runner-list" }, items));
}

// ---------------------------------------------------------------- page

// A composed module that throws must not take the whole page down.
function safely(build) {
  try {
    return build();
  } catch (err) {
    return textEl("p", "state state-error", COPY.states.renderFailed, { role: "alert" });
  }
}

export function buildIncident(detail) {
  const data = detail && typeof detail === "object" ? detail : {};
  const run = data.run || {};
  const page = el("div", { class: "page" });
  page.append(textEl("h1", "page-title", COPY.incident.heading));
  page.append(
    safely(() => runSection(data)),
    safely(() => verdictSection(data)),
    safely(() => dockerSection(data)),
    safely(() => runnerSection(data)),
    safely(() => recordsSection(data)),
    safely(() => samplesSection(data)),
    safely(() => lastFactsSection(data)),
  );
  if (run.experiment_type === "B") {
    page.append(
      safely(() => clockSection(data)),
      safely(() => timingSection(data)),
    );
  }
  page.append(
    safely(() => derivedSection(data)),
    safely(() => renderChart(data)),
    safely(() => renderEvidencePanel(data)),
    safely(() => artifactsSection(data)),
    safely(() => warningsSection(data)),
  );
  return page;
}

export async function renderIncident(root, runId, ui) {
  clear(root);
  // The back link stays visible in every state (loading, error and loaded).
  const holder = el("div", { class: "incident-body" });
  root.append(
    el("p", {}, el("a", { class: "text-link", attrs: { href: "/" } }, COPY.incident.back)),
    holder,
  );
  await ui.loadInto(holder, () => fetchRun(runId), buildIncident);
}
