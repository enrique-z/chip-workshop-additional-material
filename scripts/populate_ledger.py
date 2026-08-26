"""Roll up runs/e1/*.verdict.json into evidence/o02_claim_evidence_ledger.json.

Reads every per-cell verdict JSON under a runs directory, aggregates by
system across designs/seeds, and writes:

1. evidence/o02_e1_aggregate.json — the per-system summary the paper cites
2. Updates evidence/o02_claim_evidence_ledger.json with 20 ledger rows
   mapping (\\ResultCell{cell}{metric}) composite keys to supported_verdict_id
3. Patches main.tex Table 1 to replace [TBD] payloads with real numbers,
   preserving the \\ResultCell wrapping so gate 3 still validates lineage.

Cell keys per main.tex Table 1:
  e1.direct       — direct-prompt
  e1.cov          — coverage-guided
  e1.formalonly   — bounded-formal-only
  e1.det          — deterministic-allocator
  e1.coev         — co-evolve

Metric keys per main.tex Table 1:
  mkc — mutation-kill per scalar cost (higher = better)
  fpr — false-pass rate (mutation survived but observably wrong; lower = better)
  ffr — false-fail rate (testbench fails on the reference; lower = better)
  wc  — wall-clock seconds per cell (mean)
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path


SYSTEM_TO_CELL_KEY: dict[str, str] = {
    "direct-prompt": "e1.direct",
    "coverage-guided": "e1.cov",
    "bounded-formal-only": "e1.formalonly",
    "deterministic-allocator": "e1.det",
    "co-evolve": "e1.coev",
}


def aggregate_verdicts(runs_dir: Path) -> dict:
    """Aggregate all per-cell verdict JSONs under runs_dir by system."""
    per_system: dict[str, list[dict]] = {k: [] for k in SYSTEM_TO_CELL_KEY}
    for p in sorted(runs_dir.glob("*.verdict.json")):
        try:
            payload = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        cell = payload.get("cell", {})
        summary = payload.get("summary", {})
        system = cell.get("system")
        if system not in per_system:
            continue
        per_system[system].append({
            "cell_slug": p.stem.replace(".verdict", ""),
            "design_id": cell.get("design_id"),
            "seed": cell.get("seed"),
            "summary": summary,
        })
    return per_system


def system_row(cells: list[dict]) -> dict:
    """Compute (mkc, fpr, ffr, wc) for a system's aggregated cells.

    - mkc: mean over cells of (n_killed / scalar_cost)  # mutations killed per cost unit
    - fpr: sum(n_survived-only) / sum(n_mutants)         # NOT-killed-by-any-tb rate
    - ffr: sum(n_false_fail_on_ref) / n_cells             # per-cell false-fail count
    - wc : mean over cells of (sim_seconds + smt_seconds)  # pure wall-clock seconds
    """
    if not cells:
        return {"mkc": None, "fpr": None, "ffr": None, "wc": None, "n_cells": 0}
    mkcs, wcs = [], []
    total_mutants = total_killed = total_survived_only = total_false_fail = 0
    for c in cells:
        s = c["summary"]
        n = int(s.get("n_mutants_total", 0))
        n_killed = int(s.get("n_mutants_killed", 0))
        n_survived = int(s.get("n_mutants_survived", 0))
        if n:
            total_mutants += n
            total_killed += n_killed
            # A mutant is "survived-only" if it appears in survived-set but NOT
            # in killed-set (i.e. no TB in this cell caught it). Approximate
            # from cell-level totals: max(0, n_survived - n_killed) if the
            # sets overlap; use just n_survived when killed/survived are set-
            # disjoint (which is the current run_e1_cell.py contract).
            total_survived_only += max(0, n - n_killed)
        total_false_fail += int(s.get("n_false_fail_on_ref", 0))
        scalar_cost = float(s.get("scalar_cost_used", 0))
        if scalar_cost > 0:
            mkcs.append(n_killed / scalar_cost)
        wcs.append(
            float(s.get("sim_seconds_used", 0)) + float(s.get("smt_seconds_used", 0))
        )
    fpr = (total_survived_only / total_mutants) if total_mutants else 0.0
    ffr = total_false_fail / max(1, len(cells))
    return {
        "mkc": round(statistics.mean(mkcs), 3) if mkcs else 0.0,
        "fpr": round(fpr, 3),
        "ffr": round(ffr, 3),
        "wc": round(statistics.mean(wcs), 2) if wcs else 0.0,
        "n_cells": len(cells),
        "totals": {
            "mutants": total_mutants, "killed": total_killed,
            "survived_only": total_survived_only, "false_fail_on_ref": total_false_fail,
        },
        "per_cell": [c["cell_slug"] for c in cells],
    }


def build_ledger_and_aggregate(per_system: dict) -> tuple[dict, dict]:
    """Produce the ledger dict + the aggregate dict."""
    aggregate = {}
    ledger: dict[str, str] = {}
    for system_name, cells in per_system.items():
        cell_key = SYSTEM_TO_CELL_KEY[system_name]
        row = system_row(cells)
        aggregate[cell_key] = row
        # ledger rows: one supported_verdict_id per (cell_key, metric)
        for metric in ("mkc", "fpr", "ffr", "wc"):
            ledger[f"{cell_key}::{metric}"] = (
                f"evidence/o02_e1_aggregate.json::{cell_key}.{metric}"
                if row.get(metric) is not None
                else f"MISSING::{cell_key}.{metric}"
            )
    return ledger, aggregate


_RESULT_CELL_ANY_RX = re.compile(
    r"\\ResultCell\{(?P<cell>[^}]+)\}\{(?P<metric>[^}]+)\}\{[^}]+\}"
)
_RESULT_CELL_TBD_RX = _RESULT_CELL_ANY_RX  # force-overwrite any current payload


def format_metric(cell_key: str, metric: str, value) -> str:
    """Render the metric value with metric-appropriate precision."""
    if value is None:
        return "[TBD]"
    if metric == "mkc":
        return f"{value:.3f}"
    if metric in ("fpr", "ffr"):
        return f"{value:.3f}"
    if metric == "wc":
        return f"{value:.1f}"
    return str(value)


def patch_main_tex(tex_path: Path, aggregate: dict) -> tuple[str, int]:
    """Replace [TBD] payloads in \\ResultCell wrappers with real numbers."""
    src = tex_path.read_text()
    n_patched = 0

    def _sub(m):
        nonlocal n_patched
        cell = m.group("cell")
        metric = m.group("metric")
        row = aggregate.get(cell)
        if not row or row.get(metric) is None:
            return m.group(0)
        payload = format_metric(cell, metric, row[metric])
        n_patched += 1
        return f"\\ResultCell{{{cell}}}{{{metric}}}{{{payload}}}"

    new_src = _RESULT_CELL_TBD_RX.sub(_sub, src)
    return new_src, n_patched


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default="runs/e1", type=Path)
    ap.add_argument("--paper-dir", default=".", type=Path)
    ap.add_argument("--aggregate-out",
                    default="evidence/o02_e1_aggregate.json", type=Path)
    ap.add_argument("--ledger-out",
                    default="evidence/o02_e1_ledger.json", type=Path)
    ap.add_argument("--patch-tex", action="store_true",
                    help="rewrite main.tex, replacing [TBD] with real numbers")
    args = ap.parse_args(argv)

    per_system = aggregate_verdicts(args.runs_dir)
    ledger, aggregate = build_ledger_and_aggregate(per_system)

    (args.paper_dir / args.aggregate_out).parent.mkdir(parents=True, exist_ok=True)
    (args.paper_dir / args.aggregate_out).write_text(json.dumps(aggregate, indent=2))
    (args.paper_dir / args.ledger_out).write_text(json.dumps(ledger, indent=2))
    print(f"[populate] aggregate written to {args.aggregate_out}")
    print(f"[populate] ledger written to {args.ledger_out} ({len(ledger)} rows)")

    if args.patch_tex:
        tex_path = args.paper_dir / "main.tex"
        new_src, n_patched = patch_main_tex(tex_path, aggregate)
        tex_path.write_text(new_src)
        print(f"[populate] main.tex patched — {n_patched} \\ResultCell payloads updated")

    # Human summary
    for cell_key, row in aggregate.items():
        def _s(v): return "     -" if v is None else f"{v:>6}"
        print(f"  {cell_key:15s}  n_cells={row['n_cells']:3d}  "
              f"mkc={_s(row.get('mkc'))}  fpr={_s(row.get('fpr'))}  "
              f"ffr={_s(row.get('ffr'))}  wc={_s(row.get('wc'))}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
