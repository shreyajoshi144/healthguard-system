from __future__ import annotations
import re
from typing import Iterable

import pandas as pd

# Columns that are derived from (or are) a target/label anywhere in the
# existing pipeline. None of these may ever be used as an input feature for
# the issue classifier. Mirrors modules/ml_satisfaction_model.py's
# LEAKY_COLUMNS concept but scoped to the issue-classification task, and
# extended with the Phase 3A contract's own target-shaped fields.
TARGET_DERIVED_COLUMNS = {
    "issue_category", "issue_category_confidence", "issue_category_source",
    "true_issue_category", "primary_issue", "secondary_issues",
    "priority_score", "priority_label", "recommended_action",
    "rating_band", "is_promoter", "is_detractor", "nps_category",
    "composite_score", "overall_rating", "rating", "score", "satisfaction_score",
    "severity", "requires_human_review",
}

# The only columns the issue classifier is allowed to read features from.
ALLOWED_ISSUE_CLASSIFIER_FEATURES = {"feedback_text", "clean_text"}


def check_column_leakage(
    feature_columns: Iterable[str],
    target_columns: Iterable[str] = TARGET_DERIVED_COLUMNS,
) -> list[str]:
    """
    Returns the (sorted) list of column names that appear in BOTH
    feature_columns and target_columns — i.e. columns that would leak the
    target into the model's inputs. An empty list means the check passed.
    """
    return sorted(set(feature_columns) & set(target_columns))


def assert_no_column_leakage(
    feature_columns: Iterable[str],
    target_columns: Iterable[str] = TARGET_DERIVED_COLUMNS,
) -> None:
    leaked = check_column_leakage(feature_columns, target_columns)
    if leaked:
        raise ValueError(f"Column-level leakage check FAILED — leaky feature columns: {leaked}")


def assert_baseline_feature_columns_allowed(feature_columns: Iterable[str]) -> None:
    """
    Specific, applied check for the existing TF-IDF baseline: every feature
    column it's given must be one of ALLOWED_ISSUE_CLASSIFIER_FEATURES.
    Stricter than assert_no_column_leakage — an allowlist, not a denylist —
    because the issue classifier's only legitimate input is raw text.
    """
    not_allowed = sorted(set(feature_columns) - ALLOWED_ISSUE_CLASSIFIER_FEATURES)
    if not_allowed:
        raise ValueError(
            f"Baseline issue classifier was given non-text feature columns: {not_allowed}. "
            f"Only {sorted(ALLOWED_ISSUE_CLASSIFIER_FEATURES)} are allowed."
        )


def _normalize_text(text: str) -> str:
    """Lowercase + collapse whitespace/punctuation, so trivial formatting
    differences (extra spaces, a trailing period) don't hide a genuine
    duplicate from the overlap check."""
    text = str(text).lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def check_split_disjointness(splits: dict[str, "pd.Series"]) -> dict:
    """
    Pairwise-check every named split's feedback_text for overlap with every
    other split, using normalized-text matching (catches exact duplicates
    and trivial formatting variants, not full paraphrase detection).

    `splits` is a dict like {"train": series, "val": series, "test": series,
    "challenge": series} of feedback_text values.

    Returns a report dict:
        {
          "pairs": {"train_vs_challenge": {"n_overlap": 0, "examples": []}, ...},
          "any_overlap": bool
        }
    """
    normalized = {name: set(_normalize_text(t) for t in series) for name, series in splits.items()}
    names = list(normalized.keys())

    pairs = {}
    any_overlap = False
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            overlap = normalized[a] & normalized[b]
            pairs[f"{a}_vs_{b}"] = {
                "n_overlap": len(overlap),
                "examples": sorted(overlap)[:5],  # cap for readability
            }
            if overlap:
                any_overlap = True

    return {"pairs": pairs, "any_overlap": any_overlap}


def assert_no_split_leakage(splits: dict[str, "pd.Series"]) -> dict:
    """Raises if check_split_disjointness finds any overlap. Returns the
    report dict on success, so callers can embed it in a reproducibility
    report without re-running the check."""
    report = check_split_disjointness(splits)
    if report["any_overlap"]:
        offending = {k: v for k, v in report["pairs"].items() if v["n_overlap"] > 0}
        raise ValueError(f"Split-level leakage check FAILED — overlapping examples found: {offending}")
    return report
