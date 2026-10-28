"""
evaluation/evaluate.py
─────────────────────────
PHASE 3A — main evaluation entrypoint.

Loads the EXISTING, PERSISTED TF-IDF + Logistic Regression baseline
(modules/ml_issue_classifier.py) via its public predict() function —
no training or retraining happens anywhere in this file — and evaluates
it against:

  1. The held-out test split (evaluation/splits.py) — same distribution as
     training, same synthetic-data caveats as PHASE1_ML_UPGRADE.md already
     documents.
  2. The challenge/OOD set (evaluation/challenge_set.py) — deliberately
     harder, differently-worded, sometimes out-of-domain, evaluated and
     reported SEPARATELY so a strong test-split score can't be mistaken
     for real generalization.

Before trusting any number, it runs both leakage checks from
evaluation/leakage_checks.py:
  - column-level: confirms the baseline's only feature is text,
  - row-level: confirms challenge/test/val/train are mutually disjoint.

Output: a single machine-readable JSON report under evaluation/results/,
containing metrics, per-class breakdowns, confusion matrices, the
multi-label evaluation scaffold, and full reproducibility metadata
(dataset version, seed, model identity, timestamp, feature configuration).

Run:
    python -m evaluation.evaluate
"""

from __future__ import annotations
import sys
import json
import hashlib
import platform
from pathlib import Path
from datetime import datetime, timezone

import sklearn
import pandas as pd
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support,
    confusion_matrix, classification_report,
)

from evaluation import splits as splits_mod
from evaluation import challenge_set as challenge_mod
from evaluation import leakage_checks
from evaluation import multilabel_eval
from evaluation.data_contract import decode_secondary_issues, ALLOWED_ISSUE_CATEGORIES
from modules import ml_issue_classifier
from utils.config import (
    RANDOM_SEED, EVAL_RESULTS_DIR, ISSUE_MODEL_DIR,
    SAMPLE_DATASET_VERSION, CHALLENGE_DATASET_VERSION,
    ISSUE_ML_CONFIDENCE_THRESHOLD,
)
from utils.logger import get_logger

logger = get_logger(__name__)

LATEST_REPORT_PATH = EVAL_RESULTS_DIR / "latest_evaluation.json"


# ─────────────────────────────────────────────────────────────────────────
# Standard single-label classification metrics (test split + challenge set)
# ─────────────────────────────────────────────────────────────────────────

def _compute_classification_metrics(y_true, y_pred) -> dict:
    """Same metric set as modules/ml_issue_classifier.py's internal
    _evaluate() — recomputed here independently rather than importing that
    private function, so this evaluation harness has no runtime dependency
    on the preserved baseline module's internals (only its public predict())."""
    acc = accuracy_score(y_true, y_pred)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_true, y_pred, average="macro", zero_division=0
    )
    labels_sorted = sorted(set(y_true) | set(y_pred))
    cm = confusion_matrix(y_true, y_pred, labels=labels_sorted)
    report = classification_report(y_true, y_pred, zero_division=0, output_dict=True)
    return {
        "accuracy": round(float(acc), 4),
        "macro_precision": round(float(precision), 4),
        "macro_recall": round(float(recall), 4),
        "macro_f1": round(float(f1), 4),
        "confusion_matrix": cm.tolist(),
        "confusion_matrix_labels": labels_sorted,
        "per_class_report": report,
    }


def _evaluate_on_dataframe(df: pd.DataFrame, text_col: str, label_col: str) -> dict:
    labels, confidences = ml_issue_classifier.predict(df[text_col])
    metrics = _compute_classification_metrics(df[label_col].astype(str), labels)
    metrics["mean_confidence"] = round(float(confidences.mean()), 4)
    metrics["low_confidence_rate"] = round(
        float((confidences < ISSUE_ML_CONFIDENCE_THRESHOLD).mean()), 4
    )
    metrics["n_examples"] = len(df)
    return metrics


def _breakdown_by_challenge_type(df: pd.DataFrame, label_col: str) -> dict:
    """Accuracy per challenge_type subgroup (paraphrase/ambiguous/etc.) so
    a single aggregate challenge-set number doesn't hide which failure
    modes the baseline actually has."""
    breakdown = {}
    for ctype, group in df.groupby("challenge_type"):
        labels, confidences = ml_issue_classifier.predict(group["feedback_text"])
        acc = accuracy_score(group[label_col].astype(str), labels)
        breakdown[ctype] = {
            "n_examples": len(group),
            "accuracy": round(float(acc), 4),
            "mean_confidence": round(float(confidences.mean()), 4),
        }
    return breakdown


# ─────────────────────────────────────────────────────────────────────────
# Reproducibility metadata
# ─────────────────────────────────────────────────────────────────────────

def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _model_identity() -> dict:
    vectorizer, clf = ml_issue_classifier._load()  # public is_available() already confirmed presence
    model_path = ml_issue_classifier.MODEL_PATH
    vectorizer_path = ml_issue_classifier.VECTORIZER_PATH
    return {
        "model_name": "tfidf_logistic_regression_issue_classifier",
        "model_file": str(model_path),
        "model_file_sha256": _file_sha256(model_path),
        "vectorizer_file": str(vectorizer_path),
        "vectorizer_file_sha256": _file_sha256(vectorizer_path),
        "classifier_params": {
            k: v for k, v in clf.get_params().items()
            if k in ("C", "class_weight", "max_iter", "penalty", "solver", "random_state")
        },
        "vectorizer_params": {
            k: v for k, v in vectorizer.get_params().items()
            if k in ("ngram_range", "min_df", "max_df", "sublinear_tf", "max_features")
        },
        "confidence_threshold": ISSUE_ML_CONFIDENCE_THRESHOLD,
    }


def _reproducibility_metadata() -> dict:
    return {
        "evaluation_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "random_seed": RANDOM_SEED,
        "python_version": platform.python_version(),
        "scikit_learn_version": sklearn.__version__,
        "sample_dataset_version": SAMPLE_DATASET_VERSION,
        "challenge_dataset_version": CHALLENGE_DATASET_VERSION,
        "model": _model_identity(),
    }


# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────

def run_evaluation(rebuild_splits: bool = False, rebuild_challenge: bool = False) -> dict:
    if not ml_issue_classifier.is_available():
        raise FileNotFoundError(
            "No persisted issue-classifier model found. Phase 3A evaluates the "
            "EXISTING baseline and does not train one — run "
            "`python training/train_models.py` first."
        )

    # ── 1. Ensure splits + challenge set exist (build if missing) ────────
    print("=" * 70)
    print("STEP 1 — Loading / building dataset splits")
    print("=" * 70)
    manifest = splits_mod.build_splits(force=rebuild_splits)
    split_data = splits_mod.load_splits()

    if rebuild_challenge or not challenge_mod.CHALLENGE_SET_PATH.exists():
        challenge_mod.write_challenge_set()
    # keep_default_na=False: one of the (unmodified) challenge-set examples'
    # feedback_text is the literal string "N/A" — pandas' default na_values
    # would otherwise silently read it as a missing value. Read-side fix
    # only; the CSV on disk is untouched.
    challenge_df = pd.read_csv(challenge_mod.CHALLENGE_SET_PATH, keep_default_na=False, na_values=[])
    challenge_df["secondary_issues_list"] = challenge_df["secondary_issues"].apply(decode_secondary_issues)
    print(f"Train: {len(split_data['train'])}  Val: {len(split_data['val'])}  "
          f"Test: {len(split_data['test'])}  Challenge: {len(challenge_df)}")

    # ── 2. Leakage checks — must pass before any metric is trusted ───────
    print("\n" + "=" * 70)
    print("STEP 2 — Leakage checks")
    print("=" * 70)

    leakage_checks.assert_baseline_feature_columns_allowed(["feedback_text"])
    print("Column-level leakage check PASSED — baseline uses only 'feedback_text' as a feature.")

    split_texts = {
        "train": split_data["train"]["feedback_text"],
        "val": split_data["val"]["feedback_text"],
        "test": split_data["test"]["feedback_text"],
        "challenge": challenge_df["feedback_text"],
    }
    split_leakage_report = leakage_checks.assert_no_split_leakage(split_texts)
    print("Split-level leakage check PASSED — no overlapping examples between "
          "train/val/test/challenge (see report for pairwise details).")

    # ── 3. Evaluate on the held-out test split ────────────────────────────
    print("\n" + "=" * 70)
    print("STEP 3 — Evaluating baseline on the held-out TEST split")
    print("=" * 70)
    test_metrics = _evaluate_on_dataframe(
        split_data["test"], text_col="feedback_text", label_col="true_issue_category"
    )
    print(f"Test split  -> accuracy={test_metrics['accuracy']:.4f}  "
          f"macro_f1={test_metrics['macro_f1']:.4f}  n={test_metrics['n_examples']}")
    print(
        "[DISCLAIMER] This test split is held out from the SAME synthetic, "
        "templated sample dataset used for training (see PHASE1_ML_UPGRADE.md). "
        "Per-category vocabulary is distinct by construction, so a very high "
        "score here is expected and does NOT indicate real-world accuracy — "
        "that is exactly why the challenge set below is evaluated separately."
    )

    # ── 4. Evaluate on the challenge/OOD set, separately ──────────────────
    print("\n" + "=" * 70)
    print("STEP 4 — Evaluating baseline on the CHALLENGE/OOD set (separately)")
    print("=" * 70)
    challenge_metrics = _evaluate_on_dataframe(
        challenge_df, text_col="feedback_text", label_col="primary_issue"
    )
    challenge_by_type = _breakdown_by_challenge_type(challenge_df, label_col="primary_issue")
    print(f"Challenge set -> accuracy={challenge_metrics['accuracy']:.4f}  "
          f"macro_f1={challenge_metrics['macro_f1']:.4f}  n={challenge_metrics['n_examples']}")
    for ctype, m in sorted(challenge_by_type.items()):
        print(f"  {ctype:20s} n={m['n_examples']:3d}  accuracy={m['accuracy']:.3f}  "
              f"mean_confidence={m['mean_confidence']:.3f}")

    # ── 5. Multi-label evaluation scaffold (secondary_issues) ────────────
    print("\n" + "=" * 70)
    print("STEP 5 — Multi-label evaluation SCAFFOLD (secondary_issues)")
    print("=" * 70)
    predicted_labels, _ = ml_issue_classifier.predict(challenge_df["feedback_text"])
    y_pred_sets = [[label] for label in predicted_labels]
    y_true_sets = challenge_df["secondary_issues_list"].tolist()
    # Ground truth for this scaffold check is "primary + secondary" so the
    # single-label top-1 prediction is being compared on equal footing
    # (it can only ever predict one label, so credit it for getting the
    # primary issue right even though it has no way to also name secondaries).
    y_true_sets_full = [
        list(dict.fromkeys([row["primary_issue"]] + row["secondary_issues_list"]))
        for _, row in challenge_df.iterrows()
    ]
    multilabel_result = multilabel_eval.evaluate_multilabel(
        y_true=y_true_sets_full, y_pred=y_pred_sets, all_labels=ALLOWED_ISSUE_CATEGORIES,
    )
    print(f"Scaffold subset_accuracy={multilabel_result['subset_accuracy']:.4f}  "
          f"hamming_loss={multilabel_result['hamming_loss']:.4f}")
    print("[NOTE] Scaffold only — see multilabel_result.scaffold_notice in the JSON report.")

    # ── 6. Assemble the full report ───────────────────────────────────────
    # ── 6. Attach the benchmark baseline's results, clearly separated ────
    # Read-only: this reads the benchmark artifact's own persisted report
    # (evaluation/benchmark_model.py) if one exists. It does NOT train or
    # re-evaluate anything here — that keeps this function fast and
    # decoupled from the (separate) benchmark training step. If the
    # benchmark hasn't been run yet, this section is simply omitted rather
    # than failing the whole report.
    benchmark_baseline_section = None
    benchmark_report_path = EVAL_RESULTS_DIR / "latest_benchmark_evaluation.json"
    if benchmark_report_path.exists():
        with open(benchmark_report_path, "r", encoding="utf-8") as f:
            benchmark_report = json.load(f)
        benchmark_baseline_section = {
            "model_role": "benchmark",
            "note": (
                "SEPARATE artifact trained ONLY on data/splits/train.csv "
                "(models/benchmark_issue_classifier/) — distinct from the "
                "production model evaluated under results.model_role="
                "'production' above. See evaluation/benchmark_model.py."
            ),
            "reproducibility": benchmark_report["reproducibility"],
            "results": benchmark_report["results"],
        }
    else:
        logger.info(
            "No benchmark evaluation found at %s — run "
            "`python -m evaluation.benchmark_model` to include it in this report.",
            benchmark_report_path,
        )

    # ── 7. Attach the Phase 3B semantic baseline + comparison, likewise
    # read-only and additive. Omitted entirely if Phase 3B hasn't been run.
    semantic_baseline_section = None
    semantic_report_path = EVAL_RESULTS_DIR / "latest_semantic_evaluation.json"
    if semantic_report_path.exists():
        with open(semantic_report_path, "r", encoding="utf-8") as f:
            semantic_report = json.load(f)
        semantic_baseline_section = {
            "model_role": "semantic",
            "note": (
                "SEPARATE Phase 3B artifact (models/semantic_issue_classifier/) "
                "— pretrained sentence embeddings + Logistic Regression, "
                "trained only on data/splits/train.csv. See "
                "evaluation/semantic_model.py and evaluation/embeddings.py."
            ),
            "reproducibility": semantic_report["reproducibility"],
            "results": semantic_report["results"],
            "latency": semantic_report["latency"],
        }
    else:
        logger.info(
            "No semantic evaluation found at %s — run "
            "`python -m evaluation.semantic_model` to include it in this report.",
            semantic_report_path,
        )

    comparison_section = None
    comparison_path = EVAL_RESULTS_DIR / "latest_comparison.json"
    if comparison_path.exists():
        with open(comparison_path, "r", encoding="utf-8") as f:
            comparison_section = json.load(f)
    else:
        logger.info(
            "No benchmark-vs-semantic comparison found at %s — run "
            "`python -m evaluation.comparison` to include it in this report.",
            comparison_path,
        )

    report = {
        "phase": "3A",
        "report_type": "baseline_evaluation",
        "reproducibility": _reproducibility_metadata(),
        "data": {
            "split_manifest": manifest,
            "split_leakage_report": split_leakage_report,
            "challenge_set": {
                "path": str(challenge_mod.CHALLENGE_SET_PATH),
                "n_examples": len(challenge_df),
                "by_challenge_type_counts": challenge_df["challenge_type"].value_counts().to_dict(),
            },
        },
        "results": {
            "model_role": "production",
            "note": (
                "Metrics for the PRODUCTION model under models/issue_classifier/ "
                "(the one app.py / backend/ actually use for inference). See "
                "benchmark_baseline below for the separate, provably-unseen-data "
                "benchmark artifact's numbers, and semantic_baseline for the "
                "Phase 3B embedding-based comparison."
            ),
            "test_split": test_metrics,
            "test_split_disclaimer": (
                "Held out from the same synthetic training dataset. High scores "
                "here reflect the dataset's constructed vocabulary separation, "
                "not real-world accuracy. See PHASE1_ML_UPGRADE.md and "
                "PHASE3A_EVALUATION.md."
            ),
            "challenge_set": challenge_metrics,
            "challenge_set_by_type": challenge_by_type,
            "multilabel_scaffold": multilabel_result,
        },
        "benchmark_baseline": benchmark_baseline_section,
        "semantic_baseline": semantic_baseline_section,
        "benchmark_vs_semantic_comparison": comparison_section,
    }


    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp_slug = report["reproducibility"]["evaluation_timestamp_utc"].replace(":", "-")
    timestamped_path = EVAL_RESULTS_DIR / f"evaluation_report_{timestamp_slug}.json"
    with open(timestamped_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(LATEST_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("\n" + "=" * 70)
    print(f"Report written to:\n  {timestamped_path}\n  {LATEST_REPORT_PATH}")
    print("=" * 70)
    return report


if __name__ == "__main__":
    run_evaluation()
