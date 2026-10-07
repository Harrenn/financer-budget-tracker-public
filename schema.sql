PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    account_type TEXT NOT NULL CHECK (
        account_type IN (
            'cash', 'savings', 'credit_card', 'investment',
            'other_asset', 'other_liability'
        )
    ),
    normal_side TEXT NOT NULL CHECK (normal_side IN ('asset', 'liability')),
    currency TEXT NOT NULL DEFAULT 'PHP' CHECK (length(currency) = 3),
    is_available_cash INTEGER NOT NULL DEFAULT 0 CHECK (is_available_cash IN (0, 1)),
    include_in_net_worth INTEGER NOT NULL DEFAULT 1 CHECK (include_in_net_worth IN (0, 1)),
    balance_base_minor INTEGER,
    balance_set_at TEXT,
    credit_limit_minor INTEGER CHECK (credit_limit_minor IS NULL OR credit_limit_minor >= 0),
    statement_day INTEGER CHECK (statement_day IS NULL OR statement_day BETWEEN 1 AND 31),
    due_day INTEGER CHECK (due_day IS NULL OR due_day BETWEEN 1 AND 31),
    next_due_date TEXT,
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_aliases (
    alias TEXT PRIMARY KEY COLLATE NOCASE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS transactions (
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
    bucket TEXT CHECK (bucket IS NULL OR bucket IN ('needs', 'apartment', 'wants', 'buffer', 'savings')),
    original_message TEXT,
    status TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted', 'reversed')),
    related_transaction_id INTEGER REFERENCES transactions(id),
    reversal_of_id INTEGER UNIQUE REFERENCES transactions(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_entries (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    balance_delta_minor INTEGER NOT NULL CHECK (balance_delta_minor <> 0),
    entry_role TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transaction_tags (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (transaction_id, tag_id)
);

CREATE TABLE IF NOT EXISTS transaction_comments (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    body TEXT NOT NULL CHECK (length(trim(body)) > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS savings_categories (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS savings_allocations (
    transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id),
    category_id INTEGER NOT NULL REFERENCES savings_categories(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS savings_goals (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES savings_categories(id),
    target_minor INTEGER NOT NULL CHECK (target_minor > 0),
    target_date TEXT,
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wants_categories (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wants_allocations (
    transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id),
    category_id INTEGER NOT NULL REFERENCES wants_categories(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classification_rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL COLLATE NOCASE,
    match_type TEXT NOT NULL DEFAULT 'contains' CHECK (match_type IN ('contains', 'exact', 'word')),
    bucket TEXT NOT NULL CHECK (bucket IN ('needs', 'apartment', 'wants', 'buffer', 'savings')),
    tags_json TEXT NOT NULL DEFAULT '[]',
    rationale TEXT NOT NULL,
    user_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (user_confirmed IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (pattern, match_type)
);

CREATE TABLE IF NOT EXISTS budget_plans (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    monthly_income_minor INTEGER NOT NULL CHECK (monthly_income_minor > 0),
    needs_bps INTEGER NOT NULL CHECK (needs_bps BETWEEN 0 AND 10000),
    wants_bps INTEGER NOT NULL CHECK (wants_bps BETWEEN 0 AND 10000),
    savings_bps INTEGER NOT NULL CHECK (savings_bps BETWEEN 0 AND 10000),
    apartment_minor INTEGER NOT NULL DEFAULT 0 CHECK (apartment_minor >= 0),
    auto_save_salary INTEGER NOT NULL DEFAULT 1 CHECK (auto_save_salary IN (0, 1)),
    savings_account_id INTEGER NOT NULL REFERENCES accounts(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (needs_bps + wants_bps + savings_bps = 10000)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transactions_occurred_at ON transactions(occurred_at);
CREATE INDEX IF NOT EXISTS idx_transactions_type_status ON transactions(transaction_type, status);
CREATE INDEX IF NOT EXISTS idx_account_entries_account ON account_entries(account_id);
CREATE INDEX IF NOT EXISTS idx_account_entries_transaction ON account_entries(transaction_id);
CREATE INDEX IF NOT EXISTS idx_transaction_comments_transaction ON transaction_comments(transaction_id);
CREATE INDEX IF NOT EXISTS idx_savings_allocations_category ON savings_allocations(category_id);
CREATE INDEX IF NOT EXISTS idx_savings_goals_category ON savings_goals(category_id);
CREATE INDEX IF NOT EXISTS idx_wants_allocations_category ON wants_allocations(category_id);

CREATE VIEW IF NOT EXISTS v_account_balances AS
SELECT
    a.id,
    a.slug,
    a.name,
    a.account_type,
    a.normal_side,
    a.currency,
    a.is_available_cash,
    a.include_in_net_worth,
    a.balance_base_minor,
    COALESCE(SUM(e.balance_delta_minor), 0) AS activity_minor,
    CASE
        WHEN a.balance_base_minor IS NULL THEN NULL
        ELSE a.balance_base_minor + COALESCE(SUM(e.balance_delta_minor), 0)
    END AS current_balance_minor,
    a.balance_set_at,
    a.credit_limit_minor,
    a.statement_day,
    a.due_day,
    a.next_due_date,
    a.active
FROM accounts AS a
LEFT JOIN account_entries AS e ON e.account_id = a.id
GROUP BY a.id;

CREATE VIEW IF NOT EXISTS v_financial_position AS
SELECT
    COALESCE(SUM(CASE
        WHEN is_available_cash = 1 AND current_balance_minor IS NOT NULL
        THEN current_balance_minor ELSE 0 END), 0) AS available_cash_known_minor,
    SUM(CASE
        WHEN is_available_cash = 1 AND current_balance_minor IS NULL
        THEN 1 ELSE 0 END) AS available_cash_unknown_count,
    COALESCE(SUM(CASE
        WHEN account_type = 'savings' AND current_balance_minor IS NOT NULL
        THEN current_balance_minor ELSE 0 END), 0) AS savings_known_minor,
    SUM(CASE
        WHEN account_type = 'savings' AND current_balance_minor IS NULL
        THEN 1 ELSE 0 END) AS savings_unknown_count,
    COALESCE(SUM(CASE
        WHEN normal_side = 'asset' AND include_in_net_worth = 1 AND current_balance_minor IS NOT NULL
        THEN current_balance_minor ELSE 0 END), 0) AS assets_known_minor,
    SUM(CASE
        WHEN normal_side = 'asset' AND include_in_net_worth = 1 AND current_balance_minor IS NULL
        THEN 1 ELSE 0 END) AS assets_unknown_count,
    COALESCE(SUM(CASE
        WHEN normal_side = 'liability' AND include_in_net_worth = 1 AND current_balance_minor IS NOT NULL
        THEN current_balance_minor ELSE 0 END), 0) AS liabilities_known_minor,
    SUM(CASE
        WHEN normal_side = 'liability' AND include_in_net_worth = 1 AND current_balance_minor IS NULL
        THEN 1 ELSE 0 END) AS liabilities_unknown_count
FROM v_account_balances
WHERE active = 1;
