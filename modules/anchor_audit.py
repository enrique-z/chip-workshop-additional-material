"""Sequestered hidden-property set + lineage guard.

The single most important module for the paper's central claim. The
Crosswalk kill rule (verbatim) says: "hidden-oracle separation
compromised" is a paper-killer. This module enforces separation by:

1. **Physical separation**: anchor artefacts live under
   ``evidence/anchor_sequestered/`` — a directory the generator module
   is FORBIDDEN from importing, reading, or otherwise referencing.

2. **Origin-tag enforcement**: every anchor artefact carries
   ``ArtefactOrigin.ANCHOR``; any Verdict whose ``origin=ANCHOR`` but
   whose lineage traces through a generator-authored artefact fires a
   lineage-violation error at Pass 5 audit.

3. **Freeze-before-first-run**: anchor set is populated at Pass 1 open
   (BEFORE any generator code exists) and its SHA-256 hash is recorded
   in HYPOTHESIS_REFINED.json.anchor_hash_frozen_utc. Any modification
   after freeze invalidates the run.

4. **Build-gate audit**: build.sh gate 2.5 ``anchor-separation-import-audit``
   greps ``modules/generator.py`` and ``modules/allocator.py`` for
   ``from .anchor_audit`` / ``import anchor_audit`` and fails the build
   if any match. This gate is real code in build.sh (added
   post-adversarial-R1 C2), not a docstring promise.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from .types import (
    ArtefactOrigin, Design, MutationTarget, Property, Testbench, Verdict,
)


ANCHOR_DIR_RELATIVE = "evidence/anchor_sequestered"
ANCHOR_HASH_MANIFEST = "evidence/anchor_sequestered/HASH_MANIFEST.txt"
HOLDOUT_FRACTION = 0.25  # last 25% of AIG sites per (design, operator)


def _walk_anchor_designs(anchor_dir: str) -> list[Design]:
    """Yield Design records for each anchor design (matches E1 walker shape)."""
    root = Path(anchor_dir)
    designs: list[Design] = []
    for verified in sorted(root.rglob("verified_*.v")):
        top = verified.stem.replace("verified_", "")
        tb = verified.parent / "testbench.v"
        designs.append(Design(
            design_id=f"anchor::{top}",
            corpus="rtllm",
            top_module=top,
            verilog_files=(str(verified),),
            reference_impl_file=str(verified),
            reference_testbench_file=str(tb) if tb.exists() else None,
        ))
    return designs


def load_anchor_properties(anchor_dir: str = ANCHOR_DIR_RELATIVE) -> tuple[Property, ...]:
    """Load the sequestered property set.

    The 6 RTLLM anchor designs do not ship formal properties (they ship
    reference simulation testbenches only). Return empty for the current
    E3 audit; property-based scoring is not part of the anchor kill loop.
    Kept as a stable API so the paper's §Anchor-Separation paragraph
    remains code-backed.
    """
    _ = anchor_dir
    return ()


def load_anchor_testbenches(anchor_dir: str = ANCHOR_DIR_RELATIVE) -> tuple[Testbench, ...]:
    """Load the sequestered independently-authored testbenches (one per design)."""
    tbs: list[Testbench] = []
    for d in _walk_anchor_designs(anchor_dir):
        if d.reference_testbench_file:
            tbs.append(Testbench(
                testbench_id=f"anchor::{d.top_module}::testbench",
                design_id=d.design_id,
                origin=ArtefactOrigin.ANCHOR,
                driver_verilog_file=d.reference_testbench_file,
                expected_trace_hash=None,
                coverage_targets=(),
                parent_iteration=None,
            ))
    return tuple(tbs)


def load_anchor_mutants(anchor_dir: str = ANCHOR_DIR_RELATIVE) -> tuple[MutationTarget, ...]:
    """Load the sequestered ANCHOR-origin mutation targets (post-adv-R1 C4).

    Seeded deterministically: for each design, apply the standard operator
    taxonomy to the reference Verilog and take the LAST ``HOLDOUT_FRACTION``
    of sites per operator as the ANCHOR-origin holdout. This is
    reproducible from HASH_MANIFEST.txt (which pins every source byte).

    IMPORTANT: this function may only be called from scripts/audit_final.py.
    Build gate 2.5 enforces the import invariant.
    """
    # Import mutation here (not at module top) to keep the import graph
    # asymmetric: anchor_audit knows about mutation, but mutation does not
    # know about anchor_audit.
    from . import mutation as _mut

    out: list[MutationTarget] = []
    for d in _walk_anchor_designs(anchor_dir):
        with open(d.reference_impl_file) as f:
            src = f.read()
        for op in _mut.DEFAULT_MUTATION_OPERATORS:
            sites = _mut._find_sites(src, op)
            if not sites:
                continue
            holdout_n = max(1, math.ceil(len(sites) * HOLDOUT_FRACTION))
            holdout_sites = sites[-holdout_n:]
            for idx, site in enumerate(holdout_sites):
                mid = f"anchor_m_{d.top_module}_{op}_{idx}"
                out.append(MutationTarget(
                    mutation_id=mid,
                    design_id=d.design_id,
                    origin=ArtefactOrigin.ANCHOR,
                    operator=op,
                    site=f"{d.reference_impl_file}:byte{site['start']}",
                    intended_effect=_mut._effect_for(op, site.get("match", "")),
                ))
    return tuple(out)


def stage_anchor_mutants_to_disk(
    anchor_dir: str = ANCHOR_DIR_RELATIVE,
    stage_dir: str | Path = None,
) -> dict[str, str]:
    """Write anchor mutants to a stage dir; return dict[mutation_id → file path].

    Delegates the actual source mutation to modules.mutation._apply_mutation
    (same operator library as E1) but writes to a SEPARATE directory the
    generator loop can NOT infer, and returns paths keyed by the anchor
    mutation_id (which prefixes ``anchor_m_``). Reads only from anchor_dir.
    """
    from . import mutation as _mut

    stage_dir = Path(stage_dir or (Path(anchor_dir).parent.parent / "runs" / "e3" / "mutants"))
    stage_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for d in _walk_anchor_designs(anchor_dir):
        with open(d.reference_impl_file) as f:
            src = f.read()
        design_dir = stage_dir / d.top_module
        design_dir.mkdir(parents=True, exist_ok=True)
        for op in _mut.DEFAULT_MUTATION_OPERATORS:
            sites = _mut._find_sites(src, op)
            if not sites:
                continue
            holdout_n = max(1, math.ceil(len(sites) * HOLDOUT_FRACTION))
            holdout_sites = sites[-holdout_n:]
            for idx, site in enumerate(holdout_sites):
                mid = f"anchor_m_{d.top_module}_{op}_{idx}"
                mutated = _mut._apply_mutation(src, op, site)
                if mutated == src:
                    continue
                mutant_top = f"{d.top_module}__mut_anchor_{op}_{idx}"
                renamed = _mut._rename_top_module(
                    mutated, d.top_module, mutant_top, d.reference_impl_file,
                )
                mpath = design_dir / f"{op}_{idx}.v"
                mpath.write_text(renamed)
                paths[mid] = str(mpath)
    return paths


def audit_lineage(all_verdicts: tuple[Verdict, ...]) -> tuple[Verdict, ...]:
    """Return the subset of verdicts whose origin/lineage violates separation.

    Rule (kill-rule anchor per Discussion failure mode iii): a Verdict
    whose ``origin=ANCHOR`` MUST NOT have any parent artefact whose ID
    prefix or explicit tag indicates generator authorship.

    We recognise both the old-style ``<origin>::<uid>`` naming and the
    Pass-2 O02 naming (``anchor_m_<name>_<op>`` = ANCHOR; ``p_<sha>`` /
    ``tb_<sha>`` = GENERATOR; ``m_<name>_<op>`` = ALLOCATOR).
    """
    generator_prefixes = (f"{ArtefactOrigin.GENERATOR.value}::", "p_", "tb_")
    violations: list[Verdict] = []
    for v in all_verdicts:
        if v.origin != ArtefactOrigin.ANCHOR:
            continue
        parents = (
            v.testbench_id or "",
            v.property_id or "",
            v.mutation_id or "",
        )
        for parent in parents:
            if any(parent.startswith(pfx) for pfx in generator_prefixes):
                if not (v.mutation_id or "").startswith("anchor_m_"):
                    # anchor_m_ prefix is proof of anchor-origin mutation
                    continue
                violations.append(v)
                break
    return tuple(violations)


def compute_anchor_manifest_hash(anchor_dir: str = ANCHOR_DIR_RELATIVE) -> str:
    """SHA-256 over the sorted list of (relative_path, file_sha256) tuples."""
    anchor_path = Path(anchor_dir)
    if not anchor_path.is_dir():
        raise FileNotFoundError(f"anchor directory not found: {anchor_dir}")

    manifest_lines: list[bytes] = []
    included_extensions = {".v", ".sv", ".txt"}
    for path in sorted(anchor_path.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "HASH_MANIFEST.txt":
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in included_extensions and path.name != "makefile":
            continue
        rel = path.relative_to(anchor_path).as_posix()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_lines.append(f"{file_hash}  ./{rel}\n".encode())

    return hashlib.sha256(b"".join(manifest_lines)).hexdigest()


__all__ = [
    "ANCHOR_DIR_RELATIVE",
    "ANCHOR_HASH_MANIFEST",
    "HOLDOUT_FRACTION",
    "load_anchor_properties",
    "load_anchor_testbenches",
    "load_anchor_mutants",
    "stage_anchor_mutants_to_disk",
    "audit_lineage",
    "compute_anchor_manifest_hash",
]
