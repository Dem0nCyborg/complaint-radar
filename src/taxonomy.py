"""Shared helpers for reading config/categories.yaml and validating labels
against it. Used by src/import_reference_labels.py and src/extract.py so
reference labels and extraction output are held to the identical rules.
"""

from __future__ import annotations

import yaml

from src import config


def load_taxonomy() -> dict:
    return yaml.safe_load(config.CATEGORIES_YAML_PATH.read_text())


def valid_category_keys(taxonomy: dict | None = None) -> set[str]:
    taxonomy = taxonomy or load_taxonomy()
    return {c["key"] for c in taxonomy["categories"]}


def validate_label(category: str, severity, note, valid_categories: set[str]) -> list[str]:
    """Returns a list of human-readable error strings; empty if valid."""
    errors = []
    if category not in valid_categories:
        errors.append(f"'{category}' is not a category in categories.yaml")
        return errors

    if category == "no_complaint":
        if severity is not None:
            errors.append("severity must be null for no_complaint")
    else:
        if severity not in (1, 2, 3):
            errors.append(f"severity must be 1, 2, or 3 for '{category}', got {severity!r}")

    if category == "other" and not note:
        errors.append("category 'other' requires a note")

    return errors


def render_prompt_block(taxonomy: dict | None = None) -> str:
    """Renders categories.yaml into the text block embedded verbatim in
    every extraction prompt, so every batch sees the identical taxonomy
    text (same categories, definitions, rubric, and rules as
    config/categories.yaml -- no separate restatement that could drift).
    """
    taxonomy = taxonomy or load_taxonomy()
    lines = ["COMPLAINT CATEGORIES:"]
    for c in taxonomy["categories"]:
        lines.append(f"- {c['key']}: {c['definition']}")

    lines.append("")
    lines.append("SEVERITY (omit/null for no_complaint; required 1-3 for every other category):")
    for level, info in sorted(taxonomy["severity"].items()):
        lines.append(f"- {level} ({info['label']}): {info['description']}")

    lines.append("")
    lines.append("PRIMARY-COMPLAINT RULE:")
    lines.append(taxonomy["primary_complaint_rule"].strip())

    lines.append("")
    lines.append("TIE-BREAKERS (apply in order, only if the primary-complaint rule still leaves a tie):")
    for i, rule in enumerate(taxonomy["tie_breakers"], start=1):
        lines.append(f"{i}. {rule}")

    lines.append("")
    lines.append("RULES:")
    for rule in taxonomy["labeling_rules"]:
        lines.append(f"- {rule}")

    return "\n".join(lines)
