// The one shared rule for how the recorded OOMKilled value may be worded (incident
// Docker state section and Evidence vs Uncertainty panel). Pure and DOM-free: it only
// reads the detail docker_state object (source, manifest, raw, effective, discrepancies)
// and returns text from copy.js. It never infers a value from ExitCode, from the
// experiment label or from the memory limit.
//
// Kinds:
//   agree     manifest and raw inspect both record OOMKilled, and the values are equal
//   conflict  the two sources record different values (or the payload lists an
//             oom_killed discrepancy); neither source is selected as proof
//   single    exactly one source records OOMKilled (manifest only or raw inspect only)
//   none      OOMKilled is not recorded by any source
// The out-of-memory mechanism sentence is allowed for "agree" with true, and for
// "single" with true (attributed to that one source, never as corroborated).

import { COPY, fill } from "/static/copy.js";

function isBool(value) {
  return value === true || value === false;
}

function isInt(value) {
  return typeof value === "number" && Number.isInteger(value);
}

function block(value) {
  return value !== null && typeof value === "object" ? value : {};
}

function discrepancyOf(ds, name) {
  const list = Array.isArray(ds.discrepancies) ? ds.discrepancies : [];
  for (const d of list) {
    if (d && typeof d === "object" && d.field === name) {
      return d;
    }
  }
  return null;
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

function plainText(value) {
  return value === null || value === undefined || value === "" ? COPY.shared.notRecorded : String(value);
}

// Classifies the recorded OOMKilled evidence of one run.
export function oomState(dockerState) {
  const ds = block(dockerState);
  const man = block(ds.manifest);
  const raw = block(ds.raw);
  const eff = block(ds.effective);
  const oomDisc = discrepancyOf(ds, "oom_killed");
  const exitDisc = discrepancyOf(ds, "exit_code");

  let manifest = isBool(man.oom_killed) ? man.oom_killed : null;
  let rawValue = isBool(raw.oom_killed) ? raw.oom_killed : null;
  if (oomDisc !== null) {
    if (manifest === null && isBool(oomDisc.manifest_value)) {
      manifest = oomDisc.manifest_value;
    }
    if (rawValue === null && isBool(oomDisc.raw_value)) {
      rawValue = oomDisc.raw_value;
    }
  }
  const exitDisputed =
    exitDisc !== null || (isInt(man.exit_code) && isInt(raw.exit_code) && man.exit_code !== raw.exit_code);

  const base = { manifest: manifest, raw: rawValue, exitDisputed: exitDisputed, value: null, source: null };
  if (oomDisc !== null || (manifest !== null && rawValue !== null && manifest !== rawValue)) {
    return Object.assign(base, { kind: "conflict" });
  }
  if (manifest !== null && rawValue !== null) {
    return Object.assign(base, { kind: "agree", value: manifest });
  }
  if (manifest !== null) {
    return Object.assign(base, { kind: "single", value: manifest, source: "manifest" });
  }
  if (rawValue !== null) {
    return Object.assign(base, { kind: "single", value: rawValue, source: "raw" });
  }
  if (isBool(eff.oom_killed)) {
    // an effective value without either source block: attribute it to one unnamed source only
    return Object.assign(base, { kind: "single", value: eff.oom_killed, source: "unknown" });
  }
  return Object.assign(base, { kind: "none" });
}

function sourceName(state) {
  const key = "oomSource_" + String(state.source);
  return Object.prototype.hasOwnProperty.call(COPY.evidence, key) ? COPY.evidence[key] : COPY.evidence.oomSource_unknown;
}

// The wording allowed for the state. Each member is a string or null:
//   rowValue   replaces the single "Docker OOMKilled" value when the sources conflict
//   mechanism  the out-of-memory mechanism sentence (Experiment C only)
//   conflict   the explicit conflict / not-established sentence
//   singleNote attribution of a one-source value, "not corroborated by a second source"
export function oomStatements(state, isExperimentC, exitCode) {
  const out = { rowValue: null, mechanism: null, conflict: null, singleNote: null };
  if (state.kind === "conflict") {
    const values = { manifest: boolText(state.manifest), raw: boolText(state.raw) };
    out.rowValue = fill(COPY.evidence.oomConflictValue, values);
    out.conflict = fill(COPY.evidence.oomConflict, values);
    return out;
  }
  if (state.kind === "agree" && state.value === true && isExperimentC) {
    out.mechanism = state.exitDisputed
      ? fill(COPY.evidence.oomMechanismExitDisputed, { oom: boolText(true) })
      : fill(COPY.evidence.oomMechanism, { oom: boolText(true), code: plainText(exitCode) });
  } else if (state.kind === "single") {
    if (state.value === true && isExperimentC) {
      out.mechanism = fill(COPY.evidence.oomMechanismSingle, { source: sourceName(state) });
    } else {
      out.singleNote = fill(COPY.evidence.oomSingleNote, { value: boolText(state.value), source: sourceName(state) });
    }
  }
  return out;
}
