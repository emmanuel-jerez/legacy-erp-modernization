# Parity report: adempiere.paymenttermdiscount

- Decided: 2026-11-19T18:07:07+00:00, APPROVED BY AN AGENT (owner review pending) by implementation agent
- Run: `paymenttermdiscount-20261119T180706-c8b7`
- Oracle: the legacy PL/pgSQL function in database `erp_migration_agent`, TimeZone America/Los_Angeles
- Cases: 43 (24 sampled from seed data, 19 model-proposed edge cases; 1 candidate cases rejected)
- Visible parity: 29/29
- **Held-out parity: 14/14** (pass bar: 100%)
- Repair iterations: 0
- Model calls: 3 (0 live, 3 from cassettes); models claude-opus-5-5; effort medium; recorded cost estimate $0.76 (Claude Code client-side estimate)

## Iterations

| Iteration | Visible matched |
|---|---|
| 0 | 29/29 |

## What this proves and does not

Parity holds on the recorded cases, including held-out cases whose golden outputs no prompt ever
contained. It is evidence of equivalence, not a proof: see docs/metric-contract.md.
