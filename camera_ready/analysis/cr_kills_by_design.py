"""cr_kills_by_design.py -- per-design kill table, totals, observable slice,
and both mkc aggregation conventions per system at every price.

Conventions:
  mean-of-ratios (MoR): mean over cells of killed/cost.  Two variants:
     MoR-excl : cells with cost == 0 dropped (populate_ledger.py and
                price_sensitivity.py; this is what Table 1 and tab:price print)
     MoR-zero : cost == 0 cells kept with mkc = 0 (paired-bootstrap convention)
  ratio-of-sums (RoS): sum(killed) / sum(cost) over the 30 cells.
  cost = sim_s + smt_s + tokens/1000 * price.

Usage: python3 -I camera_ready/analysis/cr_kills_by_design.py
"""
import sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cr_common import load_cells, cost, SYSTEMS, SHORT, COMBINATIONAL, SEQUENTIAL

PRICES = [3.0, 0.30, 0.03, 0.003, 0.0]


def main():
    cells, order = load_cells()
    print("== Kills by design (summed over 3 seeds); killed/mutant-instances")
    print(f"  {'design':18s} {'class':5s} " + " ".join(f"{SHORT[s]:>8s}" for s in SYSTEMS))
    totals = defaultdict(lambda: [0, 0]); comb = defaultdict(lambda: [0, 0]); seq = defaultdict(lambda: [0, 0])
    for d in sorted(order, key=str.lower):
        row = []
        for sy in SYSTEMS:
            k = sum(cells[(d, s, sy)]["summary"]["n_mutants_killed"] for s in range(3))
            n = sum(cells[(d, s, sy)]["summary"]["n_mutants_total"] for s in range(3))
            row.append(f"{k}/{n}")
            totals[sy][0] += k; totals[sy][1] += n
            tgt = comb if d in COMBINATIONAL else seq
            tgt[sy][0] += k; tgt[sy][1] += n
        print(f"  {d.split('::')[1]:18s} {'comb' if d in COMBINATIONAL else 'seq':5s} " + " ".join(f"{x:>8s}" for x in row))
    for lab, t in [("TOTAL", totals), ("comb (7)", comb), ("seq (3)", seq)]:
        print(f"  {lab:24s} " + " ".join(f"{t[s][0]:>3d}/{t[s][1]:<4d}" for s in SYSTEMS))
    disc = [d for d in order if len({sum(cells[(d, s, sy)]['summary']['n_mutants_killed'] for s in range(3))
                                      for sy in SYSTEMS}) > 1]
    print(f"  designs where systems differ in kills: {len(disc)} -> {[d.split('::')[1] for d in disc]}")
    zero = [d.split('::')[1] for d in order if all(cells[(d, s, 'co-evolve')]['summary']['n_mutants_total'] == 0 for s in range(3))]
    print(f"  designs with zero mutants: {zero}")

    print("\n== Per-seed kills on comparator_3bit / multi_8bit (formal vs KR-1), checks main.tex l.341-344")
    for d in ["rtllm::comparator_3bit", "rtllm::multi_8bit"]:
        for s in range(3):
            f = cells[(d, s, "bounded-formal-only")]; k = cells[(d, s, "deterministic-allocator")]
            fo = [v["outcome"] for v in f["verdicts"] if v["mutation_id"]]
            print(f"  {d.split('::')[1]:15s} seed {s}: formal {f['summary']['n_mutants_killed']}/{f['summary']['n_mutants_total']} "
                  f"(miter outcomes {dict((o, fo.count(o)) for o in set(fo))}), KR-1 "
                  f"{k['summary']['n_mutants_killed']}/{k['summary']['n_mutants_total']}")

    print("\n== Observable slice: mutants killed by the formal miter (any seed)")
    obs = set()
    for (d, s, sy), c in cells.items():
        if sy == "bounded-formal-only":
            obs |= {(d, v["mutation_id"]) for v in c["verdicts"] if v["mutation_id"] and v["outcome"] == "mutation_killed"}
    print(f"  distinct observable mutants: {len(obs)} over designs "
          f"{sorted({d.split('::')[1] for d, _ in obs})}")
    for sy in SYSTEMS:
        per = defaultdict(lambda: [0, 0])
        for (d, s, s2), c in cells.items():
            if s2 != sy:
                continue
            killed = {v["mutation_id"] for v in c["verdicts"] if v["mutation_id"] and v["outcome"] == "mutation_killed"}
            for m in c["mutants"]:
                if (d, m["mutation_id"]) in obs:
                    per[d][1] += 1; per[d][0] += m["mutation_id"] in killed
        tk = sum(v[0] for v in per.values()); tn = sum(v[1] for v in per.values())
        print(f"  {sy:25s} {tk}/{tn} ({tk / tn:.0%})  " + "  ".join(f"{d.split('::')[1]} {v[0]}/{v[1]}" for d, v in sorted(per.items())))

    print("\n== mkc per system under both conventions, each price")
    for p in PRICES:
        print(f"  price ${p:g}/1k")
        res = {}
        for sy in SYSTEMS:
            ss = [cells[k]["summary"] for k in cells if k[2] == sy]
            cs = [cost(s, p) for s in ss]
            ks = [s["n_mutants_killed"] for s in ss]
            excl = [k / c for k, c in zip(ks, cs) if c > 0]
            mor_ex = sum(excl) / len(excl)
            mor_z = sum(k / c if c > 0 else 0 for k, c in zip(ks, cs)) / len(ss)
            ros = sum(ks) / sum(cs)
            res[sy] = (mor_ex, mor_z, ros)
        r_ex = sorted(SYSTEMS, key=lambda s: -res[s][0]); r_ros = sorted(SYSTEMS, key=lambda s: -res[s][2])
        for sy in SYSTEMS:
            m, z, r = res[sy]
            print(f"    {sy:25s} MoR-excl {m:.3f} (#{r_ex.index(sy) + 1})  MoR-zero {z:.3f}  RoS {r:.3f} (#{r_ros.index(sy) + 1})")
        f, k, c, d = (res[s] for s in ["bounded-formal-only", "deterministic-allocator", "co-evolve", "direct-prompt"])
        print(f"    ratios: formal/co-ev MoR {f[0] / c[0]:.3f}x RoS {f[2] / c[2]:.3f}x; "
              f"KR-1/direct MoR {k[0] / d[0]:.2f}x RoS {k[2] / d[2]:.2f}x; KR-1/co-ev MoR {k[0] / c[0]:.2f}x RoS {k[2] / c[2]:.2f}x")

    bound = {}
    print("\n== Timeout-as-kill upper bound, RoS @$3/1k (checks main.tex l.556-560)")
    for sy in SYSTEMS:
        ks = [k for k in cells if k[2] == sy]
        kk = 0; cc = 0; trec = 0; kn = 0
        for key in ks:
            c = cells[key]
            killed = {v["mutation_id"] for v in c["verdicts"] if v["mutation_id"] and v["outcome"] == "mutation_killed"}
            to = {v["mutation_id"] for v in c["verdicts"] if v["mutation_id"] and v["outcome"] == "timeout"}
            kk += len(killed | to); cc += c["summary"]["scalar_cost_used"]
            trec += sum(1 for v in c["verdicts"] if v["mutation_id"] and v["outcome"] == "timeout")
            kn += c["summary"]["n_mutants_killed"]
        bound[sy] = (kk / cc, (kn + trec) / cc, kn / cc)
        print(f"  {sy:25s} distinct-mutant unit: kills+timeout-only mutants {kk}  RoS {kk / cc:.3f}   |   "
              f"paper's mixed unit (kills {kn} + {trec} timeout mutant x TB records = {kn + trec}): RoS {(kn + trec) / cc:.3f}")

    f = bound["bounded-formal-only"][2]; ce = bound["co-evolve"]
    print(f"  miter as-measured RoS / co-evolve RoS with timeouts credited (checks l.463-464 '2.57x'): "
          f"paper's mixed unit {f / ce[1]:.3f}x, distinct-mutant unit {f / ce[0]:.3f}x")


if __name__ == "__main__":
    main()
