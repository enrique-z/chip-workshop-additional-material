"""Shared, read-only helpers for the camera-ready re-analysis (cr_*.py).

Everything here mirrors scripts/paired_bootstrap_clustered.py exactly:
  * cells loaded from sorted(runs/e1/*.verdict.json); design order = first
    appearance in that sorted glob (this order feeds the RNG, so it matters);
  * design-clustered percentile bootstrap, seeds nested, 10,000 resamples,
    random.Random(42), alpha 0.05, lo = sorted[int(0.025*n)],
    hi = sorted[int(0.975*n) - 1];
  * point estimate = unweighted mean over all paired cell differences;
  * mkc = killed / cost, and mkc = 0 when cost == 0 (paper's convention).

No file outside camera_ready/analysis/ is written by any cr_* script.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

# Public-artefact layout: the main-experiment verdicts (runs/e1/ in the
# private repository) are split across e1-1/ .. e1-4/; the LLM call manifest
# (runs/e1/agy_call_manifest.jsonl) is evidence/e1_agy_call_manifest.jsonl.
REPO = Path(__file__).resolve().parents[2]
RUN_DIRS = [REPO / f"e1-{i}" for i in range(1, 5)]
MANIFEST = REPO / "evidence" / "e1_agy_call_manifest.jsonl"
PRE_AMEND5 = REPO / "evidence" / "kr1_seq_pre_amendment_5_backup"


def verdict_paths():
    """All main-experiment verdict files, sorted by file name -- identical to
    sorted(runs/e1/*.verdict.json) in the single-directory original."""
    return sorted((p for d in RUN_DIRS for p in d.glob("*.verdict.json")),
                  key=lambda p: p.name)

SYSTEMS = ["bounded-formal-only", "deterministic-allocator", "co-evolve",
           "direct-prompt", "coverage-guided"]
SHORT = {"bounded-formal-only": "formal", "deterministic-allocator": "KR-1",
         "co-evolve": "co-ev", "direct-prompt": "direct", "coverage-guided": "cov"}
# Copied verbatim from scripts/paired_bootstrap_clustered.py
COMBINATIONAL = ["rtllm::adder_8bit", "rtllm::adder_32bit", "rtllm::adder_bcd",
                 "rtllm::comparator_3bit", "rtllm::comparator_4bit",
                 "rtllm::multi_8bit", "rtllm::sub_64bit"]
SEQUENTIAL = ["rtllm::JC_counter", "rtllm::LFSR", "rtllm::accu"]
N_RESAMPLES = 10000
SEED = 42


def load_cells(pre_amendment: bool = False):
    """Return (cells, design_order).

    cells[(design, seed, system)] = full verdict payload (dict).
    pre_amendment=True replaces the KR-1 sequential cells with the preserved
    pre-amendment-5 verdict files (same file names).
    """
    cells, order = {}, []
    for p in verdict_paths():
        src = p
        if pre_amendment and (PRE_AMEND5 / p.name).exists():
            src = PRE_AMEND5 / p.name
        d = json.loads(src.read_text())
        c = d["cell"]
        key = (c["design_id"], int(c["seed"]), c["system"])
        cells[key] = d
        if c["design_id"] not in order:
            order.append(c["design_id"])
    return cells, order


def cost(summary: dict, price_per_1k: float | None) -> float:
    """price None -> the logged scalar_cost_used (paper loader, $3/1k)."""
    if price_per_1k is None:
        return float(summary.get("scalar_cost_used", 0))
    return (float(summary["sim_seconds_used"]) + float(summary["smt_seconds_used"])
            + float(summary["llm_tokens_used"]) / 1000.0 * price_per_1k)


def mkc(summary: dict, price_per_1k: float | None) -> float:
    c = cost(summary, price_per_1k)
    return int(summary.get("n_mutants_killed", 0)) / c if c > 0 else 0.0


def per_design_diffs(cells, order, metric, a, b, designs=None):
    """dict[design] -> [metric(a) - metric(b) per seed], designs in paper order."""
    out = {}
    for d in order:
        if designs is not None and d not in designs:
            continue
        seeds = sorted({k[1] for k in cells if k[0] == d})
        diffs = [metric(cells[(d, s, a)]["summary"]) - metric(cells[(d, s, b)]["summary"])
                 for s in seeds if (d, s, a) in cells and (d, s, b) in cells]
        if diffs:
            out[d] = diffs
    return out


def cluster_bootstrap_ci(per_design_diffs, n_resamples=N_RESAMPLES, alpha=0.05, seed=SEED):
    """Verbatim logic of scripts/paired_bootstrap_clustered.py:cluster_bootstrap_ci."""
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
    return means[int((alpha / 2.0) * n_resamples)], means[int((1.0 - alpha / 2.0) * n_resamples) - 1]


def cluster_bootstrap_stat(per_design_items, stat, n_resamples=N_RESAMPLES, alpha=0.05, seed=SEED):
    """Same resampling scheme, arbitrary statistic over the pooled item list
    (used for ratio-of-sums differences). Items are per-cell tuples."""
    rng = random.Random(seed)
    designs = list(per_design_items.keys())
    n = len(designs)
    vals = []
    for _ in range(n_resamples):
        pool = []
        for _ in range(n):
            pool.extend(per_design_items[designs[rng.randrange(n)]])
        vals.append(stat(pool))
    vals.sort()
    return vals[int((alpha / 2.0) * n_resamples)], vals[int((1.0 - alpha / 2.0) * n_resamples) - 1]


def point(per_design):
    allv = [x for v in per_design.values() for x in v]
    return sum(allv) / len(allv)


def loo(per_design):
    res = []
    for held in per_design:
        pool = [x for d, v in per_design.items() if d != held for x in v]
        res.append(sum(pool) / len(pool))
    return min(res), max(res), res


def verdict(lo, hi):
    return "FIRES (hi<0)" if hi < 0 else ("CLEARS (lo>0)" if lo > 0 else "straddles 0")


def fmt(pt, lo, hi, nd=3):
    return f"{pt:+.{nd}f} [{lo:+.{nd}f}, {hi:+.{nd}f}]"
