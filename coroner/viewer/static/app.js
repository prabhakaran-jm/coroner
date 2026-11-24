// Page router. Chooses the page from location.pathname and renders it into
// <main id="main">. Page modules other than the overview are imported lazily,
// so the overview works even when they are missing.

import { COPY } from "/static/copy.js";
import { clear, el, textEl } from "/static/dom.js";
import { renderOverview } from "/static/overview.js";

function loadingNode() {
  return el(
    "div",
    { class: "state state-loading", attrs: { role: "status", "aria-live": "polite" } },
    COPY.states.loading,
  );
}

function errorMessage(result) {
  switch (result.status) {
    case 0:
      return COPY.states.errorUnreachable;
    case 400:
      return COPY.states.error400;
    case 404:
      return COPY.states.error404;
    case 500:
      return COPY.states.error500;
    default:
      return COPY.states.errorOther + String(Number.isInteger(result.status) ? result.status : 0);
  }
}

function errorPanel(result, retry) {
  const button = textEl("button", "button", COPY.states.retry, { type: "button" });
  button.addEventListener("click", retry);
  return el(
    "section",
    { class: "state state-error", attrs: { role: "alert" } },
    textEl("h2", "state-title", COPY.states.errorTitle),
    textEl("p", "", errorMessage(result)),
    button,
  );
}

// Shows a loading state, runs loader(), then renders build(data) or an error
// panel with a retry button.
async function loadInto(root, loader, build) {
  clear(root);
  root.append(loadingNode());
  const result = await loader();
  clear(root);
  if (!result.ok) {
    root.append(errorPanel(result, () => loadInto(root, loader, build)));
    return;
  }
  let node;
  try {
    node = build(result.data);
  } catch (err) {
    root.append(errorPanel({ ok: false, status: 500 }, () => loadInto(root, loader, build)));
    return;
  }
  root.append(node);
}

function unavailablePanel(root) {
  clear(root);
  root.append(
    el(
      "section",
      { class: "state state-error", attrs: { role: "alert" } },
      textEl("h2", "state-title", COPY.states.errorTitle),
      textEl("p", "", COPY.states.pageUnavailable),
    ),
  );
}

const UI = Object.freeze({ loadInto, loadingNode, errorPanel });

async function route() {
  const root = document.getElementById("main");
  if (!root) {
    return;
  }
  const path = location.pathname;
  if (path === "/") {
    await renderOverview(root, UI);
    return;
  }
  if (path === "/compare") {
    try {
      const mod = await import("/static/compare.js");
      await mod.renderCompare(root, UI);
    } catch (err) {
      unavailablePanel(root);
    }
    return;
  }
  if (path.startsWith("/runs/")) {
    let runId;
    try {
      runId = decodeURIComponent(path.slice("/runs/".length));
    } catch (err) {
      clear(root);
      root.append(errorPanel({ ok: false, status: 400 }, () => location.reload()));
      return;
    }
    try {
      const mod = await import("/static/incident.js");
      await mod.renderIncident(root, runId, UI);
    } catch (err) {
      unavailablePanel(root);
    }
    return;
  }
  clear(root);
  root.append(errorPanel({ ok: false, status: 404 }, () => location.reload()));
}

route();
