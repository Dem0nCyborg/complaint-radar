"""Phase 1, step 7: minimal CLI for hand-labeling the 150-review sample.

Fields collected: complaint_category (from config/categories.yaml),
severity (1-3), is_defect. Nothing is pre-filled or suggested.

Usage:
    python -m src.label_tool              # label the next unlabeled review
    python -m src.label_tool --browse      # read N random reviews from the
                                            # labeling pool, write nothing
    python -m src.label_tool --browse --n 20
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timezone

import duckdb
import yaml

from src import config


def load_taxonomy() -> dict:
    with open(config.CATEGORIES_YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def print_review(row: dict) -> None:
    print("\n" + "=" * 70)
    print(f"rating={row['rating']}  verified_purchase={row['verified_purchase']}  "
          f"helpful_vote={row['helpful_vote']}")
    print(f"title: {row['title']}")
    print("-" * 70)
    print(row["text"])
    print("=" * 70)


def prompt_category(categories: list[dict]) -> str:
    while True:
        print("\nComplaint category:")
        for i, cat in enumerate(categories, start=1):
            print(f"  [{i}] {cat['label']}")
        choice = input("> ").strip()
        if choice.lower() in ("q", "quit"):
            return "__quit__"
        if choice.isdigit() and 1 <= int(choice) <= len(categories):
            return categories[int(choice) - 1]["key"]
        print("Invalid choice, try again.")


def prompt_severity(severity_def: dict, rules: list[str]) -> int:
    print("\nSeverity:")
    for level, info in sorted(severity_def.items()):
        print(f"  {level} = {info['label']}: {info['description']}")
    for rule in rules:
        print(f"  * {rule}")
    while True:
        choice = input("> ").strip()
        if choice in ("1", "2", "3"):
            return int(choice)
        print("Enter 1, 2, or 3.")


def prompt_is_defect() -> bool:
    while True:
        choice = input("\nIs this a product defect? (y/n) > ").strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        print("Enter y or n.")


def run_browse(con: duckdb.DuckDBPyConnection, n: int) -> None:
    rows = con.execute(
        """
        SELECT r.rating, r.verified_purchase, r.helpful_vote, r.title, r.text
        FROM reviews r
        JOIN label_sample ls ON r.review_id = ls.review_id
        """
    ).fetchall()
    if not rows:
        raise SystemExit("label_sample is empty. Run `python -m src.sample_for_labeling` first.")

    sample = random.sample(rows, min(n, len(rows)))
    print(f"Browsing {len(sample)} random reviews from the labeling pool (read-only, nothing is saved).\n")
    for rating, verified, helpful, title, text in sample:
        print_review(
            {"rating": rating, "verified_purchase": verified, "helpful_vote": helpful, "title": title, "text": text}
        )
        input("\n[enter for next]")


def run_label(con: duckdb.DuckDBPyConnection, taxonomy: dict) -> None:
    categories = taxonomy["categories"]
    severity_def = taxonomy["severity"]
    rules = taxonomy["labeling_rules"]

    total_sample = con.execute("SELECT COUNT(*) FROM label_sample").fetchone()[0]
    if total_sample == 0:
        raise SystemExit("label_sample is empty. Run `python -m src.sample_for_labeling` first.")

    while True:
        already_labeled = con.execute(
            """
            SELECT COUNT(*) FROM labels l JOIN label_sample ls ON l.review_id = ls.review_id
            """
        ).fetchone()[0]
        if already_labeled >= config.LABEL_SAMPLE_SIZE or already_labeled >= total_sample:
            print(f"\nDone: {already_labeled} of {total_sample} labeled.")
            return

        row = con.execute(
            """
            SELECT r.review_id, r.rating, r.verified_purchase, r.helpful_vote, r.title, r.text
            FROM reviews r
            JOIN label_sample ls ON r.review_id = ls.review_id
            LEFT JOIN labels l ON r.review_id = l.review_id
            WHERE l.review_id IS NULL
            ORDER BY ls.sampled_at, r.review_id
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            print(f"\nDone: {already_labeled} of {total_sample} labeled (pool exhausted).")
            return

        review_id, rating, verified, helpful, title, text = row
        print(f"\n[{already_labeled + 1}/{min(config.LABEL_SAMPLE_SIZE, total_sample)}]")
        print_review({"rating": rating, "verified_purchase": verified, "helpful_vote": helpful, "title": title, "text": text})
        print("(enter 'q' at the category prompt to stop and save progress)")

        category = prompt_category(categories)
        if category == "__quit__":
            print("Stopping. Progress saved.")
            return

        note = None
        severity = None
        is_defect = None

        if category == "no_complaint":
            pass
        else:
            if category == "other":
                note = input("Short free-text note for 'other': ").strip()
            severity = prompt_severity(severity_def, rules)
            is_defect = prompt_is_defect()

        con.execute(
            """
            INSERT INTO labels (review_id, complaint_category, severity, is_defect, note, labeled_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (review_id, category, severity, is_defect, note, datetime.now(timezone.utc)),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browse", action="store_true", help="read-only: show random reviews, save nothing")
    parser.add_argument("--n", type=int, default=50, help="number of reviews to show in --browse mode")
    args = parser.parse_args()

    if not config.DUCKDB_PATH.exists():
        raise SystemExit(f"{config.DUCKDB_PATH} not found. Run the pipeline through `python -m src.load_duckdb` first.")

    con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        if args.browse:
            run_browse(con, args.n)
        else:
            taxonomy = load_taxonomy()
            run_label(con, taxonomy)
    finally:
        con.close()


if __name__ == "__main__":
    main()
