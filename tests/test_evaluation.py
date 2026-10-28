"""
tests/test_evaluation.py
────────────────────────────
PHASE 3A — tests for the evaluation/data-foundation package.

Uses stdlib `unittest` only (no new test-framework dependency). Run with:

    python -m unittest tests.test_evaluation -v

or, to run everything under tests/:

    python -m unittest discover -s tests -v

These tests do NOT retrain the baseline model. Tests that need a trained
model (TestEvaluatePipeline) are skipped automatically if
models/issue_classifier/ hasn't been trained yet (run
`python training/train_models.py` first) — they load the persisted
artifacts read-only, exactly like evaluation/evaluate.py does.
"""

from __future__ import annotations
import unittest
import tempfile
import shutil
from pathlib import Path

import pandas as pd

from evaluation import data_contract
from evaluation import leakage_checks
from evaluation import multilabel_eval
from evaluation.challenge_set import CHALLENGE_RECORDS, generate_challenge_set
from evaluation.data_contract import ALLOWED_ISSUE_CATEGORIES
from tests._artifact_isolation import isolated_training_artifacts


class TestDataContract(unittest.TestCase):

    def _valid_record(self) -> dict:
        return {
            "feedback_text": "The wait was far too long.",
            "primary_issue": "Wait Time",
            "secondary_issues": [],
            "sentiment": "Negative",
            "severity": "High",
            "requires_human_review": False,
            "dataset_version": "test-v1",
        }

    def test_valid_record_passes(self):
        errors = data_contract.validate_record(self._valid_record())
        self.assertEqual(errors, [])

    def test_missing_required_field_fails(self):
        rec = self._valid_record()
        del rec["severity"]
        errors = data_contract.validate_record(rec)
        self.assertTrue(any("severity" in e for e in errors))

    def test_unknown_primary_issue_fails(self):
        rec = self._valid_record()
        rec["primary_issue"] = "Not A Real Category"
        errors = data_contract.validate_record(rec)
        self.assertTrue(any("primary_issue" in e for e in errors))

    def test_unknown_sentiment_fails(self):
        rec = self._valid_record()
        rec["sentiment"] = "Ecstatic"
        errors = data_contract.validate_record(rec)
        self.assertTrue(any("sentiment" in e for e in errors))

    def test_unknown_severity_fails(self):
        rec = self._valid_record()
        rec["severity"] = "Extreme"
        errors = data_contract.validate_record(rec)
        self.assertTrue(any("severity" in e for e in errors))

    def test_primary_issue_duplicated_in_secondary_fails(self):
        rec = self._valid_record()
        rec["secondary_issues"] = ["Wait Time"]
        errors = data_contract.validate_record(rec)
        self.assertTrue(any("must not also appear" in e for e in errors))

    def test_unknown_secondary_issue_fails(self):
        rec = self._valid_record()
        rec["secondary_issues"] = ["Not A Real Category"]
        errors = data_contract.validate_record(rec)
        self.assertTrue(any("unknown categories" in e for e in errors))

    def test_empty_feedback_text_fails(self):
        rec = self._valid_record()
        rec["feedback_text"] = "   "
        errors = data_contract.validate_record(rec)
        self.assertTrue(any("feedback_text" in e for e in errors))

    def test_secondary_issues_encode_decode_roundtrip(self):
        original = ["Billing", "Communication"]
        encoded = data_contract.encode_secondary_issues(original)
        self.assertEqual(encoded, "Billing;Communication")
        decoded = data_contract.decode_secondary_issues(encoded)
        self.assertEqual(decoded, original)

    def test_secondary_issues_decode_handles_empty_and_nan(self):
        self.assertEqual(data_contract.decode_secondary_issues(""), [])
        self.assertEqual(data_contract.decode_secondary_issues(float("nan")), [])
        self.assertEqual(data_contract.decode_secondary_issues(None), [])

    def test_validate_dataframe_summary(self):
        df = pd.DataFrame([self._valid_record(), {**self._valid_record(), "severity": "Bogus"}])
        summary = data_contract.validate_dataframe(df)
        self.assertEqual(summary["n_rows"], 2)
        self.assertEqual(summary["n_valid"], 1)
        self.assertEqual(summary["n_invalid"], 1)


class TestLeakageChecks(unittest.TestCase):

    def test_column_leakage_detected(self):
        leaked = leakage_checks.check_column_leakage(
            feature_columns=["feedback_text", "priority_score"],
            target_columns=leakage_checks.TARGET_DERIVED_COLUMNS,
        )
        self.assertEqual(leaked, ["priority_score"])

    def test_column_leakage_clean_passes(self):
        leaked = leakage_checks.check_column_leakage(
            feature_columns=["feedback_text"],
            target_columns=leakage_checks.TARGET_DERIVED_COLUMNS,
        )
        self.assertEqual(leaked, [])

    def test_assert_no_column_leakage_raises_on_leak(self):
        with self.assertRaises(ValueError):
            leakage_checks.assert_no_column_leakage(["feedback_text", "issue_category"])

    def test_assert_baseline_feature_columns_allowed_rejects_extra_columns(self):
        with self.assertRaises(ValueError):
            leakage_checks.assert_baseline_feature_columns_allowed(["feedback_text", "department"])

    def test_assert_baseline_feature_columns_allowed_passes_for_text_only(self):
        leakage_checks.assert_baseline_feature_columns_allowed(["feedback_text"])  # should not raise

    def test_split_disjointness_detects_overlap(self):
        splits = {
            "train": pd.Series(["The wait was long.", "Great staff."]),
            "test": pd.Series(["The wait was long.", "Totally different text."]),
        }
        report = leakage_checks.check_split_disjointness(splits)
        self.assertTrue(report["any_overlap"])
        self.assertEqual(report["pairs"]["train_vs_test"]["n_overlap"], 1)

    def test_split_disjointness_passes_when_clean(self):
        splits = {
            "train": pd.Series(["The wait was long.", "Great staff."]),
            "test": pd.Series(["Totally unrelated text.", "Another distinct sentence."]),
        }
        report = leakage_checks.check_split_disjointness(splits)
        self.assertFalse(report["any_overlap"])

    def test_assert_no_split_leakage_raises_on_overlap(self):
        splits = {
            "train": pd.Series(["Duplicate text here."]),
            "challenge": pd.Series(["Duplicate text here."]),
        }
        with self.assertRaises(ValueError):
            leakage_checks.assert_no_split_leakage(splits)

    def test_normalize_text_catches_formatting_variants(self):
        a = leakage_checks._normalize_text("The Wait Was Long.")
        b = leakage_checks._normalize_text("the wait was long")
        self.assertEqual(a, b)


class TestChallengeSet(unittest.TestCase):

    def test_all_records_pass_contract_validation(self):
        rows = generate_challenge_set()  # raises internally if any record is invalid
        self.assertEqual(len(rows), len(CHALLENGE_RECORDS))

    def test_all_seven_challenge_types_present(self):
        required_types = {
            "paraphrase", "ambiguous", "multi_issue", "low_information",
            "unfamiliar_wording", "out_of_domain", "edge_case",
        }
        present_types = {r.challenge_type for r in CHALLENGE_RECORDS}
        self.assertTrue(required_types.issubset(present_types))

    def test_no_duplicate_text_within_challenge_set(self):
        normalized = [leakage_checks._normalize_text(r.feedback_text) for r in CHALLENGE_RECORDS]
        self.assertEqual(len(normalized), len(set(normalized)), "challenge set contains duplicate text")

    def test_every_primary_issue_is_allowed_category(self):
        for r in CHALLENGE_RECORDS:
            self.assertIn(r.primary_issue, ALLOWED_ISSUE_CATEGORIES)

    def test_multi_issue_examples_have_secondary_issues(self):
        multi_issue_records = [r for r in CHALLENGE_RECORDS if r.challenge_type == "multi_issue"]
        self.assertTrue(len(multi_issue_records) > 0)
        for r in multi_issue_records:
            self.assertTrue(len(r.secondary_issues) >= 1, f"multi_issue record has no secondary_issues: {r.feedback_text}")


class TestMultilabelEval(unittest.TestCase):

    def test_perfect_predictions_score_perfectly(self):
        y_true = [["Wait Time"], ["Billing", "Communication"]]
        y_pred = [["Wait Time"], ["Billing", "Communication"]]
        result = multilabel_eval.evaluate_multilabel(y_true, y_pred, all_labels=ALLOWED_ISSUE_CATEGORIES)
        self.assertEqual(result["subset_accuracy"], 1.0)
        self.assertEqual(result["hamming_loss"], 0.0)

    def test_completely_wrong_predictions_score_zero_subset_accuracy(self):
        y_true = [["Wait Time"]]
        y_pred = [["Billing"]]
        result = multilabel_eval.evaluate_multilabel(y_true, y_pred, all_labels=ALLOWED_ISSUE_CATEGORIES)
        self.assertEqual(result["subset_accuracy"], 0.0)
        self.assertGreater(result["hamming_loss"], 0.0)

    def test_scaffold_notice_present(self):
        result = multilabel_eval.evaluate_multilabel(
            [["Wait Time"]], [["Wait Time"]], all_labels=ALLOWED_ISSUE_CATEGORIES
        )
        self.assertIn("scaffold_notice", result)
        self.assertIn("not a real multi-label model result", result["scaffold_notice"])

    def test_mismatched_lengths_raises(self):
        with self.assertRaises(ValueError):
            multilabel_eval.evaluate_multilabel([["Wait Time"]], [], all_labels=ALLOWED_ISSUE_CATEGORIES)


class TestSplitsReproducibility(unittest.TestCase):
    """Uses a small temp CSV so this test doesn't depend on
    data/sample_patient_survey.csv already existing, and runs fast."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.csv_path = self.tmpdir / "toy.csv"
        rows = []
        # 3 unique texts per category x 8 categories = enough for a stratified split
        categories = ["Wait Time", "Billing", "Cleanliness", "Communication"]
        for cat in categories:
            for i in range(8):
                rows.append({"feedback_text": f"{cat} example number {i}", "true_issue_category": cat})
        pd.DataFrame(rows).to_csv(self.csv_path, index=False)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_same_seed_produces_identical_split(self):
        from evaluation import splits as splits_mod

        tmp_splits_dir_a = self.tmpdir / "splits_a"
        tmp_splits_dir_b = self.tmpdir / "splits_b"

        for target_dir in (tmp_splits_dir_a, tmp_splits_dir_b):
            original_dir = splits_mod.SPLITS_DIR
            original_train = splits_mod.TRAIN_PATH
            original_val = splits_mod.VAL_PATH
            original_test = splits_mod.TEST_PATH
            original_manifest = splits_mod.MANIFEST_PATH
            try:
                splits_mod.SPLITS_DIR = target_dir
                splits_mod.TRAIN_PATH = target_dir / "train.csv"
                splits_mod.VAL_PATH = target_dir / "val.csv"
                splits_mod.TEST_PATH = target_dir / "test.csv"
                splits_mod.MANIFEST_PATH = target_dir / "split_manifest.json"
                splits_mod.build_splits(source_csv=self.csv_path, random_state=42, force=True)
            finally:
                splits_mod.SPLITS_DIR = original_dir
                splits_mod.TRAIN_PATH = original_train
                splits_mod.VAL_PATH = original_val
                splits_mod.TEST_PATH = original_test
                splits_mod.MANIFEST_PATH = original_manifest

        train_a = pd.read_csv(tmp_splits_dir_a / "train.csv")["feedback_text"].tolist()
        train_b = pd.read_csv(tmp_splits_dir_b / "train.csv")["feedback_text"].tolist()
        self.assertEqual(sorted(train_a), sorted(train_b))

    def test_splits_are_disjoint(self):
        from evaluation import splits as splits_mod

        target_dir = self.tmpdir / "splits_disjoint"
        original_dir = splits_mod.SPLITS_DIR
        original_train = splits_mod.TRAIN_PATH
        original_val = splits_mod.VAL_PATH
        original_test = splits_mod.TEST_PATH
        original_manifest = splits_mod.MANIFEST_PATH
        try:
            splits_mod.SPLITS_DIR = target_dir
            splits_mod.TRAIN_PATH = target_dir / "train.csv"
            splits_mod.VAL_PATH = target_dir / "val.csv"
            splits_mod.TEST_PATH = target_dir / "test.csv"
            splits_mod.MANIFEST_PATH = target_dir / "split_manifest.json"
            splits_mod.build_splits(source_csv=self.csv_path, random_state=42, force=True)
            loaded = {
                "train": pd.read_csv(splits_mod.TRAIN_PATH)["feedback_text"],
                "val": pd.read_csv(splits_mod.VAL_PATH)["feedback_text"],
                "test": pd.read_csv(splits_mod.TEST_PATH)["feedback_text"],
            }
        finally:
            splits_mod.SPLITS_DIR = original_dir
            splits_mod.TRAIN_PATH = original_train
            splits_mod.VAL_PATH = original_val
            splits_mod.TEST_PATH = original_test
            splits_mod.MANIFEST_PATH = original_manifest

        report = leakage_checks.check_split_disjointness(loaded)
        self.assertFalse(report["any_overlap"])


class TestEvaluatePipeline(unittest.TestCase):
    """End-to-end: only runs if a trained baseline model is present, since
    it loads the real persisted artifacts (read-only, no retraining) rather
    than mocking them out."""

    @classmethod
    def setUpClass(cls):
        from modules import ml_issue_classifier
        if not ml_issue_classifier.is_available():
            raise unittest.SkipTest(
                "No trained issue-classifier model found — run "
                "`python training/train_models.py` first to exercise this test."
            )

    def test_compute_classification_metrics_matches_expected_shape(self):
        from evaluation.evaluate import _compute_classification_metrics
        y_true = ["Wait Time", "Billing", "Wait Time", "Billing"]
        y_pred = ["Wait Time", "Wait Time", "Wait Time", "Billing"]
        metrics = _compute_classification_metrics(y_true, y_pred)
        self.assertIn("accuracy", metrics)
        self.assertIn("confusion_matrix", metrics)
        self.assertIn("per_class_report", metrics)
        self.assertAlmostEqual(metrics["accuracy"], 0.75)

    def test_model_files_unchanged_after_evaluation(self):
        """Evaluation must be read-only: hash the persisted model files
        before and after running the full evaluation and confirm no
        retraining occurred."""
        import hashlib
        from evaluation.evaluate import run_evaluation
        from modules import ml_issue_classifier

        def _hash(path):
            h = hashlib.sha256()
            with open(path, "rb") as f:
                h.update(f.read())
            return h.hexdigest()

        before = {
            "model": _hash(ml_issue_classifier.MODEL_PATH),
            "vectorizer": _hash(ml_issue_classifier.VECTORIZER_PATH),
        }
        with isolated_training_artifacts():
            run_evaluation()
        after = {
            "model": _hash(ml_issue_classifier.MODEL_PATH),
            "vectorizer": _hash(ml_issue_classifier.VECTORIZER_PATH),
        }
        self.assertEqual(before, after, "Model artifacts changed during evaluation — evaluation must be read-only.")

    def test_full_report_has_required_top_level_sections(self):
        from evaluation.evaluate import run_evaluation
        with isolated_training_artifacts():
            report = run_evaluation()
        self.assertEqual(report["phase"], "3A")
        for key in ("reproducibility", "data", "results"):
            self.assertIn(key, report)
        for key in ("test_split", "challenge_set", "challenge_set_by_type", "multilabel_scaffold"):
            self.assertIn(key, report["results"])

    def test_report_challenge_metrics_reported_separately_from_test_split(self):
        from evaluation.evaluate import run_evaluation
        with isolated_training_artifacts():
            report = run_evaluation()
        test_acc = report["results"]["test_split"]["accuracy"]
        challenge_acc = report["results"]["challenge_set"]["accuracy"]
        # Not asserting a specific value (that would be gaming the test to
        # match whatever the model currently does) — just that the two are
        # computed and reported independently, which is the actual Phase 3A
        # requirement.
        self.assertIsInstance(test_acc, float)
        self.assertIsInstance(challenge_acc, float)


if __name__ == "__main__":
    unittest.main()
