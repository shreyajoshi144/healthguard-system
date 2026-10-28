
from __future__ import annotations
import json
import pandas as pd

from utils.config import ISSUE_MODEL_DIR, SATISFACTION_MODEL_DIR
from utils.logger import get_logger

logger = get_logger(__name__)


def _value_counts_dict(series: pd.Series) -> dict:
    return series.value_counts(dropna=True).to_dict()


def _safe_round(x, n=2):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


def build_dashboard_metrics(record: dict) -> dict:
    df: pd.DataFrame = record["df"]

    kpis = {
        "total_responses": len(df),
        "avg_rating": _safe_round(df["overall_rating"].mean()) if "overall_rating" in df.columns else None,
        "positive_pct": (
            _safe_round((df["sentiment_label"] == "Positive").mean() * 100)
            if "sentiment_label" in df.columns else None
        ),
        "negative_pct": (
            _safe_round((df["sentiment_label"] == "Negative").mean() * 100)
            if "sentiment_label" in df.columns else None
        ),
        "departments": int(df["department"].nunique()) if "department" in df.columns else None,
        "critical_issues": (
            int((df["priority_score"] >= 7).sum()) if "priority_score" in df.columns else None
        ),
    }

    sentiment_distribution = (
        _value_counts_dict(df["sentiment_label"]) if "sentiment_label" in df.columns else {}
    )
    issue_category_distribution = (
        _value_counts_dict(df["issue_category"]) if "issue_category" in df.columns else {}
    )
    priority_distribution = (
        _value_counts_dict(df["priority_label"]) if "priority_label" in df.columns else {}
    )
    issue_category_source_counts = (
        _value_counts_dict(df["issue_category_source"]) if "issue_category_source" in df.columns else {}
    )
    rating_distribution = (
        _value_counts_dict(df["overall_rating"].round(0)) if "overall_rating" in df.columns else {}
    )

    department_breakdown = []
    if "department" in df.columns:
        for dept, dept_df in df.groupby("department"):
            department_breakdown.append({
                "department": dept,
                "total": len(dept_df),
                "avg_rating": _safe_round(dept_df["overall_rating"].mean()) if "overall_rating" in dept_df.columns else None,
                "positive_pct": (
                    _safe_round((dept_df["sentiment_label"] == "Positive").mean() * 100)
                    if "sentiment_label" in dept_df.columns else None
                ),
                "negative_pct": (
                    _safe_round((dept_df["sentiment_label"] == "Negative").mean() * 100)
                    if "sentiment_label" in dept_df.columns else None
                ),
                "top_issue": (
                    dept_df["issue_category"].mode().iloc[0]
                    if "issue_category" in dept_df.columns and len(dept_df) else None
                ),
                "critical_issues": (
                    int((dept_df["priority_score"] >= 7).sum()) if "priority_score" in dept_df.columns else 0
                ),
            })
        department_breakdown.sort(key=lambda d: d["total"], reverse=True)

    top_keywords = []
    if "clean_text" in df.columns:
        from collections import Counter
        stop = {"the","is","and","to","of","in","for","on","with","was","were","it","this",
                "that","very","had","have","has","not","they","them","you","are","or"}
        words = " ".join(df["clean_text"].dropna().astype(str)).split()
        words = [w for w in words if w not in stop and len(w) > 2]
        top_keywords = [{"word": w, "count": c} for w, c in Counter(words).most_common(12)]

    return {
        "upload_id": record["upload_id"],
        "filename": record["filename"],
        "uploaded_at": record["uploaded_at"],
        "validation_warnings": record["validation_warnings"],
        "kpis": kpis,
        "sentiment_distribution": sentiment_distribution,
        "issue_category_distribution": issue_category_distribution,
        "priority_distribution": priority_distribution,
        "issue_category_source_counts": issue_category_source_counts,
        "rating_distribution": {str(k): v for k, v in rating_distribution.items()},
        "department_breakdown": department_breakdown,
        "top_keywords": top_keywords,
        "recommendations": record["recommendations"],
        "summaries": record["summaries"],
        "insights": record["insights"],
    }


def _load_json_if_exists(path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to read model metrics at %s: %s", path, exc)
        return None


def build_model_metrics() -> dict:
    """Read-only surface of the persisted training metrics.json files.
    Returns whatever training/train_models.py actually measured — nothing
    here is computed or estimated by the API."""
    issue_metrics = _load_json_if_exists(ISSUE_MODEL_DIR / "metrics.json")
    satisfaction_metrics = _load_json_if_exists(SATISFACTION_MODEL_DIR / "metrics.json")
    return {
        "issue_classifier": issue_metrics,
        "satisfaction_model": satisfaction_metrics,
    }
