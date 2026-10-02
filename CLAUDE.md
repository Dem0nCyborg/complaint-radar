# ComplaintRadar — Decision Log

Format: **Decision** — Rejected alternative(s) — Why.

## Phase 1: Data ingestion & cleaning

1. **Stream metadata first, use `rating_number` as a review-count proxy,
   require manual product selection (via a `selected` column in a CSV)
   before any review fetch.** — *Rejected:* downloading full category
   reviews and filtering client-side. — *Why:* avoids pulling the ~31GB
   reviews file just to find candidate products; gives an explicit
   human checkpoint before any large fetch.

2. **Complaint taxonomy is a fixed, single-label list loaded from
   `config/categories.yaml`, with an explicit `no_complaint` value and a
   free-text fallback only under `other`.** — *Rejected:* free text;
   multi-label. — *Why:* consistent, fast labeling at a 150-review scale;
   multi-label adds complexity with no clear benefit yet and can be
   revisited later if single-label proves too coarse.

3. **Severity rubric text lives once in `categories.yaml`, reused verbatim
   by the labeling CLI and (later) the LLM extraction prompt.** —
   *Rejected:* separate wording in each place. — *Why:* keeps human/LLM
   label agreement measurement meaningful — both are judged against the
   identical definition.

4. **Non-English detection (`langdetect`) only flags rows; it never drops
   them.** — *Rejected:* dropping non-English rows; a heavier fastText
   langid model. — *Why:* preserves sample size for trend analysis;
   langdetect is a light pure-Python dependency, accurate enough for a
   flag rather than a hard filter.

5. **`review_id` is a synthetic sha256 hash** (raw dataset has no native
   review ID). — *Rejected:* an ad hoc composite natural key used in
   queries. — *Why:* a single surrogate key keeps joins with `labels` and
   `label_sample` simple.

6. **venv + `requirements.txt` with pinned (`==`) versions.** — *Rejected:*
   poetry, uv. — *Why:* zero extra tooling required for anyone reviewing
   the repo to run it; `pip install -r requirements.txt` is enough.

7. **`fetch_reviews.py` does one full streaming pass over the reviews file
   for selected products — no early stop, no per-product cap — with
   checkpoint/resume (see #16 for how resume actually works).** —
   *Rejected:* the original plan's early-stop-at-target-count /
   per-product-cap approach. — *Why:* stopping early biases the kept
   sample toward whichever part of the file happens to be scanned first,
   which would skew time-trend analysis. **Trade-off, flagged explicitly:**
   this does mean reading through the full ~31GB `Home_and_Kitchen` reviews
   file over the network once (non-matching rows aren't persisted, but
   they are still read off the wire) — slower than the original "never
   touch the big file" goal, accepted here in exchange for an unbiased
   sample.

8. **Added `reviews.in_sample`: a fixed-seed (42) random sample of 4,000
   reviews drawn from the full matched set feeds LLM extraction (Phase 2);
   the full matched set feeds trend/volume analysis.** — *Rejected:* using
   the full set for LLM extraction (cost); using only a capped fetch as the
   sole dataset (biased, see #7). — *Why:* separates "what we analyze for
   trends" from "what we pay to run through an LLM," while keeping the
   LLM-extraction population an unbiased random draw.

9. **Accessory exclusion reworked twice after live testing against real
   metadata.** First pass: naive substring keywords (liner, parchment,
   paper, rack, accessory, replacement, blade, parts, cover, cookbook,
   bottle, filter, brush, bags) — *rejected* after testing showed it both
   over- and under-excluded: `"cover"` matched inside `"discover"`-type
   words, while `"blade"`/`"filter"`/`"bags"`/`"rack"`/`"cover"` also fired
   on legitimate feature mentions in real appliance titles ("stainless
   steel blades", "HEPA filter", "self-empty base with bags", "air fryer
   oven with rack"). Current design:
   - All exclusion matching is **word-boundary regex** (`\bterm\b`), not
     substring, fixing the `"cover"`/`"discover"` class of bug.
   - Terms are split into **strong** (exclude unconditionally — liner,
     parchment, cookbook, replacement, accessory/accessories, paper, parts)
     and **weak** (blade, filter, bag(s), rack, cover — plus bottle and
     brush, added here since they fail the same way: a blender sold "with
     travel bottles," a vacuum with a "side brush," both real appliances).
     A weak term only excludes when the category keyword itself does not
     appear in the title's leading 6 words — i.e. the title reads like an
     accessory/part listing rather than the appliance itself.
   - Every excluded row (title, category, matched term, tier) is written to
     `data/interim/excluded_products.csv` for audit — *rejected:* excluding
     silently. — *Why:* lets the keyword list itself be corrected against
     real misses/overreaches instead of trusting it blind.
   - Adding bottle/brush to the weak list (beyond the originally specified
     terms) was an inference made while testing, not an explicit
     instruction — confirmed.

10. **`brand_normalized` added alongside raw `brand_raw`**, via suffix
    stripping + a small hand-seeded alias table, with category-aware
    resolution for composite storefronts (e.g. "SharkNinja" → "Shark" for
    robot vacuums, "Ninja" for air fryers/blenders). — *Rejected:* using
    the raw `store` field as-is. — *Why:* the raw field has noisy variants
    ("Ninja Store", "Ninja Kitchen", "Ninja Official Store", ...) that
    would fragment brand-level analysis. **Limitation:** the alias table is
    hand-seeded and not exhaustive — unrecognized brands fall back to a
    cleaned, title-cased version of the raw string.

11. **The 150-review hand-labeling sample is: fixed-seed (7), stratified
    ~50% 1-3★ / ~50% 4-5★, English-only, drawn from the `in_sample` pool
    (not the full review set), with the exact sampled IDs persisted in a
    `label_sample` table.** — *Rejected:* labeling in natural
    rating-distribution order; sampling fresh each run; drawing from the
    full (non-LLM-sample) pool. — *Why:* the natural distribution is
    mostly positive, so an unstratified sample yields very few actual
    complaints to label; persisting IDs makes the set reproducible across
    reruns; drawing specifically from `in_sample` means these exact reviews
    will also go through LLM extraction in Phase 2, enabling a direct
    human-vs-LLM agreement comparison. **This last point (sampling from
    `in_sample` rather than the full set) was an inference, not an
    explicit instruction — confirmed.** **Reading caveat:** any
    accuracy/precision number computed against this labeled set reflects
    performance on a complaint-enriched mix, not the natural review
    population, and needs reweighting to generalize.

12. **`review_id` and the dedupe key use the identical hash input** —
    `user_id | parent_asin | timestamp | full review text` (not a truncated
    prefix). — *Rejected:* truncating text to e.g. the first 50 characters
    before hashing. — *Why:* truncation risked two distinct long reviews
    from the same user/product/timestamp colliding on the same primary
    key; using the full text for both purposes keeps them provably
    consistent with each other.

13. **Added `label_tool.py --browse` (read-only, writes nothing)** to read
    a batch of real reviews from the labeling pool before finalizing
    `config/categories.yaml`. — *Rejected:* finalizing the category list
    from assumptions alone before seeing real data. — *Why:* the shipped
    category list is an explicit placeholder; `--browse` lets it be
    corrected against real review text before the 150-review labeling pass
    is spent on a taxonomy that might not fit.

14. **Dataset access: raw per-category JSONL files streamed directly over
    HTTP** (`raw/meta_categories/meta_Home_and_Kitchen.jsonl`,
    `raw/review_categories/Home_and_Kitchen.jsonl`) via
    `load_dataset("json", data_files=..., streaming=True)`, rather than
    `load_dataset("McAuley-Lab/Amazon-Reviews-2023", "raw_meta_Home_and_Kitchen", ...)`.
    — *Rejected:* the dataset's own named-config loader. — *Why:*
    `datasets>=5` no longer executes the remote loading scripts this
    dataset repo ships, so the named-config path fails outright; the raw
    JSONL files it points to are public and stream identically through the
    generic JSON loader.

15. **Category inclusion matching converted to word-boundary regex** (same
    `CATEGORY_PATTERNS` used by both the main category check and the weak-
    exclusion leading-words check). — *Rejected:* naive substring matching
    (`"blender" in title_lower`). — *Why:* found live in test data —
    `"BlenderBottle Classic V2 Shaker Bottle"` (a shaker-bottle brand, not
    an electric blender) both wrongly matched the `blender` category via
    substring, and that same substring match then fooled the weak-exclusion
    leading-words check into thinking the title was genuinely about a
    blender (since `"bottle"` also appeared, but the check saw `"blender"`
    in the leading words and let it through). Fixing category matching to
    word-boundary closes both holes at once.

16. **`fetch_reviews.py` resumes via HTTP byte-offset Range requests, not
    `datasets`' `IterableDataset.skip()`.** — *Rejected:* `.skip(n)` —
    inspecting the `datasets` source (`SkipExamplesIterable`) confirmed it
    re-iterates the underlying stream from byte 0 every time and just
    discards the first `n` parsed records, i.e. a resume re-downloads
    everything already scanned. Confirmed the HF CDN serves this file with
    `Accept-Ranges: bytes` and returns real `206 Partial Content` for a
    mid-file `Range` request. — *Why:* for a ~31GB file, re-downloading
    from byte 0 on every resume makes interruptions prohibitively
    expensive; a true byte-offset resume makes an interruption cost
    whatever's left to scan, not what's already been scanned.
    **Correctness constraint:** a `Range` request can land mid-line (a raw
    byte offset doesn't respect JSONL line boundaries), so the checkpoint
    only ever stores the byte offset of the *start of the next line*,
    recorded after a line has been fully parsed and processed — never a
    raw mid-scan byte count. Resuming re-issues a `Range` request at
    exactly that offset, which is always a line boundary.

17. **`ingest_metadata.py` dropped `datasets` streaming entirely, moving to
    the same raw-JSONL-over-HTTP reader as `fetch_reviews.py`
    (`src/http_jsonl.py`), with its own per-line `json.loads` and
    `.get()`-based field access, plus the same byte-offset
    checkpoint/resume as #16.** — *Rejected:* keeping `datasets` streaming
    for metadata (it was only reworked for reviews). — *Why:* it crashed in
    production, not just in theory. `datasets` infers an Arrow schema from
    the first batch of streamed rows; around row 319K, later metadata rows
    introduce extra columns (`subtitle`, `author`) and represent `price` as
    a string instead of a float, and the library raised a `CastError`
    trying to force those rows into the earlier schema. The 50K-row test
    run earlier never reached row 319K, so this didn't surface until a
    longer run. Parsing each line as an independent Python dict has no
    shared schema to violate, so this class of error is now structurally
    impossible, not just handled — confirmed by running past row 319K
    (400K+ rows) without incident. Two more defensive changes that fall out
    of the same fix:
    - `parse_price()` strips non-numeric characters and returns `None`
      (rather than raising) on anything that doesn't reduce to a single
      number; `unparseable_price_count` in the funnel tracks how often
      this happens (e.g. a genuine range like `"$19.99 - $29.99"`).
    - A line that fails `json.loads` is counted (`invalid_json_lines` in
      the funnel) and skipped, not allowed to crash the run.
    **Side effect:** `datasets` and its transitive dependency tree
    (pyarrow, pandas, huggingface_hub, aiohttp, ...) are no longer used
    anywhere in the codebase and were removed; `requirements.txt` dropped
    from 38 pinned packages to 11.

18. **`clean.py` runs a second, separate dedupe pass on
    `(user_id, timestamp, text)` WITHOUT `parent_asin`, counted separately
    as `cross_listing_duplicate_dropped`.** — *Rejected:* relying on the
    existing `review_id`-based dedupe alone. — *Why:* Amazon shares one
    review pool across duplicate/cross-listed parent_asins for what's
    effectively the same product listing (e.g. the NutriBullet NBR-1201 is
    listed under several different `parent_asin`s that all draw from the
    same reviews). Since `review_id` is hashed from
    `user_id|parent_asin|timestamp|text`, the same physical review fetched
    under two different selected `parent_asin`s produces two different
    `review_id`s and survives the first dedupe pass as if they were two
    independent reviews — which would double-count that customer's
    experience in any per-product or per-brand analysis. The first
    occurrence in scan order is kept; later cross-listing copies are
    dropped.

19. **`select_sample.py` changed from pure random sampling across the
    pooled review set to sampling stratified by product**: the 4,000-review
    target is split as evenly as possible across the selected products
    (`target // n_products` per product, remainder distributed to the first
    few products in sorted `parent_asin` order), drawn only from each
    product's English-flagged reviews, same fixed seed. — *Rejected:* pure
    random sampling across the pooled set (the original design). — *Why:*
    pure random sampling would let whichever product has the most raw
    reviews dominate the LLM-extraction sample -- a product with 10x the
    review volume isn't necessarily 10x as complaint-prone, but it would
    look that way if it also supplied 10x the sampled rows, making any
    later per-product/per-brand complaint-rate comparison unreliable.
    **Behavior when a product's English review pool is smaller than its
    allocation:** every one of its English reviews is taken and the gap is
    reported as a shortfall in `data/interim/select_sample_stats.json` and
    the quality report -- the shortfall is NOT redistributed to other
    products, so the total sample can land below 4,000 when supply is
    short rather than silently over-representing a different product to
    compensate.

20. **Dropped B00AIHD94Y (NutriBullet NBR-1201) from the pipeline** (set
    `selected = n` after the fetch had already run) — *rejected:* running
    another full ~31GB fetch pass scoped to recover its reviews under
    whichever duplicate listing actually holds them. — *Why:* its review
    pool is filed under a duplicate/cross-listed `parent_asin` we didn't
    select — the fetch found only 45 raw reviews for it against a
    `rating_number` of 26,527, a two-orders-of-magnitude gap that's the
    signature of this problem (see CLAUDE.md #18 on cross-listing). Chased
    down, it would mean identifying the sibling listing(s) actually holding
    those reviews and re-running the full reviews scan to catch them too —
    dropped in favor of progress with the 17 products whose review pools
    are already correctly attached.
    **Note for future product selection:** `rating_number` is a *ratings*
    count that Amazon aggregates across shared/duplicate listings, not a
    reliable proxy for how many reviews a fetch will actually find under
    one specific `parent_asin` — a large gap between `rating_number` and
    actual fetched matches is a sign the product's reviews live elsewhere.
    **Mechanism:** `config.load_selected_parent_asins()` (the `selected`
    column) is now the single source of truth re-read by `fetch_reviews.py`,
    `clean.py`, and `load_duckdb.py` alike, so deselecting a product after
    fetch excludes it at the `clean` step (new `deselected_product_dropped`
    stat) without needing to re-fetch; `load_duckdb.py` re-filters cleaned
    reviews against the current selection as a defensive second check
    (otherwise a stale review's `parent_asin` would violate the
    `reviews.parent_asin` foreign key against `products`).
    `select_sample.py` needed no change — it already derives its per-product
    allocation from whatever `parent_asin`s are actually present in the
    loaded `reviews` table.

21. **`select_sample.py` (and, through `in_sample`, `sample_for_labeling.py`)
    is restricted to a common date window: 2021-01-01 through 2023-06-30
    inclusive** (`config.SAMPLE_WINDOW_START_DATE` /
    `_END_DATE` / `_END_EXCLUSIVE`). — *Rejected:* sampling from each
    product's full history regardless of date (the original design). —
    *Why:* the 17 products' histories start anywhere from 2012 to late
    2020 — sampling from full history would mix a product with 10+ years
    of reviews against one with 1-2, which isn't a fair basis for
    per-product/per-brand comparison. Reviews filed under one listing can
    also include reviews carried over from an earlier model generation
    that listing replaced, which is a different (and worse) signal than a
    review of the actual listed product — a shared recent window reduces
    how much of that older, possibly-mismatched history gets sampled. The
    window ends before 2023-Q3 because that quarter is partial: the
    dataset's review data ends mid-September 2023, so a full quarter isn't
    available to compare against. **This only changes what gets
    sampled/labeled** — the full `reviews` table still holds all 89,838
    cleaned rows regardless of date, for trend analysis. Re-ran with this
    restriction: all 17 products still filled their full per-product
    allocation from inside the window (smallest pool: B0CDSLV93R/Ninja
    BN401 at 598 in-window English reviews against an allocation of 235) —
    no shortfall, so no change to the total 4,000/150 sample sizes.

## Phase 2: reference labels, extraction, agreement

22. **Hand labeling dropped entirely. Reference labels for the 150-review
    `label_sample` are AI-generated**, by Claude in a separate chat session
    (given `data/interim/label_sample_export.csv` as input, a CSV export
    with no DB access), brought back as `data/interim/reference_labels.csv`
    and imported via `src/import_reference_labels.py` with
    `labeler = "claude-chat-reference"`. — *Rejected:* calling a model API
    (Anthropic or otherwise) directly from this repo to generate reference
    labels; the original plan of a human hand-labeling the 150-review
    sample. — *Why:* no API access was set up for this purpose at the time,
    and hand-labeling was dropped as a project decision (not a technical
    limitation). **This means the resulting evaluation is explicitly
    model-to-model agreement (a cheap extraction model vs. a stronger
    model's judgment), not agreement with a human-verified gold standard.**
    This must be stated that way everywhere it's described — README, this
    log, any chart or brief — and these labels are never called "human" or
    "hand" labels.

23. **`config/categories.yaml` finalized** by reading 150 real English
    `in_sample` reviews (half 1-3★, half 4-5★, excluding `label_sample`
    itself; fixed seed=99, ids in
    `data/interim/taxonomy_reading_sample_ids.json`). Final taxonomy: 9
    categories (`durability_defect`, `performance`, `connectivity_app`,
    `noise`, `design_usability`, `safety`, `customer_service`,
    `shipping_packaging`, `price_value`) plus `no_complaint` and `other`,
    each with a one-line definition, plus a primary-complaint rule (the
    complaint that ended or most limited use of the product) and an
    explicit tie-breaker precedence for when more than one category seems
    to apply. — *Rejected:* the original placeholder list, which only had
    `performance` to cover every functional complaint. — *Why:* reading
    real reviews showed noise, connectivity/app failures, and
    design/usability issues are frequent, distinct complaint types that a
    single `performance` bucket would blur together — collapsing them
    would make per-category complaint rates less useful for the dashboard
    and brief.

24. **`labels` table schema: dropped `is_defect`, added `labeler`.** —
    *Rejected:* keeping `is_defect` as a separate boolean. — *Why:*
    `is_defect` was redundant with `complaint_category` + `severity` (a
    defect is identifiable from the category itself, e.g.
    `durability_defect`); `labeler` is needed because label sources now
    genuinely differ (`claude-chat-reference` for the reference set,
    `manual` if `label_tool.py` is ever used for spot-checking). Migrated
    the existing (empty) `labels` table in place with `ALTER TABLE`;
    `src/load_duckdb.py`'s `CREATE TABLE` reflects the new schema for any
    future fresh database.

25. **Extraction model switched from Anthropic (claude-haiku) to Gemini
    Flash via Google AI Studio** (`src/extract.py`, model name in
    `config.EXTRACTION_MODEL`), key as `GEMINI_API_KEY` in `.env`. —
    *Rejected:* calling an Anthropic model directly (no API access set up
    for it). — *Why:* Google AI Studio has a usable free tier, avoiding a
    paid API dependency for a portfolio project. **Caveat:** the exact
    model name, pricing, and free-tier rate limits in `config.py` are
    placeholders, flagged in-file to verify against
    ai.google.dev/gemini-api/docs before a real run — this assistant's
    knowledge of current Gemini model names/pricing may be stale.

26. **`src/taxonomy.py` factored out as a shared module** (`load_taxonomy`,
    `valid_category_keys`, `validate_label`, `render_prompt_block`), used
    by both `import_reference_labels.py` and `extract.py`. — *Rejected:*
    each script re-implementing its own copy of category/severity
    validation. — *Why:* reference labels and extraction output must be
    held to the identical taxonomy rules for the agreement numbers to mean
    anything; one shared validator makes that true by construction instead
    of by discipline. Caught during this refactor: the primary-complaint
    rule and tie-breakers in `categories.yaml` were originally written as
    YAML comments, which `yaml.safe_load()` silently ignores — they would
    never have reached the extraction prompt. Fixed by moving them into
    real YAML fields (`primary_complaint_rule`, `tie_breakers`) that
    `render_prompt_block()` actually reads.

27. **Extraction pipeline design** (`src/extract.py`): batches of ~20
    reviews per request; Gemini's native `responseSchema` (JSON mode)
    constrains the shape of the model's output, then every item is
    re-validated independently with Pydantic plus `taxonomy.validate_label`
    (never trusting the API's structured-output guarantee alone); a batch
    that fails validation gets one corrective follow-up prompt (up to
    `EXTRACTION_MAX_MALFORMED_RETRIES`, default 3) listing what was wrong,
    and if still broken after that, those review_ids are left uncached
    rather than the run crashing — they're picked up automatically on the
    next invocation. The `extractions` table (keyed on `review_id, model`)
    is simultaneously the cache and the resume mechanism: anything already
    there is never re-sent. Rate-limit (429) and network errors get
    exponential backoff (`EXTRACTION_MAX_RATE_LIMIT_RETRIES`, respecting a
    `Retry-After` header when the API sends one). — *Rejected:* trusting
    `responseSchema` alone without independent validation; crashing the
    whole run on one bad batch; an unbounded retry loop. — *Why:* API
    structured-output guarantees aren't a substitute for enforcing our own
    taxonomy rules (e.g. severity must be null for `no_complaint`), and a
    multi-hour run against thousands of reviews needs to survive a single
    bad batch or a transient rate limit without losing all prior progress.
    Verified with mocked API responses (no real key needed): a perfect
    first response, a malformed-then-corrected retry, and a
    permanently-malformed batch that correctly gives up and reports every
    review_id as still-pending rather than raising.

28. **`src/agreement.py` implements accuracy, Cohen's kappa, per-category
    precision/recall, a confusion matrix, and severity agreement in plain
    Python** (no sklearn/numpy). — *Rejected:* adding a stats/ML library
    for this. — *Why:* these are a few dozen lines of arithmetic over
    small label sets (150 pairs); a dependency isn't justified. Verified
    against a hand-computed 10-pair synthetic dataset (accuracy, kappa,
    severity exact-match/mean-abs-diff, and every per-category
    precision/recall value matched hand calculation exactly).
