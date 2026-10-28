from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, precision_recall_fscore_support

CANDIDATE_THRESHOLDS = [float(round(x, 2)) for x in np.arange(0.30, 0.95, 0.05)]


def _max_conf_and_pred(proba, classes):
    idx = np.argmax(proba, axis=1)
    return proba[np.arange(len(proba)), idx], classes[idx]


def evaluate_threshold(proba, classes, y_true, threshold: float) -> dict:
    confidences, y_pred = _max_conf_and_pred(proba, classes)
    y_true_arr = y_true.astype(str).to_numpy()
    trusted = confidences >= threshold
    coverage = float(trusted.mean())

    if trusted.sum() == 0:
        return {
            "threshold": threshold, "coverage": 0.0, "accuracy": None, "macro_f1": None,
            "error_rate": None, "escalation_rate": round(1 - coverage, 4), "n_trusted": 0,
        }

    acc = accuracy_score(y_true_arr[trusted], y_pred[trusted])
    _, _, f1, _ = precision_recall_fscore_support(
        y_true_arr[trusted], y_pred[trusted], average="macro", zero_division=0
    )
    return {
        "threshold": threshold,
        "coverage": round(coverage, 4),
        "accuracy": round(float(acc), 4),
        "macro_f1": round(float(f1), 4),
        "error_rate": round(1 - float(acc), 4),
        "escalation_rate": round(1 - coverage, 4),
        "n_trusted": int(trusted.sum()),
    }


def build_threshold_curve(proba, classes, y_true, thresholds: list = CANDIDATE_THRESHOLDS) -> list:
    return [evaluate_threshold(proba, classes, y_true, t) for t in thresholds]


def select_operating_point(curve: list, min_accuracy: float = 0.95) -> dict:
    """
    VALIDATION ONLY: among candidate thresholds whose validation accuracy
    (on the trusted subset) is at least min_accuracy, pick the one with
    the LOWEST threshold — maximizing coverage subject to that bar. If no
    threshold meets the bar, falls back to the highest-accuracy threshold,
    reported plainly as a fallback.
    """
    qualifying = [c for c in curve if c["accuracy"] is not None and c["accuracy"] >= min_accuracy]
    if qualifying:
        chosen = min(qualifying, key=lambda c: c["threshold"])
        selection_note = (
            f"Selected the lowest threshold meeting the validation accuracy bar "
            f"(>= {min_accuracy}) — maximizes automation coverage subject to that bar."
        )
    else:
        scored = [c for c in curve if c["accuracy"] is not None]
        chosen = max(scored, key=lambda c: c["accuracy"])
        selection_note = (
            f"No candidate threshold reached the target validation accuracy bar "
            f"(>= {min_accuracy}) — falling back to the threshold with the highest "
            f"validation accuracy achieved ({chosen['accuracy']})."
        )
    return {
        "min_accuracy_bar": min_accuracy,
        "selected_threshold": chosen["threshold"],
        "selected_from_validation": chosen,
        "selection_note": selection_note,
    }


def run_threshold_analysis(
    proba_val, classes_val, y_val,
    proba_test, classes_test, y_test,
    proba_challenge, classes_challenge, y_challenge,
    min_accuracy: float = 0.95,
) -> dict:
    """Full workflow: build the validation curve, select+freeze one
    operating point from it, then evaluate that frozen threshold on test
    and challenge — never re-selecting based on those results."""
    val_curve = build_threshold_curve(proba_val, classes_val, y_val)
    operating_point = select_operating_point(val_curve, min_accuracy=min_accuracy)
    frozen_threshold = operating_point["selected_threshold"]

    test_at_frozen = evaluate_threshold(proba_test, classes_test, y_test, frozen_threshold)
    challenge_at_frozen = evaluate_threshold(proba_challenge, classes_challenge, y_challenge, frozen_threshold)

    return {
        "validation_curve": val_curve,
        "operating_point_selection": operating_point,
        "frozen_threshold": frozen_threshold,
        "test_at_frozen_threshold": test_at_frozen,
        "challenge_at_frozen_threshold": challenge_at_frozen,
    }


def challenge_threshold_by_type(proba_challenge, classes_challenge, y_challenge, challenge_types, frozen_threshold: float) -> dict:
    """Breaks the frozen operating point's coverage/accuracy down by
    challenge_type — same frozen threshold applied uniformly, not
    re-tuned per category."""
    result = {}
    ctypes = challenge_types.to_numpy()
    for ctype in sorted(set(ctypes)):
        mask = ctypes == ctype
        sub_proba = proba_challenge[mask]
        sub_y = y_challenge[mask]
        result[ctype] = evaluate_threshold(sub_proba, classes_challenge, sub_y, frozen_threshold)
    return result
