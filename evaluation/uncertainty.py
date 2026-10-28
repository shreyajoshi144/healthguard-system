from __future__ import annotations
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd


@dataclass
class PredictionRecord:
    prediction: str
    confidence: float
    margin: float
    entropy: float
    uncertainty_level: str

    def to_dict(self) -> dict:
        return asdict(self)


def _entropy(proba_row: np.ndarray) -> float:
    """Shannon entropy in nats, with a tiny epsilon to avoid log(0)."""
    p = np.clip(proba_row, 1e-12, 1.0)
    return float(-np.sum(p * np.log(p)))


def _margin(proba_row: np.ndarray) -> float:
    sorted_desc = np.sort(proba_row)[::-1]
    top1 = sorted_desc[0]
    top2 = sorted_desc[1] if len(sorted_desc) > 1 else 0.0
    return float(top1 - top2)


def derive_uncertainty_thresholds(val_proba: np.ndarray) -> dict:
    """
    VALIDATION ONLY: the 33rd/66th percentiles of max-class confidence on
    validation data become the Low/Medium/High uncertainty-level cut
    points. Returns the thresholds plus the validation confidence stats
    they were derived from, so the choice is auditable rather than
    hardcoded.
    """
    confidences = val_proba.max(axis=1)
    p33 = float(np.percentile(confidences, 33))
    p66 = float(np.percentile(confidences, 66))
    return {
        "method": "validation confidence percentiles (33rd / 66th)",
        "low_confidence_below": round(p33, 4),
        "high_confidence_at_or_above": round(p66, 4),
        "validation_confidence_stats": {
            "min": round(float(confidences.min()), 4),
            "p33": round(p33, 4),
            "median": round(float(np.median(confidences)), 4),
            "p66": round(p66, 4),
            "max": round(float(confidences.max()), 4),
            "mean": round(float(confidences.mean()), 4),
        },
    }


def _bucket(confidence: float, thresholds: dict) -> str:
    if confidence < thresholds["low_confidence_below"]:
        return "High"  # low confidence => high uncertainty
    if confidence >= thresholds["high_confidence_at_or_above"]:
        return "Low"   # high confidence => low uncertainty
    return "Medium"


def build_prediction_records(proba: np.ndarray, classes: np.ndarray, thresholds: dict) -> list[PredictionRecord]:
    """Builds one structured PredictionRecord per row of proba."""
    records = []
    for row in proba:
        idx = int(np.argmax(row))
        confidence = float(row[idx])
        record = PredictionRecord(
            prediction=str(classes[idx]),
            confidence=round(confidence, 4),
            margin=round(_margin(row), 4),
            entropy=round(_entropy(row), 4),
            uncertainty_level=_bucket(confidence, thresholds),
        )
        records.append(record)
    return records


def summarize_uncertainty(records: list[PredictionRecord]) -> dict:
    """Aggregate summary of a batch of PredictionRecords — distribution of
    uncertainty_level, and mean confidence/margin/entropy per level."""
    df = pd.DataFrame([r.to_dict() for r in records])
    summary = {
        "n_examples": len(df),
        "uncertainty_level_counts": df["uncertainty_level"].value_counts().to_dict(),
        "mean_confidence": round(float(df["confidence"].mean()), 4),
        "mean_margin": round(float(df["margin"].mean()), 4),
        "mean_entropy": round(float(df["entropy"].mean()), 4),
    }
    by_level = {}
    for level in ("Low", "Medium", "High"):
        subset = df[df["uncertainty_level"] == level]
        if len(subset) == 0:
            continue
        by_level[level] = {
            "n_examples": len(subset),
            "mean_confidence": round(float(subset["confidence"].mean()), 4),
            "mean_margin": round(float(subset["margin"].mean()), 4),
            "mean_entropy": round(float(subset["entropy"].mean()), 4),
        }
    summary["by_uncertainty_level"] = by_level
    return summary


def accuracy_by_uncertainty_level(records: list[PredictionRecord], y_true: pd.Series) -> dict:
    """Does 'uncertainty_level' actually track correctness? For each
    bucket, what fraction of predictions were right."""
    df = pd.DataFrame([r.to_dict() for r in records])
    df["correct"] = (df["prediction"].to_numpy() == y_true.astype(str).to_numpy()).astype(float)
    result = {}
    for level in ("Low", "Medium", "High"):
        subset = df[df["uncertainty_level"] == level]
        if len(subset) == 0:
            continue
        result[level] = {
            "n_examples": len(subset),
            "accuracy": round(float(subset["correct"].mean()), 4),
        }
    return result
