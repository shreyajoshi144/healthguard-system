from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import brier_score_loss

from evaluation import frozen_models as fm

try:
    from sklearn.frozen import FrozenEstimator
    _HAS_FROZEN_ESTIMATOR = True
except ImportError:
    # sklearn < 1.6 doesn't have FrozenEstimator; cv="prefit" was the old
    # way to calibrate an already-fitted estimator without refitting it.
    _HAS_FROZEN_ESTIMATOR = False

N_BINS = 10
CONFIDENCE_BUCKETS = [(0.9, 1.01), (0.7, 0.9), (0.5, 0.7), (0.0, 0.5)]


def _max_proba_and_pred(proba, classes):
    idx = np.argmax(proba, axis=1)
    return proba[np.arange(len(proba)), idx], classes[idx]


def multiclass_brier_score(proba, classes, y_true) -> float:
    y_true = y_true.astype(str).to_numpy()
    total = 0.0
    for i, cls in enumerate(classes):
        y_binary = (y_true == cls).astype(float)
        total += brier_score_loss(y_binary, proba[:, i])
    return round(float(total / len(classes)), 4)


def expected_calibration_error(confidences, correct, n_bins: int = N_BINS) -> dict:
    bin_edges = np.linspace(0, 1, n_bins + 1)
    bins = []
    ece = 0.0
    n = len(confidences)
    for lo, hi in zip(bin_edges[:-1], bin_edges[1:]):
        mask = (confidences >= lo) & (confidences < hi if hi < 1.0 else confidences <= hi)
        count = int(mask.sum())
        if count == 0:
            bins.append({"bin_range": [round(float(lo), 2), round(float(hi), 2)], "count": 0,
                         "mean_confidence": None, "accuracy": None})
            continue
        mean_conf = float(confidences[mask].mean())
        acc = float(correct[mask].mean())
        bins.append({"bin_range": [round(float(lo), 2), round(float(hi), 2)], "count": count,
                     "mean_confidence": round(mean_conf, 4), "accuracy": round(acc, 4)})
        ece += (count / n) * abs(mean_conf - acc)
    return {"ece": round(float(ece), 4), "reliability_bins": bins}


def confidence_bucket_accuracy(confidences, correct) -> dict:
    buckets = {}
    for lo, hi in CONFIDENCE_BUCKETS:
        mask = (confidences >= lo) & (confidences < hi)
        count = int(mask.sum())
        label = f"[{lo:.1f}, {min(hi, 1.0):.1f})" if hi < 1.01 else f"[{lo:.1f}, 1.0]"
        buckets[label] = {
            "count": count,
            "accuracy": round(float(correct[mask].mean()), 4) if count > 0 else None,
        }
    return buckets


def evaluate_calibration(proba, classes, y_true) -> dict:
    confidences, y_pred = _max_proba_and_pred(proba, classes)
    correct = (y_pred == y_true.astype(str).to_numpy()).astype(float)
    ece_result = expected_calibration_error(confidences, correct)
    return {
        "n_examples": len(y_true),
        "brier_score": multiclass_brier_score(proba, classes, y_true),
        "ece": ece_result["ece"],
        "reliability_bins": ece_result["reliability_bins"],
        "confidence_bucket_accuracy": confidence_bucket_accuracy(confidences, correct),
        "mean_confidence": round(float(confidences.mean()), 4),
        "accuracy": round(float(correct.mean()), 4),
    }


def _fit_calibrator(model_name, X_val_features, y_val, method):
    """Wraps the frozen model's classifier in a post-hoc calibrator fit on
    validation only. The base estimator is never refit — only the
    calibration mapping on top is fit here, using whichever API this
    sklearn version supports for calibrating an already-fitted model
    (FrozenEstimator on sklearn >= 1.6; cv='prefit' on older versions)."""
    handle = fm.get_handle(model_name)
    if _HAS_FROZEN_ESTIMATOR:
        calibrator = CalibratedClassifierCV(FrozenEstimator(handle._clf), method=method)
    else:
        calibrator = CalibratedClassifierCV(handle._clf, method=method, cv="prefit")
    calibrator.fit(X_val_features, y_val.astype(str))
    return calibrator


def select_calibration_method(model_name, val_texts, val_labels) -> dict:
    """VALIDATION ONLY: compares uncalibrated vs sigmoid vs isotonic by
    validation Brier score. Never touches test or challenge data."""
    X_val = fm.features_for(model_name, val_texts)
    proba_uncal, classes = fm.predict_proba(model_name, val_texts)
    brier_uncal = multiclass_brier_score(proba_uncal, classes, val_labels)

    trials = [{"method": "uncalibrated", "validation_brier_score": brier_uncal}]
    best = {"method": "uncalibrated", "validation_brier_score": brier_uncal, "calibrator": None}

    for method in ("sigmoid", "isotonic"):
        try:
            calibrator = _fit_calibrator(model_name, X_val, val_labels, method)
            proba_cal = calibrator.predict_proba(X_val)
            cal_classes = calibrator.classes_
            brier_cal = multiclass_brier_score(proba_cal, cal_classes, val_labels)
            trials.append({"method": method, "validation_brier_score": brier_cal})
            if brier_cal < best["validation_brier_score"]:
                best = {"method": method, "validation_brier_score": brier_cal, "calibrator": calibrator}
        except Exception as exc:
            trials.append({"method": method, "validation_brier_score": None, "error": str(exc)})

    return {
        "trials": trials,
        "selected_method": best["method"],
        "selected_validation_brier_score": best["validation_brier_score"],
        "calibrator": best["calibrator"],
    }


def apply_selected_calibration(model_name, selection, texts):
    if selection["calibrator"] is None:
        return fm.predict_proba(model_name, texts)
    X = fm.features_for(model_name, texts)
    calibrator = selection["calibrator"]
    return calibrator.predict_proba(X), calibrator.classes_


def run_calibration_analysis(
    model_name,
    val_texts, val_labels,
    test_texts, test_labels,
    challenge_texts, challenge_labels,
) -> dict:
    """Full calibration workflow: select method on validation only, then
    evaluate the frozen selected approach on val/test/challenge."""
    selection = select_calibration_method(model_name, val_texts, val_labels)

    proba_val, classes_val = apply_selected_calibration(model_name, selection, val_texts)
    proba_test, classes_test = apply_selected_calibration(model_name, selection, test_texts)
    proba_challenge, classes_challenge = apply_selected_calibration(model_name, selection, challenge_texts)

    proba_test_uncal, classes_uncal = fm.predict_proba(model_name, test_texts)
    proba_challenge_uncal, _ = fm.predict_proba(model_name, challenge_texts)

    if selection["selected_method"] == "uncalibrated":
        note = "No calibration was applied — raw model probabilities were already best on validation Brier score."
    else:
        note = f"'{selection['selected_method']}' calibration was selected (lowest validation Brier score) and applied below."

    return {
        "model": model_name,
        "method_selection": {
            "trials": selection["trials"],
            "selected_method": selection["selected_method"],
            "selected_validation_brier_score": selection["selected_validation_brier_score"],
            "note": note,
        },
        "validation": evaluate_calibration(proba_val, classes_val, val_labels),
        "test": {
            "selected_approach": evaluate_calibration(proba_test, classes_test, test_labels),
            "uncalibrated_baseline": evaluate_calibration(proba_test_uncal, classes_uncal, test_labels),
        },
        "challenge": {
            "selected_approach": evaluate_calibration(proba_challenge, classes_challenge, challenge_labels),
            "uncalibrated_baseline": evaluate_calibration(proba_challenge_uncal, classes_uncal, challenge_labels),
        },
        "_calibrator_object": selection["calibrator"],
    }
