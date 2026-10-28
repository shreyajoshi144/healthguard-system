
from __future__ import annotations
import json
import hashlib
import platform
from pathlib import Path
from datetime import datetime, timezone

import sklearn
import pandas as pd
import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support,
    confusion_matrix, classification_report,
)

from evaluation import splits as splits_mod
from evaluation import challenge_set as challenge_mod
from evaluation import leakage_checks
from evaluation.data_contract import decode_secondary_issues
from utils.config import (
    MODELS_DIR, RANDOM_SEED, EVAL_RESULTS_DIR,
    SAMPLE_DATASET_VERSION, CHALLENGE_DATASET_VERSION,
)
from utils.logger import get_logger
from modules import ml_issue_classifier as production_classifier  # read-only: path/identity comparison only, never trained or mutated

logger = get_logger(__name__)
VECTORIZER_PARAMS = dict(ngram_range=(1, 2), min_df=2, max_df=0.9, sublinear_tf=True)
CLASSIFIER_PARAMS = dict(max_iter=1000, class_weight="balanced")

MODEL_IDENTIFIER = "benchmark_tfidf_logistic_regression_issue_classifier_v1"

BENCHMARK_MODEL_DIR = MODELS_DIR / "benchmark_issue_classifier"
BENCHMARK_VECTORIZER_PATH = BENCHMARK_MODEL_DIR / "tfidf_vectorizer.joblib"
BENCHMARK_MODEL_PATH = BENCHMARK_MODEL_DIR / "logreg_issue_classifier.joblib"
BENCHMARK_METRICS_PATH = BENCHMARK_MODEL_DIR / "metrics.json"

LATEST_BENCHMARK_REPORT_PATH = EVAL_RESULTS_DIR / "latest_benchmark_evaluation.json"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _evaluate(vectorizer, clf, texts: pd.Series, labels: pd.Series, split_name: str) -> dict:
    """Same metric set as modules/ml_issue_classifier.py's internal
    _evaluate() / evaluation/evaluate.py's _compute_classification_metrics
    — kept consistent so benchmark and production numbers are directly
    comparable. transform() only here, never fit() — this function must
    never be called with the vectorizer/classifier still unfit, and must
    never call .fit()/.fit_transform() itself."""
    X_vec = vectorizer.transform(texts.fillna("").astype(str))
    y_pred = clf.predict(X_vec)
    y_true = labels.astype(str)

    acc = accuracy_score(y_true, y_pred)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_true, y_pred, average="macro", zero_division=0
    )
    labels_sorted = sorted(set(y_true) | set(y_pred))
    cm = confusion_matrix(y_true, y_pred, labels=labels_sorted)
    report = classification_report(y_true, y_pred, zero_division=0, output_dict=True)

    logger.info(
        "[benchmark:%s] accuracy=%.3f macro_precision=%.3f macro_recall=%.3f macro_f1=%.3f",
        split_name, acc, precision, recall, f1,
    )

    return {
        "n_examples": len(y_true),
        "accuracy": round(float(acc), 4),
        "macro_precision": round(float(precision), 4),
        "macro_recall": round(float(recall), 4),
        "macro_f1": round(float(f1), 4),
        "confusion_matrix": cm.tolist(),
        "confusion_matrix_labels": labels_sorted,
        "per_class_report": report,
    }


def fit_benchmark_from_frames(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    test_df: pd.DataFrame,
    challenge_df: pd.DataFrame,
    random_state: int = RANDOM_SEED,
) -> tuple[TfidfVectorizer, LogisticRegression, dict]:
    """
    Core, dependency-injected training+evaluation logic (no file I/O), so
    it's directly unit-testable with small in-memory frames — see
    tests/test_benchmark_model.py, which uses this to prove val/test rows
    are never seen by vectorizer.fit_transform()/clf.fit().

    train_df/val_df/test_df must have columns: feedback_text, true_issue_category
    challenge_df must have columns: feedback_text, primary_issue

    Returns (fitted_vectorizer, fitted_classifier, metrics_dict). Does not
    write anything to disk — that's train_benchmark()'s job.
    """
    train_text = train_df["feedback_text"].astype(str)
    train_labels = train_df["true_issue_category"].astype(str)

    non_empty = train_text.str.strip().ne("")
    if (~non_empty).any():
        logger.warning("Dropping %d train rows with empty feedback_text", (~non_empty).sum())
    train_text, train_labels = train_text[non_empty], train_labels[non_empty]

    vectorizer = TfidfVectorizer(**VECTORIZER_PARAMS)
    # fit_transform is called EXACTLY ONCE, on train_text only. val/test/
    # challenge are only ever .transform()'d, inside _evaluate(), never fit.
    X_train_vec = vectorizer.fit_transform(train_text)

    clf = LogisticRegression(random_state=random_state, **CLASSIFIER_PARAMS)
    clf.fit(X_train_vec, train_labels)

    val_metrics = _evaluate(vectorizer, clf, val_df["feedback_text"], val_df["true_issue_category"], "validation")
    test_metrics = _evaluate(vectorizer, clf, test_df["feedback_text"], test_df["true_issue_category"], "test")
    challenge_metrics = _evaluate(vectorizer, clf, challenge_df["feedback_text"], challenge_df["primary_issue"], "challenge")

    metrics = {
        "model_identifier": MODEL_IDENTIFIER,
        "model": "tfidf_logistic_regression_issue_classifier",
        "artifact_role": "benchmark",
        "trained_on": "data/splits/train.csv ONLY",
        "n_train": len(train_text),
        "classes": sorted(train_labels.unique().tolist()),
        "validation": val_metrics,
        "test": test_metrics,
        "challenge": challenge_metrics,
    }
    return vectorizer, clf, metrics


def train_benchmark(random_state: int = RANDOM_SEED, force_rebuild_splits: bool = False) -> dict:
    """
    I/O wrapper: loads data/splits/{train,val,test}.csv and
    data/challenge_set.csv from disk (building the splits first if they
    don't exist yet — never regenerating the challenge set, per scope),
    trains the benchmark artifact using ONLY train.csv, evaluates on
    val/test/challenge, persists the artifact under
    models/benchmark_issue_classifier/ (completely separate from
    models/issue_classifier/), and writes a full reproducibility report.
    """
    manifest = splits_mod.build_splits(force=force_rebuild_splits)
    split_data = splits_mod.load_splits()

    challenge_path = challenge_mod.CHALLENGE_SET_PATH
    if not challenge_path.exists():
        raise FileNotFoundError(
            f"Challenge set not found at {challenge_path}. Per this correction's scope, "
            "the challenge set must already exist (not regenerated here) — run "
            "`python -m evaluation.challenge_set` if this is a fresh checkout."
        )
    # keep_default_na=False: one challenge-set example's feedback_text is the
    # literal string "N/A" (a deliberate low_information case), which pandas
    # would otherwise read as NaN.
    challenge_df = pd.read_csv(challenge_path, keep_default_na=False, na_values=[])

    # Confirm the four sets are still mutually disjoint before trusting
    # anything trained/evaluated below.
    leakage_checks.assert_no_split_leakage({
        "train": split_data["train"]["feedback_text"],
        "val": split_data["val"]["feedback_text"],
        "test": split_data["test"]["feedback_text"],
        "challenge": challenge_df["feedback_text"],
    })

    vectorizer, clf, core_metrics = fit_benchmark_from_frames(
        train_df=split_data["train"],
        val_df=split_data["val"],
        test_df=split_data["test"],
        challenge_df=challenge_df,
        random_state=random_state,
    )

    BENCHMARK_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(vectorizer, BENCHMARK_VECTORIZER_PATH)
    joblib.dump(clf, BENCHMARK_MODEL_PATH)

    report = {
        "phase": "3A-correction",
        "report_type": "benchmark_baseline_evaluation",
        "reproducibility": {
            "model_identifier": MODEL_IDENTIFIER,
            "evaluation_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "random_seed": random_state,
            "python_version": platform.python_version(),
            "scikit_learn_version": sklearn.__version__,
            "train_dataset_version": SAMPLE_DATASET_VERSION,
            "val_dataset_version": SAMPLE_DATASET_VERSION,
            "test_dataset_version": SAMPLE_DATASET_VERSION,
            "challenge_dataset_version": CHALLENGE_DATASET_VERSION,
            "feature_configuration": {
                "vectorizer": "TfidfVectorizer",
                "vectorizer_params": VECTORIZER_PARAMS,
                "classifier": "LogisticRegression",
                "classifier_params": {**CLASSIFIER_PARAMS, "random_state": random_state},
            },
            "artifact_paths": {
                "vectorizer": str(BENCHMARK_VECTORIZER_PATH),
                "model": str(BENCHMARK_MODEL_PATH),
            },
            "artifact_sha256": {
                "vectorizer": _file_sha256(BENCHMARK_VECTORIZER_PATH),
                "model": _file_sha256(BENCHMARK_MODEL_PATH),
            },
            "production_artifact_paths_for_comparison": {
                "vectorizer": str(production_classifier.VECTORIZER_PATH),
                "model": str(production_classifier.MODEL_PATH),
            },
            "trained_only_on": "data/splits/train.csv",
            "split_sizes": {
                "train": core_metrics["n_train"],
                "validation": core_metrics["validation"]["n_examples"],
                "test": core_metrics["test"]["n_examples"],
                "challenge": core_metrics["challenge"]["n_examples"],
            },
            "split_manifest_reference": {
                "path": str(splits_mod.MANIFEST_PATH),
                "built_at": manifest.get("built_at"),
            },
        },
        "results": {
            "validation": core_metrics["validation"],
            "test": core_metrics["test"],
            "challenge": core_metrics["challenge"],
        },
        "notes": (
            "This is a SEPARATE benchmark artifact trained only on "
            "data/splits/train.csv, distinct from the production model "
            "under models/issue_classifier/. Its purpose is to provide "
            "provably-unseen val/test/challenge evaluation numbers; it is "
            "not used anywhere in app.py, backend/, or the live "
            "categorize_issues() pipeline. Same synthetic-data caveats "
            "apply as documented in PHASE1_ML_UPGRADE.md and "
            "PHASE3A_EVALUATION.md — high validation/test scores reflect "
            "the training dataset's constructed vocabulary separation, not "
            "real-world accuracy; the challenge-set numbers are the more "
            "meaningful generalization signal."
        ),
    }

    with open(BENCHMARK_METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp_slug = report["reproducibility"]["evaluation_timestamp_utc"].replace(":", "-")
    timestamped_path = EVAL_RESULTS_DIR / f"benchmark_evaluation_{timestamp_slug}.json"
    with open(timestamped_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(LATEST_BENCHMARK_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    logger.info(
        "Benchmark baseline trained on train.csv only (n=%d) and persisted to %s",
        core_metrics["n_train"], BENCHMARK_MODEL_DIR,
    )
    return report


if __name__ == "__main__":
    report = train_benchmark()
    print(json.dumps({
        "model_identifier": report["reproducibility"]["model_identifier"],
        "split_sizes": report["reproducibility"]["split_sizes"],
        "validation": {k: v for k, v in report["results"]["validation"].items()
                        if k in ("accuracy", "macro_f1", "n_examples")},
        "test": {k: v for k, v in report["results"]["test"].items()
                  if k in ("accuracy", "macro_f1", "n_examples")},
        "challenge": {k: v for k, v in report["results"]["challenge"].items()
                       if k in ("accuracy", "macro_f1", "n_examples")},
    }, indent=2))
