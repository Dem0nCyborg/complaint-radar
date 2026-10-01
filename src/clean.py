"""Phase 1, step 3: dedupe, drop empty text, flag (not drop) non-English.

Reads data/raw/reviews_raw.jsonl, writes cleaned rows to
data/interim/reviews_clean.jsonl plus drop-count stats for the quality
report.

Usage:
    python -m src.clean
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from langdetect import DetectorFactory, LangDetectException, detect
from tqdm import tqdm

from src import config

DetectorFactory.seed = 0  # deterministic langdetect output


def to_timestamp_iso(epoch_ms: int | None) -> str | None:
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()


def is_english(text: str) -> bool:
    try:
        return detect(text) == "en"
    except LangDetectException:
        return False


def main() -> None:
    if not config.RAW_REVIEWS_PATH.exists():
        raise SystemExit(f"{config.RAW_REVIEWS_PATH} not found. Run `python -m src.fetch_reviews` first.")

    config.INTERIM_DIR.mkdir(parents=True, exist_ok=True)

    raw_count = 0
    empty_dropped = 0
    duplicate_dropped = 0
    non_english_flagged = 0
    seen_ids: set[str] = set()
    cleaned_rows = []

    with open(config.RAW_REVIEWS_PATH, encoding="utf-8") as f:
        lines = f.readlines()

    for line in tqdm(lines, desc="cleaning reviews"):
        line = line.strip()
        if not line:
            continue
        raw_count += 1
        row = json.loads(line)

        text = (row.get("text") or "").strip()
        if not text:
            empty_dropped += 1
            continue

        user_id = row.get("user_id") or ""
        parent_asin = row.get("parent_asin") or ""
        timestamp = row.get("timestamp")

        review_id = config.compute_review_id(user_id, parent_asin, timestamp, text)
        if review_id in seen_ids:
            duplicate_dropped += 1
            continue
        seen_ids.add(review_id)

        likely_english = is_english(text)
        if not likely_english:
            non_english_flagged += 1

        cleaned_rows.append(
            {
                "review_id": review_id,
                "parent_asin": parent_asin,
                "asin": row.get("asin"),
                "user_id": user_id,
                "rating": row.get("rating"),
                "title": row.get("title") or "",
                "text": text,
                "review_timestamp": to_timestamp_iso(timestamp),
                "verified_purchase": bool(row.get("verified_purchase")),
                "helpful_vote": row.get("helpful_vote") or 0,
                "is_likely_english": likely_english,
            }
        )

    with open(config.CLEAN_REVIEWS_PATH, "w", encoding="utf-8") as f:
        for row in cleaned_rows:
            f.write(json.dumps(row) + "\n")

    stats = {
        "raw_count": raw_count,
        "empty_text_dropped": empty_dropped,
        "duplicate_dropped": duplicate_dropped,
        "non_english_flagged": non_english_flagged,
        "final_count": len(cleaned_rows),
    }
    config.CLEAN_STATS_PATH.write_text(json.dumps(stats, indent=2))

    print(json.dumps(stats, indent=2))
    print(f"\nWrote {len(cleaned_rows):,} cleaned reviews to {config.CLEAN_REVIEWS_PATH}")
    print("Next: `python -m src.load_duckdb`")


if __name__ == "__main__":
    main()
