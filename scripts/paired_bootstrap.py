"""Paired-bootstrap 95% CI on (co-evolve - deterministic-allocator) per-cell
mutation-kill-per-scalar-cost — the falsifier the freeze doc pre-registers.

Reads runs/e1/*.verdict.json, groups by (design_id, seed), extracts each
system's `n_mutants_killed / scalar_cost_used` (mkc), and computes the
paired difference (co-evolve - deterministic-allocator) across
(design, seed) pairs. Percentile bootstrap, 1000 resamples.

Writes evidence/o02_paired_bootstrap.json with:
- point_estimate_paired_diff (mean over cells)
- ci_lo, ci_hi (95% percentile CI)
- kill_rule_ii_verdict: 'FIRES' if ci_hi < 0, 'CLEARS' if ci_lo > 0, else 'STRADDLES'
- Similar comparison against bounded-formal-only (the second, harder baseline)
"""

from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def load_per_cell_mkc(runs_dir: Path) -> dict:
    """Return dict[(design_id, seed)] → dict[system] → mkc."""
    out: dict = defaultdict(dict)
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
        out[(design_id, int(seed))][system] = mkc
    return out


def paired_diff(pairs: list[tuple[float, float]]) -> list[float]:
    """Return list of (arm_a - arm_b) diffs for paired samples."""
    return [a - b for (a, b) in pairs]


def percentile_bootstrap_ci(
    diffs: list[float], n_resamples: int = 1000, alpha: float = 0.05, seed: int = 42,
) -> tuple[float, float]:
    """Return (ci_lo, ci_hi) percentile CI on the mean of `diffs`."""
    rng = random.Random(seed)
    n = len(diffs)
    if n == 0:
        return (0.0, 0.0)
    resample_means: list[float] = []
    for _ in range(n_resamples):
        resample = [diffs[rng.randrange(n)] for _ in range(n)]
        resample_means.append(sum(resample) / n)
    resample_means.sort()
    lo_idx = int((alpha / 2.0) * n_resamples)
    hi_idx = int((1.0 - alpha / 2.0) * n_resamples) - 1
    return (resample_means[lo_idx], resample_means[hi_idx])


def kill_rule_verdict(lo: float, hi: float) -> str:
    """Interpret the CI wrt the paper's pre-registered falsifier (ii)."""
    if hi < 0:
        return "FIRES — co-evolve strictly dominated (upper CI < 0)"
    if lo > 0:
        return "CLEARS — co-evolve strictly dominant (lower CI > 0)"
    return "STRADDLES — inconclusive at 95% CI"


def main(argv=None) -> int:
    runs_dir = Path("runs/e1")
    if not runs_dir.exists():
        print(f"!!! runs dir not found: {runs_dir}", file=sys.stderr)
        return 1

    cells_by_key = load_per_cell_mkc(runs_dir)
    n_pairs_full = len(cells_by_key)
    print(f"[bootstrap] loaded {n_pairs_full} (design, seed) pairs")

    # We compare co-evolve against BOTH baselines the freeze doc names.
    # KR-1 baseline: deterministic-allocator (the "tuned to be hardest for
    #                co-evolve to beat" per HYPOTHESIS_REFINED §KR-1).
    # Second baseline: bounded-formal-only (came out strongest in the data).
    comparisons = [
        ("co-evolve", "deterministic-allocator"),
        ("co-evolve", "bounded-formal-only"),
        ("deterministic-allocator", "bounded-formal-only"),
    ]

    out: dict = {
        "n_pairs_full_grid": n_pairs_full,
        "resamples": 1000,
        "alpha": 0.05,
        "ci_kind": "percentile",
        "comparisons": [],
    }

    for arm_a, arm_b in comparisons:
        pairs = []
        for k, sys_to_mkc in cells_by_key.items():
            if arm_a in sys_to_mkc and arm_b in sys_to_mkc:
                pairs.append((sys_to_mkc[arm_a], sys_to_mkc[arm_b]))
        diffs = paired_diff(pairs)
        pt = statistics.mean(diffs) if diffs else 0.0
        lo, hi = percentile_bootstrap_ci(diffs)
        verdict = kill_rule_verdict(lo, hi)
        comp = {
            "arm_a": arm_a,
            "arm_b": arm_b,
            "n_pairs": len(pairs),
            "point_estimate_paired_diff": round(pt, 4),
            "ci_lo": round(lo, 4),
            "ci_hi": round(hi, 4),
            "verdict": verdict,
        }
        out["comparisons"].append(comp)
        print(f"[bootstrap] {arm_a} − {arm_b}: "
              f"point={pt:+.4f} 95% CI [{lo:+.4f}, {hi:+.4f}]  → {verdict}")

    out_path = Path("evidence/o02_paired_bootstrap.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"[bootstrap] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
