# ComplaintRadar

ComplaintRadar extracts product complaints from Amazon reviews of air
fryers, blenders, and robot vacuums using an LLM, checks the extraction
against a reference set of labels, stores the results in DuckDB, and
presents them through a Tableau Public dashboard and a weekly stakeholder
brief. The source data is the McAuley-Lab/Amazon-Reviews-2023 dataset on
Hugging Face.

## Status

All three phases have been run on real data. All 4,000 sampled reviews
were extracted by `gemini-3.5-flash-lite`, the agreement check against the
reference labels is done, the Tableau exports are generated, and the weekly
brief has been run.

**Dashboard:** DASHBOARD_LINK_PENDING

See `CLAUDE.md` for the decision log behind every non-obvious choice below.

## Results

**Extraction agreement (model-to-model).** On the 150-review label sample,
`gemini-3.5-flash-lite` matched the reference labels on **89.3%** of
complaint categories (**Cohen's kappa 0.856**). Severity matched exactly on
75.3% of the 81 reviews where both sides assigned one (mean absolute
difference 0.27 on the 1-3 scale). The most common disagreement was
performance vs. design/usability (3 of 19 performance reviews), a genuine
judgment call in the taxonomy rather than a wrong reading. The extraction
prompt was **not** tuned against these 150 labels, so the figure is not
inflated by fitting to them. Reference labels are AI-generated (see
Method); this is model-to-model agreement, not agreement with human
annotators.

**Complaint share by brand** (share of reviews in the 2021-Q1 to 2023-Q2
sample that contain any complaint):

| Category | Brand | Reviews | Complaint share |
|---|---|---|---|
| Air fryer | Instant | 235 | 40.4% |
| Air fryer | GoWISE USA | 236 | 38.1% |
| Air fryer | Ninja | 706 | 25.1% |
| Air fryer | COSORI | 235 | 23.8% |
| Blender | Magic Bullet | 235 | 55.7% |
| Blender | Oster | 235 | 42.1% |
| Blender | Ninja | 706 | 36.7% |
| Robot vacuum | iRobot | 472 | 65.9% |
| Robot vacuum | Shark | 705 | 57.2% |
| Robot vacuum | eufy | 235 | 38.3% |

Brands are compared within a category only. The products differ in price
and type (for example, personal vs. countertop blenders), so these are
descriptive figures, not a ranking of product quality.

**The top complaint differs by category.** Air fryers: design/usability
(134), durability/defect (125), safety (71). Blenders: durability/defect
(195), performance (77). Robot vacuums: performance (267),
durability/defect (209), connectivity/app (156). In a spot check of 12 of the 71
air-fryer safety complaints, most described a burning-plastic or chemical
smell.

**Star ratings hide severity.** 83% of 1-star reviews (548 of 658) describe
a severe problem, as expected. But 30 of 2,365 five-star reviews also
describe a severe one, typically a unit that failed and was replaced, after
which the reviewer still gave five stars. Star rating alone undercounts
serious product failures.

**No statistically reliable quarter-over-quarter changes.** The weekly
brief tests every brand/quarter change with a two-proportion z-test and a
Benjamini-Hochberg correction across all tests. Nothing survives in this
sample: the largest raw swings (up to 31 points) rest on 14 to 31 reviews
per quarter, and one of them (COSORI) reverses itself the next quarter. The
brief reports that rather than presenting noise as a trend (see
`reports/example_brief.md` and `CLAUDE.md` #33).

**Cost.** About 210 API calls in total (8 for the pilot, 200 for the
full run, plus retries). Estimated at about $0.59 at paid-tier list
prices.

## Method

**Taxonomy.** `config/categories.yaml` holds 9 complaint categories plus
`no_complaint` and `other`, each with a one-line definition, a
primary-complaint rule (assign the complaint that ended or most limited use
of the product, not just the first one mentioned), and an explicit
tie-breaker order for when more than one category seems to fit. It was
written by reading 150 real English reviews (half 1-3 star, half 4-5 star),
not drafted from assumptions. See `CLAUDE.md` #23.

**Reference labels are AI-generated, not from a human annotator.** They
come from Claude in a separate chat session, given a plain CSV export of
150 reviews (`data/interim/label_sample_export.csv`) with no database
access, and brought back as `data/interim/reference_labels.csv` for import
(`src/import_reference_labels.py`, `labeler = "claude-chat-reference"`).
**The agreement metric this project reports is model-to-model agreement: a
cheap extraction model compared against a stronger model's judgment. It is
not agreement with a human-verified gold standard, and is never described
that way.** See `CLAUDE.md` #22.

**Extraction** (`src/extract.py`) runs the 4,000-review sample through
Gemini 3.5 Flash-Lite (Google AI Studio), batching ~20 reviews per request with
structured JSON output, independently re-validated with Pydantic against
the same taxonomy rules as the reference labels. A malformed batch gets one
corrective retry; a review that still fails is left uncached and picked up
automatically on the next run, rather than crashing the whole job. Results
are cached in DuckDB (`extractions` table, keyed on review + model), so a
rerun only processes what's not already done.

**Agreement** (`src/agreement.py`) computes, over the 150-review overlap
between reference labels and extraction output: category accuracy, Cohen's
kappa, per-category precision/recall, a confusion matrix, and severity
agreement (exact-match rate and mean absolute difference). Implemented in
plain Python, no stats library, verified against a hand-computed synthetic
dataset.

**Pilot first.** Extraction ran on the 150-review label sample first, and
the cost estimate and agreement numbers were reviewed before the full
4,000-review run.

## Data funnel (Phase 1, run on real data)

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
- Reviews sampled for LLM extraction, stratified evenly across the 17
  products, dated 2021-01-01 through 2023-06-30: 4,000

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
cp .env.example .env
# Add GEMINI_API_KEY (required for src/extract.py and src/brief.py).
# HF_TOKEN is optional, only for higher Hugging Face rate limits.
```

Requires Python 3.11+ (developed on 3.13).

## Pipeline (run in order)

```bash
# --- Phase 1: data ---

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

# 6. Flag a fixed-seed sample for LLM extraction, stratified evenly across
#    products and restricted to a common date window, so no single product
#    or era dominates.
python -m src.select_sample

# 7. Draw the fixed, stratified 150-review label_sample.
python -m src.sample_for_labeling

# 8. Regenerate the data-quality report any time after the steps above.
python -m src.quality_report

# --- Phase 2: labels, extraction, agreement ---

# 9. Export the 150 label_sample reviews for an external reference-labeling
#    chat session (no DB access needed on the other end).
#    Then: take data/interim/label_sample_export.csv to a Claude chat,
#    get back data/interim/reference_labels.csv, and import it.
python -m src.import_reference_labels

# 10. Pilot: estimate cost, then run extraction on the 150 label_sample
#     reviews first and check agreement before scaling up.
python -m src.extract --target label_sample --estimate-only
python -m src.extract --target label_sample
python -m src.agreement

# 11. After reviewing the pilot's cost and agreement numbers: full run.
python -m src.extract --target in_sample --estimate-only
python -m src.extract --target in_sample

# --- Phase 3: exports and brief ---

# 12. Export CSVs for the Tableau dashboard (see exports/DASHBOARD_SPEC.md).
python -m src.export_tableau

# 13. Generate a weekly brief; every number in it is validated against the
#     SQL query that produced it before anything is saved.
python -m src.brief
```

Output: `data/processed/complaint_radar.duckdb`,
`reports/data_quality_report.md`, `reports/agreement_stats.json` (via
`agreement.py`), `exports/*.csv`, and `reports/example_brief.md`.

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
- Reviews filed under one listing can include reviews carried over from an
  earlier model generation that listing replaced. This is a known
  characteristic of Amazon's review system, not specific to one product
  here. It's part of why the sample for labeling and LLM extraction is
  restricted to a common analysis window: 2021-Q1 through 2023-Q2.
- The sample used for reference labeling and LLM extraction only includes
  reviews dated 2021-01-01 through 2023-06-30. The full 89,838-row cleaned
  dataset in DuckDB is not date-restricted and covers each product's
  complete history; the window restriction applies only to what gets
  sampled, not to what's stored. See `CLAUDE.md` #21.
- The complaint taxonomy is single-label: one complaint category per
  review, not multiple. See `CLAUDE.md` #2 for why.
- **Reference labels are AI-generated (Claude, in a chat session), not
  from a human annotator.** The agreement numbers this project reports are
  model-to-model agreement, not agreement with a human-verified gold
  standard. See `CLAUDE.md` #22. This is stated here, in the Method
  section above, and in the decision log; it should not be described any
  other way in any chart, brief, or future write-up.
- The Gemini model name and pricing in `src/config.py` were checked
  against Google's documentation on 2026-10-02 (`CLAUDE.md` #32). Recheck
  them if you run this much later.
- Quarterly trend points rest on roughly 20 to 70 reviews per brand per
  quarter, too few to detect changes reliably. That is why the brief
  applies a multiple-comparisons correction, and why the dashboard's trend
  chart should be read as descriptive.

## How this was built

The code, pipeline design, and this documentation were built with Claude
Code. Every non-obvious decision, the alternative that was rejected, and
the reason, is recorded in `CLAUDE.md` as the work happened, not written
up after the fact.

## Schema

See `CLAUDE.md` and `src/load_duckdb.py` for the authoritative schema:
`products`, `reviews` (full cleaned set, `in_sample` flags the
LLM-extraction subset), `label_sample` (the persisted 150-review reference/
pilot sample), `labels` (reference-label output; `labeler` records the
source, e.g. `claude-chat-reference`), and `extractions` (one row per
review per extraction model, doubling as the cache/resume mechanism for
`src/extract.py`).

## Possible extensions

- Run the same 150-review check against `gemini-3.8-flash` to measure what
  the more expensive model adds over Flash-Lite.
- Have a person label a subset of the 150 reviews, to check the reference
  labels themselves.
- Allow more than one complaint per review (multi-label).
