# Eval report: legacy-to-Python migration

- Generated: 2026-11-19T19:56:45+00:00 (mode: replay)
- Model: claude-opus-5-5; effort: high
- Oracle: legacy PL/pgSQL functions in `erp_migration_agent`, TimeZone America/Los_Angeles
- Pass bar: 100% held-out parity (docs/metric-contract.md)

| Function | Difficulty | Status | Held-out parity | Visible parity | Repairs | Cases (sampled + edge) | Model calls |
|---|---|---|---|---|---|---|---|
| `currencyround` | warm-up | converged | 14/14 (100%) | 30/30 (100%) | 0 | 44 (24 + 20) | 3 |
| `paymenttermduedate` | branching | converged | 14/14 (100%) | 29/29 (100%) | 0 | 43 (24 + 19) | 3 |
| `paymenttermdiscount` | multi-table | converged | 13/13 (100%) | 28/28 (100%) | 0 | 41 (24 + 17) | 3 |

Totals: 9 model calls (0 live, 9 from cassettes), recorded model time 603 s, recorded cost estimate $2.05 (Claude Code client-side estimate at record time).

## `currencyround` (warm-up)

One table read, NULL pass-through, a costing flag, and PostgreSQL rounding (half away from zero, fixed scale).

- Cases: 44 (24 sampled, 20 edge; 30 visible, 14 held out); 0 candidate cases rejected
- Visible parity by iteration: 0: 30/30
- Model calls: 3 (0 live, 3 cassette); model claude-opus-5-5, effort high; recorded cost estimate $0.40; tokens as recorded: cache_creation_input_tokens 20127, cache_read_input_tokens 0, input_tokens 6, output_tokens 11596

## `paymenttermduedate` (branching)

Fixed-day versus net-days branches, a month cutoff, month arithmetic and day truncation in the session time zone.

- Cases: 43 (24 sampled, 19 edge; 29 visible, 14 held out); 1 candidate cases rejected
- Visible parity by iteration: 0: 29/29
- Model calls: 3 (0 live, 3 cassette); model claude-opus-5-5, effort high; recorded cost estimate $0.65; tokens as recorded: cache_creation_input_tokens 27659, cache_read_input_tokens 1488, input_tokens 6, output_tokens 21435

## `paymenttermdiscount` (multi-table)

Reads payment terms, currencies, client accounting schemas and holidays (through nextbusinessday); discount windows, grace days and rounding.

- Cases: 41 (24 sampled, 17 edge; 28 visible, 13 held out); 2 candidate cases rejected
- Visible parity by iteration: 0: 28/28
- Model calls: 3 (0 live, 3 cassette); model claude-opus-5-5, effort high; recorded cost estimate $1.00; tokens as recorded: cache_creation_input_tokens 41064, cache_read_input_tokens 1488, input_tokens 6, output_tokens 33471
