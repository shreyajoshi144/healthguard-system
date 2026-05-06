"""
utils/helpers.py
────────────────
Shared utility helpers used across all modules.
Import with:  from utils.helpers import find_rating_col, find_text_col
"""

from __future__ import annotations
import pandas as pd
import numpy as np
from typing import Optional


# ── Column finders ─────────────────────────────────────────────────────────────

def find_rating_col(df: pd.DataFrame) -> Optional[str]:
    """
    Return the name of the primary numeric satisfaction / rating column,
    or None if not found.
    Priority order: overall_rating > rating > satisfaction_score > score
    """
    candidates = ["overall_rating", "rating", "satisfaction_score", "score"]
    for c in candidates:
        if c in df.columns and pd.api.types.is_numeric_dtype(df[c]):
            return c
    # Fallback: any numeric column whose name contains 'rating' or 'score'
    for c in df.columns:
        if ("rating" in c or "score" in c) and pd.api.types.is_numeric_dtype(df[c]):
            return c
    return None


def find_text_col(df: pd.DataFrame) -> Optional[str]:
    """
    Return the name of the primary free-text feedback column,
    or None if not found.
    Falls back to the object column with the longest average string length.
    """
    priority = ["feedback_text", "clean_text", "comments", "feedback",
                "review", "text", "response", "patient_comment"]
    for c in priority:
        if c in df.columns:
            return c
    obj_cols = df.select_dtypes(include="object").columns.tolist()
    if obj_cols:
        avg_len = {c: df[c].dropna().astype(str).str.len().mean() for c in obj_cols}
        return max(avg_len, key=avg_len.get)
    return None


def find_dept_col(df: pd.DataFrame) -> Optional[str]:
    """Return the department column name, or None."""
    candidates = ["department", "dept", "ward", "unit", "division", "section"]
    for c in candidates:
        if c in df.columns:
            return c
    return None


def find_date_col(df: pd.DataFrame) -> Optional[str]:
    """Return the first datetime column, or None."""
    date_cols = df.select_dtypes(include="datetime64[ns]").columns.tolist()
    return date_cols[0] if date_cols else None


# ── Validation helpers ─────────────────────────────────────────────────────────

def is_patient_survey(df: pd.DataFrame) -> tuple[bool, str]:
    """
    Check whether the uploaded DataFrame looks like a patient satisfaction survey.
    Returns (is_valid: bool, reason: str).
    """
    if df is None or df.empty:
        return False, "DataFrame is empty."

    if len(df) < 5:
        return False, "Too few rows (< 5) to analyse."

    has_text   = find_text_col(df) is not None
    has_rating = find_rating_col(df) is not None
    has_dept   = find_dept_col(df) is not None

    # Survey keyword match in column names
    survey_keywords = {"patient", "satisfaction", "rating", "feedback", "department",
                       "staff", "wait", "cleanliness", "appointment", "hospital",
                       "clinic", "care", "doctor", "nurse", "treatment"}
    col_names_lower = set(" ".join(df.columns).lower().split("_"))
    keyword_match = bool(col_names_lower & survey_keywords)

    if not (has_text or has_rating or keyword_match):
        return False, (
            "The dataset does not appear to be a patient satisfaction survey. "
            "Expected columns: patient_id, department, overall_rating, feedback_text, date."
        )

    return True, "Dataset validated as patient satisfaction survey."


# ── Numeric utilities ──────────────────────────────────────────────────────────

def safe_mean(series: pd.Series, decimals: int = 2) -> float:
    """Return mean of series, rounded, ignoring NaNs. Returns 0.0 on empty."""
    if series.dropna().empty:
        return 0.0
    return round(float(series.mean()), decimals)


def percentile_rank(series: pd.Series, value: float) -> float:
    """Return the percentile rank (0–100) of `value` within `series`."""
    clean = series.dropna()
    if clean.empty:
        return 0.0
    return float((clean < value).mean() * 100)


def normalize_series(series: pd.Series,
                     new_min: float = 0.0,
                     new_max: float = 10.0) -> pd.Series:
    """Min-max normalize a numeric series to [new_min, new_max]."""
    s_min = series.min()
    s_max = series.max()
    if s_max == s_min:
        return pd.Series(np.full(len(series), (new_min + new_max) / 2), index=series.index)
    return (series - s_min) / (s_max - s_min) * (new_max - new_min) + new_min


# ── String utilities ───────────────────────────────────────────────────────────

def truncate_text(text: str, max_chars: int = 200) -> str:
    """Truncate a string to max_chars, appending '…' if cut."""
    if not isinstance(text, str):
        return ""
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"


def clean_column_name(name: str) -> str:
    """Standardise a column name: lower, strip, underscores."""
    import re
    return re.sub(r"[^\w]", "_", name.strip().lower())


# ── DataFrame utilities ────────────────────────────────────────────────────────

def summarise_df(df: pd.DataFrame) -> dict:
    """Return a quick summary dict for logging / display."""
    return {
        "rows":          len(df),
        "columns":       list(df.columns),
        "numeric_cols":  df.select_dtypes(include="number").columns.tolist(),
        "text_col":      find_text_col(df),
        "rating_col":    find_rating_col(df),
        "dept_col":      find_dept_col(df),
        "date_col":      find_date_col(df),
        "null_pct":      round(df.isnull().mean().mean() * 100, 2),
    }
