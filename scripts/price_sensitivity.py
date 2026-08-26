"""Price-sensitivity re-aggregation of E1 mkc under 4 different LLM token prices.

Addresses robustness MAJOR 6 + convergent confound #1 across
the $3.0/1k rate is a frontier hosted-model reference,
not a marginal cost. The paper claims the verdict is "price-conditional"
in §Results; this script quantifies exactly how conditional.

Reads runs/e1/*.verdict.json for per-cell (n_mutants_killed,
sim_seconds_used, smt_seconds_used, llm_tokens_used) and recomputes
mutation-kill per scalar-cost unit at four representative prices:

    $3.00 / 1k  — frontier hosted-model output tier (pinned reference; freeze doc)
    $0.30 / 1k  — hosted Flash / Haiku tier (2026 typical high-volume LLM)
    $0.03 / 1k  — open-weights hosted inference typical
    $0.00 / 1k  — local inference marginal cost (electricity only)

Writes evidence/o02_price_sensitivity.json + a per-system rollup with
ordering flip diagnosis.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


PRICES_USD_PER_1K = [3.00, 0.30, 0.03, 0.00]
SYSTEMS = [
    "direct-prompt",
    "coverage-guided",
    "bounded-formal-only",
    "deterministic-allocator",
    "co-evolve",
]


def load_per_cell(runs_dir: Path) -> list[dict]:
    """Return list of per-cell (system, design_id, seed, killed, sim, smt, tokens)."""
    out: list[dict] = []
    for p in sorted(runs_dir.glob("*.verdict.json")):
        try:
            payload = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        cell = payload.get("cell", {})
        s = payload.get("summary", {})
        out.append({
            "system": cell.get("system"),
            "design_id": cell.get("design_id"),
            "seed": cell.get("seed"),
            "n_killed": int(s.get("n_mutants_killed", 0)),
            "sim_s": float(s.get("sim_seconds_used", 0)),
            "smt_s": float(s.get("smt_seconds_used", 0)),
            "tokens": int(s.get("llm_tokens_used", 0)),
        })
    return out


def mkc_at_price(cells: list[dict], price_per_1k: float) -> dict:
    """Return dict[system] → {mkc_mean_over_cells, absolute_kills, mean_cost}."""
    by_sys: dict[str, list[float]] = defaultdict(list)
    totals: dict[str, dict] = {s: {"killed": 0, "cost": 0.0, "cells": 0} for s in SYSTEMS}
    for c in cells:
        s = c["system"]
        if s not in totals:
            continue
        cost = c["sim_s"] + c["smt_s"] + (c["tokens"] / 1000.0) * price_per_1k
        if cost > 0:
            by_sys[s].append(c["n_killed"] / cost)
        totals[s]["killed"] += c["n_killed"]
        totals[s]["cost"] += cost
        totals[s]["cells"] += 1
    out: dict[str, dict] = {}
    for s in SYSTEMS:
        vals = by_sys[s]
        mkc = statistics.mean(vals) if vals else 0.0
        out[s] = {
            "mkc_mean_over_cells": round(mkc, 4),
            "absolute_kills": totals[s]["killed"],
            "mean_scalar_cost_per_cell": round(
                totals[s]["cost"] / max(1, totals[s]["cells"]), 3
            ),
            "n_cells": totals[s]["cells"],
        }
    return out


def rank_systems(mkcs_at_price: dict) -> list[str]:
    """Return system names sorted best→worst by mkc."""
    return sorted(SYSTEMS, key=lambda s: -mkcs_at_price[s]["mkc_mean_over_cells"])


def main(argv=None) -> int:
    runs_dir = Path("runs/e1")
    if not runs_dir.exists():
        print(f"!!! runs dir not found: {runs_dir}", file=sys.stderr)
        return 1

    cells = load_per_cell(runs_dir)
    print(f"[price-sensitivity] loaded {len(cells)} cells")

    out: dict = {
        "prices_usd_per_1k_tokens": PRICES_USD_PER_1K,
        "systems": SYSTEMS,
        "by_price": {},
        "ordering_by_price": {},
        "note": (
            "Recomputes mutation-kill per scalar-cost unit at 4 token prices. "
            "Scalar cost = sim_seconds + smt_seconds + (llm_tokens / 1000) * price."
        ),
    }
    for price in PRICES_USD_PER_1K:
        mkcs = mkc_at_price(cells, price)
        ordering = rank_systems(mkcs)
        out["by_price"][f"${price:.2f}"] = mkcs
        out["ordering_by_price"][f"${price:.2f}"] = ordering
        print(f"\n[price ${price:.2f}/1k]  best→worst:")
        for rank, s in enumerate(ordering, 1):
            row = mkcs[s]
            print(f"  {rank}. {s:<28} mkc={row['mkc_mean_over_cells']:.3f}  "
                  f"abs_kills={row['absolute_kills']:>3d}  cost/cell={row['mean_scalar_cost_per_cell']:.2f}")

    # Diagnose ordering flips
    order_3 = out["ordering_by_price"]["$3.00"]
    order_0 = out["ordering_by_price"]["$0.00"]
    coev_rank_3 = order_3.index("co-evolve") + 1
    coev_rank_0 = order_0.index("co-evolve") + 1
    formalonly_rank_3 = order_3.index("bounded-formal-only") + 1
    formalonly_rank_0 = order_0.index("bounded-formal-only") + 1
    out["flip_diagnosis"] = {
        "co-evolve_rank_at_$3.00": coev_rank_3,
        "co-evolve_rank_at_$0.00": coev_rank_0,
        "bounded-formal-only_rank_at_$3.00": formalonly_rank_3,
        "bounded-formal-only_rank_at_$0.00": formalonly_rank_0,
        "ordering_flips_between_$3.00_and_$0.00": order_3 != order_0,
    }
    print("\n[flip diagnosis]")
    print(f"  co-evolve rank: #{coev_rank_3} at $3/1k → #{coev_rank_0} at $0/1k")
    print(f"  bounded-formal-only rank: #{formalonly_rank_3} at $3/1k → #{formalonly_rank_0} at $0/1k")
    print(f"  ordering flips: {order_3 != order_0}")

    out_path = Path("evidence/o02_price_sensitivity.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\n[price-sensitivity] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
