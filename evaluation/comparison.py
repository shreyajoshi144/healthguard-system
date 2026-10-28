
from __future__ import annotations
import json
import time
from pathlib import Path
from datetime import datetime, timezone

import joblib
import pandas as pd
from sklearn.metrics import accuracy_score

from evaluation import benchmark_model as benchmark_mod
from evaluation import semantic_model as semantic_mod
from evaluation import challenge_set as challenge_mod
from utils.config import EVAL_RESULTS_DIR
from utils.logger import get_logger

logger = get_logger(__name__)

LATEST_COMPARISON_PATH = EVAL_RESULTS_DIR / "latest_comparison.json"


def _benchmark_challenge_by_type(challenge_df: pd.DataFrame) -> dict:
    """Read-only: loads the frozen benchmark's persisted vectorizer +
    classifier and computes a per-challenge_type accuracy breakdown,
    exactly like evaluation/semantic_model.py's _evaluate_by_challenge_type.
    Never calls .fit()/.fit_transform() — inference only."""
    vectorizer = joblib.load(benchmark_mod.BENCHMARK_VECTORIZER_PATH)
    clf = joblib.load(benchmark_mod.BENCHMARK_MODEL_PATH)

    breakdown = {}
    for ctype in semantic_mod.CHALLENGE_TYPES:
        subset = challenge_df[challenge_df["challenge_type"] == ctype]
        if subset.empty:
            continue
        X = vectorizer.transform(subset["feedback_text"].fillna("").astype(str))
        proba = clf.predict_proba(X)
        idx = proba.argmax(axis=1)
        y_pred = clf.classes_[idx]
        confidences = proba[range(len(proba)), idx]
        acc = accuracy_score(subset["primary_issue"].astype(str), y_pred)
        breakdown[ctype] = {
            "n_examples": len(subset),
            "accuracy": round(float(acc), 4),
            "mean_confidence": round(float(confidences.mean()), 4),
        }
    return breakdown


def _benchmark_latency(texts: pd.Series, n_repeats: int = 3) -> dict:
    """Read-only latency measurement for the frozen benchmark, using the
    same per-sample/batch methodology as evaluation/embeddings.py's
    measure_embedding_latency, so the two models' latency numbers are
    directly comparable."""
    import numpy as np

    vectorizer = joblib.load(benchmark_mod.BENCHMARK_VECTORIZER_PATH)
    clf = joblib.load(benchmark_mod.BENCHMARK_MODEL_PATH)
    texts_clean = texts.fillna("").astype(str).tolist()

    per_sample_times = []
    for _ in range(n_repeats):
        start = time.perf_counter()
        for t in texts_clean:
            X = vectorizer.transform([t])
            _ = clf.predict(X)
        per_sample_times.append((time.perf_counter() - start) / len(texts_clean))

    batch_times = []
    for _ in range(n_repeats):
        start = time.perf_counter()
        X = vectorizer.transform(texts_clean)
        _ = clf.predict(X)
        batch_times.append(time.perf_counter() - start)

    return {
        "n_texts": len(texts_clean),
        "n_repeats": n_repeats,
        "per_sample_ms_mean": round(float(np.mean(per_sample_times)) * 1000, 4),
        "batch_total_ms_mean": round(float(np.mean(batch_times)) * 1000, 4),
        "batch_per_example_ms_mean": round(float(np.mean(batch_times)) / len(texts_clean) * 1000, 4),
    }


def _delta(semantic_value: float, benchmark_value: float) -> float:
    return round(semantic_value - benchmark_value, 4)


def build_comparison_report() -> dict:
    if not benchmark_mod.BENCHMARK_METRICS_PATH.exists():
        raise FileNotFoundError(
            f"Frozen benchmark metrics not found at {benchmark_mod.BENCHMARK_METRICS_PATH}. "
            "Run `python -m evaluation.benchmark_model` first (Phase 3A)."
        )
    if not semantic_mod.SEMANTIC_METRICS_PATH.exists():
        raise FileNotFoundError(
            f"Semantic model metrics not found at {semantic_mod.SEMANTIC_METRICS_PATH}. "
            "Run `python -m evaluation.semantic_model` first (Phase 3B)."
        )

    with open(benchmark_mod.BENCHMARK_METRICS_PATH, "r", encoding="utf-8") as f:
        benchmark_report = json.load(f)
    with open(semantic_mod.SEMANTIC_METRICS_PATH, "r", encoding="utf-8") as f:
        semantic_report = json.load(f)

    challenge_df = pd.read_csv(
        challenge_mod.CHALLENGE_SET_PATH, keep_default_na=False, na_values=[]
    )

    # Re-derive, read-only, what the frozen benchmark's own report doesn't
    # already contain, using identical methodology to the semantic report.
    benchmark_challenge_by_type = _benchmark_challenge_by_type(challenge_df)
    test_texts = pd.read_csv(
        Path(benchmark_report["reproducibility"]["split_manifest_reference"]["path"]).parent / "test.csv"
    )["feedback_text"]
    benchmark_latency = _benchmark_latency(test_texts)

    b_val = benchmark_report["results"]["validation"]
    b_test = benchmark_report["results"]["test"]
    b_chal = benchmark_report["results"]["challenge"]
    s_val = semantic_report["results"]["validation"]
    s_test = semantic_report["results"]["test"]
    s_chal = semantic_report["results"]["challenge"]

    def _split_comparison(b, s, split_name):
        return {
            "split": split_name,
            "benchmark": {k: b[k] for k in ("n_examples", "accuracy", "macro_precision", "macro_recall", "macro_f1")},
            "semantic": {k: s[k] for k in ("n_examples", "accuracy", "macro_precision", "macro_recall", "macro_f1")},
            "delta_semantic_minus_benchmark": {
                "accuracy": _delta(s["accuracy"], b["accuracy"]),
                "macro_precision": _delta(s["macro_precision"], b["macro_precision"]),
                "macro_recall": _delta(s["macro_recall"], b["macro_recall"]),
                "macro_f1": _delta(s["macro_f1"], b["macro_f1"]),
            },
        }

    challenge_type_comparison = {}
    for ctype in semantic_mod.CHALLENGE_TYPES:
        b_t = benchmark_challenge_by_type.get(ctype)
        s_t = semantic_report["results"]["challenge_by_type"].get(ctype)
        if not b_t or not s_t:
            continue
        challenge_type_comparison[ctype] = {
            "n_examples": b_t["n_examples"],
            "benchmark_accuracy": b_t["accuracy"],
            "semantic_accuracy": s_t["accuracy"],
            "delta_semantic_minus_benchmark": _delta(s_t["accuracy"], b_t["accuracy"]),
            "benchmark_mean_confidence": b_t["mean_confidence"],
            "semantic_mean_confidence": s_t["mean_confidence"],
        }

    benchmark_artifact_bytes = (
        benchmark_mod.BENCHMARK_VECTORIZER_PATH.stat().st_size
        + benchmark_mod.BENCHMARK_MODEL_PATH.stat().st_size
    )
    semantic_artifact_bytes = semantic_mod.SEMANTIC_CLASSIFIER_PATH.stat().st_size
    embedding_package_bytes = semantic_report["reproducibility"]["artifact_size_bytes"]["pretrained_embedding_package_on_disk"]

    report = {
        "phase": "3B",
        "report_type": "benchmark_vs_semantic_comparison",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "models_compared": {
            "benchmark": {
                "model_identifier": benchmark_report["reproducibility"]["model_identifier"],
                "description": "TF-IDF (1-2 grams) + Logistic Regression, frozen Phase 3A artifact",
            },
            "semantic": {
                "model_identifier": semantic_report["reproducibility"]["model_identifier"],
                "description": (
                    f"{semantic_report['reproducibility']['embedding_model_name']} "
                    f"v{semantic_report['reproducibility']['embedding_model_version']} "
                    "(frozen pretrained embeddings, mean-pooled) + Logistic Regression"
                ),
            },
        },
        "methodology_note": (
            "Both models: same data splits (data/splits/train.csv for fitting, "
            "val.csv for model selection only, test.csv for the one-time final "
            "benchmark, data/challenge_set.csv as a diagnostic never used for "
            "tuning), same classifier family (LogisticRegression, "
            "class_weight='balanced'), same random seed. The only real "
            "variable is the feature representation. Neither model's metrics "
            "were computed differently to favor one over the other."
        ),
        "results_by_split": {
            "validation": _split_comparison(b_val, s_val, "validation"),
            "test": _split_comparison(b_test, s_test, "test"),
            "challenge": _split_comparison(b_chal, s_chal, "challenge"),
        },
        "challenge_by_type_comparison": challenge_type_comparison,
        "latency_comparison": {
            "benchmark": benchmark_latency,
            "semantic": semantic_report["latency"],
            "note": (
                "Semantic latency includes embedding (spaCy vector lookup) + "
                "classification; benchmark latency includes TF-IDF transform + "
                "classification. Both measured the same way (per-sample loop "
                "and batch) on the same test-set texts."
            ),
        },
        "artifact_size_comparison_bytes": {
            "benchmark_total": benchmark_artifact_bytes,
            "semantic_trained_classifier_only": semantic_artifact_bytes,
            "semantic_plus_embedding_package": semantic_artifact_bytes + embedding_package_bytes,
            "note": (
                "'benchmark_total' = TF-IDF vectorizer + classifier (both "
                "trained on project data). 'semantic_trained_classifier_only' "
                "= just the small classifier head this project trained. "
                "'semantic_plus_embedding_package' additionally counts the "
                "~55MB pretrained spaCy model package as installed — a fixed, "
                "shared dependency, not something retrained per-project, but "
                "still part of what must be deployed for the semantic model "
                "to run at all."
            ),
        },
    }

    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp_slug = report["generated_at_utc"].replace(":", "-")
    timestamped_path = EVAL_RESULTS_DIR / f"comparison_{timestamp_slug}.json"
    with open(timestamped_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(LATEST_COMPARISON_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    logger.info("Comparison report written to %s and %s", timestamped_path, LATEST_COMPARISON_PATH)
    return report


if __name__ == "__main__":
    report = build_comparison_report()
    print(json.dumps(report["results_by_split"]["challenge"], indent=2))
    print(json.dumps(report["challenge_by_type_comparison"], indent=2))
    print(json.dumps(report["artifact_size_comparison_bytes"], indent=2))
