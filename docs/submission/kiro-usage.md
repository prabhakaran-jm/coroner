# How Kiro was used (recorded usage only)

This page lists only what the repository records. It does not describe Kiro features that the repository has no trace of. Counts were taken from the files on branch `feat/coroner-m2-demo`.

## Task history is not proof of completion
`tasks.md` checkboxes and `tasks.meta.json` `executionHistory` record that tasks were run and marked complete. The evidence that the resulting code works is the test suite and the acceptance records (`docs/m0-results/`, `docs/m1-results/`), not the task log itself. A task log shows that work was done in Kiro's task workflow. It does not show that the work is correct.

## What the repository records

| Item | Record |
|---|---|
| Specs | `.kiro/specs/coroner-m0/` (`requirements.md` 346 lines, `design.md` 1,573, `tasks.md` 894), `.kiro/specs/coroner-m1-ui/` (`requirements.md` 491, `design.md` 1,562, `tasks.md` 745), `.kiro/specs/coroner-m2-demo/` (`tasks.md` only, 382 lines). |
| Spec workflow type | `.config.kiro` files: `requirements-first` for m0 and m1-ui, `fast-task` for m2-demo. |
| Tasks and sub-tasks | Each `tasks.md` is a numbered task list with sub-tasks, requirement references, `Verify` commands and `Safety` notes. Task status marks (`[x]`, `[-]`, `[~]`, `[ ]`) show progress. |
| Checkpoints | Checkpoint tasks that stop for user review: 9 in m0 (all marked done), 4 in m1-ui (all done), 6 in m2-demo (2 done, 4 open at the time of writing). |
| Execution logs | `tasks.meta.json` holds `executionHistory`: 103 tasks with logs (234 entries) in m0, 56 (122) in m1-ui and 42 (114) in m2-demo. Each entry has an execution id, a chat session id and a timestamp. They record that a task was run, not what was said. |
| Property-test status | `tasks.meta.json` `pbtResults`: 6 entries in m0, all `passed`. m1-ui and m2-demo have none. |

## How the specs were used

- **Requirements, design, tasks.** M0 and M1 started from numbered requirements and a design, then a task list that references requirement numbers. Tests and records cite those numbers (for example `_Requirements: 14.4, 14.5_` lines in `coroner-m0/tasks.md`).
- **Checkpoints.** Each group of tasks ends in a checkpoint that asks for real pytest counts and exit codes before the next group. The m0 checkpoint tasks carry target dates in November 2026.
- **Evidence rules.** The m2-demo task list (Global Rule 6) forbids claiming human testing without observations, and the M1.1 acceptance record forbids recording an expected result as an observed one. Accordingly: human-operated Chrome, 200% zoom and a screen reader are NOT TESTED (`docs/m1-results/m1.1-acceptance.md`).
- **Demo preparation.** `coroner-m2-demo/tasks.md` fixes the demo facts, the Docker and `runs/` safety rules and the human-only tasks (rehearsal, recording, upload). It states that no `requirements.md` or `design.md` exists for that spec.
- **Property-based tests.** The M0 tasks name numbered properties and the repository has 28 `test_prop_*` files with 83 `@given` tests (94 `@given` tests in the whole test tree). See `docs/submission/devpost-writeup.md` for the areas covered.

## Not recorded in the repository

The `.kiro` directory contains only `specs/` (a search of `.kiro` and the repository found no hooks, steering, MCP configuration, powers, Kiro Crew or sub-agent files). The repository records no Kiro hooks, steering documents, MCP servers, Kiro powers or custom agents, and no sub-agent activity. Nothing is claimed about them. The Devpost "how Kiro was used" prompts ask about those features; answer them only from what you actually did, and leave them out otherwise. Whether Kiro IDE, CLI, Web or Crew was used is also not recorded in the repository.

## Sources

`.kiro/specs/*/tasks.md`, `.kiro/specs/*/tasks.meta.json`, `.kiro/specs/*/.config.kiro`, `docs/m1-results/m1.1-acceptance.md`, `README.md`.
