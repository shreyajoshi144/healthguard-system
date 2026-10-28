from __future__ import annotations
import json
import hashlib
import platform
from pathlib import Path
from datetime import datetime, timezone

import sklearn
import joblib
import pandas as pd

from evaluation import splits as splits_mod
from evaluation import challenge_set as challenge_mod
from evaluation import leakage_checks
from evaluation import frozen_models as fm
from evaluation import calibration as cal_mod
from evaluation import uncertainty as unc_mod
from evaluation import ood_detection as ood_mod
from evaluation import threshold_analysis as thresh_mod
from evaluation import disagreement as dis_mod
from evaluation import benchmark_model as benchmark_mod
from evaluation import semantic_model as semantic_mod
from utils.config import MODELS_DIR, EVAL_RESULTS_DIR, RANDOM_SEED, SAMPLE_DATASET_VERSION, CHALLENGE_DATASET_VERSION
from utils.logger import get_logger

logger = get_logger(__name__)

UNCERTAINTY_MODELS_DIR = MODELS_DIR / "uncertainty"
LATEST_RELIABILITY_REPORT_PATH = EVAL_RESULTS_DIR / "latest_reliability_report.json"

THRESHOLD_MIN_ACCURACY_BAR = 0.95  # chosen a priori, not tuned against test/challenge
OOD_TARGET_FLAG_RATE = ood_mod.OOD_VALIDATION_FLAG_RATE_TARGET


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_data():
    split_data = splits_mod.load_splits()
    challenge_df = pd.read_csv(challenge_mod.CHALLENGE_SET_PATH, keep_default_na=False, na_values=[])
    return split_data["train"], split_data["val"], split_data["test"], challenge_df


def _verify_no_leakage(train_df, val_df, test_df, challenge_df):
    leakage_checks.assert_no_split_leakage({
        "train": train_df["feedback_text"],
        "val": val_df["feedback_text"],
        "test": test_df["feedback_text"],
        "challenge": challenge_df["feedback_text"],
    })


def _run_calibration_for_model(model_name, val_df, test_df, challenge_df) -> dict:
    result = cal_mod.run_calibration_analysis(
        model_name,
        val_df["feedback_text"], val_df["true_issue_category"],
        test_df["feedback_text"], test_df["true_issue_category"],
        challenge_df["feedback_text"], challenge_df["primary_issue"],
    )
    calibrator = result.pop("_calibrator_object")
    artifact_info = {"artifact_path": None, "artifact_sha256": None}
    if calibrator is not None:
        UNCERTAINTY_MODELS_DIR.mkdir(parents=True, exist_ok=True)
        path = UNCERTAINTY_MODELS_DIR / f"calibrator_{model_name}.joblib"
        joblib.dump(calibrator, path)
        artifact_info = {"artifact_path": str(path), "artifact_sha256": _file_sha256(path)}
    result["artifact"] = artifact_info
    return result


def _run_uncertainty_for_model(model_name, val_df, test_df, challenge_df) -> dict:
    proba_val, classes = fm.predict_proba(model_name, val_df["feedback_text"])
    thresholds = unc_mod.derive_uncertainty_thresholds(proba_val)

    def _records_and_summary(proba, classes, y_true):
        records = unc_mod.build_prediction_records(proba, classes, thresholds)
        return {
            "summary": unc_mod.summarize_uncertainty(records),
            "accuracy_by_uncertainty_level": unc_mod.accuracy_by_uncertainty_level(records, y_true),
        }

    proba_test, classes_test = fm.predict_proba(model_name, test_df["feedback_text"])
    proba_challenge, classes_challenge = fm.predict_proba(model_name, challenge_df["feedback_text"])

    challenge_records = unc_mod.build_prediction_records(proba_challenge, classes_challenge, thresholds)
    challenge_by_type = {}
    for ctype in sorted(challenge_df["challenge_type"].unique()):
        mask = (challenge_df["challenge_type"] == ctype).to_numpy()
        sub_records = [r for r, m in zip(challenge_records, mask) if m]
        sub_y = challenge_df.loc[mask, "primary_issue"]
        challenge_by_type[ctype] = {
            "summary": unc_mod.summarize_uncertainty(sub_records),
            "accuracy_by_uncertainty_level": unc_mod.accuracy_by_uncertainty_level(sub_records, sub_y),
        }

    return {
        "thresholds": thresholds,
        "validation": _records_and_summary(proba_val, classes, val_df["true_issue_category"]),
        "test": _records_and_summary(proba_test, classes_test, test_df["true_issue_category"]),
        "challenge": _records_and_summary(proba_challenge, classes_challenge, challenge_df["primary_issue"]),
        "challenge_by_type": challenge_by_type,
    }


def _run_ood_for_model(model_name, train_df, val_df, test_df, challenge_df) -> dict:
    detector = ood_mod.build_ood_detector(
        model_name, train_df["feedback_text"], train_df["true_issue_category"], val_df["feedback_text"]
    )

    # Persist centroids + threshold as a small, explainable JSON artifact
    # (not joblib — these are just numeric vectors, kept inspectable).
    UNCERTAINTY_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path = UNCERTAINTY_MODELS_DIR / f"ood_detector_{model_name}.json"
    serializable_centroids = {
        cls: (vec.tolist() if hasattr(vec, "tolist") else list(vec))
        for cls, vec in detector["centroids"].items()
    }
    artifact_content = {
        "model": model_name,
        "method": detector["method"],
        "n_classes": detector["n_classes"],
        "threshold_info": detector["threshold_info"],
        "centroids": serializable_centroids,
    }
    with open(artifact_path, "w", encoding="utf-8") as f:
        json.dump(artifact_content, f)
    artifact_sha256 = _file_sha256(artifact_path)

    test_result = ood_mod.evaluate_ood_detector(detector, test_df["feedback_text"])
    challenge_result = ood_mod.evaluate_ood_detector(
        detector, challenge_df["feedback_text"], challenge_df["challenge_type"]
    )

    return {
        "method": detector["method"],
        "threshold_info": detector["threshold_info"],
        "artifact": {"path": str(artifact_path), "sha256": artifact_sha256},
        "test": test_result,
        "challenge": challenge_result,
    }


def _run_threshold_for_model(model_name, val_df, test_df, challenge_df) -> dict:
    proba_val, classes_val = fm.predict_proba(model_name, val_df["feedback_text"])
    proba_test, classes_test = fm.predict_proba(model_name, test_df["feedback_text"])
    proba_challenge, classes_challenge = fm.predict_proba(model_name, challenge_df["feedback_text"])

    result = thresh_mod.run_threshold_analysis(
        proba_val, classes_val, val_df["true_issue_category"],
        proba_test, classes_test, test_df["true_issue_category"],
        proba_challenge, classes_challenge, challenge_df["primary_issue"],
        min_accuracy=THRESHOLD_MIN_ACCURACY_BAR,
    )
    result["challenge_by_type_at_frozen_threshold"] = thresh_mod.challenge_threshold_by_type(
        proba_challenge, classes_challenge, challenge_df["primary_issue"],
        challenge_df["challenge_type"], result["frozen_threshold"],
    )
    return result


def _run_disagreement(val_df, test_df, challenge_df) -> dict:
    val_result = dis_mod.compare_predictions(val_df["feedback_text"], val_df["true_issue_category"])
    test_result = dis_mod.compare_predictions(test_df["feedback_text"], test_df["true_issue_category"])
    challenge_result = dis_mod.compare_predictions(challenge_df["feedback_text"], challenge_df["primary_issue"])
    challenge_by_type = dis_mod.compare_by_challenge_type(
        challenge_df["feedback_text"], challenge_df["primary_issue"], challenge_df["challenge_type"]
    )
    return {
        "validation": val_result,
        "test": test_result,
        "challenge": challenge_result,
        "challenge_by_type": challenge_by_type,
    }


def _frozen_artifact_hashes() -> dict:
    return {
        "production": {
            "vectorizer_sha256": None,  # production model not touched by Phase 3C; included for completeness only if present
        },
        "benchmark": {
            "vectorizer_sha256": _file_sha256(benchmark_mod.BENCHMARK_VECTORIZER_PATH),
            "model_sha256": _file_sha256(benchmark_mod.BENCHMARK_MODEL_PATH),
        },
        "semantic": {
            "classifier_sha256": _file_sha256(semantic_mod.SEMANTIC_CLASSIFIER_PATH),
        },
    }


def run_reliability_analysis() -> dict:
    train_df, val_df, test_df, challenge_df = _load_data()
    _verify_no_leakage(train_df, val_df, test_df, challenge_df)

    logger.info("Phase 3C: running calibration analysis for both frozen models (validation-only selection)...")
    calibration_results = {
        model_name: _run_calibration_for_model(model_name, val_df, test_df, challenge_df)
        for model_name in fm.MODEL_NAMES
    }

    logger.info("Phase 3C: computing per-prediction uncertainty signals...")
    uncertainty_results = {
        model_name: _run_uncertainty_for_model(model_name, val_df, test_df, challenge_df)
        for model_name in fm.MODEL_NAMES
    }

    logger.info("Phase 3C: building OOD detectors (train centroids, validation threshold)...")
    ood_results = {
        model_name: _run_ood_for_model(model_name, train_df, val_df, test_df, challenge_df)
        for model_name in fm.MODEL_NAMES
    }

    logger.info("Phase 3C: running threshold/coverage analysis (validation-selected, frozen)...")
    threshold_results = {
        model_name: _run_threshold_for_model(model_name, val_df, test_df, challenge_df)
        for model_name in fm.MODEL_NAMES
    }

    logger.info("Phase 3C: comparing frozen benchmark vs semantic predictions...")
    disagreement_results = _run_disagreement(val_df, test_df, challenge_df)

    report = {
        "phase": "3C",
        "report_type": "uncertainty_calibration_ood_reliability",
        "reproducibility": {
            "evaluation_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "random_seed": RANDOM_SEED,
            "python_version": platform.python_version(),
            "scikit_learn_version": sklearn.__version__,
            "dataset_versions": {
                "train_val_test": SAMPLE_DATASET_VERSION,
                "challenge": CHALLENGE_DATASET_VERSION,
            },
            "split_sizes": {
                "train": len(train_df), "validation": len(val_df),
                "test": len(test_df), "challenge": len(challenge_df),
            },
            "model_identifiers": {
                "benchmark": benchmark_mod.MODEL_IDENTIFIER,
                "semantic": semantic_mod.MODEL_IDENTIFIER,
            },
            "methods": {
                "calibration": "CalibratedClassifierCV (sigmoid/isotonic) fit on validation only, vs uncalibrated baseline; selected by lowest validation Brier score",
                "uncertainty_signals": "max-class confidence, top1-top2 margin, Shannon entropy; Low/Medium/High buckets from validation confidence 33rd/66th percentiles",
                "ood_detection": "nearest-training-class-centroid cosine distance in each model's own feature space; threshold = validation-score percentile targeting a "
                                  f"{OOD_TARGET_FLAG_RATE:.0%} in-domain flag rate",
                "threshold_selection": f"lowest validation threshold achieving >= {THRESHOLD_MIN_ACCURACY_BAR:.0%} validation accuracy on the trusted subset, frozen before test/challenge evaluation",
                "disagreement": "argmax prediction comparison between the two frozen models, accuracy conditioned on agreement/disagreement",
            },
            "frozen_artifact_hashes": _frozen_artifact_hashes(),
            "new_artifact_locations": {
                "calibrators_and_ood_detectors": str(UNCERTAINTY_MODELS_DIR),
                "reports": str(EVAL_RESULTS_DIR),
            },
        },
        "calibration": calibration_results,
        "uncertainty": uncertainty_results,
        "ood_detection": ood_results,
        "threshold_coverage_analysis": threshold_results,
        "model_disagreement": disagreement_results,
    }

    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp_slug = report["reproducibility"]["evaluation_timestamp_utc"].replace(":", "-")
    timestamped_path = EVAL_RESULTS_DIR / f"reliability_report_{timestamp_slug}.json"
    with open(timestamped_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    with open(LATEST_RELIABILITY_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("Phase 3C reliability report written to %s and %s", timestamped_path, LATEST_RELIABILITY_REPORT_PATH)
    return report


if __name__ == "__main__":
    report = run_reliability_analysis()
    print(json.dumps({
        "calibration_selected_methods": {
            m: report["calibration"][m]["method_selection"]["selected_method"] for m in fm.MODEL_NAMES
        },
        "ood_challenge_flag_rates": {
            m: report["ood_detection"][m]["challenge"]["flag_rate"] for m in fm.MODEL_NAMES
        },
        "frozen_thresholds": {
            m: report["threshold_coverage_analysis"][m]["frozen_threshold"] for m in fm.MODEL_NAMES
        },
        "challenge_coverage_at_frozen_threshold": {
            m: report["threshold_coverage_analysis"][m]["challenge_at_frozen_threshold"]["coverage"] for m in fm.MODEL_NAMES
        },
        "model_agreement_rate_challenge": report["model_disagreement"]["challenge"]["agreement_rate"],
    }, indent=2, default=str))
