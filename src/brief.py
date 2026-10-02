"""Weekly stakeholder brief: compute the biggest STATISTICALLY SIGNIFICANT
quarter-over-quarter complaint-share changes in SQL, then have the LLM
write a short brief from those numbers ONLY.

Neither a minimum-sample guard (BRIEF_MIN_QUARTER_REVIEWS) nor a plain,
uncorrected two-proportion z-test is enough to keep noise out. An earlier
version of this brief, using the n>=10 guard alone, led with Cosori's
-25.3-then-+26.3-percentage-point reversal. Adding an uncorrected z-test
(p < 0.05, no multiple-comparisons correction) did NOT catch this: both
legs of that exact reversal came back individually "significant"
(p=0.026, p=0.017) -- testing dozens of brand/category/quarter pairs means
some will clear p<0.05 purely by chance, and an immediate reversal is the
textbook signature of that. The fix is a Benjamini-Hochberg false discovery
rate correction (q <= BRIEF_SIGNIFICANCE_ALPHA, default 0.05) applied across
ALL tested pairs at once, not a per-pair threshold. Bonferroni was
considered and rejected as too conservative at this sample size (it would
likely leave nothing significant at all); a rule that specifically detects
reversals was also considered and rejected for targeting this one observed
case rather than the general multiple-comparisons problem. See CLAUDE.md.

If nothing survives the BH correction, the brief says so directly, then
separately lists the largest raw swings, explicitly labeled as NOT
significant, with their sample sizes -- generated deterministically in
Python, not by the LLM, since there's no judgment call left to make once
"nothing is significant" is established.

The SQL (compute_all_qoq_changes) is the single source of truth for every
number that can legitimately appear in the brief. When the LLM is used
(only when at least one change is significant), it's given exactly that
filtered result set and nothing else, explicitly told not to introduce any
number not in it. After generation, validate_brief_numbers() extracts every
numeric token from the brief text and checks it against the set of numbers
actually present in the SQL results (allowing for reasonable rounding/
percent-vs-decimal formatting) -- if anything doesn't match, the brief is
NOT saved; the mismatch is printed instead so it can be inspected.

Requires GEMINI_API_KEY in .env (same key as src/extract.py) when at least
one change is significant; no API call is made otherwise.

Usage:
    python -m src.brief                 # print + save to reports/example_brief.md
    python -m src.brief --no-save       # print only
"""

from __future__ import annotations

import argparse
import math
import os
import re

import duckdb
import requests
from dotenv import load_dotenv

from src import config

NO_SIGNIFICANT_CHANGES_MESSAGE = "No statistically reliable changes in this period."


def two_proportion_z_test(x1: int, n1: int, x2: int, n2: int) -> tuple[float, float]:
    """Two-proportion z-test comparing x2/n2 against x1/n1. Returns (z, two-tailed p-value).

    Uses math.erf (stdlib) instead of scipy, since this is the only place
    that would need it -- not worth the dependency for one normal-CDF call.
    """
    p1, p2 = x1 / n1, x2 / n2
    pooled = (x1 + x2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0  # both proportions identical (and degenerate); no evidence of a difference
    z = (p2 - p1) / se
    p_value = 1 - math.erf(abs(z) / math.sqrt(2))
    return z, p_value


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[float]:
    """Benjamini-Hochberg step-up FDR correction. Returns adjusted p-values
    (q-values) in the SAME order as the input; compare each to `alpha`.

    Standard algorithm: rank p-values ascending, adjusted_p(rank) =
    min(p(rank) * m / rank, adjusted_p(rank+1)) -- the running minimum from
    the largest rank down enforces monotonicity (an adjusted p-value may
    never be smaller than one belonging to a larger raw p-value).
    """
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running_min = 1.0
    for rank in range(m, 0, -1):
        idx = order[rank - 1]
        candidate = p_values[idx] * m / rank
        running_min = min(running_min, candidate)
        adjusted[idx] = running_min
    return adjusted


def compute_all_qoq_changes(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Every candidate quarter-over-quarter change meeting the minimum
    sample guard, with both the raw two-proportion z-test p-value and the
    Benjamini-Hochberg-adjusted p-value (computed across this entire set,
    as BH requires -- it is not a per-row calculation).
    """
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
                   LAG(total_reviews) OVER (PARTITION BY category, brand ORDER BY yr, qtr) AS prev_total_reviews,
                   LAG(complaint_reviews) OVER (PARTITION BY category, brand ORDER BY yr, qtr) AS prev_complaint_reviews
            FROM quarterly
        )
        SELECT category, brand, yr, qtr, total_reviews, complaint_reviews, complaint_share,
               prev_yr, prev_qtr, prev_total_reviews, prev_complaint_reviews, prev_share,
               (complaint_share - prev_share) AS qoq_change
        FROM with_lag
        WHERE prev_share IS NOT NULL
          AND total_reviews >= ?
          AND prev_total_reviews >= ?
        """,
        [config.EXTRACTION_MODEL, config.BRIEF_MIN_QUARTER_REVIEWS, config.BRIEF_MIN_QUARTER_REVIEWS],
    ).fetchall()
    cols = [
        "category", "brand", "year", "quarter", "total_reviews", "complaint_reviews", "complaint_share",
        "prev_year", "prev_quarter", "prev_total_reviews", "prev_complaint_reviews", "prev_share", "qoq_change",
    ]
    candidates = [dict(zip(cols, row)) for row in rows]

    for r in candidates:
        z, p_value = two_proportion_z_test(
            r["prev_complaint_reviews"], r["prev_total_reviews"], r["complaint_reviews"], r["total_reviews"]
        )
        r["z_score"] = z
        r["p_value"] = p_value

    adjusted = benjamini_hochberg([r["p_value"] for r in candidates], config.BRIEF_SIGNIFICANCE_ALPHA)
    for r, q in zip(candidates, adjusted):
        r["bh_adjusted_p"] = q

    return candidates


def find_biggest_qoq_changes(con: duckdb.DuckDBPyConnection) -> tuple[list[dict], list[dict]]:
    """Returns (significant_changes, top_raw_changes). significant_changes
    is what the LLM is allowed to write about (BH-adjusted p < alpha,
    biggest |change| first). top_raw_changes is the biggest |change|
    overall regardless of significance, for the "nothing significant, here
    are the raw swings for transparency" fallback -- not meant to be passed
    to the LLM.
    """
    all_candidates = compute_all_qoq_changes(con)

    significant = [r for r in all_candidates if r["bh_adjusted_p"] <= config.BRIEF_SIGNIFICANCE_ALPHA]
    significant.sort(key=lambda r: abs(r["qoq_change"]), reverse=True)

    top_raw = sorted(all_candidates, key=lambda r: abs(r["qoq_change"]), reverse=True)
    return significant[: config.BRIEF_TOP_N_CHANGES], top_raw[: config.BRIEF_TOP_N_CHANGES]


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
        if "bh_adjusted_p" in r:
            add_number(r["bh_adjusted_p"])
        if "p_value" in r:
            add_number(r["p_value"])

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


def render_raw_swings_fallback(top_raw: list[dict]) -> str:
    """Deterministic (no LLM) fallback text for when nothing is
    significant: the fixed header plus the largest raw swings, explicitly
    labeled not significant. No judgment call is needed here, so there's
    nothing for an LLM to usefully add.
    """
    lines = [
        NO_SIGNIFICANT_CHANGES_MESSAGE,
        "",
        "Largest raw swings this period (NOT statistically significant after Benjamini-Hochberg correction):",
    ]
    for r in top_raw:
        direction = "up" if r["qoq_change"] > 0 else "down"
        lines.append(
            f"- {r['category']} / {r['brand']}: {r['prev_share']*100:.1f}% "
            f"(Q{r['prev_quarter']} {r['prev_year']}, n={r['prev_total_reviews']}) to "
            f"{r['complaint_share']*100:.1f}% (Q{r['quarter']} {r['year']}, n={r['total_reviews']}), "
            f"{direction} {abs(r['qoq_change'])*100:.1f} points "
            f"(not significant, adjusted p={r['bh_adjusted_p']:.3f})"
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

    con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    significant, top_raw = find_biggest_qoq_changes(con)
    con.close()

    if not significant:
        print(f"No quarter-over-quarter change survived Benjamini-Hochberg correction "
              f"(q <= {config.BRIEF_SIGNIFICANCE_ALPHA}) this period. No API call made.\n")
        if top_raw:
            print("Largest raw swings (not significant), for reference:")
            for r in top_raw:
                print(f"  {r['category']}/{r['brand']}: {r['prev_share']*100:.1f}% -> "
                      f"{r['complaint_share']*100:.1f}% (adjusted p={r['bh_adjusted_p']:.3f})")
        brief_text = render_raw_swings_fallback(top_raw)
        print()
        print(brief_text)

        problems = validate_brief_numbers(brief_text, top_raw)
        if problems:
            # Should not happen (this text is generated, not LLM-written) -- but check anyway, per spec.
            print("\nVALIDATION FAILED on the deterministic fallback text itself (this indicates a bug):")
            for p in problems:
                print(f"  - {p}")
            raise SystemExit(1)
    else:
        load_dotenv()
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise SystemExit("GEMINI_API_KEY not found in environment/.env.")

        print(f"SQL results feeding the brief (BH-adjusted q <= {config.BRIEF_SIGNIFICANCE_ALPHA}):")
        print(render_results_for_prompt(significant))
        print()

        prompt = build_brief_prompt(significant)
        brief_text = call_gemini_text(prompt, api_key)

        problems = validate_brief_numbers(brief_text, significant)
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
        provenance = (
            f"Generated by `src/brief.py` using {config.BRIEF_MODEL}. Every number below is "
            "validated against the SQL query in `find_biggest_qoq_changes()` -- see CLAUDE.md."
            if significant
            else "Generated by `src/brief.py`. No LLM call was made: nothing survived "
                 "Benjamini-Hochberg correction, so the text below is deterministic, not model-written. "
                 "See CLAUDE.md."
        )
        example_path.write_text(f"# Example weekly brief\n\n{provenance}\n\n{brief_text.strip()}\n")
        print(f"\nWrote {example_path}")


if __name__ == "__main__":
    main()
