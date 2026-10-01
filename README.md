# ComplaintRadar

LLM-powered complaint tracker for kitchen/home appliances (air fryers,
blenders, robot vacuums), built on the McAuley-Lab/Amazon-Reviews-2023
dataset. **This repo currently covers Phase 1 only: data ingestion and
cleaning.** See `CLAUDE.md` for the decision log behind every non-obvious
choice below.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional: add HF_TOKEN for higher HF rate limits
```

Requires Python 3.11+ (developed on 3.13).

## Pipeline (run in order)

```bash
# 1. Stream item metadata only, filter to air fryers/blenders/robot vacuums,
#    write candidates to data/interim/candidate_products.csv
python -m src.ingest_metadata

# 2. --> Open data/interim/candidate_products.csv, mark `selected` = y
#        for the products you want. <--

# 3. Full streaming pass over reviews, keep everything for selected products.
#    Safe to Ctrl-C and rerun -- it resumes from a checkpoint.
python -m src.fetch_reviews

# 4. Dedupe, drop empty text, flag (not drop) non-English reviews.
python -m src.clean

# 5. Create the DuckDB file and load selected products + cleaned reviews.
python -m src.load_duckdb

# 6. Flag a fixed-seed random 4,000-review sample (in_sample) for the
#    Phase 2 LLM extraction step.
python -m src.select_sample

# 7. Draw the fixed, stratified 150-review hand-labeling sample.
python -m src.sample_for_labeling

# 8. Skim real reviews before finalizing config/categories.yaml (read-only).
python -m src.label_tool --browse --n 50

# 9. Hand-label (resumable; Ctrl-C and rerun picks up where you left off).
python -m src.label_tool

# 10. Regenerate the data-quality report any time after steps above.
python -m src.quality_report
```

Output: `data/processed/complaint_radar.duckdb` and
`reports/data_quality_report.md`.

## Important caveats

- **Step 3 streams through the entire ~31GB reviews file once**, not just
  the matching rows -- there's no server-side filter, so finding every
  review for your selected products means scanning the whole file. This is
  intentional (see CLAUDE.md #7): stopping early would bias the sample
  toward file order.
  - **Resume is a true byte-offset resume**, not a re-download-and-discard:
    it uses HTTP Range requests against the HF CDN (confirmed to support
    them) and checkpoints the exact byte offset of the next unprocessed
    line every ~2,000 rows (`data/raw/fetch_checkpoint.json`). An
    interruption costs you re-scanning only what's left, not what's
    already done -- see CLAUDE.md #16 for why this needed a custom reader
    instead of the `datasets` library's own streaming/resume.
  - `ingest_metadata.py` does **not** have this same resume (it's a single
    streaming pass over the much smaller ~12GB metadata file); an
    interrupted metadata run has to restart from scratch.
- Both `ingest_metadata.py` and `fetch_reviews.py` accept a `--limit N`
  flag. **This is a development/smoke-test knob only** (caps rows scanned
  for a fast dry run) -- omit it for real runs.
- `config/categories.yaml`'s category list is a **placeholder** drafted
  before reading real review text. Run `label_tool.py --browse` and adjust
  it before spending the real 150-review labeling pass.
- The hand-labeling sample is deliberately **not** representative of the
  natural rating distribution (~50% 1-3★ / ~50% 4-5★, to get enough actual
  complaints to label) -- see `reports/data_quality_report.md` for the
  reading caveat this implies for any accuracy number computed later.

## Schema

See `CLAUDE.md` and `src/load_duckdb.py` for the authoritative schema:
`products`, `reviews` (full fetched set, `in_sample` flags the LLM-extraction
subset), `label_sample` (the persisted 150-review hand-labeling set), and
`labels` (hand-label output).
