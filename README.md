# Anonymous artifact — "LLM Verification Loops Do Not Beat Formal Bounded Miters at Matched Scalar Cost"

Supplementary artifact for an anonymous NeurIPS 2026 **AI for Chip Design** workshop submission.
The workshop's submission form accepts only a PDF, so this repository carries the material that
would otherwise be supplementary.

## What is here

| Path | Contents |
|---|---|
| `evidence/` | Every stamped ledger and aggregate the paper's tables are generated from: E1 ledger + aggregate, paired-bootstrap CIs, combinational-slice CI, price sweep, observable-mutant filter, E2 pilot aggregate/ledger, claim-to-evidence map, and the sequestered anchor's SHA-256 manifest. |
| `e1-1/` … `e1-4/` | Per-cell verdict records for all **150 E1 cells** (10 designs x 3 seeds x 5 systems). Split across four directories only because the hosting service caps files per directory; together they are the complete E1 set. |
| `e1_pilot/`, `e2_pilot/`, `e3/` | Per-cell records for the formal pilot receipts, the 36-cell E2 transfer pilot, and the hidden-oracle audit report. |
| `modules/` | The four system modules (generator, allocator, mutation, anchor-audit) plus typed verdict records. |
| `scripts/` | Per-cell driver, grid runner, ledger population, paired bootstrap, price sweep, observable-mutant filter, anchor-audit smoke test. |
| `figs/` | The plotting script for the paper's figure; it reads the ledgers at run time (no hardcoded data). |
| `experiments/`, `e1_grid.jsonl`, `e2_pilot_grid.jsonl` | The frozen grid definitions. `e1_grid.jsonl` shows that the E1 grid interleaves all five systems within each (design, seed) — the property the paper's load-sensitivity appendix relies on. |
| `HYPOTHESIS_REFINED.sanitized.json` | The pre-registration, including the full freeze-amendment log with each amendment's direction-of-effect. |
| `SANITIZATION_NOTE.md` | Exactly what was redacted for anonymity (tooling/model names and absolute paths). No numeric value, verdict outcome, hash or timestamp was altered. |

## Reproducing

Per-cell: `python3 scripts/run_e1_cell.py '<grid line>' --runs-dir e1-1`
Aggregates: `python3 scripts/populate_ledger.py`, `populate_e2_pilot.py`, `paired_bootstrap.py`,
`price_sensitivity.py`, `observable_mkc.py`. Figure: `python3 figs/e1_e2_mkc.py`.
Toolchain: OSS CAD Suite (yosys, SymbiYosys, z3, cvc5, boolector, bitwuzla, yices, iverilog,
verilator), native ARM64.

**Note on `build.sh`:** gates 2, 3, 4 and 7 import a helper package that is not included in this
anonymized snapshot because it contains identifying strings. The LaTeX compile and page-cap gates
run as-is; the anti-fabrication and citation gates are described in the paper and their outputs
(the stamped ledgers) are included here in full.

## Reading the verdict records

Each `*.verdict.json` holds the cell definition, the typed verdict list, and a summary. Outcomes
are `mutation_killed`, `mutation_survived` (proved equivalent), `engine_error`, and `timeout`.
The paper's Appendix on metric composition explains why these four must not be collapsed into
"killed vs not killed" — for the bounded-formal-only arm, 84 of 129 mutant verdicts are engine
errors and only 8 are proofs of equivalence.
