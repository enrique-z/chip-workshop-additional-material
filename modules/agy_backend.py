"""agy (Antigravity CLI) subprocess wrapper — the sole LLM backend for O02.

Per HYPOTHESIS_REFINED.json §freeze_amendment_2_agy_gemini_generator_backend
(2026-08-16 Pass 2 open), the O02 generator invokes Google's gemini-3.1-pro-
preview via Enrique's Ultra-tier `agy` CLI. This module is a thin subprocess
wrapper adapted from BP-09's papers/p_judge_bp09_goodhart_breakpoint/experiments
/agy_generator.py with three O02-specific additions:

1. Prompt-hash + response-hash manifest logging — every call appends one line
   to runs/e1/agy_call_manifest.jsonl, satisfying the Pass 5 reproducibility
   audit (kill-rule LCB requires we can name the exact model + prompt for
   every generated artefact).
2. RTL/SVA-aware fence stripping — Gemini often wraps Verilog in ```verilog
   or ```systemverilog fences even when told not to; extract the inner
   payload.
3. Token accounting — agy does not emit token counts, so we estimate via
   len(stdout)/4 (Gemini's approximate BPE ratio for code) and log it into
   the scalar cost function's `llm_tokens` dimension.

Anchor-separation guarantee: this module is imported ONLY by modules/generator.py.
It has no reference to modules/anchor_audit. See modules/anchor_audit.py L1-30
for the invariant.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


_DEFAULT_MODEL: str = "gemini-3.1-pro-preview"
AGY_BIN: str = os.environ.get("O02_AGY_BIN", os.path.expanduser("~/.local/bin/agy"))
AGY_TIMEOUT_S: int = int(os.environ.get("O02_AGY_TIMEOUT_S", "180"))
AGY_INTER_CALL_SLEEP_S: float = float(os.environ.get("O02_AGY_INTER_CALL_SLEEP_S", "0.5"))
AGY_MANIFEST_PATH: Path = Path(
    os.environ.get(
        "O02_AGY_MANIFEST_PATH",
        str(Path(__file__).resolve().parents[1] / "runs" / "e1" / "agy_call_manifest.jsonl"),
    )
)
GEMINI_TOKENS_PER_CHAR: float = 0.25  # ~4 chars/token for code, matches Gemini 3.x tokenizer estimates


def _resolve_default_model() -> str:
    try:
        with open(os.path.expanduser("~/.gemini/settings.json")) as f:
            data = json.load(f)
        name = data.get("model", {}).get("name")
        if isinstance(name, str) and name:
            return name
    except (OSError, ValueError, KeyError):
        pass
    return _DEFAULT_MODEL


GENERATOR_MODEL_ID: str = os.environ.get(
    "O02_GENERATOR_MODEL_ID", _resolve_default_model()
)


@dataclass(frozen=True)
class AgyHandle:
    model_id: str
    lineage: str
    client: str


@dataclass(frozen=True)
class AgyResponse:
    """Raw wrapper output: text + accounting fields the allocator debits."""
    text: str
    wall_seconds: float
    prompt_tokens_est: int
    response_tokens_est: int
    llm_tokens: int          # sum (goes into scalar_cost_function)
    model_id: str
    prompt_sha256: str
    response_sha256: str


def load_backend() -> AgyHandle:
    if not os.path.exists(AGY_BIN):
        raise RuntimeError(
            f"agy binary not found at {AGY_BIN}; install via `agy install` or "
            f"set O02_AGY_BIN."
        )
    return AgyHandle(
        model_id=GENERATOR_MODEL_ID,
        lineage="Google/Gemini",
        client="agy",
    )


_last_call_ts: float = 0.0


def call(prompt: str, *, handle: AgyHandle | None = None) -> AgyResponse:
    """Invoke agy on `prompt`; block until output or timeout.

    Raises RuntimeError on nonzero exit or empty output — callers MUST NOT
    fabricate a fallback (anti-fabrication rule; see METHOD.md Part 2).
    """
    global _last_call_ts
    if handle is None:
        handle = load_backend()

    elapsed = time.time() - _last_call_ts
    if elapsed < AGY_INTER_CALL_SLEEP_S:
        time.sleep(AGY_INTER_CALL_SLEEP_S - elapsed)

    prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
    t0 = time.time()
    try:
        completed = subprocess.run(
            [AGY_BIN, "--print", prompt],
            capture_output=True,
            text=True,
            timeout=AGY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        wall = time.time() - t0
        _last_call_ts = time.time()
        _log_manifest({
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model_id": handle.model_id,
            "prompt_sha256": prompt_sha,
            "prompt_len": len(prompt),
            "wall_seconds": wall,
            "outcome": "timeout",
            "timeout_s": AGY_TIMEOUT_S,
        })
        raise RuntimeError(
            f"agy timed out after {AGY_TIMEOUT_S}s on prompt of length {len(prompt)}"
        ) from exc

    wall = time.time() - t0
    _last_call_ts = time.time()

    if completed.returncode != 0:
        _log_manifest({
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model_id": handle.model_id,
            "prompt_sha256": prompt_sha,
            "prompt_len": len(prompt),
            "wall_seconds": wall,
            "outcome": "nonzero_exit",
            "returncode": completed.returncode,
            "stderr_head": completed.stderr.strip()[:400],
        })
        raise RuntimeError(
            f"agy exited {completed.returncode}: {completed.stderr.strip()[:400]}"
        )

    text = completed.stdout.strip()
    if not text:
        _log_manifest({
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model_id": handle.model_id,
            "prompt_sha256": prompt_sha,
            "prompt_len": len(prompt),
            "wall_seconds": wall,
            "outcome": "empty_output",
            "stderr_head": completed.stderr.strip()[:400],
        })
        raise RuntimeError(
            f"agy returned empty output; stderr: {completed.stderr.strip()[:400]}"
        )

    response_sha = hashlib.sha256(text.encode()).hexdigest()
    prompt_toks = max(1, int(len(prompt) * GEMINI_TOKENS_PER_CHAR))
    response_toks = max(1, int(len(text) * GEMINI_TOKENS_PER_CHAR))

    _log_manifest({
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_id": handle.model_id,
        "prompt_sha256": prompt_sha,
        "prompt_len": len(prompt),
        "prompt_tokens_est": prompt_toks,
        "response_sha256": response_sha,
        "response_len": len(text),
        "response_tokens_est": response_toks,
        "wall_seconds": wall,
        "outcome": "ok",
    })

    return AgyResponse(
        text=text,
        wall_seconds=wall,
        prompt_tokens_est=prompt_toks,
        response_tokens_est=response_toks,
        llm_tokens=prompt_toks + response_toks,
        model_id=handle.model_id,
        prompt_sha256=prompt_sha,
        response_sha256=response_sha,
    )


def _log_manifest(entry: dict) -> None:
    """Append one JSONL line to AGY_MANIFEST_PATH. Never raises."""
    try:
        AGY_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(AGY_MANIFEST_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


_FENCE_LANGS = ("verilog", "systemverilog", "sv", "python", "text", "")


def strip_code_fence(text: str, *, expected_lang: str | None = None) -> str:
    """Remove one leading ```lang fence and its trailing ``` if present.

    Idempotent on already-clean text. If `expected_lang` is given and the
    fence's declared language differs, we still strip (the LLM's language
    hint is not authoritative).
    """
    t = text.strip()
    if not t.startswith("```"):
        return t
    first_nl = t.find("\n")
    if first_nl == -1:
        return t
    header = t[3:first_nl].strip().lower()
    body = t[first_nl + 1:]
    end_fence = body.rfind("```")
    if end_fence != -1:
        body = body[:end_fence]
    _ = header, expected_lang  # accepted but not enforced
    return body.strip()


__all__ = [
    "AGY_BIN",
    "AGY_TIMEOUT_S",
    "AGY_MANIFEST_PATH",
    "GENERATOR_MODEL_ID",
    "AgyHandle",
    "AgyResponse",
    "call",
    "load_backend",
    "strip_code_fence",
]


if __name__ == "__main__":
    handle = load_backend()
    print(f"agy binary:       {AGY_BIN}")
    print(f"model:            {handle.model_id}")
    print(f"lineage:          {handle.lineage}")
    print(f"manifest:         {AGY_MANIFEST_PATH}")
    print()
    print("Smoke test — proposing a trivial SVA assertion...")
    resp = call(
        "Return ONLY a one-line SystemVerilog assertion, no fences, no prose: "
        "assert that a 4-bit signal `x` never exceeds 15.",
        handle=handle,
    )
    print(f"  wall={resp.wall_seconds:.2f}s  llm_tokens={resp.llm_tokens}")
    print(f"  response:\n{resp.text}")
