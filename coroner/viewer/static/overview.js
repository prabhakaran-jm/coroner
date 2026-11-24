// Overview page: three incident cards (A, B, C) and the list of other runs.
// Fields come from the GET /api/runs list items. Every artifact value is
// shown as text only. Null is shown as "not recorded", never as 0 or false.

import { COPY } from "/static/copy.js";
import { fetchRuns } from "/static/api.js";
import { el, textEl } from "/static/dom.js";

const CARD_TYPES = ["A", "B", "C"];

function runHref(runId) {
  return "/runs/" + encodeURIComponent(runId);
}

function verdictClass(verdict) {
  if (verdict === "PASS") {
    return "badge badge-pass";
  }
  if (verdict === "FAIL") {
    return "badge badge-fail";
  }
  return "badge badge-unknown";
}

function verdictBadge(verdict) {
  const label = typeof verdict === "string" && verdict !== "" ? verdict : COPY.shared.verdictUnknown;
  const known = label === "PASS" || label === "FAIL" || label === "UNKNOWN";
  return textEl("span", verdictClass(known ? label : "UNKNOWN"), label);
}

function show(value) {
  return value === null || value === undefined || value === "" ? COPY.shared.notRecorded : String(value);
}

function showBool(value) {
  if (value === true) {
    return COPY.shared.boolTrue;
  }
  if (value === false) {
    return COPY.shared.boolFalse;
  }
  return COPY.shared.notRecorded;
}

function row(label, valueNode) {
  return el("div", { class: "fact" }, textEl("dt", "fact-label", label), el("dd", { class: "fact-value" }, valueNode));
}

function mono(value) {
  return textEl("span", "mono", value);
}

function plain(value) {
  return textEl("span", "", value);
}

function stateSourceText(source) {
  if (typeof source === "string" && Object.prototype.hasOwnProperty.call(COPY.dockerSource, source)) {
    return COPY.dockerSource[source];
  }
  return COPY.shared.notRecorded;
}

function emptyCard(type) {
  return el(
    "article",
    { class: "card card-empty", attrs: { "aria-labelledby": "card-title-" + type } },
    cardHeader(type),
    textEl("p", "card-note", COPY.shared.noSavedRun),
    el(
      "dl",
      { class: "facts" },
      row(COPY.overview.runIdLabel, plain(COPY.shared.notRecorded)),
      row(COPY.shared.verdictLabel, plain(COPY.shared.notRecorded)),
      row(COPY.overview.exitCodeLabel, plain(COPY.shared.notRecorded)),
      row(COPY.overview.oomKilledLabel, plain(COPY.shared.notRecorded)),
      row(COPY.overview.sampleCountLabel, plain(COPY.shared.notRecorded)),
      row(COPY.overview.collectorSurvivalLabel, plain(COPY.shared.notRecorded)),
    ),
  );
}

function cardHeader(type) {
  const title = textEl("h2", "card-title", COPY.overview.cardTitle + type, { id: "card-title-" + type });
  const parts = [title];
  if (type === "A") {
    parts.push(textEl("p", "card-desc", COPY.overview.experimentADescription));
  }
  return el("header", { class: "card-head" }, parts);
}

function collectorText(value) {
  return value === true ? COPY.overview.collectorSurvived : COPY.overview.collectorNotSurvived;
}

function runCard(type, item) {
  const facts = [
    row(COPY.overview.runIdLabel, mono(item.run_id)),
    row(COPY.shared.verdictLabel, verdictBadge(item.verdict)),
    row(COPY.overview.exitCodeLabel, mono(show(item.exit_code))),
    row(COPY.overview.oomKilledLabel, mono(showBool(item.oom_killed))),
    row(COPY.overview.sampleCountLabel, mono(show(item.sample_count))),
  ];
  if (item.collector_survival === true || item.collector_survival === false) {
    facts.push(row(COPY.overview.collectorSurvivalLabel, plain(collectorText(item.collector_survival))));
  }
  facts.push(row(COPY.overview.stateSourceLabel, plain(stateSourceText(item.docker_state_source))));
  if (item.verdict === "UNKNOWN" && item.reason) {
    facts.push(row(COPY.overview.reasonLabel, mono(item.reason)));
  }
  const children = [cardHeader(type)];
  if (item.docker_state_discrepancy === true) {
    children.push(textEl("p", "discrepancy", COPY.shared.discrepancy));
  }
  children.push(el("dl", { class: "facts" }, facts));
  children.push(
    el(
      "a",
      { class: "button button-link", attrs: { href: runHref(item.run_id) } },
      COPY.overview.openIncident,
      el("span", { class: "visually-hidden" }, " ", COPY.overview.cardTitle + type),
    ),
  );
  return el("article", { class: "card", attrs: { "aria-labelledby": "card-title-" + type } }, children);
}

function typeOf(item) {
  return item.experiment_type === "A" || item.experiment_type === "B" || item.experiment_type === "C"
    ? item.experiment_type
    : null;
}

function experimentText(item) {
  const type = typeOf(item);
  if (type !== null) {
    return type;
  }
  return show(item.experiment);
}

function listRow(item) {
  const detail = item.verdict === "UNKNOWN" && item.reason ? mono(item.reason) : plain("");
  return el(
    "tr",
    {},
    el("td", {}, el("a", { class: "text-link mono", attrs: { href: runHref(item.run_id) } }, item.run_id)),
    el("td", {}, mono(experimentText(item))),
    el("td", {}, verdictBadge(item.verdict)),
    el("td", {}, detail),
  );
}

function runList(items) {
  const section = el("section", { class: "run-list", attrs: { "aria-labelledby": "run-list-title" } });
  section.append(textEl("h2", "section-title", COPY.overview.listHeading, { id: "run-list-title" }));
  if (items.length === 0) {
    section.append(textEl("p", "muted", COPY.overview.listEmpty));
    return section;
  }
  const table = el(
    "table",
    { class: "table" },
    textEl("caption", "visually-hidden", COPY.overview.listCaption),
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        textEl("th", "", COPY.overview.colRunId, { scope: "col" }),
        textEl("th", "", COPY.overview.colExperiment, { scope: "col" }),
        textEl("th", "", COPY.overview.colVerdict, { scope: "col" }),
        textEl("th", "", COPY.overview.colDetail, { scope: "col" }),
      ),
    ),
    el("tbody", {}, items.map(listRow)),
  );
  section.append(el("div", { class: "table-wrap" }, table));
  return section;
}

export function buildOverview(data) {
  const runs = Array.isArray(data.runs) ? data.runs : [];
  const label = typeof data.runs_dir_label === "string" ? data.runs_dir_label : "";

  const chosen = new Map();
  for (const item of runs) {
    const type = typeOf(item);
    if (type !== null && !chosen.has(type)) {
      chosen.set(type, item);
    }
  }
  const others = runs.filter((item) => !Array.from(chosen.values()).includes(item));

  const page = el("div", { class: "page" });
  page.append(
    el(
      "section",
      { class: "hero" },
      textEl("p", "brand-line", COPY.shared.brand),
      textEl("h1", "page-title", COPY.shared.heading),
      textEl("p", "subtitle", COPY.shared.subtitle),
    ),
  );

  if (data.truncated === true) {
    const listed = Number.isInteger(data.listed) ? data.listed : runs.length;
    const total = Number.isInteger(data.total_found) ? data.total_found : runs.length;
    page.append(
      textEl(
        "p",
        "notice",
        COPY.overview.truncatedPrefix + listed + COPY.overview.truncatedMiddle + total + COPY.overview.truncatedSuffix,
        { role: "status" },
      ),
    );
  }

  if (runs.length === 0) {
    page.append(
      el(
        "section",
        { class: "state state-empty" },
        textEl("h2", "state-title", COPY.overview.emptyTitle),
        el(
          "p",
          {},
          COPY.overview.emptyBefore,
          el("span", { class: "mono" }, label !== "" ? label : COPY.shared.notRecorded),
          COPY.overview.emptyAfter,
        ),
      ),
    );
  }

  const cards = el("section", { class: "cards", attrs: { "aria-label": COPY.overview.cardsLabel } });
  for (const type of CARD_TYPES) {
    const item = chosen.get(type);
    cards.append(item ? runCard(type, item) : emptyCard(type));
  }
  page.append(cards);
  if (runs.length > 0) {
    page.append(runList(others));
  }
  return page;
}

export async function renderOverview(root, ui) {
  await ui.loadInto(root, fetchRuns, buildOverview);
}
