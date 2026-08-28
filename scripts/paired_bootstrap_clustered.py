"""Design-clustered paired bootstrap on the E1 grid.

Motivated by GPT round-2 adjudication F-04: "The primary paired bootstrap
resamples 30 design-seed cells, although the 30 observations arise from
only 10 designs with three repeated seeds. Resampling cells as
independent can produce intervals that are too narrow."

Two robustness recomputations of the same paired difference reported in
`evidence/o02_paired_bootstrap.json`, using ONLY the per-cell verdicts
already on disk (`runs/e1/*.verdict.json`).  No new experiment.

1. Cluster bootstrap on design (n_design = 10).  Each resample draws 10
   designs with replacement; the three seeds of every drawn design are
   kept together (seed-nested cluster).  10,000 resamples.  Percentile
   95% CI on the mean per-cell paired difference.

2. Leave-one-design-out.  For each of the 10 designs, drop the three
   cells belonging to that design and recompute the point estimate of
   the mean paired difference.  Report min/max/mean and the count of
   leave-one-out slices whose sign matches the full-grid sign
   (`fire_count` out of 10).

3. The same two recomputations restricted to the COMBINATIONAL-ONLY
   slice (7 designs x 3 seeds = 21 cells), which the paper calls the
   decisive baseline-independence test.  The published slice CI in
   `evidence/o02_combinational_sensitivity_ci.json` resamples the 21
   cells as independent and is subject to the identical critique, so
   the slice is re-run here through the identical clustering.  The
   unclustered slice CI is recomputed alongside it (same 1,000
   resamples, same RNG seed as the original) so the two are directly
   comparable and the published number is independently reproduced.

Writes `evidence/o02_paired_bootstrap_clustered.json`.
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def load_per_cell_mkc(runs_dir: Path):
    """Return dict[design_id] -> list[dict[system] -> mkc] (one entry per seed)."""
    by_design: dict = defaultdict(dict)
    for p in sorted(runs_dir.glob("*.verdict.json")):
        try:
            payload = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        cell = payload.get("cell", {})
        summary = payload.get("summary", {})
        design_id = cell.get("design_id")
        seed = cell.get("seed")
        system = cell.get("system")
        n_killed = int(summary.get("n_mutants_killed", 0))
        cost = float(summary.get("scalar_cost_used", 0))
        if not design_id or system is None or seed is None:
            continue
        mkc = n_killed / cost if cost > 0 else 0.0
        seed = int(seed)
        by_design.setdefault(design_id, {}).setdefault(seed, {})[system] = mkc
    # Flatten to design_id -> list of dict[system]->mkc (one per seed, sorted)
    flat: dict = {}
    for design_id, seeds in by_design.items():
        flat[design_id] = [seeds[s] for s in sorted(seeds.keys())]
    return flat


def per_cell_diffs(clusters, arm_a, arm_b):
    """Return dict[design_id] -> list of paired diffs (one per seed cell)."""
    out = {}
    for design_id, seed_cells in clusters.items():
        diffs = []
        for cell in seed_cells:
            if arm_a in cell and arm_b in cell:
                diffs.append(cell[arm_a] - cell[arm_b])
        if diffs:
            out[design_id] = diffs
    return out


def cluster_bootstrap_ci(per_design_diffs, n_resamples=10000, alpha=0.05, seed=42):
    """Percentile bootstrap resampling designs with replacement (seeds nested).

    Point estimate is the mean over all cell-level paired diffs (unweighted
    per cell, matching the original cell-level bootstrap's estimand).
    """
    rng = random.Random(seed)
    designs = list(per_design_diffs.keys())
    n_design = len(designs)
    if n_design == 0:
        return (0.0, 0.0)
    means = []
    for _ in range(n_resamples):
        pool = []
        for _ in range(n_design):
            d = designs[rng.randrange(n_design)]
            pool.extend(per_design_diffs[d])
        means.append(sum(pool) / len(pool))
    means.sort()
    lo = means[int((alpha / 2.0) * n_resamples)]
    hi = means[int((1.0 - alpha / 2.0) * n_resamples) - 1]
    return lo, hi


def cell_bootstrap_ci(diffs, n_resamples=1000, alpha=0.05, seed=42):
    """Unclustered percentile CI, resampling CELLS as independent.

    Byte-identical procedure to `scripts/paired_bootstrap.py`
    (`percentile_bootstrap_ci`), reproduced here so the clustered and
    unclustered intervals for the same slice come out of one run.
    """
    rng = random.Random(seed)
    n = len(diffs)
    if n == 0:
        return (0.0, 0.0)
    means = []
    for _ in range(n_resamples):
        means.append(sum(diffs[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((alpha / 2.0) * n_resamples)]
    hi = means[int((1.0 - alpha / 2.0) * n_resamples) - 1]
    return lo, hi


def leave_one_design_out(per_design_diffs):
    """For each design, drop it and return (design_id, mean_paired_diff)."""
    designs = list(per_design_diffs.keys())
    results = []
    for held in designs:
        pool = []
        for d, diffs in per_design_diffs.items():
            if d == held:
                continue
            pool.extend(diffs)
        pt = sum(pool) / len(pool) if pool else 0.0
        results.append((held, pt))
    return results


def verdict_label(lo, hi):
    if hi < 0:
        return "FIRES — co-evolve strictly dominated (upper CI < 0)"
    if lo > 0:
        return "CLEARS — co-evolve strictly dominant (lower CI > 0)"
    return "STRADDLES — inconclusive at 95% CI"


def main() -> int:
    runs_dir = Path("runs/e1")
    if not runs_dir.exists():
        print(f"!!! runs dir not found: {runs_dir}", file=sys.stderr)
        return 1

    clusters = load_per_cell_mkc(runs_dir)
    n_design = len(clusters)
    total_cells = sum(len(v) for v in clusters.values())
    print(f"[cluster-boot] n_design={n_design} total_cells={total_cells}")

    comparisons = [
        ("co-evolve", "deterministic-allocator"),
        ("co-evolve", "bounded-formal-only"),
        ("deterministic-allocator", "bounded-formal-only"),
    ]

    out = {
        "n_design": n_design,
        "n_cells_total": total_cells,
        "resamples": 10000,
        "alpha": 0.05,
        "ci_kind": "percentile (design-clustered, seeds nested)",
        "resample_unit": "design",
        "comparisons": [],
    }

    for arm_a, arm_b in comparisons:
        per_design = per_cell_diffs(clusters, arm_a, arm_b)
        all_diffs = [d for v in per_design.values() for d in v]
        if not all_diffs:
            continue
        pt = statistics.mean(all_diffs)
        lo, hi = cluster_bootstrap_ci(per_design)
        loo = leave_one_design_out(per_design)
        fire_count = sum(1 for _, v in loo if v < 0)  # sign preserved (negative point)
        # For dets vs bfo the full-grid point estimate is positive; count k such that v > 0.
        if pt > 0:
            fire_count = sum(1 for _, v in loo if v > 0)
        comp = {
            "arm_a": arm_a,
            "arm_b": arm_b,
            "n_cells_pooled": len(all_diffs),
            "point_estimate_paired_diff": round(pt, 4),
            "ci_lo": round(lo, 4),
            "ci_hi": round(hi, 4),
            "verdict": verdict_label(lo, hi),
            "loo_min_point": round(min(v for _, v in loo), 4),
            "loo_max_point": round(max(v for _, v in loo), 4),
            "loo_mean_point": round(statistics.mean(v for _, v in loo), 4),
            "loo_sign_preserved_count": fire_count,
            "loo_n_designs": len(loo),
        }
        out["comparisons"].append(comp)
        print(f"[cluster-boot] {arm_a} - {arm_b}: point={pt:+.4f} "
              f"95% cluster-CI [{lo:+.4f}, {hi:+.4f}]  LOO sign preserved "
              f"{fire_count}/{len(loo)}")

    # ---- Combinational-only slice, clustered the identical way --------
    # The 7 designs on which KR-1 (deterministic-allocator) is
    # algorithmically independent of the formal miter; the 3 sequential
    # designs are excluded because amendment 5 routes KR-1 there to the
    # same bounded miter as bounded-formal-only.  Design list copied
    # verbatim from evidence/o02_combinational_sensitivity_ci.json.
    combinational = [
        "rtllm::adder_8bit",
        "rtllm::adder_32bit",
        "rtllm::adder_bcd",
        "rtllm::comparator_3bit",
        "rtllm::comparator_4bit",
        "rtllm::multi_8bit",
        "rtllm::sub_64bit",
    ]
    comb_clusters = {k: v for k, v in clusters.items() if k in combinational}
    slice_out = {
        "slice": "combinational-only",
        "combinational_designs": combinational,
        "n_design": len(comb_clusters),
        "n_cells_total": sum(len(v) for v in comb_clusters.values()),
        "comparisons": [],
    }
    for arm_a, arm_b in comparisons:
        per_design = per_cell_diffs(comb_clusters, arm_a, arm_b)
        all_diffs = [d for v in per_design.values() for d in v]
        if not all_diffs:
            continue
        pt = statistics.mean(all_diffs)
        c_lo, c_hi = cluster_bootstrap_ci(per_design)
        u_lo, u_hi = cell_bootstrap_ci(all_diffs)
        loo = leave_one_design_out(per_design)
        fire_count = sum(1 for _, v in loo if (v > 0 if pt > 0 else v < 0))
        slice_out["comparisons"].append({
            "arm_a": arm_a,
            "arm_b": arm_b,
            "n_cells_pooled": len(all_diffs),
            "point_estimate_paired_diff": round(pt, 4),
            "clustered_ci_lo": round(c_lo, 4),
            "clustered_ci_hi": round(c_hi, 4),
            "clustered_verdict": verdict_label(c_lo, c_hi),
            "unclustered_ci_lo": round(u_lo, 4),
            "unclustered_ci_hi": round(u_hi, 4),
            "unclustered_verdict": verdict_label(u_lo, u_hi),
            "unclustered_resamples": 1000,
            "loo_min_point": round(min(v for _, v in loo), 4),
            "loo_max_point": round(max(v for _, v in loo), 4),
            "loo_sign_preserved_count": fire_count,
            "loo_n_designs": len(loo),
        })
        print(f"[cluster-boot][comb] {arm_a} - {arm_b}: point={pt:+.4f} "
              f"clustered [{c_lo:+.4f}, {c_hi:+.4f}] | "
              f"unclustered [{u_lo:+.4f}, {u_hi:+.4f}]  LOO sign "
              f"{fire_count}/{len(loo)}")
    out["combinational_slice"] = slice_out

    out_path = Path("evidence/o02_paired_bootstrap_clustered.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"[cluster-boot] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
