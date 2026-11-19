# Parity report: adempiere.currencyround

- Decided: 2026-11-19T18:07:05+00:00, APPROVED BY AN AGENT (owner review pending) by implementation agent
- Run: `currencyround-20261119T180704-c4ed`
- Oracle: the legacy PL/pgSQL function in database `erp_migration_agent`, TimeZone America/Los_Angeles
- Cases: 42 (24 sampled from seed data, 18 model-proposed edge cases; 2 candidate cases rejected)
- Visible parity: 28/28
- **Held-out parity: 14/14** (pass bar: 100%)
- Repair iterations: 0
- Model calls: 3 (0 live, 3 from cassettes); models claude-opus-5-5; effort medium; recorded cost estimate $0.30 (Claude Code client-side estimate)

## Iterations

| Iteration | Visible matched |
|---|---|
| 0 | 28/28 |

## What this proves and does not

Parity holds on the recorded cases, including held-out cases whose golden outputs no prompt ever
contained. It is evidence of equivalence, not a proof: see docs/metric-contract.md.
