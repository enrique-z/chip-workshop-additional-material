# Sanitization note (2026-08-25)
Copies in this bundle are byte-identical to the repository originals EXCEPT:
- the hosted-model name in engine/backend strings is replaced by `hosted-model-redacted`
 (double-blind: vendor/subscription identity), and
- absolute build-machine paths are relativised or replaced by `[path-redacted]`.
No numeric value, verdict outcome, hash, or timestamp was altered. The unsanitized
originals remain in the (private) repository and are available to chairs on request.

## Camera-ready additions (2026-10-09)
The paper is now de-anonymized. The hosted model is `gemini-3.1-pro-preview` (Google Gemini,
via the Antigravity CLI `agy`), as stated in the camera-ready paper; `hosted-model-redacted` in
the earlier files denotes this model. The earlier files are left unchanged.
- `evidence/e1_agy_call_manifest.jsonl` (`runs/e1/agy_call_manifest.jsonl` in the private
  repository): byte-identical copy. It holds hashes, lengths, timings and outcomes only (no
  prompt or response text); it contains no absolute paths, e-mail addresses, tokens or keys.
  The `model_id` field (`gemini-3.1-pro-preview`) is kept, matching the paper.
- `evidence/kr1_seq_pre_amendment_5_backup/*.verdict.json`: absolute build-machine paths in
  `counterexample_trace` strings were handled as in the original bundle: the repository-root
  prefix was removed (72 occurrences, JC_counter and LFSR files), and 12 paths truncated
  mid-string in LFSR traces were replaced by `[path-redacted]`. The accu files needed no change.
  No other byte was altered; these files contain no model name.
- `camera_ready/analysis/`: only the path constants were adapted to this layout (verdicts read
  from `e1-1/`..`e1-4/`, sorted by file name as in the original single directory; manifest path
  above); `run_all.sh` gained an optional `OUT_DIR`. Re-running reproduces every shipped
  `cr_*.out` byte for byte.
- modules/agy_backend.py added for the camera-ready (LLM backend call, referenced by the paper); one internal note path in an error message removed, code unchanged.
