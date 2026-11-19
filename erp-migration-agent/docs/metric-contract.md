# Metric contract

Written before the implementation; the code in `src/erp_migration_agent/` must stay faithful to it.
If the two disagree, the code is wrong or this document is updated first, in the same change.

## What is being measured

A **port** is a Python module that replaces one legacy PL/pgSQL function from iDempiere's
`adempiere` schema. The **oracle** is the legacy function itself, called in PostgreSQL. The
port is accepted only when it reproduces the oracle's output on every case, including the
cases the model never saw.

## The oracle session

Golden outputs depend on the database session, so every oracle call runs with:

- database `erp_migration_agent` (this repo's clone of the frozen seed), as the ERP owner;
- `TimeZone = 'America/Los_Angeles'`, pinned explicitly. It is the cluster default observed on
  2026-11-19, and it matters: iDempiere's `adddays` adds `86400 * n` seconds, so legacy dates
  drift by an hour across daylight-saving changes;
- `search_path = adempiere, pg_catalog`.

The session time zone is part of the port's input: the data-access interface exposes it as
`data.session_tz`.

## Cases

A case is one call: positional arguments, optional fixture rows, and the golden result.

**Arguments** are normalized by PostgreSQL before anything else. Each raw argument is cast to
the parameter's declared type (`%s::numeric`, `%s::timestamptz`, …) in the oracle session, and
the value PostgreSQL returns is what both the oracle and the port receive. A case whose
arguments PostgreSQL rejects is dropped and logged.

**Two sources**, recorded per case as `source`:

1. `sampled`: deterministic SQL over real seeded rows (payment terms, currencies, order totals,
   order dates), ordered by a hash of the row id so the sample is stable and spread out. The
   sampling queries live in `targets.py`.
2. `edge`: cases the model proposes from its spec: boundaries, NULLs, unusual dates, and
   synthetic configuration rows (**fixtures**) the seed lacks, such as a fixed-due-date payment
   term or a 2026 holiday.

**Fixtures** are rows inserted into the function's dependency tables inside a transaction that
is always rolled back. They are cloned from an existing row, take a primary key in the reserved
range 990000–999999, and get harness-assigned values for unique columns (`*_uu`, `name`,
`value`, `iso_code`). Only tables the legacy source reads accept fixtures. The rows read back
inside that transaction are given to the port, so the port sees exactly what the oracle saw.

**Size.** 30–50 cases per function: up to 24 sampled and up to 20 edge. A case is a duplicate
when its normalized arguments and the fixture rows actually inserted (as read back) match an
earlier case; duplicates are removed. If validation leaves fewer than 30 cases, the run goes on
and the stage output and audit log flag it.

**Split.** Within each source, cases are ordered by `sha256(case_id)`, and every third case is
**held out**. Roughly two thirds are **visible**. The split is a pure function of the case ids, so
it is reproducible. The model sees visible cases (inputs and golden outputs) when it writes and
repairs the port. It never sees a held-out golden output, and held-out cases are never in a
port or repair prompt. A repair prompt lists up to 12 visible mismatches (inputs, expected,
actual, reason) and states the total count. Held-out edge cases were proposed by the model as inputs during
characterization; their outputs are recorded afterwards and never sent back.

## Parity

A case **matches** when the port's result equals the golden result under these rules:

| Legacy type | Port must return | Equal when |
|---|---|---|
| `numeric` | `decimal.Decimal` | The PostgreSQL text form matches exactly: same value **and** same scale (`2.20` ≠ `2.2`). A negative zero (`-0.00`) never matches, because PostgreSQL numeric has none. Special values match by text: `NaN`, `Infinity`, `-Infinity` (Python's `-NaN` and `sNaN` never match). |
| `timestamp with time zone` | timezone-aware `datetime` | Same instant (compared in UTC). A naive datetime never matches. |
| `timestamp without time zone` | naive `datetime` | Same wall-clock value. |
| `date` | `datetime.date` (not a `datetime`) | Same date. |
| `integer` / `bigint` | `int` (not `bool`) | Same value. |
| `character varying` / `text` / `character` | `str` | Same string. |
| `boolean` | `bool` | Same value. |
| any, golden `NULL` | `None` | Only `None` matches `NULL`, and `None` matches only `NULL`. |
| any, oracle raised | an exception | The port raises any exception. The SQLSTATE is recorded but not compared. |

Types are checked by identity with the real classes (`type(value) is decimal.Decimal`), so a
port-made class or subclass never passes, whatever it calls itself.

Only legacy errors are pinned: SQLSTATE classes 22 (data exception), 21 (cardinality) and P0
(PL/pgSQL `RAISE`). Any other failure of the oracle call, such as a statement timeout, a write
attempted by the function (the call always runs read-only), or a client-side decoding error,
rejects the case instead of recording it as expected behaviour.

A port that raises where the oracle returned a value, returns the wrong Python type, times out,
crashes, or fails the import guard is a mismatch on every affected case. A port that never ran
on a case does not count as "raising", so it fails even where the oracle raised.

**Parity rate** = matched cases / cases, per set (visible, held-out).

## Pass bar

A port is **accepted** only when all of these hold:

1. The source passes the import guard (allowlisted imports, no dynamic code, no dunder access).
2. Visible parity is 100%, within at most 3 repair iterations after the first port.
3. Held-out parity is **100%**.

Anything less is reported as **not converged**, with the failing cases and the last diagnosis.
An accepted port still needs an `approve` decision before it is written to `migrated/<function>/`.
A person approves with `--by <name>`; an automated agent must add `--agent`, and the auto-approve
flag is labelled as such. Both non-human decisions are labelled in every record.

## What this does not prove

- Parity is over the recorded cases, not all inputs. The held-out set estimates
  generalization; it is not a proof of equivalence.
- Only the pinned session time zone is covered. A different `TimeZone` changes legacy results.
- Fixture rows bypass deferred foreign-key checks because the transaction never commits.
