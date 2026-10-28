"""
modules/ml_issue_classifier.py
────────────────────────────────
Supervised ML issue-category classifier: TF-IDF (word n-grams) + multinomial
Logistic Regression. Trained on labelled patient feedback text.

This module owns two responsibilities:
  1. TRAINING  — fit_and_evaluate() fits the vectorizer + classifier on a
     train split, evaluates on held-out validation/test splits, and returns
     metrics (accuracy, precision, recall, F1, confusion matrix).
  2. INFERENCE — predict() loads the persisted vectorizer + model from disk
     and classifies new feedback text without any retraining.

No leakage: the only input feature is the free-text feedback itself (TF-IDF
of the cleaned text). No rating, sentiment, or other engineered columns are
used, so this model cannot leak target information from itself.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support,
    confusion_matrix, classification_report,
)

from utils.logger import get_logger
from utils.config import (
    MODELS_DIR, ISSUE_MODEL_DIR, RANDOM_SEED,
    ISSUE_ML_CONFIDENCE_THRESHOLD,
)

logger = get_logger(__name__)

VECTORIZER_PATH = ISSUE_MODEL_DIR / "tfidf_vectorizer.joblib"
MODEL_PATH = ISSUE_MODEL_DIR / "logreg_issue_classifier.joblib"
METRICS_PATH = ISSUE_MODEL_DIR / "metrics.json"


# ─────────────────────────────────────────────────────────────────────────
# Training
# ─────────────────────────────────────────────────────────────────────────

def fit_and_evaluate(
    texts: pd.Series,
    labels: pd.Series,
    test_size: float = 0.15,
    val_size: float = 0.15,
    random_state: int = RANDOM_SEED,
    dataset_notice: str | None = None,
) -> dict:
    """
    Train/val/test split -> fit TF-IDF + LogisticRegression on train only ->
    evaluate on val and test. Persists the fitted vectorizer + model +
    metrics.json under ISSUE_MODEL_DIR. Returns the metrics dict.

    `dataset_notice` is stored verbatim in metrics.json (and logged) so
    anyone reading the persisted metrics — not just this file's docstring —
    sees what the numbers were measured against. Pass a clear note when
    training on a synthetic/benchmark dataset so scores aren't mistaken for
    real-world performance.
    """
    texts = texts.astype(str)
    labels = labels.astype(str)

    # Drop rows with empty text or singleton classes (can't stratify on n=1)
    mask = texts.str.strip().ne("")
    texts, labels = texts[mask], labels[mask]
    class_counts = labels.value_counts()
    valid_classes = class_counts[class_counts >= 3].index
    keep = labels.isin(valid_classes)
    dropped = (~keep).sum()
    if dropped:
        logger.warning("Dropping %d rows with rare classes (<3 samples)", dropped)
    texts, labels = texts[keep], labels[keep]

    # First split off test, then split remainder into train/val
    X_temp, X_test, y_temp, y_test = train_test_split(
        texts, labels, test_size=test_size, random_state=random_state, stratify=labels
    )
    relative_val = val_size / (1 - test_size)
    X_train, X_val, y_train, y_val = train_test_split(
        X_temp, y_temp, test_size=relative_val, random_state=random_state, stratify=y_temp
    )

    logger.info(
        "Issue classifier split sizes -> train:%d val:%d test:%d",
        len(X_train), len(X_val), len(X_test),
    )

    vectorizer = TfidfVectorizer(
        ngram_range=(1, 2),
        min_df=2,
        max_df=0.9,
        sublinear_tf=True,
    )
    X_train_vec = vectorizer.fit_transform(X_train)
    X_val_vec = vectorizer.transform(X_val)
    X_test_vec = vectorizer.transform(X_test)

    clf = LogisticRegression(
        max_iter=1000,
        class_weight="balanced",
        random_state=random_state,
    )
    clf.fit(X_train_vec, y_train)

    val_metrics = _evaluate(clf, X_val_vec, y_val, split_name="validation")
    test_metrics = _evaluate(clf, X_test_vec, y_test, split_name="test")

    metrics = {
        "model": "tfidf_logistic_regression_issue_classifier",
        "dataset_notice": dataset_notice or (
            "Evaluated on whatever dataset was passed to fit_and_evaluate(). "
            "Check the caller / metrics provenance before treating these "
            "numbers as real-world performance."
        ),
        "n_train": len(X_train),
        "n_val": len(X_val),
        "n_test": len(X_test),
        "classes": sorted(labels.unique().tolist()),
        "validation": val_metrics,
        "test": test_metrics,
    }

    ISSUE_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(vectorizer, VECTORIZER_PATH)
    joblib.dump(clf, MODEL_PATH)
    with open(METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    logger.info("Issue classifier trained and persisted to %s", ISSUE_MODEL_DIR)
    return metrics


def _evaluate(clf, X_vec, y_true, split_name: str) -> dict:
    y_pred = clf.predict(X_vec)
    acc = accuracy_score(y_true, y_pred)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_true, y_pred, average="macro", zero_division=0
    )
    labels_sorted = sorted(set(y_true) | set(y_pred))
    cm = confusion_matrix(y_true, y_pred, labels=labels_sorted)
    report = classification_report(y_true, y_pred, zero_division=0, output_dict=True)

    logger.info(
        "[%s] accuracy=%.3f macro_precision=%.3f macro_recall=%.3f macro_f1=%.3f",
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

_vectorizer_cache = None
_model_cache = None


def is_available() -> bool:
    return VECTORIZER_PATH.exists() and MODEL_PATH.exists()


def _load():
    global _vectorizer_cache, _model_cache
    if _vectorizer_cache is None or _model_cache is None:
        if not is_available():
            raise FileNotFoundError(
                f"ML issue classifier artifacts not found in {ISSUE_MODEL_DIR}. "
                "Run `python training/train_models.py` first."
            )
        _vectorizer_cache = joblib.load(VECTORIZER_PATH)
        _model_cache = joblib.load(MODEL_PATH)
        logger.info("Loaded ML issue classifier artifacts from %s", ISSUE_MODEL_DIR)
    return _vectorizer_cache, _model_cache


def predict(texts: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """
    Predict issue category for a batch of texts using the persisted model.
    Returns (labels, confidences) where confidences is the max predicted
    class probability per row. No retraining occurs.
    """
    vectorizer, clf = _load()
    texts_clean = texts.astype(str).fillna("")
    X_vec = vectorizer.transform(texts_clean)
    proba = clf.predict_proba(X_vec)
    class_idx = np.argmax(proba, axis=1)
    labels = clf.classes_[class_idx]
    confidences = proba[np.arange(len(proba)), class_idx]
    return labels, confidences


def predict_one(text: str) -> tuple[str, float]:
    labels, confidences = predict(pd.Series([text]))
    return labels[0], float(confidences[0])


def get_confidence_threshold() -> float:
    return ISSUE_ML_CONFIDENCE_THRESHOLD
