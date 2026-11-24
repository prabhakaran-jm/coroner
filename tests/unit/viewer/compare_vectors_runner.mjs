// Runs the golden vectors through compare-logic.js and prints one JSON object {vector id: result}.
// Usage: node compare_vectors_runner.mjs <path to compare_vectors.json>
// No npm package and no package.json: the module is loaded as text through a data: URL.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const logicPath = resolve(here, "..", "..", "..", "coroner", "viewer", "static", "compare-logic.js");
const vectorsPath = process.argv[2];
if (!vectorsPath) {
  console.error("usage: node compare_vectors_runner.mjs <compare_vectors.json>");
  process.exit(2);
}

const source = readFileSync(logicPath);
const mod = await import("data:text/javascript;base64," + source.toString("base64"));
const vectors = JSON.parse(readFileSync(vectorsPath, "utf8")).vectors;
const results = {};
for (const vector of vectors) {
  results[vector.id] = mod.evaluateComparison(JSON.parse(JSON.stringify(vector.slots)));
}
console.log(JSON.stringify(results));
