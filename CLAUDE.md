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
