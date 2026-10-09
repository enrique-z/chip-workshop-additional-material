"""cr_separate_budgets.py -- report the two cost axes separately instead of
folding seconds and dollars into one scalar.

TIME view  : time = sim_s + smt_s (tokens free). LLM call latency is NOT in
             this time (generator records log wall_seconds = 0); the total
             logged LLM latency is reported separately from
             evidence/e1_agy_call_manifest.jsonl (runs/e1/ in the original).
MONEY view : money = tokens/1000 * $0.003 (i.e. $3 per million tokens; a
             realistic hosted-API order of magnitude, stated as an
             assumption, not a measured bill -- the runs used a flat-rate
             subscription). Non-LLM arms spend $0, so kills per $ is undefined
             for them; the money comparison against them is "kills at $0".

CIs: design-clustered percentile bootstrap, seeds nested, 10k, seed 42.
Usage: python3 -I camera_ready/analysis/cr_separate_budgets.py
"""
import json
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cr_common import (load_cells, mkc, per_design_diffs, cluster_bootstrap_ci,
                       cluster_bootstrap_stat, point, fmt, verdict, SYSTEMS, MANIFEST)

PRICE = 0.003  # $/1k tokens == $3 / 1M tokens


def tsec(s):
    return float(s["sim_seconds_used"]) + float(s["smt_seconds_used"])


def dollars(s):
    return float(s["llm_tokens_used"]) / 1000.0 * PRICE


def kills(s):
    return int(s["n_mutants_killed"])


def ros_items(cells, order, a, b, costf):
    """per design -> list of (kills_a, cost_a, kills_b, cost_b) per seed."""
    out = {}
    for d in order:
        seeds = sorted({k[1] for k in cells if k[0] == d})
        out[d] = [(kills(cells[(d, s, a)]["summary"]), costf(cells[(d, s, a)]["summary"]),
                   kills(cells[(d, s, b)]["summary"]), costf(cells[(d, s, b)]["summary"])) for s in seeds]
    return out


def ros_diff(pool):
    ka = sum(x[0] for x in pool); ca = sum(x[1] for x in pool)
    kb = sum(x[2] for x in pool); cb = sum(x[3] for x in pool)
    ra = ka / ca if ca > 0 else 0.0
    rb = kb / cb if cb > 0 else 0.0
    return ra - rb


def main():
    cells, order = load_cells()

    print("== Raw totals per system (30 cells each)")
    print(f"  {'system':25s} {'kills':>7s} {'sim_s':>8s} {'smt_s':>8s} {'time_s':>8s} {'tokens':>8s} "
          f"{'$@0.003':>8s} {'zero-time cells':>15s}")
    tot = {}
    for sy in SYSTEMS:
        ss = [cells[k]["summary"] for k in cells if k[2] == sy]
        t = dict(k=sum(map(kills, ss)), n=sum(s["n_mutants_total"] for s in ss),
                 sim=sum(float(s["sim_seconds_used"]) for s in ss),
                 smt=sum(float(s["smt_seconds_used"]) for s in ss),
                 tok=sum(int(s["llm_tokens_used"]) for s in ss),
                 z=sum(1 for s in ss if tsec(s) == 0), cells=len(ss))
        t["time"] = t["sim"] + t["smt"]; t["usd"] = t["tok"] / 1000 * PRICE
        tot[sy] = t
        print(f"  {sy:25s} {t['k']:>3d}/{t['n']:<3d} {t['sim']:8.1f} {t['smt']:8.1f} {t['time']:8.1f} "
              f"{t['tok']:8d} {t['usd']:8.4f} {t['z']:>15d}")

    print("\n== TIME view (tokens free): kills per time-second")
    print(f"  {'system':25s} {'ratio-of-sums':>14s} {'mean-of-ratios (0 if t=0)':>26s} {'mean-of-ratios (t>0 cells)':>27s}")
    for sy in SYSTEMS:
        ss = [cells[k]["summary"] for k in cells if k[2] == sy]
        mor0 = sum(mkc(s, 0.0) for s in ss) / len(ss)
        pos = [mkc(s, 0.0) for s in ss if tsec(s) > 0]
        print(f"  {sy:25s} {tot[sy]['k'] / tot[sy]['time']:14.3f} {mor0:26.3f} {sum(pos) / len(pos):27.3f}")
    rank = sorted(SYSTEMS, key=lambda s: -tot[s]["k"] / tot[s]["time"])
    print("  ratio-of-sums rank:", " > ".join(rank))

    pairs = [("co-evolve", "deterministic-allocator"), ("co-evolve", "bounded-formal-only"),
             ("co-evolve", "direct-prompt"), ("deterministic-allocator", "direct-prompt"),
             ("deterministic-allocator", "bounded-formal-only")]
    print("\n  Paired differences, TIME view, point [95% clustered CI]")
    for a, b in pairs:
        pd = per_design_diffs(cells, order, lambda s: mkc(s, 0.0), a, b)
        lo, hi = cluster_bootstrap_ci(pd)
        items = ros_items(cells, order, a, b, tsec)
        rlo, rhi = cluster_bootstrap_stat(items, ros_diff)
        rpt = ros_diff([x for v in items.values() for x in v])
        print(f"    {a} - {b}:")
        print(f"      mean-of-ratios {fmt(point(pd), lo, hi)} {verdict(lo, hi)}")
        print(f"      ratio-of-sums  {fmt(rpt, rlo, rhi)} {verdict(rlo, rhi)}")

    print(f"\n== MONEY view at ${PRICE}/1k tokens (= ${PRICE * 1000:g}/M), time free")
    for sy in SYSTEMS:
        t = tot[sy]
        if t["usd"] > 0:
            print(f"  {sy:25s} spend ${t['usd']:.4f}  kills {t['k']}  kills/$ {t['k'] / t['usd']:.1f}  "
                  f"$/kill {t['usd'] / t['k']:.5f}  tokens/kill {t['tok'] / t['k']:.0f}")
        else:
            print(f"  {sy:25s} spend $0  kills {t['k']}  kills/$ undefined (no LLM spend)")
    print("\n  Paired per-cell differences, MONEY view, point [95% clustered CI]")
    for a, b in [("co-evolve", "deterministic-allocator"), ("co-evolve", "bounded-formal-only"),
                 ("co-evolve", "direct-prompt"), ("co-evolve", "coverage-guided")]:
        pk = per_design_diffs(cells, order, kills, a, b)
        lo, hi = cluster_bootstrap_ci(pk)
        pu = per_design_diffs(cells, order, dollars, a, b)
        ulo, uhi = cluster_bootstrap_ci(pu)
        print(f"    {a} - {b}: kills/cell {fmt(point(pk), lo, hi)} {verdict(lo, hi)}; "
              f"$/cell {fmt(point(pu), ulo, uhi, 5)}")
    for a, b in [("co-evolve", "direct-prompt"), ("co-evolve", "coverage-guided")]:
        items = ros_items(cells, order, a, b, dollars)
        rlo, rhi = cluster_bootstrap_stat(items, ros_diff)
        rpt = ros_diff([x for v in items.values() for x in v])
        print(f"    {a} - {b}: kills per $ (ratio-of-sums) {fmt(rpt, rlo, rhi, 1)} {verdict(rlo, rhi)}")

    print("\n== LLM latency (excluded from every time/cost figure above)")
    gen_wall = sum(v["wall_seconds"] for d in cells.values() for v in d["verdicts"] if v["origin"] == "generator")
    gen_n = sum(1 for d in cells.values() for v in d["verdicts"] if v["origin"] == "generator")
    print(f"  generator records in E1 verdict files: {gen_n}, summed wall_seconds = {gen_wall:.1f}")
    man = [json.loads(l) for l in MANIFEST.read_text().splitlines() if l.strip()]
    wall = sum(m["wall_seconds"] for m in man)
    print(f"  agy_call_manifest.jsonl: {len(man)} calls, total wall {wall:.1f} s "
          f"({wall / len(man):.1f} s/call), utc {man[0]['utc']} .. {man[-1]['utc']}")
    print("  outcomes:", dict(Counter(m["outcome"] for m in man)))
    byday = Counter(); wday = Counter()
    for m in man:
        byday[m["utc"][:10]] += 1; wday[m["utc"][:10]] += m["wall_seconds"]
    for d in sorted(byday):
        print(f"    {d}: {byday[d]} calls, {wday[d]:.1f} s")
    llm_time = sum(tot[s]["time"] for s in ["co-evolve", "direct-prompt", "coverage-guided"])
    print(f"  LLM-arm sim+smt total = {llm_time:.1f} s; manifest latency / that = {wall / llm_time:.1f}x "
          "(manifest has no cell id and spans dates beyond the E1 run; not attributable per system)")


if __name__ == "__main__":
    main()
