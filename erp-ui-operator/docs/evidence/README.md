# Evidence bundle: drifted 3-line run

One run from the replay eval on 2026-11-19 (`restock-3-lines`, drifted selector map,
vendor "Placeholder Co-Packer C"). Trimmed to stay small:

- `run.json`: every step with its layer (deterministic or LLM fallback), postcondition,
  fallback proposals and guardrail verdicts, healed selectors, usage and the database
  verification result.
- `prompts/`: the exact fallback prompts (normalized snapshots; the run id appears as `<run>`).
- `healed-selectors.json`: the selector-map entries the run repaired.
- `screens/`: screenshots after the fallback steps and each save. The full run also had a
  screenshot per step and a Playwright trace (18 MB), which stay in the gitignored `runs/`.

The purchase order it created was a draft and was deleted after the eval.
