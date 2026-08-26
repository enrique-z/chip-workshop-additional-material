"""Smoke tests for ``modules.anchor_audit`` — runnable at any pass boundary.

Two invariants tested:

1. ``compute_anchor_manifest_hash(evidence/anchor_sequestered)`` returns the
   exact SHA-256 recorded in
   ``HYPOTHESIS_REFINED.json.anchor_frozen_2026_08_16.manifest_root_sha256``.
   A mismatch means the anchor set drifted since freeze -> paper hard-kills.

2. ``audit_lineage(...)`` correctly identifies an ANCHOR-origin Verdict whose
   parent traces to a GENERATOR-authored artefact. Confirms the paper's
   central kill-rule invariant (hidden-oracle separation) has real code
   backing, not just a docstring.

Exit codes: 0 = both pass; 1 = hash mismatch; 2 = lineage audit broken.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAPER = HERE.parent
sys.path.insert(0, str(PAPER))

from modules.anchor_audit import audit_lineage, compute_anchor_manifest_hash  # noqa: E402
from modules.types import ArtefactOrigin, Verdict, VerdictClass  # noqa: E402


def test_hash_matches_freeze_doc() -> bool:
    freeze_doc = json.loads((PAPER / "HYPOTHESIS_REFINED.json").read_text())
    expected = freeze_doc["anchor_frozen_2026_08_16"]["manifest_root_sha256"]
    actual = compute_anchor_manifest_hash(str(PAPER / "evidence" / "anchor_sequestered"))
    if actual != expected:
        print(f"!!! anchor hash mismatch: expected {expected}, got {actual}", file=sys.stderr)
        return False
    print(f"    anchor hash OK ({actual})")
    return True


def test_lineage_audit_catches_generator_parent_of_anchor_verdict() -> bool:
    clean = Verdict(
        verdict_id="v1", testbench_id="anchor::t1", property_id="anchor::p1",
        mutation_id="anchor::m1", engine="z3", outcome=VerdictClass.PROVED,
        origin=ArtefactOrigin.ANCHOR, wall_seconds=1.0,
    )
    violation = Verdict(
        verdict_id="v2", testbench_id="anchor::t2",
        property_id="generator::iter3_prop_17", mutation_id="anchor::m2",
        engine="z3", outcome=VerdictClass.PROVED,
        origin=ArtefactOrigin.ANCHOR, wall_seconds=1.0,
    )
    non_anchor = Verdict(
        verdict_id="v3", testbench_id="generator::t3", property_id="generator::p3",
        mutation_id=None, engine="z3", outcome=VerdictClass.PROVED,
        origin=ArtefactOrigin.GENERATOR, wall_seconds=1.0,
    )
    caught = audit_lineage((clean, violation, non_anchor))
    if len(caught) != 1 or caught[0].verdict_id != "v2":
        print(f"!!! lineage audit broken: expected 1 violation (v2), got {[v.verdict_id for v in caught]}", file=sys.stderr)
        return False
    print(f"    lineage audit OK (violation v2 caught, clean v1 + non-anchor v3 ignored)")
    return True


def main() -> int:
    print("==> smoke test 1: compute_anchor_manifest_hash vs freeze doc")
    if not test_hash_matches_freeze_doc():
        return 1
    print("==> smoke test 2: audit_lineage catches generator-parent-of-anchor")
    if not test_lineage_audit_catches_generator_parent_of_anchor_verdict():
        return 2
    print("==> SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
