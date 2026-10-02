"""Phase 2: LLM complaint extraction via Gemini Flash (Google AI Studio).

Batches ~20 reviews per request (config.EXTRACTION_BATCH_SIZE), asks for
structured JSON output (Gemini's native responseSchema), validates every
item with Pydantic plus the same taxonomy rules used for reference labels
(src/taxonomy.py), and retries a batch with a corrective follow-up if its
output is malformed. The `extractions` table (review_id, model) is both the
cache and the resume mechanism: a review already extracted by this model is
never re-sent to the API, so a rerun only processes what's left.

Requires GEMINI_API_KEY in .env. Model name, pricing, and free-tier limits
in src/config.py are placeholders -- VERIFY against
https://ai.google.dev/gemini-api/docs before a real run.

Usage:
    python -m src.extract --target label_sample --estimate-only
    python -m src.extract --target label_sample
    python -m src.extract --target in_sample
    python -m src.extract --target in_sample --limit 100   # dev-only cap
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone

import duckdb
import requests
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from src import config
from src import taxonomy as taxonomy_mod


class ExtractionItem(BaseModel):
    review_id: str
    complaint_category: str
    severity: int | None = None
    note: str | None = None


class RateLimitError(Exception):
    def __init__(self, retry_after: float | None):
        self.retry_after = retry_after


def get_pending_reviews(con: duckdb.DuckDBPyConnection, target: str, model: str) -> list[dict]:
    if target == "label_sample":
        source = "JOIN label_sample ls ON r.review_id = ls.review_id"
        extra_where = ""
    elif target == "in_sample":
        source = ""
        extra_where = "AND r.in_sample = TRUE"
    else:
        raise ValueError(target)

    rows = con.execute(
        f"""
        SELECT r.review_id, r.title, r.text
        FROM reviews r
        {source}
        LEFT JOIN extractions e ON r.review_id = e.review_id AND e.model = ?
        WHERE e.review_id IS NULL {extra_where}
        ORDER BY r.review_id
        """,
        [model],
    ).fetchall()
    return [{"review_id": rid, "title": title or "", "text": text} for rid, title, text in rows]


def build_response_schema(valid_categories: set[str]) -> dict:
    return {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "review_id": {"type": "STRING"},
                "complaint_category": {"type": "STRING", "enum": sorted(valid_categories)},
                "severity": {"type": "INTEGER", "nullable": True},
                "note": {"type": "STRING", "nullable": True},
            },
            "required": ["review_id", "complaint_category"],
        },
    }


def build_batch_prompt(reviews: list[dict], taxonomy_block: str) -> str:
    lines = [
        "You are extracting product complaints from Amazon reviews of kitchen/home appliances.",
        "",
        taxonomy_block,
        "",
        "For EACH review below, return one JSON object: review_id, complaint_category, "
        "severity (1-3, or null for no_complaint), note (a short free-text string, required "
        "only when complaint_category is 'other', else null).",
        "Return a JSON array with exactly one object per review below. Every review_id listed "
        "must appear exactly once, and no other review_id may appear.",
        "",
        "REVIEWS:",
    ]
    for r in reviews:
        text = r["text"]
        if len(text) > 2000:
            text = text[:2000] + " [truncated]"
        lines.append(f"---\nreview_id: {r['review_id']}\ntitle: {r['title']}\ntext: {text}")
    return "\n".join(lines)


def call_gemini(prompt: str, schema: dict, api_key: str) -> str:
    url = f"{config.EXTRACTION_API_BASE}/models/{config.EXTRACTION_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0,
        },
    }
    resp = requests.post(url, params={"key": api_key}, json=body, timeout=120)
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        raise RateLimitError(float(retry_after) if retry_after else None)
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError(f"no candidates in response: {data}")
    parts = candidates[0].get("content", {}).get("parts") or []
    if not parts or "text" not in parts[0]:
        raise ValueError(f"no text part in response: {data}")
    return parts[0]["text"]


def call_gemini_with_backoff(prompt: str, schema: dict, api_key: str) -> str:
    wait = config.EXTRACTION_RATE_LIMIT_BASE_WAIT_SECONDS
    last_exc = None
    for attempt in range(1, config.EXTRACTION_MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return call_gemini(prompt, schema, api_key)
        except RateLimitError as e:
            sleep_s = e.retry_after if e.retry_after else wait
            print(f"    rate limited, waiting {sleep_s:.0f}s (retry {attempt}/{config.EXTRACTION_MAX_RATE_LIMIT_RETRIES})...")
            time.sleep(sleep_s)
            wait = min(wait * 2, 120)
            last_exc = e
        except requests.exceptions.RequestException as e:
            print(f"    network error ({e}), waiting {wait:.0f}s (retry {attempt}/{config.EXTRACTION_MAX_RATE_LIMIT_RETRIES})...")
            time.sleep(wait)
            wait = min(wait * 2, 120)
            last_exc = e
    raise RuntimeError(f"Exceeded rate-limit/network retries: {last_exc}")


def process_batch(
    reviews: list[dict], taxonomy_block: str, valid_categories: set[str], schema: dict, api_key: str
) -> tuple[list[ExtractionItem], str | None, set[str]]:
    """Returns (validated items, last raw response text, review_ids still missing/failed)."""
    expected_ids = {r["review_id"] for r in reviews}
    prompt = build_batch_prompt(reviews, taxonomy_block)
    raw_text = None

    for attempt in range(1, config.EXTRACTION_MAX_MALFORMED_RETRIES + 1):
        errors: list[str] = []
        try:
            raw_text = call_gemini_with_backoff(prompt, schema, api_key)
            items = json.loads(raw_text)
            if not isinstance(items, list):
                raise ValueError("response is not a JSON array")
        except (json.JSONDecodeError, ValueError) as e:
            errors.append(f"could not parse response: {e}")
            items = []

        parsed: list[ExtractionItem] = []
        seen_ids: set[str] = set()
        for raw_item in items:
            try:
                item = ExtractionItem(**raw_item)
            except ValidationError as e:
                errors.append(f"schema error: {e}")
                continue
            if item.review_id not in expected_ids:
                errors.append(f"unexpected review_id {item.review_id}")
                continue
            if item.review_id in seen_ids:
                errors.append(f"duplicate review_id {item.review_id} in response")
                continue
            label_errors = taxonomy_mod.validate_label(item.complaint_category, item.severity, item.note, valid_categories)
            if label_errors:
                errors.append(f"{item.review_id}: {'; '.join(label_errors)}")
                continue
            seen_ids.add(item.review_id)
            parsed.append(item)

        missing = expected_ids - seen_ids
        if missing:
            errors.append(f"missing {len(missing)} review_id(s): {sorted(missing)[:5]}")

        if not errors:
            return parsed, raw_text, set()

        if attempt < config.EXTRACTION_MAX_MALFORMED_RETRIES:
            print(f"    batch malformed (attempt {attempt}/{config.EXTRACTION_MAX_MALFORMED_RETRIES}): "
                  f"{errors[0]}{' (+more)' if len(errors) > 1 else ''}; retrying...")
            prompt = (
                build_batch_prompt(reviews, taxonomy_block)
                + "\n\nYour previous response had problems: "
                + "; ".join(errors[:5])
                + ". Return a corrected JSON array covering every review_id exactly once."
            )
        else:
            print(f"    batch still malformed after {config.EXTRACTION_MAX_MALFORMED_RETRIES} attempts, "
                  f"giving up on {len(missing) or len(expected_ids) - len(parsed)} review(s) this run.")
            return parsed, raw_text, missing or (expected_ids - seen_ids)

    return [], raw_text, expected_ids


def estimate_cost(reviews: list[dict], taxonomy_block: str) -> dict:
    chars_per_token = 4  # rough heuristic for English text
    n_batches = math.ceil(len(reviews) / config.EXTRACTION_BATCH_SIZE) if reviews else 0
    total_input_chars = 0
    for i in range(0, len(reviews), config.EXTRACTION_BATCH_SIZE):
        total_input_chars += len(build_batch_prompt(reviews[i : i + config.EXTRACTION_BATCH_SIZE], taxonomy_block))
    est_input_tokens = total_input_chars / chars_per_token
    est_output_tokens = len(reviews) * 40  # rough: one small JSON object per review
    est_cost = (
        est_input_tokens / 1_000_000 * config.EXTRACTION_EST_INPUT_COST_PER_MILLION
        + est_output_tokens / 1_000_000 * config.EXTRACTION_EST_OUTPUT_COST_PER_MILLION
    )
    return {
        "n_reviews": len(reviews),
        "n_batches": n_batches,
        "est_input_tokens": int(est_input_tokens),
        "est_output_tokens": int(est_output_tokens),
        "est_cost_usd": round(est_cost, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["label_sample", "in_sample"], required=True)
    parser.add_argument("--estimate-only", action="store_true", help="print cost estimate, make no API calls")
    parser.add_argument("--limit", type=int, default=None, help="dev-only: cap PENDING reviews processed this run")
    args = parser.parse_args()

    if not config.DUCKDB_PATH.exists():
        raise SystemExit(f"{config.DUCKDB_PATH} not found. Run the pipeline through load_duckdb.py first.")

    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")

    con = duckdb.connect(str(config.DUCKDB_PATH))
    pending = get_pending_reviews(con, args.target, config.EXTRACTION_MODEL)
    already_done = con.execute(
        "SELECT COUNT(*) FROM extractions WHERE model = ?", [config.EXTRACTION_MODEL]
    ).fetchone()[0]

    valid_categories = taxonomy_mod.valid_category_keys()
    taxonomy_block = taxonomy_mod.render_prompt_block()

    print(f"Target: {args.target}. Model: {config.EXTRACTION_MODEL}.")
    print(f"{len(pending):,} reviews pending extraction, {already_done:,} already cached (will not be re-sent).")

    estimate = estimate_cost(pending, taxonomy_block)
    print(
        f"\nCost estimate (ROUGH -- verify current Gemini pricing/free-tier limits before relying on this):\n"
        f"  ~{estimate['est_input_tokens']:,} input tokens, ~{estimate['est_output_tokens']:,} output tokens, "
        f"{estimate['n_batches']} API calls\n"
        f"  ~${estimate['est_cost_usd']:.4f} at placeholder rates "
        f"(${config.EXTRACTION_EST_INPUT_COST_PER_MILLION}/1M in, ${config.EXTRACTION_EST_OUTPUT_COST_PER_MILLION}/1M out) "
        f"-- likely $0 if this run stays inside the Google AI Studio free tier."
    )

    if args.estimate_only:
        print("\n--estimate-only: stopping before any API calls.")
        con.close()
        return

    if not pending:
        print("\nNothing to do.")
        con.close()
        return

    if not api_key:
        con.close()
        raise SystemExit("\nGEMINI_API_KEY not found (checked .env and environment). Add it and rerun.")

    if args.limit:
        pending = pending[: args.limit]
        print(f"\n--limit {args.limit}: dev-only cap, processing {len(pending)} of the pending reviews this run.")

    schema = build_response_schema(valid_categories)
    n_batches_total = math.ceil(len(pending) / config.EXTRACTION_BATCH_SIZE)
    n_success = 0
    n_failed = 0

    for batch_num, i in enumerate(range(0, len(pending), config.EXTRACTION_BATCH_SIZE), start=1):
        batch = pending[i : i + config.EXTRACTION_BATCH_SIZE]
        parsed, raw_text, missing = process_batch(batch, taxonomy_block, valid_categories, schema, api_key)

        if parsed:
            now = datetime.now(timezone.utc)
            con.executemany(
                """
                INSERT INTO extractions (review_id, model, complaint_category, severity, note, raw_response, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (item.review_id, config.EXTRACTION_MODEL, item.complaint_category, item.severity, item.note, raw_text, now)
                    for item in parsed
                ],
            )
            n_success += len(parsed)
        n_failed += len(missing)

        print(f"  batch {batch_num}/{n_batches_total}: {len(parsed)} extracted, {len(missing)} failed "
              f"(total so far: {n_success} ok, {n_failed} failed)")

    con.close()
    print(f"\nDone. {n_success:,} extracted this run, {n_failed:,} failed "
          "(not cached, so they'll be retried automatically on the next run).")


if __name__ == "__main__":
    main()
