"""Verilog-source mutation seeding + SBY-miter equivalence checking.

Design decision (Pass 2 open, 2026-08-16): mutations are applied at the
Verilog *source* level rather than the AIG level. Two motivations:

1. Readers-inspectable evidence — every mutant produces a source diff
   that a NeurIPS readers can eyeball. AIG-level mutations require the
   readers to trust the AIG-to-mutation-semantics mapping.
2. Time budget — the Pass-2 pilot has a 4h wall-clock ceiling on 12
   cells. Verilog-source mutation via a small regex library ships in
   <1 day; AIG mutation via aigmap + custom binary manipulation is
   3-4 days of scaffolding for equivalent expressive power on the
   RTLLM small-design corpus.

The operator taxonomy below mirrors µGA / universalmutator's core
operators, adapted for the SystemVerilog subset that RTLLM ships:

- ``constant_flip_0_to_1`` / ``constant_flip_1_to_0`` : swap 1'b0 ↔ 1'b1
- ``bitwise_and_to_or`` / ``bitwise_or_to_and``       : swap & ↔ |
- ``signal_invert_rhs``                               : negate an assign RHS
- ``stuck_at_0`` / ``stuck_at_1``                     : force an assign RHS to a constant
- ``arithmetic_op_swap``                              : + ↔ -
- ``compare_op_swap``                                 : < ↔ <=, > ↔ >=

``check_equivalence`` constructs a SymbiYosys miter that instantiates
both reference and mutant modules with identical inputs and asserts
output equality. If sby PROVES the miter, the mutant is behaviourally
equivalent to the reference (a benign mutation that should be filtered
out of the E1 scoring pool). If sby DISPROVES, the mutation changes
observable behaviour and is a valid candidate for E1 kill-rate scoring.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .types import ArtefactOrigin, Design, MutationTarget, Verdict, VerdictClass


DEFAULT_MUTATION_OPERATORS: tuple[str, ...] = (
    "constant_flip_0_to_1",
    "constant_flip_1_to_0",
    "bitwise_and_to_or",
    "bitwise_or_to_and",
    "signal_invert_rhs",
    "stuck_at_0",
    "stuck_at_1",
    "arithmetic_op_swap",
    "compare_op_swap",
)

def _resolve_sby_bin() -> str:
    """Resolve the sby binary. Prefer $O02_SBY_BIN, then $PATH, then OSS CAD Suite."""
    override = os.environ.get("O02_SBY_BIN")
    if override and shutil.which(override):
        return override
    p = shutil.which("sby")
    if p:
        return p
    cad_bin = os.path.expanduser("~/tools/oss-cad-suite/bin/sby")
    if os.path.exists(cad_bin):
        return cad_bin
    return "sby"  # let subprocess raise a clear error


def _env_with_oss_cad_suite() -> dict:
    """Return an env with OSS CAD Suite bin prepended to PATH so sby finds yosys/z3."""
    env = os.environ.copy()
    cad_bin_dir = os.path.expanduser("~/tools/oss-cad-suite/bin")
    if os.path.isdir(cad_bin_dir) and cad_bin_dir not in env.get("PATH", ""):
        env["PATH"] = cad_bin_dir + os.pathsep + env.get("PATH", "")
    return env


SBY_BIN: str = _resolve_sby_bin()
MUTATION_STAGE_DIR: Path = Path(
    os.environ.get(
        "O02_MUTATION_STAGE_DIR",
        str(Path(__file__).resolve().parents[1] / "runs" / "e1" / "mutants"),
    )
)
DEFAULT_MITER_DEPTH: int = 20
DEFAULT_MITER_ENGINE: str = "z3"


# ─────────────────────────── seed_mutants ───────────────────────────


def seed_mutants(
    design: Design,
    operators: tuple[str, ...] = DEFAULT_MUTATION_OPERATORS,
    origin: ArtefactOrigin = ArtefactOrigin.ALLOCATOR,
    max_per_operator: int = 3,
) -> tuple[MutationTarget, ...]:
    """Emit MutationTarget records + write mutated Verilog to disk.

    For each operator, apply up to ``max_per_operator`` mutations to distinct
    eligible sites in the reference implementation. Each mutant lives at
    ``MUTATION_STAGE_DIR/<design_top>/<operator>_<idx>.v`` and shares the
    reference's top-module name (so downstream miters can rebind directly).
    """
    ref_path = design.reference_impl_file or design.verilog_files[0]
    with open(ref_path) as f:
        ref_src = f.read()

    out_dir = MUTATION_STAGE_DIR / design.top_module
    out_dir.mkdir(parents=True, exist_ok=True)

    mutants: list[MutationTarget] = []

    for op in operators:
        sites = _find_sites(ref_src, op)
        for idx, site in enumerate(sites[:max_per_operator]):
            mutated = _apply_mutation(ref_src, op, site)
            if mutated == ref_src:
                continue  # substitution didn't take — skip silently
            mutant_top = f"{design.top_module}__mut_{op}_{idx}"
            renamed = _rename_top_module(
                mutated, design.top_module, mutant_top, ref_path,
            )
            mutant_path = out_dir / f"{op}_{idx}.v"
            mutant_path.write_text(renamed)
            mutants.append(MutationTarget(
                mutation_id=f"m_{design.top_module}_{op}_{idx}",
                design_id=design.design_id,
                origin=origin,
                operator=op,
                site=f"{ref_path}:byte{site['start']}",
                intended_effect=_effect_for(op, site.get("match", "")),
            ))

    return tuple(mutants)


def _find_sites(src: str, operator: str) -> list[dict]:
    """Return list of {'start': int, 'end': int, 'match': str} for the operator."""
    if operator == "constant_flip_0_to_1":
        return _matches(src, r"\b(\d+)'b0\b|\b1'b0\b")
    if operator == "constant_flip_1_to_0":
        return _matches(src, r"\b(\d+)'b1\b|\b1'b1\b")
    if operator == "bitwise_and_to_or":
        return _matches(src, r"(?<![\w&])&(?![\w=&])")
    if operator == "bitwise_or_to_and":
        return _matches(src, r"(?<![\w|])\|(?![\w=|])")
    if operator == "signal_invert_rhs":
        return _matches(src, r"^\s*assign\s+\w+.*=\s*[^;]+;", flags=re.MULTILINE)
    if operator == "stuck_at_0":
        return _matches(src, r"^\s*assign\s+\w+(?:\[[^\]]+\])?\s*=\s*[^;]+;", flags=re.MULTILINE)
    if operator == "stuck_at_1":
        return _matches(src, r"^\s*assign\s+\w+(?:\[[^\]]+\])?\s*=\s*[^;]+;", flags=re.MULTILINE)
    if operator == "arithmetic_op_swap":
        return _matches(src, r"(?<!\+)\+(?![\+=])")
    if operator == "compare_op_swap":
        return _matches(src, r"<=|>=|(?<!=)<(?!=)|(?<!=)>(?!=)")
    return []


def _matches(src: str, pattern: str, flags: int = 0) -> list[dict]:
    out: list[dict] = []
    for m in re.finditer(pattern, src, flags=flags):
        out.append({"start": m.start(), "end": m.end(), "match": m.group(0)})
    return out


def _apply_mutation(src: str, operator: str, site: dict) -> str:
    """Apply one operator at one site; return the new source."""
    start, end, matched = site["start"], site["end"], site["match"]
    prefix, suffix = src[:start], src[end:]

    if operator == "constant_flip_0_to_1":
        return prefix + matched.replace("'b0", "'b1") + suffix
    if operator == "constant_flip_1_to_0":
        return prefix + matched.replace("'b1", "'b0") + suffix
    if operator == "bitwise_and_to_or":
        return prefix + "|" + suffix
    if operator == "bitwise_or_to_and":
        return prefix + "&" + suffix
    if operator == "signal_invert_rhs":
        m = re.match(r"^(\s*assign\s+\w+(?:\[[^\]]+\])?\s*=\s*)([^;]+);", matched)
        if not m:
            return src
        return prefix + m.group(1) + "~(" + m.group(2).strip() + ");" + suffix
    if operator == "stuck_at_0":
        m = re.match(r"^(\s*assign\s+\w+(?:\[[^\]]+\])?\s*=\s*)([^;]+);", matched)
        if not m:
            return src
        return prefix + m.group(1) + "{$bits(" + m.group(1).split("assign")[1].strip().rstrip("=").strip().split()[0] + "){1'b0}};" + suffix
    if operator == "stuck_at_1":
        m = re.match(r"^(\s*assign\s+(\w+)(?:\[[^\]]+\])?\s*=\s*)([^;]+);", matched)
        if not m:
            return src
        return prefix + m.group(1) + "{$bits(" + m.group(2) + "){1'b1}};" + suffix
    if operator == "arithmetic_op_swap":
        return prefix + "-" + suffix
    if operator == "compare_op_swap":
        swap = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}[matched]
        return prefix + swap + suffix
    return src


_MODULE_DECL_RE = re.compile(r"\bmodule\s+(\w+)\b")


def _rename_top_module(src: str, old_top: str, new_top: str, ref_path: str) -> str:
    """Rename every module in the mutant so it can coexist with the reference.

    Both the reference and mutant files typically declare the same set of
    submodules (e.g. RTLLM's adder_8bit ships `verified_adder_8bit` + a
    `full_adder` helper). Renaming ONLY the top causes yosys to fail with
    'Re-definition of module ...' when the miter's [files] section pulls in
    both. So: append a `__mut_<slug>` suffix to every module name AND to every
    instantiation site of those names within this file.
    """
    slug = new_top.split("__mut_", 1)[-1] if "__mut_" in new_top else "mut"
    module_names = _MODULE_DECL_RE.findall(src)
    seen: set[str] = set()
    out = src
    for name in module_names:
        if name in seen:
            continue
        seen.add(name)
        new_name = f"{name}__mut_{slug}"
        # Rename definitions
        out = re.sub(rf"\bmodule\s+{re.escape(name)}\b", f"module {new_name}", out)
        # Rename instantiations (heuristic: `<name>` at the start of a line
        # or after whitespace, followed by an identifier + '(')
        out = re.sub(
            rf"(^|\s){re.escape(name)}(\s+\w+\s*\()",
            rf"\1{new_name}\2",
            out, flags=re.MULTILINE,
        )
    return out


def _effect_for(operator: str, matched: str) -> str:
    return {
        "constant_flip_0_to_1": f"flipped constant 0→1 at token '{matched}'",
        "constant_flip_1_to_0": f"flipped constant 1→0 at token '{matched}'",
        "bitwise_and_to_or": "swapped bitwise & → | (weakened conjunction)",
        "bitwise_or_to_and": "swapped bitwise | → & (strengthened conjunction)",
        "signal_invert_rhs": f"inverted RHS of '{matched.strip()[:60]}'",
        "stuck_at_0": f"forced RHS of '{matched.strip()[:60]}' to all-zeros",
        "stuck_at_1": f"forced RHS of '{matched.strip()[:60]}' to all-ones",
        "arithmetic_op_swap": "swapped + → - (arithmetic-op mutation)",
        "compare_op_swap": f"swapped compare op at token '{matched}'",
    }.get(operator, f"unknown operator {operator}")


# ─────────────────────────── check_equivalence ───────────────────────────


def check_equivalence(
    reference_verilog_file: str,
    mutant_verilog_file: str,
    ref_top: str,
    mut_top: str,
    engine: str = DEFAULT_MITER_ENGINE,
    depth: int = DEFAULT_MITER_DEPTH,
    workdir: Path | None = None,
) -> Verdict:
    """SBY-miter equivalence check between reference and mutant modules.

    Returns Verdict with:
      - outcome=MUTATION_SURVIVED if sby PROVES miter (behaviourally equivalent)
      - outcome=MUTATION_KILLED   if sby DISPROVES miter (distinguishable)
      - outcome=TIMEOUT / ENGINE_ERROR / RESOURCE_EXHAUSTED on failure modes

    Deliberate naming choice: we treat "behaviourally equivalent = mutation
    survived" because such a mutant is undetectable by ANY testbench and
    should be filtered out of the E1 scoring pool. Downstream aggregators
    read this Verdict + drop equivalent mutants before computing kill rates.
    """
    workdir = workdir or (MUTATION_STAGE_DIR / "miter" / uuid.uuid4().hex[:12])
    workdir.mkdir(parents=True, exist_ok=True)

    ports = _infer_ports(reference_verilog_file, ref_top)
    ref_inst = _declared_ref_top(reference_verilog_file, ref_top)
    miter_sv = _build_miter(ports, ref_top, mut_top, ref_inst=ref_inst)
    miter_path = workdir / "miter.sv"
    miter_path.write_text(miter_sv)

    ref_copy = workdir / Path(reference_verilog_file).name
    mut_copy = workdir / Path(mutant_verilog_file).name
    shutil.copy(reference_verilog_file, ref_copy)
    shutil.copy(mutant_verilog_file, mut_copy)

    sby_src = _build_sby(
        engine=engine,
        depth=depth,
        miter_top="mut_miter",
        miter_file=miter_path.name,
        ref_file=ref_copy.name,
        mut_file=mut_copy.name,
    )
    sby_path = workdir / "miter.sby"
    sby_path.write_text(sby_src)

    t0 = time.time()
    try:
        proc = subprocess.run(
            [SBY_BIN, "-f", sby_path.name],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=max(30, depth * 6),
            env=_env_with_oss_cad_suite(),
        )
    except subprocess.TimeoutExpired:
        wall = time.time() - t0
        return Verdict(
            verdict_id=f"miter_{workdir.name}",
            testbench_id=None,
            property_id=None,
            mutation_id=None,
            engine=f"sby+{engine}",
            outcome=VerdictClass.TIMEOUT,
            origin=ArtefactOrigin.ALLOCATOR,
            wall_seconds=wall,
            smt_seconds=wall,
        )
    wall = time.time() - t0
    stdout = proc.stdout + "\n" + proc.stderr
    outcome = _classify_sby_output(proc.returncode, stdout)

    return Verdict(
        verdict_id=f"miter_{workdir.name}",
        testbench_id=None,
        property_id=None,
        mutation_id=None,
        engine=f"sby+{engine}",
        outcome=outcome,
        origin=ArtefactOrigin.ALLOCATOR,
        wall_seconds=wall,
        smt_seconds=wall,
        counterexample_trace=str(workdir / "miter" / "engine_0" / "trace.vcd")
                            if outcome == VerdictClass.MUTATION_KILLED else None,
    )


_PORT_RE = re.compile(
    r"module\s+(?:verified_)?%s\s*\(([^)]*)\)",
    re.MULTILINE,
)


def _infer_ports(verilog_file: str, top: str) -> list[dict]:
    """Extract (dir, width, name) from the module header.

    Handles the common RTLLM shape:
      module foo(input [7:0] a, b, input cin, output [7:0] sum, output cout);
    The width and direction carry forward across bare names until the next
    ``input|output|inout`` keyword.
    """
    with open(verilog_file) as f:
        src = f.read()
    header_re = re.compile(
        rf"module\s+(?:verified_)?{re.escape(top)}\s*\(([^)]*)\)",
        re.DOTALL,
    )
    m = header_re.search(src)
    if not m:
        return []
    body = m.group(1)
    ports: list[dict] = []
    cur_dir = "input"
    cur_width = ""
    for chunk in body.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        pm = re.match(
            r"(input|output|inout)\s*(?:reg|wire)?\s*(\[[^\]]+\])?\s*(\w+)",
            chunk,
        )
        if pm:
            cur_dir = pm.group(1)
            cur_width = pm.group(2) or ""
            ports.append({"dir": cur_dir, "width": cur_width, "name": pm.group(3)})
        else:
            name_only = re.match(r"(\w+)$", chunk)
            if name_only:
                ports.append({"dir": cur_dir, "width": cur_width, "name": name_only.group(1)})
    return ports


def _declared_ref_top(verilog_file: str, base_top: str) -> str:
    """Return the module name the reference file actually declares.

    RTLLM references declare ``verified_<base_top>``; VerilogEval references
    declare the base name directly (``RefModule``). Preserves the historical
    E1 behaviour (verified_ prefix) whenever that declaration exists.
    """
    with open(verilog_file) as f:
        src = f.read()
    if re.search(rf"^\s*module\s+verified_{re.escape(base_top)}\b", src, re.MULTILINE):
        return f"verified_{base_top}"
    return base_top


def _build_miter(ports: list[dict], ref_top: str, mut_top: str,
                 ref_inst: str | None = None) -> str:
    """Build a SystemVerilog miter module. Inputs shared; outputs compared."""
    inputs = [p for p in ports if p["dir"] == "input"]
    outputs = [p for p in ports if p["dir"] == "output"]

    hdr_ins = ", ".join(f"input {p['width']} {p['name']}".strip().replace("  ", " ") for p in inputs)
    ref_output_decls = "\n".join(
        f"  wire {p['width']} ref_{p['name']};".replace("  ", " ") for p in outputs
    )
    mut_output_decls = "\n".join(
        f"  wire {p['width']} mut_{p['name']};".replace("  ", " ") for p in outputs
    )
    ref_port_map = ", ".join(
        [f".{p['name']}({p['name']})" for p in inputs]
        + [f".{p['name']}(ref_{p['name']})" for p in outputs]
    )
    mut_port_map = ", ".join(
        [f".{p['name']}({p['name']})" for p in inputs]
        + [f".{p['name']}(mut_{p['name']})" for p in outputs]
    )
    equality_asserts = "\n".join(
        f"    assert (ref_{p['name']} === mut_{p['name']});" for p in outputs
    )

    return (
        f"module mut_miter ({hdr_ins});\n"
        f"{ref_output_decls}\n"
        f"{mut_output_decls}\n"
        f"  {ref_inst or f'verified_{ref_top}'} ref_dut({ref_port_map});\n"
        f"  {mut_top} mut_dut({mut_port_map});\n"
        f"  always @* begin\n"
        f"{equality_asserts}\n"
        f"  end\n"
        f"endmodule\n"
    )


def _build_sby(
    *,
    engine: str,
    depth: int,
    miter_top: str,
    miter_file: str,
    ref_file: str,
    mut_file: str,
) -> str:
    return (
        f"[options]\n"
        f"mode prove\n"
        f"depth {depth}\n\n"
        f"[engines]\n"
        f"smtbmc {engine}\n\n"
        f"[script]\n"
        f"read -formal {ref_file}\n"
        f"read -formal {mut_file}\n"
        f"read -formal {miter_file}\n"
        f"prep -top {miter_top}\n\n"
        f"[files]\n"
        f"{miter_file}\n"
        f"{ref_file}\n"
        f"{mut_file}\n"
    )


_SBY_PASS_RE = re.compile(r"DONE\s*\(PASS", re.IGNORECASE)
_SBY_FAIL_RE = re.compile(r"DONE\s*\(FAIL", re.IGNORECASE)
_SBY_TIMEOUT_RE = re.compile(r"DONE\s*\(TIMEOUT|timeout", re.IGNORECASE)


def _classify_sby_output(rc: int, stdout: str) -> VerdictClass:
    if _SBY_TIMEOUT_RE.search(stdout):
        return VerdictClass.TIMEOUT
    if _SBY_FAIL_RE.search(stdout):
        return VerdictClass.MUTATION_KILLED
    if _SBY_PASS_RE.search(stdout):
        return VerdictClass.MUTATION_SURVIVED
    if "out of memory" in stdout.lower() or "killed" in stdout.lower():
        return VerdictClass.RESOURCE_EXHAUSTED
    return VerdictClass.ENGINE_ERROR


__all__ = [
    "DEFAULT_MUTATION_OPERATORS",
    "seed_mutants",
    "check_equivalence",
]
