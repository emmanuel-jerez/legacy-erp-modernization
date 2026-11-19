# Eval report: legacy-to-Python migration

- Generated: 2026-11-19T18:04:44+00:00 (mode: replay)
- Model: claude-opus-5-5; effort: medium
- Oracle: legacy PL/pgSQL functions in `erp_migration_agent`, TimeZone America/Los_Angeles
- Pass bar: 100% held-out parity (docs/metric-contract.md)

| Function | Difficulty | Status | Held-out parity | Visible parity | Repairs | Cases (sampled + edge) | Model calls |
|---|---|---|---|---|---|---|---|
| `currencyround` | warm-up | converged | 14/14 (100%) | 28/28 (100%) | 0 | 42 (24 + 18) | 3 |
| `paymenttermduedate` | branching | converged | 13/13 (100%) | 28/28 (100%) | 0 | 41 (24 + 17) | 3 |
| `paymenttermdiscount` | multi-table | converged | 14/14 (100%) | 29/29 (100%) | 0 | 43 (24 + 19) | 3 |

Totals: 9 model calls (0 live, 9 from cassettes), recorded model time 423 s, recorded cost estimate $1.59 (Claude Code client-side estimate at record time).

## `currencyround` (warm-up)

One table read, NULL pass-through, a costing flag, and PostgreSQL rounding (half away from zero, fixed scale).

- Cases: 42 (24 sampled, 18 edge; 28 visible, 14 held out); 2 candidate cases rejected
- Visible parity by iteration: 0: 28/28
- Model calls: 3 (0 live, 3 cassette); model claude-opus-5-5, effort medium; recorded cost estimate $0.30; tokens as recorded: cache_creation_input_tokens 17189, cache_read_input_tokens 0, input_tokens 6, output_tokens 8222

## `paymenttermduedate` (branching)

Fixed-day versus net-days branches, a month cutoff, month arithmetic and day truncation in the session time zone.

- Cases: 41 (24 sampled, 17 edge; 28 visible, 13 held out); 3 candidate cases rejected
- Visible parity by iteration: 0: 28/28
- Model calls: 3 (0 live, 3 cassette); model claude-opus-5-5, effort medium; recorded cost estimate $0.53; tokens as recorded: cache_creation_input_tokens 26377, cache_read_input_tokens 2661, input_tokens 6, output_tokens 15692

## `paymenttermdiscount` (multi-table)

Reads payment terms, currencies, client accounting schemas and holidays (through nextbusinessday); discount windows, grace days and rounding.

- Cases: 43 (24 sampled, 19 edge; 29 visible, 14 held out); 1 candidate cases rejected
- Visible parity by iteration: 0: 29/29
- Model calls: 3 (0 live, 3 cassette); model claude-opus-5-5, effort medium; recorded cost estimate $0.76; tokens as recorded: cache_creation_input_tokens 37873, cache_read_input_tokens 2661, input_tokens 6, output_tokens 22473
