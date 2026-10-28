"""
tests/_artifact_isolation.py
──────────────────────────────
PHASE 3C reproducibility/test-isolation fix — shared test-only helpers.

Several tests intentionally call the real training/evaluation entrypoints
(`benchmark_model.train_benchmark()`, `semantic_model.train_semantic()`,
`comparison.build_comparison_report()`, `evaluate.run_evaluation()`,
`reliability_report.run_reliability_analysis()`) to verify they actually
work end-to-end. Left unmodified, those functions persist their output to
the SAME fixed paths the repository's frozen deliverables live at
(`models/benchmark_issue_classifier/`, `models/semantic_issue_classifier/`,
`models/uncertainty/`, `evaluation/results/latest_*.json`) — which means
simply running the test suite would overwrite them every time.

The two context managers below redirect ONLY the write targets (never the
read targets a test needs to stay pointed at the real frozen data) into a
fresh temporary directory for the duration of a `with` block, then restore
every patched module attribute to its original value afterward — even if
the test raises. This does not change what any test asserts or verifies;
it only changes *where* the artifact a test creates during its own run
gets physically written.

Not a test module itself (filename doesn't match `test*.py`, so
`python -m unittest discover` does not try to collect it).
"""

from __future__ import annotations
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from evaluation import benchmark_model, semantic_model, comparison, evaluate
from evaluation import reliability_report as reliability_report_mod


@contextmanager
def isolated_training_artifacts():
    """
    Redirects every path that evaluation/benchmark_model.py,
    evaluation/semantic_model.py, evaluation/comparison.py, and
    evaluation/evaluate.py WRITE to, into one shared temporary directory —
    so a test that chains e.g. train_benchmark() -> train_semantic() ->
    build_comparison_report() -> run_evaluation() together still sees each
    isolated step's output (they all point into the same temp tree), while
    none of it touches the real, frozen
    models/benchmark_issue_classifier/, models/semantic_issue_classifier/,
    or evaluation/results/latest_*.json.

    Use this around any test that calls train_benchmark(), train_semantic(),
    build_comparison_report(), or run_evaluation().
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="hg_test_training_artifacts_"))
    bench_dir = tmpdir / "benchmark_issue_classifier"
    sem_dir = tmpdir / "semantic_issue_classifier"
    results_dir = tmpdir / "evaluation_results"
    bench_dir.mkdir(parents=True)
    sem_dir.mkdir(parents=True)
    results_dir.mkdir(parents=True)

    originals = {
        (benchmark_model, "BENCHMARK_MODEL_DIR"): benchmark_model.BENCHMARK_MODEL_DIR,
        (benchmark_model, "BENCHMARK_VECTORIZER_PATH"): benchmark_model.BENCHMARK_VECTORIZER_PATH,
        (benchmark_model, "BENCHMARK_MODEL_PATH"): benchmark_model.BENCHMARK_MODEL_PATH,
        (benchmark_model, "BENCHMARK_METRICS_PATH"): benchmark_model.BENCHMARK_METRICS_PATH,
        (benchmark_model, "LATEST_BENCHMARK_REPORT_PATH"): benchmark_model.LATEST_BENCHMARK_REPORT_PATH,
        (benchmark_model, "EVAL_RESULTS_DIR"): benchmark_model.EVAL_RESULTS_DIR,
        (semantic_model, "SEMANTIC_MODEL_DIR"): semantic_model.SEMANTIC_MODEL_DIR,
        (semantic_model, "SEMANTIC_CLASSIFIER_PATH"): semantic_model.SEMANTIC_CLASSIFIER_PATH,
        (semantic_model, "SEMANTIC_METRICS_PATH"): semantic_model.SEMANTIC_METRICS_PATH,
        (semantic_model, "LATEST_SEMANTIC_REPORT_PATH"): semantic_model.LATEST_SEMANTIC_REPORT_PATH,
        (semantic_model, "EVAL_RESULTS_DIR"): semantic_model.EVAL_RESULTS_DIR,
        (comparison, "LATEST_COMPARISON_PATH"): comparison.LATEST_COMPARISON_PATH,
        (comparison, "EVAL_RESULTS_DIR"): comparison.EVAL_RESULTS_DIR,
        (evaluate, "LATEST_REPORT_PATH"): evaluate.LATEST_REPORT_PATH,
        (evaluate, "EVAL_RESULTS_DIR"): evaluate.EVAL_RESULTS_DIR,
    }

    try:
        benchmark_model.BENCHMARK_MODEL_DIR = bench_dir
        benchmark_model.BENCHMARK_VECTORIZER_PATH = bench_dir / "tfidf_vectorizer.joblib"
        benchmark_model.BENCHMARK_MODEL_PATH = bench_dir / "logreg_issue_classifier.joblib"
        benchmark_model.BENCHMARK_METRICS_PATH = bench_dir / "metrics.json"
        benchmark_model.LATEST_BENCHMARK_REPORT_PATH = results_dir / "latest_benchmark_evaluation.json"
        benchmark_model.EVAL_RESULTS_DIR = results_dir

        semantic_model.SEMANTIC_MODEL_DIR = sem_dir
        semantic_model.SEMANTIC_CLASSIFIER_PATH = sem_dir / "logreg_classifier.joblib"
        semantic_model.SEMANTIC_METRICS_PATH = sem_dir / "metrics.json"
        semantic_model.LATEST_SEMANTIC_REPORT_PATH = results_dir / "latest_semantic_evaluation.json"
        semantic_model.EVAL_RESULTS_DIR = results_dir

        comparison.LATEST_COMPARISON_PATH = results_dir / "latest_comparison.json"
        comparison.EVAL_RESULTS_DIR = results_dir

        evaluate.LATEST_REPORT_PATH = results_dir / "latest_evaluation.json"
        evaluate.EVAL_RESULTS_DIR = results_dir

        yield tmpdir
    finally:
        for (module, attr), value in originals.items():
            setattr(module, attr, value)
        shutil.rmtree(tmpdir, ignore_errors=True)


@contextmanager
def isolated_reliability_artifacts():
    """
    Redirects only what evaluation/reliability_report.py WRITES
    (calibrators + OOD detector JSONs under models/uncertainty/, and its
    own reliability report JSON under evaluation/results/) into a
    temporary directory.

    Deliberately does NOT touch benchmark_model.BENCHMARK_VECTORIZER_PATH/
    BENCHMARK_MODEL_PATH or semantic_model.SEMANTIC_CLASSIFIER_PATH —
    run_reliability_analysis() needs to keep reading the REAL, already-
    frozen benchmark/semantic artifacts (via evaluation/frozen_models.py)
    so it's genuinely evaluating them, not artifacts a test just created.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="hg_test_uncertainty_artifacts_"))
    results_dir = tmpdir / "evaluation_results"
    results_dir.mkdir(parents=True)

    originals = {
        "UNCERTAINTY_MODELS_DIR": reliability_report_mod.UNCERTAINTY_MODELS_DIR,
        "LATEST_RELIABILITY_REPORT_PATH": reliability_report_mod.LATEST_RELIABILITY_REPORT_PATH,
        "EVAL_RESULTS_DIR": reliability_report_mod.EVAL_RESULTS_DIR,
    }

    try:
        reliability_report_mod.UNCERTAINTY_MODELS_DIR = tmpdir / "uncertainty"
        reliability_report_mod.LATEST_RELIABILITY_REPORT_PATH = results_dir / "latest_reliability_report.json"
        reliability_report_mod.EVAL_RESULTS_DIR = results_dir

        yield tmpdir
    finally:
        for attr, value in originals.items():
            setattr(reliability_report_mod, attr, value)
        shutil.rmtree(tmpdir, ignore_errors=True)
