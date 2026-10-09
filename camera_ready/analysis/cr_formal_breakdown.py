"""cr_formal_breakdown.py -- outcome breakdown of the bounded formal miter
(bounded-formal-only arm: one verdict per mutant-instance), per design, plus
the engine-level outcome breakdown of mutant x TB records for every arm.

Usage: python3 -I camera_ready/analysis/cr_formal_breakdown.py
"""
import sys
from collections import Counter, defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cr_common import load_cells, SYSTEMS, COMBINATIONAL

OUTS = ["mutation_killed", "mutation_survived", "engine_error", "timeout"]


def main():
    cells, order = load_cells()
    print("== bounded-formal-only: mutant verdict outcomes (129 = one per mutant-instance)")
    tot = Counter(); per = defaultdict(Counter); cells_err = 0
    for (d, s, sy), c in cells.items():
        if sy != "bounded-formal-only":
            continue
        vs = [v for v in c["verdicts"] if v["mutation_id"]]
        assert len(vs) == c["summary"]["n_mutants_total"]
        for v in vs:
            tot[v["outcome"]] += 1; per[d][v["outcome"]] += 1
        cells_err += bool(vs) and all(v["outcome"] == "engine_error" for v in vs)
    print("  total:", {o: tot[o] for o in OUTS}, "sum", sum(tot.values()))
    print(f"  engine_error share of non-kills: {tot['engine_error']}/{sum(tot.values()) - tot['mutation_killed']}")
    print(f"  cells where every miter verdict is engine_error: {cells_err}/30")
    print(f"  {'design':18s} {'class':5s} " + " ".join(f"{o[:9]:>10s}" for o in OUTS))
    for d in sorted(order, key=str.lower):
        print(f"  {d.split('::')[1]:18s} {'comb' if d in COMBINATIONAL else 'seq':5s} " + " ".join(f"{per[d][o]:>10d}" for o in OUTS))
    err_designs = sorted(d.split('::')[1] for d in order if per[d]["engine_error"] and per[d]["engine_error"] == sum(per[d].values()))
    print(f"  designs where the miter engine-errors on every mutant: {err_designs}")

    print("\n== Mutant x TB records by (engine, outcome), all arms")
    for sy in SYSTEMS:
        c2 = Counter()
        for (d, s, s2), c in cells.items():
            if s2 == sy:
                for v in c["verdicts"]:
                    if v["mutation_id"]:
                        c2[(v["engine"], v["outcome"])] += 1
        print(f"  {sy}: " + "; ".join(f"{e}/{o} {n}" for (e, o), n in sorted(c2.items())))


if __name__ == "__main__":
    main()
