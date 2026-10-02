"""Export clean CSVs for the Tableau Public dashboard (built manually, not
by this script) from extracted reviews.

Only reviews with a successful extraction (config.EXTRACTION_MODEL) are
included -- run src/extract.py --target in_sample to completion first.

Writes to exports/:
- reviews_extracted.csv: one row per extracted review, joined with product/
  brand/category, plus a quarter label. The detail table; also backs the
  4th chart in DASHBOARD_SPEC.md directly (no separate pre-agg needed for
  a simple category-count chart).
- complaint_share_by_brand_category.csv: per (category, brand), total
  reviews, reviews with any complaint (complaint_category != no_complaint),
  and the resulting share.
- quarterly_trend_by_brand.csv: per (category, brand, year, quarter), the
  same total/complaint/share breakdown, for trend charts over time.
- severity_vs_rating.csv: cross-tab of star rating (1-5) against severity
  (1-3, or "no_complaint"), with counts.

Usage:
    python -m src.export_tableau
"""

from __future__ import annotations

import csv

import duckdb

from src import config


def fetch_extracted_reviews(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """
        SELECT
            r.review_id, p.parent_asin, p.brand_normalized, p.category, p.title AS product_title,
            r.rating, r.review_timestamp, r.verified_purchase, r.helpful_vote,
            e.complaint_category, e.severity, e.note,
            EXTRACT(year FROM r.review_timestamp)::INT AS review_year,
            ((EXTRACT(month FROM r.review_timestamp)::INT - 1) // 3 + 1) AS review_quarter
        FROM reviews r
        JOIN products p ON r.parent_asin = p.parent_asin
        JOIN extractions e ON r.review_id = e.review_id
        WHERE e.model = ?
        ORDER BY p.category, p.brand_normalized, r.review_timestamp
        """,
        [config.EXTRACTION_MODEL],
    ).fetchall()
    cols = [
        "review_id", "parent_asin", "brand", "category", "product_title",
        "rating", "review_timestamp", "verified_purchase", "helpful_vote",
        "complaint_category", "severity", "note", "year", "quarter",
    ]
    return [dict(zip(cols, row)) for row in rows]


def write_csv(path, rows: list[dict], fieldnames: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_complaint_share_by_brand_category(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for r in rows:
        key = (r["category"], r["brand"])
        g = groups.setdefault(key, {"category": r["category"], "brand": r["brand"], "total_reviews": 0, "complaint_reviews": 0})
        g["total_reviews"] += 1
        if r["complaint_category"] != "no_complaint":
            g["complaint_reviews"] += 1
    out = []
    for g in groups.values():
        g["complaint_share"] = round(g["complaint_reviews"] / g["total_reviews"], 4) if g["total_reviews"] else None
        out.append(g)
    out.sort(key=lambda g: (g["category"], -g["complaint_share"]))
    return out


def build_quarterly_trend_by_brand(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for r in rows:
        key = (r["category"], r["brand"], r["year"], r["quarter"])
        g = groups.setdefault(
            key,
            {"category": r["category"], "brand": r["brand"], "year": r["year"], "quarter": r["quarter"],
             "total_reviews": 0, "complaint_reviews": 0},
        )
        g["total_reviews"] += 1
        if r["complaint_category"] != "no_complaint":
            g["complaint_reviews"] += 1
    out = []
    for g in groups.values():
        g["complaint_share"] = round(g["complaint_reviews"] / g["total_reviews"], 4) if g["total_reviews"] else None
        out.append(g)
    out.sort(key=lambda g: (g["category"], g["brand"], g["year"], g["quarter"]))
    return out


def build_severity_vs_rating(rows: list[dict]) -> list[dict]:
    counts: dict[tuple, int] = {}
    for r in rows:
        severity_label = str(r["severity"]) if r["severity"] is not None else "no_complaint"
        key = (int(r["rating"]), severity_label)
        counts[key] = counts.get(key, 0) + 1
    out = [{"star_rating": rating, "severity": sev, "count": n} for (rating, sev), n in counts.items()]
    out.sort(key=lambda g: (g["star_rating"], g["severity"]))
    return out


def main() -> None:
    if not config.DUCKDB_PATH.exists():
        raise SystemExit(f"{config.DUCKDB_PATH} not found.")

    config.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    rows = fetch_extracted_reviews(con)
    con.close()

    if not rows:
        raise SystemExit(
            f"No extracted reviews found for model={config.EXTRACTION_MODEL}. "
            "Run `python -m src.extract --target in_sample` to completion first."
        )

    write_csv(
        config.EXPORTS_DIR / "reviews_extracted.csv",
        rows,
        ["review_id", "parent_asin", "brand", "category", "product_title", "rating", "review_timestamp",
         "verified_purchase", "helpful_vote", "complaint_category", "severity", "note", "year", "quarter"],
    )

    share_rows = build_complaint_share_by_brand_category(rows)
    write_csv(
        config.EXPORTS_DIR / "complaint_share_by_brand_category.csv",
        share_rows,
        ["category", "brand", "total_reviews", "complaint_reviews", "complaint_share"],
    )

    trend_rows = build_quarterly_trend_by_brand(rows)
    write_csv(
        config.EXPORTS_DIR / "quarterly_trend_by_brand.csv",
        trend_rows,
        ["category", "brand", "year", "quarter", "total_reviews", "complaint_reviews", "complaint_share"],
    )

    severity_rows = build_severity_vs_rating(rows)
    write_csv(
        config.EXPORTS_DIR / "severity_vs_rating.csv",
        severity_rows,
        ["star_rating", "severity", "count"],
    )

    print(f"Wrote {len(rows):,} rows to {config.EXPORTS_DIR / 'reviews_extracted.csv'}")
    print(f"Wrote {len(share_rows)} rows to {config.EXPORTS_DIR / 'complaint_share_by_brand_category.csv'}")
    print(f"Wrote {len(trend_rows)} rows to {config.EXPORTS_DIR / 'quarterly_trend_by_brand.csv'}")
    print(f"Wrote {len(severity_rows)} rows to {config.EXPORTS_DIR / 'severity_vs_rating.csv'}")


if __name__ == "__main__":
    main()
