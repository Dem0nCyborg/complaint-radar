"""Shared paths, constants, and small pure-function helpers for the Phase 1 pipeline."""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path

# --- paths -------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
INTERIM_DIR = DATA_DIR / "interim"
PROCESSED_DIR = DATA_DIR / "processed"
REPORTS_DIR = ROOT_DIR / "reports"
CONFIG_DIR = ROOT_DIR / "config"

CANDIDATE_CSV_PATH = INTERIM_DIR / "candidate_products.csv"
EXCLUDED_PRODUCTS_CSV_PATH = INTERIM_DIR / "excluded_products.csv"
METADATA_FUNNEL_PATH = INTERIM_DIR / "metadata_funnel.json"
METADATA_CHECKPOINT_PATH = INTERIM_DIR / "ingest_metadata_checkpoint.json"

RAW_REVIEWS_PATH = RAW_DIR / "reviews_raw.jsonl"
FETCH_CHECKPOINT_PATH = RAW_DIR / "fetch_checkpoint.json"
FETCH_STATS_PATH = INTERIM_DIR / "fetch_stats.json"

CLEAN_REVIEWS_PATH = INTERIM_DIR / "reviews_clean.jsonl"
CLEAN_STATS_PATH = INTERIM_DIR / "clean_stats.json"

SELECT_SAMPLE_STATS_PATH = INTERIM_DIR / "select_sample_stats.json"

DUCKDB_PATH = PROCESSED_DIR / "complaint_radar.duckdb"
CATEGORIES_YAML_PATH = CONFIG_DIR / "categories.yaml"

DATA_QUALITY_REPORT_PATH = REPORTS_DIR / "data_quality_report.md"

# --- dataset source ------------------------------------------------------
# Both the metadata and review files are read directly as raw JSONL over
# HTTP (src/http_jsonl.py: json.loads per line, fields read with .get()),
# not through the `datasets` library at all. Originally just a workaround
# for `datasets>=5` no longer executing the loading script this dataset repo
# ships -- but metadata ingestion specifically needed this regardless, once
# `datasets`' own streaming+Arrow-cast path crashed around row 319K on rows
# with a different schema (extra columns, price as a string). See CLAUDE.md
# #17 for the full story.

_HF_RESOLVE_BASE = "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/resolve/main"
META_JSONL_URL = f"{_HF_RESOLVE_BASE}/raw/meta_categories/meta_Home_and_Kitchen.jsonl"
REVIEW_JSONL_URL = f"{_HF_RESOLVE_BASE}/raw/review_categories/Home_and_Kitchen.jsonl"

# --- category filtering ---------------------------------------------------

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "air_fryer": ["air fryer", "airfryer", "air-fryer"],
    "blender": ["blender"],
    "robot_vacuum": ["robot vacuum", "robovac", "robotic vacuum", "vacuum robot"],
}


def _word_boundary_pattern(words: list[str]):
    escaped = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(escaped) + r")\b", re.IGNORECASE)


# Word-boundary, not substring: a naive `"blender" in title_lower` check also
# fires on "BlenderBottle" (a shaker-bottle brand, not an electric blender) --
# found live in test data, where it both wrongly matched the category AND
# then fooled the weak-exclusion leading-words check below into thinking the
# title was about a real blender.
CATEGORY_PATTERNS: dict[str, "re.Pattern"] = {
    cat: _word_boundary_pattern(kws) for cat, kws in CATEGORY_KEYWORDS.items()
}

# Exclusion matching is word-boundary (regex \b...\b), not naive substring --
# a naive `"cover" in title_lower` check also fires on "discover", "recover",
# etc. Matched as whole words only.
#
# Split into two tiers:
# - STRONG: essentially never appears in a genuine appliance title, so these
#   exclude unconditionally, wherever they appear in the title.
# - WEAK: these words also show up as legitimate feature mentions in real
#   appliance titles (a blender sold "with 2 travel bottles", a robot vacuum
#   described as having a "side brush" or "HEPA filter", an air fryer oven
#   "with rack"). Excluding on sight would drop real candidates, so a weak
#   term only excludes when the category keyword itself (e.g. "air fryer")
#   does NOT appear in the leading words of the title -- i.e. when the title
#   reads like an accessory/part listing rather than the appliance itself.
STRONG_EXCLUSION_KEYWORDS: list[str] = [
    "liner",
    "liners",
    "parchment",
    "cookbook",
    "cookbooks",
    "replacement",
    "replacements",
    "accessory",
    "accessories",
    "paper",
    "parts",
]

# "bottle"/"brush" added to WEAK alongside the user-specified blade/filter/
# bags/rack/cover: same failure mode (legitimate feature mention in a real
# appliance title), flagged for review rather than silently left STRONG.
WEAK_EXCLUSION_KEYWORDS: list[str] = [
    "blade",
    "blades",
    "filter",
    "filters",
    "bag",
    "bags",
    "rack",
    "racks",
    "cover",
    "covers",
    "bottle",
    "bottles",
    "brush",
    "brushes",
]

LEADING_WORDS_WINDOW = 6  # how many leading words count as "the title's lead"

STRONG_EXCLUSION_PATTERN = _word_boundary_pattern(STRONG_EXCLUSION_KEYWORDS)
WEAK_EXCLUSION_PATTERN = _word_boundary_pattern(WEAK_EXCLUSION_KEYWORDS)


def has_category_keyword_in_leading_words(title_lower: str, category: str) -> bool:
    leading_text = " ".join(title_lower.split()[:LEADING_WORDS_WINDOW])
    return bool(CATEGORY_PATTERNS[category].search(leading_text))


def check_exclusion(title: str, category: str) -> tuple[bool, str | None, str | None]:
    """Returns (excluded, matched_term, tier) where tier is 'strong'/'weak'/None."""
    strong_match = STRONG_EXCLUSION_PATTERN.search(title)
    if strong_match:
        return True, strong_match.group(1).lower(), "strong"
    weak_match = WEAK_EXCLUSION_PATTERN.search(title)
    if weak_match and not has_category_keyword_in_leading_words(title.lower(), category):
        return True, weak_match.group(1).lower(), "weak"
    return False, None, None

# --- brand normalization ---------------------------------------------------
# Heuristic, hand-seeded, not exhaustive: strips common storefront suffixes
# and maps known aliases. SharkNinja sells both Shark (vacuums) and Ninja
# (kitchen appliances) under one corporate storefront name, so that one is
# resolved using the product's category rather than a flat string match.

GENERIC_BRAND_SUFFIXES_TO_STRIP: list[str] = [
    " official store",
    " store",
    " direct",
    ".com",
    " inc.",
    " inc",
    " llc",
    " amazon",
]

COMPOSITE_BRAND_ALIASES: dict[str, dict[str, str]] = {
    "sharkninja": {"robot_vacuum": "Shark", "air_fryer": "Ninja", "blender": "Ninja"},
}

SIMPLE_BRAND_ALIASES: dict[str, str] = {
    "ninja": "Ninja",
    "shark": "Shark",
    "hamilton beach": "Hamilton Beach",
    "instant": "Instant",
    "instant pot": "Instant",
    "cosori": "Cosori",
    "irobot": "iRobot",
    "eufy": "eufy",
    "roborock": "Roborock",
    "nutribullet": "NutriBullet",
    "vitamix": "Vitamix",
    "oster": "Oster",
    "philips": "Philips",
    "bissell": "Bissell",
    "ecovacs": "Ecovacs",
    "dreame": "Dreame",
    "cuisinart": "Cuisinart",
    "breville": "Breville",
    "kitchenaid": "KitchenAid",
    "black+decker": "Black+Decker",
    "black & decker": "Black+Decker",
}


def normalize_brand(raw_brand: str | None, category: str) -> str:
    """Best-effort normalization of a noisy storefront 'brand' string.

    Not exhaustive -- falls back to a title-cased, suffix-stripped version
    of the raw string when no alias matches.
    """
    if not raw_brand:
        return ""
    cleaned = raw_brand.strip()
    lowered = cleaned.lower()
    for suffix in GENERIC_BRAND_SUFFIXES_TO_STRIP:
        if lowered.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            lowered = cleaned.lower()
    for composite_key, category_map in COMPOSITE_BRAND_ALIASES.items():
        if composite_key in lowered:
            return category_map.get(category, cleaned.title())
    for alias_key, normalized in SIMPLE_BRAND_ALIASES.items():
        if alias_key in lowered:
            return normalized
    return cleaned if cleaned.isupper() and len(cleaned) <= 5 else cleaned.title()


# --- selection -------------------------------------------------------------


def load_selected_parent_asins() -> set[str]:
    """The single source of truth for "which products are in the pipeline
    right now": the `selected` column in the candidate CSV. Shared by
    fetch_reviews.py, clean.py, and load_duckdb.py so that deselecting a
    product there (e.g. dropping a mislabeled/duplicate-listing product
    after fetch already ran) takes effect at every later stage without
    needing to re-fetch -- see CLAUDE.md on dropping B00AIHD94Y.
    """
    if not CANDIDATE_CSV_PATH.exists():
        raise SystemExit(f"{CANDIDATE_CSV_PATH} not found. Run `python -m src.ingest_metadata` first.")
    selected = set()
    with open(CANDIDATE_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("selected") or "").strip().lower() == "y":
                selected.add(row["parent_asin"])
    if not selected:
        raise SystemExit(
            f"No rows marked selected='y' in {CANDIDATE_CSV_PATH}. Edit that file first, then rerun."
        )
    return selected


# --- fetch / sampling parameters -------------------------------------------

CHECKPOINT_INTERVAL = 2000  # rows scanned between checkpoint writes (ingest_metadata.py and fetch_reviews.py)

SAMPLE_SIZE = 4000
SAMPLE_SEED = 42

LABEL_SAMPLE_SIZE = 150
LABEL_SAMPLE_SEED = 7
LABEL_SAMPLE_LOW_STAR_FRACTION = 0.5  # target share of the sample that is 1-3 star


def compute_review_id(user_id: str, parent_asin: str, timestamp: int, text: str) -> str:
    """Stable hash used as BOTH the dedupe key and the stored review_id.

    Using the full review text (not a truncated prefix) in the hash input is
    deliberate: truncating risks two distinct long reviews from the same
    user/product/timestamp colliding on the same primary key.
    """
    raw = f"{user_id}|{parent_asin}|{timestamp}|{text.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
