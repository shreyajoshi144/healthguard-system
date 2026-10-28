

from __future__ import annotations
import io
import uuid
import threading
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from modules.data_loader import load_data
from modules.data_validator import validate_dataframe
from modules.data_cleaner import clean_data
from modules.preprocessor import preprocess_data
from modules.feature_engineer import engineer_features
from modules.sentiment import run_sentiment_analysis
from modules.issue_categorizer import categorize_issues
from modules.priority_scorer import score_priorities
from modules.action_mapper import map_actions
from modules.recommender import generate_recommendations
from modules.summarizer import summarize_feedback
from modules.insight_generator import generate_insights
from modules import ml_issue_classifier
from modules import ml_satisfaction_model
from utils.logger import get_logger

logger = get_logger(__name__)


class _NamedBytesIO(io.BytesIO):
    """Adapts raw upload bytes to the file-like interface modules/data_loader.py
    expects (a `.name` attribute + standard file object methods) — this is
    the same shape a Streamlit `UploadedFile` presents, so load_data() runs
    completely unmodified in its actual parsing/validation logic."""
    def __init__(self, data: bytes, name: str):
        super().__init__(data)
        self.name = name


# ── In-memory upload store ──────────────────────────────────────────────
_STORE_LOCK = threading.Lock()
_UPLOADS: dict[str, dict] = {}
_LATEST_UPLOAD_ID: Optional[str] = None


class PipelineError(Exception):
    """Raised for expected, user-facing pipeline failures (bad file, failed
    validation, etc). Distinct from unexpected exceptions so the API layer
    can return a 4xx instead of a 500."""


def run_full_pipeline(filename: str, file_bytes: bytes, owner_id: str | None = None) -> dict:
    """
    Run an uploaded CSV through the exact same stages app.py runs, persist
    the result in the in-memory store, and return a summary dict.
    """
    if not filename.lower().endswith(".csv"):
        raise PipelineError(
            "Only .csv uploads are supported in this API. The PDF path is "
            "an unfinished stub in app.py (pre-existing, not part of this "
            "phase's scope)."
        )

    wrapped_file = _NamedBytesIO(file_bytes, filename)
    df_raw = load_data(wrapped_file)
    if df_raw is None:
        raise PipelineError(
            "The uploaded file could not be loaded — it doesn't look like a "
            "valid patient satisfaction CSV (see modules/data_loader.py / "
            "utils/helpers.is_patient_survey for the exact checks)."
        )

    validation_warnings = validate_dataframe(df_raw)

    df = clean_data(df_raw.copy())
    df = preprocess_data(df)
    df = engineer_features(df)
    df = run_sentiment_analysis(df)
    df = categorize_issues(df)
    df = score_priorities(df)
    df = map_actions(df)

    insights = generate_insights(df)
    recommendations = generate_recommendations(df)
    summaries = summarize_feedback(df)

    upload_id = str(uuid.uuid4())
    record = {
        "upload_id": upload_id,
        "owner_id": owner_id,
        "filename": filename,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "df": df,
        "validation_warnings": validation_warnings,
        "insights": insights,
        "recommendations": recommendations,
        "summaries": summaries,
    }

    global _LATEST_UPLOAD_ID
    with _STORE_LOCK:
        _UPLOADS[upload_id] = record
        _LATEST_UPLOAD_ID = upload_id

    logger.info("Pipeline run complete for upload_id=%s, shape=%s", upload_id, df.shape)

    return {
        "upload_id": upload_id,
        "owner_id": owner_id,
        "filename": filename,
        "n_rows": len(df),
        "n_columns": len(df.columns),
        "columns": list(df.columns),
        "validation_warnings": validation_warnings,
        "issue_category_source_counts": (
            df["issue_category_source"].value_counts().to_dict()
            if "issue_category_source" in df.columns else {}
        ),
    }


def get_upload(upload_id: Optional[str] = None, owner_id: str | None = None) -> dict:
    """Return the stored record for upload_id, or the most recent upload
    if upload_id is None. Raises PipelineError if nothing is available."""
    with _STORE_LOCK:
        target_id = upload_id or _LATEST_UPLOAD_ID
        if target_id is None or target_id not in _UPLOADS or (owner_id is not None and _UPLOADS[target_id].get("owner_id") != owner_id):
            raise PipelineError(
                "No uploaded dataset found. POST a CSV to /api/upload first."
            )
        return _UPLOADS[target_id]


def list_uploads(owner_id: str | None = None) -> list[dict]:
    with _STORE_LOCK:
        return [
            {
                "upload_id": rec["upload_id"],
                "filename": rec["filename"],
                "uploaded_at": rec["uploaded_at"],
                "n_rows": len(rec["df"]),
            }
            for rec in sorted(_UPLOADS.values(), key=lambda r: r["uploaded_at"]) if owner_id is None or rec.get("owner_id") == owner_id
        ]


def analyze_single_feedback(
    feedback_text: str,
    department: Optional[str] = None,
    overall_rating: Optional[int] = None,
) -> dict:
    """
    Run a single piece of feedback through the same pipeline stages a CSV
    upload goes through (minus the parts that only make sense for a full
    table), using ONLY persisted, already-trained models — no fitting/
    retraining happens anywhere in this call.
    """
    row = {
        "patient_id": "adhoc-1",
        "department": department or "General",
        "feedback_text": feedback_text,
    }
    if overall_rating is not None:
        row["overall_rating"] = overall_rating

    df = pd.DataFrame([row])
    df = clean_data(df)
    df = preprocess_data(df)
    df = engineer_features(df)
    df = run_sentiment_analysis(df)
    df = categorize_issues(df)
    df = score_priorities(df)
    df = map_actions(df)

    result = {
        "feedback_text": feedback_text,
        "department": department or "General",
        "overall_rating": overall_rating,
        "sentiment": {
            "label": df.at[0, "sentiment_label"] if "sentiment_label" in df.columns else None,
            "compound": float(df.at[0, "vader_compound"]) if "vader_compound" in df.columns else None,
            "score_0_5": float(df.at[0, "sentiment_score"]) if "sentiment_score" in df.columns else None,
        },
        "issue_category": df.at[0, "issue_category"] if "issue_category" in df.columns else None,
        "issue_category_confidence": (
            float(df.at[0, "issue_category_confidence"])
            if "issue_category_confidence" in df.columns else None
        ),
        "issue_category_source": (
            df.at[0, "issue_category_source"] if "issue_category_source" in df.columns else None
        ),
        "priority_score": float(df.at[0, "priority_score"]) if "priority_score" in df.columns else None,
        "priority_label": df.at[0, "priority_label"] if "priority_label" in df.columns else None,
        "recommended_action": df.at[0, "recommended_action"] if "recommended_action" in df.columns else None,
        "satisfaction_prediction": None,
    }

    if ml_satisfaction_model.is_available():
        try:
            pred = ml_satisfaction_model.predict(df)
            result["satisfaction_prediction"] = {
                "predicted_rating_band": str(pred[0]),
                "model_status": "experimental",
                "note": (
                    "Predicted from text sentiment, length, department, and "
                    "issue category only (no rating used as input). "
                    "Experimental — see /api/metrics for measured accuracy."
                ),
            }
        except Exception as exc:
            logger.warning("Satisfaction prediction failed for ad-hoc analysis: %s", exc)

    return result


def get_model_status() -> dict:
    return {
        "issue_classifier_available": ml_issue_classifier.is_available(),
        "satisfaction_model_available": ml_satisfaction_model.is_available(),
        "issue_classifier_confidence_threshold": ml_issue_classifier.get_confidence_threshold(),
    }
