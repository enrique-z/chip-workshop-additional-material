"""Roll up runs/e2_pilot/*.verdict.json — E2 pilot subset (pre-declared, R5 pass).

Pre-declaration: CHANGELOG_v3.md §A (written before the grid ran) fixes the
grid (12 VerilogEval designs x seed 0 x {direct-prompt, deterministic-allocator,
co-evolve} x agy hosted-model-redacted), the metrics, and the reporting policy.

Reuses populate_ledger.aggregate_verdicts / system_row VERBATIM (imported, not
copied) so the mkc/fpr/ffr/wc formulas are identical to E1 Table 1 by
construction. Adds the E2-pre-registered transfer statistic:

    transfer_score(s) = DR_e2(s) / DR_e1(s),   DR = total killed / total mutants

with DR_e1 read from evidence/o02_e1_aggregate.json totals. Falsifier half 1
(freeze doc secondary_experiment_e2_transfer): transfer_score <= 1 => "no
improvement on unseen family" FIRES for that system. Falsifier half 2
(LLM-swap ordering invariance) is NOT TESTED (single backend) and is stamped
as such in the output.

Writes:
  evidence/o02_e2_pilot_aggregate.json  — per-system rows + transfer scores
  evidence/o02_e2_pilot_ledger.json     — composite key -> aggregate pointer
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from populate_ledger import aggregate_verdicts, system_row  # noqa: E402

PILOT_SYSTEMS = {
    "direct-prompt": "e2p.direct",
    "deterministic-allocator": "e2p.det",
    "co-evolve": "e2p.coev",
}
E1_KEY_FOR = {
    "direct-prompt": "e1.direct",
    "deterministic-allocator": "e1.det",
    "co-evolve": "e1.coev",
}
N_EXPECTED_CELLS = 12  # per system: 12 designs x 1 seed


def main() -> int:
    paper_root = HERE.parent
    runs_dir = paper_root / "runs" / "e2_pilot"
    e1_aggregate = json.loads((paper_root / "evidence" / "o02_e1_aggregate.json").read_text())

    per_system = aggregate_verdicts(runs_dir)

    aggregate: dict = {}
    ledger: dict[str, str] = {}
    incomplete = []
    for system_name, cell_key in PILOT_SYSTEMS.items():
        cells = per_system.get(system_name, [])
        row = system_row(cells)
        if row["n_cells"] != N_EXPECTED_CELLS:
            incomplete.append(f"{system_name}: {row['n_cells']}/{N_EXPECTED_CELLS} cells")
        # Transfer score vs E1 (pre-registered statistic).
        e1_row = e1_aggregate[E1_KEY_FOR[system_name]]
        dr_e1 = e1_row["totals"]["killed"] / e1_row["totals"]["mutants"]
        dr_e2 = (row["totals"]["killed"] / row["totals"]["mutants"]) if row["totals"]["mutants"] else None
        row["detection_rate_e2"] = round(dr_e2, 4) if dr_e2 is not None else None
        row["detection_rate_e1"] = round(dr_e1, 4)
        ts = (dr_e2 / dr_e1) if (dr_e2 is not None and dr_e1 > 0) else None
        row["transfer_score"] = round(ts, 3) if ts is not None else None
        row["falsifier_transfer_le_1"] = (
            "FIRES (transfer_score <= 1)" if ts is not None and ts <= 1.0
            else ("does not fire" if ts is not None else "UNDEFINED")
        )
        aggregate[cell_key] = row
        for metric in ("mkc", "fpr", "ffr", "wc", "transfer_score"):
            ledger[f"{cell_key}::{metric}"] = (
                f"evidence/o02_e2_pilot_aggregate.json::{cell_key}.{metric}"
                if row.get(metric) is not None
                else f"MISSING::{cell_key}.{metric}"
            )

    aggregate["_meta"] = {
        "experiment": "E2 pilot subset (pre-declared; CHANGELOG_v3.md §A)",
        "grid": "12 VerilogEval designs x 1 seed (0) x 3 systems x 1 backend "
                "(agy hosted-model-redacted) = 36 cells",
        "relation_to_full_e2": "SUBSET of the pre-registered 360-cell E2; "
                               "full E2 (5 seeds, 2 backends) remains planned",
        "llm_swap_invariance_falsifier": "NOT TESTED (single backend available)",
        "incomplete_systems": incomplete,
        "_source_runs_dir": "runs/e2_pilot",
    }
    blob = json.dumps(aggregate, sort_keys=True).encode()
    aggregate["_meta"]["_aggregate_sha256_16"] = hashlib.sha256(blob).hexdigest()[:16]

    ev = paper_root / "evidence"
    (ev / "o02_e2_pilot_aggregate.json").write_text(json.dumps(aggregate, indent=1))
    (ev / "o02_e2_pilot_ledger.json").write_text(json.dumps(ledger, indent=1))

    print(json.dumps({k: {m: aggregate[k][m] for m in
          ("mkc", "fpr", "ffr", "wc", "n_cells", "transfer_score",
           "detection_rate_e2", "detection_rate_e1", "falsifier_transfer_le_1")}
          for k in PILOT_SYSTEMS.values()}, indent=1))
    if incomplete:
        print("INCOMPLETE:", incomplete)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
