# All monetary amounts, people and transactions in this suite are fictional fixtures.
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import finance  # noqa: E402


class FinanceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "test.sqlite3"
        finance.init_db(self.db_path)
        self.conn = finance.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def set_balance(self, account, amount):
        finance.set_account(
            self.conn,
            account,
            balance_minor=finance.parse_minor(amount, allow_zero=True),
            set_balance=True,
        )

    def test_seeded_balances_are_unknown_not_fake_zeroes(self):
        summary = finance.financial_summary(self.conn)
        self.assertFalse(summary["available_cash"]["complete"])
        self.assertFalse(summary["net_worth"]["complete"])
        self.assertIn("Cash on Hand", summary["available_cash"]["unknown_accounts"])
        self.assertIn("Bank A Credit Card", summary["net_worth"]["unknown_accounts"])

    def test_cash_savings_cards_income_and_reversal(self):
        self.set_balance("cash", "1000")
        self.set_balance("savings", "5000")
        self.set_balance("bank-a", "200")
        self.set_balance("bank-b", "0")
        self.set_balance("bank-c", "0")

        lunch = finance.record_expense(
            self.conn, finance.parse_minor("100"), "Lunch", "cash", "needs",
            tags=["food", "meal"], original_message="lunch 100",
        )
        finance.record_expense(
            self.conn, finance.parse_minor("250"), "Headphones", "bank-a", "wants",
            tags=["electronics"],
        )
        finance.record_transfer(
            self.conn, finance.parse_minor("200"), "cash", "savings",
            description="Emergency fund",
        )
        finance.record_card_payment(
            self.conn, finance.parse_minor("100"), "cash", "bank-a",
        )
        finance.record_income(
            self.conn, finance.parse_minor("500"), "Freelance payment", "cash",
            source="Client",
        )

        summary = finance.financial_summary(self.conn)
        self.assertTrue(summary["net_worth"]["complete"])
        self.assertEqual(summary["available_cash"]["amount_minor"], 110000)
        self.assertEqual(summary["savings"]["amount_minor"], 520000)
        self.assertEqual(summary["credit_card_debt"]["amount_minor"], 35000)
        self.assertEqual(summary["net_worth"]["amount_minor"], 595000)

        undone = finance.undo_transaction(self.conn, lunch["transaction_id"])
        self.assertEqual(undone["reversed_transaction_id"], lunch["transaction_id"])
        summary = finance.financial_summary(self.conn)
        self.assertEqual(summary["available_cash"]["amount_minor"], 120000)
        self.assertEqual(summary["net_worth"]["amount_minor"], 605000)

        activity = finance.activity_summary(self.conn)
        self.assertEqual(activity["expenses"]["needs_minor"], 0)
        self.assertEqual(activity["expenses"]["wants_minor"], 25000)
        self.assertEqual(activity["savings_allocated_minor"], 20000)

    def test_credit_card_overpayment_accepts_negative_balance(self):
        # A credit-card overpayment is a negative liability (issuer owes the user).
        finance.set_account(
            self.conn, "bank-a",
            balance_minor=finance.parse_minor("-321.45", allow_zero=True, allow_negative=True),
            set_balance=True,
        )
        self.set_balance("cash", "1000")
        self.set_balance("savings", "5000")
        self.set_balance("bank-b", "0")
        self.set_balance("bank-c", "0")
        summary = finance.financial_summary(self.conn)
        # Negative card debt reduces net liability; net worth counts the credit.
        self.assertTrue(summary["net_worth"]["complete"])
        self.assertEqual(summary["credit_card_debt"]["amount_minor"], -32145)

    def test_classification_precedents(self):
        lunch = finance.classify_text(self.conn, "Bought lunch")
        snack = finance.classify_text(self.conn, "Had meryenda")
        self.assertEqual(lunch[0]["bucket"], "needs")
        self.assertEqual(lunch[0]["tags"], ["food", "meal"])
        self.assertEqual(snack[0]["bucket"], "wants")
        self.assertFalse(snack[0]["user_confirmed"])

    def test_monthly_budget_and_salary_auto_savings(self):
        self.set_balance("cash", "1000")
        self.set_balance("savings", "5000")
        plan = finance.set_budget_plan(
            self.conn,
            finance.parse_minor("40001.23"),
            finance.parse_basis_points("45%"),
            finance.parse_basis_points("25"),
            finance.parse_basis_points("30"),
        )

        self.assertEqual(plan["categories"]["needs"]["target_minor"], 1800055)
        self.assertEqual(plan["categories"]["wants"]["target_minor"], 1000031)
        self.assertEqual(plan["categories"]["savings"]["target_minor"], 1200037)
        self.assertTrue(plan["auto_save_salary"])

        result = finance.record_salary(
            self.conn,
            finance.parse_minor("40001.23"),
            "Monthly salary",
            "cash",
            occurred_at="2026-08-10",
            original_message="salary is 40,001.23",
        )
        income = result["income"]
        transfer = result["savings_transfer"]
        self.assertEqual(income["type"], "income")
        self.assertEqual(transfer["type"], "transfer")
        self.assertEqual(transfer["bucket"], "savings")
        self.assertEqual(transfer["amount_minor"], 1200037)
        self.assertEqual(transfer["related_transaction_id"], income["transaction_id"])
        self.assertEqual(result["remaining_available_minor"], 2800086)

        cash = finance.resolve_account(self.conn, "cash")
        savings = finance.resolve_account(self.conn, "savings")
        self.assertEqual(finance.account_payload(self.conn, cash["id"])["balance_minor"], 2900086)
        self.assertEqual(finance.account_payload(self.conn, savings["id"])["balance_minor"], 1700037)

        budget = finance.monthly_budget_summary(self.conn, "2026-08", "2026-08-10")
        self.assertEqual(budget["actual_income_minor"], 4000123)
        self.assertEqual(budget["categories"]["savings"]["actual_minor"], 1200037)
        self.assertEqual(budget["categories"]["savings"]["remaining_minor"], 0)

    def test_budget_percentages_must_total_one_hundred(self):
        with self.assertRaisesRegex(ValueError, "exactly 100%"):
            finance.set_budget_plan(
                self.conn,
                finance.parse_minor("1000"),
                finance.parse_basis_points("45"),
                finance.parse_basis_points("25"),
                finance.parse_basis_points("29"),
            )

    def test_apartment_budget_is_fixed_and_separate_from_regular_needs(self):
        plan = finance.set_budget_plan(
            self.conn,
            finance.parse_minor("40001.23"),
            finance.parse_basis_points("45"),
            finance.parse_basis_points("25"),
            finance.parse_basis_points("30"),
            apartment_minor=finance.parse_minor("7000"),
        )
        self.assertEqual(plan["categories"]["needs"]["target_minor"], 1100055)
        self.assertEqual(plan["categories"]["apartment"]["target_minor"], 700000)

        apartment_expense = finance.record_expense(
            self.conn, finance.parse_minor("100"), "Electric bill", "cash", "apartment",
            tags=["apartment", "utilities"], occurred_at="2026-08-11",
        )
        self.assertEqual(apartment_expense["bucket"], "apartment")
        finance.record_expense(
            self.conn, finance.parse_minor("50"), "Lunch", "cash", "needs",
            tags=["food", "meal"], occurred_at="2026-08-11",
        )
        budget = finance.monthly_budget_summary(self.conn, "2026-08", "2026-08-11")
        self.assertEqual(budget["categories"]["apartment"]["actual_minor"], 10000)
        self.assertEqual(budget["categories"]["needs"]["actual_minor"], 5000)
        self.assertEqual(budget["categories"]["needs"]["pace_status"], "under_pace")
        self.assertEqual(
            budget["categories"]["apartment"]["pace_status"], "not_applicable"
        )
        self.assertIsNone(budget["categories"]["apartment"]["pace_difference_minor"])
        self.assertEqual(budget["categories"]["wants"]["pace_status"], "under_pace")
        self.assertEqual(
            budget["categories"]["savings"]["pace_status"], "short_of_pace"
        )
        expected_needs_pace = (1100055 * 11 + 15) // 31
        self.assertEqual(
            budget["categories"]["needs"]["pace_difference_minor"],
            expected_needs_pace - 5000,
        )
        self.assertFalse(budget["categories"]["apartment"]["pace_applicable"])
        self.assertEqual(
            budget["categories"]["needs"]["buffer_minor"],
            expected_needs_pace - 5000,
        )
        self.assertIsNone(budget["categories"]["apartment"]["buffer_minor"])
        self.assertLess(budget["categories"]["savings"]["buffer_minor"], 0)

        with self.assertRaisesRegex(ValueError, "fit within the needs budget"):
            finance.set_budget_plan(
                self.conn,
                finance.parse_minor("1000"),
                finance.parse_basis_points("45"),
                finance.parse_basis_points("25"),
                finance.parse_basis_points("30"),
                apartment_minor=finance.parse_minor("500"),
            )

    def test_database_integrity(self):
        report = finance.doctor(self.conn)
        self.assertTrue(report["ok"])
        self.assertEqual(report["schema_version"], "7")
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_receivables_raise_net_worth_without_affecting_budget(self):
        self.set_balance("cash", "1000")
        self.set_balance("savings", "500")
        self.set_balance("bank-a", "0")
        self.set_balance("bank-b", "0")
        self.set_balance("bank-c", "0")
        finance.set_budget_plan(
            self.conn,
            finance.parse_minor("40001.23"),
            finance.parse_basis_points("45"),
            finance.parse_basis_points("25"),
            finance.parse_basis_points("30"),
            apartment_minor=finance.parse_minor("7000"),
        )

        entry = finance.record_receivable(
            self.conn, finance.parse_minor("200.17"), "Example Person",
            original_message="utang ni Example Person",
        )
        self.assertEqual(entry["type"], "adjustment")
        self.assertIsNone(entry["bucket"])
        summary = finance.financial_summary(self.conn)
        self.assertEqual(summary["receivables"]["amount_minor"], 20017)
        self.assertEqual(summary["net_worth"]["amount_minor"], 170017)
        budget = finance.monthly_budget_summary(self.conn, "2026-08", "2026-08-12")
        self.assertEqual(budget["categories"]["needs"]["actual_minor"], 0)
        self.assertEqual(budget["categories"]["wants"]["actual_minor"], 0)

        payment = finance.record_receivable_payment(
            self.conn, finance.parse_minor("100.17"), "Example Person", "cash"
        )
        self.assertEqual(payment["type"], "transfer")
        summary = finance.financial_summary(self.conn)
        self.assertEqual(summary["receivables"]["amount_minor"], 10000)
        self.assertEqual(summary["available_cash"]["amount_minor"], 110017)
        self.assertEqual(summary["net_worth"]["amount_minor"], 170017)


    def backdate_plan(self, created_at):
        self.conn.execute(
            "UPDATE budget_plans SET created_at = ? WHERE id = 1", (created_at,)
        )
        self.conn.commit()

    def set_standard_plan(self):
        return finance.set_budget_plan(
            self.conn,
            finance.parse_minor("40001.23"),
            finance.parse_basis_points("45"),
            finance.parse_basis_points("25"),
            finance.parse_basis_points("30"),
            apartment_minor=finance.parse_minor("7000"),
        )

    def test_budget_rollover_moves_unspent_allowance_to_buffer(self):
        self.set_standard_plan()
        self.backdate_plan("2026-06-01T00:00:00+08:00")
        finance.record_expense(
            self.conn, finance.parse_minor("500"), "July lunch", "cash", "needs",
            tags=["food", "meal"], occurred_at="2026-07-10",
        )
        finance.record_expense(
            self.conn, finance.parse_minor("200"), "July treat", "cash", "wants",
            tags=["food", "snack"], occurred_at="2026-07-10",
        )
        budget = finance.monthly_budget_summary(self.conn, "2026-08", "2026-08-10")
        needs = budget["categories"]["needs"]
        self.assertEqual(needs["base_target_minor"], 1100055)
        self.assertEqual(needs["rollover_minor"], 0)
        self.assertEqual(needs["target_minor"], 1100055)
        wants = budget["categories"]["wants"]
        self.assertEqual(wants["rollover_minor"], 0)
        self.assertEqual(wants["target_minor"], 1000031)
        # July buffer already held June's untouched needs+wants. Add July's
        # unused base needs and wants to that pool.
        self.assertEqual(budget["categories"]["buffer"]["target_minor"], 5530172)

    def test_budget_rollover_compounds_until_spent(self):
        self.set_standard_plan()
        self.backdate_plan("2026-06-01T00:00:00+08:00")
        # Nothing spent: only buffer stacks; needs and wants remain base targets.
        august = finance.monthly_budget_summary(self.conn, "2026-08", "2026-08-31")
        self.assertEqual(august["categories"]["needs"]["target_minor"], 1100055)
        self.assertEqual(august["categories"]["buffer"]["target_minor"], 5600172)
        september = finance.monthly_budget_summary(self.conn, "2026-09", "2026-09-30")
        self.assertEqual(september["categories"]["buffer"]["target_minor"], 8400258)

    def test_category_surplus_absorbs_other_category_overspending(self):
        self.set_standard_plan()
        self.backdate_plan("2026-07-01T00:00:00+08:00")
        finance.record_expense(
            self.conn, finance.parse_minor("9500.55"), "July needs", "cash",
            "needs", tags=["food"], occurred_at="2026-07-10",
        )
        finance.record_expense(
            self.conn, finance.parse_minor("11500.31"), "July wants", "cash",
            "wants", occurred_at="2026-07-10",
        )
        finance.record_expense(
            self.conn, finance.parse_minor("7000"), "July apartment", "cash",
            "apartment", tags=["apartment", "utilities"], occurred_at="2026-07-10",
        )
        budget = finance.monthly_budget_summary(self.conn, "2026-08", "2026-08-10")
        self.assertEqual(budget["categories"]["needs"]["target_minor"], 1100055)
        self.assertEqual(budget["categories"]["wants"]["target_minor"], 1000031)
        self.assertEqual(budget["categories"]["apartment"]["target_minor"], 700000)
        self.assertEqual(budget["categories"]["buffer"]["target_minor"], 0)

    def test_uncovered_overspending_reduces_that_category_next_month(self):
        self.set_standard_plan()
        self.backdate_plan("2026-07-01T00:00:00+08:00")
        finance.record_expense(
            self.conn, finance.parse_minor("10500.55"), "July needs", "cash",
            "needs", occurred_at="2026-07-10",
        )
        finance.record_expense(
            self.conn, finance.parse_minor("11500.31"), "July wants", "cash",
            "wants", occurred_at="2026-07-10",
        )
        finance.record_expense(
            self.conn, finance.parse_minor("7000"), "July apartment", "cash",
            "apartment", occurred_at="2026-07-10",
        )
        budget = finance.monthly_budget_summary(self.conn, "2026-08", "2026-08-10")
        self.assertEqual(budget["categories"]["buffer"]["target_minor"], 0)
        self.assertEqual(budget["categories"]["wants"]["carry_reduction_minor"], 100000)
        self.assertEqual(budget["categories"]["wants"]["target_minor"], 900031)

    def test_no_rollover_from_months_before_plan_creation(self):
        self.set_standard_plan()
        # Plan was created this month; July spending predates it.
        finance.record_expense(
            self.conn, finance.parse_minor("100"), "Old lunch", "cash", "needs",
            tags=["food", "meal"], occurred_at="2026-07-10",
        )
        budget = finance.monthly_budget_summary(self.conn, "2026-08", "2026-08-10")
        needs = budget["categories"]["needs"]
        self.assertEqual(needs["rollover_minor"], 0)
        self.assertEqual(needs["target_minor"], 1100055)
        self.assertEqual(budget["categories"]["buffer"]["target_minor"], 0)

    def test_wants_categories_default_named_refund_and_retirement(self):
        category = finance.add_wants_category(self.conn, "Dates")
        self.assertEqual(category["spent_minor"], 0)
        expense = finance.record_expense(
            self.conn, finance.parse_minor("300"), "Cinema", "cash", "wants",
            wants_category_identifier="Dates",
        )
        self.assertEqual(expense["wants_category"]["slug"], "dates")
        default = finance.record_expense(
            self.conn, finance.parse_minor("50"), "Treat", "cash", "wants"
        )
        self.assertEqual(default["wants_category"]["slug"], "wants")
        refund = finance.record_refund(
            self.conn, finance.parse_minor("100"), "Cinema refund", "cash",
            related_public_id=expense["transaction_id"],
        )
        self.assertEqual(refund["wants_category"]["slug"], "dates")
        self.assertEqual(finance.list_wants_categories(self.conn)[1]["spent_minor"], 20000)
        retired = finance.remove_wants_category(self.conn, "Dates")
        self.assertTrue(retired["retired"])
        self.assertEqual(retired["spent_minor"], 20000)

    def test_savings_categories_and_goals_track_transfer_progress(self):
        self.set_balance("cash", "10000")
        self.set_balance("savings", "0")
        category = finance.add_savings_category(self.conn, "Emergency Fund")
        self.assertEqual(category["saved_minor"], 0)

        transfer = finance.record_transfer(
            self.conn, finance.parse_minor("1500"), "cash", "savings",
            description="Emergency contribution",
            savings_category_identifier="Emergency Fund",
        )
        self.assertEqual(transfer["savings_category"]["slug"], "emergency-fund")

        default_transfer = finance.record_transfer(
            self.conn, finance.parse_minor("200"), "cash", "savings",
            description="Uncategorised savings",
        )
        self.assertEqual(default_transfer["savings_category"]["slug"], "savings")

        goal = finance.add_savings_goal(
            self.conn, "Emergency cushion", finance.parse_minor("5000"),
            "Emergency Fund", target_date="2026-12-31", notes="Six weeks of basics.",
        )
        self.assertEqual(goal["saved_minor"], 150000)
        self.assertEqual(goal["remaining_minor"], 350000)
        self.assertEqual(goal["target_date"], "2026-12-31")

        retired = finance.remove_savings_category(self.conn, "Emergency Fund")
        self.assertTrue(retired["retired"])
        self.assertFalse(retired["active"])
        self.assertEqual([c["slug"] for c in finance.list_savings_categories(self.conn)], ["savings"])
        self.assertEqual(finance.list_savings_goals(self.conn), [])
        with self.assertRaisesRegex(ValueError, "default Savings"):
            finance.remove_savings_category(self.conn, "Savings")

    def test_transaction_comments_are_optional_and_auditable(self):
        transaction = finance.record_expense(
            self.conn, finance.parse_minor("100"), "Lunch", "cash", "needs",
            tags=["food", "meal"],
        )
        self.assertEqual(transaction["comments"], [])

        comment = finance.add_transaction_comment(
            self.conn, transaction["transaction_id"], "Shared with the team."
        )
        self.assertTrue(comment["active"])
        refreshed = finance.transaction_payload(self.conn, transaction["transaction_id"])
        self.assertEqual(refreshed["comments"][0]["body"], "Shared with the team.")

        removed = finance.remove_transaction_comment(self.conn, comment["comment_id"])
        self.assertFalse(removed["active"])
        self.assertEqual(finance.list_transaction_comments(
            self.conn, transaction["transaction_id"]
        ), [])
        history = finance.list_transaction_comments(
            self.conn, transaction["transaction_id"], include_retired=True
        )
        self.assertFalse(history[0]["active"])


if __name__ == "__main__":
    unittest.main()
