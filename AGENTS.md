# Contributor instructions

This repository contains reusable ledger software. Keep real financial records, personal configuration, credentials, databases, backups and logs outside Git.

Use fictional data in documentation and tests. Store money as integer minor units. Preserve the audit trail and reversal-based corrections; transfers and card payments must not be counted as income or duplicated expenses.

Run `python3 -m unittest discover -s tests -v` after changing ledger behavior. Tests use temporary databases. Use a disposable database with `--db` for manual checks.

Do not run commands against a user's real ledger or publish financial data without their explicit authorization.
