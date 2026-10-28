"""
tests/test_benchmark_model.py
────────────────────────────────
PHASE 3A CORRECTION — tests proving evaluation/benchmark_model.py actually
does what it claims:

  1. Benchmark training uses ONLY data/splits/train.csv (or, for the fast
     unit tests, an equivalent in-memory train frame) — never val/test.
  2. Validation/test rows are provably unseen during training: the fitted
     TF-IDF vectorizer's vocabulary cannot contain a word that appears
     ONLY in the validation set and nowhere in train.
  3. The benchmark artifact is stored at a path completely separate from
     the production artifact, and training it never touches the
     production files.
  4. The end-to-end evaluation (train_benchmark()) completes successfully
     against the real, persisted data/splits/*.csv and
     data/challenge_set.csv, and produces a report with the expected
     shape and split sizes.

Run:
    python -m unittest tests.test_benchmark_model -v
"""

from __future__ import annotations
import unittest
import hashlib

import pandas as pd

from evaluation import benchmark_model
from evaluation import leakage_checks
from tests._artifact_isolation import isolated_training_artifacts


def _toy_frames():
    """Small, hand-built train/val/test/challenge frames where val and
    test each contain at least one word that never appears anywhere in
    train — so if the vectorizer's vocabulary ever includes that word, we
    know training saw data it shouldn't have."""
    train_df = pd.DataFrame([
        {"feedback_text": "The wait was long and staff seemed rushed.", "true_issue_category": "Wait Time"},
        {"feedback_text": "Billing charges were confusing this month.", "true_issue_category": "Billing"},
        {"feedback_text": "Staff were friendly and the room was clean.", "true_issue_category": "General Positive"},
        {"feedback_text": "Wait time was excessive at the front desk.", "true_issue_category": "Wait Time"},
        {"feedback_text": "The invoice had unexpected billing charges.", "true_issue_category": "Billing"},
        {"feedback_text": "Everyone was kind and the visit felt clean.", "true_issue_category": "General Positive"},
    ])
    # "zephyrtastic" and "quibblewhack" are nonsense tokens chosen so they
    # cannot possibly appear in train_df above by coincidence.
    val_df = pd.DataFrame([
        {"feedback_text": "This visit felt zephyrtastic honestly.", "true_issue_category": "General Positive"},
        {"feedback_text": "Wait time was long again unfortunately.", "true_issue_category": "Wait Time"},
    ])
    test_df = pd.DataFrame([
        {"feedback_text": "Billing was a quibblewhack nightmare frankly.", "true_issue_category": "Billing"},
        {"feedback_text": "Staff were kind during the whole visit.", "true_issue_category": "General Positive"},
    ])
    challenge_df = pd.DataFrame([
        {"feedback_text": "An entirely unseen sentence about waiting.", "primary_issue": "Wait Time"},
        {"feedback_text": "Something about billing paperwork issues.", "primary_issue": "Billing"},
    ])
    return train_df, val_df, test_df, challenge_df


class TestBenchmarkTrainsOnlyOnTrain(unittest.TestCase):
    """Task 8, bullet 1 & 2: benchmark training uses only train.csv, and
    validation/test remain genuinely unseen during training."""

    def setUp(self):
        self.train_df, self.val_df, self.test_df, self.challenge_df = _toy_frames()
        self.vectorizer, self.clf, self.metrics = benchmark_model.fit_benchmark_from_frames(
            self.train_df, self.val_df, self.test_df, self.challenge_df, random_state=42,
        )

    def test_vectorizer_vocabulary_excludes_validation_only_words(self):
        vocab = set(self.vectorizer.get_feature_names_out())
        self.assertNotIn("zephyrtastic", vocab,
                          "Validation-only word leaked into the training vocabulary.")

    def test_vectorizer_vocabulary_excludes_test_only_words(self):
        vocab = set(self.vectorizer.get_feature_names_out())
        self.assertNotIn("quibblewhack", vocab,
                          "Test-only word leaked into the training vocabulary.")

    def test_n_train_matches_train_frame_size(self):
        self.assertEqual(self.metrics["n_train"], len(self.train_df))

    def test_reported_split_sizes_match_input_frames(self):
        self.assertEqual(self.metrics["validation"]["n_examples"], len(self.val_df))
        self.assertEqual(self.metrics["test"]["n_examples"], len(self.test_df))
        self.assertEqual(self.metrics["challenge"]["n_examples"], len(self.challenge_df))

    def test_trained_on_field_documents_train_only(self):
        self.assertEqual(self.metrics["trained_on"], "data/splits/train.csv ONLY")

    def test_classifier_was_fit_on_train_labels_only(self):
        # The classifier's learned classes must exactly match the labels
        # present in train_df — if val/test labels the trainer never saw
        # somehow leaked in as extra classes, this would catch it.
        self.assertEqual(set(self.clf.classes_), set(self.train_df["true_issue_category"].unique()))


class TestArtifactSeparation(unittest.TestCase):
    """Task 8, bullet 3: benchmark artifact is stored separately from
    production, and training it never touches the production files."""

    def test_benchmark_paths_differ_from_production_paths(self):
        from modules import ml_issue_classifier as production

        self.assertNotEqual(benchmark_model.BENCHMARK_MODEL_PATH, production.MODEL_PATH)
        self.assertNotEqual(benchmark_model.BENCHMARK_VECTORIZER_PATH, production.VECTORIZER_PATH)
        self.assertNotEqual(benchmark_model.BENCHMARK_MODEL_DIR, production.ISSUE_MODEL_DIR)

    def test_production_artifacts_unchanged_after_benchmark_training(self):
        """End-to-end guarantee: run the real train_benchmark() (writing to
        an isolated temp location, not the repo's frozen benchmark path —
        see tests/_artifact_isolation.py) against the actual persisted
        splits/challenge set, and confirm the production model's files are
        byte-for-byte unchanged afterwards."""
        from modules import ml_issue_classifier as production

        if not production.is_available():
            self.skipTest("No production model found — run training/train_models.py first.")

        def _hash(path):
            h = hashlib.sha256()
            with open(path, "rb") as f:
                h.update(f.read())
            return h.hexdigest()

        before = {
            "model": _hash(production.MODEL_PATH),
            "vectorizer": _hash(production.VECTORIZER_PATH),
        }

        try:
            with isolated_training_artifacts():
                benchmark_model.train_benchmark()
        except FileNotFoundError:
            self.skipTest("data/splits or data/challenge_set.csv not present — run the Phase 3A setup first.")

        after = {
            "model": _hash(production.MODEL_PATH),
            "vectorizer": _hash(production.VECTORIZER_PATH),
        }
        self.assertEqual(before, after, "Production model artifacts changed during benchmark training.")

    def test_benchmark_artifact_written_to_its_own_directory(self):
        try:
            with isolated_training_artifacts():
                benchmark_model.train_benchmark()
                # Assertions must run INSIDE the isolated block — the
                # artifact only exists at the redirected path for the
                # duration of the `with`.
                self.assertTrue(benchmark_model.BENCHMARK_MODEL_PATH.exists())
                self.assertTrue(benchmark_model.BENCHMARK_VECTORIZER_PATH.exists())
                self.assertTrue(benchmark_model.BENCHMARK_METRICS_PATH.exists())
                self.assertIn("benchmark_issue_classifier", str(benchmark_model.BENCHMARK_MODEL_DIR))
        except FileNotFoundError:
            self.skipTest("data/splits or data/challenge_set.csv not present — run the Phase 3A setup first.")


class TestBenchmarkEndToEnd(unittest.TestCase):
    """Task 8, bullet 4: the evaluation completes successfully against the
    real, persisted Phase 3A data (not regenerated, not modified)."""

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

    def test_train_benchmark_completes_and_returns_expected_shape(self):
        with isolated_training_artifacts():
            report = benchmark_model.train_benchmark()
        self.assertEqual(report["phase"], "3A-correction")
        self.assertIn("reproducibility", report)
        self.assertIn("results", report)
        for split_name in ("validation", "test", "challenge"):
            self.assertIn(split_name, report["results"])
            self.assertIn("accuracy", report["results"][split_name])
            self.assertIn("macro_f1", report["results"][split_name])
            self.assertIn("confusion_matrix", report["results"][split_name])

    def test_reproducibility_metadata_present(self):
        with isolated_training_artifacts():
            report = benchmark_model.train_benchmark()
        repro = report["reproducibility"]
        for field in (
            "model_identifier", "evaluation_timestamp_utc", "random_seed",
            "train_dataset_version", "challenge_dataset_version",
            "feature_configuration", "split_sizes",
        ):
            self.assertIn(field, repro, f"missing reproducibility field: {field}")
        self.assertEqual(repro["model_identifier"], benchmark_model.MODEL_IDENTIFIER)

    def test_split_sizes_match_actual_files_on_disk(self):
        import evaluation.splits as splits_mod
        import evaluation.challenge_set as challenge_mod

        train_n = len(pd.read_csv(splits_mod.TRAIN_PATH))
        val_n = len(pd.read_csv(splits_mod.VAL_PATH))
        test_n = len(pd.read_csv(splits_mod.TEST_PATH))
        challenge_n = len(pd.read_csv(challenge_mod.CHALLENGE_SET_PATH, keep_default_na=False, na_values=[]))

        with isolated_training_artifacts():
            report = benchmark_model.train_benchmark()
        sizes = report["reproducibility"]["split_sizes"]
        self.assertEqual(sizes["train"], train_n)
        self.assertEqual(sizes["validation"], val_n)
        self.assertEqual(sizes["test"], test_n)
        self.assertEqual(sizes["challenge"], challenge_n)

    def test_evaluate_report_includes_separated_benchmark_section(self):
        """Task 6: the main machine-readable report clearly separates
        benchmark metrics from production metrics."""
        from evaluation.evaluate import run_evaluation

        with isolated_training_artifacts():
            benchmark_model.train_benchmark()  # ensure a benchmark report exists to merge
            report = run_evaluation()

        self.assertEqual(report["results"]["model_role"], "production")
        self.assertIsNotNone(report["benchmark_baseline"])
        self.assertEqual(report["benchmark_baseline"]["model_role"], "benchmark")
        self.assertIn("validation", report["benchmark_baseline"]["results"])
        self.assertIn("test", report["benchmark_baseline"]["results"])
        self.assertIn("challenge", report["benchmark_baseline"]["results"])

    def test_did_not_regenerate_challenge_set_or_splits(self):
        """The challenge set and splits must not be regenerated by running
        the benchmark — their manifests/row counts should be identical
        before and after."""
        import evaluation.splits as splits_mod
        import evaluation.challenge_set as challenge_mod

        before_train = pd.read_csv(splits_mod.TRAIN_PATH)["feedback_text"].tolist()
        before_challenge = pd.read_csv(
            challenge_mod.CHALLENGE_SET_PATH, keep_default_na=False, na_values=[]
        )["feedback_text"].tolist()

        with isolated_training_artifacts():
            benchmark_model.train_benchmark()

        after_train = pd.read_csv(splits_mod.TRAIN_PATH)["feedback_text"].tolist()
        after_challenge = pd.read_csv(
            challenge_mod.CHALLENGE_SET_PATH, keep_default_na=False, na_values=[]
        )["feedback_text"].tolist()

        self.assertEqual(before_train, after_train)
        self.assertEqual(before_challenge, after_challenge)


if __name__ == "__main__":
    unittest.main()
