from __future__ import annotations
import json
import hashlib
import platform
import time
from pathlib import Path
from datetime import datetime, timezone

import sklearn
import numpy as np
import pandas as pd
import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support,
    confusion_matrix, classification_report,
)

from evaluation import splits as splits_mod
from evaluation import challenge_set as challenge_mod
from evaluation import leakage_checks
from evaluation import embeddings as embeddings_mod
from utils.config import (
    MODELS_DIR, RANDOM_SEED, EVAL_RESULTS_DIR,
    SAMPLE_DATASET_VERSION, CHALLENGE_DATASET_VERSION,
)
from utils.logger import get_logger
from modules import ml_issue_classifier as production_classifier  # read-only path comparison
from evaluation import benchmark_model as benchmark_mod  # read-only path comparison

logger = get_logger(__name__)

# Same fixed choices as evaluation/benchmark_model.py, deliberately kept
# identical so the embedding representation is the only real variable.
# C is the one hyperparameter actually searched (see select_classifier_C).
CLASSIFIER_FIXED_PARAMS = dict(max_iter=1000, class_weight="balanced")
C_GRID = [0.1, 1.0, 10.0]

MODEL_IDENTIFIER = "semantic_spacy_md_logistic_regression_issue_classifier_v1"

SEMANTIC_MODEL_DIR = MODELS_DIR / "semantic_issue_classifier"
SEMANTIC_CLASSIFIER_PATH = SEMANTIC_MODEL_DIR / "logreg_classifier.joblib"
SEMANTIC_METRICS_PATH = SEMANTIC_MODEL_DIR / "metrics.json"

LATEST_SEMANTIC_REPORT_PATH = EVAL_RESULTS_DIR / "latest_semantic_evaluation.json"

CHALLENGE_TYPES = [
    "paraphrase", "ambiguous", "multi_issue", "low_information",
    "unfamiliar_wording", "out_of_domain", "edge_case",
]


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _predict_with_confidence(clf: LogisticRegression, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    proba = clf.predict_proba(X)
    idx = np.argmax(proba, axis=1)
    labels = clf.classes_[idx]
    confidences = proba[np.arange(len(proba)), idx]
    return labels, confidences


def _evaluate(clf: LogisticRegression, X: np.ndarray, labels: pd.Series, split_name: str) -> dict:
    """Same metric set as evaluation/benchmark_model.py's _evaluate(), plus
    mean_confidence — kept consistent so the two reports are directly
    comparable. Only ever calls .predict()/.predict_proba(), never .fit()."""
    y_pred, confidences = _predict_with_confidence(clf, X)
    y_true = labels.astype(str)

    acc = accuracy_score(y_true, y_pred)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_true, y_pred, average="macro", zero_division=0
    )
    labels_sorted = sorted(set(y_true) | set(y_pred))
    cm = confusion_matrix(y_true, y_pred, labels=labels_sorted)
    report = classification_report(y_true, y_pred, zero_division=0, output_dict=True)

    logger.info(
        "[semantic:%s] accuracy=%.3f macro_precision=%.3f macro_recall=%.3f macro_f1=%.3f mean_conf=%.3f",
        split_name, acc, precision, recall, f1, confidences.mean(),
    )

    return {
        "n_examples": len(y_true),
        "accuracy": round(float(acc), 4),
        "macro_precision": round(float(precision), 4),
        "macro_recall": round(float(recall), 4),
        "macro_f1": round(float(f1), 4),
        "mean_confidence": round(float(confidences.mean()), 4),
        "confusion_matrix": cm.tolist(),
        "confusion_matrix_labels": labels_sorted,
        "per_class_report": report,
    }


def _evaluate_by_challenge_type(clf: LogisticRegression, challenge_df: pd.DataFrame, X_challenge: np.ndarray) -> dict:
    breakdown = {}
    for ctype in CHALLENGE_TYPES:
        mask = (challenge_df["challenge_type"] == ctype).to_numpy()
        if not mask.any():
            continue
        y_pred, confidences = _predict_with_confidence(clf, X_challenge[mask])
        y_true = challenge_df.loc[mask, "primary_issue"].astype(str)
        acc = accuracy_score(y_true, y_pred)
        breakdown[ctype] = {
            "n_examples": int(mask.sum()),
            "accuracy": round(float(acc), 4),
            "mean_confidence": round(float(confidences.mean()), 4),
        }
    return breakdown


def select_classifier_C(
    X_train: np.ndarray, y_train: pd.Series,
    X_val: np.ndarray, y_val: pd.Series,
    c_grid: list[float] = C_GRID,
    random_state: int = RANDOM_SEED,
) -> dict:
    """
    Model selection using VALIDATION ONLY: fit one classifier per C value
    on train, score each on validation macro F1, return the winner and the
    full trial log. Test and challenge are never touched here.
    """
    trials = []
    best = None
    for C in c_grid:
        clf = LogisticRegression(C=C, random_state=random_state, **CLASSIFIER_FIXED_PARAMS)
        clf.fit(X_train, y_train)
        y_val_pred = clf.predict(X_val)
        val_macro_f1 = precision_recall_fscore_support(
            y_val.astype(str), y_val_pred, average="macro", zero_division=0
        )[2]
        trial = {"C": C, "validation_macro_f1": round(float(val_macro_f1), 4)}
        trials.append(trial)
        if best is None or val_macro_f1 > best["validation_macro_f1"]:
            best = {**trial, "classifier": clf}

    logger.info(
        "Model selection on validation: tried C=%s, selected C=%s (val macro_f1=%.4f)",
        c_grid, best["C"], best["validation_macro_f1"],
    )
    return {
        "trials": trials,
        "selected_C": best["C"],
        "selected_validation_macro_f1": best["validation_macro_f1"],
        "selected_classifier": best["classifier"],
    }


def fit_semantic_from_frames(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    test_df: pd.DataFrame,
    challenge_df: pd.DataFrame,
    random_state: int = RANDOM_SEED,
) -> tuple[LogisticRegression, dict]:
    """
    Core, dependency-injected training+evaluation logic (no file I/O) —
    directly unit-testable with small in-memory frames, mirroring
    evaluation/benchmark_model.py's fit_benchmark_from_frames().

    Embeds all four sets with the frozen pretrained embedder, runs
    validation-only model selection over train+val embeddings, evaluates
    the selected classifier on test and challenge, and measures latency.

    Returns (fitted_classifier, metrics_dict). Writes nothing to disk.
    """
    train_text = train_df["feedback_text"].astype(str)
    train_labels = train_df["true_issue_category"].astype(str)
    non_empty = train_text.str.strip().ne("")
    if (~non_empty).any():
        logger.warning("Dropping %d train rows with empty feedback_text", (~non_empty).sum())
    train_text, train_labels = train_text[non_empty], train_labels[non_empty]

    t0 = time.perf_counter()
    X_train = embeddings_mod.embed_texts(train_text)
    embed_train_seconds = time.perf_counter() - t0

    X_val = embeddings_mod.embed_texts(val_df["feedback_text"])
    X_test = embeddings_mod.embed_texts(test_df["feedback_text"])
    X_challenge = embeddings_mod.embed_texts(challenge_df["feedback_text"])

    # ── Model selection: VALIDATION ONLY ──────────────────────────────
    selection = select_classifier_C(
        X_train, train_labels, X_val, val_df["true_issue_category"],
        random_state=random_state,
    )
    clf = selection.pop("selected_classifier")

    # ── Final evaluation: TEST once, CHALLENGE once, never used to tune ─
    val_metrics = _evaluate(clf, X_val, val_df["true_issue_category"], "validation")
    test_metrics = _evaluate(clf, X_test, test_df["true_issue_category"], "test")
    challenge_metrics = _evaluate(clf, X_challenge, challenge_df["primary_issue"], "challenge")
    challenge_by_type = _evaluate_by_challenge_type(clf, challenge_df, X_challenge)

    latency = embeddings_mod.measure_embedding_latency(test_df["feedback_text"])
    # Full pipeline (embed + classify) latency on the test set, batch mode:
    t0 = time.perf_counter()
    _ = clf.predict(X_test)
    classify_only_seconds = time.perf_counter() - t0
    latency["classifier_predict_only_batch_ms"] = round(classify_only_seconds * 1000, 4)
    latency["embed_train_set_total_seconds"] = round(embed_train_seconds, 4)

    metrics = {
        "model_identifier": MODEL_IDENTIFIER,
        "model": "spacy_embedding_logistic_regression_issue_classifier",
        "artifact_role": "semantic",
        "trained_on": "data/splits/train.csv ONLY",
        "n_train": len(train_text),
        "classes": sorted(train_labels.unique().tolist()),
        "model_selection": selection,
        "validation": val_metrics,
        "test": test_metrics,
        "challenge": challenge_metrics,
        "challenge_by_type": challenge_by_type,
        "latency": latency,
    }
    return clf, metrics


def train_semantic(random_state: int = RANDOM_SEED, force_rebuild_splits: bool = False) -> dict:
    """
    I/O wrapper: loads data/splits/{train,val,test}.csv and
    data/challenge_set.csv (never regenerating either), trains the
    semantic classifier using ONLY train.csv (embeddings + model
    selection on val), evaluates on val/test/challenge, persists the
    classifier under models/semantic_issue_classifier/ (separate from both
    the production and benchmark artifacts), and writes a full
    reproducibility + comparison-ready report.
    """
    manifest = splits_mod.build_splits(force=force_rebuild_splits)
    split_data = splits_mod.load_splits()

    challenge_path = challenge_mod.CHALLENGE_SET_PATH
    if not challenge_path.exists():
        raise FileNotFoundError(
            f"Challenge set not found at {challenge_path}. Per this phase's scope, "
            "the challenge set must already exist (not regenerated here) — run "
            "`python -m evaluation.challenge_set` if this is a fresh checkout."
        )
    challenge_df = pd.read_csv(challenge_path, keep_default_na=False, na_values=[])

    leakage_checks.assert_no_split_leakage({
        "train": split_data["train"]["feedback_text"],
        "val": split_data["val"]["feedback_text"],
        "test": split_data["test"]["feedback_text"],
        "challenge": challenge_df["feedback_text"],
    })

    clf, core_metrics = fit_semantic_from_frames(
        train_df=split_data["train"],
        val_df=split_data["val"],
        test_df=split_data["test"],
        challenge_df=challenge_df,
        random_state=random_state,
    )

    SEMANTIC_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, SEMANTIC_CLASSIFIER_PATH)
    classifier_artifact_bytes = SEMANTIC_CLASSIFIER_PATH.stat().st_size

    import en_core_web_md  # the installed pretrained-embedding package itself
    embedding_package_dir = Path(en_core_web_md.__file__).parent
    embedding_package_bytes = sum(f.stat().st_size for f in embedding_package_dir.rglob("*") if f.is_file())

    report = {
        "phase": "3B",
        "report_type": "semantic_baseline_evaluation",
        "reproducibility": {
            "model_identifier": MODEL_IDENTIFIER,
            "evaluation_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "random_seed": random_state,
            "python_version": platform.python_version(),
            "scikit_learn_version": sklearn.__version__,
            "embedding_model_name": embeddings_mod.EMBEDDING_MODEL_NAME,
            "embedding_model_version": embeddings_mod.EMBEDDING_MODEL_VERSION,
            "embedding_model_source": embeddings_mod.EMBEDDING_MODEL_SOURCE,
            "embedding_dim": embeddings_mod.EMBEDDING_DIM,
            "train_dataset_version": SAMPLE_DATASET_VERSION,
            "val_dataset_version": SAMPLE_DATASET_VERSION,
            "test_dataset_version": SAMPLE_DATASET_VERSION,
            "challenge_dataset_version": CHALLENGE_DATASET_VERSION,
            "feature_configuration": {
                "embedding": "spaCy static word vectors, mean-pooled (frozen, not fit on project data)",
                "embedding_dim": embeddings_mod.EMBEDDING_DIM,
                "classifier": "LogisticRegression",
                "classifier_fixed_params": CLASSIFIER_FIXED_PARAMS,
                "classifier_selected_C": core_metrics["model_selection"]["selected_C"],
                "classifier_C_grid_searched": C_GRID,
            },
            "artifact_paths": {
                "classifier": str(SEMANTIC_CLASSIFIER_PATH),
            },
            "artifact_sha256": {
                "classifier": _file_sha256(SEMANTIC_CLASSIFIER_PATH),
            },
            "artifact_size_bytes": {
                "trained_classifier_only": classifier_artifact_bytes,
                "pretrained_embedding_package_on_disk": embedding_package_bytes,
                "note": (
                    "The classifier is the only artifact this project trained "
                    "(small — see bytes above). The embedding package is a "
                    "fixed, off-the-shelf pip dependency, not something fit on "
                    "project data; its on-disk size is reported separately for "
                    "an honest total-deployment-cost picture."
                ),
            },
            "benchmark_artifact_paths_for_comparison": {
                "vectorizer": str(benchmark_mod.BENCHMARK_VECTORIZER_PATH),
                "model": str(benchmark_mod.BENCHMARK_MODEL_PATH),
            },
            "production_artifact_paths_for_comparison": {
                "vectorizer": str(production_classifier.VECTORIZER_PATH),
                "model": str(production_classifier.MODEL_PATH),
            },
            "trained_only_on": "data/splits/train.csv",
            "validation_usage": "model selection only (classifier C), never used to fit the final model",
            "challenge_usage": "final diagnostic only, never used for tuning",
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
        "model_selection": core_metrics["model_selection"],
        "results": {
            "validation": core_metrics["validation"],
            "test": core_metrics["test"],
            "challenge": core_metrics["challenge"],
            "challenge_by_type": core_metrics["challenge_by_type"],
        },
        "latency": core_metrics["latency"],
        "notes": (
            "SEPARATE semantic-embedding artifact trained only on "
            "data/splits/train.csv, distinct from both the production "
            "model (models/issue_classifier/) and the frozen Phase 3A "
            "benchmark (models/benchmark_issue_classifier/). Not wired "
            "into app.py, backend/, or any live inference path. Embedding "
            "model choice and its constraints are documented in "
            "evaluation/embeddings.py. Same synthetic-data caveats apply "
            "as documented in PHASE1_ML_UPGRADE.md / PHASE3A_EVALUATION.md "
            "— validation/test scores reflect the training dataset's "
            "constructed vocabulary separation; the challenge-set numbers "
            "are the more meaningful generalization signal."
        ),
    }

    with open(SEMANTIC_METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp_slug = report["reproducibility"]["evaluation_timestamp_utc"].replace(":", "-")
    timestamped_path = EVAL_RESULTS_DIR / f"semantic_evaluation_{timestamp_slug}.json"
    with open(timestamped_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(LATEST_SEMANTIC_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    logger.info(
        "Semantic classifier trained on train.csv only (n=%d, C=%s) and persisted to %s",
        core_metrics["n_train"], core_metrics["model_selection"]["selected_C"], SEMANTIC_MODEL_DIR,
    )
    return report


if __name__ == "__main__":
    report = train_semantic()
    print(json.dumps({
        "model_identifier": report["reproducibility"]["model_identifier"],
        "embedding_model": report["reproducibility"]["embedding_model_name"],
        "selected_C": report["reproducibility"]["feature_configuration"]["classifier_selected_C"],
        "split_sizes": report["reproducibility"]["split_sizes"],
        "validation": {k: v for k, v in report["results"]["validation"].items()
                        if k in ("accuracy", "macro_f1", "mean_confidence", "n_examples")},
        "test": {k: v for k, v in report["results"]["test"].items()
                  if k in ("accuracy", "macro_f1", "mean_confidence", "n_examples")},
        "challenge": {k: v for k, v in report["results"]["challenge"].items()
                       if k in ("accuracy", "macro_f1", "mean_confidence", "n_examples")},
        "challenge_by_type": report["results"]["challenge_by_type"],
        "latency": report["latency"],
        "artifact_size_bytes": report["reproducibility"]["artifact_size_bytes"],
    }, indent=2))
