#!/usr/bin/env python3
"""Local, auditable ledger for the Financer workspace."""

from __future__ import annotations

import argparse
import calendar
import json
import re
import sqlite3
import sys
import unicodedata
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_ROOT / "data" / "financer.sqlite3"
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"
LOCAL_TZ = ZoneInfo("Asia/Manila")
DEFAULT_CURRENCY = "PHP"
SCHEMA_VERSION = "7"


def now_iso() -> str:
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def occurred_iso(value: str | None) -> str:
    if not value:
        return now_iso()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            # Date-only: use the current time on that day, not a hardcoded noon.
            now = datetime.now(LOCAL_TZ)
            parsed = datetime.combine(date.fromisoformat(value), now.time(), LOCAL_TZ)
        else:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed.isoformat(timespec="seconds")
    except ValueError as exc:
        raise ValueError("Date must be YYYY-MM-DD or an ISO-8601 timestamp.") from exc


def parse_minor(value: str | int | Decimal, *, allow_zero: bool = False, allow_negative: bool = False) -> int:
    raw = str(value).strip().replace(",", "")
    raw = raw.replace("₱", "")
    raw = re.sub(r"(?i)^PHP\s*", "", raw)
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid monetary amount: {value!r}") from exc
    if amount < 0 and not allow_negative:
        qualifier = "zero or greater" if allow_zero else "greater than zero"
        raise ValueError(f"Amount must be {qualifier}.")
    if amount == 0 and not allow_zero:
        raise ValueError("Amount must be greater than zero.")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError("Amounts may have at most two decimal places.")
    return int(cents)


def parse_basis_points(value: str | int | Decimal) -> int:
    raw = str(value).strip().removesuffix("%").strip()
    try:
        percent = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid percentage: {value!r}") from exc
    if percent < 0 or percent > 100:
        raise ValueError("Percentage must be between 0 and 100.")
    basis_points = percent * 100
    if basis_points != basis_points.to_integral_value():
        raise ValueError("Percentages may have at most two decimal places.")
    return int(basis_points)


def percentage_text(basis_points: int) -> str:
    whole, fractional = divmod(basis_points, 100)
    return f"{whole}%" if fractional == 0 else f"{whole}.{fractional:02d}%"


def proportional_minor(amount_minor: int, basis_points: int) -> int:
    return (amount_minor * basis_points + 5000) // 10000


def format_money(minor: int | None, currency: str = DEFAULT_CURRENCY) -> str | None:
    if minor is None:
        return None
    sign = "-" if minor < 0 else ""
    absolute = abs(minor)
    symbol = "₱" if currency == "PHP" else f"{currency} "
    return f"{sign}{symbol}{absolute // 100:,}.{absolute % 100:02d}"


def format_signed_money(minor: int | None, currency: str = DEFAULT_CURRENCY) -> str | None:
    if minor is None:
        return None
    prefix = "+" if minor > 0 else ""
    return f"{prefix}{format_money(minor, currency)}"


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE)
    normalized = re.sub(r"[-_]+", "-", normalized).strip("-")
    if not normalized:
        raise ValueError("Name cannot be converted to a usable slug.")
    return normalized


def parse_tags(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    raw_items = [value] if isinstance(value, str) else list(value)
    result: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        for item in str(raw_item).split(","):
            display = item.strip()
            if not display:
                continue
            tag_slug = slugify(display)
            if tag_slug not in seen:
                seen.add(tag_slug)
                result.append(display)
    return result


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def audit(
    conn: sqlite3.Connection,
    action: str,
    entity_type: str,
    entity_id: str | int | None,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log(action, entity_type, entity_id, details_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (action, entity_type, None if entity_id is None else str(entity_id),
         json.dumps(details or {}, ensure_ascii=False, sort_keys=True), now_iso()),
    )


def migrate_budget_buckets(conn: sqlite3.Connection) -> None:
    transaction_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transactions'"
    ).fetchone()
    rules_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'classification_rules'"
    ).fetchone()
    migrate_transactions = (
        transaction_sql_row is not None and "'buffer'" not in transaction_sql_row["sql"]
    )
    migrate_rules = rules_sql_row is not None and "'buffer'" not in rules_sql_row["sql"]
    if not migrate_transactions and not migrate_rules:
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            if migrate_transactions:
                conn.execute(
                    """
                    CREATE TABLE transactions_v7 (
                        id INTEGER PRIMARY KEY,
                        public_id TEXT NOT NULL UNIQUE,
                        occurred_at TEXT NOT NULL,
                        transaction_type TEXT NOT NULL CHECK (
                            transaction_type IN (
                                'expense', 'income', 'transfer', 'card_payment',
                                'refund', 'adjustment', 'reversal'
                            )
                        ),
                        amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                        currency TEXT NOT NULL CHECK (length(currency) = 3),
                        description TEXT NOT NULL,
                        counterparty TEXT,
                        bucket TEXT CHECK (
                            bucket IS NULL OR bucket IN (
                                'needs', 'apartment', 'wants', 'buffer', 'savings'
                            )
                        ),
                        original_message TEXT,
                        status TEXT NOT NULL DEFAULT 'posted'
                            CHECK (status IN ('posted', 'reversed')),
                        related_transaction_id INTEGER REFERENCES transactions_v7(id),
                        reversal_of_id INTEGER UNIQUE REFERENCES transactions_v7(id),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO transactions_v7
                    SELECT * FROM transactions
                    """
                )
                conn.execute("DROP TABLE transactions")
                conn.execute("ALTER TABLE transactions_v7 RENAME TO transactions")

            if migrate_rules:
                conn.execute(
                    """
                    CREATE TABLE classification_rules_v7 (
                        id INTEGER PRIMARY KEY,
                        pattern TEXT NOT NULL COLLATE NOCASE,
                        match_type TEXT NOT NULL DEFAULT 'contains'
                            CHECK (match_type IN ('contains', 'exact', 'word')),
                        bucket TEXT NOT NULL
                            CHECK (bucket IN ('needs', 'apartment', 'wants', 'buffer', 'savings')),
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        rationale TEXT NOT NULL,
                        user_confirmed INTEGER NOT NULL DEFAULT 0
                            CHECK (user_confirmed IN (0, 1)),
                        priority INTEGER NOT NULL DEFAULT 100,
                        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (pattern, match_type)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO classification_rules_v7
                    SELECT * FROM classification_rules
                    """
                )
                conn.execute("DROP TABLE classification_rules")
                conn.execute(
                    "ALTER TABLE classification_rules_v7 RENAME TO classification_rules"
                )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    foreign_key_issues = list(conn.execute("PRAGMA foreign_key_check"))
    if foreign_key_issues:
        raise sqlite3.IntegrityError(
            f"Budget bucket migration created foreign-key issues: {foreign_key_issues}"
        )


def init_db(db_path: Path | str = DEFAULT_DB) -> None:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema not found: {SCHEMA_PATH}")
    conn = connect(db_path)
    stamp = now_iso()
    try:
        migrate_budget_buckets(conn)
        with conn:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            budget_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(budget_plans)")
            }
            if "apartment_minor" not in budget_columns:
                conn.execute(
                    """
                    ALTER TABLE budget_plans
                    ADD COLUMN apartment_minor INTEGER NOT NULL DEFAULT 0
                    CHECK (apartment_minor >= 0)
                    """
                )
            conn.execute(
                """
                INSERT INTO app_meta(key, value, updated_at) VALUES ('schema_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (SCHEMA_VERSION, stamp),
            )
            for key, value in (
                ("default_currency", DEFAULT_CURRENCY),
                ("timezone", "Asia/Manila"),
            ):
                conn.execute(
                    "INSERT OR IGNORE INTO app_meta(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, stamp),
                )

            conn.execute(
                """
                INSERT OR IGNORE INTO savings_categories(slug, name, active, created_at, updated_at)
                VALUES ('savings', 'Savings', 1, ?, ?)
                """,
                (stamp, stamp),
            )
            default_savings_category_id = conn.execute(
                "SELECT id FROM savings_categories WHERE slug = 'savings'"
            ).fetchone()["id"]
            conn.execute(
                """
                INSERT OR IGNORE INTO savings_allocations(transaction_id, category_id, created_at)
                SELECT id, ?, ? FROM transactions
                WHERE transaction_type = 'transfer' AND bucket = 'savings'
                """,
                (default_savings_category_id, stamp),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO wants_categories(slug, name, active, created_at, updated_at)
                VALUES ('wants', 'Wants', 1, ?, ?)
                """,
                (stamp, stamp),
            )
            default_wants_category_id = conn.execute(
                "SELECT id FROM wants_categories WHERE slug = 'wants'"
            ).fetchone()["id"]
            conn.execute(
                """
                INSERT OR IGNORE INTO wants_allocations(transaction_id, category_id, created_at)
                SELECT id, ?, ? FROM transactions
                WHERE bucket = 'wants' AND transaction_type IN ('expense', 'refund')
                """,
                (default_wants_category_id, stamp),
            )

            account_seeds = [
                ("cash-on-hand", "Cash on Hand", "cash", "asset", 1,
                 "Default spendable cash account; balance awaiting user input."),
                ("savings", "Savings", "savings", "asset", 0,
                 "Excluded from available cash and included in net worth."),
                ("receivables", "Receivables", "other_asset", "asset", 0,
                 "Money owed to the user; excluded from budgets and available cash."),
                ("bank-a-card", "Bank A Credit Card", "credit_card", "liability", 0,
                 "Limit, statement date, due date, and balance awaiting user input."),
                ("bank-b-card", "Bank B Credit Card", "credit_card", "liability", 0,
                 "Limit, statement date, due date, and balance awaiting user input."),
                ("bank-c-card", "Bank C Credit Card", "credit_card", "liability", 0,
                 "Limit, statement date, due date, and balance awaiting user input."),
            ]
            for slug, name, account_type, side, available, notes in account_seeds:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO accounts(
                        slug, name, account_type, normal_side, currency,
                        is_available_cash, include_in_net_worth, notes,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (slug, name, account_type, side, DEFAULT_CURRENCY,
                     available, notes, stamp, stamp),
                )
            conn.execute(
                """
                UPDATE accounts
                SET balance_base_minor = 0, balance_set_at = ?, updated_at = ?
                WHERE slug = 'receivables' AND balance_base_minor IS NULL
                """,
                (stamp, stamp),
            )

            aliases = {
                "cash-on-hand": ["cash", "cash on hand", "cash-on-hand"],
                "savings": ["saving", "savings"],
                "receivables": ["receivable", "receivables", "utang", "money owed"],
                "bank-a-card": ["bank-a", "bank-a card", "bank-a credit card"],
                "bank-b-card": ["bank-b", "bank-b card", "bank-b credit card"],
                "bank-c-card": ["bank-c", "bank-c card", "bank-c credit card", "bank c card"],
            }
            for account_slug, values in aliases.items():
                account_id = conn.execute(
                    "SELECT id FROM accounts WHERE slug = ?", (account_slug,)
                ).fetchone()["id"]
                for alias in values:
                    conn.execute(
                        "INSERT OR IGNORE INTO account_aliases(alias, account_id) VALUES (?, ?)",
                        (alias, account_id),
                    )

            rules = [
                ("lunch", "word", "needs", ["food", "meal"],
                 "A normal lunch sustains the user through the day.", 1, 200),
                ("meryenda", "word", "wants", ["food", "snack"],
                 "Interpreted as an optional snack; user wording said optional.", 0, 190),
                ("snack", "word", "wants", ["food", "snack"],
                 "Optional snacks default to wants unless context makes them necessary.", 0, 100),
            ]
            for pattern, match_type, bucket, tags, rationale, confirmed, priority in rules:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO classification_rules(
                        pattern, match_type, bucket, tags_json, rationale,
                        user_confirmed, priority, active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (pattern, match_type, bucket, json.dumps(tags), rationale,
                     confirmed, priority, stamp, stamp),
                )
    finally:
        conn.close()


def resolve_account(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT a.* FROM accounts AS a
        LEFT JOIN account_aliases AS aa ON aa.account_id = a.id
        WHERE lower(a.slug) = lower(?) OR lower(a.name) = lower(?) OR lower(aa.alias) = lower(?)
        ORDER BY a.id LIMIT 1
        """,
        (identifier, identifier, identifier),
    ).fetchone()
    if row is not None:
        if not row["active"]:
            raise ValueError(f"Account is inactive: {identifier}")
        return row

    partial = conn.execute(
        "SELECT * FROM accounts WHERE active = 1 AND (slug LIKE ? OR name LIKE ?)",
        (f"%{identifier}%", f"%{identifier}%"),
    ).fetchall()
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        choices = ", ".join(row["slug"] for row in partial)
        raise ValueError(f"Ambiguous account {identifier!r}; choose one of: {choices}")
    raise ValueError(f"Unknown account: {identifier}")


def account_payload(conn: sqlite3.Connection, account_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM v_account_balances WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown account id: {account_id}")
    return {
        "slug": row["slug"],
        "name": row["name"],
        "type": row["account_type"],
        "side": row["normal_side"],
        "currency": row["currency"],
        "available_cash": bool(row["is_available_cash"]),
        "balance_known": row["current_balance_minor"] is not None,
        "balance_minor": row["current_balance_minor"],
        "balance": format_money(row["current_balance_minor"], row["currency"]),
        "activity_since_setup_minor": row["activity_minor"],
        "credit_limit_minor": row["credit_limit_minor"],
        "credit_limit": format_money(row["credit_limit_minor"], row["currency"]),
        "statement_day": row["statement_day"],
        "due_day": row["due_day"],
        "next_due_date": row["next_due_date"],
        "balance_set_at": row["balance_set_at"],
    }


def list_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id FROM accounts WHERE active = 1 ORDER BY id").fetchall()
    return [account_payload(conn, row["id"]) for row in rows]


def set_account(
    conn: sqlite3.Connection,
    identifier: str,
    *,
    name: str | None = None,
    balance_minor: int | None = None,
    set_balance: bool = False,
    credit_limit_minor: int | None = None,
    set_limit: bool = False,
    statement_day: int | None = None,
    due_day: int | None = None,
    next_due_date: str | None = None,
    notes: str | None = None,
    available_cash: bool | None = None,
) -> dict[str, Any]:
    account = resolve_account(conn, identifier)
    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = name
    if set_balance:
        if balance_minor is None:
            raise ValueError("Current account balance must be supplied.")
        if balance_minor < 0 and account["normal_side"] != "liability":
            raise ValueError(
                "Only liability (credit-card) accounts may hold a negative balance "
                "(e.g. an overpayment the issuer owes you). Assets must be zero or greater."
            )
        activity = conn.execute(
            "SELECT COALESCE(SUM(balance_delta_minor), 0) AS total FROM account_entries WHERE account_id = ?",
            (account["id"],),
        ).fetchone()["total"]
        updates["balance_base_minor"] = balance_minor - activity
        updates["balance_set_at"] = now_iso()
    if set_limit:
        if account["account_type"] != "credit_card":
            raise ValueError("Credit limits apply only to credit-card accounts.")
        updates["credit_limit_minor"] = credit_limit_minor
    if statement_day is not None:
        updates["statement_day"] = statement_day
    if due_day is not None:
        updates["due_day"] = due_day
    if next_due_date is not None:
        date.fromisoformat(next_due_date)
        updates["next_due_date"] = next_due_date
    if notes is not None:
        updates["notes"] = notes
    if available_cash is not None:
        updates["is_available_cash"] = int(available_cash)
    if not updates:
        raise ValueError("No account changes were supplied.")
    updates["updated_at"] = now_iso()

    with conn:
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE accounts SET {assignments} WHERE id = ?",
            (*updates.values(), account["id"]),
        )
        audit(conn, "account_updated", "account", account["id"], updates)
    return account_payload(conn, account["id"])


def add_account(
    conn: sqlite3.Connection,
    name: str,
    account_type: str,
    *,
    slug: str | None = None,
    balance_minor: int | None = None,
    available_cash: bool = False,
    aliases: Sequence[str] = (),
) -> dict[str, Any]:
    account_slug = slugify(slug or name)
    if account_type in {"cash", "savings", "investment", "other_asset"}:
        side = "asset"
    else:
        side = "liability"
    if available_cash and account_type != "cash":
        raise ValueError("Only cash/e-wallet accounts can be marked as available cash.")
    stamp = now_iso()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO accounts(
                slug, name, account_type, normal_side, currency,
                is_available_cash, include_in_net_worth, balance_base_minor,
                balance_set_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (account_slug, name, account_type, side, DEFAULT_CURRENCY,
             int(available_cash), balance_minor,
             stamp if balance_minor is not None else None, stamp, stamp),
        )
        account_id = cursor.lastrowid
        for alias in {name, account_slug, *aliases}:
            conn.execute(
                "INSERT OR IGNORE INTO account_aliases(alias, account_id) VALUES (?, ?)",
                (alias, account_id),
            )
        audit(conn, "account_created", "account", account_id,
              {"slug": account_slug, "type": account_type})
    return account_payload(conn, int(account_id))


def budget_plan_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT bp.*, a.slug AS savings_account_slug, a.name AS savings_account_name
        FROM budget_plans AS bp
        JOIN accounts AS a ON a.id = bp.savings_account_id
        WHERE bp.id = 1
        """
    ).fetchone()
    if row is None:
        raise ValueError("No monthly budget plan is configured. Use budget-set first.")

    gross_needs_target = proportional_minor(row["monthly_income_minor"], row["needs_bps"])
    apartment_target = row["apartment_minor"]
    needs_target = gross_needs_target - apartment_target
    wants_target = proportional_minor(row["monthly_income_minor"], row["wants_bps"])
    savings_target = row["monthly_income_minor"] - gross_needs_target - wants_target

    def category(name: str, basis_points: int, target_minor: int) -> dict[str, Any]:
        return {
            "category": name,
            "percent_basis_points": basis_points,
            "percent": percentage_text(basis_points),
            "target_minor": target_minor,
            "target": format_money(target_minor),
        }

    needs_category = category("needs", row["needs_bps"], needs_target)
    needs_category.update({
        "allocation_type": "needs_remainder_after_apartment",
        "percent_basis_points": None,
        "percent": None,
        "source_percent_basis_points": row["needs_bps"],
        "source_percent": percentage_text(row["needs_bps"]),
    })

    return {
        "monthly_income_minor": row["monthly_income_minor"],
        "monthly_income": format_money(row["monthly_income_minor"]),
        "created_at": row["created_at"],
        "categories": {
            "needs": needs_category,
            "apartment": {
                "category": "apartment",
                "allocation_type": "fixed",
                "percent_basis_points": None,
                "percent": None,
                "target_minor": apartment_target,
                "target": format_money(apartment_target),
            },
            "wants": category("wants", row["wants_bps"], wants_target),
            "buffer": {
                "category": "buffer", "allocation_type": "rollover_pool",
                "percent_basis_points": None, "percent": None,
                "target_minor": 0, "target": format_money(0),
            },
            "savings": category("savings", row["savings_bps"], savings_target),
        },
        "auto_save_salary": bool(row["auto_save_salary"]),
        "savings_account": row["savings_account_slug"],
        "savings_account_name": row["savings_account_name"],
        "updated_at": row["updated_at"],
    }


def set_budget_plan(
    conn: sqlite3.Connection,
    monthly_income_minor: int,
    needs_bps: int,
    wants_bps: int,
    savings_bps: int,
    *,
    apartment_minor: int = 0,
    savings_account_identifier: str = "savings",
    auto_save_salary: bool = True,
) -> dict[str, Any]:
    if monthly_income_minor <= 0:
        raise ValueError("Monthly income must be greater than zero.")
    if needs_bps + wants_bps + savings_bps != 10000:
        raise ValueError("Needs, wants, and savings percentages must total exactly 100%.")
    gross_needs_target = proportional_minor(monthly_income_minor, needs_bps)
    if apartment_minor < 0 or apartment_minor > gross_needs_target:
        raise ValueError("Apartment allocation must fit within the needs budget.")
    savings_account = resolve_account(conn, savings_account_identifier)
    if savings_account["account_type"] != "savings":
        raise ValueError("Automatic salary savings must use a savings account.")
    stamp = now_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO budget_plans(
                id, monthly_income_minor, needs_bps, wants_bps, savings_bps,
                apartment_minor, auto_save_salary, savings_account_id, created_at, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                monthly_income_minor = excluded.monthly_income_minor,
                needs_bps = excluded.needs_bps,
                wants_bps = excluded.wants_bps,
                savings_bps = excluded.savings_bps,
                apartment_minor = excluded.apartment_minor,
                auto_save_salary = excluded.auto_save_salary,
                savings_account_id = excluded.savings_account_id,
                updated_at = excluded.updated_at
            """,
            (monthly_income_minor, needs_bps, wants_bps, savings_bps, apartment_minor,
             int(auto_save_salary), savings_account["id"], stamp, stamp),
        )
        audit(conn, "budget_plan_set", "budget_plan", 1, {
            "monthly_income_minor": monthly_income_minor,
            "needs_bps": needs_bps,
            "wants_bps": wants_bps,
            "savings_bps": savings_bps,
            "apartment_minor": apartment_minor,
            "auto_save_salary": auto_save_salary,
            "savings_account": savings_account["slug"],
        })
    return budget_plan_payload(conn)


def new_public_id() -> str:
    stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d")
    return f"FIN-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def new_comment_public_id() -> str:
    stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d")
    return f"COM-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def transaction_payload(conn: sqlite3.Connection, public_id: str) -> dict[str, Any]:
    tx = conn.execute(
        "SELECT * FROM transactions WHERE public_id = ?", (public_id,)
    ).fetchone()
    if tx is None:
        raise ValueError(f"Unknown transaction: {public_id}")
    tag_rows = conn.execute(
        """
        SELECT t.display_name FROM tags AS t
        JOIN transaction_tags AS tt ON tt.tag_id = t.id
        WHERE tt.transaction_id = ? ORDER BY t.slug
        """,
        (tx["id"],),
    ).fetchall()
    entry_rows = conn.execute(
        """
        SELECT e.balance_delta_minor, e.entry_role, a.id AS account_id,
               a.slug, a.name, a.currency
        FROM account_entries AS e
        JOIN accounts AS a ON a.id = e.account_id
        WHERE e.transaction_id = ? ORDER BY e.id
        """,
        (tx["id"],),
    ).fetchall()
    impacts = []
    for entry in entry_rows:
        current = account_payload(conn, entry["account_id"])
        impacts.append({
            "account": entry["slug"],
            "account_name": entry["name"],
            "role": entry["entry_role"],
            "delta_minor": entry["balance_delta_minor"],
            "delta": format_money(entry["balance_delta_minor"], entry["currency"]),
            "resulting_balance_known": current["balance_known"],
            "resulting_balance_minor": current["balance_minor"],
            "resulting_balance": current["balance"],
        })
    related_public_id = None
    if tx["related_transaction_id"] is not None:
        related = conn.execute(
            "SELECT public_id FROM transactions WHERE id = ?",
            (tx["related_transaction_id"],),
        ).fetchone()
        related_public_id = related["public_id"] if related else None
    reversal_public_id = None
    if tx["reversal_of_id"] is not None:
        original = conn.execute(
            "SELECT public_id FROM transactions WHERE id = ?", (tx["reversal_of_id"],)
        ).fetchone()
        reversal_public_id = original["public_id"] if original else None
    savings_category = conn.execute(
        """
        SELECT sc.slug, sc.name FROM savings_allocations AS sa
        JOIN savings_categories AS sc ON sc.id = sa.category_id
        WHERE sa.transaction_id = ?
        """,
        (tx["id"],),
    ).fetchone()
    wants_category = conn.execute(
        """
        SELECT wc.slug, wc.name FROM wants_allocations AS wa
        JOIN wants_categories AS wc ON wc.id = wa.category_id
        WHERE wa.transaction_id = ?
        """,
        (tx["id"],),
    ).fetchone()
    comment_rows = conn.execute(
        """
        SELECT public_id, body, created_at, updated_at
        FROM transaction_comments
        WHERE transaction_id = ? AND active = 1
        ORDER BY id
        """,
        (tx["id"],),
    ).fetchall()
    return {
        "transaction_id": tx["public_id"],
        "type": tx["transaction_type"],
        "status": tx["status"],
        "amount_minor": tx["amount_minor"],
        "amount": format_money(tx["amount_minor"], tx["currency"]),
        "currency": tx["currency"],
        "description": tx["description"],
        "counterparty": tx["counterparty"],
        "bucket": tx["bucket"],
        "savings_category": (
            {"slug": savings_category["slug"], "name": savings_category["name"]}
            if savings_category is not None else None
        ),
        "wants_category": (
            {"slug": wants_category["slug"], "name": wants_category["name"]}
            if wants_category is not None else None
        ),
        "tags": [row["display_name"] for row in tag_rows],
        "occurred_at": tx["occurred_at"],
        "original_message": tx["original_message"],
        "comments": [
            {
                "comment_id": row["public_id"],
                "body": row["body"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in comment_rows
        ],
        "related_transaction_id": related_public_id,
        "reversal_of": reversal_public_id,
        "account_impacts": impacts,
    }


def add_transaction_comment(
    conn: sqlite3.Connection, transaction_public_id: str, body: str
) -> dict[str, Any]:
    clean_body = body.strip()
    if not clean_body:
        raise ValueError("Comment cannot be empty.")
    transaction = conn.execute(
        "SELECT id FROM transactions WHERE public_id = ?", (transaction_public_id,)
    ).fetchone()
    if transaction is None:
        raise ValueError(f"Unknown transaction: {transaction_public_id}")
    public_id = new_comment_public_id()
    stamp = now_iso()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO transaction_comments(
                public_id, transaction_id, body, active, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?)
            """,
            (public_id, transaction["id"], clean_body, stamp, stamp),
        )
        audit(
            conn, "transaction_comment_added", "transaction_comment", public_id,
            {"transaction_id": transaction_public_id},
        )
    return {
        "comment_id": public_id,
        "transaction_id": transaction_public_id,
        "body": clean_body,
        "active": True,
        "created_at": stamp,
    }


def list_transaction_comments(
    conn: sqlite3.Connection, transaction_public_id: str, include_retired: bool = False
) -> list[dict[str, Any]]:
    transaction = conn.execute(
        "SELECT id FROM transactions WHERE public_id = ?", (transaction_public_id,)
    ).fetchone()
    if transaction is None:
        raise ValueError(f"Unknown transaction: {transaction_public_id}")
    active_filter = "" if include_retired else "AND active = 1"
    rows = conn.execute(
        f"""
        SELECT public_id, body, active, created_at, updated_at
        FROM transaction_comments
        WHERE transaction_id = ? {active_filter}
        ORDER BY id
        """,
        (transaction["id"],),
    ).fetchall()
    return [
        {
            "comment_id": row["public_id"],
            "transaction_id": transaction_public_id,
            "body": row["body"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def remove_transaction_comment(conn: sqlite3.Connection, comment_public_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT tc.*, tx.public_id AS transaction_public_id
        FROM transaction_comments AS tc
        JOIN transactions AS tx ON tx.id = tc.transaction_id
        WHERE tc.public_id = ?
        """,
        (comment_public_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown comment: {comment_public_id}")
    if not row["active"]:
        raise ValueError(f"Comment is already removed: {comment_public_id}")
    stamp = now_iso()
    with conn:
        conn.execute(
            "UPDATE transaction_comments SET active = 0, updated_at = ? WHERE id = ?",
            (stamp, row["id"]),
        )
        audit(
            conn, "transaction_comment_removed", "transaction_comment", comment_public_id,
            {"transaction_id": row["transaction_public_id"]},
        )
    return {
        "comment_id": comment_public_id,
        "transaction_id": row["transaction_public_id"],
        "body": row["body"],
        "active": False,
        "removed_at": stamp,
    }


def _create_transaction(
    conn: sqlite3.Connection,
    *,
    transaction_type: str,
    amount_minor: int,
    description: str,
    entries: Sequence[tuple[sqlite3.Row, int, str]],
    bucket: str | None = None,
    tags: Iterable[str] = (),
    occurred_at: str | None = None,
    counterparty: str | None = None,
    original_message: str | None = None,
    related_transaction_id: int | None = None,
    reversal_of_id: int | None = None,
) -> dict[str, Any]:
    if not entries:
        raise ValueError("A transaction must affect at least one account.")
    public_id = new_public_id()
    stamp = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO transactions(
            public_id, occurred_at, transaction_type, amount_minor, currency,
            description, counterparty, bucket, original_message, status,
            related_transaction_id, reversal_of_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'posted', ?, ?, ?, ?)
        """,
        (public_id, occurred_iso(occurred_at), transaction_type, amount_minor,
         DEFAULT_CURRENCY, description.strip(), counterparty, bucket,
         original_message, related_transaction_id, reversal_of_id, stamp, stamp),
    )
    transaction_id = int(cursor.lastrowid)
    for account, delta, role in entries:
        if account["currency"] != DEFAULT_CURRENCY:
            raise ValueError("Multi-currency transactions are not supported yet.")
        conn.execute(
            """
            INSERT INTO account_entries(
                transaction_id, account_id, balance_delta_minor, entry_role, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (transaction_id, account["id"], delta, role, stamp),
        )
    for display_name in parse_tags(tags):
        tag_slug = slugify(display_name)
        conn.execute(
            "INSERT OR IGNORE INTO tags(slug, display_name) VALUES (?, ?)",
            (tag_slug, display_name),
        )
        tag_id = conn.execute("SELECT id FROM tags WHERE slug = ?", (tag_slug,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO transaction_tags(transaction_id, tag_id) VALUES (?, ?)",
            (transaction_id, tag_id),
        )
    audit(conn, "transaction_posted", "transaction", public_id,
          {"type": transaction_type, "amount_minor": amount_minor, "bucket": bucket})
    return transaction_payload(conn, public_id)


def record_expense(
    conn: sqlite3.Connection,
    amount_minor: int,
    description: str,
    account_identifier: str,
    bucket: str,
    *,
    tags: Iterable[str] = (),
    occurred_at: str | None = None,
    merchant: str | None = None,
    original_message: str | None = None,
    wants_category_identifier: str | None = None,
) -> dict[str, Any]:
    if bucket not in {"needs", "apartment", "wants", "buffer"}:
        raise ValueError(
            "Expenses must be bucketed as needs, apartment, wants, or buffer; "
            "use transfer for savings."
        )
    if wants_category_identifier and bucket != "wants":
        raise ValueError("A wants category can only be used with the wants bucket.")
    account = resolve_account(conn, account_identifier)
    delta = -amount_minor if account["normal_side"] == "asset" else amount_minor
    merged_tags = [*( ["apartment"] if bucket == "apartment" else [] ), *parse_tags(tags)]
    with conn:
        transaction = _create_transaction(
            conn, transaction_type="expense", amount_minor=amount_minor,
            description=description, entries=[(account, delta, "payment")],
            bucket=bucket, tags=merged_tags, occurred_at=occurred_at,
            counterparty=merchant, original_message=original_message,
        )
        if bucket == "wants":
            assign_wants_category(
                conn, transaction["transaction_id"], wants_category_identifier or "wants"
            )
            transaction = transaction_payload(conn, transaction["transaction_id"])
        return transaction


def record_income(
    conn: sqlite3.Connection,
    amount_minor: int,
    description: str,
    account_identifier: str,
    *,
    source: str | None = None,
    tags: Iterable[str] = (),
    occurred_at: str | None = None,
    original_message: str | None = None,
) -> dict[str, Any]:
    account = resolve_account(conn, account_identifier)
    if account["normal_side"] != "asset":
        raise ValueError("Income must enter an asset account, not a liability.")
    merged_tags = ["income", *parse_tags(tags)]
    with conn:
        return _create_transaction(
            conn, transaction_type="income", amount_minor=amount_minor,
            description=description, entries=[(account, amount_minor, "deposit")],
            tags=merged_tags, occurred_at=occurred_at,
            counterparty=source, original_message=original_message,
        )


def record_salary(
    conn: sqlite3.Connection,
    amount_minor: int,
    description: str,
    account_identifier: str,
    *,
    source: str | None = None,
    tags: Iterable[str] = (),
    occurred_at: str | None = None,
    original_message: str | None = None,
) -> dict[str, Any]:
    plan = budget_plan_payload(conn)
    income_account = resolve_account(conn, account_identifier)
    savings_account = resolve_account(conn, plan["savings_account"])
    if income_account["normal_side"] != "asset":
        raise ValueError("Salary must enter an asset account, not a liability.")
    if income_account["id"] == savings_account["id"] and plan["auto_save_salary"]:
        raise ValueError("Salary deposited directly to Savings cannot be auto-transferred to itself.")

    savings_bps = plan["categories"]["savings"]["percent_basis_points"]
    savings_minor = proportional_minor(amount_minor, savings_bps)
    effective_occurrence = occurred_iso(occurred_at)
    merged_income_tags = ["income", "salary", *parse_tags(tags)]

    with conn:
        income = _create_transaction(
            conn, transaction_type="income", amount_minor=amount_minor,
            description=description, entries=[(income_account, amount_minor, "deposit")],
            tags=merged_income_tags, occurred_at=effective_occurrence,
            counterparty=source, original_message=original_message,
        )
        savings_transfer = None
        if plan["auto_save_salary"] and savings_minor > 0:
            income_row = conn.execute(
                "SELECT id FROM transactions WHERE public_id = ?",
                (income["transaction_id"],),
            ).fetchone()
            savings_transfer = _create_transaction(
                conn, transaction_type="transfer", amount_minor=savings_minor,
                description=f"Automatic {plan['categories']['savings']['percent']} salary savings",
                entries=[(income_account, -savings_minor, "source"),
                         (savings_account, savings_minor, "destination")],
                bucket="savings", tags=["transfer", "savings", "salary", "automatic"],
                occurred_at=effective_occurrence, original_message=original_message,
                related_transaction_id=income_row["id"],
            )
            assign_savings_category(conn, savings_transfer["transaction_id"], "savings")
            savings_transfer = transaction_payload(conn, savings_transfer["transaction_id"])
        income = transaction_payload(conn, income["transaction_id"])

    return {
        "income": income,
        "savings_transfer": savings_transfer,
        "gross_salary_minor": amount_minor,
        "gross_salary": format_money(amount_minor),
        "saved_minor": savings_minor if plan["auto_save_salary"] else 0,
        "saved": format_money(savings_minor if plan["auto_save_salary"] else 0),
        "remaining_available_minor": amount_minor - savings_minor
        if plan["auto_save_salary"] else amount_minor,
        "remaining_available": format_money(
            amount_minor - savings_minor if plan["auto_save_salary"] else amount_minor
        ),
        "savings_percent": plan["categories"]["savings"]["percent"],
    }


def record_transfer(
    conn: sqlite3.Connection,
    amount_minor: int,
    source_identifier: str,
    destination_identifier: str,
    *,
    description: str = "Transfer",
    tags: Iterable[str] = (),
    savings_category_identifier: str | None = None,
    occurred_at: str | None = None,
    original_message: str | None = None,
) -> dict[str, Any]:
    source = resolve_account(conn, source_identifier)
    destination = resolve_account(conn, destination_identifier)
    if source["id"] == destination["id"]:
        raise ValueError("Transfer source and destination must be different.")
    if source["normal_side"] != "asset" or destination["normal_side"] != "asset":
        raise ValueError("Use card-payment for a transfer to a credit-card liability.")
    is_savings = destination["account_type"] == "savings"
    if savings_category_identifier is not None and not is_savings:
        raise ValueError("Savings categories apply only to transfers into a savings account.")
    bucket = "savings" if is_savings else None
    merged_tags = ["transfer", *( ["savings"] if is_savings else [] ), *parse_tags(tags)]
    with conn:
        transfer = _create_transaction(
            conn, transaction_type="transfer", amount_minor=amount_minor,
            description=description,
            entries=[(source, -amount_minor, "source"),
                     (destination, amount_minor, "destination")],
            bucket=bucket, tags=merged_tags, occurred_at=occurred_at,
            original_message=original_message,
        )
        if is_savings:
            assign_savings_category(
                conn, transfer["transaction_id"], savings_category_identifier or "savings"
            )
        return transaction_payload(conn, transfer["transaction_id"])


def resolve_wants_category(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row:
    row = conn.execute(
        """SELECT * FROM wants_categories
        WHERE active = 1 AND (lower(slug) = lower(?) OR lower(name) = lower(?))
        ORDER BY id LIMIT 1""", (identifier, identifier),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown active wants category: {identifier}")
    return row


def assign_wants_category(
    conn: sqlite3.Connection, transaction_public_id: str, category_identifier: str
) -> None:
    category = resolve_wants_category(conn, category_identifier)
    transaction = conn.execute(
        "SELECT id, transaction_type, bucket FROM transactions WHERE public_id = ?",
        (transaction_public_id,),
    ).fetchone()
    if transaction is None:
        raise ValueError(f"Unknown transaction: {transaction_public_id}")
    if transaction["bucket"] != "wants" or transaction["transaction_type"] not in {"expense", "refund"}:
        raise ValueError("Only wants expenses and refunds can have a wants category.")
    conn.execute(
        """INSERT INTO wants_allocations(transaction_id, category_id, created_at)
        VALUES (?, ?, ?) ON CONFLICT(transaction_id) DO UPDATE
        SET category_id = excluded.category_id""",
        (transaction["id"], category["id"], now_iso()),
    )


def wants_category_payload(conn: sqlite3.Connection, category_id: int) -> dict[str, Any]:
    row = conn.execute(
        """SELECT wc.*, COALESCE(SUM(CASE
            WHEN tx.status = 'posted' AND tx.transaction_type = 'expense' THEN tx.amount_minor
            WHEN tx.status = 'posted' AND tx.transaction_type = 'refund' THEN -tx.amount_minor
            ELSE 0 END), 0) AS spent_minor
        FROM wants_categories wc
        LEFT JOIN wants_allocations wa ON wa.category_id = wc.id
        LEFT JOIN transactions tx ON tx.id = wa.transaction_id
        WHERE wc.id = ? GROUP BY wc.id""", (category_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown wants category.")
    return {"slug": row["slug"], "name": row["name"], "active": bool(row["active"]),
            "spent_minor": row["spent_minor"], "spent": format_money(row["spent_minor"])}


def list_wants_categories(conn: sqlite3.Connection, include_retired: bool = False) -> list[dict[str, Any]]:
    where = "" if include_retired else "WHERE active = 1"
    rows = conn.execute(f"SELECT id FROM wants_categories {where} ORDER BY id").fetchall()
    return [wants_category_payload(conn, row["id"]) for row in rows]


def add_wants_category(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    slug, stamp = slugify(name), now_iso()
    with conn:
        existing = conn.execute("SELECT * FROM wants_categories WHERE slug = ?", (slug,)).fetchone()
        if existing and existing["active"]:
            raise ValueError(f"Wants category already exists: {existing['name']}")
        if existing:
            conn.execute("UPDATE wants_categories SET name = ?, active = 1, updated_at = ? WHERE id = ?",
                         (name.strip(), stamp, existing["id"]))
            category_id = existing["id"]
        else:
            category_id = conn.execute(
                "INSERT INTO wants_categories(slug,name,active,created_at,updated_at) VALUES (?,?,1,?,?)",
                (slug, name.strip(), stamp, stamp),
            ).lastrowid
        audit(conn, "wants_category_added", "wants_category", category_id, {"slug": slug})
    return wants_category_payload(conn, int(category_id))


def remove_wants_category(conn: sqlite3.Connection, identifier: str) -> dict[str, Any]:
    category = resolve_wants_category(conn, identifier)
    if category["slug"] == "wants":
        raise ValueError("The default Wants category cannot be removed.")
    with conn:
        conn.execute("UPDATE wants_categories SET active = 0, updated_at = ? WHERE id = ?",
                     (now_iso(), category["id"]))
        audit(conn, "wants_category_removed", "wants_category", category["id"], {"slug": category["slug"]})
    payload = wants_category_payload(conn, category["id"])
    payload["retired"] = True
    return payload


def resolve_savings_category(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM savings_categories
        WHERE lower(slug) = lower(?) OR lower(name) = lower(?)
        ORDER BY id LIMIT 1
        """,
        (identifier, identifier),
    ).fetchone()
    if row is not None:
        if not row["active"]:
            raise ValueError(f"Savings category is retired: {identifier}")
        return row
    matches = conn.execute(
        """
        SELECT * FROM savings_categories
        WHERE active = 1 AND (slug LIKE ? OR name LIKE ?)
        ORDER BY id
        """,
        (f"%{identifier}%", f"%{identifier}%"),
    ).fetchall()
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("Ambiguous savings category; use its exact name.")
    raise ValueError(f"Unknown savings category: {identifier}")


def assign_savings_category(
    conn: sqlite3.Connection, transaction_public_id: str, category_identifier: str
) -> None:
    category = resolve_savings_category(conn, category_identifier)
    transaction = conn.execute(
        "SELECT id, transaction_type, bucket FROM transactions WHERE public_id = ?",
        (transaction_public_id,),
    ).fetchone()
    if transaction is None:
        raise ValueError(f"Unknown transaction: {transaction_public_id}")
    if transaction["transaction_type"] != "transfer" or transaction["bucket"] != "savings":
        raise ValueError("Only savings transfers can have a savings category.")
    conn.execute(
        """
        INSERT INTO savings_allocations(transaction_id, category_id, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(transaction_id) DO UPDATE SET category_id = excluded.category_id
        """,
        (transaction["id"], category["id"], now_iso()),
    )


def add_savings_category(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    slug = slugify(name)
    stamp = now_iso()
    with conn:
        existing = conn.execute(
            "SELECT * FROM savings_categories WHERE slug = ?", (slug,)
        ).fetchone()
        if existing is not None:
            if existing["active"]:
                raise ValueError(f"Savings category already exists: {existing['name']}")
            conn.execute(
                "UPDATE savings_categories SET name = ?, active = 1, updated_at = ? WHERE id = ?",
                (name.strip(), stamp, existing["id"]),
            )
            category_id = existing["id"]
        else:
            cursor = conn.execute(
                """
                INSERT INTO savings_categories(slug, name, active, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (slug, name.strip(), stamp, stamp),
            )
            category_id = cursor.lastrowid
        audit(conn, "savings_category_added", "savings_category", category_id, {"slug": slug})
    return savings_category_payload(conn, int(category_id))


def remove_savings_category(conn: sqlite3.Connection, identifier: str) -> dict[str, Any]:
    category = resolve_savings_category(conn, identifier)
    if category["slug"] == "savings":
        raise ValueError("The default Savings category cannot be removed.")
    with conn:
        conn.execute(
            "UPDATE savings_categories SET active = 0, updated_at = ? WHERE id = ?",
            (now_iso(), category["id"]),
        )
        conn.execute(
            "UPDATE savings_goals SET active = 0, updated_at = ? WHERE category_id = ? AND active = 1",
            (now_iso(), category["id"]),
        )
        audit(conn, "savings_category_removed", "savings_category", category["id"], {"slug": category["slug"]})
    payload = savings_category_payload(conn, category["id"])
    payload["retired"] = True
    return payload


def savings_category_payload(conn: sqlite3.Connection, category_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT sc.*, COALESCE(SUM(CASE WHEN tx.status = 'posted' THEN tx.amount_minor ELSE 0 END), 0) AS saved_minor
        FROM savings_categories AS sc
        LEFT JOIN savings_allocations AS sa ON sa.category_id = sc.id
        LEFT JOIN transactions AS tx ON tx.id = sa.transaction_id
        WHERE sc.id = ?
        GROUP BY sc.id
        """,
        (category_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown savings category.")
    return {
        "slug": row["slug"], "name": row["name"], "active": bool(row["active"]),
        "saved_minor": row["saved_minor"], "saved": format_money(row["saved_minor"]),
    }


def list_savings_categories(conn: sqlite3.Connection, include_retired: bool = False) -> list[dict[str, Any]]:
    where = "" if include_retired else "WHERE sc.active = 1"
    rows = conn.execute(
        f"""
        SELECT sc.id FROM savings_categories AS sc {where} ORDER BY sc.id
        """
    ).fetchall()
    return [savings_category_payload(conn, row["id"]) for row in rows]


def add_savings_goal(
    conn: sqlite3.Connection,
    name: str,
    target_minor: int,
    category_identifier: str = "savings",
    *,
    target_date: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    if target_date is not None:
        date.fromisoformat(target_date)
    category = resolve_savings_category(conn, category_identifier)
    slug = slugify(name)
    stamp = now_iso()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO savings_goals(slug, name, category_id, target_minor, target_date, notes, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (slug, name.strip(), category["id"], target_minor, target_date, notes, stamp, stamp),
        )
        audit(conn, "savings_goal_added", "savings_goal", cursor.lastrowid, {"slug": slug})
    return savings_goal_payload(conn, int(cursor.lastrowid))


def remove_savings_goal(conn: sqlite3.Connection, identifier: str) -> dict[str, Any]:
    goal = resolve_savings_goal(conn, identifier)
    with conn:
        conn.execute("UPDATE savings_goals SET active = 0, updated_at = ? WHERE id = ?", (now_iso(), goal["id"]))
        audit(conn, "savings_goal_removed", "savings_goal", goal["id"], {"slug": goal["slug"]})
    result = savings_goal_payload(conn, goal["id"])
    result["retired"] = True
    return result


def resolve_savings_goal(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM savings_goals WHERE active = 1 AND (lower(slug) = lower(?) OR lower(name) = lower(?))",
        (identifier, identifier),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown active savings goal: {identifier}")
    return row


def savings_goal_payload(conn: sqlite3.Connection, goal_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT sg.*, sc.slug AS category_slug, sc.name AS category_name,
               COALESCE(SUM(CASE WHEN tx.status = 'posted' THEN tx.amount_minor ELSE 0 END), 0) AS saved_minor
        FROM savings_goals AS sg
        JOIN savings_categories AS sc ON sc.id = sg.category_id
        LEFT JOIN savings_allocations AS sa ON sa.category_id = sc.id
        LEFT JOIN transactions AS tx ON tx.id = sa.transaction_id
        WHERE sg.id = ?
        GROUP BY sg.id
        """,
        (goal_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown savings goal.")
    saved_minor = row["saved_minor"]
    remaining_minor = max(0, row["target_minor"] - saved_minor)
    return {
        "slug": row["slug"], "name": row["name"], "active": bool(row["active"]),
        "category": {"slug": row["category_slug"], "name": row["category_name"]},
        "target_minor": row["target_minor"], "target": format_money(row["target_minor"]),
        "saved_minor": saved_minor, "saved": format_money(saved_minor),
        "remaining_minor": remaining_minor, "remaining": format_money(remaining_minor),
        "complete": saved_minor >= row["target_minor"], "target_date": row["target_date"], "notes": row["notes"],
    }


def list_savings_goals(conn: sqlite3.Connection, include_retired: bool = False) -> list[dict[str, Any]]:
    where = "" if include_retired else "WHERE active = 1"
    rows = conn.execute(f"SELECT id FROM savings_goals {where} ORDER BY id").fetchall()
    return [savings_goal_payload(conn, row["id"]) for row in rows]


def record_receivable(
    conn: sqlite3.Connection,
    amount_minor: int,
    debtor: str,
    *,
    description: str | None = None,
    tags: Iterable[str] = (),
    occurred_at: str | None = None,
    original_message: str | None = None,
) -> dict[str, Any]:
    receivables = resolve_account(conn, "receivables")
    merged_tags = ["receivable", "debt", *parse_tags(tags)]
    with conn:
        return _create_transaction(
            conn,
            transaction_type="adjustment",
            amount_minor=amount_minor,
            description=description or f"Amount owed by {debtor}",
            entries=[(receivables, amount_minor, "receivable_created")],
            tags=merged_tags,
            occurred_at=occurred_at,
            counterparty=debtor,
            original_message=original_message,
        )


def record_receivable_payment(
    conn: sqlite3.Connection,
    amount_minor: int,
    debtor: str,
    destination_identifier: str,
    *,
    description: str | None = None,
    tags: Iterable[str] = (),
    occurred_at: str | None = None,
    original_message: str | None = None,
) -> dict[str, Any]:
    receivables = resolve_account(conn, "receivables")
    destination = resolve_account(conn, destination_identifier)
    if destination["normal_side"] != "asset":
        raise ValueError("A receivable payment must enter an asset account.")
    current = account_payload(conn, receivables["id"])["balance_minor"]
    if current is not None and amount_minor > current:
        raise ValueError("Receivable payment exceeds total receivables outstanding.")
    merged_tags = ["receivable", "repayment", "transfer", *parse_tags(tags)]
    with conn:
        return _create_transaction(
            conn,
            transaction_type="transfer",
            amount_minor=amount_minor,
            description=description or f"Receivable payment from {debtor}",
            entries=[
                (receivables, -amount_minor, "receivable_reduction"),
                (destination, amount_minor, "destination"),
            ],
            tags=merged_tags,
            occurred_at=occurred_at,
            counterparty=debtor,
            original_message=original_message,
        )


def record_card_payment(
    conn: sqlite3.Connection,
    amount_minor: int,
    source_identifier: str,
    card_identifier: str,
    *,
    description: str = "Credit-card payment",
    tags: Iterable[str] = (),
    occurred_at: str | None = None,
    original_message: str | None = None,
) -> dict[str, Any]:
    source = resolve_account(conn, source_identifier)
    card = resolve_account(conn, card_identifier)
    if source["normal_side"] != "asset":
        raise ValueError("Card payments must come from an asset account.")
    if card["account_type"] != "credit_card" or card["normal_side"] != "liability":
        raise ValueError("The destination must be a credit-card account.")
    merged_tags = ["credit-card-payment", *parse_tags(tags)]
    with conn:
        return _create_transaction(
            conn, transaction_type="card_payment", amount_minor=amount_minor,
            description=description,
            entries=[(source, -amount_minor, "payment_source"),
                     (card, -amount_minor, "liability_reduction")],
            tags=merged_tags, occurred_at=occurred_at,
            original_message=original_message,
        )


def record_refund(
    conn: sqlite3.Connection,
    amount_minor: int,
    description: str,
    account_identifier: str,
    *,
    bucket: str | None = None,
    tags: Iterable[str] = (),
    occurred_at: str | None = None,
    related_public_id: str | None = None,
    original_message: str | None = None,
    wants_category_identifier: str | None = None,
) -> dict[str, Any]:
    account = resolve_account(conn, account_identifier)
    delta = amount_minor if account["normal_side"] == "asset" else -amount_minor
    related_id = None
    if related_public_id:
        related = conn.execute(
            "SELECT id, bucket FROM transactions WHERE public_id = ?", (related_public_id,)
        ).fetchone()
        if related is None:
            raise ValueError(f"Unknown related transaction: {related_public_id}")
        related_id = related["id"]
        if bucket is None:
            bucket = related["bucket"]
        if bucket == "wants" and wants_category_identifier is None:
            allocation = conn.execute(
                """SELECT wc.slug FROM wants_allocations wa
                JOIN wants_categories wc ON wc.id = wa.category_id
                WHERE wa.transaction_id = ?""", (related_id,),
            ).fetchone()
            if allocation:
                wants_category_identifier = allocation["slug"]
    if wants_category_identifier and bucket != "wants":
        raise ValueError("A wants category can only be used with the wants bucket.")
    merged_tags = ["refund", *parse_tags(tags)]
    with conn:
        transaction = _create_transaction(
            conn, transaction_type="refund", amount_minor=amount_minor,
            description=description, entries=[(account, delta, "refund")],
            bucket=bucket, tags=merged_tags, occurred_at=occurred_at,
            related_transaction_id=related_id, original_message=original_message,
        )
        if bucket == "wants":
            assign_wants_category(
                conn, transaction["transaction_id"], wants_category_identifier or "wants"
            )
            transaction = transaction_payload(conn, transaction["transaction_id"])
        return transaction


def undo_transaction(
    conn: sqlite3.Connection,
    public_id: str,
    *,
    reason: str = "User-requested correction",
    original_message: str | None = None,
) -> dict[str, Any]:
    original = conn.execute(
        "SELECT * FROM transactions WHERE public_id = ?", (public_id,)
    ).fetchone()
    if original is None:
        raise ValueError(f"Unknown transaction: {public_id}")
    if original["status"] == "reversed":
        raise ValueError(f"Transaction is already reversed: {public_id}")
    if original["transaction_type"] == "reversal":
        raise ValueError("Reverse the replacement transaction instead of reversing a reversal.")
    entry_rows = conn.execute(
        """
        SELECT e.balance_delta_minor, a.id AS account_id
        FROM account_entries AS e
        JOIN accounts AS a ON a.id = e.account_id
        WHERE e.transaction_id = ? ORDER BY e.id
        """,
        (original["id"],),
    ).fetchall()
    tags = [row["display_name"] for row in conn.execute(
        """
        SELECT t.display_name FROM tags AS t
        JOIN transaction_tags AS tt ON tt.tag_id = t.id
        WHERE tt.transaction_id = ?
        """,
        (original["id"],),
    ).fetchall()]
    entries = []
    for row in entry_rows:
        account = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (row["account_id"],)
        ).fetchone()
        entries.append((account, -row["balance_delta_minor"], "reversal"))
    with conn:
        reversal = _create_transaction(
            conn, transaction_type="reversal", amount_minor=original["amount_minor"],
            description=f"Reversal of {public_id}: {reason}", entries=entries,
            bucket=original["bucket"], tags=tags,
            occurred_at=None, original_message=original_message,
            reversal_of_id=original["id"],
        )
        conn.execute(
            "UPDATE transactions SET status = 'reversed', updated_at = ? WHERE id = ?",
            (now_iso(), original["id"]),
        )
        audit(conn, "transaction_reversed", "transaction", public_id,
              {"reversal_id": reversal["transaction_id"], "reason": reason})
    return {
        "reversed_transaction_id": public_id,
        "reversal": transaction_payload(conn, reversal["transaction_id"]),
    }


def recent_transactions(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT public_id FROM transactions ORDER BY occurred_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [transaction_payload(conn, row["public_id"]) for row in rows]


def financial_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    position = conn.execute("SELECT * FROM v_financial_position").fetchone()
    accounts = list_accounts(conn)

    def unknown_names(predicate: Any) -> list[str]:
        return [a["name"] for a in accounts if predicate(a) and not a["balance_known"]]

    available_unknown = unknown_names(lambda a: a["available_cash"])
    savings_unknown = unknown_names(lambda a: a["type"] == "savings")
    receivables_unknown = unknown_names(lambda a: a["slug"] == "receivables")
    asset_unknown = unknown_names(lambda a: a["side"] == "asset")
    liability_unknown = unknown_names(lambda a: a["side"] == "liability")
    net_known = position["assets_known_minor"] - position["liabilities_known_minor"]

    def block(known_minor: int, unknown: list[str]) -> dict[str, Any]:
        complete = not unknown
        return {
            "complete": complete,
            "amount_minor": known_minor if complete else None,
            "amount": format_money(known_minor) if complete else None,
            "known_component_minor": known_minor,
            "known_component": format_money(known_minor),
            "unknown_accounts": unknown,
        }

    return {
        "available_cash": block(position["available_cash_known_minor"], available_unknown),
        "savings": block(position["savings_known_minor"], savings_unknown),
        "receivables": block(
            sum(
                a["balance_minor"] or 0
                for a in accounts
                if a["slug"] == "receivables"
            ),
            receivables_unknown,
        ),
        "credit_card_debt": block(position["liabilities_known_minor"], liability_unknown),
        "net_worth": block(net_known, [*asset_unknown, *liability_unknown]),
        "accounts": accounts,
    }


def activity_summary(
    conn: sqlite3.Connection,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    conditions = ["status = 'posted'"]
    params: list[Any] = []
    if from_date:
        conditions.append("occurred_at >= ?")
        params.append(occurred_iso(from_date if "T" in from_date else f"{from_date}T00:00:00+08:00"))
    if to_date:
        conditions.append("occurred_at <= ?")
        params.append(occurred_iso(to_date if "T" in to_date else f"{to_date}T23:59:59+08:00"))
    where = " AND ".join(conditions)
    rows = conn.execute(
        f"""
        SELECT transaction_type, bucket, SUM(amount_minor) AS total
        FROM transactions WHERE {where}
        GROUP BY transaction_type, bucket
        """,
        params,
    ).fetchall()
    income = 0
    needs = 0
    apartment = 0
    wants = 0
    buffer = 0
    savings = 0
    refunds = 0
    for row in rows:
        if row["transaction_type"] == "income":
            income += row["total"]
        elif row["transaction_type"] == "expense" and row["bucket"] == "needs":
            needs += row["total"]
        elif row["transaction_type"] == "expense" and row["bucket"] == "apartment":
            apartment += row["total"]
        elif row["transaction_type"] == "expense" and row["bucket"] == "wants":
            wants += row["total"]
        elif row["transaction_type"] == "expense" and row["bucket"] == "buffer":
            buffer += row["total"]
        elif row["transaction_type"] == "transfer" and row["bucket"] == "savings":
            savings += row["total"]
        elif row["transaction_type"] == "refund":
            refunds += row["total"]
            if row["bucket"] == "needs":
                needs -= row["total"]
            elif row["bucket"] == "apartment":
                apartment -= row["total"]
            elif row["bucket"] == "wants":
                wants -= row["total"]
            elif row["bucket"] == "buffer":
                buffer -= row["total"]
    return {
        "window": {"from": from_date, "to": to_date},
        "income_minor": income,
        "income": format_money(income),
        "expenses": {
            "needs_minor": needs,
            "needs": format_money(needs),
            "apartment_minor": apartment,
            "apartment": format_money(apartment),
            "wants_minor": wants,
            "wants": format_money(wants),
            "buffer_minor": buffer,
            "buffer": format_money(buffer),
            "total_minor": needs + apartment + wants + buffer,
            "total": format_money(needs + apartment + wants + buffer),
        },
        "savings_allocated_minor": savings,
        "savings_allocated": format_money(savings),
        "refunds_minor": refunds,
        "refunds": format_money(refunds),
    }


def _month_totals(
    conn: sqlite3.Connection,
    month: str,
    as_of_iso: str | None = None,
) -> sqlite3.Row:
    """Aggregate posted ledger activity for one YYYY-MM month.

    With `as_of_iso` set, only transactions on or before that date count.
    Without it, every posted transaction in the month counts (full month).
    """
    sql = """
        SELECT
            COALESCE(SUM(CASE
                WHEN tx.transaction_type = 'income' THEN tx.amount_minor ELSE 0 END), 0) AS income,
            COALESCE(SUM(CASE
                WHEN tx.transaction_type = 'expense' AND tx.bucket = 'needs'
                     AND NOT EXISTS (
                         SELECT 1 FROM transaction_tags AS tt
                         JOIN tags AS tag ON tag.id = tt.tag_id
                         WHERE tt.transaction_id = tx.id AND tag.slug = 'apartment'
                     ) THEN tx.amount_minor
                WHEN tx.transaction_type = 'refund' AND tx.bucket = 'needs'
                     AND NOT EXISTS (
                         SELECT 1 FROM transaction_tags AS tt
                         JOIN tags AS tag ON tag.id = tt.tag_id
                         WHERE tt.transaction_id = tx.id AND tag.slug = 'apartment'
                     ) THEN -tx.amount_minor
                ELSE 0 END), 0) AS needs,
            COALESCE(SUM(CASE
                WHEN tx.transaction_type = 'expense' AND (
                    tx.bucket = 'apartment' OR (
                        tx.bucket = 'needs' AND EXISTS (
                            SELECT 1 FROM transaction_tags AS tt
                            JOIN tags AS tag ON tag.id = tt.tag_id
                            WHERE tt.transaction_id = tx.id AND tag.slug = 'apartment'
                        )
                    )
                ) THEN tx.amount_minor
                WHEN tx.transaction_type = 'refund' AND (
                    tx.bucket = 'apartment' OR (
                        tx.bucket = 'needs' AND EXISTS (
                            SELECT 1 FROM transaction_tags AS tt
                            JOIN tags AS tag ON tag.id = tt.tag_id
                            WHERE tt.transaction_id = tx.id AND tag.slug = 'apartment'
                        )
                    )
                ) THEN -tx.amount_minor
                ELSE 0 END), 0) AS apartment,
            COALESCE(SUM(CASE
                WHEN tx.transaction_type = 'expense' AND tx.bucket = 'wants' THEN tx.amount_minor
                WHEN tx.transaction_type = 'refund' AND tx.bucket = 'wants' THEN -tx.amount_minor
                ELSE 0 END), 0) AS wants,
            COALESCE(SUM(CASE
                WHEN tx.transaction_type = 'expense' AND tx.bucket = 'buffer' THEN tx.amount_minor
                WHEN tx.transaction_type = 'refund' AND tx.bucket = 'buffer' THEN -tx.amount_minor
                ELSE 0 END), 0) AS buffer,
            COALESCE(SUM(CASE
                WHEN tx.transaction_type = 'transfer' AND tx.bucket = 'savings' THEN tx.amount_minor
                ELSE 0 END), 0) AS savings
        FROM transactions AS tx
        WHERE tx.status = 'posted'
          AND substr(tx.occurred_at, 1, 7) = ?
    """
    params: list[Any] = [month]
    if as_of_iso is not None:
        sql += " AND substr(tx.occurred_at, 1, 10) <= ?"
        params.append(as_of_iso)
    return conn.execute(sql, params).fetchone()


def _month_before(month: str) -> str:
    year, month_number = map(int, month.split("-"))
    if month_number == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month_number - 1:02d}"


def _budget_carry_state(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    month: str,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Opening buffer and category reductions produced by prior months."""
    if month in cache:
        return cache[month]
    pooled = ("needs", "apartment", "wants")
    if month <= plan["created_at"][:7]:
        cache[month] = {"buffer": 0, "debts": {name: 0 for name in pooled}}
        return cache[month]

    previous = _month_before(month)
    opening = _budget_carry_state(conn, plan, previous, cache)
    previous_totals = _month_totals(conn, previous)

    cushion = max(0, opening["buffer"] - previous_totals["buffer"])
    deficits: dict[str, int] = {}
    for name in pooled:
        base = plan["categories"][name]["target_minor"]
        incoming_debt = opening["debts"][name]
        effective_target = max(0, base - incoming_debt)
        debt_beyond_reset = max(0, incoming_debt - base)
        balance = effective_target - previous_totals[name]
        cushion += max(0, balance)
        deficits[name] = debt_beyond_reset + max(0, -balance)

    total_deficit = sum(deficits.values())
    coverage = min(cushion, total_deficit)
    covered = {name: 0 for name in pooled}
    if coverage and total_deficit:
        remainders: list[tuple[int, int, str]] = []
        allocated = 0
        for order, name in enumerate(pooled):
            numerator = deficits[name] * coverage
            covered[name], remainder = divmod(numerator, total_deficit)
            allocated += covered[name]
            remainders.append((remainder, -order, name))
        for _, _, name in sorted(remainders, reverse=True)[:coverage - allocated]:
            covered[name] += 1

    state = {
        "buffer": cushion - coverage,
        "debts": {name: deficits[name] - covered[name] for name in pooled},
    }
    cache[month] = state
    return state


def monthly_budget_summary(
    conn: sqlite3.Connection,
    month: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    plan = budget_plan_payload(conn)
    selected_month = month or datetime.now(LOCAL_TZ).strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", selected_month):
        raise ValueError("Month must be YYYY-MM.")
    try:
        parsed_month = datetime.strptime(selected_month, "%Y-%m")
    except ValueError as exc:
        raise ValueError("Month must be a valid YYYY-MM value.") from exc

    year, month_number = parsed_month.year, parsed_month.month
    days_in_month = calendar.monthrange(year, month_number)[1]
    today = datetime.now(LOCAL_TZ).date()
    if as_of is not None:
        try:
            as_of_date = date.fromisoformat(as_of)
        except ValueError as exc:
            raise ValueError("As-of date must be YYYY-MM-DD.") from exc
        if as_of_date.strftime("%Y-%m") != selected_month:
            raise ValueError("As-of date must fall within the selected month.")
    elif selected_month == today.strftime("%Y-%m"):
        as_of_date = today
    elif selected_month < today.strftime("%Y-%m"):
        as_of_date = date(year, month_number, days_in_month)
    else:
        as_of_date = date(year, month_number, 1)
    elapsed_days = as_of_date.day

    totals = _month_totals(conn, selected_month, as_of_date.isoformat())
    carry = _budget_carry_state(conn, plan, selected_month, {})

    categories: dict[str, dict[str, Any]] = {}
    for name in ("needs", "apartment", "wants", "buffer", "savings"):
        configured = plan["categories"][name]
        rollover_minor = carry["buffer"] if name == "buffer" else 0
        base_target_minor = configured["target_minor"]
        carry_reduction_minor = carry["debts"].get(name, 0)
        target_minor = (
            base_target_minor + rollover_minor
            if name == "buffer" else max(0, base_target_minor - carry_reduction_minor)
        )
        actual_minor = totals[name]
        remaining_minor = target_minor - actual_minor
        if name in {"apartment", "buffer"}:
            pace_target_minor = None
            pace_variance_minor = None
            buffer_minor = None
            pace_status = "not_applicable"
        else:
            pace_target_minor = (
                target_minor * elapsed_days + days_in_month // 2
            ) // days_in_month
            pace_variance_minor = actual_minor - pace_target_minor
            buffer_minor = (
                pace_variance_minor if name == "savings" else -pace_variance_minor
            )
            if name == "savings" and pace_variance_minor < 0:
                pace_status = "short_of_pace"
            elif name == "savings" and pace_variance_minor > 0:
                pace_status = "above_pace"
            elif name != "savings" and pace_variance_minor < 0:
                pace_status = "under_pace"
            elif name != "savings" and pace_variance_minor > 0:
                pace_status = "over_pace"
            else:
                pace_status = "on_pace"
        categories[name] = {
            **configured,
            "base_target_minor": base_target_minor,
            "base_target": format_money(base_target_minor),
            "rollover_minor": rollover_minor,
            "rollover": format_money(rollover_minor),
            "carry_reduction_minor": carry_reduction_minor,
            "carry_reduction": format_money(carry_reduction_minor),
            "target_minor": target_minor,
            "target": format_money(target_minor),
            "actual_minor": actual_minor,
            "actual": format_money(actual_minor),
            "remaining_minor": remaining_minor,
            "remaining": format_money(remaining_minor),
            "within_monthly_budget": remaining_minor >= 0,
            "pace_target_minor": pace_target_minor,
            "pace_target": format_money(pace_target_minor),
            "pace_status": pace_status,
            "pace_difference_minor": (
                abs(pace_variance_minor) if pace_variance_minor is not None else None
            ),
            "pace_difference": (
                format_money(abs(pace_variance_minor))
                if pace_variance_minor is not None else None
            ),
            "pace_applicable": name not in {"apartment", "buffer"},
            "buffer_minor": buffer_minor,
            "buffer": format_signed_money(buffer_minor),
        }

    return {
        "month": selected_month,
        "as_of": as_of_date.isoformat(),
        "elapsed_days": elapsed_days,
        "days_in_month": days_in_month,
        "planned_income_minor": plan["monthly_income_minor"],
        "planned_income": plan["monthly_income"],
        "actual_income_minor": totals["income"],
        "actual_income": format_money(totals["income"]),
        "categories": categories,
        "auto_save_salary": plan["auto_save_salary"],
        "savings_account": plan["savings_account"],
    }


def classify_text(conn: sqlite3.Connection, text: str) -> list[dict[str, Any]]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    matches = []
    for row in conn.execute(
        "SELECT * FROM classification_rules WHERE active = 1 ORDER BY user_confirmed DESC, priority DESC, id"
    ):
        pattern = row["pattern"].casefold()
        if row["match_type"] == "exact":
            matched = normalized.strip() == pattern.strip()
        elif row["match_type"] == "word":
            matched = re.search(rf"(?<!\w){re.escape(pattern)}(?!\w)", normalized) is not None
        else:
            matched = pattern in normalized
        if matched:
            matches.append({
                "pattern": row["pattern"],
                "match_type": row["match_type"],
                "bucket": row["bucket"],
                "tags": json.loads(row["tags_json"]),
                "rationale": row["rationale"],
                "user_confirmed": bool(row["user_confirmed"]),
                "priority": row["priority"],
            })
    return matches


def set_rule(
    conn: sqlite3.Connection,
    pattern: str,
    match_type: str,
    bucket: str,
    tags: Iterable[str],
    rationale: str,
    priority: int,
    user_confirmed: bool,
) -> dict[str, Any]:
    stamp = now_iso()
    parsed_tags = parse_tags(tags)
    with conn:
        conn.execute(
            """
            INSERT INTO classification_rules(
                pattern, match_type, bucket, tags_json, rationale,
                user_confirmed, priority, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(pattern, match_type) DO UPDATE SET
                bucket = excluded.bucket,
                tags_json = excluded.tags_json,
                rationale = excluded.rationale,
                user_confirmed = excluded.user_confirmed,
                priority = excluded.priority,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (pattern.strip(), match_type, bucket, json.dumps(parsed_tags), rationale,
             int(user_confirmed), priority, stamp, stamp),
        )
        row = conn.execute(
            "SELECT id FROM classification_rules WHERE pattern = ? AND match_type = ?",
            (pattern.strip(), match_type),
        ).fetchone()
        audit(conn, "classification_rule_set", "classification_rule", row["id"],
              {"pattern": pattern, "bucket": bucket, "tags": parsed_tags})
    return classify_text(conn, pattern)[0]


def list_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{
        "pattern": row["pattern"],
        "match_type": row["match_type"],
        "bucket": row["bucket"],
        "tags": json.loads(row["tags_json"]),
        "rationale": row["rationale"],
        "user_confirmed": bool(row["user_confirmed"]),
        "priority": row["priority"],
        "active": bool(row["active"]),
    } for row in conn.execute(
        "SELECT * FROM classification_rules ORDER BY user_confirmed DESC, priority DESC, id"
    )]


def doctor(conn: sqlite3.Connection) -> dict[str, Any]:
    quick_check = [row[0] for row in conn.execute("PRAGMA quick_check")]
    foreign_key_issues = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
    empty_transactions = [row["public_id"] for row in conn.execute(
        """
        SELECT t.public_id FROM transactions AS t
        LEFT JOIN account_entries AS e ON e.transaction_id = t.id
        GROUP BY t.id HAVING COUNT(e.id) = 0
        """
    )]
    unknown_balances = [account["name"] for account in list_accounts(conn)
                        if not account["balance_known"]]
    errors = []
    if quick_check != ["ok"]:
        errors.append({"quick_check": quick_check})
    if foreign_key_issues:
        errors.append({"foreign_key_issues": foreign_key_issues})
    if empty_transactions:
        errors.append({"transactions_without_entries": empty_transactions})
    return {
        "ok": not errors,
        "schema_version": conn.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        ).fetchone()["value"],
        "errors": errors,
        "warnings": ({"unknown_account_balances": unknown_balances}
                     if unknown_balances else {}),
    }


def backup_database(conn: sqlite3.Connection, db_path: Path, output: str | None = None) -> Path:
    if output:
        destination = Path(output).expanduser().resolve()
    else:
        stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
        destination = PROJECT_ROOT / "backups" / f"financer-{stamp}.sqlite3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination == db_path.resolve():
        raise ValueError("Backup destination must differ from the live database.")
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
    finally:
        target.close()
    return destination


def emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Financer local ledger")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create/upgrade the database and seed initial accounts")
    sub.add_parser("accounts", help="List account configuration and balances")
    sub.add_parser("summary", help="Show available cash, savings, debt, and net worth")
    p = sub.add_parser("budget", help="Show the monthly budget plan and remaining allowances")
    p.add_argument("--month", help="Calendar month in YYYY-MM format")
    p.add_argument("--as-of", help="Pacing date in YYYY-MM-DD format")

    p = sub.add_parser("budget-set", help="Configure monthly budget targets")
    p.add_argument("monthly_income")
    p.add_argument("--needs", required=True, help="Needs percentage")
    p.add_argument("--wants", required=True, help="Wants percentage")
    p.add_argument("--savings", required=True, help="Savings percentage")
    p.add_argument("--apartment", default="0",
                   help="Fixed rent and utilities allocation taken from needs")
    p.add_argument("--savings-account", default="savings")
    auto_save = p.add_mutually_exclusive_group()
    auto_save.add_argument("--auto-save-salary", action="store_true", dest="auto_save_salary")
    auto_save.add_argument("--no-auto-save-salary", action="store_false", dest="auto_save_salary")
    p.set_defaults(auto_save_salary=True)
    sub.add_parser("rules", help="List classification rules")
    sub.add_parser("doctor", help="Check database integrity and ledger invariants")

    p = sub.add_parser("account-add", help="Add a cash, savings, card, or other account")
    p.add_argument("name")
    p.add_argument("--slug")
    p.add_argument("--type", required=True, choices=[
        "cash", "savings", "credit_card", "investment", "other_asset", "other_liability"
    ])
    p.add_argument("--balance")
    p.add_argument("--available-cash", action="store_true")
    p.add_argument("--alias", action="append", default=[])

    p = sub.add_parser("account-set", help="Set current balance or card details")
    p.add_argument("account")
    p.add_argument("--name")
    p.add_argument("--balance")
    p.add_argument("--limit")
    p.add_argument("--statement-day", type=int, choices=range(1, 32), metavar="1-31")
    p.add_argument("--due-day", type=int, choices=range(1, 32), metavar="1-31")
    p.add_argument("--next-due-date")
    p.add_argument("--notes")
    available = p.add_mutually_exclusive_group()
    available.add_argument("--available-cash", action="store_true", dest="available_cash")
    available.add_argument("--not-available-cash", action="store_false", dest="available_cash")
    p.set_defaults(available_cash=None)

    p = sub.add_parser("expense", help="Record a needs, apartment, wants, or buffer expense")
    p.add_argument("amount")
    p.add_argument("description")
    p.add_argument("--account", default="cash-on-hand")
    p.add_argument("--bucket", required=True, choices=["needs", "apartment", "wants", "buffer"])
    p.add_argument("--wants-category")
    p.add_argument("--tags", default="")
    p.add_argument("--merchant")
    p.add_argument("--date")
    p.add_argument("--original-message")

    p = sub.add_parser("income", help="Record actual income")
    p.add_argument("amount")
    p.add_argument("description")
    p.add_argument("--account", default="cash-on-hand")
    p.add_argument("--source")
    p.add_argument("--tags", default="")
    p.add_argument("--date")
    p.add_argument("--original-message")
    p.add_argument("--salary", action="store_true",
                   help="Apply the configured automatic salary savings allocation")

    p = sub.add_parser("transfer", help="Move money between asset accounts")
    p.add_argument("amount")
    p.add_argument("--from", required=True, dest="source")
    p.add_argument("--to", required=True, dest="destination")
    p.add_argument("--description", default="Transfer")
    p.add_argument("--tags", default="")
    p.add_argument("--savings-category", help="Savings category for transfers into Savings")
    p.add_argument("--date")
    p.add_argument("--original-message")

    p = sub.add_parser("savings-categories", help="List savings categories and balances")
    p.add_argument("--all", action="store_true", help="Include retired categories")

    p = sub.add_parser("savings-category-add", help="Add or restore a savings category")
    p.add_argument("name")

    p = sub.add_parser("savings-category-remove", help="Retire a savings category without changing history")
    p.add_argument("category")

    p = sub.add_parser("wants-categories", help="List wants categories and spending")
    p.add_argument("--all", action="store_true", help="Include retired categories")
    p = sub.add_parser("wants-category-add", help="Add or restore a wants category")
    p.add_argument("name")
    p = sub.add_parser("wants-category-remove", help="Retire a wants category without changing history")
    p.add_argument("category")

    p = sub.add_parser("savings-goals", help="List savings goals and progress")
    p.add_argument("--all", action="store_true", help="Include retired goals")

    p = sub.add_parser("savings-goal-add", help="Set a savings goal")
    p.add_argument("name")
    p.add_argument("--target", required=True)
    p.add_argument("--category", default="savings")
    p.add_argument("--target-date")
    p.add_argument("--notes")

    p = sub.add_parser("savings-goal-remove", help="Retire a savings goal")
    p.add_argument("goal")

    p = sub.add_parser("receivable-add", help="Record money owed to the user")
    p.add_argument("amount")
    p.add_argument("debtor")
    p.add_argument("--description")
    p.add_argument("--tags", default="")
    p.add_argument("--date")
    p.add_argument("--original-message")

    p = sub.add_parser("receivable-payment", help="Record repayment of money owed")
    p.add_argument("amount")
    p.add_argument("debtor")
    p.add_argument("--to", default="cash-on-hand", dest="destination")
    p.add_argument("--description")
    p.add_argument("--tags", default="")
    p.add_argument("--date")
    p.add_argument("--original-message")

    p = sub.add_parser("card-payment", help="Pay a credit-card liability from cash")
    p.add_argument("amount")
    p.add_argument("--from", default="cash-on-hand", dest="source")
    p.add_argument("--card", required=True)
    p.add_argument("--description", default="Credit-card payment")
    p.add_argument("--tags", default="")
    p.add_argument("--date")
    p.add_argument("--original-message")

    p = sub.add_parser("refund", help="Record a refund without treating it as income")
    p.add_argument("amount")
    p.add_argument("description")
    p.add_argument("--account", required=True)
    p.add_argument("--bucket", choices=["needs", "apartment", "wants", "buffer"])
    p.add_argument("--wants-category")
    p.add_argument("--related")
    p.add_argument("--tags", default="")
    p.add_argument("--date")
    p.add_argument("--original-message")

    p = sub.add_parser("undo", help="Reverse a posted transaction without deleting it")
    p.add_argument("transaction_id")
    p.add_argument("--reason", default="User-requested correction")
    p.add_argument("--original-message")

    p = sub.add_parser("recent", help="Show recent transactions")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("transaction-comment-add", help="Add a comment to a transaction")
    p.add_argument("transaction_id")
    p.add_argument("comment")

    p = sub.add_parser("transaction-comments", help="List comments on a transaction")
    p.add_argument("transaction_id")
    p.add_argument("--all", action="store_true", help="Include removed comments")

    p = sub.add_parser("transaction-comment-remove", help="Retire a transaction comment")
    p.add_argument("comment_id")

    p = sub.add_parser("activity", help="Summarize income, spending, and savings allocations")
    p.add_argument("--from-date")
    p.add_argument("--to-date")

    p = sub.add_parser("classify", help="Find durable classification precedents for text")
    p.add_argument("text")

    p = sub.add_parser("rule-set", help="Add or update a durable classification rule")
    p.add_argument("pattern")
    p.add_argument("--match-type", choices=["contains", "exact", "word"], default="word")
    p.add_argument(
        "--bucket", required=True,
        choices=["needs", "apartment", "wants", "buffer", "savings"],
    )
    p.add_argument("--tags", required=True)
    p.add_argument("--rationale", required=True)
    p.add_argument("--priority", type=int, default=200)
    p.add_argument("--inferred", action="store_true",
                   help="Mark as agent-inferred rather than user-confirmed")

    p = sub.add_parser("backup", help="Create a consistent SQLite backup")
    p.add_argument("--output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db).expanduser().resolve()
    init_db(db_path)
    if args.command == "init":
        conn = connect(db_path)
        try:
            emit({"status": "initialized", "database": str(db_path),
                  "accounts": list_accounts(conn), "rules": list_rules(conn)})
        finally:
            conn.close()
        return 0

    conn = connect(db_path)
    try:
        if args.command == "accounts":
            result = list_accounts(conn)
        elif args.command == "summary":
            result = financial_summary(conn)
        elif args.command == "budget":
            result = monthly_budget_summary(conn, args.month, args.as_of)
        elif args.command == "budget-set":
            result = set_budget_plan(
                conn, parse_minor(args.monthly_income),
                parse_basis_points(args.needs), parse_basis_points(args.wants),
                parse_basis_points(args.savings),
                apartment_minor=parse_minor(args.apartment, allow_zero=True),
                savings_account_identifier=args.savings_account,
                auto_save_salary=args.auto_save_salary,
            )
        elif args.command == "rules":
            result = list_rules(conn)
        elif args.command == "doctor":
            result = doctor(conn)
        elif args.command == "account-add":
            result = add_account(
                conn, args.name, args.type, slug=args.slug,
                balance_minor=parse_minor(args.balance, allow_zero=True) if args.balance is not None else None,
                available_cash=args.available_cash, aliases=args.alias,
            )
        elif args.command == "account-set":
            if args.balance is not None:
                balance_acct = None
                try:
                    balance_acct = resolve_account(conn, args.account)
                except Exception:
                    balance_acct = None
                balance_minor = parse_minor(
                    args.balance,
                    allow_zero=True,
                    allow_negative=bool(balance_acct and balance_acct["normal_side"] == "liability"),
                )
            else:
                balance_minor = None
            result = set_account(
                conn, args.account, name=args.name,
                balance_minor=balance_minor,
                set_balance=args.balance is not None,
                credit_limit_minor=parse_minor(args.limit, allow_zero=True) if args.limit is not None else None,
                set_limit=args.limit is not None,
                statement_day=args.statement_day, due_day=args.due_day,
                next_due_date=args.next_due_date, notes=args.notes,
                available_cash=args.available_cash,
            )
        elif args.command == "expense":
            result = record_expense(
                conn, parse_minor(args.amount), args.description, args.account, args.bucket,
                tags=parse_tags(args.tags), occurred_at=args.date,
                merchant=args.merchant, original_message=args.original_message,
                wants_category_identifier=args.wants_category,
            )
        elif args.command == "income":
            recorder = record_salary if args.salary else record_income
            result = recorder(
                conn, parse_minor(args.amount), args.description, args.account,
                source=args.source, tags=parse_tags(args.tags), occurred_at=args.date,
                original_message=args.original_message,
            )
        elif args.command == "transfer":
            result = record_transfer(
                conn, parse_minor(args.amount), args.source, args.destination,
                description=args.description, tags=parse_tags(args.tags),
                savings_category_identifier=args.savings_category,
                occurred_at=args.date, original_message=args.original_message,
            )
        elif args.command == "savings-categories":
            result = list_savings_categories(conn, args.all)
        elif args.command == "savings-category-add":
            result = add_savings_category(conn, args.name)
        elif args.command == "savings-category-remove":
            result = remove_savings_category(conn, args.category)
        elif args.command == "wants-categories":
            result = list_wants_categories(conn, args.all)
        elif args.command == "wants-category-add":
            result = add_wants_category(conn, args.name)
        elif args.command == "wants-category-remove":
            result = remove_wants_category(conn, args.category)
        elif args.command == "savings-goals":
            result = list_savings_goals(conn, args.all)
        elif args.command == "savings-goal-add":
            result = add_savings_goal(
                conn, args.name, parse_minor(args.target), args.category,
                target_date=args.target_date, notes=args.notes,
            )
        elif args.command == "savings-goal-remove":
            result = remove_savings_goal(conn, args.goal)
        elif args.command == "card-payment":
            result = record_card_payment(
                conn, parse_minor(args.amount), args.source, args.card,
                description=args.description, tags=parse_tags(args.tags),
                occurred_at=args.date, original_message=args.original_message,
            )
        elif args.command == "refund":
            result = record_refund(
                conn, parse_minor(args.amount), args.description, args.account,
                bucket=args.bucket, tags=parse_tags(args.tags), occurred_at=args.date,
                related_public_id=args.related, original_message=args.original_message,
                wants_category_identifier=args.wants_category,
            )
        elif args.command == "undo":
            result = undo_transaction(
                conn, args.transaction_id, reason=args.reason,
                original_message=args.original_message,
            )
        elif args.command == "receivable-add":
            result = record_receivable(
                conn, parse_minor(args.amount), args.debtor,
                description=args.description, tags=parse_tags(args.tags),
                occurred_at=args.date, original_message=args.original_message,
            )
        elif args.command == "receivable-payment":
            result = record_receivable_payment(
                conn, parse_minor(args.amount), args.debtor, args.destination,
                description=args.description, tags=parse_tags(args.tags),
                occurred_at=args.date, original_message=args.original_message,
            )
        elif args.command == "recent":
            if args.limit < 1 or args.limit > 500:
                raise ValueError("Recent limit must be between 1 and 500.")
            result = recent_transactions(conn, args.limit)
        elif args.command == "transaction-comment-add":
            result = add_transaction_comment(conn, args.transaction_id, args.comment)
        elif args.command == "transaction-comments":
            result = list_transaction_comments(conn, args.transaction_id, args.all)
        elif args.command == "transaction-comment-remove":
            result = remove_transaction_comment(conn, args.comment_id)
        elif args.command == "activity":
            result = activity_summary(conn, args.from_date, args.to_date)
        elif args.command == "classify":
            result = {"text": args.text, "matches": classify_text(conn, args.text)}
        elif args.command == "rule-set":
            result = set_rule(
                conn, args.pattern, args.match_type, args.bucket, parse_tags(args.tags),
                args.rationale, args.priority, not args.inferred,
            )
        elif args.command == "backup":
            destination = backup_database(conn, db_path, args.output)
            result = {"backup": str(destination)}
        else:
            parser.error(f"Unhandled command: {args.command}")
            return 2
        emit({"status": "ok", "result": result})
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        emit({"status": "error", "error": str(exc)})
        raise SystemExit(1)
