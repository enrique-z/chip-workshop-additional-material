"""Per-cell E1 runner — reads ONE JSONL line, executes the (design x seed x system) cell.

Writes:
- runs/<experiment>/<design>_<seed>_<system>.log         (raw stdout/stderr traces)
- runs/<experiment>/<design>_<seed>_<system>.verdict.json (typed Verdict batch + summary)

Invoked in parallel (see PASS_2_EXECUTION_PLAYBOOK.md):
    parallel -j 8 --joblog runs/e1/parallel.joblog \\
        'python3 scripts/run_e1_cell.py {}' \\
        :::: experiments/e1_grid.jsonl

Each JSONL line is a dict:
    {"design_id": "rtllm::adder_8bit", "seed": 0, "system": "co-evolve",
     "cost_budget": {"sim_seconds_remaining": 300, "smt_seconds_remaining": 300,
                     "llm_tokens_remaining": 20000}}

Cell must complete in <90 min or write a TIMEOUT verdict + exit 0. NEVER raise
past the CLI boundary — a raise = missing log file = anti-fab guard fails Pass 3.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import allocator, generator, mutation  # noqa: E402
from modules.types import (  # noqa: E402
    ArtefactOrigin,
    BudgetLedger,
    Design,
    MutationTarget,
    Verdict,
    VerdictClass,
)


CELL_WALL_CLOCK_HARD_TIMEOUT_SECONDS = 90 * 60
DEFAULT_MUTANTS_PER_DESIGN = 5


# Cache walker output across process invocations by design_id
_DESIGN_CACHE: dict[str, Design] | None = None


def load_cell_config(json_line: str) -> dict:
    return json.loads(json_line)


def resolve_design(design_id: str, corpora_root: Path) -> Design:
    global _DESIGN_CACHE
    if _DESIGN_CACHE is None:
        _DESIGN_CACHE = {}
        for corpus in ("rtllm", "verilogeval"):
            sub = corpora_root / ("RTLLM" if corpus == "rtllm" else "verilog-eval")
            if not sub.exists():
                continue
            for d in generator.enumerate_designs(sub, corpus):
                _DESIGN_CACHE[d.design_id] = d
    d = _DESIGN_CACHE.get(design_id)
    if d is None:
        raise ValueError(f"design {design_id!r} not found in corpora")
    return d


def summarize_verdicts(
    verdicts: tuple[Verdict, ...], mutants: tuple[MutationTarget, ...],
) -> dict:
    """Produce the ledger-populating rollup for Table 1 cells."""
    mut_ids_all = {m.mutation_id for m in mutants}
    killed = {v.mutation_id for v in verdicts if v.outcome == VerdictClass.MUTATION_KILLED and v.mutation_id}
    survived = {v.mutation_id for v in verdicts if v.outcome == VerdictClass.MUTATION_SURVIVED and v.mutation_id}
    false_fail_refs = [v for v in verdicts if v.mutation_id is None and v.outcome == VerdictClass.DISPROVED]
    engine_errors = [v for v in verdicts if v.outcome == VerdictClass.ENGINE_ERROR]
    timeouts = [v for v in verdicts if v.outcome == VerdictClass.TIMEOUT]

    sim_seconds = sum(v.sim_seconds for v in verdicts)
    smt_seconds = sum(v.smt_seconds for v in verdicts)
    llm_tokens = sum(v.llm_tokens for v in verdicts)
    scalar_cost = sim_seconds + smt_seconds + (llm_tokens / 1000.0) * allocator.LLM_PRICE_PER_1K_TOKENS_USD

    return {
        "n_mutants_total": len(mut_ids_all),
        "n_mutants_killed": len(killed & mut_ids_all),
        "n_mutants_survived": len(survived & mut_ids_all),
        "kill_rate": (len(killed & mut_ids_all) / len(mut_ids_all)) if mut_ids_all else 0.0,
        "n_false_fail_on_ref": len(false_fail_refs),
        "n_engine_errors": len(engine_errors),
        "n_timeouts": len(timeouts),
        "sim_seconds_used": sim_seconds,
        "smt_seconds_used": smt_seconds,
        "llm_tokens_used": llm_tokens,
        "scalar_cost_used": scalar_cost,
        "mutation_kill_per_scalar_cost": (
            (len(killed & mut_ids_all) / scalar_cost) if scalar_cost > 0 else 0.0
        ),
    }


def write_outputs(
    cell: dict, verdicts: tuple[Verdict, ...], mutants: tuple[MutationTarget, ...],
    log_lines: list[str], runs_dir: Path, summary: dict,
) -> None:
    slug = f"{cell['design_id'].replace('::', '_')}_{cell['seed']}_{cell['system']}"
    log_path = runs_dir / f"{slug}.log"
    verdict_path = runs_dir / f"{slug}.verdict.json"
    log_path.write_text("\n".join(log_lines))

    def _verdict_dict(v: Verdict) -> dict:
        return {
            "verdict_id": v.verdict_id,
            "testbench_id": v.testbench_id,
            "property_id": v.property_id,
            "mutation_id": v.mutation_id,
            "engine": v.engine,
            "outcome": v.outcome.value,
            "origin": v.origin.value,
            "wall_seconds": v.wall_seconds,
            "smt_seconds": v.smt_seconds,
            "sim_seconds": v.sim_seconds,
            "llm_tokens": v.llm_tokens,
            "counterexample_trace": v.counterexample_trace,
        }

    payload = {
        "cell": cell,
        "summary": summary,
        "n_verdicts": len(verdicts),
        "verdicts": [_verdict_dict(v) for v in verdicts],
        "n_mutants": len(mutants),
        "mutants": [
            {
                "mutation_id": m.mutation_id, "operator": m.operator,
                "site": m.site, "intended_effect": m.intended_effect,
                "origin": m.origin.value,
            } for m in mutants
        ],
    }
    verdict_path.write_text(json.dumps(payload, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cell_json", help="JSONL line describing one cell")
    parser.add_argument("--runs-dir", default="runs/e1", help="output directory")
    parser.add_argument("--corpora-root", default="corpora", help="corpora dir")
    parser.add_argument("--mutants-per-design", type=int,
                        default=DEFAULT_MUTANTS_PER_DESIGN)
    args = parser.parse_args(argv)

    cell = load_cell_config(args.cell_json)
    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = [f"[cell] {json.dumps(cell)}"]

    verdicts: tuple[Verdict, ...] = ()
    mutants: tuple[MutationTarget, ...] = ()
    summary: dict = {"n_mutants_total": 0, "n_mutants_killed": 0}
    t0 = time.time()

    try:
        design = resolve_design(cell["design_id"], Path(args.corpora_root))
        log_lines.append(f"[resolved] {design.reference_impl_file}")

        # Deterministic mutant seed pool — same K mutants across all systems for
        # comparability. Reads from a stable per-design cache directory.
        mutants = mutation.seed_mutants(design, max_per_operator=2)[: args.mutants_per_design]
        log_lines.append(f"[mutants] seeded {len(mutants)} mutants: "
                         + ", ".join(m.mutation_id for m in mutants))

        ledger = BudgetLedger(**cell["cost_budget"])
        log_lines.append(f"[ledger:start] {ledger}")

        verdicts, ledger = allocator.dispatch_system(
            cell["system"], design, cell["seed"], ledger,
            mutants=mutants,
            generator_fn=generator.propose if cell["system"] in (
                "direct-prompt", "coverage-guided", "co-evolve") else None,
            proposals_per_call=int(cell.get("proposals_per_call", 3)),
            co_evolve_iters=int(cell.get("co_evolve_iters", 2)),
        )
        log_lines.append(f"[ledger:end] {ledger}")
        log_lines.append(f"[wall] {time.time() - t0:.2f}s")
    except Exception as e:
        log_lines.append("[error] " + "".join(traceback.format_exception(e)))

    summary = summarize_verdicts(verdicts, mutants)
    log_lines.append(f"[summary] {json.dumps(summary)}")

    write_outputs(cell, verdicts, mutants, log_lines, runs_dir, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
