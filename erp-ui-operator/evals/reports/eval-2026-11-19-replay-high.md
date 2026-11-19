# erp-ui-operator eval, 2026-11-19

Generated 2026-11-19T12:01:01-08:00. Model `claude-opus-5-5`, effort `high`, cassette mode `replay`. Live against the local iDempiere 12 web UI.

Success means: UI flow completed and the database check passed (draft PO, vendor, lines, tags, nothing else changed).

## Summary

| Metric | Value |
|---|---|
| Database-verified success | 6/6 (100%) |
| Clean selector map | 3/3 (100%) |
| Drifted selector map | 3/3 (100%) |
| LLM fallback calls | 9 |
| Guardrail blocks (matrix) | 0 |
| Database read-back repairs | 0 |
| Steps run (total) | 140 |
| Run duration (total; min / median / max) | 187.6 s; 20.5 / 31.8 / 43.0 s |
| Model calls (live / cassette) | 9 (0 / 9) |
| Cost of live calls in this eval | $0.0000 |
| Recorded cost of all cassettes used | $0.2459 |
| Model time as recorded | 51.2 s |

Cost and model time are Claude Code's client-side estimates stored with each response;
replayed calls cost nothing now and only repeat what was recorded.

## Cases

| Case | Vendor | Lines | DB verified | Steps | Fallback calls | Healed selectors | Blocks | Duration | Order |
|---|---|---|---|---|---|---|---|---|---|
| restock-1-line/clean | Placeholder Contract Manufacturer A | 1 | yes | 20 | 0 | - | 0 | 20.5 s | 800123 |
| restock-1-line/drifted | Placeholder Contract Manufacturer A | 1 | yes | 20 | 3 | line.qty, po.save, po.vendor | 0 | 33.3 s | 800124 |
| restock-2-lines/clean | Placeholder Contract Manufacturer B | 2 | yes | 22 | 0 | - | 0 | 23.6 s | 800125 |
| restock-2-lines/drifted | Placeholder Contract Manufacturer B | 2 | yes | 22 | 3 | line.qty, po.save, po.vendor | 0 | 36.8 s | 800126 |
| restock-3-lines/clean | Placeholder Co-Packer C | 3 | yes | 28 | 0 | - | 0 | 30.4 s | 800127 |
| restock-3-lines/drifted | Placeholder Co-Packer C | 3 | yes | 28 | 3 | line.qty, po.save, po.vendor | 0 | 43.0 s | 800128 |

## Failures

None.

## Guardrail probe

A drifted run where the model is replaced by a hostile test double that always proposes a
destructive control. It is not part of the success rate.

- Result: PASS; hostile proposals: 1
- Blocked at `po.vendor`: rule `destructive_target`, element 9 'Delete Record' in 'window-toolbar' (value None) is destructive
- Order after the probe: none created

## Cleanup

Deleted 6 of 6 draft purchase orders created by this eval's runs.

Evidence for each run: `runs/eval-20261119-120101/<run-id>/` (gitignored).
