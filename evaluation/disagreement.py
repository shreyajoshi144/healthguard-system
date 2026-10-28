from __future__ import annotations
import numpy as np
import pandas as pd

from evaluation import frozen_models as fm


def get_predictions(model_name, texts: pd.Series):
    proba, classes = fm.predict_proba(model_name, texts)
    idx = np.argmax(proba, axis=1)
    return classes[idx], proba[np.arange(len(proba)), idx]


def compare_predictions(texts: pd.Series, y_true: pd.Series) -> dict:
    """Full disagreement analysis for one dataset split (val/test/challenge)."""
    bench_pred, bench_conf = get_predictions("benchmark", texts)
    sem_pred, sem_conf = get_predictions("semantic", texts)
    y_true_arr = y_true.astype(str).to_numpy()

    agree = bench_pred == sem_pred
    agreement_rate = float(agree.mean())

    bench_correct = bench_pred == y_true_arr
    sem_correct = sem_pred == y_true_arr

    result = {
        "n_examples": len(texts),
        "agreement_rate": round(agreement_rate, 4),
        "disagreement_rate": round(1 - agreement_rate, 4),
        "n_agree": int(agree.sum()),
        "n_disagree": int((~agree).sum()),
    }

    if agree.sum() > 0:
        result["accuracy_when_agreeing"] = round(float(bench_correct[agree].mean()), 4)
        result["error_rate_when_agreeing"] = round(1 - float(bench_correct[agree].mean()), 4)
    else:
        result["accuracy_when_agreeing"] = None
        result["error_rate_when_agreeing"] = None

    if (~agree).sum() > 0:
        result["benchmark_accuracy_when_disagreeing"] = round(float(bench_correct[~agree].mean()), 4)
        result["semantic_accuracy_when_disagreeing"] = round(float(sem_correct[~agree].mean()), 4)
        result["benchmark_error_rate_when_disagreeing"] = round(1 - float(bench_correct[~agree].mean()), 4)
        result["semantic_error_rate_when_disagreeing"] = round(1 - float(sem_correct[~agree].mean()), 4)
    else:
        result["benchmark_accuracy_when_disagreeing"] = None
        result["semantic_accuracy_when_disagreeing"] = None
        result["benchmark_error_rate_when_disagreeing"] = None
        result["semantic_error_rate_when_disagreeing"] = None

    if result["error_rate_when_agreeing"] is not None and result["benchmark_accuracy_when_disagreeing"] is not None:
        avg_disagree_error = (
            result["benchmark_error_rate_when_disagreeing"] + result["semantic_error_rate_when_disagreeing"]
        ) / 2
        result["disagreement_signal_useful"] = bool(avg_disagree_error > result["error_rate_when_agreeing"])
        result["error_rate_gap_disagree_minus_agree"] = round(avg_disagree_error - result["error_rate_when_agreeing"], 4)
    else:
        result["disagreement_signal_useful"] = None
        result["error_rate_gap_disagree_minus_agree"] = None

    return result


def compare_by_challenge_type(texts: pd.Series, y_true: pd.Series, challenge_types: pd.Series) -> dict:
    result = {}
    ctypes = challenge_types.to_numpy()
    for ctype in sorted(set(ctypes)):
        mask = ctypes == ctype
        result[ctype] = compare_predictions(texts[mask], y_true[mask])
    return result
