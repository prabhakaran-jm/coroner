# Kiroween 2026 submission checklist

Status labels: DONE (evidence in the repository or a fetched page), PENDING (an action remains, usually human-only), BLOCKED (waits on something else), DEFERRED (a decision to postpone).

Checked on 2026-11-25. Nothing in this file was submitted to Devpost, and the Devpost page was not modified. **Devpost competition submission: not confirmed.** This file records no evidence that a submission exists.

## 1. Sources for the official requirements (paraphrased)

Fetched on 2026-11-25. Content was rephrased for compliance with licensing restrictions. Read the pages yourself before submitting, because rules can change. A later automated re-request of both Devpost URLs returned HTTP 403 (the site answered but refused a non-browser client), so open them in a browser.

- Kiroween 2026 overview and requirements: https://kiroween.devpost.com/
- Official Rules: https://kiroween.devpost.com/rules

What those pages say, in brief:

- **Deadline:** Friday, 4 December 2026, 2:00 PM Pacific Time. The Submission Period began on 30 October 2026 (9:00 AM PT). In UK time (GMT in December) the deadline is 22:00 on 4 December.
- **New projects only:** the work must have been built during the Submission Period. Pre-existing code beyond standard frameworks and libraries must be disclosed.
- **Build:** a working software application that uses Kiro (IDE, CLI, Web and/or Kiro Crew), entered in one category: Resurrection, Frankenstein, Skeleton Crew or Costume Contest.
- **Repository:** a public GitHub repository (not GitLab or Bitbucket) containing all source, assets and instructions, with an open-source license file that GitHub detects and shows in the About section.
- **Root `.kiro`:** the repository must contain `/.kiro` at its root, and `.kiro` or its sub-folders must not be in `.gitignore`.
- **Video:** under three minutes (judges need not watch longer), showing the project working on the device it was built for, public on YouTube, with no unlicensed third-party trademarks or copyrighted music.
- **Text description** of features and functionality.
- **Kiro write-up:** how Kiro was used (the rules list IDE/CLI/Web/Crew, custom agents, hooks, specs, steering, MCP, property-based testing and powers as examples of what to cover).
- **Testing access:** a link to a website, functioning demo or test build, with testing instructions in the submission form if access is private.
- **Category and bonus categories** must be identified in the submission. Bonus prizes (blog, social post, startup, student, Ghost of Kiroween Past, Kiro Crew app) each have extra requirements. Best Demo needs no extra step.
- **Eligibility:** teams of up to three; some countries and territories are excluded (see the overview page). No Devpost-specific eligibility check was made for the author.

## 2. Requirements checklist

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Submit before 4 Dec 2026, 2:00 PM PT (22:00 UK) | PENDING | Deadline from the Devpost overview and rules pages. Not yet submitted as far as this repository shows |
| 2 | Project built during the Submission Period (30 Oct to 4 Dec 2026) | DONE | `git log --reverse`: first commit `bbc74dd` dated 2026-11-13, M0 commit dated 2026-11-14. Disclose any pre-existing work in the description if there is any |
| 3 | Built with Kiro | DONE | `.kiro/specs/` (3 specs), `.kiro/specs/*/tasks.meta.json` execution logs; see `kiro-usage.md`. The task log records that tasks were run; the tests and acceptance records are the evidence that the code works |
| 4 | Public GitHub repository | DONE | `https://github.com/prabhakaran-jm/coroner` returned its README without login on 2026-11-25. Confirm in a private browser window |
| 5 | Open-source license file in the repository | DONE | `LICENSE` is the MIT License, copyright 2026 Prabhakaran Jayaraman Masani, tracked by git. The rules name no approved-license list on the pages fetched |
| 6 | License visible in the repository About section | PENDING | Not checked on GitHub's page. Open the repository page and confirm "MIT license" appears in About |
| 7 | `/.kiro` at the repository root, not ignored | DONE | 13 tracked files under `.kiro/`; `git check-ignore -v .kiro` printed nothing; `.gitignore` has no `.kiro` entry |
| 8 | All source, assets and instructions in the repository | DONE | `coroner/`, `scripts/`, `requirements/`, `docs/`, `README.md`. Real `runs/` is gitignored on purpose (see section 5 below) |
| 9 | Working project or reproducible test build | BLOCKED | `JUDGING.md` is written and was verified on Windows on 2026-11-25 with Python 3.12.10. It is not on `master` until PR #3 merges, so judges cannot reach it until then |
| 10 | Install and test instructions | DONE | `README.md` section 3 and the Viewer section; `JUDGING.md` |
| 11 | Text description | PENDING | Draft in `docs/submission/devpost-writeup.md`. The user must paste it on Devpost |
| 12 | Kiro usage write-up | PENDING | Draft facts in `docs/submission/kiro-usage.md`. Add only what you actually did with hooks, steering, MCP, powers or custom agents; the repository records none |
| 13 | Demo video under 3 minutes | PENDING | Not recorded. `docs/m2-plan/recording-script.md` budgets 2:42 and says it is not rehearsed |
| 14 | Video public on YouTube | PENDING | No YouTube URL exists |
| 15 | Video shows the project on its device, no unlicensed material | PENDING | Applies when recorded. The recording script lists Clipchamp, and says to disclose a synthetic voice if ElevenLabs is used |
| 16 | Human rehearsal (timed, read aloud) | PENDING | `recording-script.md`: "NOT YET REHEARSED". Normal-Chrome checks, 200% zoom and screen reader are NOT TESTED |
| 17 | Category chosen and entered on Devpost | PENDING | Recommendation: Frankenstein (proposed, decision pending). The live Devpost category is unchanged. See section 4 and the "Category: Frankenstein (proposed)" section of `devpost-writeup.md` |
| 18 | Testing instructions field | PENDING | Text in `docs/submission/judge-testing-instructions.md`. Link is live only after PR #3 merges |
| 19 | Replace any old Devpost draft | PENDING | The old draft is not in this repository. See section 3 |
| 20 | Accurate claims (what Coroner does and does not prove) | DONE in repo docs | README, JUDGING.md and writeup draft follow the demo facts in `.kiro/specs/coroner-m2-demo/tasks.md`. Recheck the pasted Devpost text |
| 21 | Viewer never publicly hosted | DONE | Viewer binds `127.0.0.1` only (`coroner/viewer/cli.py`); no hosted site exists or is claimed |
| 22 | Bonus prizes (optional) | PENDING | Blog and Social Blitz each need a public post in the Submission Period with required wording and hashtag `#KiroweenHackathon26`. None is recorded. Best Demo needs nothing extra |
| 23 | Devpost competition submission itself | PENDING | **Not confirmed.** Devpost is the only place that can show a final submission |

## 3. Old Devpost draft

`grep` for `Costume`, `Frankenstein` and `Devpost` across the repository (excluding `.git`, `.hypothesis`, `.venv`, `runs`) found no old Devpost draft. `Costume` and `Frankenstein` had no hits at all. The `Devpost` and `Kiroween` hits were the README tagline, a requirements user story ("hackathon reviewer"), `.kiro/specs/coroner-m2-demo/tasks.md` and its `tasks.meta.json`, `docs/m2-plan/` notes and the M1.1 acceptance decision. The search ran before the `docs/submission/` files were written.

The old draft is therefore not in the repository. The user must replace it manually on Devpost. This file cannot list its outdated claims, because its content is unknown here. When replacing it, check that the new text does not repeat any of these (all are unsupported by the records): a hosted or public Viewer, request or event-loop timelines, synthetic runs as proof of the real experiments, Linux or macOS testing, human testing of the Viewer, or Kiro sub-agent activity.

## 4. Category assessment: Costume Contest or Frankenstein

Recommendation only. Nothing was changed.

Official definitions (https://kiroween.devpost.com/ and https://kiroween.devpost.com/rules, paraphrased):

- **Frankenstein:** stitch a chimera of technologies into one app, joining seemingly incompatible elements into something unexpectedly powerful.
- **Costume Contest:** any app, with a haunting, polished, unforgettable user interface that brings in spooky design elements which enhance the app's function.
- Both categories have a $5,000 category prize, and every category is scored on the same criteria (potential impact, technological implementation, design, innovation and presentation).

What the repository records:

- The Viewer's stylesheet describes a "dark graphite incident-response theme" (`coroner/viewer/static/style.css`). The records show no spooky or Halloween design elements. The product name and the "What survived after the process died?" subtitle give a forensic theme, which is not the same as a haunting interface.
- The components are a Python Probe, a FastAPI Collector, a Docker Compose Runner, JSON Lines evidence on a named volume and a standard-library web Viewer. They work together, but none of them is an obviously incompatible pairing.

Assessment:

- **Costume Contest** asks for spooky design that enhances function. The repository does not show that, and the project's strength is evidence discipline, not a haunting look. Entering it risks a visible mismatch with the definition.
- **Frankenstein** fits only moderately. The honest case is a chimera of container runtime facts (Docker inspect), telemetry preserved outside a dying process, and a read-only forensic viewer, built to a spec-driven, property-tested workflow. A judge could still read that as an ordinary toolchain.
- **Recommendation:** Frankenstein is the closer fit to the written definition, provided the description frames the stitching honestly and does not overstate it. Choose Costume Contest only if you add spooky design elements to the Viewer and show them in the video, which is a code change that is not planned. The user decides.

## 5. Real and synthetic evidence

- The real saved runs (`a-20261120T211146Z-4300f395`, `b-20261120T211311Z-ce313da1`, `c-20261120T211433Z-09b81ca1`) are not published (`runs/` is gitignored). The Devpost text and the video must say so.
- The judge build uses synthetic runs from `scripts/make-sample-runs.py`. Label them synthetic wherever they appear.

## 6. Phase 7 CI: DEFERRED

No `.github/workflows/` directory exists (`Test-Path .github` returned False on 2026-11-25), so there is no continuous integration. It is DEFERRED: the rules do not ask for CI, and Docker is not needed for the proposed job.

Proposed minimal workflow, described in text only. It was not written to disk and is **untested on a GitHub runner**:

- Trigger: `push` and `pull_request` on `master`.
- One job on `ubuntu-latest` (a Windows runner would match the verified platform better, but the PowerShell preflight test is Windows-only; choose one and say which in the file).
- Steps: check out the repository; set up Python 3.12; `python -m pip install -r requirements/dev.txt`; run `python -m pytest tests/unit -q -p no:cacheprovider -rs`.
- No Docker, no `--run-docker`, no secrets, read-only permissions (`contents: read`).
- Caveat: the project has been tested only on Windows. On Linux, some tests may fail or skip (for example the PowerShell preflight test and symlink or browser tests). Treat a first Linux run as a finding, not a regression, and do not claim Linux support from a green run unless the user decides to.

## 7. Open items for the user, in order

1. Merge PR #3, then open the `JUDGING.md` link in a private window.
2. Rehearse and time the video, record it, upload it to YouTube as public, and keep it under 3 minutes.
3. Choose the category and paste `devpost-writeup.md`, the Kiro write-up and `judge-testing-instructions.md` into Devpost, adding the YouTube URL.
4. Confirm the license shows in the GitHub About section.
5. Submit on Devpost before 22:00 UK time on 4 December 2026 and confirm that Devpost shows the submission as submitted.
