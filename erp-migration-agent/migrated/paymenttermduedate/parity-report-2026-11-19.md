# Parity report: adempiere.paymenttermduedate

- Decided: 2026-11-19T18:07:06+00:00, APPROVED BY AN AGENT (owner review pending) by implementation agent
- Run: `paymenttermduedate-20261119T180705-fac3`
- Oracle: the legacy PL/pgSQL function in database `erp_migration_agent`, TimeZone America/Los_Angeles
- Cases: 41 (24 sampled from seed data, 17 model-proposed edge cases; 3 candidate cases rejected)
- Visible parity: 28/28
- **Held-out parity: 13/13** (pass bar: 100%)
- Repair iterations: 0
- Model calls: 3 (0 live, 3 from cassettes); models claude-opus-5-5; effort medium; recorded cost estimate $0.53 (Claude Code client-side estimate)

## Iterations

| Iteration | Visible matched |
|---|---|
| 0 | 28/28 |

## What this proves and does not

Parity holds on the recorded cases, including held-out cases whose golden outputs no prompt ever
contained. It is evidence of equivalence, not a proof: see docs/metric-contract.md.
