"""LLM-driven proposer of (test, property, mutation-target) triples.

Backend pinning: per HYPOTHESIS_REFINED.json §freeze_amendment_2 (generator backend)
_generator_backend, the Pass-2 generator invokes the pinned hosted model via `agy` (see modules/agy_backend). No other LLM is called from
this file.

Attribution guarantee (kill-rule anchor): this module MUST NOT import
``modules.anchor_audit``. Enforced at build.sh gate 2.5. Every returned
artefact carries ``origin=ArtefactOrigin.GENERATOR`` so lineage-audit at
Pass 5 can verify no anchor artefact ever transited through here.

Mode dispatch (E1 ablation hook):
- ``direct-prompt``   — LLM sees only the design source + spec; blind proposal
- ``coverage-guided`` — LLM sees prior coverage summary; targets uncovered signals
- ``co-evolve``       — LLM sees prior Verdicts; targets surviving mutants +
                        missed properties

The three non-LLM E1 systems (bounded-formal-only, deterministic-allocator,
plus the pure allocator baseline) bypass this file entirely — they are
dispatched from modules/allocator.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Iterator

from . import agy_backend
from .types import (
    ArtefactOrigin,
    Design,
    MutationTarget,
    Property,
    Testbench,
    Verdict,
    VerdictClass,
)


PROPOSALS_PER_CALL_DEFAULT: int = 4


def propose(
    design: Design,
    iteration: int,
    prior_verdicts: tuple[Verdict, ...],
    config: dict,
) -> tuple[tuple[Property, ...], tuple[Testbench, ...], tuple[MutationTarget, ...]]:
    """Generate next-round (property, testbench, mutation-target) triples.

    Returns tuples of matched length; the allocator pairs by index.
    """
    mode = config.get("mode", "direct-prompt")
    if mode not in ("direct-prompt", "coverage-guided", "co-evolve"):
        raise ValueError(f"generator.propose called with unknown mode: {mode!r}")
    n = int(config.get("proposals_per_call", PROPOSALS_PER_CALL_DEFAULT))
    seed = int(config.get("seed", 0))

    prompt = _build_prompt(design, mode, iteration, prior_verdicts, n=n, seed=seed)
    response = agy_backend.call(prompt)
    parsed = _parse_triples(response.text, design=design, iteration=iteration)

    if not parsed:
        raise RuntimeError(
            f"generator.propose parsed 0 triples from agy response of length "
            f"{len(response.text)} (prompt_sha256={response.prompt_sha256[:12]}); "
            f"mode={mode}, iteration={iteration}"
        )

    props: list[Property] = []
    tbs: list[Testbench] = []
    muts: list[MutationTarget] = []

    for idx, triple in enumerate(parsed):
        stable = _stable_id(design.design_id, iteration, seed, idx)
        props.append(Property(
            property_id=f"p_{stable}",
            design_id=design.design_id,
            origin=ArtefactOrigin.GENERATOR,
            sva_text=triple["property_sva"],
            intended_bound=int(triple.get("intended_bound", 20)),
            parent_iteration=iteration,
        ))
        tbs.append(Testbench(
            testbench_id=f"tb_{stable}",
            design_id=design.design_id,
            origin=ArtefactOrigin.GENERATOR,
            driver_verilog_file=_stage_testbench(
                design, triple["testbench_verilog"], stable,
            ),
            expected_trace_hash=None,
            coverage_targets=tuple(triple.get("coverage_targets", [])),
            parent_iteration=iteration,
        ))
        muts.append(MutationTarget(
            mutation_id=f"m_{stable}",
            design_id=design.design_id,
            origin=ArtefactOrigin.GENERATOR,
            operator=triple.get("mutation_operator", "stuck_at_0"),
            site=triple.get("mutation_site", "wire:0"),
            intended_effect=triple.get(
                "mutation_intended_effect", "output should diverge",
            ),
        ))

    return tuple(props), tuple(tbs), tuple(muts)


def _build_prompt(
    design: Design,
    mode: str,
    iteration: int,
    prior_verdicts: tuple[Verdict, ...],
    *,
    n: int,
    seed: int,
) -> str:
    src = _read(design.verilog_files[0])
    spec = _read_optional(_sibling(design.verilog_files[0], "design_description.txt"))
    ref_tb = _read_optional(_sibling(design.verilog_files[0], "testbench.v"))

    header = (
        f"You are proposing verification artefacts for RTL design "
        f"`{design.top_module}` (corpus={design.corpus}, iteration={iteration}, seed={seed}).\n"
        f"Return a JSON array of EXACTLY {n} objects. Each object MUST have the keys:\n"
        f"  - property_sva            : one SystemVerilog assertion body, e.g. "
        f"                              'assert property (@(posedge clk) req |-> ##[1:3] gnt);'\n"
        f"                              For combinational designs, use an immediate assertion "
        f"                              'assert (cout == a[7] & b[7]);' style.\n"
        f"  - intended_bound          : integer k-induction depth (default 20)\n"
        f"  - testbench_verilog       : a self-contained SystemVerilog module `tb` that instantiates "
        f"                              `{design.top_module}`, drives at least 10 stimulus vectors, "
        f"                              and $displays a pass/fail summary\n"
        f"  - coverage_targets        : list of signal names the testbench aims to exercise\n"
        f"  - mutation_operator       : one of {{stuck_at_0, stuck_at_1, invert_signal, "
        f"                              swap_and_or, flip_gate, constant_flip}}\n"
        f"  - mutation_site           : Verilog file:line OR 'wire:<name>' identifier of the mutation target\n"
        f"  - mutation_intended_effect: one-line human description of the behavioural change\n"
        f"\n"
        f"Return ONLY the JSON array, no fences, no commentary. Do NOT number the objects."
    )

    mode_block = _mode_block(mode, prior_verdicts)

    spec_block = (
        f"\n=== SPEC (design_description.txt) ===\n{spec.strip()}\n"
        if spec else ""
    )
    ref_tb_block = (
        f"\n=== REFERENCE TESTBENCH (testbench.v) ===\n{ref_tb.strip()[:3000]}\n"
        if ref_tb else ""
    )

    return (
        f"{header}\n\n"
        f"=== MODE ===\n{mode_block}\n"
        f"=== DESIGN SOURCE ({design.verilog_files[0]}) ===\n{src}\n"
        f"{spec_block}{ref_tb_block}"
    )


def _mode_block(mode: str, prior_verdicts: tuple[Verdict, ...]) -> str:
    if mode == "direct-prompt":
        return (
            "Direct-prompt: you have ONLY the design source + spec + reference testbench. "
            "Propose diverse artefacts that exercise arithmetic corner cases, control-flow "
            "branches, and reset behaviour without any external feedback."
        )
    if mode == "coverage-guided":
        cov = _summarize_coverage(prior_verdicts)
        return (
            "Coverage-guided: prior-iteration testbenches produced this coverage summary:\n"
            f"{cov}\n"
            "Prioritise properties + testbenches that exercise the UNCOVERED signals + branches."
        )
    if mode == "co-evolve":
        summary = _summarize_verdicts(prior_verdicts)
        return (
            "Co-evolve: prior-iteration Verdicts follow. Surviving mutants + timed-out "
            "properties are the residual curriculum — propose artefacts that would kill "
            "the surviving mutants OR close the timed-out proofs at lower depth.\n"
            f"{summary}"
        )
    raise ValueError(mode)


def _summarize_coverage(prior_verdicts: tuple[Verdict, ...]) -> str:
    if not prior_verdicts:
        return "  (no prior iteration — treat as full-signal coverage gap)"
    lines: list[str] = []
    hit: set[str] = set()
    for v in prior_verdicts:
        if v.outcome in (VerdictClass.PROVED, VerdictClass.BOUNDED_PASS,
                         VerdictClass.MUTATION_KILLED):
            hit.add(v.property_id or v.mutation_id or v.testbench_id or "unknown")
    lines.append(f"  covered artefacts: {sorted(hit)[:12]}")
    lines.append(f"  total prior verdicts: {len(prior_verdicts)}")
    return "\n".join(lines)


def _summarize_verdicts(prior_verdicts: tuple[Verdict, ...]) -> str:
    if not prior_verdicts:
        return "  (no prior verdicts — first iteration)"
    tally: dict[str, int] = {}
    survivors: list[str] = []
    proved: list[str] = []
    for v in prior_verdicts:
        tally[v.outcome.value] = tally.get(v.outcome.value, 0) + 1
        if v.outcome == VerdictClass.MUTATION_SURVIVED and v.mutation_id:
            survivors.append(v.mutation_id)
        elif v.outcome == VerdictClass.PROVED and v.property_id:
            proved.append(v.property_id)
    return (
        f"  outcome tally: {tally}\n"
        f"  surviving mutants (first 8): {survivors[:8]}\n"
        f"  already-proved properties (first 8, do NOT re-propose): {proved[:8]}"
    )


_FENCE_RE = re.compile(r"^```(?:json|text)?\s*\n?", re.IGNORECASE)


def _parse_triples(text: str, *, design: Design, iteration: int) -> list[dict]:
    """Strip fences → json.loads → basic schema check."""
    payload = agy_backend.strip_code_fence(text, expected_lang="json")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        first_bracket = payload.find("[")
        last_bracket = payload.rfind("]")
        if first_bracket == -1 or last_bracket == -1 or last_bracket < first_bracket:
            return []
        try:
            data = json.loads(payload[first_bracket:last_bracket + 1])
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    valid: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if "property_sva" not in item or "testbench_verilog" not in item:
            continue
        valid.append(item)
    return valid


def _stable_id(design_id: str, iteration: int, seed: int, idx: int) -> str:
    key = f"{design_id}::i{iteration}::s{seed}::{idx}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _stage_testbench(design: Design, tb_text: str, stable: str) -> str:
    """Write the generator's testbench to runs/e1/staged_tb/ and return its path."""
    stage_dir = Path(
        os.environ.get(
            "O02_STAGE_DIR",
            str(Path(__file__).resolve().parents[1] / "runs" / "e1" / "staged_tb"),
        )
    )
    stage_dir.mkdir(parents=True, exist_ok=True)
    path = stage_dir / f"tb_{design.top_module}_{stable}.sv"
    path.write_text(agy_backend.strip_code_fence(tb_text, expected_lang="systemverilog"))
    return str(path)


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def _read_optional(path: str | None) -> str:
    if path is None or not os.path.exists(path):
        return ""
    return _read(path)


def _sibling(peer_path: str, name: str) -> str | None:
    d = os.path.dirname(peer_path)
    candidate = os.path.join(d, name)
    return candidate if os.path.exists(candidate) else None


# ─────────────────────────── corpus walker ───────────────────────────


def enumerate_designs(
    corpus_root: str | Path,
    corpus_name: str,
    *,
    exclude_ids: frozenset[str] = frozenset(),
) -> Iterator[Design]:
    """Yield Design records from the corpus tree.

    corpus_name ∈ {'rtllm', 'verilogeval'}. exclude_ids is used by the E1/E2
    grid builder to skip the 6 sequestered anchor designs (per anchor_frozen
    _2026_08_16.e1_e2_designs_MUST_NOT_include).
    """
    root = Path(corpus_root)
    if corpus_name == "rtllm":
        yield from _walk_rtllm(root, exclude_ids)
    elif corpus_name == "verilogeval":
        yield from _walk_verilogeval(root, exclude_ids)
    else:
        raise ValueError(f"unknown corpus: {corpus_name}")


def _walk_rtllm(root: Path, exclude_ids: frozenset[str]) -> Iterator[Design]:
    for verified_v in sorted(root.rglob("verified_*.v")):
        parts = verified_v.parts
        if any(seg.startswith("_") or seg.startswith(".") for seg in parts):
            continue  # skip _chatgpt3.5, _chatgpt4, _pic, .git, __pycache__
        design_name = verified_v.stem.replace("verified_", "")
        design_id = f"rtllm::{design_name}"
        if design_id in exclude_ids or design_name in exclude_ids:
            continue
        design_dir = verified_v.parent
        tb = design_dir / "testbench.v"
        yield Design(
            design_id=design_id,
            corpus="rtllm",
            top_module=design_name,
            verilog_files=(str(verified_v),),
            reference_impl_file=str(verified_v),
            reference_testbench_file=str(tb) if tb.exists() else None,
        )


def _walk_verilogeval(root: Path, exclude_ids: frozenset[str]) -> Iterator[Design]:
    for ref in sorted(root.rglob("Prob*_ref.sv")):
        m = re.match(r"Prob(\d+)_(.+)_ref\.sv$", ref.name)
        if not m:
            continue
        prob_num, prob_name = m.group(1), m.group(2)
        design_id = f"verilogeval::prob{prob_num}_{prob_name}"
        if design_id in exclude_ids:
            continue
        tb = ref.parent / f"Prob{prob_num}_{prob_name}_test.sv"
        yield Design(
            design_id=design_id,
            corpus="verilogeval",
            # E2-pilot fix (freeze_amendment_6): VerilogEval *_ref.sv files
            # declare `module RefModule`, not `top_module`. The walker was
            # never exercised before the E2 pilot; "top_module" broke port
            # inference, mutation renaming, and miter construction.
            top_module="RefModule",
            verilog_files=(str(ref),),
            reference_impl_file=str(ref),
            reference_testbench_file=str(tb) if tb.exists() else None,
        )


__all__ = ["propose", "enumerate_designs", "PROPOSALS_PER_CALL_DEFAULT"]
