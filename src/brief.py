"""Weekly stakeholder brief: compute the biggest quarter-over-quarter
complaint-share changes in SQL, then have the LLM write a short brief from
those numbers ONLY.

The SQL (find_biggest_qoq_changes) is the single source of truth for every
number that can legitimately appear in the brief. The LLM is given exactly
that result set and nothing else, explicitly told not to introduce any
number not in it. After generation, validate_brief_numbers() extracts every
numeric token from the brief text and checks it against the set of numbers
actually present in the SQL results (allowing for reasonable rounding/
percent-vs-decimal formatting) -- if anything doesn't match, the brief is
NOT saved; the mismatch is printed instead so it can be inspected.

Requires GEMINI_API_KEY in .env (same key as src/extract.py).

Usage:
    python -m src.brief                 # print + save to reports/example_brief.md
    python -m src.brief --no-save       # print only
"""

from __future__ import annotations

import argparse
import os
import re

import duckdb
import requests
from dotenv import load_dotenv

from src import config


def find_biggest_qoq_changes(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """
        WITH extracted AS (
            SELECT p.category, p.brand_normalized AS brand,
                   EXTRACT(year FROM r.review_timestamp)::INT AS yr,
                   ((EXTRACT(month FROM r.review_timestamp)::INT - 1) // 3 + 1) AS qtr,
                   e.complaint_category
            FROM reviews r
            JOIN products p ON r.parent_asin = p.parent_asin
            JOIN extractions e ON r.review_id = e.review_id
            WHERE e.model = ?
        ),
        quarterly AS (
            SELECT category, brand, yr, qtr,
                   COUNT(*) AS total_reviews,
                   SUM(CASE WHEN complaint_category != 'no_complaint' THEN 1 ELSE 0 END) AS complaint_reviews,
                   SUM(CASE WHEN complaint_category != 'no_complaint' THEN 1 ELSE 0 END)::DOUBLE / COUNT(*) AS complaint_share
            FROM extracted
            GROUP BY category, brand, yr, qtr
        ),
        with_lag AS (
            SELECT *,
                   LAG(complaint_share) OVER (PARTITION BY category, brand ORDER BY yr, qtr) AS prev_share,
                   LAG(yr) OVER (PARTITION BY category, brand ORDER BY yr, qtr) AS prev_yr,
                   LAG(qtr) OVER (PARTITION BY category, brand ORDER BY yr, qtr) AS prev_qtr,
                   LAG(total_reviews) OVER (PARTITION BY category, brand ORDER BY yr, qtr) AS prev_total_reviews
            FROM quarterly
        )
        SELECT category, brand, yr, qtr, total_reviews, complaint_reviews, complaint_share,
               prev_yr, prev_qtr, prev_total_reviews, prev_share,
               (complaint_share - prev_share) AS qoq_change
        FROM with_lag
        WHERE prev_share IS NOT NULL
          AND total_reviews >= ?
          AND prev_total_reviews >= ?
        ORDER BY ABS(complaint_share - prev_share) DESC
        LIMIT ?
        """,
        [config.EXTRACTION_MODEL, config.BRIEF_MIN_QUARTER_REVIEWS, config.BRIEF_MIN_QUARTER_REVIEWS, config.BRIEF_TOP_N_CHANGES],
    ).fetchall()
    cols = [
        "category", "brand", "year", "quarter", "total_reviews", "complaint_reviews", "complaint_share",
        "prev_year", "prev_quarter", "prev_total_reviews", "prev_share", "qoq_change",
    ]
    return [dict(zip(cols, row)) for row in rows]


def allowed_numbers(results: list[dict]) -> set[str]:
    """Every number that's legitimately derivable from the SQL results, as
    normalized strings, for checking the brief against. Includes both
    fraction (0.xx) and percentage (xx.x / xx) forms since the LLM may use
    either, and a couple of roundings of each.
    """
    allowed: set[str] = set()

    def add_number(x):
        if x is None:
            return
        for nd in (0, 1, 2):
            allowed.add(f"{round(float(x), nd):g}")

    def add_percent(fraction):
        if fraction is None:
            return
        pct = fraction * 100
        for nd in (0, 1):
            allowed.add(f"{round(pct, nd):g}")

    for r in results:
        add_number(r["total_reviews"])
        add_number(r["complaint_reviews"])
        add_number(r["prev_total_reviews"])
        add_number(r["year"])
        add_number(r["prev_year"])
        add_number(r["quarter"])
        add_number(r["prev_quarter"])
        add_percent(r["complaint_share"])
        add_percent(r["prev_share"])
        add_percent(r["qoq_change"])
        add_percent(abs(r["qoq_change"]))
        add_number(r["qoq_change"])
        add_number(abs(r["qoq_change"]))

    return allowed


NUMBER_RE = re.compile(r"-?\d+\.?\d*")


def validate_brief_numbers(brief_text: str, results: list[dict]) -> list[str]:
    """Returns a list of numeric tokens found in the brief that don't match
    anything derivable from the SQL results. Empty list means it's clean.
    """
    allowed = allowed_numbers(results)
    problems = []
    for token in NUMBER_RE.findall(brief_text):
        try:
            value = float(token)
        except ValueError:
            continue
        normalized = {f"{round(value, nd):g}" for nd in (0, 1, 2)}
        if not (normalized & allowed):
            problems.append(token)
    return problems


def render_results_for_prompt(results: list[dict]) -> str:
    lines = []
    for r in results:
        direction = "up" if r["qoq_change"] > 0 else "down"
        lines.append(
            f"- {r['category']} / {r['brand']}: complaint share went {direction} from "
            f"{r['prev_share']*100:.1f}% (Q{r['prev_quarter']} {r['prev_year']}, n={r['prev_total_reviews']}) "
            f"to {r['complaint_share']*100:.1f}% (Q{r['quarter']} {r['year']}, n={r['total_reviews']}), "
            f"a change of {r['qoq_change']*100:+.1f} percentage points."
        )
    return "\n".join(lines)


def call_gemini_text(prompt: str, api_key: str) -> str:
    url = f"{config.EXTRACTION_API_BASE}/models/{config.BRIEF_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}}
    resp = requests.post(url, params={"key": api_key}, json=body, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def build_brief_prompt(results: list[dict]) -> str:
    return (
        "Write a short stakeholder brief (150-250 words) summarizing the biggest "
        "quarter-over-quarter changes in complaint share for a kitchen/home appliance "
        "product line, based ONLY on the numbers below. Do not introduce any number, "
        "percentage, product, brand, or quarter that is not listed below. If you are "
        "unsure of a figure, omit it rather than estimate it. Plain, direct language, "
        "no marketing tone.\n\n"
        "DATA (every number you may use is here):\n"
        f"{render_results_for_prompt(results)}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-save", action="store_true", help="print only, don't write reports/example_brief.md")
    args = parser.parse_args()

    if not config.DUCKDB_PATH.exists():
        raise SystemExit(f"{config.DUCKDB_PATH} not found.")

    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not found in environment/.env.")

    con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    results = find_biggest_qoq_changes(con)
    con.close()

    if not results:
        raise SystemExit(
            "No quarter-over-quarter comparisons available yet (need at least two quarters "
            f"of >= {config.BRIEF_MIN_QUARTER_REVIEWS} extracted reviews for some brand/category). "
            "Run src/extract.py --target in_sample to completion first."
        )

    print("SQL results feeding the brief:")
    print(render_results_for_prompt(results))
    print()

    prompt = build_brief_prompt(results)
    brief_text = call_gemini_text(prompt, api_key)

    problems = validate_brief_numbers(brief_text, results)
    if problems:
        print("VALIDATION FAILED -- the brief mentions numbers not derivable from the SQL results:")
        for p in problems:
            print(f"  - {p}")
        print("\nBrief text (NOT saved):\n")
        print(brief_text)
        raise SystemExit(1)

    print("Validation passed: every number in the brief matches the SQL results.\n")
    print(brief_text)

    if not args.no_save:
        config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        example_path = config.REPORTS_DIR / "example_brief.md"
        example_path.write_text(
            "# Example weekly brief\n\n"
            f"Generated by `src/brief.py` using {config.BRIEF_MODEL}. Every number below is "
            "validated against the SQL query in `find_biggest_qoq_changes()` -- see CLAUDE.md.\n\n"
            + brief_text.strip()
            + "\n"
        )
        print(f"\nWrote {example_path}")


if __name__ == "__main__":
    main()
