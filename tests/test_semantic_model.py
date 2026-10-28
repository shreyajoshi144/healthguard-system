"""
tests/test_semantic_model.py
────────────────────────────────
PHASE 3B — tests proving evaluation/semantic_model.py and
evaluation/comparison.py actually do what they claim:

  1. Train-only fitting: the classifier's .fit() is called only with
     train-sized data, at every step of model selection — never val/test.
  2. No split leakage: train/val/test/challenge remain mutually disjoint
     (reusing the same check the frozen benchmark and production model
     are already held to).
  3. Benchmark artifacts unchanged: running the semantic pipeline must not
     touch the frozen Phase 3A benchmark files in any way.
  4. Semantic artifacts stored separately: distinct path from both the
     production and (frozen) benchmark artifacts.
  5. End-to-end evaluation works: train_semantic() completes against the
     real, persisted Phase 3A data and produces the expected report shape.
  6. Comparison report is generated correctly: build_comparison_report()
     produces correct deltas and includes both models' numbers.

Run:
    python -m unittest tests.test_semantic_model -v
"""

from __future__ import annotations
import unittest
import hashlib
from unittest.mock import patch

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from evaluation import leakage_checks
from tests._artifact_isolation import isolated_training_artifacts


def _spacy_available() -> bool:
    try:
        from evaluation.embeddings import load_embedder
        load_embedder()
        return True
    except Exception:
        return False


def _toy_frames():
    """Small hand-built frames spanning 3 categories — enough for the
    classifier to fit without degenerate single-class behavior, small
    enough that embedding them is fast."""
    train_df = pd.DataFrame([
        {"feedback_text": "The wait was long and staff seemed rushed.", "true_issue_category": "Wait Time"},
        {"feedback_text": "Billing charges were confusing this month.", "true_issue_category": "Billing"},
        {"feedback_text": "Staff were friendly and the room was clean.", "true_issue_category": "General Positive"},
        {"feedback_text": "Wait time was excessive at the front desk.", "true_issue_category": "Wait Time"},
        {"feedback_text": "The invoice had unexpected billing charges.", "true_issue_category": "Billing"},
        {"feedback_text": "Everyone was kind and the visit felt clean.", "true_issue_category": "General Positive"},
    ])
    val_df = pd.DataFrame([
        {"feedback_text": "This visit felt wonderfully pleasant honestly.", "true_issue_category": "General Positive"},
        {"feedback_text": "Waiting around again took forever unfortunately.", "true_issue_category": "Wait Time"},
    ])
    test_df = pd.DataFrame([
        {"feedback_text": "Billing was a total nightmare frankly.", "true_issue_category": "Billing"},
        {"feedback_text": "Staff were kind during the whole visit.", "true_issue_category": "General Positive"},
    ])
    challenge_df = pd.DataFrame([
        {"feedback_text": "An entirely unseen sentence about waiting.", "primary_issue": "Wait Time", "challenge_type": "paraphrase"},
        {"feedback_text": "Something about billing paperwork issues.", "primary_issue": "Billing", "challenge_type": "paraphrase"},
    ])
    return train_df, val_df, test_df, challenge_df


@unittest.skipUnless(_spacy_available(), "spaCy / en_core_web_md not installed in this environment")
class TestSemanticTrainsOnlyOnTrain(unittest.TestCase):
    """Task: train-only fitting. Wraps LogisticRegression.fit to record
    the size of every training call made during the full semantic
    training + model-selection pipeline, and asserts every single one
    matches the train set size — never val, never test, never challenge."""

    def test_every_fit_call_uses_only_train_sized_data(self):
        from evaluation import semantic_model

        train_df, val_df, test_df, challenge_df = _toy_frames()
        n_train = len(train_df)

        fit_call_sizes = []
        original_fit = LogisticRegression.fit

        def spy_fit(self, X, y, *args, **kwargs):
            fit_call_sizes.append(len(y))
            return original_fit(self, X, y, *args, **kwargs)

        with patch.object(LogisticRegression, "fit", spy_fit):
            semantic_model.fit_semantic_from_frames(
                train_df, val_df, test_df, challenge_df, random_state=42,
            )

        self.assertTrue(len(fit_call_sizes) >= 1, "LogisticRegression.fit was never called")
        for size in fit_call_sizes:
            self.assertEqual(
                size, n_train,
                f"A .fit() call used {size} examples, expected exactly n_train={n_train} "
                "— validation/test/challenge data may have leaked into training.",
            )

    def test_model_selection_only_uses_train_and_val(self):
        """select_classifier_C must never see test or challenge data —
        checked structurally: its signature only accepts train/val arrays."""
        from evaluation.semantic_model import select_classifier_C
        import inspect

        sig = inspect.signature(select_classifier_C)
        param_names = set(sig.parameters.keys())
        self.assertEqual(param_names, {"X_train", "y_train", "X_val", "y_val", "c_grid", "random_state"})

    def test_classifier_classes_match_train_labels_only(self):
        from evaluation import semantic_model

        train_df, val_df, test_df, challenge_df = _toy_frames()
        clf, metrics = semantic_model.fit_semantic_from_frames(
            train_df, val_df, test_df, challenge_df, random_state=42,
        )
        self.assertEqual(set(clf.classes_), set(train_df["true_issue_category"].unique()))


class TestSemanticNoSplitLeakage(unittest.TestCase):
    """Task: no split leakage. Reuses the same disjointness check the
    frozen benchmark is held to."""

    def test_toy_frames_are_disjoint(self):
        train_df, val_df, test_df, challenge_df = _toy_frames()
        report = leakage_checks.check_split_disjointness({
            "train": train_df["feedback_text"],
            "val": val_df["feedback_text"],
            "test": test_df["feedback_text"],
            "challenge": challenge_df["feedback_text"],
        })
        self.assertFalse(report["any_overlap"])

    def test_detects_leakage_if_introduced(self):
        train_df, val_df, test_df, challenge_df = _toy_frames()
        val_df = pd.concat([val_df, train_df.iloc[[0]]], ignore_index=True)  # inject a leak
        with self.assertRaises(ValueError):
            leakage_checks.assert_no_split_leakage({
                "train": train_df["feedback_text"],
                "val": val_df["feedback_text"],
                "test": test_df["feedback_text"],
                "challenge": challenge_df["feedback_text"],
            })


class TestArtifactSeparation(unittest.TestCase):
    """Task: benchmark artifacts unchanged; semantic artifacts stored
    separately from both production and benchmark."""

    def test_semantic_path_differs_from_benchmark_and_production(self):
        from evaluation import semantic_model
        from evaluation import benchmark_model
        from modules import ml_issue_classifier as production

        self.assertNotEqual(semantic_model.SEMANTIC_CLASSIFIER_PATH, benchmark_model.BENCHMARK_MODEL_PATH)
        self.assertNotEqual(semantic_model.SEMANTIC_CLASSIFIER_PATH, benchmark_model.BENCHMARK_VECTORIZER_PATH)
        self.assertNotEqual(semantic_model.SEMANTIC_CLASSIFIER_PATH, production.MODEL_PATH)
        self.assertNotEqual(semantic_model.SEMANTIC_MODEL_DIR, benchmark_model.BENCHMARK_MODEL_DIR)

    @unittest.skipUnless(_spacy_available(), "spaCy / en_core_web_md not installed in this environment")
    def test_benchmark_and_production_unchanged_after_semantic_training(self):
        from evaluation import semantic_model
        from evaluation import benchmark_model
        from modules import ml_issue_classifier as production

        if not (benchmark_model.BENCHMARK_MODEL_PATH.exists() and production.is_available()):
            self.skipTest("Frozen benchmark or production model not present in this environment.")

        def _hash(path):
            h = hashlib.sha256()
            with open(path, "rb") as f:
                h.update(f.read())
            return h.hexdigest()

        before = {
            "benchmark_model": _hash(benchmark_model.BENCHMARK_MODEL_PATH),
            "benchmark_vectorizer": _hash(benchmark_model.BENCHMARK_VECTORIZER_PATH),
            "production_model": _hash(production.MODEL_PATH),
            "production_vectorizer": _hash(production.VECTORIZER_PATH),
        }

        try:
            with isolated_training_artifacts():
                semantic_model.train_semantic()
        except FileNotFoundError:
            self.skipTest("data/splits or data/challenge_set.csv not present.")

        after = {
            "benchmark_model": _hash(benchmark_model.BENCHMARK_MODEL_PATH),
            "benchmark_vectorizer": _hash(benchmark_model.BENCHMARK_VECTORIZER_PATH),
            "production_model": _hash(production.MODEL_PATH),
            "production_vectorizer": _hash(production.VECTORIZER_PATH),
        }
        self.assertEqual(before, after, "Benchmark or production artifacts changed during semantic training.")


@unittest.skipUnless(_spacy_available(), "spaCy / en_core_web_md not installed in this environment")
class TestSemanticEndToEnd(unittest.TestCase):
    """Task: end-to-end evaluation works; comparison report is generated
    correctly. Runs against the real, persisted Phase 3A data."""

    @classmethod
    def setUpClass(cls):
        import evaluation.splits as splits_mod
        import evaluation.challenge_set as challenge_mod
        if not (splits_mod.TRAIN_PATH.exists() and splits_mod.VAL_PATH.exists()
                and splits_mod.TEST_PATH.exists() and challenge_mod.CHALLENGE_SET_PATH.exists()):
            raise unittest.SkipTest(
                "data/splits/*.csv or data/challenge_set.csv not found — run "
                "`python -m evaluation.splits` and `python -m evaluation.challenge_set` first."
            )

    def test_train_semantic_completes_and_returns_expected_shape(self):
        from evaluation import semantic_model

        with isolated_training_artifacts():
            report = semantic_model.train_semantic()
        self.assertEqual(report["phase"], "3B")
        for key in ("reproducibility", "model_selection", "results", "latency"):
            self.assertIn(key, report)
        for split_name in ("validation", "test", "challenge"):
            self.assertIn(split_name, report["results"])
            self.assertIn("accuracy", report["results"][split_name])
            self.assertIn("mean_confidence", report["results"][split_name])
        self.assertIn("challenge_by_type", report["results"])
        for ctype in semantic_model.CHALLENGE_TYPES:
            if ctype in report["results"]["challenge_by_type"]:
                self.assertIn("accuracy", report["results"]["challenge_by_type"][ctype])

    def test_model_selection_used_validation_not_test_or_challenge(self):
        from evaluation import semantic_model

        with isolated_training_artifacts():
            report = semantic_model.train_semantic()
        selection = report["model_selection"]
        self.assertIn("trials", selection)
        self.assertIn("selected_C", selection)
        self.assertIn("selected_validation_macro_f1", selection)
        # Every trial's score field is explicitly named "validation_macro_f1"
        # — structurally impossible to confuse with a test/challenge score.
        for trial in selection["trials"]:
            self.assertIn("validation_macro_f1", trial)

    def test_reproducibility_metadata_present(self):
        from evaluation import semantic_model

        with isolated_training_artifacts():
            report = semantic_model.train_semantic()
        repro = report["reproducibility"]
        for field in (
            "model_identifier", "embedding_model_name", "embedding_model_version",
            "evaluation_timestamp_utc", "random_seed", "train_dataset_version",
            "challenge_dataset_version", "feature_configuration", "split_sizes",
            "artifact_sha256", "artifact_size_bytes",
        ):
            self.assertIn(field, repro, f"missing reproducibility field: {field}")
        self.assertEqual(repro["model_identifier"], semantic_model.MODEL_IDENTIFIER)
        self.assertEqual(repro["embedding_model_name"], "en_core_web_md")

    def test_split_sizes_match_actual_files_on_disk(self):
        from evaluation import semantic_model
        import evaluation.splits as splits_mod
        import evaluation.challenge_set as challenge_mod

        train_n = len(pd.read_csv(splits_mod.TRAIN_PATH))
        val_n = len(pd.read_csv(splits_mod.VAL_PATH))
        test_n = len(pd.read_csv(splits_mod.TEST_PATH))
        challenge_n = len(pd.read_csv(challenge_mod.CHALLENGE_SET_PATH, keep_default_na=False, na_values=[]))

        with isolated_training_artifacts():
            report = semantic_model.train_semantic()
        sizes = report["reproducibility"]["split_sizes"]
        self.assertEqual(sizes["train"], train_n)
        self.assertEqual(sizes["validation"], val_n)
        self.assertEqual(sizes["test"], test_n)
        self.assertEqual(sizes["challenge"], challenge_n)

    def test_did_not_regenerate_challenge_set_or_splits(self):
        from evaluation import semantic_model
        import evaluation.splits as splits_mod
        import evaluation.challenge_set as challenge_mod

        before_train = pd.read_csv(splits_mod.TRAIN_PATH)["feedback_text"].tolist()
        before_challenge = pd.read_csv(
            challenge_mod.CHALLENGE_SET_PATH, keep_default_na=False, na_values=[]
        )["feedback_text"].tolist()

        with isolated_training_artifacts():
            semantic_model.train_semantic()

        after_train = pd.read_csv(splits_mod.TRAIN_PATH)["feedback_text"].tolist()
        after_challenge = pd.read_csv(
            challenge_mod.CHALLENGE_SET_PATH, keep_default_na=False, na_values=[]
        )["feedback_text"].tolist()

        self.assertEqual(before_train, after_train)
        self.assertEqual(before_challenge, after_challenge)

    def test_comparison_report_generated_correctly(self):
        from evaluation import semantic_model, benchmark_model, comparison

        with isolated_training_artifacts():
            semantic_model.train_semantic()
            benchmark_model.train_benchmark()
            report = comparison.build_comparison_report()

        self.assertEqual(report["phase"], "3B")
        self.assertIn("models_compared", report)
        self.assertIn("benchmark", report["models_compared"])
        self.assertIn("semantic", report["models_compared"])

        for split_name in ("validation", "test", "challenge"):
            self.assertIn(split_name, report["results_by_split"])
            split_result = report["results_by_split"][split_name]
            # Verify the delta is actually computed correctly, not just present
            expected_delta = round(
                split_result["semantic"]["accuracy"] - split_result["benchmark"]["accuracy"], 4
            )
            self.assertEqual(split_result["delta_semantic_minus_benchmark"]["accuracy"], expected_delta)

        for ctype in semantic_model.CHALLENGE_TYPES:
            if ctype in report["challenge_by_type_comparison"]:
                entry = report["challenge_by_type_comparison"][ctype]
                expected_delta = round(entry["semantic_accuracy"] - entry["benchmark_accuracy"], 4)
                self.assertEqual(entry["delta_semantic_minus_benchmark"], expected_delta)

        self.assertIn("latency_comparison", report)
        self.assertIn("benchmark", report["latency_comparison"])
        self.assertIn("semantic", report["latency_comparison"])
        self.assertIn("artifact_size_comparison_bytes", report)
        self.assertGreater(report["artifact_size_comparison_bytes"]["benchmark_total"], 0)
        self.assertGreater(report["artifact_size_comparison_bytes"]["semantic_trained_classifier_only"], 0)

    def test_evaluate_report_includes_semantic_and_comparison_sections(self):
        from evaluation.evaluate import run_evaluation
        from evaluation import semantic_model, benchmark_model, comparison

        with isolated_training_artifacts():
            semantic_model.train_semantic()
            benchmark_model.train_benchmark()
            comparison.build_comparison_report()
            report = run_evaluation()

        self.assertIsNotNone(report["semantic_baseline"])
        self.assertEqual(report["semantic_baseline"]["model_role"], "semantic")
        self.assertIsNotNone(report["benchmark_vs_semantic_comparison"])


if __name__ == "__main__":
    unittest.main()
