"""Phase 1, step 2: one full streaming pass over Home_and_Kitchen reviews,
keeping every review for the products marked `selected = y` in
data/interim/candidate_products.csv.

Deliberately NOT an early-stop / per-product-capped fetch: stopping as soon
as a target count is hit would bias the kept reviews toward whatever the
dataset's internal file order happens to favor (e.g. skewing time trends).
Instead this does one full pass and keeps everything that matches, so
downstream trend analysis sees the true population for the selected
products. A fixed-size random sample for LLM extraction is drawn
separately afterwards (see src/select_sample.py), with its own flag.

This streams through the full ~31GB Home_and_Kitchen reviews file over the
network exactly once (bytes for non-matching rows aren't persisted to disk,
but they are still read off the wire) -- that's the necessary tradeoff for
an unbiased sample.

Resume uses HTTP byte-offset Range requests (see src/http_jsonl.py), not
`datasets`' IterableDataset.skip() -- .skip() was found to re-iterate the
stream from byte 0 and discard already-seen records, which would make every
resume re-download everything already scanned. The checkpoint only ever
stores the byte offset of the START of the next unprocessed line (recorded
after a line is fully parsed and handled), so a Range request on resume
always lands exactly on a line boundary.

Usage:
    python -m src.fetch_reviews [--limit N] [--force]

--limit is a development/smoke-test knob only (caps how many additional
rows are scanned this run, and does NOT mark the pass complete) -- omit it
for the real run.
--force re-runs from scratch even if a previous run already completed.
"""

from __future__ import annotations

import argparse
import json

from tqdm import tqdm

from src import config, http_jsonl


def load_checkpoint() -> dict:
    if config.FETCH_CHECKPOINT_PATH.exists():
        return json.loads(config.FETCH_CHECKPOINT_PATH.read_text())
    return {"byte_offset": 0, "examples_processed": 0, "matches_found": {}, "complete": False}


def save_checkpoint(checkpoint: dict) -> None:
    config.FETCH_CHECKPOINT_PATH.write_text(json.dumps(checkpoint, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="dev-only: cap rows scanned this run")
    parser.add_argument("--force", action="store_true", help="ignore an existing completed checkpoint")
    args = parser.parse_args()

    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    config.INTERIM_DIR.mkdir(parents=True, exist_ok=True)

    selected_asins = config.load_selected_parent_asins()
    print(f"Selected {len(selected_asins)} products: {sorted(selected_asins)}")

    checkpoint = (
        {"byte_offset": 0, "examples_processed": 0, "matches_found": {}, "complete": False}
        if args.force
        else load_checkpoint()
    )

    if checkpoint["complete"] and not args.force:
        print("A previous run already completed a full pass (checkpoint says complete=true).")
        print(f"Matches so far: {checkpoint['matches_found']}")
        print("Pass --force to re-scan from scratch, or proceed straight to `python -m src.clean`.")
        return

    resume_byte_offset = checkpoint["byte_offset"]
    resume_examples_processed = checkpoint["examples_processed"]
    matches_found: dict[str, int] = dict(checkpoint["matches_found"])

    total_bytes = http_jsonl.get_content_length(config.REVIEW_JSONL_URL)
    if resume_byte_offset:
        print(f"Resuming via HTTP Range request at byte {resume_byte_offset:,} "
              f"({resume_examples_processed:,} examples already processed) -- no re-download of prior bytes.")
    if args.limit:
        print(f"--limit {args.limit}: this is a dev smoke-test run, not a real full pass.")

    reader = http_jsonl.iter_jsonl_resumable(config.REVIEW_JSONL_URL, start_byte=resume_byte_offset)

    file_mode = "a" if resume_byte_offset else "w"
    scanned_this_run = 0
    current_byte_offset = resume_byte_offset

    pbar = tqdm(
        total=total_bytes,
        initial=resume_byte_offset,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc="scanning reviews",
    )

    with open(config.RAW_REVIEWS_PATH, file_mode, encoding="utf-8") as out_f:
        try:
            for next_byte_offset, example in reader:
                scanned_this_run += 1
                parent_asin = example.get("parent_asin")
                if parent_asin in selected_asins:
                    out_f.write(json.dumps(example) + "\n")
                    matches_found[parent_asin] = matches_found.get(parent_asin, 0) + 1

                pbar.update(next_byte_offset - current_byte_offset)
                current_byte_offset = next_byte_offset

                if scanned_this_run % config.CHECKPOINT_INTERVAL == 0:
                    out_f.flush()
                    save_checkpoint(
                        {
                            "byte_offset": current_byte_offset,
                            "examples_processed": resume_examples_processed + scanned_this_run,
                            "matches_found": matches_found,
                            "complete": False,
                        }
                    )

                if args.limit and scanned_this_run >= args.limit:
                    break
        finally:
            pbar.close()
            out_f.flush()
            save_checkpoint(
                {
                    "byte_offset": current_byte_offset,
                    "examples_processed": resume_examples_processed + scanned_this_run,
                    "matches_found": matches_found,
                    "complete": False,
                }
            )

    is_full_pass = args.limit is None
    final_checkpoint = {
        "byte_offset": current_byte_offset,
        "examples_processed": resume_examples_processed + scanned_this_run,
        "matches_found": matches_found,
        "complete": is_full_pass,
    }
    save_checkpoint(final_checkpoint)

    stats = {
        "total_examples_scanned": resume_examples_processed + scanned_this_run,
        "matches_by_product": matches_found,
        "total_matches": sum(matches_found.values()),
        "full_pass_complete": is_full_pass,
    }
    config.FETCH_STATS_PATH.write_text(json.dumps(stats, indent=2))

    print(f"\nScanned {scanned_this_run:,} examples this run "
          f"({resume_examples_processed + scanned_this_run:,} total, "
          f"byte {current_byte_offset:,}"
          + (f" of {total_bytes:,}" if total_bytes else "") + ").")
    print(f"Total matches so far: {stats['total_matches']:,}")
    print(json.dumps(matches_found, indent=2))
    if is_full_pass:
        print(f"\nFull pass complete. Raw matches written to {config.RAW_REVIEWS_PATH}")
        print("Next: `python -m src.clean`")
    else:
        print("\nThis was a limited/interrupted run -- rerun without --limit to continue/complete "
              "(it will resume from the checkpointed byte offset, not from scratch).")


if __name__ == "__main__":
    main()
