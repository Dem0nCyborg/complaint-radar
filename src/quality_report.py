"""Phase 1, step 8: write reports/data_quality_report.md summarizing every
drop/flag/sample step in the pipeline.

Usage:
    python -m src.quality_report
"""

from __future__ import annotations

import json

import duckdb

from src import config


def read_json(path, default=None):
    if path.exists():
        return json.loads(path.read_text())
    return default


def main() -> None:
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    metadata_funnel = read_json(config.METADATA_FUNNEL_PATH, {})
    fetch_stats = read_json(config.FETCH_STATS_PATH, {})
    clean_stats = read_json(config.CLEAN_STATS_PATH, {})

    lines = ["# ComplaintRadar — Phase 1 Data Quality Report", ""]

    lines.append("## 1. Metadata filtering funnel")
    if metadata_funnel:
        lines.append(f"- Metadata rows scanned: {metadata_funnel.get('total_metadata_scanned', 0):,}")
        lines.append("- Keyword matches before accessory exclusion, by category:")
        for cat, n in metadata_funnel.get("matched_pre_exclusion_by_category", {}).items():
            lines.append(f"  - {cat}: {n:,}")
        lines.append("- Excluded as accessories/parts, by category (strong/weak tier):")
        for cat, tiers in metadata_funnel.get("excluded_by_category_and_tier", {}).items():
            lines.append(f"  - {cat}: strong={tiers.get('strong', 0):,}, weak={tiers.get('weak', 0):,}")
        lines.append(f"- Rows that failed to parse as JSON (skipped, not crashed): {metadata_funnel.get('invalid_json_lines', 0):,}")
        lines.append(f"- Candidate rows with an unparseable price (kept, price=NULL): {metadata_funnel.get('unparseable_price_count', 0):,}")
        lines.append("- Final candidates shown to user, by category:")
        for cat, n in metadata_funnel.get("final_candidates_by_category", {}).items():
            lines.append(f"  - {cat}: {n:,}")
    else:
        lines.append("_Not yet run: `python -m src.ingest_metadata`_")
    lines.append("")

    lines.append("## 2. Review fetch (full pass, selected products only)")
    if fetch_stats:
        lines.append(f"- Total review rows scanned: {fetch_stats.get('total_examples_scanned', 0):,}")
        lines.append(f"- Full pass complete: {fetch_stats.get('full_pass_complete')}")
        lines.append(f"- Total matches kept (raw, pre-cleaning): {fetch_stats.get('total_matches', 0):,}")
        lines.append("- Matches by product:")
        for asin, n in fetch_stats.get("matches_by_product", {}).items():
            lines.append(f"  - {asin}: {n:,}")
    else:
        lines.append("_Not yet run: `python -m src.fetch_reviews`_")
    lines.append("")

    lines.append("## 3. Cleaning")
    if clean_stats:
        lines.append(f"- Raw reviews in: {clean_stats.get('raw_count', 0):,}")
        lines.append(f"- Dropped (empty text): {clean_stats.get('empty_text_dropped', 0):,}")
        lines.append(f"- Dropped (duplicate): {clean_stats.get('duplicate_dropped', 0):,}")
        lines.append(f"- Flagged non-English (kept, not dropped): {clean_stats.get('non_english_flagged', 0):,}")
        lines.append(f"- Final cleaned count: {clean_stats.get('final_count', 0):,}")
    else:
        lines.append("_Not yet run: `python -m src.clean`_")
    lines.append("")

    lines.append("## 4. Loaded data / sampling")
    if config.DUCKDB_PATH.exists():
        con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
        n_products = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        n_reviews = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        n_in_sample = con.execute("SELECT COUNT(*) FROM reviews WHERE in_sample").fetchone()[0]
        n_label_sample = con.execute("SELECT COUNT(*) FROM label_sample").fetchone()[0]
        strata = con.execute("SELECT stratum, COUNT(*) FROM label_sample GROUP BY stratum").fetchall()
        n_labeled = con.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
        con.close()

        lines.append(f"- Products loaded: {n_products}")
        lines.append(f"- Reviews loaded (full set, feeds trend analysis): {n_reviews:,}")
        lines.append(f"- Reviews flagged in_sample (feeds LLM extraction, seed={config.SAMPLE_SEED}): {n_in_sample:,}")
        lines.append(f"- Hand-labeling sample size (seed={config.LABEL_SAMPLE_SEED}): {n_label_sample}")
        for stratum, n in strata:
            lines.append(f"  - {stratum}: {n}")
        lines.append(f"- Reviews hand-labeled so far: {n_labeled}")
        lines.append("")
        lines.append(
            "**Reading caveat:** the hand-labeling sample is stratified ~50% 1-3 star / "
            "~50% 4-5 star, not the natural rating distribution. Any accuracy/precision "
            "number computed against it reflects a complaint-enriched mix and needs "
            "reweighting before it generalizes to the full review population."
        )
    else:
        lines.append("_Not yet run: `python -m src.load_duckdb`_")

    config.DATA_QUALITY_REPORT_PATH.write_text("\n".join(lines) + "\n")
    print(f"Wrote {config.DATA_QUALITY_REPORT_PATH}")


if __name__ == "__main__":
    main()
