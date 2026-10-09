# Does LLM Co-Evolution Pay Off in RTL Verification? A Cost-Normalised Audit on Ten RTLLM Designs

Enrique Zueco, David Scott Lewis, Haley Yi — AIXC Research, Zaragoza, Spain

AI for Chip Design workshop @ NeurIPS 2026 (poster).

Artefact repository for the paper: per-cell verdict records, ledgers and aggregates, system
modules, drivers, the figure script, the pre-registration, and the camera-ready re-analysis
scripts with their outputs.

## Layout

| Path | Contents |
|---|---|
| `e1-1/` … `e1-4/` | Per-cell verdict records for all 150 main-experiment (E1) cells (10 designs x 3 seeds x 5 systems). Split across four directories only because of a per-directory file cap on the host; together they correspond to `runs/e1/` in the paper. |
| `e1_pilot/` | Formal pilot receipts (18 verdict files). |
| `e2_pilot/` | The 36-cell transfer pilot (E2). |
| `e3/` | Anchor (hidden-oracle) audit report (`audit_report.json`). |
| `evidence/` | Ledgers and aggregates the paper's tables are stamped from (E1 ledger and aggregate, paired and design-clustered paired bootstrap, combinational-slice CI, price sweep, observable-mutant filter, E2 pilot ledger and aggregate, claim-to-evidence map) and the sequestered anchor's SHA-256 manifest (`evidence/anchor_sequestered/HASH_MANIFEST.txt`). |
| `evidence/kr1_seq_pre_amendment_5_backup/` | The nine deterministic-allocator verdicts on the sequential designs (accu, JC_counter, LFSR x 3 seeds) as recorded before freeze amendment 5. |
| `evidence/e1_agy_call_manifest.jsonl` | LLM call log for the main experiment (`runs/e1/agy_call_manifest.jsonl` in the paper): one line per call with UTC time, model id, prompt/response SHA-256 and lengths, estimated tokens, wall seconds and outcome. No prompt or response text. |
| `modules/` | System modules (generator, allocator, mutation, anchor audit) and typed verdict records. |
| `scripts/` | Per-cell driver, grid runner, ledger population, paired and design-clustered bootstrap, price sweep, observable-mutant filter, anchor-audit smoke test. |
| `figs/` | Figure script `e1_e2_mkc.py` (reads the ledgers) and its output. |
| `experiments/` | Frozen grid definitions (`e1_grid.jsonl`, `e1_grid_seq_extension.jsonl`, `e2_pilot_grid.jsonl`). The E1 grid interleaves all five systems within each (design, seed). |
| `camera_ready/analysis/` | Camera-ready re-analysis scripts (`cr_*.py`, `run_all.sh`) and their outputs (`cr_*.out`). |
| `HYPOTHESIS_REFINED.sanitized.json` | The pre-registration, including the freeze-amendment log. |
| `SANITIZATION_NOTE.md` | What was changed relative to the private repository (absolute paths; model-name redaction in the original bundle). |

## Reproducing

Camera-ready numbers (kill counts, ratio of sums, separate budgets, price-sweep intervals, unit
reconciliation, per-design kills, formal-outcome breakdown):

    bash camera_ready/analysis/run_all.sh

The scripts read only the E1 verdicts, the call manifest and the pre-amendment verdicts. Set
`OUT_DIR=<dir>` to write the outputs elsewhere and compare them with the shipped `cr_*.out`
files; they are byte-identical.

Per cell: `python3 scripts/run_e1_cell.py '<grid line>' --runs-dir e1-1`.
Aggregates: `scripts/populate_ledger.py`, `populate_e2_pilot.py`, `paired_bootstrap.py`,
`paired_bootstrap_clustered.py`, `price_sensitivity.py`, `observable_mkc.py`.
Figure: `python3 figs/e1_e2_mkc.py`.
Toolchain: OSS CAD Suite (yosys, SymbiYosys, z3, cvc5, boolector, bitwuzla, yices, iverilog,
verilator), native ARM64.

`build.sh` is not included: it depends on the private repository. Its outputs (the stamped
ledgers and aggregates) are included in `evidence/`.

## Reading the verdict records

Each `*.verdict.json` holds the cell definition, the typed verdict list and a summary. Outcomes
are `mutation_killed`, `mutation_survived`, `engine_error` and `timeout`. For the formal miter,
`mutation_survived` means PASS of a k=2 prove-mode miter (an induction proof), not a general
equivalence proof. Of the bounded-formal-only arm's 129 checks, 36 killed the mutant, 8 returned
PASS, 1 timed out and 84 are recorded as engine errors by a coarse classifier that maps any
other solver status (including inconclusive induction) to `engine_error`; solver output was not
saved. These outcomes should not be collapsed into killed vs. not killed.
