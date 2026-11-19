# Delegated review of the three migrated functions

- **Date:** 2026-11-19
- **Reviewer:** Claude (Opus 5.5), an AI agent. The owner, Emmanuel Jerez, delegated this
  review and asked for it to be labelled as an agent review. No person checked these ports.
- **Scope:** the accepted ports in [`migrated/`](../migrated): `currencyround`,
  `paymenttermduedate` and `paymenttermdiscount`.
- **Verdict:** all three accepted. This record closes the "owner review pending" note on the
  build agent's approvals through delegation. The tool-generated `approval.json` files and the
  parity reports are unchanged: they record what the build agent decided and when.

## What was checked

1. **Source against port.** I read each legacy PL/pgSQL function (`pg_get_functiondef` in the
   `erp_migration_agent` database), with its helpers `nextbusinessday` and `adddays`, against
   its Python port. I compared branch by branch and checked NULL handling, rounding and numeric scale.
2. **Pinned cases.** `uv run pytest migrated` passed 252 tests. These pin every golden case to
   the port, and to the legacy function while the database is up.
3. **Fresh differential check.** [`differential_review.py`](differential_review.py) generates
   inputs that are not in the golden cases. Each input runs through the legacy function (the oracle)
   and through the port, and the results are compared with the repository's own parity rules
   (`pgtypes.compare`). The inputs cover:
   - random amounts at scales 0–8, plus rounding boundaries such as `0.005`, `-0`, `1e-10` and `999999999.995`;
   - every currency and payment term in the database, plus NULL and unknown IDs;
   - timestamps across 2023–2026, with extra weight around daylight-saving changes;
   - pay dates from 3 days before to 45 days after the document date.

## Results of the fresh differential check

| Function | Seed 7 | Seed 11 | Mismatches |
|---|---|---|---|
| `currencyround` | 400 / 400 | 400 / 400 | 0 |
| `paymenttermduedate` | 400 / 400 | 400 / 400 | 0 |
| `paymenttermdiscount` | 400 / 400 | 400 / 400 | 0 |

Raw output: [seed 7](differential-review-2026-11-19-seed7.json),
[seed 11](differential-review-2026-11-19-seed11.json).

Reproduce from the `erp-migration-agent` folder, with the stack up:

```sh
uv run python -I reviews/differential_review.py "$PWD/migrated" 400 7
```

## Observations, none blocking

- **Daylight-saving quirk reproduced on purpose.** In `paymenttermduedate`, legacy `adddays`
  adds 86,400 seconds per day in Los Angeles time, so a due date that crosses a daylight-saving
  change lands an hour off midnight. The port keeps this, and the random dates around those
  changes confirmed it. A real migration would raise this with the business before keeping or fixing it.
- **Float arithmetic.** The `paymenttermduedate` port converts the day count through a
  Python `float`. That is exact for whole and half days, which is what the payment terms hold.
  Very long fractional day counts could round differently from PostgreSQL. No seeded or random
  case showed this.
- **Real data only.** The differential check used only real rows. Fixture rows (synthetic
  payment terms and currencies) remain covered only by the golden cases.
- **Session time zone.** Parity holds for the pinned time zone, America/Los_Angeles, as the
  metric contract states.
