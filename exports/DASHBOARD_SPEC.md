# Dashboard spec

Four charts for the Tableau Public dashboard, built manually from the CSVs
in this folder (`src/export_tableau.py` generates them; nothing here is a
Tableau file). All four are scoped to the 4,000-review `in_sample` set,
extracted by the model named in `src/config.py`'s `EXTRACTION_MODEL`, dated
2021-01-01 through 2023-06-30 (see CLAUDE.md #21 for why that window).

Every number in these CSVs comes from `complaint_category`/`severity`
extracted by an LLM (model-to-model agreement against it is in
`reports/agreement_stats.json` and the README), not a human-verified gold
standard. Any chart built from them should be read with that in mind.

## Chart 1: Complaint share by brand, within category

- **CSV:** `complaint_share_by_brand_category.csv`
- **Fields:** `category` (filter/facet), `brand`, `complaint_share`
  (bar length), `total_reviews` (tooltip, so a bar backed by a handful of
  reviews isn't read the same as one backed by thousands)
- **Chart type:** horizontal bar chart, one small-multiple panel per
  `category` (air_fryer / blender / robot_vacuum), bars sorted by
  `complaint_share` descending within each panel
- **Question it answers:** within each product category, which brands'
  reviews mention a complaint most often?

## Chart 2: Quarterly complaint trend by brand

- **CSV:** `quarterly_trend_by_brand.csv`
- **Fields:** `year` + `quarter` (x-axis, combine into one quarter label),
  `complaint_share` (y-axis, line), `brand` (color/line), `category`
  (filter), `total_reviews` (tooltip)
- **Chart type:** multi-line time series, one line per brand, filterable by
  category
- **Question it answers:** is a brand's complaint share rising, falling, or
  flat over the analysis window? A brand with few reviews in a given
  quarter will show a noisier line; `total_reviews` in the tooltip is there
  to catch that before reading too much into a spike.

## Chart 3: Severity vs. star rating

- **CSV:** `severity_vs_rating.csv`
- **Fields:** `star_rating` (x-axis), `severity` (y-axis, categorical:
  1, 2, 3, or `no_complaint`), `count` (cell value/color)
- **Chart type:** heatmap (star rating x severity grid, color by count, or
  normalize to % within each star rating for an easier read)
- **Question it answers:** do low star ratings correspond to severe
  complaints, or do some 1-star reviews reflect minor annoyances (and some
  high-star reviews still mention a real, if non-fatal, issue)? This is
  the check on whether star rating alone would have been a usable proxy
  for complaint severity (it isn't, if this chart shows spread rather than
  a clean diagonal).

## Chart 4: Complaint category breakdown

- **CSV:** `reviews_extracted.csv` (aggregate `complaint_category` counts
  directly in Tableau; no separate pre-aggregated file for this one)
- **Fields:** `complaint_category` (bars/segments), count of `review_id`,
  `category` (filter)
- **Chart type:** bar chart or treemap, one bar/segment per category key
  (`no_complaint` excluded or shown separately so it doesn't dominate the
  scale)
- **Question it answers:** across all products, what are the most common
  types of complaints, and does that mix differ by product category (an
  air fryer's top complaint is unlikely to be the same as a robot vacuum's)?
