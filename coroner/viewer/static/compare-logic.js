// Pure comparison logic of the /compare page (Requirement 8).
// No imports, no DOM access, no network. It takes the recorded facts of the three
// slots and returns statement texts, the headline and the omitted reasons. Every
// value comes from the slot facts; nothing is inferred from another run, and no
// cause is ever inferred from ExitCode alone.
//
// Slot facts (one object per key "a", "b", "c"):
//   state              "loaded" | "none" | "unavailable" | "error"
//   run_id             string or null
//   experiment_type    "A" | "B" | "C" (loaded runs)
//   exit_code          integer or null (docker_state.effective.exit_code)
//   oom_killed         boolean or null (docker_state.effective.oom_killed)
//   sample_count       integer or null
//   discrepant_fields  names of fields whose Docker state sources disagree
//   http_status        integer (state "error")

const SLOT_TYPE = { a: "A", b: "B", c: "C" };
const HEADLINE_TEXT = "The same exit code does not always mean the same failure";
const SHARED_EXIT_CODE = 137;
const FIELD_LABEL = { exit_code: "ExitCode", oom_killed: "OOMKilled" };

function show(value) {
  return value === null || value === undefined ? "None" : String(value);
}

function slotProblem(name, slot) {
  const label = name.toUpperCase();
  const state = slot.state;
  if (state === "none") {
    return "slot " + label + ": no saved run for Experiment " + label;
  }
  if (state === "unavailable") {
    return "slot " + label + ": no saved run found for run id " + show(slot.run_id);
  }
  if (state === "error") {
    return (
      "slot " + label + ": run " + show(slot.run_id) + " could not be loaded (HTTP " + show(slot.http_status) + ")"
    );
  }
  if (slot.experiment_type !== SLOT_TYPE[name]) {
    return (
      "slot " + label + ": run " + show(slot.run_id) + " is Experiment " + show(slot.experiment_type) +
      ", expected Experiment " + label
    );
  }
  return null;
}

function valueProblem(name, slot, field) {
  const label = name.toUpperCase();
  const shown = FIELD_LABEL[field];
  const discrepant = Array.isArray(slot.discrepant_fields) ? slot.discrepant_fields : [];
  if (discrepant.indexOf(field) >= 0) {
    return "slot " + label + ": Docker state sources disagree (" + shown + ")";
  }
  if (slot[field] === null || slot[field] === undefined) {
    return "slot " + label + ": " + shown + " not recorded";
  }
  return null;
}

function reasons(slots, names, fields) {
  const out = [];
  for (const name of names) {
    const slot = slots[name];
    const problem = slotProblem(name, slot);
    if (problem !== null) {
      out.push(problem);
      continue;
    }
    for (const field of fields) {
      const reason = valueProblem(name, slot, field);
      if (reason !== null) {
        out.push(reason);
      }
    }
  }
  return out;
}

export function evaluateComparison(slots) {
  const statements = [];
  const omitted = {};
  let headline = null;

  const exitOf = (name) => slots[name].exit_code;
  const oomOf = (name) => slots[name].oom_killed;

  // S1: A differs from B and C by exit code.
  const s1 = reasons(slots, ["a", "b", "c"], ["exit_code"]);
  if (s1.length > 0) {
    omitted.S1 = s1;
  } else if (exitOf("a") !== exitOf("b") && exitOf("a") !== exitOf("c")) {
    statements.push({
      id: "S1",
      text:
        "Experiment A differs from Experiment B and Experiment C by exit code (A=" + exitOf("a") +
        ", B=" + exitOf("b") + ", C=" + exitOf("c") + ")",
    });
  }

  // S2: B and C share ExitCode 137.
  const exitReasons = reasons(slots, ["b", "c"], ["exit_code"]);
  let shared = false;
  if (exitReasons.length > 0) {
    omitted.S2 = exitReasons;
  } else {
    shared = exitOf("b") === SHARED_EXIT_CODE && exitOf("c") === SHARED_EXIT_CODE;
    if (shared) {
      statements.push({ id: "S2", text: "Experiment B and Experiment C share ExitCode 137" });
    }
  }

  // S3 and the headline: only when B and C share 137 and OOMKilled differs.
  if (exitReasons.length > 0 || shared) {
    const s3 = reasons(slots, ["b", "c"], ["exit_code", "oom_killed"]);
    if (s3.length > 0) {
      omitted.S3 = s3;
      omitted.headline = s3.slice();
    } else if (oomOf("b") !== oomOf("c")) {
      statements.push({
        id: "S3",
        text:
          "OOMKilled distinguishes Experiment B (" + String(oomOf("b")) + ") from Experiment C (" +
          String(oomOf("c")) + ")",
      });
      headline = HEADLINE_TEXT;
    }
  }

  // S4: evidence survived all three deaths.
  const s4 = [];
  for (const name of ["a", "b", "c"]) {
    const slot = slots[name];
    const problem = slotProblem(name, slot);
    if (problem !== null) {
      s4.push(problem);
    } else if (slot.sample_count === null || slot.sample_count === undefined) {
      s4.push("slot " + name.toUpperCase() + ": Sample count not recorded");
    } else if (slot.sample_count === 0) {
      s4.push("slot " + name.toUpperCase() + ": no preserved Samples");
    }
  }
  if (s4.length > 0) {
    omitted.S4 = s4;
  } else {
    statements.push({
      id: "S4",
      text:
        "Evidence survived all three deaths in the recorded runs (Samples: A=" + slots.a.sample_count +
        ", B=" + slots.b.sample_count + ", C=" + slots.c.sample_count + ")",
    });
  }

  statements.sort((x, y) => (x.id < y.id ? -1 : x.id > y.id ? 1 : 0));
  return { statements, headline, omitted };
}
