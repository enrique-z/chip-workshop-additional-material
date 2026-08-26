"""Grouped-bar figure: mutation-kill per scalar-cost unit (mkc), E1 grid vs E2 pilot.

Anti-fab rule 3 compliant: NO hardcoded data arrays — every plotted value is
read at run time from the two frozen aggregates:
  evidence/o02_e1_aggregate.json        (E1: 5 systems, 150 cells)
  evidence/o02_e2_pilot_aggregate.json  (E2 pilot subset: 3 systems, 36 cells)

Output: figs/e1_e2_mkc.pdf
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
EV = HERE.parent / "evidence"

E1_KEYS = [
    ("e1.formalonly", "bounded-\nformal-only"),
    ("e1.det", "deterministic-\nallocator"),
    ("e1.direct", "direct-\nprompt"),
    ("e1.coev", "co-evolve"),
    ("e1.cov", "coverage-\nguided"),
]
E2_FOR_E1 = {"e1.det": "e2p.det", "e1.direct": "e2p.direct", "e1.coev": "e2p.coev"}


def main() -> None:
    e1 = json.loads((EV / "o02_e1_aggregate.json").read_text())
    e2 = json.loads((EV / "o02_e2_pilot_aggregate.json").read_text())

    labels = [lab for _, lab in E1_KEYS]
    e1_vals = [e1[k]["mkc"] for k, _ in E1_KEYS]
    e2_vals = [e2[E2_FOR_E1[k]]["mkc"] if k in E2_FOR_E1 else None for k, _ in E1_KEYS]

    x = range(len(labels))
    w = 0.38
    fig, ax = plt.subplots(figsize=(5.6, 2.6))
    b1 = ax.bar([i - w / 2 for i in x], e1_vals, w,
                label="E1 (RTLLM, 150 cells)", color="#4878a8", edgecolor="black", linewidth=0.4)
    xs2 = [i + w / 2 for i, v in zip(x, e2_vals) if v is not None]
    vs2 = [v for v in e2_vals if v is not None]
    b2 = ax.bar(xs2, vs2, w,
                label="E2 pilot (VerilogEval, 36 cells, pre-declared)",
                color="#c8823c", edgecolor="black", linewidth=0.4)
    for rect in list(b1) + list(b2):
        ax.annotate(f"{rect.get_height():.3f}", (rect.get_x() + rect.get_width() / 2,
                    rect.get_height()), ha="center", va="bottom", fontsize=6)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=7)
    ax.set_ylabel("mkc (kills / scalar-cost unit)", fontsize=8)
    ax.tick_params(axis="y", labelsize=7)
    ax.legend(fontsize=7, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(HERE / "e1_e2_mkc.pdf")
    print("wrote", HERE / "e1_e2_mkc.pdf")


if __name__ == "__main__":
    main()
