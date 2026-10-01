"""Phase 1, step 5: draw a fixed-seed random sample of reviews for LLM
extraction (Phase 2), flagged via reviews.in_sample.

The full `reviews` table (every review for the selected products) feeds
trend/volume analysis. A separate, smaller random sample feeds the LLM
extraction step later, to bound its cost -- the earlier 3,000-5,000 review
target from the original Phase 1 spec now applies to this sample, not the
full fetch.

Sampling is done in Python with a fixed seed over a sorted id list (not via
DuckDB's own SAMPLE clause) so the result is reproducible independent of
DuckDB version/internals.

Usage:
    python -m src.select_sample
"""

from __future__ import annotations

import random

import duckdb

from src import config


def main() -> None:
    if not config.DUCKDB_PATH.exists():
        raise SystemExit(f"{config.DUCKDB_PATH} not found. Run `python -m src.load_duckdb` first.")

    con = duckdb.connect(str(config.DUCKDB_PATH))
    all_ids = [row[0] for row in con.execute("SELECT review_id FROM reviews ORDER BY review_id").fetchall()]

    if not all_ids:
        raise SystemExit("No reviews loaded yet.")

    rng = random.Random(config.SAMPLE_SEED)
    sample_size = min(config.SAMPLE_SIZE, len(all_ids))
    sample_ids = rng.sample(all_ids, sample_size)

    con.execute("UPDATE reviews SET in_sample = FALSE")
    con.executemany(
        "UPDATE reviews SET in_sample = TRUE WHERE review_id = ?",
        [(rid,) for rid in sample_ids],
    )
    con.close()

    print(f"Flagged {sample_size:,} of {len(all_ids):,} reviews as in_sample "
          f"(seed={config.SAMPLE_SEED}).")
    print("Next: `python -m src.sample_for_labeling`")


if __name__ == "__main__":
    main()
