"""Phase 1, step 1: stream item METADATA ONLY (not reviews) and build a
candidate product list for air fryers, blenders, and robot vacuums.

The McAuley-Lab/Amazon-Reviews-2023 Home_and_Kitchen reviews file is ~31GB;
this script never touches it. The metadata file (~12GB) is streamed once
in full (no server-side filter exists), but that's a pipeline we only ever
run once, and it's a fraction of the reviews file's size.

Reads raw JSONL directly over HTTP (src/http_jsonl.py) rather than through
`datasets`' streaming+Arrow-cast path. That path crashed around row 319K
with a CastError: later rows add columns (subtitle, author) and represent
price as a string, which don't match the Arrow schema `datasets` infers
from the first batch of rows. Parsing each line independently with
`json.loads` and reading fields with `.get()` has no schema to violate --
see CLAUDE.md #17.

Resume uses the same HTTP byte-offset Range approach as fetch_reviews.py
(see CLAUDE.md #16): the checkpoint stores the byte offset of the start of
the next unprocessed line, plus all funnel counters and the current
contents of the candidate/excluded CSVs, so an interruption only costs
what's left to scan.

Usage:
    python -m src.ingest_metadata [--limit N] [--force]

--limit is a development/smoke-test knob only (caps how many additional
rows are scanned this run, and does NOT mark the pass complete) -- omit it
for the real run.
--force re-runs from scratch even if a previous run already completed.
"""

from __future__ import annotations

import argparse
import csv
import json
import re

from tqdm import tqdm

from src import config, http_jsonl


def matches_category(title: str) -> str | None:
    for category, pattern in config.CATEGORY_PATTERNS.items():
        if pattern.search(title):
            return category
    return None


def parse_price(raw_price) -> float | None:
    """Defensive: the field is a float in most rows but a string (often with
    currency symbols, ranges, or "None") in others. Returns None rather than
    raising on anything that can't be cleanly parsed as a single number.
    """
    if raw_price is None:
        return None
    if isinstance(raw_price, (int, float)):
        return float(raw_price)
    text = str(raw_price).strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def empty_checkpoint() -> dict:
    return {
        "byte_offset": 0,
        "total_scanned": 0,
        "matched_pre_exclusion_by_category": {cat: 0 for cat in config.CATEGORY_KEYWORDS},
        "excluded_by_category_and_tier": {cat: {"strong": 0, "weak": 0} for cat in config.CATEGORY_KEYWORDS},
        "invalid_json_lines": 0,
        "unparseable_price_count": 0,
        "complete": False,
    }


def load_checkpoint() -> dict:
    if config.METADATA_CHECKPOINT_PATH.exists():
        return json.loads(config.METADATA_CHECKPOINT_PATH.read_text())
    return empty_checkpoint()


def save_checkpoint(checkpoint: dict) -> None:
    config.METADATA_CHECKPOINT_PATH.write_text(json.dumps(checkpoint, indent=2))


CANDIDATE_FIELDNAMES = [
    "parent_asin", "title", "brand_raw", "brand_normalized", "category",
    "main_category", "average_rating", "rating_number", "price", "selected",
]
EXCLUDED_FIELDNAMES = ["parent_asin", "title", "category", "matched_term", "tier"]


def resort_candidates_csv() -> None:
    """Re-reads the (possibly append-order) candidate CSV and rewrites it
    sorted by (category, -rating_number). Reading back existing rows (rather
    than regenerating them) preserves any `selected` values already written
    by hand, including ones from before this run.
    """
    if not config.CANDIDATE_CSV_PATH.exists():
        return
    with open(config.CANDIDATE_CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: (r["category"], -int(r["rating_number"] or 0)))
    with open(config.CANDIDATE_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CANDIDATE_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="dev-only: cap additional rows scanned this run")
    parser.add_argument("--force", action="store_true", help="ignore an existing completed checkpoint")
    args = parser.parse_args()

    config.INTERIM_DIR.mkdir(parents=True, exist_ok=True)

    checkpoint = empty_checkpoint() if args.force else load_checkpoint()

    if checkpoint["complete"] and not args.force:
        print("A previous run already completed a full metadata scan (checkpoint says complete=true).")
        print(f"Candidates are in {config.CANDIDATE_CSV_PATH}")
        print("Pass --force to re-scan from scratch, or mark 'selected' and move on to `python -m src.fetch_reviews`.")
        return

    resume_byte_offset = checkpoint["byte_offset"]
    total_scanned = checkpoint["total_scanned"]
    matched_pre_exclusion = checkpoint["matched_pre_exclusion_by_category"]
    excluded_by_category_tier = checkpoint["excluded_by_category_and_tier"]
    invalid_json_lines = checkpoint["invalid_json_lines"]
    unparseable_price_count = checkpoint["unparseable_price_count"]

    total_bytes = http_jsonl.get_content_length(config.META_JSONL_URL)
    if resume_byte_offset:
        print(f"Resuming via HTTP Range request at byte {resume_byte_offset:,} "
              f"({total_scanned:,} rows already scanned).")
    if args.limit:
        print(f"--limit {args.limit}: this is a dev smoke-test run, not a real full pass.")

    file_mode = "a" if resume_byte_offset else "w"
    candidates_f = open(config.CANDIDATE_CSV_PATH, file_mode, newline="", encoding="utf-8")
    candidates_writer = csv.DictWriter(candidates_f, fieldnames=CANDIDATE_FIELDNAMES)
    excluded_f = open(config.EXCLUDED_PRODUCTS_CSV_PATH, file_mode, newline="", encoding="utf-8")
    excluded_writer = csv.DictWriter(excluded_f, fieldnames=EXCLUDED_FIELDNAMES)
    if file_mode == "w":
        candidates_writer.writeheader()
        excluded_writer.writeheader()

    reader = http_jsonl.iter_lines_resumable(config.META_JSONL_URL, start_byte=resume_byte_offset)
    scanned_this_run = 0
    current_byte_offset = resume_byte_offset

    pbar = tqdm(
        total=total_bytes, initial=resume_byte_offset,
        unit="B", unit_scale=True, unit_divisor=1024, desc="scanning metadata",
    )

    def checkpoint_dict(complete: bool) -> dict:
        return {
            "byte_offset": current_byte_offset,
            "total_scanned": total_scanned + scanned_this_run,
            "matched_pre_exclusion_by_category": matched_pre_exclusion,
            "excluded_by_category_and_tier": excluded_by_category_tier,
            "invalid_json_lines": invalid_json_lines,
            "unparseable_price_count": unparseable_price_count,
            "complete": complete,
        }

    try:
        for next_byte_offset, raw_line in reader:
            scanned_this_run += 1
            pbar.update(next_byte_offset - current_byte_offset)
            current_byte_offset = next_byte_offset

            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError:
                invalid_json_lines += 1
                continue

            title = item.get("title") or ""
            category = matches_category(title)
            if category is None:
                continue

            matched_pre_exclusion[category] += 1

            excluded, matched_term, tier = config.check_exclusion(title, category)
            if excluded:
                excluded_by_category_tier[category][tier] += 1
                excluded_writer.writerow(
                    {"parent_asin": item.get("parent_asin"), "title": title, "category": category,
                     "matched_term": matched_term, "tier": tier}
                )
                continue

            details = item.get("details")
            brand_raw = (
                item.get("store")
                or (details.get("Brand") if isinstance(details, dict) else None)
                or ""
            )
            brand_normalized = config.normalize_brand(brand_raw, category)

            price_raw = item.get("price")
            price = parse_price(price_raw)
            if price is None and price_raw not in (None, "", "None"):
                unparseable_price_count += 1

            candidates_writer.writerow(
                {
                    "parent_asin": item.get("parent_asin"),
                    "title": title,
                    "brand_raw": brand_raw,
                    "brand_normalized": brand_normalized,
                    "category": category,
                    "main_category": item.get("main_category"),
                    "average_rating": item.get("average_rating"),
                    "rating_number": item.get("rating_number") or 0,
                    "price": price,
                    "selected": "",
                }
            )

            if scanned_this_run % config.CHECKPOINT_INTERVAL == 0:
                candidates_f.flush()
                excluded_f.flush()
                save_checkpoint(checkpoint_dict(complete=False))

            if args.limit and scanned_this_run >= args.limit:
                break
    finally:
        pbar.close()
        candidates_f.close()
        excluded_f.close()
        save_checkpoint(checkpoint_dict(complete=False))

    is_full_pass = args.limit is None
    final_checkpoint = checkpoint_dict(complete=is_full_pass)
    save_checkpoint(final_checkpoint)

    resort_candidates_csv()

    with open(config.CANDIDATE_CSV_PATH, newline="", encoding="utf-8") as f:
        candidates = list(csv.DictReader(f))

    funnel = {
        "total_metadata_scanned": final_checkpoint["total_scanned"],
        "matched_pre_exclusion_by_category": matched_pre_exclusion,
        "excluded_by_category_and_tier": excluded_by_category_tier,
        "invalid_json_lines": invalid_json_lines,
        "unparseable_price_count": unparseable_price_count,
        "final_candidates_by_category": {
            cat: sum(1 for c in candidates if c["category"] == cat)
            for cat in config.CATEGORY_KEYWORDS
        },
        "full_pass_complete": is_full_pass,
    }
    config.METADATA_FUNNEL_PATH.write_text(json.dumps(funnel, indent=2))

    print(f"\nScanned {scanned_this_run:,} rows this run ({funnel['total_metadata_scanned']:,} total, "
          f"byte {current_byte_offset:,}" + (f" of {total_bytes:,}" if total_bytes else "") + ").")
    print(f"{len(candidates):,} candidates in {config.CANDIDATE_CSV_PATH}")
    print(json.dumps(funnel, indent=2))

    if is_full_pass:
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
    else:
        print("\nThis was a limited/interrupted run -- rerun without --limit to continue/complete "
              "(it will resume from the checkpointed byte offset, not from scratch).")


if __name__ == "__main__":
    main()
