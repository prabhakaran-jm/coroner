// Compare page: Experiment A, B and C side by side, each from its own saved run.
// Data comes only from GET /api/runs and GET /api/runs/{run_id}. Every statement and the
// headline come from evaluateComparison (compare-logic.js); this module only resolves the
// slots, renders recorded values as text, and never infers a missing value from another run.

import { COPY, fill } from "/static/copy.js";
import { fetchRun, fetchRuns } from "/static/api.js";
import { evaluateComparison } from "/static/compare-logic.js";
import { clear, el, textEl } from "/static/dom.js";
import { oomState, oomStatements } from "/static/oom-state.js";

const SLOTS = ["a", "b", "c"];
const BYTES_PER_MIB = 1048576;

function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function upper(key) {
  return key.toUpperCase();
}

function isInt(value) {
  return typeof value === "number" && Number.isInteger(value);
}

function plain(value) {
  return value === null || value === undefined || value === "" ? COPY.shared.notRecorded : String(value);
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

function mono(value) {
  return textEl("span", "mono", value);
}

function verdictBadge(verdict) {
  const label = verdict === "PASS" || verdict === "FAIL" || verdict === "UNKNOWN" ? verdict : COPY.shared.verdictUnknown;
  const className = label === "PASS" ? "badge badge-pass" : label === "FAIL" ? "badge badge-fail" : "badge badge-unknown";
  return textEl("span", className, label);
}

function bytesText(bytes, template) {
  if (!isInt(bytes) || bytes < 0) {
    return COPY.shared.notRecorded;
  }
  return fill(template, { mib: (bytes / BYTES_PER_MIB).toFixed(2), bytes: bytes });
}

function limitText(bytes) {
  if (isInt(bytes) && bytes === 0) {
    return COPY.incident.memoryZero;
  }
  return bytesText(bytes, COPY.incident.memoryValue);
}

function typeOf(item) {
  return item && (item.experiment_type === "A" || item.experiment_type === "B" || item.experiment_type === "C")
    ? item.experiment_type
    : null;
}

// ------------------------------------------------------------ slot resolution

// The slot choices of the address. The server has already validated them.
function explicitIds() {
  const params = new URLSearchParams(location.search);
  const out = {};
  for (const key of SLOTS) {
    const value = params.get(key);
    out[key] = value !== null && value !== "" ? value : null;
  }
  return out;
}

async function resolveSlot(key, explicitId, items) {
  const type = upper(key);
  let runId = explicitId;
  let viaQuery = true;
  if (runId === null) {
    viaQuery = false;
    const first = items.find((item) => typeOf(item) === type);
    if (!first) {
      return { key: key, state: "none", run_id: null, viaQuery: false };
    }
    runId = first.run_id;
  }
  const result = await fetchRun(runId);
  if (!result.ok) {
    if (result.status === 404) {
      return { key: key, state: "unavailable", run_id: runId, viaQuery: viaQuery };
    }
    return { key: key, state: "error", run_id: runId, viaQuery: viaQuery, http_status: result.status };
  }
  const detail = result.data && typeof result.data === "object" ? result.data : {};
  const run = detail.run && typeof detail.run === "object" ? detail.run : {};
  const actual = typeOf(run);
  return {
    key: key,
    state: actual === type ? "loaded" : "excluded",
    run_id: runId,
    viaQuery: viaQuery,
    detail: detail,
    actual_type: actual,
  };
}

async function loadCompare() {
  const list = await fetchRuns();
  if (!list.ok) {
    return list;
  }
  const items = Array.isArray(list.data.runs) ? list.data.runs : [];
  const explicit = explicitIds();
  const slots = await Promise.all(SLOTS.map((key) => resolveSlot(key, explicit[key], items)));
  return { ok: true, status: 200, data: { items: items, explicit: explicit, slots: slots } };
}

// ------------------------------------------------------------ slot facts for the logic

function dockerOf(slot) {
  const detail = slot.detail || {};
  const ds = detail.docker_state && typeof detail.docker_state === "object" ? detail.docker_state : {};
  const eff = ds.effective && typeof ds.effective === "object" ? ds.effective : {};
  const discrepancies = Array.isArray(ds.discrepancies) ? ds.discrepancies : [];
  return { ds: ds, eff: eff, discrepancies: discrepancies };
}

// Field names of the Docker state discrepancies. A conflicting OOMKilled pair that the payload did
// not list is added, so the comparison logic withholds statements that would rely on it.
function discrepantFields(slot, discrepancies) {
  const fields = discrepancies.map((d) => (d && typeof d.field === "string" ? d.field : ""));
  if (oomState(dockerOf(slot).ds).kind === "conflict" && fields.indexOf("oom_killed") < 0) {
    fields.push("oom_killed");
  }
  return fields;
}
function slotFacts(slot) {
  if (slot.state === "none") {
    return { state: "none", run_id: null };
  }
  if (slot.state === "unavailable") {
    return { state: "unavailable", run_id: slot.run_id };
  }
  if (slot.state === "error") {
    return { state: "error", run_id: slot.run_id, http_status: slot.http_status };
  }
  const { eff, discrepancies } = dockerOf(slot);
  const summary = (slot.detail || {}).evidence_summary || {};
  return {
    state: "loaded",
    run_id: slot.run_id,
    experiment_type: slot.actual_type,
    exit_code: isInt(eff.exit_code) ? eff.exit_code : null,
    oom_killed: typeof eff.oom_killed === "boolean" ? eff.oom_killed : null,
    sample_count: isInt(summary.sample_count) ? summary.sample_count : null,
    discrepant_fields: discrepantFields(slot, discrepancies),
  };
}

// ------------------------------------------------------------ address links

function compareHref(ids) {
  const parts = [];
  for (const key of SLOTS) {
    if (ids[key] !== null) {
      parts.push(key + "=" + encodeURIComponent(ids[key]));
    }
  }
  return parts.length === 0 ? "/compare" : "/compare?" + parts.join("&");
}

function withSlot(explicit, key, runId) {
  const next = Object.assign({}, explicit);
  next[key] = runId;
  return next;
}

// ------------------------------------------------------------ slot panels

function slotStateText(slot) {
  const type = upper(slot.key);
  if (slot.state === "none") {
    return fill(COPY.compare.stateNone, { type: type });
  }
  if (slot.state === "unavailable") {
    return fill(COPY.compare.stateUnavailable, { run: slot.run_id }) + " (" + COPY.shared.noSavedRun + ")";
  }
  if (slot.state === "error") {
    return fill(COPY.compare.stateError, { run: slot.run_id, status: slot.http_status });
  }
  return null;
}

function chooser(slot, items, explicit) {
  const type = upper(slot.key);
  const choices = items.filter((item) => typeOf(item) === type);
  const heading = fill(COPY.compare.chooseSummary, { slot: type });
  const list = el("ul", { class: "choose-list" });
  list.append(
    el(
      "li",
      {},
      el(
        "a",
        { class: "text-link", attrs: { href: compareHref(withSlot(explicit, slot.key, null)) } },
        fill(COPY.compare.chooseDefault, { slot: type }),
      ),
    ),
  );
  for (const item of choices) {
    const current = item.run_id === slot.run_id;
    const attrs = { href: compareHref(withSlot(explicit, slot.key, item.run_id)) };
    if (current) {
      attrs["aria-current"] = "true";
    }
    list.append(
      el(
        "li",
        {},
        el(
          "a",
          { class: "text-link mono", attrs: attrs },
          item.run_id,
          el("span", { class: "visually-hidden" }, " ", fill(COPY.compare.chooseRun, { run: item.run_id, slot: type })),
        ),
        current ? textEl("span", "muted", " " + COPY.compare.chooseCurrent) : null,
      ),
    );
  }
  const body = [list];
  if (choices.length === 0) {
    body.push(textEl("p", "muted", fill(COPY.compare.chooseNone, { type: type })));
  }
  return el("details", { class: "chooser" }, textEl("summary", "", heading), body);
}

function slotPanel(slot, items, explicit) {
  const type = upper(slot.key);
  const titleId = "slot-title-" + slot.key;
  const children = [textEl("h3", "card-title", fill(COPY.compare.slotTitle, { slot: type, type: type }), { id: titleId })];
  const stateText = slotStateText(slot);
  let className = "card";
  if (stateText !== null) {
    className = "card card-empty";
    children.push(el("p", { class: "slot-state", attrs: { role: "status" } }, stateText));
  } else {
    const run = (slot.detail || {}).run || {};
    children.push(
      el("p", { class: "slot-run" }, textEl("span", "muted", COPY.compare.slotRunId + ": "), mono(slot.run_id)),
    );
    children.push(
      textEl(
        "p",
        "card-note",
        slot.viaQuery ? COPY.compare.slotChosenQuery : fill(COPY.compare.slotChosenDefault, { type: type }),
      ),
    );
    if (slot.state === "excluded") {
      className = "card card-excluded";
      children.push(
        el(
          "p",
          { class: "warning", attrs: { role: "alert" } },
          fill(COPY.compare.warningExcluded, {
            run: slot.run_id,
            actual: slot.actual_type !== null ? slot.actual_type : COPY.compare.warningTypeNotRecorded,
            slot: type,
            expected: type,
          }),
        ),
      );
    }
    if (run.error && typeof run.error === "object") {
      children.push(textEl("p", "notice", fill(COPY.compare.unusableRun, { reason: plain(run.error.reason) })));
    }
    children.push(
      el(
        "a",
        { class: "text-link", attrs: { href: "/runs/" + encodeURIComponent(slot.run_id) } },
        fill(COPY.compare.openIncident, { run: slot.run_id }),
      ),
    );
  }
  children.push(chooser(slot, items, explicit));
  return el("article", { class: className, attrs: { "aria-labelledby": titleId } }, children);
}

// ------------------------------------------------------------ recorded values table
// The Docker OOMKilled cell follows the same rule as the incident page (oom-state.js): when the
// sources conflict both original values are shown by source and no single value is stated as fact.
function oomCellText(ds, eff) {
  const state = oomState(ds);
  if (state.kind === "conflict") {
    return oomStatements(state, false, null).rowValue;
  }
  if (state.kind === "none") {
    return boolText(eff.oom_killed);
  }
  return boolText(state.value);
}

function cellsFor(slot) {
  const detail = slot.detail || {};
  const run = detail.run || {};
  const { ds, eff, discrepancies } = dockerOf(slot);
  const summary = detail.evidence_summary || {};
  const peak = summary.highest_rss && typeof summary.highest_rss === "object" ? summary.highest_rss.rss_bytes : null;
  const fields = discrepancies.map((d) => (d && typeof d.field === "string" ? d.field : "?"));
  const source = has(COPY.dockerSource, ds.source) ? COPY.dockerSource[ds.source] : COPY.shared.notRecorded;
  return [
    mono(slot.run_id),
    mono(plain(slot.actual_type)),
    verdictBadge(run.verdict),
    mono(plain(eff.exit_code)),
    mono(oomCellText(ds, eff)),
    mono(bytesText(peak, COPY.compare.peakValue)),
    mono(limitText(eff.memory_limit_bytes)),
    mono(plain(summary.sample_count)),
    textEl("span", "", source),
    textEl(
      "span",
      fields.length > 0 ? "discrepancy-text" : "",
      fields.length > 0
        ? COPY.shared.discrepancy + ": " + fill(COPY.compare.discrepancyFields, { fields: fields.join(", ") })
        : COPY.compare.discrepancyNone,
    ),
  ];
}

const ROW_LABELS = [
  COPY.compare.rowRunId,
  COPY.compare.rowActualType,
  COPY.compare.rowVerdict,
  COPY.compare.rowExitCode,
  COPY.compare.rowOom,
  COPY.compare.rowRssPeak,
  COPY.compare.rowLimit,
  COPY.compare.rowSamples,
  COPY.compare.rowSource,
  COPY.compare.rowDiscrepancy,
];

function factsTable(slots) {
  const columns = slots.map((slot) =>
    slot.state === "loaded" || slot.state === "excluded"
      ? cellsFor(slot)
      : ROW_LABELS.map(() => textEl("span", "muted", COPY.compare.cellNoRun)),
  );
  const head = el(
    "tr",
    {},
    textEl("th", "", COPY.compare.colField, { scope: "col" }),
    slots.map((slot) => textEl("th", "", fill(COPY.compare.colSlot, { slot: upper(slot.key) }), { scope: "col" })),
  );
  const rows = ROW_LABELS.map((label, index) =>
    el(
      "tr",
      {},
      textEl("th", "", label, { scope: "row" }),
      columns.map((column) => el("td", {}, column[index])),
    ),
  );
  return el(
    "section",
    { class: "compare-facts", attrs: { "aria-labelledby": "compare-facts-title" } },
    textEl("h2", "section-title", COPY.compare.factsHeading, { id: "compare-facts-title" }),
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        { class: "table compare-table" },
        textEl("caption", "visually-hidden", COPY.compare.factsCaption),
        el("thead", {}, head),
        el("tbody", {}, rows),
      ),
    ),
  );
}

// ------------------------------------------------------------ statements

function statementsSection(result) {
  const children = [textEl("h2", "section-title", COPY.compare.statementsHeading, { id: "compare-statements-title" })];
  children.push(textEl("p", "muted", COPY.compare.statementsNote));
  if (result.statements.length === 0) {
    children.push(textEl("p", "", COPY.compare.noStatements));
  } else {
    children.push(
      el(
        "ul",
        { class: "statements" },
        result.statements.map((s) => textEl("li", "", s.text, { "data-statement": s.id.toLowerCase() })),
      ),
    );
  }
  const omittedKeys = Object.keys(result.omitted).filter((key) => result.omitted[key].length > 0);
  children.push(textEl("h3", "subsection-title", COPY.compare.omittedHeading));
  if (omittedKeys.length === 0) {
    children.push(textEl("p", "muted", COPY.compare.omittedNone));
  } else {
    const list = el("ul", { class: "omitted" });
    for (const key of omittedKeys) {
      const nameKey = "omittedName_" + key;
      const name = has(COPY.compare, nameKey) ? COPY.compare[nameKey] : key;
      list.append(
        el(
          "li",
          {},
          textEl("strong", "", name),
          el("ul", { class: "ev-sub" }, result.omitted[key].map((reason) => textEl("li", "", reason))),
        ),
      );
    }
    children.push(list);
  }
  return el("section", { class: "compare-statements", attrs: { "aria-labelledby": "compare-statements-title" } }, children);
}

function headlineSection(result) {
  // Shown only when the logic returned the headline; the text is the copy.js phrase.
  if (result.headline !== COPY.compare.headline) {
    return null;
  }
  return el(
    "section",
    { class: "compare-headline", attrs: { "aria-label": COPY.compare.headlineSection } },
    textEl("p", "headline-text", COPY.compare.headline),
  );
}

// ------------------------------------------------------------ limitations and Runner text

function limitationsSection() {
  return el(
    "section",
    { class: "compare-limits", attrs: { "aria-labelledby": "compare-limits-title" } },
    textEl("h2", "section-title", COPY.compare.limitsHeading, { id: "compare-limits-title" }),
    el(
      "ul",
      { class: "limit-list" },
      textEl("li", "", COPY.compare.limitExit),
      textEl("li", "", COPY.compare.limitRss),
      textEl("li", "", COPY.compare.limitSlots),
    ),
  );
}

function runnerItem(label, value, truncated, length) {
  const item = el(
    "div",
    { class: "runner-item" },
    textEl("p", "runner-item-label", label),
    textEl("p", "runner-text", typeof value === "string" ? value : COPY.shared.notRecorded),
  );
  if (typeof value === "string" && truncated === true) {
    item.append(textEl("p", "muted", fill(COPY.compare.truncatedNote, { n: plain(length) })));
  }
  return item;
}

function runnerBlock(slot) {
  const rt = (slot.detail || {}).runner_text || {};
  const limits = Array.isArray(rt.limitations) ? rt.limitations : [];
  const block = el(
    "div",
    { class: "runner-block" },
    textEl("h3", "runner-block-title", fill(COPY.compare.runnerSlot, { slot: upper(slot.key), run: slot.run_id })),
    runnerItem(COPY.compare.runnerDescription, rt.termination_description, rt.termination_description_truncated, rt.termination_description_original_length),
    runnerItem(COPY.compare.runnerCause, rt.cause_label, rt.cause_label_truncated, rt.cause_label_original_length),
  );
  const listWrap = el("div", { class: "runner-item" }, textEl("p", "runner-item-label", COPY.compare.runnerLimitations));
  if (limits.length === 0) {
    listWrap.append(textEl("p", "runner-text", COPY.compare.runnerNone));
  } else {
    listWrap.append(
      el(
        "ul",
        { class: "runner-list" },
        limits.map((item) => textEl("li", "runner-text", item && typeof item.text === "string" ? item.text : COPY.shared.notRecorded)),
      ),
    );
  }
  block.append(listWrap);
  return block;
}

function runnerSection(slots) {
  const loaded = slots.filter((slot) => slot.state === "loaded" || slot.state === "excluded");
  if (loaded.length === 0) {
    return null;
  }
  return el(
    "section",
    { class: "compare-runner", attrs: { "aria-labelledby": "compare-runner-title" } },
    textEl("h2", "section-title", COPY.compare.runnerHeading, { id: "compare-runner-title" }),
    textEl("p", "muted", COPY.compare.runnerNote),
    el("div", { class: "runner-blocks" }, loaded.map(runnerBlock)),
  );
}

// ------------------------------------------------------------ page

export function buildCompare(data) {
  const slots = data.slots;
  const facts = {};
  for (const slot of slots) {
    facts[slot.key] = slotFacts(slot);
  }
  const result = evaluateComparison(facts);
  const page = el("div", { class: "page compare-page" });
  page.append(
    el(
      "section",
      { class: "hero" },
      textEl("h1", "page-title", COPY.compare.heading),
      textEl("p", "subtitle", COPY.compare.intro),
    ),
  );
  const headline = headlineSection(result);
  if (headline !== null) {
    page.append(headline);
  }
  page.append(
    el(
      "section",
      { class: "compare-slot-section", attrs: { "aria-labelledby": "compare-slots-title" } },
      textEl("h2", "section-title", COPY.compare.slotsHeading, { id: "compare-slots-title" }),
      el(
        "div",
        { class: "compare-slots", attrs: { role: "group", "aria-label": COPY.compare.slotsLabel } },
        slots.map((slot) => slotPanel(slot, data.items, data.explicit)),
      ),
    ),
  );
  page.append(factsTable(slots));
  page.append(statementsSection(result));
  page.append(limitationsSection());
  const runner = runnerSection(slots);
  if (runner !== null) {
    page.append(runner);
  }
  return page;
}

export function renderCompare(root, ui) {
  clear(root);
  return ui.loadInto(root, loadCompare, buildCompare);
}
