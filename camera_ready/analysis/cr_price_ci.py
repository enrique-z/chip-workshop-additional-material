"""cr_price_ci.py -- design-clustered 95% CIs on the paired per-cell mkc
difference at several LLM-token prices.

Metric (paper's primary definition, scripts/paired_bootstrap_clustered.py):
  mkc_cell = n_mutants_killed / cost_cell, mkc = 0 when cost_cell == 0,
  cost_cell = sim_s + smt_s + tokens/1000 * price.
  Point = mean over the 30 (or 21 comb) paired cell differences.
  CI = design-clustered percentile bootstrap, seeds nested, 10k resamples, seed 42.

Step 0 (sanity): reproduce the published clustered intervals using the
paper's own loader (scalar_cost_used), then show that recomputing cost from
components at $3/1k is identical.

Usage: python3 -I camera_ready/analysis/cr_price_ci.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cr_common import (load_cells, mkc, per_design_diffs, cluster_bootstrap_ci, point,
                       loo, verdict, fmt, COMBINATIONAL)

PRICES = [3.0, 0.30, 0.03, 0.003, 0.0]
COMPS = [("co-evolve", "deterministic-allocator", None, "co-ev - KR-1, full grid (10 designs)"),
         ("co-evolve", "bounded-formal-only", None, "co-ev - formal, full grid (10 designs)"),
         ("co-evolve", "deterministic-allocator", COMBINATIONAL, "co-ev - KR-1, comb slice (7 designs)")]
EXTRA = [("co-evolve", "direct-prompt", None, "co-ev - direct, full grid"),
         ("deterministic-allocator", "bounded-formal-only", None, "KR-1 - formal, full grid"),
         ("co-evolve", "bounded-formal-only", COMBINATIONAL, "co-ev - formal, comb slice"),
         ("deterministic-allocator", "bounded-formal-only", COMBINATIONAL, "KR-1 - formal, comb slice")]

PUBLISHED = {  # main.tex Table tab:cluster / evidence/o02_paired_bootstrap_clustered.json
    "co-ev - KR-1, full grid (10 designs)": (-0.932, -0.087),
    "co-ev - formal, full grid (10 designs)": (-1.025, +0.037),
    "co-ev - KR-1, comb slice (7 designs)": (-0.570, -0.001),
}


def run(cells, order, price, a, b, designs):
    pd = per_design_diffs(cells, order, lambda s: mkc(s, price), a, b, designs)
    lo, hi = cluster_bootstrap_ci(pd)
    return point(pd), lo, hi, pd


def main():
    cells, order = load_cells()
    print(f"cells={len(cells)} designs={len(order)} order={order}")
    print("\n== Step 0: sanity check vs published clustered CIs (paper loader: scalar_cost_used)")
    ok = True
    for a, b, ds, lab in COMPS:
        pt, lo, hi, _ = run(cells, order, None, a, b, ds)
        plo, phi = PUBLISHED[lab]
        match = abs(lo - plo) <= 0.0006 and abs(hi - phi) <= 0.0006
        ok &= match
        print(f"  {lab:40s} point={pt:+.4f} CI=[{lo:+.4f}, {hi:+.4f}]  published [{plo:+.3f}, {phi:+.3f}]  "
              f"{'MATCH' if match else 'MISMATCH'}")
        pt3, lo3, hi3, _ = run(cells, order, 3.0, a, b, ds)
        print(f"  {'':40s} recomputed-from-components @$3/1k: point={pt3:+.4f} CI=[{lo3:+.4f}, {hi3:+.4f}]")
    print(f"  SANITY: {'PASS' if ok else 'FAIL'}  (note -1.0245 is printed as -1.025 in main.tex)")

    print("\n== Step 1: price sweep, amended (as-run) KR-1 verdicts; point [95% design-clustered CI]")
    for a, b, ds, lab in COMPS + EXTRA:
        print(f"  {lab}")
        for p in PRICES:
            pt, lo, hi, pd = run(cells, order, p, a, b, ds)
            print(f"    ${p:<6g}/1k  {fmt(pt, lo, hi)}  {verdict(lo, hi)}")

    print("\n== Step 2: leave-one-design-out point range @$3/1k (sign check)")
    for a, b, ds, lab in COMPS:
        pt, lo, hi, pd = run(cells, order, 3.0, a, b, ds)
        mn, mx, res = loo(pd)
        same = sum(1 for r in res if (r < 0) == (pt < 0))
        print(f"  {lab:40s} LOO range [{mn:+.4f}, {mx:+.4f}] sign kept {same}/{len(res)}")

    print("\n== Step 3: PRE-amendment-5 KR-1 verdicts (9 sequential KR-1 cells swapped in from "
          "evidence/kr1_seq_pre_amendment_5_backup/)")
    pcells, porder = load_cells(pre_amendment=True)
    assert porder == order
    k_am = sum(cells[k]["summary"]["n_mutants_killed"] for k in cells if k[2] == "deterministic-allocator")
    k_pre = sum(pcells[k]["summary"]["n_mutants_killed"] for k in pcells if k[2] == "deterministic-allocator")
    print(f"  KR-1 total kills: amended {k_am}/129, pre-amendment {k_pre}/129")
    for a, b, ds, lab in COMPS:
        for p in [3.0, 0.0]:
            pt, lo, hi, _ = run(pcells, porder, p, a, b, ds)
            print(f"  {lab:40s} ${p:<4g}/1k  {fmt(pt, lo, hi)}  {verdict(lo, hi)}")


if __name__ == "__main__":
    main()
