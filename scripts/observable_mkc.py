"""Observable-mutation kill rate — filter out behaviorally-equivalent mutants.

Addresses robustness convergent confound #2 across all three
"~71% behaviorally-equivalent mutants make
the primary metric partly measure benchmark artefacts, not system quality."

Approach:
1. For each (design, mutation_id) pair, the `bounded-formal-only` system's
   sby miter emits a definitive verdict: MUTATION_KILLED means the mutant is
   observably distinct from the reference; MUTATION_SURVIVED means the miter
   PROVED equivalence at k-induction depth 2.
2. Build the observability filter: keep only mutation_ids the miter classified
   as MUTATION_KILLED at least once across seeds (i.e., at least one seed's
   run agreed the mutant is distinguishable).
3. Recompute each system's mkc using ONLY those observable mutants in numerator
   AND denominator (per (design, seed) cell: count only killed[obs]/total[obs]).

Writes evidence/o02_observable_mkc.json with:
- observable_mutants_by_design: {design_id: [mutation_ids observed distinguishable]}
- filtered_per_system: {system: {mkc_filtered_mean, killed_obs_total, obs_total,
                                 filter_kept_fraction}}
- ordering_change_diagnosis: raw ordering vs filtered ordering
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


LLM_PRICE_PER_1K_USD = 3.00
SYSTEMS = [
    "direct-prompt",
    "coverage-guided",
    "bounded-formal-only",
    "deterministic-allocator",
    "co-evolve",
]


def build_observability_filter(runs_dir: Path) -> dict[str, set[str]]:
    """Return dict[design_id] → set of mutation_ids observed distinguishable."""
    obs: dict[str, set[str]] = defaultdict(set)
    for p in sorted(runs_dir.glob("*bounded-formal-only.verdict.json")):
        try:
            payload = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        design_id = payload.get("cell", {}).get("design_id")
        if not design_id:
            continue
        for v in payload.get("verdicts", []):
            mid = v.get("mutation_id")
            if not mid:
                continue
            outcome = v.get("outcome")
            if outcome == "mutation_killed":
                obs[design_id].add(mid)
    return dict(obs)


def load_per_cell_verdicts(runs_dir: Path) -> list[dict]:
    """Return list of per-cell dicts with per-mutant verdict lists."""
    out: list[dict] = []
    for p in sorted(runs_dir.glob("*.verdict.json")):
        try:
            payload = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        cell = payload.get("cell", {})
        out.append({
            "system": cell.get("system"),
            "design_id": cell.get("design_id"),
            "seed": cell.get("seed"),
            "verdicts": payload.get("verdicts", []),
            "mutants": payload.get("mutants", []),
            "summary": payload.get("summary", {}),
        })
    return out


def filtered_mkc(cells: list[dict], obs_filter: dict[str, set[str]],
                 price_per_1k: float = LLM_PRICE_PER_1K_USD) -> dict:
    """Per-system rollup restricted to observable mutants."""
    by_sys: dict[str, dict] = {s: {"mkcs": [], "killed_obs": 0, "obs_total": 0,
                                    "raw_total": 0, "cells": 0}
                                for s in SYSTEMS}
    for c in cells:
        s = c["system"]
        if s not in by_sys:
            continue
        obs_set = obs_filter.get(c["design_id"], set())
        cell_mutants = {m["mutation_id"] for m in c["mutants"]}
        obs_in_cell = cell_mutants & obs_set
        by_sys[s]["raw_total"] += len(cell_mutants)
        by_sys[s]["obs_total"] += len(obs_in_cell)
        by_sys[s]["cells"] += 1
        killed_in_cell = {v.get("mutation_id") for v in c["verdicts"]
                          if v.get("outcome") == "mutation_killed"
                          and v.get("mutation_id")}
        killed_obs = killed_in_cell & obs_in_cell
        by_sys[s]["killed_obs"] += len(killed_obs)
        # cell-level filtered mkc
        summary = c["summary"]
        cost = (float(summary.get("sim_seconds_used", 0))
                + float(summary.get("smt_seconds_used", 0))
                + (int(summary.get("llm_tokens_used", 0)) / 1000.0) * price_per_1k)
        if cost > 0:
            by_sys[s]["mkcs"].append(len(killed_obs) / cost)
    out: dict = {}
    for s in SYSTEMS:
        agg = by_sys[s]
        out[s] = {
            "mkc_filtered_mean": round(statistics.mean(agg["mkcs"]), 4) if agg["mkcs"] else 0.0,
            "killed_observable": agg["killed_obs"],
            "observable_total": agg["obs_total"],
            "raw_total": agg["raw_total"],
            "filter_kept_fraction": round(
                agg["obs_total"] / max(1, agg["raw_total"]), 3
            ),
            "observable_kill_rate": round(
                agg["killed_obs"] / max(1, agg["obs_total"]), 3
            ),
            "n_cells": agg["cells"],
        }
    return out


def main() -> int:
    runs_dir = Path("runs/e1")
    obs_filter = build_observability_filter(runs_dir)
    print(f"[observable-mkc] observability filter built from bounded-formal-only miter")
    total_obs = sum(len(v) for v in obs_filter.values())
    print(f"  observable mutants per design:")
    for did, mids in sorted(obs_filter.items()):
        print(f"    {did}: {len(mids)} observable")
    print(f"  total observable mutants across designs: {total_obs}")

    cells = load_per_cell_verdicts(runs_dir)
    filtered = filtered_mkc(cells, obs_filter)

    ordering = sorted(SYSTEMS, key=lambda s: -filtered[s]["mkc_filtered_mean"])
    print("\n[observable-mkc] ordering after equivalent-mutant filter:")
    for rank, s in enumerate(ordering, 1):
        row = filtered[s]
        print(f"  {rank}. {s:<28} filtered_mkc={row['mkc_filtered_mean']:.3f}  "
              f"obs_kill_rate={row['observable_kill_rate']:.3f}  "
              f"killed/obs={row['killed_observable']}/{row['observable_total']}")

    raw_ordering = ["bounded-formal-only", "deterministic-allocator",
                     "direct-prompt", "co-evolve", "coverage-guided"]
    out_path = Path("evidence/o02_observable_mkc.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "note": "Filtered mkc restricted to mutants the bounded-formal-only miter classified as MUTATION_KILLED (behaviorally distinguishable from reference). ",
        "observability_filter_source": "bounded-formal-only miter outcomes at k-induction depth 2",
        "observable_mutants_by_design": {k: sorted(v) for k, v in obs_filter.items()},
        "total_observable_mutants": total_obs,
        "filtered_per_system": filtered,
        "filtered_ordering_best_to_worst": ordering,
        "raw_ordering_best_to_worst": raw_ordering,
        "ordering_holds_under_filter": ordering == raw_ordering,
        "ordering_holds_top_2": ordering[:2] == raw_ordering[:2],
    }, indent=2))
    print(f"\n[observable-mkc] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
