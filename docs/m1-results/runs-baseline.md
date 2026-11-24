# M1.1 runs/ baseline (task 1.1)

Date: 2026-11-21 (system clock of the machine that ran the check)

Before-snapshot of the real `runs/` directory, taken once before any M1.1 Viewer work:

| Item | Value |
|---|---|
| File count | 1217 |
| Total bytes | 10210863 |
| Aggregate SHA-256 (of the snapshot file) | `F1872DAC1C6337A1C3D7CE6AE07CC385A837BC981B834F8318135BE8595E483F` |
| Snapshot file name | `runs-before.snapshot.txt` |

The full per-file listing (relative path, SHA-256, size, sorted by path) is kept outside the repository, read-only, until task 4.8 acceptance. It is never committed.

## Per-run artifact SHA-256 (keyed by run id)

| Run id | manifest.json | evidence.jsonl | docker_inspect.json |
|---|---|---|---|
| a-20261120T133934Z-6d2f2350 | `45f8d5ba881b337a66abf6e6eaac673c527f4db37a19122ccc1389e163fd7b35` | `0896d6cac04f50a19ee88368ecb9bd86e98b1c961be182534b79030d0a5230d1` | `2dc56addce9486cf938c866f3fba8ef227f48b29bb312f806fff3838f3cb067d` |
| a-20261120T134213Z-06deaa12 | `8d132fe4991204612b8f386ccd5d6874897f49877132e1a520dada9059e1de20` | `97fbbe13706af62e5ef42e40cc50c5baa61bcb8ae14814f410809dea1f7ad3ea` | `b321017d8110a7d08013f1a50355d634f2fea577f05b5de17a16e23687276bcb` |
| a-20261120T134252Z-b51dcf11 | `204046791205a1eb53759a0230a79a46dc0fe7e9134da28a20a99b81b66e6f31` | `bdf3d2321db6825b0663f152fe26b3511138f0bf7d2130d4ec56df5353b5fa57` | `a0d1526860b4b591d38c52c0cd8d0fc4bc05cd2c02725b058579b4cebbd1829c` |
| a-20261120T134321Z-a5e5a30e | `b1ad6ab3ea31876e78f59ade23ed80b2f2baff21ac30515933b023dcc2ee84cd` | `0dc10b3f65962c706832397608ec33f4f3d9ec8f2b5c1441556dce336b4eeaa5` | `f0be20d309f143c6935e64434f05918e841fa040e826fec8b9478c3fe5fd5804` |
| a-20261120T211146Z-4300f395 | `22bea488f10fc28632c26387091c8a26ce7ed1ef06a2cc18cd704af76fdd7e1f` | `d04c6cfc9f0798939fb1645feb0d7cb3c42cf69e8184f5ee86d2759f754926d0` | `8bc5643e3963fb9d06cbf9f1663c1dc34c59f4d553526e53deed9c3dddb1e4b5` |
| b-20261120T173541Z-3fa52342 | `415b762d9bc41d781fa5112e534f95957ec8e4fcc73902db31609cc35891cf29` | `00e8f5df59de237c4c4c95951917d5cd5b9a855a0e7e2d52840de5dd18150b94` | `6294b7711129d16ddc65f32a25579b5ff15ef2406b02607b316b1e6c1d52fc1f` |
| b-20261120T211311Z-ce313da1 | `3f8b66a11fbc0e21f0661bd5622bf005886f6d91f155b5bd386cbc810bd09c06` | `c6bc83a93fa56e4832022fa817e2e8efee071c0f7d649f63709376062ded9ba9` | `2a45f805e8feda7910dfa16b20b8933a5a6bb6f664254f22a033e277d5bc3f0a` |
| c-20261120T174958Z-94f4225c | `170d0ae569fc810d3c5a0f2ed3e4d1429b73f282dae05e86635524ca41a2b49c` | `a5e8de221f7da1e9f51d77b0f7103892be97d890fb39dd1fca8e01756403f146` | `1366a7152913f310cf88b2beaf577f072a55e42f45cc99b14ac9df1d3b0949da` |
| c-20261120T211433Z-09b81ca1 | `0324fc41bc1b1254f2b0906701c3da01c87bb81b096483448f7f788a8e2d1fc9` | `3d7ae9cfc73c9af5938481ade226bbcd0ebce56718e16e37fe65edb4f0d9206b` | `0b4fe819692d93a13b91a1b2545303539f967ee17a9318b0794186334b71bafd` |

## Preflight record

- Branch: `feat/coroner-forensic-ui`; `master` exists.
- Python: 3.12.10. Node: not available (`node --version` failed), so the Node vector test will be reported as skipped.
- Pins in `requirements/dev.txt` and installed versions match: fastapi 0.115.6, starlette 0.41.3, uvicorn 0.34.0, httpx 0.28.1, pytest 8.3.4, hypothesis 6.122.3.
- `runs/` is ignored (`.gitignore:223`), `.hypothesis/` is ignored (`.gitignore:51`), and `git ls-files runs` prints nothing.
- A second run of the snapshot command refused: "snapshot exists; refusing to overwrite".

## Task 4.8 after-snapshot (appended)

Date: 2026-11-23 (machine clock, UTC). Taken after the Viewer was launched on `runs/`, all task 4.8 requests were made, and the Viewer was stopped.

| Item | Before (task 1.1) | After (task 4.8) |
|---|---|---|
| File count | 1217 | 1217 |
| Total bytes | 10210863 | 10210863 |
| Aggregate SHA-256 (of the snapshot file) | `F1872DAC1C6337A1C3D7CE6AE07CC385A837BC981B834F8318135BE8595E483F` | `F1872DAC1C6337A1C3D7CE6AE07CC385A837BC981B834F8318135BE8595E483F` |
| Snapshot file name | `runs-before.snapshot.txt` | `runs-after.snapshot.txt` |

Result: identical (`Compare-Object` of the two files printed nothing). A check while the Viewer was still running also showed 0 differing lines. Both snapshot files stay outside the repository, read-only, until the user approves their deletion (checkpoint 4.14).
