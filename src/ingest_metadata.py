"""Phase 1, step 1: stream item METADATA ONLY (not reviews) and build a
candidate product list for air fryers, blenders, and robot vacuums.

The McAuley-Lab/Amazon-Reviews-2023 Home_and_Kitchen reviews file is ~31GB;
this script never touches it. The metadata file (~12GB) is streamed once
in full (no server-side filter exists), but that's a pipeline we only ever
run once, and it's a fraction of the reviews file's size.

Usage:
    python -m src.ingest_metadata [--limit N]

--limit is a development/smoke-test knob only (caps how many metadata rows
are scanned) -- omit it for the real run.
"""

from __future__ import annotations

import argparse
import csv
import json

from datasets import load_dataset
from tqdm import tqdm

from src import config


def matches_category(title: str) -> str | None:
    for category, pattern in config.CATEGORY_PATTERNS.items():
        if pattern.search(title):
            return category
    return None


def parse_price(raw_price) -> float | None:
    if raw_price is None:
        return None
    try:
        return float(raw_price)
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="dev-only: cap rows scanned")
    args = parser.parse_args()

    config.INTERIM_DIR.mkdir(parents=True, exist_ok=True)

    dataset = load_dataset(
        "json", data_files={"full": config.META_JSONL_URL}, split="full", streaming=True
    )
    if args.limit:
        dataset = dataset.take(args.limit)

    total_scanned = 0
    matched_pre_exclusion = {cat: 0 for cat in config.CATEGORY_KEYWORDS}
    excluded_by_category_tier = {
        cat: {"strong": 0, "weak": 0} for cat in config.CATEGORY_KEYWORDS
    }
    candidates: list[dict] = []
    excluded_rows: list[dict] = []

    for item in tqdm(dataset, desc="scanning metadata"):
        total_scanned += 1
        title = item.get("title") or ""
        title_lower = title.lower()

        category = matches_category(title_lower)
        if category is None:
            continue

        matched_pre_exclusion[category] += 1

        excluded, matched_term, tier = config.check_exclusion(title, category)
        if excluded:
            excluded_by_category_tier[category][tier] += 1
            excluded_rows.append(
                {
                    "parent_asin": item.get("parent_asin"),
                    "title": title,
                    "category": category,
                    "matched_term": matched_term,
                    "tier": tier,
                }
            )
            continue

        details = item.get("details") or {}
        brand_raw = item.get("store") or (details.get("Brand") if isinstance(details, dict) else None) or ""
        brand_normalized = config.normalize_brand(brand_raw, category)

        candidates.append(
            {
                "parent_asin": item.get("parent_asin"),
                "title": title,
                "brand_raw": brand_raw,
                "brand_normalized": brand_normalized,
                "category": category,
                "main_category": item.get("main_category"),
                "average_rating": item.get("average_rating"),
                "rating_number": item.get("rating_number") or 0,
                "price": parse_price(item.get("price")),
                "selected": "",
            }
        )

    candidates.sort(key=lambda row: (row["category"], -row["rating_number"]))

    fieldnames = [
        "parent_asin",
        "title",
        "brand_raw",
        "brand_normalized",
        "category",
        "main_category",
        "average_rating",
        "rating_number",
        "price",
        "selected",
    ]
    with open(config.CANDIDATE_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(candidates)

    with open(config.EXCLUDED_PRODUCTS_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["parent_asin", "title", "category", "matched_term", "tier"])
        writer.writeheader()
        writer.writerows(excluded_rows)

    funnel = {
        "total_metadata_scanned": total_scanned,
        "matched_pre_exclusion_by_category": matched_pre_exclusion,
        "excluded_by_category_and_tier": excluded_by_category_tier,
        "final_candidates_by_category": {
            cat: sum(1 for c in candidates if c["category"] == cat)
            for cat in config.CATEGORY_KEYWORDS
        },
    }
    with open(config.METADATA_FUNNEL_PATH, "w", encoding="utf-8") as f:
        json.dump(funnel, f, indent=2)

    print(f"\nScanned {total_scanned:,} metadata rows.")
    print(f"Wrote {len(candidates):,} candidates to {config.CANDIDATE_CSV_PATH}")
    print(f"Wrote {len(excluded_rows):,} excluded rows to {config.EXCLUDED_PRODUCTS_CSV_PATH}")
    print(json.dumps(funnel, indent=2))

    print("\nTop candidates by category (by rating_number, a review-count proxy):\n")
    for category in config.CATEGORY_KEYWORDS:
        rows = [c for c in candidates if c["category"] == category][:20]
        print(f"=== {category} (showing top {len(rows)}) ===")
        for row in rows:
            print(
                f"  {row['parent_asin']}  n={row['rating_number']:<6}  "
                f"avg={row['average_rating']}  {row['brand_normalized']:<15}  {row['title'][:70]}"
            )
        print()

    print(f"Next step: open {config.CANDIDATE_CSV_PATH}, mark 'selected' = y for your picks,")
    print("then run `python -m src.fetch_reviews`.")


if __name__ == "__main__":
    main()
