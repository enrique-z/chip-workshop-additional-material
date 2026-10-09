#!/usr/bin/env bash
# Regenerate every camera-ready re-analysis output from e1-1..e1-4/ + evidence/ (read-only).
# Usage (from anywhere): bash camera_ready/analysis/run_all.sh
set -euo pipefail
# Optional: OUT_DIR=/some/dir bash camera_ready/analysis/run_all.sh writes the
# outputs there instead of over the shipped .out files (for byte comparison).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${OUT_DIR:-$HERE}"
mkdir -p "$OUT"
for s in cr_price_ci cr_separate_budgets cr_unit_reconcile cr_kills_by_design cr_formal_breakdown; do
  echo "[run_all] $s"
  python3 -I "$HERE/$s.py" > "$OUT/$s.out"
done
echo "[run_all] done; outputs: $OUT/cr_*.out"
