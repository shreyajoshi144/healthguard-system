"""
modules/ml_satisfaction_model.py
──────────────────────────────────
Satisfaction-tier prediction model: predicts `rating_band`
(Poor / Average / Good / Excellent) from signals that are available
*independently* of the rating itself.

LEAKAGE — FEATURE / TARGET LOGIC (read before touching this file)
────────────────────────────────────────────────────────────────
Target:
    rating_band — derived by feature_engineer.py by binning overall_rating.

Features used (all derived from TEXT or metadata, never from the rating):
    - vader_compound   : VADER sentiment score of the feedback text
                         (computed by sentiment.py from feedback_text/clean_text)
    - word_count        : length of the cleaned feedback text
    - char_count         : character length of the cleaned feedback text
    - department        : categorical, one-hot encoded
    - issue_category    : categorical (rule-based or ML-predicted), one-hot encoded

Explicitly EXCLUDED because they are derived from overall_rating and would
leak the target directly:
    overall_rating, rating_band, is_promoter, is_detractor, nps_category,
    composite_score, priority_score (priority_score itself partly derives
    from rating).

If a future dataset does not support this (e.g. no rating column at all, or
fewer than 2 usable classes after binning), `fit_and_evaluate` raises a
clear ValueError instead of silently training on a degenerate target.
"""

from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import pandas as pd
import joblib
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support,
    confusion_matrix, classification_report,
)

from utils.logger import get_logger
from utils.config import SATISFACTION_MODEL_DIR, RANDOM_SEED

logger = get_logger(__name__)

MODEL_PATH = SATISFACTION_MODEL_DIR / "satisfaction_pipeline.joblib"
METRICS_PATH = SATISFACTION_MODEL_DIR / "metrics.json"

NUMERIC_FEATURES = ["vader_compound", "word_count", "char_count"]
CATEGORICAL_FEATURES = ["department", "issue_category"]
TARGET_COL = "rating_band"

LEAKY_COLUMNS = {
    "overall_rating", "rating", "score", "satisfaction_score", "rating_band",
    "is_promoter", "is_detractor", "nps_category", "composite_score",
    "priority_score", "priority_label",
}


# ─────────────────────────────────────────────────────────────────────────
# Training
# ─────────────────────────────────────────────────────────────────────────

def _check_leakage_free(feature_cols: list[str]) -> None:
    leaked = set(feature_cols) & LEAKY_COLUMNS
    if leaked:
        raise ValueError(f"Refusing to train: leaky columns in feature set: {leaked}")


def fit_and_evaluate(
    df: pd.DataFrame,
    test_size: float = 0.15,
    val_size: float = 0.15,
    random_state: int = RANDOM_SEED,
    dataset_notice: str | None = None,
) -> dict:
    missing = [c for c in NUMERIC_FEATURES + CATEGORICAL_FEATURES + [TARGET_COL] if c not in df.columns]
    if missing:
        raise ValueError(
            f"Cannot train satisfaction model — required columns missing: {missing}. "
            "This dataset does not support a valid, leakage-free satisfaction target."
        )

    feature_cols = NUMERIC_FEATURES + CATEGORICAL_FEATURES
    _check_leakage_free(feature_cols)

    data = df[feature_cols + [TARGET_COL]].dropna(subset=[TARGET_COL]).copy()
    data[TARGET_COL] = data[TARGET_COL].astype(str)

    class_counts = data[TARGET_COL].value_counts()
    valid_classes = class_counts[class_counts >= 3].index
    if len(valid_classes) < 2:
        raise ValueError(
            "Cannot train satisfaction model — fewer than 2 target classes with >=3 samples. "
            "Not a valid classification target for this dataset."
        )
    data = data[data[TARGET_COL].isin(valid_classes)]

    X = data[feature_cols]
    y = data[TARGET_COL]

    X_temp, X_test, y_temp, y_test = train_test_split(
        X, y, test_size=test_size, random_state=random_state, stratify=y
    )
    relative_val = val_size / (1 - test_size)
    X_train, X_val, y_train, y_val = train_test_split(
        X_temp, y_temp, test_size=relative_val, random_state=random_state, stratify=y_temp
    )

    logger.info(
        "Satisfaction model split sizes -> train:%d val:%d test:%d",
        len(X_train), len(X_val), len(X_test),
    )

    preprocessor = ColumnTransformer(
        transformers=[
            ("num", "passthrough", NUMERIC_FEATURES),
            ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES),
        ]
    )
    pipeline = Pipeline(steps=[
        ("preprocess", preprocessor),
        ("clf", RandomForestClassifier(
            n_estimators=200,
            max_depth=8,
            class_weight="balanced",
            random_state=random_state,
            n_jobs=-1,
        )),
    ])
    pipeline.fit(X_train, y_train)

    val_metrics = _evaluate(pipeline, X_val, y_val, "validation")
    test_metrics = _evaluate(pipeline, X_test, y_test, "test")

    metrics = {
        "model": "random_forest_satisfaction_tier_classifier",
        "model_status": "experimental",
        "status_note": (
            "This model is experimental. Its accuracy is well above the "
            "4-class random baseline (25%) but is not strong enough for "
            "unsupervised operational decisions — treat predictions as a "
            "supporting signal, not a substitute for the actual rating."
        ),
        "dataset_notice": dataset_notice or (
            "Evaluated on whatever dataset was passed to fit_and_evaluate(). "
            "Check the caller / metrics provenance before treating these "
            "numbers as real-world performance."
        ),
        "target": TARGET_COL,
        "features": feature_cols,
        "n_train": len(X_train),
        "n_val": len(X_val),
        "n_test": len(X_test),
        "classes": sorted(y.unique().tolist()),
        "validation": val_metrics,
        "test": test_metrics,
    }

    SATISFACTION_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, MODEL_PATH)
    with open(METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    logger.info("Satisfaction model trained and persisted to %s", SATISFACTION_MODEL_DIR)
    return metrics


def _evaluate(pipeline, X, y_true, split_name: str) -> dict:
    y_pred = pipeline.predict(X)
    acc = accuracy_score(y_true, y_pred)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_true, y_pred, average="macro", zero_division=0
    )
    labels_sorted = sorted(set(y_true) | set(y_pred))
    cm = confusion_matrix(y_true, y_pred, labels=labels_sorted)
    report = classification_report(y_true, y_pred, zero_division=0, output_dict=True)

    logger.info(
        "[satisfaction:%s] accuracy=%.3f macro_precision=%.3f macro_recall=%.3f macro_f1=%.3f",
        split_name, acc, precision, recall, f1,
    )

    return {
        "accuracy": round(float(acc), 4),
        "macro_precision": round(float(precision), 4),
        "macro_recall": round(float(recall), 4),
        "macro_f1": round(float(f1), 4),
        "confusion_matrix": cm.tolist(),
        "confusion_matrix_labels": labels_sorted,
        "per_class_report": report,
    }


# ─────────────────────────────────────────────────────────────────────────
# Inference
# ─────────────────────────────────────────────────────────────────────────

_pipeline_cache = None


def is_available() -> bool:
    return MODEL_PATH.exists()


def _load():
    global _pipeline_cache
    if _pipeline_cache is None:
        if not is_available():
            raise FileNotFoundError(
                f"Satisfaction model artifact not found at {MODEL_PATH}. "
                "Run `python training/train_models.py` first."
            )
        _pipeline_cache = joblib.load(MODEL_PATH)
        logger.info("Loaded satisfaction model pipeline from %s", MODEL_PATH)
    return _pipeline_cache


def predict(df: pd.DataFrame) -> np.ndarray:
    """Predict rating_band for new rows. Expects NUMERIC_FEATURES and
    CATEGORICAL_FEATURES columns to already be present (i.e. run after
    feature_engineer + sentiment + issue_categorizer in the pipeline)."""
    pipeline = _load()
    missing = [c for c in NUMERIC_FEATURES + CATEGORICAL_FEATURES if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns for satisfaction inference: {missing}")
    return pipeline.predict(df[NUMERIC_FEATURES + CATEGORICAL_FEATURES])
