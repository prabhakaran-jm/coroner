# Devpost testing instructions (text for the testing field)

Paste the text between the lines into the Devpost testing-instructions field. It does not need login credentials.

Status: the link below works only after pull request #3 is merged into `master`. Until then it returns a 404. Check it in a private window before submitting. No hosted site exists and none is claimed.

---

Coroner is tested locally. There is no hosted site. No login is needed. No Docker is needed.

Instructions: https://github.com/prabhakaran-jm/coroner/blob/master/JUDGING.md

Summary: install Python 3.12 and the pinned requirements (`requirements/dev.txt`), generate three synthetic sample runs with `scripts/make-sample-runs.py --out <new folder>`, start the Viewer with `python -m coroner.viewer --runs-dir <that folder>`, and open http://127.0.0.1:8765/ on the same machine. Expected: A has ExitCode 1 and OOMKilled false, B has 137 and false, C has 137 and true. The Compare page shows the headline "The same exit code does not always mean the same failure".

The sample runs are synthetic. They are not the real Docker experiments, which were recorded on one Windows host with Docker Desktop and are planned to be shown in the demo video (to be added). Verified on Windows with Python 3.12.10. Linux and macOS are untested.

---
