# Financer Budget Tracker

A local Python ledger for income, expenses, transfers, credit-card payments, savings goals and monthly budgets. It stores money as integer centavos in SQLite and preserves corrections through reversals and an audit log.

## Requirements

Python 3.10 or newer. No third-party Python dependencies are required. The shell launcher uses `.venv/bin/python` when available, otherwise `python3`.

## Getting started

```bash
./financer init
./financer --help
./financer summary
./financer doctor
```

Initialization creates a **new local database** at `data/financer.sqlite3`. This repository includes no personal ledger or backups. Seeded accounts have generic names; most balances remain unknown until explicitly supplied. The receivables account starts at zero. Default currency is PHP and the timezone is Asia/Manila.

## Example usage

All amounts and transactions below are fictional. Configure a plan appropriate to your own situation.

```bash
./financer budget-set --help
./financer account-set --help
./financer income 1000 "Example freelance payment" --account cash-on-hand
./financer expense 100 "Example lunch" --account cash-on-hand --bucket needs --tags food,meal
./financer recent
./financer budget
```

Expenses use `needs`, `apartment`, `wants` or `buffer`. Apartment covers rent/utilities. Savings allocations are transfers, not expenses. Salary entries can create linked automatic savings transfers with `income --salary`. Unused monthly allowances pool into the next month's buffer, with overspending handled before carryover. Savings and wants categories, savings goals and transaction comments are supported; run `./financer --help` for commands.

## Backups and daily summaries

```bash
./financer backup
./scripts/daily_backup.sh
python3 scripts/daily_summary.py
```

The backup script resolves the project directory from its own location and retains 31 days of automated daily snapshots. Manual backups are kept separately. Schedule it with your local scheduler if desired. Neither backups nor logs belong in Git.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use fictional fixtures and temporary databases.

## Privacy

Databases, SQLite sidecars, backups, logs, local credentials and Python caches are ignored by Git. Review staged files before pushing; ignore rules do not remove files already tracked. Keep real financial records on your own machine. This public source distribution starts with fresh history and contains no history from the private workspace.
