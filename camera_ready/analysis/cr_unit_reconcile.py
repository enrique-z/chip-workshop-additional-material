"""cr_unit_reconcile.py -- reconcile the "verdict" counts used in main.tex.

Units counted per system from runs/e1/*.verdict.json:
  cells             : verdict files (30 / system)
  records           : len(payload["verdicts"])  -- every record of any kind
  mutant x TB       : records with a mutation_id (one per mutant per testbench / miter)
  ref-TB records    : origin=allocator, no mutation_id (TB run on the reference: proved/disproved)
  LLM bookkeeping   : origin=generator records (one per LLM generation call)
  mutant-instances  : sum of n_mutants_total (= len(payload["mutants"]))
  distinct mutants  : distinct (design, mutation_id) across seeds

Then recomputes Table 1 columns (fpr, ffr, wc, mkc) from raw data with the
populate_ledger.py definitions, plus from per-verdict data, and checks them
against main.tex Table 1 and the App B / App D (load) tables.

Usage: python3 -I camera_ready/analysis/cr_unit_reconcile.py
"""
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cr_common import load_cells, SYSTEMS

# Numbers as printed in main.tex (line numbers refer to main.tex at time of writing)
TEX_TABLE1 = {  # l.290-294
    "direct-prompt": dict(mkc=0.129, fpr=0.636, ffr=0.700, wc=12.0),
    "coverage-guided": dict(mkc=0.076, fpr=0.628, ffr=0.700, wc=27.9),
    "bounded-formal-only": dict(mkc=0.595, fpr=0.721, ffr=0.000, wc=7.7),
    "deterministic-allocator": dict(mkc=0.583, fpr=0.558, ffr=0.000, wc=21.4),
    "co-evolve": dict(mkc=0.102, fpr=0.535, ffr=0.267, wc=30.7),
}
TEX_APPB = {  # l.524-528: verdicts, killed, proved-equiv., engine error, timeout
    "bounded-formal-only": (129, 36, 8, 84, 1),
    "deterministic-allocator": (399, 165, 168, 54, 12),
    "direct-prompt": (297, 91, 74, 124, 8),
    "coverage-guided": (667, 160, 128, 364, 15),
    "co-evolve": (698, 184, 172, 324, 18),
}
TEX_APPD = {  # l.675-680: timeouts / denominator
    "bounded-formal-only": (1, 129), "deterministic-allocator": (12, 483),
    "co-evolve": (18, 938), "coverage-guided": (15, 907), "direct-prompt": (8, 417),
}


def main():
    cells, _ = load_cells()
    rows = {}
    print("== Unit counts per system")
    hdr = ("cells", "records", "mutxTB", "refTB", "LLMgen", "mut-inst", "distinct-mut", "killed-inst")
    print(f"  {'system':25s} " + " ".join(f"{h:>12s}" for h in hdr))
    for sy in SYSTEMS:
        ks = [k for k in cells if k[2] == sy]
        rec = mut = ref = gen = inst = 0
        distinct = set(); killed_inst = 0; oc = Counter(); n_surv_sum = 0; incons = 0
        ff_ver = 0
        for k in ks:
            d = cells[k]; s = d["summary"]
            rec += len(d["verdicts"])
            inst += s["n_mutants_total"]
            assert s["n_mutants_total"] == len(d["mutants"]) == d["n_mutants"]
            for m in d["mutants"]:
                distinct.add((k[0], m["mutation_id"]))
            killed_ids = set()
            for v in d["verdicts"]:
                if v["mutation_id"]:
                    mut += 1; oc[v["outcome"]] += 1
                    if v["outcome"] == "mutation_killed":
                        killed_ids.add(v["mutation_id"])
                elif v["origin"] == "generator":
                    gen += 1
                else:
                    ref += 1
                    if v["outcome"] == "disproved":
                        ff_ver += 1
            assert len(killed_ids) == s["n_mutants_killed"], (k, killed_ids, s)
            killed_inst += len(killed_ids)
            n_surv_sum += s["n_mutants_survived"]
            incons += (s["n_mutants_killed"] + s["n_mutants_survived"] != s["n_mutants_total"])
        assert rec == mut + ref + gen
        r = dict(cells=len(ks), records=rec, mut=mut, ref=ref, gen=gen, inst=inst,
                 distinct=len(distinct), killed=killed_inst, oc=oc, surv_sum=n_surv_sum,
                 incons=incons, ff_ver=ff_ver)
        rows[sy] = r
        print(f"  {sy:25s} " + " ".join(f"{x:>12d}" for x in
              (r['cells'], rec, mut, ref, gen, inst, len(distinct), killed_inst)))
    T = {k: sum(r[k] for r in rows.values()) for k in ("records", "mut", "ref", "gen", "inst", "distinct", "killed")}
    print(f"  {'TOTAL':25s} {'':>12s} " + " ".join(f"{T[k]:>12d}" for k in ("records", "mut", "ref", "gen", "inst", "distinct", "killed")))
    print(f"\n  2,874 = all records ({T['records']}); 2,190 = mutant x TB records ({T['mut']}); "
          f"645 = mutant-instances ({T['inst']}, 129/system); distinct mutants/system = "
          f"{rows['co-evolve']['distinct']} (each seeded identically in all 3 seeds? "
          f"{'yes' if rows['co-evolve']['distinct'] * 3 == rows['co-evolve']['inst'] else 'no'})")

    print("\n== Mutant x TB outcome breakdown (App B unit) vs main.tex App B (l.524-528)")
    for sy in SYSTEMS:
        oc = rows[sy]["oc"]
        mine = (rows[sy]["mut"], oc["mutation_killed"], oc["mutation_survived"], oc["engine_error"], oc["timeout"])
        other = {k: v for k, v in oc.items() if k not in ("mutation_killed", "mutation_survived", "engine_error", "timeout")}
        print(f"  {sy:25s} recs/killed/survived/err/timeout = {mine}  tex {TEX_APPB[sy]}  "
              f"{'OK' if mine == TEX_APPB[sy] else 'DIFF'} {other or ''}")
        r = rows[sy]
        print(f"  {'':25s} 1 - killedTB/recs = {1 - oc['mutation_killed'] / r['mut']:.3f} (unit: mutant x TB)  vs  "
              f"1 - killed/instances = {1 - r['killed'] / r['inst']:.3f} (unit: mutant-instance = Table 1 fpr)")

    print("\n== Table 1 recomputed (populate_ledger.py definitions) vs main.tex l.290-294")
    for sy in SYSTEMS:
        ss = [cells[k]["summary"] for k in cells if k[2] == sy]
        fpr = sum(s["n_mutants_total"] - s["n_mutants_killed"] for s in ss) / sum(s["n_mutants_total"] for s in ss)
        ffr = sum(s["n_false_fail_on_ref"] for s in ss) / len(ss)
        wc = statistics.mean(s["sim_seconds_used"] + s["smt_seconds_used"] for s in ss)
        pos = [s["n_mutants_killed"] / s["scalar_cost_used"] for s in ss if s["scalar_cost_used"] > 0]
        mkc_tab = statistics.mean(pos)
        mkc_all = sum(s["n_mutants_killed"] / s["scalar_cost_used"] if s["scalar_cost_used"] > 0 else 0 for s in ss) / len(ss)
        t = TEX_TABLE1[sy]
        mine = dict(mkc=round(mkc_tab, 3), fpr=round(fpr, 3), ffr=round(ffr, 3), wc=round(wc, 1))
        ok = all(abs(mine[k] - t[k]) < 1e-9 for k in t)
        print(f"  {sy:25s} mkc={mine['mkc']:.3f} (over {len(pos)} cost>0 cells; over all 30 w/ mkc=0: {mkc_all:.3f}) "
              f"fpr={mine['fpr']:.3f} ffr={mine['ffr']:.3f} wc={mine['wc']:.1f}  {'MATCH' if ok else 'DIFF vs ' + str(t)}")
        print(f"  {'':25s} ffr cross-check from ref-TB 'disproved' records: {rows[sy]['ff_ver']}/30 = {rows[sy]['ff_ver'] / 30:.3f}")

    print("\n== Timeout rates: App D (l.675-680) denominators vs mutant x TB denominators")
    tt = sum(rows[s]["oc"]["timeout"] for s in SYSTEMS)
    print(f"  all: {tt}/{T['records']} = {tt / T['records']:.1%} (records)  vs  {tt}/{T['mut']} = {tt / T['mut']:.1%} (mutant x TB)")
    for sy in SYSTEMS:
        r = rows[sy]; to = r["oc"]["timeout"]
        texn, texd = TEX_APPD[sy]
        print(f"  {sy:25s} tex {texn}/{texd} = {texn / texd:.1%} [denominator = records: "
              f"{'yes' if texd == r['records'] else 'no'}]  -> mutant x TB {to}/{r['mut']} = {to / r['mut']:.1%}; "
              f"per cell {to / 30:.2f}")

    print("\n== Summary-field consistency (n_killed + n_survived == n_total?)")
    for sy in SYSTEMS:
        r = rows[sy]
        print(f"  {sy:25s} cells inconsistent: {r['incons']}/30; sum n_mutants_survived = {r['surv_sum']} "
              f"vs instances - killed = {r['inst'] - r['killed']}")


if __name__ == "__main__":
    main()
