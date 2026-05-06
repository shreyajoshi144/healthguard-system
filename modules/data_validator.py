"""
modules/data_validator.py
─────────────────────────
Validates that the uploaded DataFrame is a well-formed
patient satisfaction survey. Returns a list of issue strings.
An empty list means validation passed.
"""

from __future__ import annotations
import pandas as pd
from typing import List
from utils.logger import get_logger
from utils.helpers import find_rating_col, find_text_col


logger = get_logger(__name__)

MIN_ROWS         = 5
RATING_MIN       = 1
RATING_MAX       = 5
RECOMMENDED_COLS = {"patient_id", "department", "overall_rating", "feedback_text", "date"}

def validate_dataframe(df: pd.DataFrame) -> List[str]:
    """Run all checks. Returns list of issues. Empty = valid."""
    if df is None or not isinstance(df, pd.DataFrame):
        return ["Dataset is None or not a valid DataFrame."]
    issues = []
    issues += _check_row_count(df)
    issues += _check_required_columns(df)
    issues += _check_rating_ranges(df)
    issues += _check_null_density(df)
    issues += _check_text_column(df)
    issues += _check_department_column(df)
    issues += _check_duplicate_patients(df)
    if issues:
        logger.warning("Validation: %d issue(s) found", len(issues))
    else:
        logger.info("Validation passed. Shape: %s", df.shape)
    return issues

def is_valid(df: pd.DataFrame) -> bool:
    return len(validate_dataframe(df)) == 0

def _check_row_count(df):
    return [f"Too few rows: {len(df)} found, minimum {MIN_ROWS} required."] if len(df) < MIN_ROWS else []

def _check_required_columns(df):
    missing = RECOMMENDED_COLS - set(df.columns)
    return [f"Recommended columns not found: {sorted(missing)}. Auto-detection will be attempted."] if missing else []

def _check_rating_ranges(df):
    issues = []
    for col in df.columns:
        if any(kw in col for kw in ["rating","score","satisfaction"]):
            if pd.api.types.is_numeric_dtype(df[col]):
                bad = ((df[col] < RATING_MIN) | (df[col] > RATING_MAX)).sum()
                if bad > 0:
                    issues.append(f"Column '{col}': {bad} values outside [1–5]. Will be clipped.")
    return issues

def _check_null_density(df):
    return [f"Column '{c}' is {df[c].isnull().mean()*100:.1f}% null — may not contribute."
            for c in df.columns if df[c].isnull().mean() > 0.5]

def _check_text_column(df):
    tc = find_text_col(df)
    if tc is None:
        return ["No feedback text column found. Sentiment & summarisation will be skipped."]
    non_empty = df[tc].dropna().astype(str).str.strip().ne("").mean() * 100
    return [f"Text column '{tc}' is only {non_empty:.1f}% non-empty. NLP quality limited."] if non_empty < 30 else []

def _check_department_column(df):
    if "department" not in df.columns:
        return ["No 'department' column. Benchmarking and per-dept recommendations unavailable."]
    n = df["department"].nunique()
    return [f"Only {n} department(s) found. Benchmarking works best with 3+."] if n < 2 else []

def _check_duplicate_patients(df):
    if "patient_id" not in df.columns:
        return []
    d = df["patient_id"].duplicated().sum()
    return [f"{d} duplicate patient_id values found. Will be removed during cleaning."] if d > 0 else []