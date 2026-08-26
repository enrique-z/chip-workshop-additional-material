"""Data spine for the co-evolving generator/allocator loop.

Design principle: every artefact that crosses the generator ↔ allocator
boundary is a frozen dataclass. Immutability lets us cheaply snapshot
the loop state per iteration and reconstruct any run for the Pass 5
reproducibility audit.

Kill-rule anchor: the paper fails if hidden-oracle separation is
compromised. ``Verdict.origin`` MUST distinguish generator-authored
artefacts from anchor-set artefacts; a Verdict whose origin is
``ANCHOR`` and whose parent is ``GENERATOR`` fires a lineage-violation
error at build gate 3 (Pass 5 audit).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ArtefactOrigin(str, Enum):
    """Lineage tag — critical for hidden-oracle separation."""
    GENERATOR = "generator"          # LLM-authored, inside optimization loop
    ANCHOR = "anchor"                # sequestered, never touches generator
    ALLOCATOR = "allocator"          # coverage-heuristic, deterministic
    HUMAN = "human"                  # independently-authored baseline


class VerdictClass(str, Enum):
    """Deng 2026-inspired outcome taxonomy for verification runs."""
    PROVED = "proved"                # k-induction succeeded up to depth K
    DISPROVED = "disproved"          # counterexample found
    BOUNDED_PASS = "bounded_pass"    # BMC up to depth K without cex
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTED = "oom"
    ENGINE_ERROR = "engine_error"    # sby/yosys/z3 crash — like Rosetta CTS
    MUTATION_KILLED = "mutation_killed"     # miter distinguished mut from ref
    MUTATION_SURVIVED = "mutation_survived" # test suite didn't detect the mutation


@dataclass(frozen=True)
class Design:
    """RTL design under test (from RTLLM or VerilogEval corpora).

    RealBench was originally scoped as E3 corpus but the repository could
    not be located 2026-08-16 (see adv-R1 C3); E3 pivoted to the 6-design
    RTLLM sequestered anchor set at evidence/anchor_sequestered/.
    """
    design_id: str                       # e.g. "verilogeval::adder_8bit"
    corpus: str                          # "rtllm" | "verilogeval"
    top_module: str
    verilog_files: tuple[str, ...]       # absolute paths
    reference_impl_file: Optional[str] = None
    reference_testbench_file: Optional[str] = None


@dataclass(frozen=True)
class Property:
    """A formal property (SVA-style assertion or safety invariant)."""
    property_id: str
    design_id: str
    origin: ArtefactOrigin
    sva_text: str                        # SystemVerilog Assertion body
    intended_bound: int = 20             # k-induction depth to attempt
    parent_iteration: Optional[int] = None  # generator iteration that proposed it


@dataclass(frozen=True)
class Testbench:
    """A simulation testbench (verilator/iverilog driver + expected trace)."""
    testbench_id: str
    design_id: str
    origin: ArtefactOrigin
    driver_verilog_file: str
    expected_trace_hash: Optional[str] = None
    coverage_targets: tuple[str, ...] = ()  # signal names to hit
    parent_iteration: Optional[int] = None


@dataclass(frozen=True)
class MutationTarget:
    """AIG-mutation operator applied to a specific site of the design."""
    mutation_id: str
    design_id: str
    origin: ArtefactOrigin
    operator: str                        # e.g. "flip_and_gate", "invert_signal"
    site: str                            # AIG node ID or Verilog file:line
    intended_effect: str                 # human-readable behaviour change


@dataclass(frozen=True)
class Verdict:
    """Result of running a (testbench, property, mutant) triple through the allocator."""
    verdict_id: str
    testbench_id: Optional[str]
    property_id: Optional[str]
    mutation_id: Optional[str]
    engine: str                          # "verilator" | "sby+z3" | "sby+cvc5" | ...
    outcome: VerdictClass
    origin: ArtefactOrigin               # lineage — must match parents
    wall_seconds: float
    smt_seconds: float = 0.0
    sim_seconds: float = 0.0
    llm_tokens: int = 0
    counterexample_trace: Optional[str] = None


@dataclass(frozen=True)
class BudgetLedger:
    """Fixed verification budget the allocator distributes across engines.

    All units in seconds (sim/SMT) or tokens (LLM). Ledger is decremented
    per Verdict; ``exhausted()`` returns True when any dimension hits 0.
    """
    sim_seconds_remaining: float
    smt_seconds_remaining: float
    llm_tokens_remaining: int

    def exhausted(self) -> bool:
        return (
            self.sim_seconds_remaining <= 0
            or self.smt_seconds_remaining <= 0
            or self.llm_tokens_remaining <= 0
        )

    def debit(self, verdict: "Verdict") -> "BudgetLedger":
        """Return a NEW ledger with budgets debited by verdict's cost."""
        return BudgetLedger(
            sim_seconds_remaining=self.sim_seconds_remaining - verdict.sim_seconds,
            smt_seconds_remaining=self.smt_seconds_remaining - verdict.smt_seconds,
            llm_tokens_remaining=self.llm_tokens_remaining - verdict.llm_tokens,
        )


__all__ = [
    "ArtefactOrigin",
    "VerdictClass",
    "Design",
    "Property",
    "Testbench",
    "MutationTarget",
    "Verdict",
    "BudgetLedger",
]
