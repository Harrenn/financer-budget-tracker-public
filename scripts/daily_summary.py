#!/usr/bin/env python3
"""Daily end-of-day transaction summary for the financer ledger.

Usage: python3 scripts/daily_summary.py [--date YYYY-MM-DD] [--db PATH]
Defaults to today (Asia/Manila) and the live DB.

Prints that day's posted, non-reversal transactions in chronological order,
each with amount, bucket, tags, account, and any active comments.
"""
import argparse
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MANILA = ZoneInfo("Asia/Manila")

DEFAULT_DB = str(Path(__file__).resolve().parents[1] / "data" / "financer.sqlite3")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today, Asia/Manila)")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    if args.date:
        d = date.fromisoformat(args.date)
    else:
        d = datetime.now(MANILA).date()
    day = d.isoformat()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # All posted, non-reversal transactions on that day, oldest → newest.
    rows = cur.execute(
        """
        SELECT t.id, t.public_id, t.amount_minor, t.description, t.bucket,
               t.transaction_type, t.occurred_at,
               (SELECT GROUP_CONCAT(c.body, ' | ') FROM transaction_comments c
                WHERE c.transaction_id = t.id AND c.active = 1) AS comments
        FROM transactions t
        WHERE t.status = 'posted' AND t.transaction_type != 'reversal'
          AND date(t.occurred_at) = ?
        ORDER BY t.occurred_at
        """,
        (day,),
    ).fetchall()

    # tags per txn (joined via transaction_tags → tags.slug)
    tagmap = {}
    for r in cur.execute(
        """
        SELECT tt.transaction_id, GROUP_CONCAT(tg.slug, ',') AS tags
        FROM transaction_tags tt JOIN tags tg ON tg.id = tt.tag_id
        GROUP BY tt.transaction_id
        """
    ):
        tagmap[r["transaction_id"]] = r["tags"]

    total_minor = 0
    for r in rows:
        total_minor += r["amount_minor"]

    print(f"Financer review — {day}")
    if not rows:
        print("No posted, non-reversal transactions for this day.")
        return 0
    for r in rows:
        amt = r["amount_minor"] / 100
        bucket = r["bucket"] or "n/a"
        tags = tagmap.get(r["id"]) or ""
        comments = r["comments"]
        print()
        print(f"• Amount: ₱{amt:,.2f}")
        print(f"• Description: {r['description'] or '(none)'}")
        print(f"• Status: ✓ posted")
        print(f"• Category: {bucket}")
        if tags:
            print(f"• Tags: {tags}")
        if comments:
            print(f"• Comments: {comments}")
    print(f"\nTotal: ₱{total_minor/100:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
