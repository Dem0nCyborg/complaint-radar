"""Phase 1, step 4: create the DuckDB schema and load selected products +
cleaned reviews.

Usage:
    python -m src.load_duckdb
"""

from __future__ import annotations

import csv
import json

import duckdb

from src import config

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    parent_asin     TEXT PRIMARY KEY,
    title           TEXT,
    brand_raw       TEXT,
    brand_normalized TEXT,
    category        TEXT,
    main_category   TEXT,
    average_rating  DOUBLE,
    rating_number   INTEGER,
    price           DOUBLE
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id           TEXT PRIMARY KEY,
    parent_asin         TEXT REFERENCES products(parent_asin),
    asin                TEXT,
    user_id             TEXT,
    rating              DOUBLE,
    title               TEXT,
    text                TEXT,
    review_timestamp    TIMESTAMP,
    verified_purchase   BOOLEAN,
    helpful_vote        INTEGER,
    is_likely_english   BOOLEAN,
    in_sample           BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS label_sample (
    review_id   TEXT PRIMARY KEY REFERENCES reviews(review_id),
    stratum     TEXT,
    sampled_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS labels (
    review_id           TEXT PRIMARY KEY REFERENCES reviews(review_id),
    complaint_category  TEXT,
    severity            INTEGER,
    is_defect           BOOLEAN,
    note                TEXT,
    labeled_at          TIMESTAMP
);
"""


def load_selected_products() -> list[dict]:
    products = []
    with open(config.CANDIDATE_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("selected") or "").strip().lower() == "y":
                products.append(
                    {
                        "parent_asin": row["parent_asin"],
                        "title": row["title"],
                        "brand_raw": row["brand_raw"],
                        "brand_normalized": row["brand_normalized"],
                        "category": row["category"],
                        "main_category": row["main_category"],
                        "average_rating": float(row["average_rating"]) if row["average_rating"] else None,
                        "rating_number": int(row["rating_number"]) if row["rating_number"] else 0,
                        "price": float(row["price"]) if row["price"] else None,
                    }
                )
    return products


def load_cleaned_reviews() -> list[dict]:
    reviews = []
    with open(config.CLEAN_REVIEWS_PATH, encoding="utf-8") as f:
        for line in f:
            reviews.append(json.loads(line))
    return reviews


def main() -> None:
    if not config.CANDIDATE_CSV_PATH.exists():
        raise SystemExit(f"{config.CANDIDATE_CSV_PATH} not found. Run `python -m src.ingest_metadata` first.")
    if not config.CLEAN_REVIEWS_PATH.exists():
        raise SystemExit(f"{config.CLEAN_REVIEWS_PATH} not found. Run `python -m src.clean` first.")

    config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    products = load_selected_products()
    selected_asins = {p["parent_asin"] for p in products}

    all_reviews = load_cleaned_reviews()
    # Defensive re-check against the CURRENT selection, independent of
    # clean.py already having filtered: a review's parent_asin not being in
    # `products` would otherwise violate the reviews.parent_asin foreign key.
    reviews = [r for r in all_reviews if r["parent_asin"] in selected_asins]
    n_skipped = len(all_reviews) - len(reviews)
    if n_skipped:
        print(f"Skipping {n_skipped:,} cleaned reviews for no-longer-selected products "
              f"(not in the current candidate CSV selection).")

    con = duckdb.connect(str(config.DUCKDB_PATH))
    con.execute(SCHEMA_SQL)

    con.execute("DELETE FROM labels")
    con.execute("DELETE FROM label_sample")
    con.execute("DELETE FROM reviews")
    con.execute("DELETE FROM products")

    con.executemany(
        """
        INSERT INTO products
            (parent_asin, title, brand_raw, brand_normalized, category, main_category,
             average_rating, rating_number, price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                p["parent_asin"], p["title"], p["brand_raw"], p["brand_normalized"], p["category"],
                p["main_category"], p["average_rating"], p["rating_number"], p["price"],
            )
            for p in products
        ],
    )

    con.executemany(
        """
        INSERT INTO reviews
            (review_id, parent_asin, asin, user_id, rating, title, text,
             review_timestamp, verified_purchase, helpful_vote, is_likely_english, in_sample)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE)
        """,
        [
            (
                r["review_id"], r["parent_asin"], r["asin"], r["user_id"], r["rating"], r["title"],
                r["text"], r["review_timestamp"], r["verified_purchase"], r["helpful_vote"],
                r["is_likely_english"],
            )
            for r in reviews
        ],
    )

    n_products = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    n_reviews = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    con.close()

    print(f"Loaded {n_products} products and {n_reviews:,} reviews into {config.DUCKDB_PATH}")
    print("Next: `python -m src.select_sample` then `python -m src.quality_report`")


if __name__ == "__main__":
    main()
