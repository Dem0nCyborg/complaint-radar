"""Phase 1, step 5: draw a fixed-seed, per-product-stratified sample of
reviews for LLM extraction (Phase 2), flagged via reviews.in_sample.

The full `reviews` table (every review for the selected products) feeds
trend/volume analysis. A separate, smaller sample feeds the LLM extraction
step later, to bound its cost -- the earlier 3,000-5,000 review target from
the original Phase 1 spec now applies to this sample, not the full fetch.

Stratified by product, NOT pure random across the pooled set: allocation is
split as evenly as possible across the selected products
(config.SAMPLE_SIZE // n_products each, remainder distributed to the first
few products in sorted parent_asin order), sampled only from each product's
English-flagged reviews (is_likely_english = TRUE), with a fixed seed.
Pure random sampling across the pooled set would let whichever product has
the most reviews dominate the LLM-extraction sample, which would make any
later per-brand/per-product complaint-rate comparison unreliable -- a
product with 10x the raw review volume isn't necessarily 10x as complaint-
prone, but it would look that way if it also supplied 10x the sampled rows.
See CLAUDE.md.

Also restricted to a common date window (config.SAMPLE_WINDOW_START_DATE
through config.SAMPLE_WINDOW_END_DATE inclusive) -- see CLAUDE.md for why.
This only affects what gets sampled/labeled; the full `reviews` table keeps
every review regardless of date, for trend analysis.

If a product has fewer English, in-window reviews than its allocation,
every one of them is taken and the gap is reported as a shortfall --
allocation is NOT silently redistributed to other products, so the total
sample can land below config.SAMPLE_SIZE when supply is short.

Sampling is done in Python (not DuckDB's own SAMPLE clause) over sorted id
lists, product by product in sorted parent_asin order, with one
`random.Random(config.SAMPLE_SEED)` instance reused across products -- so
the result is reproducible independent of DuckDB version/internals and of
dict/query row ordering.

Usage:
    python -m src.select_sample
"""

from __future__ import annotations

import json
import random

import duckdb

from src import config


def main() -> None:
    if not config.DUCKDB_PATH.exists():
        raise SystemExit(f"{config.DUCKDB_PATH} not found. Run `python -m src.load_duckdb` first.")

    con = duckdb.connect(str(config.DUCKDB_PATH))
    products = [row[0] for row in con.execute("SELECT DISTINCT parent_asin FROM reviews ORDER BY parent_asin").fetchall()]

    if not products:
        raise SystemExit("No reviews loaded yet.")

    n_products = len(products)
    base_allocation, remainder = divmod(config.SAMPLE_SIZE, n_products)

    rng = random.Random(config.SAMPLE_SEED)
    con.execute("UPDATE reviews SET in_sample = FALSE")

    by_product = {}
    total_taken = 0

    for i, parent_asin in enumerate(products):
        allocation = base_allocation + (1 if i < remainder else 0)
        pool = sorted(
            row[0] for row in con.execute(
                """
                SELECT review_id FROM reviews
                WHERE parent_asin = ?
                  AND is_likely_english = TRUE
                  AND review_timestamp >= ?
                  AND review_timestamp < ?
                """,
                [parent_asin, config.SAMPLE_WINDOW_START_DATE, config.SAMPLE_WINDOW_END_EXCLUSIVE],
            ).fetchall()
        )
        taken = rng.sample(pool, min(allocation, len(pool)))
        shortfall = max(0, allocation - len(pool))

        if taken:
            con.executemany(
                "UPDATE reviews SET in_sample = TRUE WHERE review_id = ?",
                [(rid,) for rid in taken],
            )

        by_product[parent_asin] = {
            "allocated": allocation,
            "in_window_pool_size": len(pool),
            "taken": len(taken),
            "shortfall": shortfall,
        }
        total_taken += len(taken)

    con.close()

    stats = {
        "target_total": config.SAMPLE_SIZE,
        "seed": config.SAMPLE_SEED,
        "n_products": n_products,
        "base_allocation_per_product": base_allocation,
        "total_taken": total_taken,
        "total_shortfall": config.SAMPLE_SIZE - total_taken,
        "by_product": by_product,
    }
    config.SELECT_SAMPLE_STATS_PATH.write_text(json.dumps(stats, indent=2))

    print(f"Flagged {total_taken:,} of {config.SAMPLE_SIZE:,} target reviews as in_sample "
          f"across {n_products} products (seed={config.SAMPLE_SEED}, "
          f"base allocation {base_allocation}/product).")
    for parent_asin, info in by_product.items():
        flag = "  <-- SHORTFALL" if info["shortfall"] else ""
        print(f"  {parent_asin}: allocated={info['allocated']} in_window_pool={info['in_window_pool_size']} "
              f"taken={info['taken']}{flag}")
    if stats["total_shortfall"] > 0:
        print(f"\nTotal shortfall: {stats['total_shortfall']:,} short of the {config.SAMPLE_SIZE:,} target "
              "(not redistributed to other products -- see CLAUDE.md).")
    print("\nNext: `python -m src.sample_for_labeling`")


if __name__ == "__main__":
    main()
