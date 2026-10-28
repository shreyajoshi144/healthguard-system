from __future__ import annotations
import numpy as np
import pandas as pd
import scipy.sparse as sp

from evaluation import frozen_models as fm

OOD_VALIDATION_FLAG_RATE_TARGET = 0.05  # threshold = the value that would flag ~5% of in-domain validation


def compute_class_centroids(model_name: str, train_texts: pd.Series, train_labels: pd.Series) -> dict:
    """TRAIN ONLY: one mean feature vector per class, in the given frozen
    model's own feature space. Returns {class_label: centroid_vector} plus
    the class order, for reproducibility."""
    X_train = fm.features_for(model_name, train_texts)
    labels = train_labels.astype(str).to_numpy()
    centroids = {}
    for label in sorted(set(labels)):
        mask = labels == label
        rows = X_train[mask]
        if sp.issparse(rows):
            centroid = np.asarray(rows.mean(axis=0)).ravel()
        else:
            centroid = rows.mean(axis=0)
        centroids[label] = centroid
    return centroids


def _cosine_similarity_to_centroids(X, centroids: dict) -> np.ndarray:
    """Returns (n_samples, n_classes) cosine similarity matrix between X's
    rows and each centroid. Handles both sparse (TF-IDF) and dense
    (embedding) X uniformly."""
    labels = list(centroids.keys())
    centroid_matrix = np.stack([np.asarray(centroids[l]).ravel() for l in labels])

    if sp.issparse(X):
        X_dense = X.toarray()
    else:
        X_dense = np.asarray(X)

    X_norm = np.linalg.norm(X_dense, axis=1, keepdims=True)
    X_norm[X_norm == 0] = 1e-12
    C_norm = np.linalg.norm(centroid_matrix, axis=1, keepdims=True)
    C_norm[C_norm == 0] = 1e-12

    X_unit = X_dense / X_norm
    C_unit = centroid_matrix / C_norm
    return X_unit @ C_unit.T  # (n_samples, n_classes)


def ood_scores(model_name: str, texts: pd.Series, centroids: dict) -> np.ndarray:
    """OOD score per example = 1 - similarity to nearest training-class
    centroid. Higher = more dissimilar to anything seen in training =
    more likely OOD."""
    X = fm.features_for(model_name, texts)
    sims = _cosine_similarity_to_centroids(X, centroids)
    max_sim = sims.max(axis=1)
    return 1.0 - max_sim


def select_ood_threshold(val_scores: np.ndarray, target_flag_rate: float = OOD_VALIDATION_FLAG_RATE_TARGET) -> dict:
    """
    VALIDATION ONLY: the threshold is set at the percentile of validation
    OOD scores corresponding to target_flag_rate — i.e. the score value
    above which only `target_flag_rate` of genuinely in-domain validation
    examples fall. This is the standard way to define a one-class-style
    threshold purely from in-domain calibration data.
    """
    percentile = (1 - target_flag_rate) * 100
    threshold = float(np.percentile(val_scores, percentile))
    actual_flag_rate = float((val_scores > threshold).mean())
    return {
        "method": f"validation OOD-score {percentile:.1f}th percentile",
        "target_flag_rate": target_flag_rate,
        "threshold": round(threshold, 4),
        "actual_validation_flag_rate": round(actual_flag_rate, 4),
        "validation_score_stats": {
            "min": round(float(val_scores.min()), 4),
            "median": round(float(np.median(val_scores)), 4),
            "max": round(float(val_scores.max()), 4),
            "mean": round(float(val_scores.mean()), 4),
        },
    }


def flag_ood(scores: np.ndarray, threshold: float) -> np.ndarray:
    return scores > threshold


def build_ood_detector(model_name: str, train_texts: pd.Series, train_labels: pd.Series, val_texts: pd.Series) -> dict:
    """Fits centroids on TRAIN and selects the threshold on VALIDATION —
    the frozen, reusable OOD detector for one model."""
    centroids = compute_class_centroids(model_name, train_texts, train_labels)
    val_scores = ood_scores(model_name, val_texts, centroids)
    threshold_info = select_ood_threshold(val_scores)
    return {
        "model": model_name,
        "method": "nearest_class_centroid_cosine_distance",
        "n_classes": len(centroids),
        "centroids": centroids,  # kept in-memory only; not JSON-serialized as-is by callers
        "threshold_info": threshold_info,
    }


def evaluate_ood_detector(detector: dict, texts: pd.Series, challenge_types: pd.Series | None = None) -> dict:
    """Applies the already-frozen detector to a new split (test or
    challenge). If challenge_types is given, also breaks the OOD flag rate
    down by challenge_type."""
    scores = ood_scores(detector["model"], texts, detector["centroids"])
    threshold = detector["threshold_info"]["threshold"]
    flags = flag_ood(scores, threshold)

    result = {
        "n_examples": len(texts),
        "flag_rate": round(float(flags.mean()), 4),
        "score_stats": {
            "min": round(float(scores.min()), 4),
            "median": round(float(np.median(scores)), 4),
            "max": round(float(scores.max()), 4),
            "mean": round(float(scores.mean()), 4),
        },
    }
    if challenge_types is not None:
        by_type = {}
        ctypes = challenge_types.to_numpy()
        for ctype in sorted(set(ctypes)):
            mask = ctypes == ctype
            by_type[ctype] = {
                "n_examples": int(mask.sum()),
                "flag_rate": round(float(flags[mask].mean()), 4),
                "mean_score": round(float(scores[mask].mean()), 4),
            }
        result["by_challenge_type"] = by_type
    return result
