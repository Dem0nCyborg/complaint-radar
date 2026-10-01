# ComplaintRadar

ComplaintRadar extracts product complaints from Amazon reviews of air
fryers, blenders, and robot vacuums using an LLM, checks the extraction
against a hand-labeled sample, stores the results in DuckDB, and presents
them through a Tableau Public dashboard and a weekly stakeholder brief. The
source data is the McAuley-Lab/Amazon-Reviews-2023 dataset on Hugging Face.

## Status

Phase 1 (data ingestion, cleaning, and labeling tooling) is complete. This
README describes what exists right now. Phase 2 (LLM extraction and an
accuracy check against hand labels) and Phase 3 (the dashboard and the
weekly brief) have not been started. There are no complaint-extraction
results, accuracy numbers, or charts yet.

See `CLAUDE.md` for the decision log behind every non-obvious choice below.

## Data funnel (this run)

- Metadata rows scanned: 3,735,584 (the item metadata file only, not the
  much larger reviews file)
- Candidate products after keyword filtering and accessory exclusion:
  14,082 (3,287 air fryers, 8,640 blenders, 2,155 robot vacuums)
- Products selected for review collection: 18, one (NutriBullet, see
  Limitations) later dropped, 17 remain
- Review rows scanned (full pass over the reviews file): 67,409,944
- Raw review rows matched to the 17 selected products: 90,703
- Rows remaining after cleaning (dedupe, empty-text removal): 89,838
- Reviews flagged non-English (kept, not dropped): 5,271 (5.9% of the
  cleaned set)
- Reviews sampled for LLM extraction (Phase 2), stratified evenly across
  the 17 products: 4,000

Full detail, including per-product counts, drop reasons, and quarterly
review volume, is in `reports/data_quality_report.md`.

## Selected products

| Brand | Product | Category | parent_asin | Cleaned reviews |
|---|---|---|---|---|
| Instant | Instant Essentials 4QT Air Fryer Oven | air_fryer | B0C2ZMCN8T | 6,629 |
| Ninja | Ninja AF101 Air Fryer that Crisps, Roasts, Reheats, & Dehydrates | air_fryer | B07FDJMC9Q | 5,867 |
| Gowise USA | GoWISE USA 1700W 5.8-QT 8-in-1 Digital Air Fryer | air_fryer | B09TR3DQ8R | 5,348 |
| Cosori | COSORI Air Fryer Pro LE 5-Qt | air_fryer | B0BZT7SWZB | 3,566 |
| Ninja | Ninja DZ201 Foodi 8 Quart 6-in-1 DualZone 2-Basket Air Fryer | air_fryer | B0BJCDKNT2 | 1,732 |
| Ninja | Ninja AF080 Mini Air Fryer, 2 Quarts | air_fryer | B0BLZKZ5P5 | 1,633 |
| Magic Bullet | magic bullet Blender, Small, Black, 11-Piece Set | blender | B0BQLF2WMN | 10,545 |
| Ninja | Ninja BL610 Professional 72oz Countertop Blender | blender | B00NGV4506 | 6,965 |
| Ninja | Ninja BL660 Professional Compact Smoothie & Food Processor | blender | B0CBT1F9QJ | 6,465 |
| Oster | Oster Blender, Pro 1200 with Glass Jar, 24oz | blender | B0BCL798D9 | 3,027 |
| Ninja | Ninja BN401 Nutri Pro Compact Personal Blender | blender | B0CDSLV93R | 689 |
| eufy | eufy by Anker, BoostIQ RoboVac 11S (Slim) | robot_vacuum | B0BYRM8PXH | 19,003 |
| Shark | Shark RV1001AE IQ Robot Self-Empty XL | robot_vacuum | B0BLL3SCXK | 5,287 |
| iRobot | iRobot Roomba 675 | robot_vacuum | B07DL4QY5V | 6,677 |
| Shark | Shark AV752 ION Robot Vacuum, Tri-Brush System | robot_vacuum | B0BTPR8Q2Q | 2,272 |
| iRobot | iRobot Roomba i3+ EVO (3550) | robot_vacuum | B09YZ931H2 | 2,118 |
| Shark | Shark AV1010AE IQ Robot Vacuum with XL Self-Empty Base | robot_vacuum | B0BMWSG9RR | 2,015 |

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
#    Safe to Ctrl-C and rerun, it resumes from a checkpoint.
python -m src.fetch_reviews

# 4. Dedupe (same-listing and cross-listing), drop empty text, flag
#    (not drop) non-English reviews. Re-checks the current `selected`
#    column, so deselecting a product after fetch excludes it here.
python -m src.clean

# 5. Create the DuckDB file and load selected products + cleaned reviews.
python -m src.load_duckdb

# 6. Flag a fixed-seed sample for the Phase 2 LLM extraction step,
#    stratified evenly across products so no single product dominates.
python -m src.select_sample

# 7. Draw the fixed, stratified 150-review hand-labeling sample.
python -m src.sample_for_labeling

# 8. Skim real reviews before finalizing config/categories.yaml (read-only).
python -m src.label_tool --browse --n 50

# 9. Hand-label (resumable, Ctrl-C and rerun picks up where you left off).
python -m src.label_tool

# 10. Regenerate the data-quality report any time after steps above.
python -m src.quality_report
```

Output: `data/processed/complaint_radar.duckdb` and
`reports/data_quality_report.md`.

## Known limitations

- The dataset's review data ends around September 2023. There is nothing
  more recent to analyze.
- NutriBullet (parent_asin B00AIHD94Y) was dropped from the pipeline. Its
  review pool is filed under a different, duplicate Amazon listing that
  wasn't selected, so the fetch only found 45 reviews for it against a
  rating count of 26,527. Recovering its reviews would mean identifying
  the sibling listing and running another full pass over the reviews file.
  Dropped in favor of progress with the 17 products whose review pools are
  correctly attached. See `CLAUDE.md` #20.
- The Ninja BN401 (B0CDSLV93R) has a short review history: only 689
  reviews total, and meaningful review volume doesn't start until around
  2022 even though the earliest matched review dates to 2018. Trend
  analysis for this product will have much less signal than the others.
- `rating_number` in the item metadata counts ratings across shared or
  duplicate listings. It is not a reliable count of how many reviews a
  fetch will find for one specific `parent_asin`, as the NutriBullet case
  shows.
- The complaint taxonomy is single-label: one complaint category per
  review, not multiple. See `CLAUDE.md` #2 for why.
- The complaint categories in `config/categories.yaml` are still a draft.
  They were written before reading real review text and will be revised
  after a manual read-through (see "Next step" below) before the hand
  labeling pass starts.

## How this was built

The code, pipeline design, and this documentation were built with Claude
Code. Every non-obvious decision, the alternative that was rejected, and
the reason, is recorded in `CLAUDE.md` as the work happened, not written
up after the fact.

## Schema

See `CLAUDE.md` and `src/load_duckdb.py` for the authoritative schema:
`products`, `reviews` (full cleaned set, `in_sample` flags the
LLM-extraction subset), `label_sample` (the persisted 150-review
hand-labeling set), and `labels` (hand-label output).

## Next step

Read a sample of real reviews and finalize `config/categories.yaml` before
hand-labeling starts:

```bash
python -m src.label_tool --browse --n 50
```

This is read-only. It shows 50 random reviews from the labeling pool and
saves nothing.
