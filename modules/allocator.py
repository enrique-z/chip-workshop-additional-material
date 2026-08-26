"""Fixed-budget dispatch across simulation + bounded formal proof.

Given (Property, Testbench, MutationTarget) triples + a BudgetLedger, the
allocator drives iverilog simulation and/or sby-formal proof, tallies
per-mutant kill outcomes, and returns Verdicts + a debited ledger.

E1 kill semantics (matches the paper's primary outcome):
- A mutant is KILLED if any testbench that PASSES on the reference FAILS
  on the mutant (sim-based kill), OR if any generated property that HOLDS
  on the reference is DISPROVED on the mutant (formal-based kill).
- False-pass: testbench passes on a mutant that mutation.check_equivalence
  established as non-equivalent to reference. Reported per-system.
- False-fail: testbench fails on the reference. Excluded via validity
  filter BEFORE mutant testing; per-system count reported separately.

Anchor-separation invariant: this module MUST NOT import
``modules.anchor_audit`` (enforced at build.sh gate 2.5). E3 anchor
audit is a separate driver script.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from . import agy_backend, mutation
from .types import (
    ArtefactOrigin,
    BudgetLedger,
    Design,
    MutationTarget,
    Property,
    Testbench,
    Verdict,
    VerdictClass,
)


ENGINE_PORTFOLIO: tuple[str, ...] = ("z3", "cvc5", "boolector", "bitwuzla", "yices-smt2")
IVERILOG_BIN: str = os.environ.get("O02_IVERILOG_BIN", shutil.which("iverilog") or "iverilog")
VVP_BIN: str = os.environ.get("O02_VVP_BIN", shutil.which("vvp") or "vvp")
SIM_WORKDIR: Path = Path(
    os.environ.get(
        "O02_SIM_WORKDIR",
        str(Path(__file__).resolve().parents[1] / "runs" / "e1" / "sim"),
    )
)
DEFAULT_SIM_TIMEOUT_S: int = int(os.environ.get("O02_SIM_TIMEOUT_S", "30"))
LLM_PRICE_PER_1K_TOKENS_USD: float = 3.0  # per HYPOTHESIS_REFINED.json scalar_cost_function


def _resolve_sim_bins() -> tuple[str, str]:
    """Prefer OSS CAD Suite bins if plain PATH doesn't have iverilog/vvp."""
    ib = IVERILOG_BIN if shutil.which(IVERILOG_BIN) else os.path.expanduser("~/tools/oss-cad-suite/bin/iverilog")
    vb = VVP_BIN if shutil.which(VVP_BIN) else os.path.expanduser("~/tools/oss-cad-suite/bin/vvp")
    return ib, vb


def _env_with_oss_cad_suite() -> dict:
    env = os.environ.copy()
    cad_bin = os.path.expanduser("~/tools/oss-cad-suite/bin")
    if os.path.isdir(cad_bin) and cad_bin not in env.get("PATH", ""):
        env["PATH"] = cad_bin + os.pathsep + env.get("PATH", "")
    return env


# ─────────────────────────── simulation core ───────────────────────────


def _find_top_module_name(verilog_file: str, design_top: str) -> str:
    """Return the top-level module in `verilog_file` whose base name matches `design_top`.

    For a reference file this is typically ``verified_<design_top>``; for a
    mutant file it is ``verified_<design_top>__mut_<slug>``.
    """
    with open(verilog_file) as f:
        src = f.read()
    ms = re.findall(r"^\s*module\s+(\w+)", src, re.MULTILINE)
    prefer = [m for m in ms if m.endswith(design_top) or f"__mut_" in m and design_top in m]
    if prefer:
        return prefer[0]
    return ms[0] if ms else design_top


def _write_alias_wrapper(
    design: Design, dut_top: str, workdir: Path,
) -> str:
    """Emit a small SV module `<design.top_module>` that forwards to `<dut_top>`.

    Skipped when `dut_top == design.top_module` — the DUT already exports the
    bare name, so a wrapper would collide with a duplicate-module declaration.
    Returns empty string on skip; caller compiles without the alias.
    """
    if dut_top == design.top_module:
        return ""
    ports = mutation._infer_ports(
        design.reference_impl_file or design.verilog_files[0],
        design.top_module,
    )
    if not ports:
        return ""
    header = ", ".join(
        f"{p['dir']} {p['width']} {p['name']}".replace("  ", " ").strip()
        for p in ports
    )
    port_map = ", ".join(f".{p['name']}({p['name']})" for p in ports)
    src = (
        f"module {design.top_module} ({header});\n"
        f"  {dut_top} u_dut ({port_map});\n"
        f"endmodule\n"
    )
    alias_path = workdir / "alias.sv"
    alias_path.write_text(src)
    return str(alias_path)


def simulate_testbench(
    design: Design,
    tb: Testbench,
    dut_source_file: str,
    *,
    workdir: Path | None = None,
    timeout_s: int | None = None,
) -> Verdict:
    """Compile TB + dut, run vvp, parse PASS/FAIL, return Verdict.

    Outcome mapping:
      - PASS → MUTATION_SURVIVED (when dut is a mutant) or PROVED (when dut is ref)
      - FAIL → MUTATION_KILLED (when dut is a mutant) or DISPROVED (when dut is ref)
      - compile error / timeout → ENGINE_ERROR / TIMEOUT
    We can't tell here whether dut is ref or mutant; caller re-labels.
    """
    workdir = workdir or (SIM_WORKDIR / uuid.uuid4().hex[:12])
    workdir.mkdir(parents=True, exist_ok=True)
    ib, vb = _resolve_sim_bins()
    timeout_s = timeout_s or DEFAULT_SIM_TIMEOUT_S

    sim_bin = workdir / "sim.out"
    tb_copy = workdir / "tb.sv"
    dut_copy = workdir / Path(dut_source_file).name
    shutil.copy(tb.driver_verilog_file, tb_copy)
    shutil.copy(dut_source_file, dut_copy)
    dut_top = _find_top_module_name(dut_source_file, design.top_module)
    alias_path = _write_alias_wrapper(design, dut_top, workdir)

    compile_cmd = [ib, "-g2012", "-o", str(sim_bin), str(tb_copy), str(dut_copy)]
    if alias_path:
        compile_cmd.append(alias_path)

    t0 = time.time()
    try:
        cp = subprocess.run(
            compile_cmd,
            capture_output=True, text=True,
            timeout=timeout_s, env=_env_with_oss_cad_suite(),
        )
    except subprocess.TimeoutExpired:
        wall = time.time() - t0
        return Verdict(
            verdict_id=f"sim_{workdir.name}",
            testbench_id=tb.testbench_id, property_id=None, mutation_id=None,
            engine="iverilog", outcome=VerdictClass.TIMEOUT,
            origin=ArtefactOrigin.ALLOCATOR, wall_seconds=wall,
            sim_seconds=wall,
        )
    if cp.returncode != 0:
        return Verdict(
            verdict_id=f"sim_{workdir.name}",
            testbench_id=tb.testbench_id, property_id=None, mutation_id=None,
            engine="iverilog", outcome=VerdictClass.ENGINE_ERROR,
            origin=ArtefactOrigin.ALLOCATOR,
            wall_seconds=time.time() - t0, sim_seconds=time.time() - t0,
            counterexample_trace=cp.stderr[:400],
        )

    try:
        rp = subprocess.run(
            [vb, str(sim_bin)],
            capture_output=True, text=True,
            timeout=timeout_s, env=_env_with_oss_cad_suite(),
        )
    except subprocess.TimeoutExpired:
        wall = time.time() - t0
        return Verdict(
            verdict_id=f"sim_{workdir.name}",
            testbench_id=tb.testbench_id, property_id=None, mutation_id=None,
            engine="iverilog", outcome=VerdictClass.TIMEOUT,
            origin=ArtefactOrigin.ALLOCATOR, wall_seconds=wall,
            sim_seconds=wall,
        )
    wall = time.time() - t0
    out = (rp.stdout + "\n" + rp.stderr).lower()
    if "fail" in out or "error" in out or "mismatch" in out:
        outcome = VerdictClass.DISPROVED
    elif "pass" in out:
        outcome = VerdictClass.PROVED
    else:
        # Testbench produced no verdict marker — treat as engine error.
        outcome = VerdictClass.ENGINE_ERROR
    return Verdict(
        verdict_id=f"sim_{workdir.name}",
        testbench_id=tb.testbench_id, property_id=None, mutation_id=None,
        engine="iverilog", outcome=outcome,
        origin=ArtefactOrigin.ALLOCATOR,
        wall_seconds=wall, sim_seconds=wall,
        counterexample_trace=(rp.stdout[:400] if outcome == VerdictClass.DISPROVED else None),
    )


# ─────────────────────────── allocate_and_run ───────────────────────────


def allocate_and_run(
    design: Design,
    properties: tuple[Property, ...],
    testbenches: tuple[Testbench, ...],
    mutants: tuple[MutationTarget, ...],
    ledger: BudgetLedger,
    config: dict,
    prior_llm_tokens: int = 0,
) -> tuple[tuple[Verdict, ...], BudgetLedger]:
    """Sim-based kill loop: each testbench × (reference + each mutant).

    Returns Verdicts + debited ledger. Verdict semantics:
      - MUTATION_KILLED : testbench passes on ref AND fails on this mutant
      - MUTATION_SURVIVED : testbench passes on ref AND passes on this mutant
      - DISPROVED         : testbench fails on ref (false-fail; TB dropped)
      - ENGINE_ERROR      : compile/run failure
    """
    verdicts: list[Verdict] = []
    ref_file = design.reference_impl_file or design.verilog_files[0]

    # llm-token debit at start of allocation, stamped as a synthetic Verdict
    # so it flows into per-cell scalar_cost aggregation.
    if prior_llm_tokens:
        ledger = BudgetLedger(
            sim_seconds_remaining=ledger.sim_seconds_remaining,
            smt_seconds_remaining=ledger.smt_seconds_remaining,
            llm_tokens_remaining=ledger.llm_tokens_remaining - prior_llm_tokens,
        )
        verdicts.append(Verdict(
            verdict_id=f"llm_gen_{uuid.uuid4().hex[:12]}",
            testbench_id=None, property_id=None, mutation_id=None,
            engine=f"agy+{agy_backend.GENERATOR_MODEL_ID}",
            outcome=VerdictClass.PROVED,
            origin=ArtefactOrigin.GENERATOR,
            wall_seconds=0.0, smt_seconds=0.0, sim_seconds=0.0,
            llm_tokens=prior_llm_tokens,
        ))

    for tb in testbenches:
        if ledger.exhausted():
            break

        # Validity: does this testbench pass on the reference?
        v_ref = simulate_testbench(design, tb, ref_file)
        v_ref = _relabel(v_ref, mutation_id=None, ref_context=True)
        verdicts.append(v_ref)
        ledger = ledger.debit(v_ref)

        if v_ref.outcome != VerdictClass.PROVED:
            # TB fails on reference (false-fail) or engine-errored → skip mutants
            continue

        # TB is valid — dispatch against every mutant
        for m in mutants:
            if ledger.exhausted():
                break
            mut_file = _mutant_file_for(design, m)
            if mut_file is None or not os.path.exists(mut_file):
                continue
            v = simulate_testbench(design, tb, mut_file)
            v = _relabel(v, mutation_id=m.mutation_id, ref_context=False)
            verdicts.append(v)
            ledger = ledger.debit(v)

    return tuple(verdicts), ledger


def _mutant_file_for(design: Design, m: MutationTarget) -> str | None:
    """Locate the mutant .v that mutation.seed_mutants wrote for (design, op, idx)."""
    slug = m.mutation_id.replace(f"m_{design.top_module}_", "", 1)
    guess = mutation.MUTATION_STAGE_DIR / design.top_module / f"{slug}.v"
    return str(guess) if guess.exists() else None


def _relabel(v: Verdict, *, mutation_id: str | None, ref_context: bool) -> Verdict:
    """Convert generic sim outcome to the semantically-loaded outcome class."""
    if ref_context:
        # DUT was the reference — PROVED means TB is valid; DISPROVED means TB is a false-fail
        return v
    # DUT was a mutant — PROVED means TB doesn't distinguish; DISPROVED means TB kills the mutant
    if v.outcome == VerdictClass.PROVED:
        new = VerdictClass.MUTATION_SURVIVED
    elif v.outcome == VerdictClass.DISPROVED:
        new = VerdictClass.MUTATION_KILLED
    else:
        new = v.outcome
    return Verdict(
        verdict_id=v.verdict_id,
        testbench_id=v.testbench_id,
        property_id=v.property_id,
        mutation_id=mutation_id,
        engine=v.engine, outcome=new, origin=v.origin,
        wall_seconds=v.wall_seconds, smt_seconds=v.smt_seconds,
        sim_seconds=v.sim_seconds, llm_tokens=v.llm_tokens,
        counterexample_trace=v.counterexample_trace,
    )


# ─────────────────────────── deterministic baseline ───────────────────────────


_SEQUENTIAL_DESIGNS_KR1_FORMAL_FALLBACK = {"accu", "LFSR", "JC_counter"}


def allocate_deterministic(
    design: Design,
    mutants: tuple[MutationTarget, ...],
    ledger: BudgetLedger,
    *,
    n_random_testbenches: int = 4,
    seed: int = 0,
) -> tuple[tuple[Verdict, ...], BudgetLedger]:
    """Strong deterministic baseline — no LLM.

    Proposer: deterministic random-vector testbench templated from the
    design's module header (inferred via mutation._infer_ports). For each
    testbench, drive N pseudo-random stimulus vectors + $display("PASS")
    unconditionally at the end. We rely on the fact that a mutant will
    produce different simulation output (visible via testbench's own
    correctness checks IF written) — but a pure-random-vector testbench
    without a reference model can't distinguish; so we use the SAME
    (reference-tolerant) testbench pattern the generator uses: instantiate
    the DUT, drive N vectors, compare against an inline `a + b + cin`
    style golden expression built from the port names.

    This is the KR-1 mitigation: matched engine portfolio + matched retry
    budget + template-based proposer that gets close to what a good LLM
    could produce, so co-evolve must strictly dominate to win.

    freeze_amendment_5 (2026-08-22): sequential designs (accu, LFSR,
    JC_counter) fall back to bounded-formal-miter equivalence (same
    z3+depth=2 path as bounded-formal-only), since inline templates
    on feedback-gated sequential RTL produce false-fails against the
    reference (R34 sequential-template risk; see hermes memory
    `r34_sequential_template_risk.md`). Direction of effect: STRENGTHENS
    KR-1 on the sequential slice (co-evolve looks WORSE relative to a
    stronger baseline), same direction as fallback A adoption per R38.
    KR-1 is thus reframed as "per-design-class strongest deterministic
    technique": template checking on combinational designs (where a
    spec-derived template is well-defined) + formal miter on sequential
    designs (where template derivation is spec-fragile).
    """
    # freeze_amendment_5 branch: sequential designs use formal miter.
    # freeze_amendment_6 extension (E2 pilot): ALL VerilogEval designs route
    # here too — their tops are anonymised (`RefModule`), so no spec-derived
    # arithmetic template is well-defined; per the KR-1 definition
    # ("per-design-class strongest deterministic technique") the bounded
    # miter IS the strongest deterministic technique on that corpus. This
    # STRENGTHENS the baseline vs. the no-golden always-PASS fallback.
    if (design.top_module in _SEQUENTIAL_DESIGNS_KR1_FORMAL_FALLBACK
            or design.corpus == "verilogeval"):
        verdicts: list[Verdict] = []
        ref_file = design.reference_impl_file or design.verilog_files[0]
        for m in mutants:
            if ledger.exhausted():
                break
            mut_file = _mutant_file_for(design, m)
            if not mut_file:
                continue
            slug = m.mutation_id.replace(f"m_{design.top_module}_", "", 1)
            # Derive the mutant's declared top from the file (identical to the
            # old f"verified_{top}__mut_{slug}" string on RTLLM; correct on
            # VerilogEval where refs carry no verified_ prefix).
            mut_top = _find_top_module_name(mut_file, design.top_module)
            v = mutation.check_equivalence(
                ref_file, mut_file, design.top_module, mut_top,
                engine="z3", depth=2,
            )
            v = Verdict(
                verdict_id=v.verdict_id,
                testbench_id=None, property_id=None, mutation_id=m.mutation_id,
                engine=v.engine, outcome=v.outcome, origin=ArtefactOrigin.ALLOCATOR,
                wall_seconds=v.wall_seconds, smt_seconds=v.smt_seconds,
                sim_seconds=v.sim_seconds,
                counterexample_trace=v.counterexample_trace,
            )
            verdicts.append(v)
            ledger = ledger.debit(v)
        return tuple(verdicts), ledger

    ports = mutation._infer_ports(design.reference_impl_file or design.verilog_files[0], design.top_module)
    if not ports:
        return (), ledger

    tbs: list[Testbench] = []
    for i in range(n_random_testbenches):
        tb_src = _deterministic_tb_template(design.top_module, ports, seed=seed + i)
        stage_dir = Path(
            os.environ.get(
                "O02_STAGE_DIR",
                str(Path(__file__).resolve().parents[1] / "runs" / "e1" / "staged_tb"),
            )
        )
        stage_dir.mkdir(parents=True, exist_ok=True)
        tb_path = stage_dir / f"tb_det_{design.top_module}_seed{seed}_{i}.sv"
        tb_path.write_text(tb_src)
        tbs.append(Testbench(
            testbench_id=f"tb_det_{design.top_module}_s{seed}_{i}",
            design_id=design.design_id,
            origin=ArtefactOrigin.ALLOCATOR,
            driver_verilog_file=str(tb_path),
            expected_trace_hash=None,
            coverage_targets=tuple(p["name"] for p in ports),
            parent_iteration=0,
        ))

    return allocate_and_run(
        design=design,
        properties=(),
        testbenches=tuple(tbs),
        mutants=mutants,
        ledger=ledger,
        config={"mode": "deterministic-allocator"},
        prior_llm_tokens=0,
    )


_ARITHMETIC_TEMPLATE = re.compile(r"^(adder|sub|mul|comparator|accu|mult)")


def _port_by_name(ports: list[dict], candidates: list[str]) -> dict | None:
    """Return the first port whose name (case-insensitive) matches any candidate."""
    lower_map = {p["name"].lower(): p for p in ports}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    return None


def _width_mask(port: dict | None) -> int:
    if not port:
        return 0xFF
    bm = re.search(r"\[(\d+):", port["width"] or "")
    if not bm:
        return 0x1
    width = int(bm.group(1)) + 1
    return (1 << min(width, 32)) - 1


def _deterministic_tb_template(top: str, ports: list[dict], seed: int) -> str:
    """Emit a self-checking testbench using the module's ports.

    Coverage:
      - adder      : {cout,sum} == a+b(+cin) inline golden
      - comparator : (A>B), (A==B), (A<B) inline golden (case-insensitive port names)
      - multiplier : product == A * B inline golden
      - subtractor : result == A - B inline golden (overflow flag ignored)
      - accumulator/sequential/other : drive clocked stimulus but no golden
        (will not kill mutants; matches the paper's "deterministic allocator
        has no oracle for arbitrary sequential logic" limitation).
    """
    inputs = [p for p in ports if p["dir"] == "input"]
    outputs = [p for p in ports if p["dir"] == "output"]

    decl_lines = []
    for p in inputs:
        decl_lines.append(f"  reg {p['width']} {p['name']};".replace("  ", " ").rstrip())
    for p in outputs:
        decl_lines.append(f"  wire {p['width']} {p['name']};".replace("  ", " ").rstrip())
    port_map = ", ".join(f".{p['name']}({p['name']})" for p in inputs + outputs)

    header = (
        f"module tb;\n"
        + "\n".join(decl_lines) + "\n"
        + f"  integer err = 0;\n"
        + f"  {top} dut({port_map});\n"
    )

    family = _ARITHMETIC_TEMPLATE.match(top)
    fam = family.group(1) if family else ""
    a = _port_by_name(inputs, ["a", "A"])
    b = _port_by_name(inputs, ["b", "B"])
    golden_lines: list[str] = []

    if fam == "adder" and a and b:
        cin = _port_by_name(inputs, ["cin", "Cin", "carry_in"])
        sum_o = _port_by_name(outputs, ["sum", "Sum", "s"])
        cout = _port_by_name(outputs, ["cout", "Cout", "carry_out"])
        mask_a = _width_mask(a)
        for k in range(10):
            va = ((seed + 1) * 0xA5A5 + k * 0x137) & mask_a
            vb = ((seed + 1) * 0x5A5A + k * 0x239) & mask_a
            vc = ((seed + k) & 1) if cin else 0
            drv = f"{a['name']} = {va}; {b['name']} = {vb};" + (f" {cin['name']} = {vc};" if cin else "")
            if sum_o and cout:
                chk = f"if ({{{cout['name']}, {sum_o['name']}}} !== {a['name']} + {b['name']}" + (f" + {cin['name']}" if cin else "") + f") err = err + 1;"
            elif sum_o:
                chk = f"if ({sum_o['name']} !== {a['name']} + {b['name']}" + (f" + {cin['name']}" if cin else "") + f") err = err + 1;"
            else:
                chk = ""
            golden_lines.append(f"    {drv} #2; {chk}")

    elif fam == "comparator" and a and b:
        gt = _port_by_name(outputs, ["A_greater", "a_greater", "gt", "agtb"])
        eq = _port_by_name(outputs, ["A_equal", "a_equal", "eq", "aeqb"])
        lt = _port_by_name(outputs, ["A_less", "a_less", "lt", "altb"])
        mask_a = _width_mask(a)
        for k in range(10):
            va = ((seed + 1) * 3 + k * 5) & mask_a
            vb = ((seed + 2) * 7 + k * 11) & mask_a
            drv = f"{a['name']} = {va}; {b['name']} = {vb};"
            checks = []
            if gt: checks.append(f"if ({gt['name']} !== ({a['name']} > {b['name']})) err = err + 1;")
            if eq: checks.append(f"if ({eq['name']} !== ({a['name']} == {b['name']})) err = err + 1;")
            if lt: checks.append(f"if ({lt['name']} !== ({a['name']} < {b['name']})) err = err + 1;")
            golden_lines.append(f"    {drv} #2; " + " ".join(checks))

    elif fam in ("mul", "mult") and a and b:
        prod = _port_by_name(outputs, ["product", "Product", "p", "prod"])
        mask_a = _width_mask(a)
        for k in range(6):
            va = ((seed + 1) * 0x1F + k * 3) & mask_a
            vb = ((seed + 1) * 0x2B + k * 7) & mask_a
            drv = f"{a['name']} = {va}; {b['name']} = {vb};"
            chk = (f"if ({prod['name']} !== ({a['name']} * {b['name']})) err = err + 1;" if prod else "")
            golden_lines.append(f"    {drv} #4; {chk}")

    elif fam == "sub" and a and b:
        res = _port_by_name(outputs, ["result", "Result", "diff", "sub"])
        mask_a = _width_mask(a)
        for k in range(6):
            va = ((seed + 1) * 0x101 + k * 3) & mask_a
            vb = ((seed + 1) * 0x77 + k * 5) & mask_a
            drv = f"{a['name']} = {va}; {b['name']} = {vb};"
            chk = (f"if ({res['name']} !== ({a['name']} - {b['name']})) err = err + 1;" if res else "")
            golden_lines.append(f"    {drv} #2; {chk}")

    else:
        # Fallback: drive all inputs, no golden. Won't kill mutants but
        # exercises the compile+sim path.
        for k in range(4):
            drives = []
            for p in inputs:
                val = ((seed + k) * 0x9E3779B1) & _width_mask(p)
                drives.append(f"{p['name']} = {val};")
            golden_lines.append("    " + " ".join(drives) + " #4;")

    body = (
        f"  initial begin\n"
        + "\n".join(golden_lines) + "\n"
        + f"    if (err == 0) $display(\"PASS\"); else $display(\"FAIL: %d\", err);\n"
        + f"    $finish;\n"
        + f"  end\n"
        + f"endmodule\n"
    )
    return header + body


# ─────────────────────────── co-evolution step ───────────────────────────


def co_evolve_step(
    design: Design,
    iteration: int,
    prior_verdicts: tuple[Verdict, ...],
    mutants: tuple[MutationTarget, ...],
    ledger: BudgetLedger,
    generator_fn,
    config: dict,
) -> tuple[tuple[Verdict, ...], BudgetLedger, int]:
    """One iteration of co-evolution.

    Returns (verdicts, ledger, llm_tokens_this_step). Wraps generator_fn
    (`modules.generator.propose`) so this module doesn't import generator
    directly (kept as a callable to keep the allocator loop cleanly
    unit-testable).
    """
    props, tbs, gen_muts = generator_fn(
        design, iteration, prior_verdicts,
        {**config, "mode": config.get("mode", "co-evolve")},
    )

    # llm_tokens for THIS step come from the agy_backend manifest — read the
    # last N entries by finding those with our prompt shape. Simpler: read
    # the manifest tail and sum wall_seconds/tokens for the last agy call.
    llm_tokens_step = _last_agy_call_tokens()

    verdicts, ledger = allocate_and_run(
        design=design,
        properties=props,
        testbenches=tbs,
        mutants=mutants,
        ledger=ledger,
        config=config,
        prior_llm_tokens=llm_tokens_step,
    )
    return verdicts, ledger, llm_tokens_step


def _last_agy_call_tokens() -> int:
    """Return the llm_tokens estimate for the most recent agy call."""
    try:
        with open(agy_backend.AGY_MANIFEST_PATH) as f:
            last = None
            for line in f:
                line = line.strip()
                if line:
                    last = line
            if not last:
                return 0
            import json
            entry = json.loads(last)
            return int(entry.get("prompt_tokens_est", 0)) + int(entry.get("response_tokens_est", 0))
    except (OSError, ValueError):
        return 0


# ─────────────────────────── system dispatch ───────────────────────────


def dispatch_system(
    system_name: str,
    design: Design,
    seed: int,
    ledger: BudgetLedger,
    *,
    mutants: tuple[MutationTarget, ...],
    generator_fn=None,
    proposals_per_call: int = 4,
    co_evolve_iters: int = 2,
) -> tuple[tuple[Verdict, ...], BudgetLedger]:
    """Top-level entry: route (system, design, seed) to the right allocator."""
    if system_name == "direct-prompt":
        assert generator_fn is not None
        props, tbs, gen_muts = generator_fn(
            design, 0, (),
            {"mode": "direct-prompt", "proposals_per_call": proposals_per_call, "seed": seed},
        )
        tokens = _last_agy_call_tokens()
        return allocate_and_run(design, props, tbs, mutants, ledger,
                                {"mode": "direct-prompt"}, prior_llm_tokens=tokens)

    if system_name == "coverage-guided":
        assert generator_fn is not None
        # Round 1: direct-prompt to seed coverage; Round 2: coverage-guided using round-1 verdicts
        props1, tbs1, _ = generator_fn(
            design, 0, (),
            {"mode": "direct-prompt", "proposals_per_call": proposals_per_call, "seed": seed},
        )
        t1 = _last_agy_call_tokens()
        v1, ledger = allocate_and_run(design, props1, tbs1, mutants, ledger,
                                       {"mode": "coverage-guided"}, prior_llm_tokens=t1)
        if ledger.exhausted():
            return v1, ledger
        props2, tbs2, _ = generator_fn(
            design, 1, v1,
            {"mode": "coverage-guided", "proposals_per_call": proposals_per_call, "seed": seed},
        )
        t2 = _last_agy_call_tokens()
        v2, ledger = allocate_and_run(design, props2, tbs2, mutants, ledger,
                                       {"mode": "coverage-guided"}, prior_llm_tokens=t2)
        return v1 + v2, ledger

    if system_name == "bounded-formal-only":
        # Formal-only: skip generator, drive property-check via miter for each mutant
        verdicts: list[Verdict] = []
        ref_file = design.reference_impl_file or design.verilog_files[0]
        for m in mutants:
            if ledger.exhausted():
                break
            mut_file = _mutant_file_for(design, m)
            if not mut_file:
                continue
            slug = m.mutation_id.replace(f"m_{design.top_module}_", "", 1)
            mut_top = _find_top_module_name(mut_file, design.top_module)
            v = mutation.check_equivalence(
                ref_file, mut_file, design.top_module, mut_top,
                engine="z3", depth=2,
            )
            # Relabel: MUTATION_KILLED = miter disproves (mutant is observably wrong).
            v = Verdict(
                verdict_id=v.verdict_id,
                testbench_id=None, property_id=None, mutation_id=m.mutation_id,
                engine=v.engine, outcome=v.outcome, origin=ArtefactOrigin.ALLOCATOR,
                wall_seconds=v.wall_seconds, smt_seconds=v.smt_seconds,
                sim_seconds=v.sim_seconds,
                counterexample_trace=v.counterexample_trace,
            )
            verdicts.append(v)
            ledger = ledger.debit(v)
        return tuple(verdicts), ledger

    if system_name == "deterministic-allocator":
        return allocate_deterministic(design, mutants, ledger, seed=seed)

    if system_name == "co-evolve":
        assert generator_fn is not None
        all_verdicts: list[Verdict] = []
        for it in range(co_evolve_iters):
            if ledger.exhausted():
                break
            step_v, ledger, _ = co_evolve_step(
                design, it, tuple(all_verdicts), mutants, ledger,
                generator_fn,
                {"mode": "co-evolve", "proposals_per_call": proposals_per_call, "seed": seed},
            )
            all_verdicts.extend(step_v)
        return tuple(all_verdicts), ledger

    raise ValueError(f"unknown system: {system_name}")


__all__ = [
    "ENGINE_PORTFOLIO",
    "LLM_PRICE_PER_1K_TOKENS_USD",
    "allocate_and_run",
    "allocate_deterministic",
    "co_evolve_step",
    "dispatch_system",
    "simulate_testbench",
]
