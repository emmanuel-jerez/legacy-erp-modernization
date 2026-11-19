# Medium versus high effort

Measured 2026-11-19. All three evals ran again with `claude-opus-5-5` at **high** effort
(`ERP_LLM_EFFORT=high`), and the results are shown here next to the committed
medium-effort results.

## What differs between the two runs

- **Only the effort setting changed.** The code, prompts, eval sets, guardrails, seed data
  and the model are the same. No code changed for this comparison.
- **Cassettes are keyed by effort.** The high-effort runs recorded new cassettes, and the
  medium-effort cassettes and reports stay as they were. Each project now has a second set
  of reports with `-high` in the name.
- **Each eval ran once at high effort.** A model answer can differ between two runs even
  at the same effort, so one flipped case is evidence of variance, not of a trend. The
  medium results are also a single run.
- **Cost figures are Claude Code's client-side estimates**, stored with each cassette when
  it was recorded. Model time is the sum of the recorded call durations, not wall-clock
  time (live calls ran up to 3 at a time).
- **The schema and migration reports are offline replays** of the recorded high-effort
  runs, the same way the medium reports were made. Each replay gave the same scores as its
  record run. The UI operator has both a record report and a replay report at each effort,
  because that eval always drives the live web UI.

## Summary

| | Medium | High |
|---|---|---|
| Schema agent: golden accuracy | 36 / 38 | 36 / 38 (one case fixed, one new miss) |
| Schema agent: adversarial prompts handled safely | 10 / 10 | 10 / 10 |
| Schema agent: SQL attacks blocked without the model | 18 / 18 | 18 / 18 |
| Migration agent: held-out parity, all 3 functions | 41 / 41, no repairs | 41 / 41, no repairs |
| UI operator: draft orders verified in the database | 6 / 6 | 6 / 6 |
| Model calls, all three evals | 102 | 102 |
| Recorded model time, all three evals | 1,088 s | 1,315 s (+21%) |
| Recorded cost estimate, all three evals | $6.01 | $6.64 (+10%) |

At high effort, the accuracy, parity and success results were the same, and model time and
cost went up. On these eval sets, high effort gave no measurable improvement over medium.

## Schema agent

Reports: [medium](erp-schema-agent/evals/reports/eval-report.md),
[high](erp-schema-agent/evals/reports/eval-report-high.md).

| Metric | Medium | High |
|---|---|---|
| Golden execution accuracy | 36 / 38 (94.7%) | 36 / 38 (94.7%) |
| Adversarial prompts passed | 10 / 10 | 10 / 10 |
| Adversarial prompts blocked, by layer | intent screen 3, model 6 | intent screen 3, model 6 |
| SQL attacks stopped (no model) | 18 / 18 | 18 / 18 |
| Answers with every figure grounded | 37 / 38; 1 of 373 figures ungrounded | 37 / 38; 2 of 381 figures ungrounded |
| Runs that used the repair loop | 0 | 0 |
| Retrieval recall | 100% | 100% |
| Model calls | 84 | 84 |
| Calls made live for this comparison | 0 (medium cassettes reused) | 84 |
| Output tokens as recorded | 62,101 | 66,598 (+7%) |
| Recorded model time | 626 s | 661 s (+6%) |
| Recorded cost estimate | $4.19 | $4.34 (+4%) |

**Golden questions that flipped:**

- `top_product_california_2026` **failed at medium, passed at high.** At medium, the model
  chose to return the top 3 products "so the leader and how close the runners-up are can
  both be seen", which gave extra rows. At high, it returned only the top product, with
  ties, as the question asked.
- `vendor_lead_time` **passed at medium, failed at high.** At high, the model rounded the
  average lead time to 1 decimal place (33.1) instead of 2 (33.05). The rows and vendors
  were correct, but the scorer compares values at the reference precision, so the column
  did not match.
- `vitamin_c_prices` **failed at both efforts** in the same way. Both queries left out the
  2-pack, but they also returned the 2 oz single (`FH-004-2`), so the answer had an extra
  row.

**Grounding:** the ungrounded figure changed. At medium, a "6 states" count in
`revenue_by_state_month_q3` was not found in the rows. At high, that answer was grounded,
but `revenue_by_month` gave approximate bounds ("about 70,000 to 82,000 a month from May")
that the grounding check correctly did not match to any cell.

The SQL attack results do not use the model, so they are the same by design. The guardrail
cases were blocked by the same layers at both efforts.

## Migration agent

Reports: [medium](erp-migration-agent/evals/reports/eval-report.md),
[high](erp-migration-agent/evals/reports/eval-report-high.md). Audit logs: `audit/` and
`audit-high/` in the same folder.

| Function | Medium: held-out / visible parity | High: held-out / visible parity | Repairs (medium / high) |
|---|---|---|---|
| `currencyround` | 14 / 14, 28 / 28 | 14 / 14, 30 / 30 | 0 / 0 |
| `paymenttermduedate` | 13 / 13, 28 / 28 | 14 / 14, 29 / 29 | 0 / 0 |
| `paymenttermdiscount` | 14 / 14, 29 / 29 | 13 / 13, 28 / 28 | 0 / 0 |

| Metric | Medium | High |
|---|---|---|
| Functions converged on the first port | 3 / 3 | 3 / 3 |
| Guard rejections or runner failures | 0 | 0 |
| Candidate cases rejected by the oracle | 6 (2, 3, 1) | 3 (0, 1, 2) |
| Model calls | 9 | 9 |
| Calls made live for this comparison | 0 (medium cassettes reused) | 9 |
| Output tokens as recorded | 46,387 | 66,502 (+43%) |
| Recorded model time | 423 s | 603 s (+43%) |
| Recorded cost estimate | $1.59 ($0.30, $0.53, $0.76) | $2.05 ($0.40, $0.65, $1.00) (+29%) |

The case counts differ because the model proposes the edge cases. About a third of all
cases are held out, by a fixed function of the case ids, so 13 / 13 and 14 / 14 are both full parity on different
case sets, not a miss. Both efforts met the 100% held-out pass bar for every function
without a repair. High effort used about 43% more output tokens and model time for the
same outcome.

## UI operator

Reports: medium [record](erp-ui-operator/evals/reports/eval-2026-11-19-record.md) and
[replay](erp-ui-operator/evals/reports/eval-2026-11-19-replay.md); high
[record](erp-ui-operator/evals/reports/eval-2026-11-19-record-high.md) and
[replay](erp-ui-operator/evals/reports/eval-2026-11-19-replay-high.md).

| Metric | Medium | High |
|---|---|---|
| Draft orders verified in the database | 6 / 6 | 6 / 6 |
| Clean selector map / drifted selector map | 3 / 3, 3 / 3 | 3 / 3, 3 / 3 |
| Model fallback calls (all on drifted runs) | 9 | 9 |
| Guardrail blocks in the task matrix | 0 | 0 |
| Guardrail probe ("Delete Record" proposal) | Blocked, no order created | Blocked, no order created |
| Database read-back repairs | 0 | 0 |
| Steps run | 140 | 140 |
| Live calls in the record report | 6 (3 from cassettes) | 9 |
| Recorded model time | 38.5 s | 51.2 s (+33%) |
| Recorded cost estimate | $0.23 | $0.25 (+6%) |
| Run duration, replay (total) | 185.2 s | 187.6 s |

At both efforts, the model made the same 9 proposals: the same action on the same element,
with the same value, for every drifted step (`po.vendor`, `line.qty`, `po.save`). Only the
written reasons differ. Each proposal took longer at high effort, so the record run was
slower (245.3 s against 219.3 s in total). The replay runs, which use no model calls, took
about the same time.

## Reproduce

From each project folder, after `uv sync`:

```sh
ERP_LLM_EFFORT=high uv run schema-agent eval            # offline replay
ERP_LLM_EFFORT=high uv run migration-agent eval         # offline replay
ERP_LLM_EFFORT=high uv run ui-operator eval --llm-mode replay   # drives the live web UI
```

Each command writes to the project's default report name. To keep the committed
medium-effort reports, rename the new output, then restore the medium files with
`git checkout`.
