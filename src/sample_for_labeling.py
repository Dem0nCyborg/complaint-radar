"""Phase 1, step 6: draw the fixed, reproducible 150-review hand-labeling
sample and persist it to the `label_sample` table.

Design choices:
- English only (is_likely_english = TRUE), per spec.
- Stratified ~50% 1-3 star / ~50% 4-5 star: the natural rating distribution
  is mostly positive, so an unstratified sample would hand you very few
  complaints to label. This intentionally over-represents negative reviews.
  IMPORTANT: any accuracy/precision number later computed against this set
  reflects a complaint-enriched mix, not the natural population -- it needs
  reweighting before it can be read as "accuracy on all reviews."
- Drawn from the `in_sample` pool (the same reviews that will go through
  LLM extraction in Phase 2), not the full review set. This is an
  assumption, not something explicitly specified -- flagged for review:
  the intent is to let Phase 2 directly compare LLM output against these
  human labels on the identical reviews. If you'd rather label from the
  full pool regardless of in_sample, that's a one-line change here.
- Sampled IDs are persisted (not re-rolled each run) so the set is stable;
  rerunning without --force is a no-op once a sample already exists.

Usage:
    python -m src.sample_for_labeling [--force]
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timezone

import duckdb

from src import config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-draw the sample even if one already exists")
    args = parser.parse_args()

    if not config.DUCKDB_PATH.exists():
        raise SystemExit(f"{config.DUCKDB_PATH} not found. Run `python -m src.load_duckdb` first.")

    con = duckdb.connect(str(config.DUCKDB_PATH))

    existing = con.execute("SELECT COUNT(*) FROM label_sample").fetchone()[0]
    if existing and not args.force:
        print(f"label_sample already has {existing} rows. Pass --force to re-draw (this changes the set).")
        con.close()
        return

    pool = "SELECT review_id, rating FROM reviews WHERE in_sample = TRUE AND is_likely_english = TRUE"
    low_ids = [r[0] for r in con.execute(f"{pool} AND rating <= 3").fetchall()]
    high_ids = [r[0] for r in con.execute(f"{pool} AND rating > 3").fetchall()]

    rng = random.Random(config.LABEL_SAMPLE_SEED)
    low_ids.sort()
    high_ids.sort()

    target_low = round(config.LABEL_SAMPLE_SIZE * config.LABEL_SAMPLE_LOW_STAR_FRACTION)
    target_high = config.LABEL_SAMPLE_SIZE - target_low

    low_sample = rng.sample(low_ids, min(target_low, len(low_ids)))
    high_sample = rng.sample(high_ids, min(target_high, len(high_ids)))

    # Top up from whichever stratum has spare reviews if the other fell short.
    shortfall = config.LABEL_SAMPLE_SIZE - len(low_sample) - len(high_sample)
    if shortfall > 0:
        remaining_low = [i for i in low_ids if i not in set(low_sample)]
        remaining_high = [i for i in high_ids if i not in set(high_sample)]
        topup_pool = remaining_low + remaining_high
        low_sample += rng.sample(topup_pool, min(shortfall, len(topup_pool)))

    con.execute("DELETE FROM label_sample")
    now = datetime.now(timezone.utc)
    rows = [(rid, "low_1_3", now) for rid in low_sample] + [(rid, "high_4_5", now) for rid in high_sample]
    con.executemany("INSERT INTO label_sample (review_id, stratum, sampled_at) VALUES (?, ?, ?)", rows)
    con.close()

    print(f"Drew label_sample: {len(low_sample)} low (1-3★), {len(high_sample)} high (4-5★), "
          f"{len(rows)} total (target {config.LABEL_SAMPLE_SIZE}, seed={config.LABEL_SAMPLE_SEED}).")
    if len(rows) < config.LABEL_SAMPLE_SIZE:
        print(f"WARNING: only {len(rows)} eligible reviews available -- short of the {config.LABEL_SAMPLE_SIZE} target.")
    print("Next: `python -m src.label_tool --browse` to read some before finalizing config/categories.yaml,")
    print("then `python -m src.label_tool` to label.")


if __name__ == "__main__":
    main()
