#!/usr/bin/env bash
# Serial E1 grid runner — one cell at a time via run_e1_cell.py.
#
# Serialisation is intentional: agy hosted-model backend is single-concurrency (OAuth lock
# contention), so cells that use the LLM generator (direct-prompt, coverage-
# guided, co-evolve) can't run in parallel. Rather than fight that with
# multiple job pools per system, we do everything serially and emit a
# per-cell joblog line for resume-on-failure. Rough per-cell wall-clock:
#
#   direct-prompt          : ~30–60 s
#   coverage-guided        : ~60–90 s
#   bounded-formal-only    : ~10–20 s
#   deterministic-allocator: ~5–15 s
#   co-evolve              : ~90–150 s
#
# 120-cell full grid: ~90–150 minutes wall-clock.
#
# Resume-friendly: if a slug's .verdict.json already exists AND its summary
# reports at least one verdict, we skip it. Delete the JSON to force re-run.
#
# Usage:
#   bash scripts/run_e1_grid.sh [experiments/e1_grid.jsonl [runs/e1]]

set -uo pipefail
export LC_ALL=C

source $HOME/tools/oss-cad-suite/environment 2>/dev/null || true

GRID="${1:-experiments/e1_grid.jsonl}"
RUNS_DIR="${2:-runs/e1}"
JOBLOG="${RUNS_DIR}/joblog.tsv"

mkdir -p "$RUNS_DIR"

total=$(wc -l < "$GRID" | tr -d ' ')
i=0
skipped=0
run=0
failed=0

echo -e "cell\trc\twall_s\tkilled\ttotal\tscalar_cost" > "$JOBLOG"

while IFS= read -r line; do
    i=$((i+1))
    slug=$(python3 -c "import sys, json; d=json.loads(sys.stdin.read()); print(f'{d[\"design_id\"].replace(\"::\",\"_\")}_{d[\"seed\"]}_{d[\"system\"]}')" <<<"$line")
    vpath="${RUNS_DIR}/${slug}.verdict.json"

    if [ -f "$vpath" ] && python3 -c "import json,sys; d=json.load(open('${vpath}')); sys.exit(0 if d.get('n_verdicts',0) > 0 else 1)" 2>/dev/null; then
        skipped=$((skipped+1))
        echo "[${i}/${total}] ${slug} SKIP (already ran)"
        continue
    fi

    t0=$(date +%s)
    python3 scripts/run_e1_cell.py "$line" --runs-dir "$RUNS_DIR" >/dev/null 2>&1
    rc=$?
    dt=$(($(date +%s) - t0))

    if [ -f "$vpath" ]; then
        summary=$(python3 -c "import json; d=json.load(open('${vpath}')); s=d['summary']; print(f'{s[\"n_mutants_killed\"]}\t{s[\"n_mutants_total\"]}\t{s[\"scalar_cost_used\"]:.2f}')")
        killed=$(echo "$summary" | cut -f1)
        total_muts=$(echo "$summary" | cut -f2)
        cost=$(echo "$summary" | cut -f3)
        echo -e "${slug}\t${rc}\t${dt}\t${killed}\t${total_muts}\t${cost}" >> "$JOBLOG"
        echo "[${i}/${total}] ${slug} rc=${rc} dt=${dt}s killed=${killed}/${total_muts} cost=${cost}"
        run=$((run+1))
    else
        echo -e "${slug}\t${rc}\t${dt}\t-\t-\t-" >> "$JOBLOG"
        echo "[${i}/${total}] ${slug} rc=${rc} dt=${dt}s NO_VERDICT"
        failed=$((failed+1))
    fi
done < "$GRID"

echo
echo "=== E1 GRID DONE: ${run} run, ${skipped} skipped, ${failed} no-verdict, ${total} total ==="
echo "joblog: ${JOBLOG}"
