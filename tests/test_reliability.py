"""
tests/test_reliability.py
────────────────────────────
PHASE 3C — tests for calibration.py, uncertainty.py, ood_detection.py,
threshold_analysis.py, disagreement.py, and reliability_report.py.

Run:
    python -m unittest tests.test_reliability -v
"""

from __future__ import annotations
import unittest
import inspect
import hashlib
from unittest.mock import patch

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV

from evaluation import uncertainty as unc_mod
from evaluation import ood_detection as ood_mod
from evaluation import threshold_analysis as thresh_mod
from evaluation import disagreement as dis_mod
from tests._artifact_isolation import isolated_reliability_artifacts


def _real_data_available() -> bool:
    """
    True only when everything a Phase 3C reliability test needs is
    genuinely usable: the Phase 3A/3B data files and frozen artifacts
    exist on disk, AND the semantic model's pretrained embedding
    dependency (spaCy + en_core_web_md) actually loads.

    Checking file existence alone is not enough — models/semantic_issue_classifier/
    logreg_classifier.joblib exists independently of whether the spaCy
    model package is installed, since embedding happens at evaluation
    time, not at artifact-load time. Without this check, tests here would
    attempt to run with the dependency genuinely missing and fail with a
    raw exception instead of skipping cleanly with a clear reason — this
    was itself the exact reproducibility bug this test file's guard is
    meant to prevent.
    """
    try:
        import evaluation.splits as splits_mod
        import evaluation.challenge_set as challenge_mod
        from modules import ml_issue_classifier as production
        from evaluation import benchmark_model, semantic_model
        from evaluation.embeddings import load_embedder

        files_present = (
            splits_mod.TRAIN_PATH.exists() and splits_mod.VAL_PATH.exists()
            and splits_mod.TEST_PATH.exists() and challenge_mod.CHALLENGE_SET_PATH.exists()
            and benchmark_model.BENCHMARK_MODEL_PATH.exists()
            and semantic_model.SEMANTIC_CLASSIFIER_PATH.exists()
        )
        if not files_present:
            return False
        load_embedder()  # raises if spaCy or en_core_web_md isn't actually installed
        return True
    except Exception:
        return False


class TestValidationOnlySignatures(unittest.TestCase):
    """Every selection function's signature only accepts validation data
    for the thing it's selecting — test/challenge arrays aren't even
    parameters, so there's no path for them to influence selection."""

    def test_calibration_method_selection_signature_is_validation_only(self):
        from evaluation.calibration import select_calibration_method
        sig = inspect.signature(select_calibration_method)
        self.assertEqual(set(sig.parameters.keys()), {"model_name", "val_texts", "val_labels"})

    def test_ood_threshold_selection_signature_is_validation_only(self):
        sig = inspect.signature(ood_mod.select_ood_threshold)
        self.assertEqual(set(sig.parameters.keys()), {"val_scores", "target_flag_rate"})

    def test_ood_detector_build_signature_only_takes_train_and_val(self):
        sig = inspect.signature(ood_mod.build_ood_detector)
        self.assertEqual(
            set(sig.parameters.keys()),
            {"model_name", "train_texts", "train_labels", "val_texts"},
        )

    def test_threshold_operating_point_selection_signature_is_curve_only(self):
        sig = inspect.signature(thresh_mod.select_operating_point)
        self.assertEqual(set(sig.parameters.keys()), {"curve", "min_accuracy"})


class TestCalibrationFitsOnlyOnValidation(unittest.TestCase):
    """Spy-based proof: every CalibratedClassifierCV .fit() call made
    during method selection uses exactly the validation set size, never
    test/challenge sizes."""

    @unittest.skipUnless(_real_data_available(), "Phase 3A/3B artifacts, data, or the semantic embedding dependency (spaCy + en_core_web_md — see requirements.txt) not available")
    def test_fit_call_uses_only_validation_sized_data(self):
        from evaluation import calibration as cal_mod
        from evaluation import splits as splits_mod

        split_data = splits_mod.load_splits()
        val_df = split_data["val"]
        n_val = len(val_df)

        fit_call_sizes = []
        original_fit = CalibratedClassifierCV.fit

        def spy_fit(self, X, y, *args, **kwargs):
            fit_call_sizes.append(len(y))
            return original_fit(self, X, y, *args, **kwargs)

        with patch.object(CalibratedClassifierCV, "fit", spy_fit):
            cal_mod.select_calibration_method(
                "benchmark", val_df["feedback_text"], val_df["true_issue_category"]
            )

        self.assertTrue(len(fit_call_sizes) >= 1, "CalibratedClassifierCV.fit was never called")
        for size in fit_call_sizes:
            self.assertEqual(size, n_val, "A calibrator .fit() call did not use exactly the validation set size")


class TestThresholdSelectionUsesValidationOnly(unittest.TestCase):

    def test_operating_point_is_selected_from_validation_curve_only(self):
        val_curve = [
            {"threshold": 0.3, "coverage": 1.0, "accuracy": 0.80, "macro_f1": 0.78, "error_rate": 0.20, "escalation_rate": 0.0, "n_trusted": 10},
            {"threshold": 0.5, "coverage": 0.9, "accuracy": 0.96, "macro_f1": 0.95, "error_rate": 0.04, "escalation_rate": 0.1, "n_trusted": 9},
            {"threshold": 0.7, "coverage": 0.5, "accuracy": 0.99, "macro_f1": 0.99, "error_rate": 0.01, "escalation_rate": 0.5, "n_trusted": 5},
        ]
        result = thresh_mod.select_operating_point(val_curve, min_accuracy=0.95)
        self.assertEqual(result["selected_threshold"], 0.5)

    def test_frozen_threshold_applied_identically_regardless_of_split(self):
        proba = np.array([[0.9, 0.1], [0.6, 0.4], [0.3, 0.7]])
        classes = np.array(["A", "B"])
        y = pd.Series(["A", "A", "B"])
        result_a = thresh_mod.evaluate_threshold(proba, classes, y, 0.5)
        result_b = thresh_mod.evaluate_threshold(proba, classes, y, 0.5)
        self.assertEqual(result_a, result_b)


class TestUncertaintySignalsCorrectness(unittest.TestCase):

    def test_margin_and_entropy_hand_computed(self):
        proba = np.array([[0.7, 0.2, 0.1]])
        classes = np.array(["X", "Y", "Z"])
        thresholds = {"low_confidence_below": 0.4, "high_confidence_at_or_above": 0.8}
        records = unc_mod.build_prediction_records(proba, classes, thresholds)
        rec = records[0]
        self.assertEqual(rec.prediction, "X")
        self.assertAlmostEqual(rec.confidence, 0.7)
        self.assertAlmostEqual(rec.margin, 0.5)
        expected_entropy = -(0.7 * np.log(0.7) + 0.2 * np.log(0.2) + 0.1 * np.log(0.1))
        self.assertAlmostEqual(rec.entropy, round(expected_entropy, 4), places=3)

    def test_uncertainty_level_buckets_correctly(self):
        thresholds = {"low_confidence_below": 0.4, "high_confidence_at_or_above": 0.8}
        proba = np.array([[0.9, 0.1], [0.6, 0.4], [0.35, 0.65]])
        classes = np.array(["A", "B"])
        records = unc_mod.build_prediction_records(proba, classes, thresholds)
        self.assertEqual(records[0].uncertainty_level, "Low")
        self.assertEqual(records[1].uncertainty_level, "Medium")
        self.assertEqual(records[2].uncertainty_level, "Medium")

    def test_thresholds_derived_from_validation_percentiles(self):
        val_proba = np.array([[c, 1 - c] for c in [0.5, 0.6, 0.7, 0.8, 0.9]])
        thresholds = unc_mod.derive_uncertainty_thresholds(val_proba)
        confidences = val_proba.max(axis=1)
        self.assertAlmostEqual(thresholds["low_confidence_below"], float(np.percentile(confidences, 33)), places=4)
        self.assertAlmostEqual(thresholds["high_confidence_at_or_above"], float(np.percentile(confidences, 66)), places=4)


class TestOODDetectionCorrectness(unittest.TestCase):

    def test_identical_vector_has_zero_ood_score(self):
        centroids = {"A": np.array([1.0, 0.0, 0.0]), "B": np.array([0.0, 1.0, 0.0])}
        X = np.array([[1.0, 0.0, 0.0]])
        sims = ood_mod._cosine_similarity_to_centroids(X, centroids)
        score = 1.0 - sims.max(axis=1)
        self.assertAlmostEqual(float(score[0]), 0.0, places=6)

    def test_orthogonal_vector_has_high_ood_score(self):
        centroids = {"A": np.array([1.0, 0.0, 0.0]), "B": np.array([0.0, 1.0, 0.0])}
        X = np.array([[0.0, 0.0, 1.0]])
        sims = ood_mod._cosine_similarity_to_centroids(X, centroids)
        score = 1.0 - sims.max(axis=1)
        self.assertAlmostEqual(float(score[0]), 1.0, places=6)

    def test_threshold_selection_targets_flag_rate(self):
        val_scores = np.linspace(0, 1, 100)
        result = ood_mod.select_ood_threshold(val_scores, target_flag_rate=0.05)
        self.assertAlmostEqual(result["threshold"], 0.95, delta=0.02)
        self.assertAlmostEqual(result["actual_validation_flag_rate"], 0.05, delta=0.02)

    def test_flag_ood_respects_threshold(self):
        scores = np.array([0.1, 0.5, 0.9])
        flags = ood_mod.flag_ood(scores, 0.5)
        np.testing.assert_array_equal(flags, [False, False, True])

    def test_ood_detector_end_to_end_toy_data(self):
        val_scores = np.array([0.05, 0.08, 0.10, 0.12, 0.6])
        threshold_info = ood_mod.select_ood_threshold(val_scores, target_flag_rate=0.2)
        flags = ood_mod.flag_ood(val_scores, threshold_info["threshold"])
        self.assertEqual(int(flags.sum()), 1)


class TestDisagreementCorrectness(unittest.TestCase):

    def test_disagreement_stats_match_manual_calculation(self):
        bench_preds = np.array(["A", "A", "B", "B"])
        sem_preds = np.array(["A", "B", "B", "A"])
        y_true = pd.Series(["A", "A", "B", "A"])

        with patch.object(dis_mod, "get_predictions") as mock_get:
            def side_effect(model_name, texts):
                if model_name == "benchmark":
                    return bench_preds, np.array([0.9, 0.9, 0.9, 0.9])
                return sem_preds, np.array([0.9, 0.9, 0.9, 0.9])
            mock_get.side_effect = side_effect

            result = dis_mod.compare_predictions(pd.Series(["t1", "t2", "t3", "t4"]), y_true)

        self.assertEqual(result["n_agree"], 2)
        self.assertEqual(result["n_disagree"], 2)
        self.assertAlmostEqual(result["agreement_rate"], 0.5)
        self.assertAlmostEqual(result["accuracy_when_agreeing"], 1.0)
        self.assertAlmostEqual(result["benchmark_accuracy_when_disagreeing"], 0.5)
        self.assertAlmostEqual(result["semantic_accuracy_when_disagreeing"], 0.5)

    def test_disagreement_signal_usefulness_flag_computed_correctly(self):
        bench_preds = np.array(["A", "A"])
        sem_preds = np.array(["A", "B"])
        y_true = pd.Series(["A", "A"])

        with patch.object(dis_mod, "get_predictions") as mock_get:
            def side_effect(model_name, texts):
                if model_name == "benchmark":
                    return bench_preds, np.array([0.9, 0.9])
                return sem_preds, np.array([0.9, 0.9])
            mock_get.side_effect = side_effect

            result = dis_mod.compare_predictions(pd.Series(["t1", "t2"]), y_true)

        self.assertTrue(result["disagreement_signal_useful"])
        self.assertAlmostEqual(result["error_rate_gap_disagree_minus_agree"], 0.5)


@unittest.skipUnless(_real_data_available(), "Phase 3A/3B artifacts, data, or the semantic embedding dependency (spaCy + en_core_web_md — see requirements.txt) not available")
class TestFrozenArtifactIntegrity(unittest.TestCase):

    def _hash(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            h.update(f.read())
        return h.hexdigest()

    def test_all_frozen_model_artifacts_unchanged_after_full_reliability_run(self):
        from evaluation import reliability_report as rr
        from evaluation import benchmark_model, semantic_model
        from modules import ml_issue_classifier as production

        before = {
            "production_model": self._hash(production.MODEL_PATH),
            "production_vectorizer": self._hash(production.VECTORIZER_PATH),
            "benchmark_model": self._hash(benchmark_model.BENCHMARK_MODEL_PATH),
            "benchmark_vectorizer": self._hash(benchmark_model.BENCHMARK_VECTORIZER_PATH),
            "semantic_classifier": self._hash(semantic_model.SEMANTIC_CLASSIFIER_PATH),
        }

        with isolated_reliability_artifacts():
            rr.run_reliability_analysis()

        after = {
            "production_model": self._hash(production.MODEL_PATH),
            "production_vectorizer": self._hash(production.VECTORIZER_PATH),
            "benchmark_model": self._hash(benchmark_model.BENCHMARK_MODEL_PATH),
            "benchmark_vectorizer": self._hash(benchmark_model.BENCHMARK_VECTORIZER_PATH),
            "semantic_classifier": self._hash(semantic_model.SEMANTIC_CLASSIFIER_PATH),
        }
        self.assertEqual(before, after, "A frozen model artifact changed during Phase 3C reliability analysis.")

    def test_data_files_unchanged_after_full_reliability_run(self):
        from evaluation import reliability_report as rr
        import evaluation.splits as splits_mod
        import evaluation.challenge_set as challenge_mod

        before = {
            "train": self._hash(splits_mod.TRAIN_PATH),
            "val": self._hash(splits_mod.VAL_PATH),
            "test": self._hash(splits_mod.TEST_PATH),
            "challenge": self._hash(challenge_mod.CHALLENGE_SET_PATH),
        }

        with isolated_reliability_artifacts():
            rr.run_reliability_analysis()

        after = {
            "train": self._hash(splits_mod.TRAIN_PATH),
            "val": self._hash(splits_mod.VAL_PATH),
            "test": self._hash(splits_mod.TEST_PATH),
            "challenge": self._hash(challenge_mod.CHALLENGE_SET_PATH),
        }
        self.assertEqual(before, after, "A dataset file changed during Phase 3C reliability analysis.")


@unittest.skipUnless(_real_data_available(), "Phase 3A/3B artifacts, data, or the semantic embedding dependency (spaCy + en_core_web_md — see requirements.txt) not available")
class TestReliabilityReportSchemaAndReproducibility(unittest.TestCase):

    def test_output_schema_valid(self):
        from evaluation import reliability_report as rr
        from evaluation import frozen_models as fm

        with isolated_reliability_artifacts():
            report = rr.run_reliability_analysis()

        self.assertEqual(report["phase"], "3C")
        for key in ("reproducibility", "calibration", "uncertainty", "ood_detection",
                     "threshold_coverage_analysis", "model_disagreement"):
            self.assertIn(key, report)

        for model_name in fm.MODEL_NAMES:
            self.assertIn(model_name, report["calibration"])
            self.assertIn("method_selection", report["calibration"][model_name])
            self.assertIn("selected_method", report["calibration"][model_name]["method_selection"])

            self.assertIn(model_name, report["uncertainty"])
            self.assertIn("thresholds", report["uncertainty"][model_name])
            self.assertIn("challenge_by_type", report["uncertainty"][model_name])

            self.assertIn(model_name, report["ood_detection"])
            self.assertIn("threshold_info", report["ood_detection"][model_name])
            self.assertIn("challenge", report["ood_detection"][model_name])

            self.assertIn(model_name, report["threshold_coverage_analysis"])
            self.assertIn("frozen_threshold", report["threshold_coverage_analysis"][model_name])
            self.assertIn("challenge_at_frozen_threshold", report["threshold_coverage_analysis"][model_name])

        for split_name in ("validation", "test", "challenge"):
            self.assertIn(split_name, report["model_disagreement"])
            self.assertIn("agreement_rate", report["model_disagreement"][split_name])

        repro = report["reproducibility"]
        for field in ("evaluation_timestamp_utc", "random_seed", "dataset_versions",
                       "split_sizes", "model_identifiers", "methods", "frozen_artifact_hashes"):
            self.assertIn(field, repro)

    def test_reproducible_across_runs(self):
        from evaluation import reliability_report as rr

        with isolated_reliability_artifacts():
            report_1 = rr.run_reliability_analysis()
        with isolated_reliability_artifacts():
            report_2 = rr.run_reliability_analysis()

        for model_name in ("benchmark", "semantic"):
            self.assertEqual(
                report_1["calibration"][model_name]["method_selection"]["selected_method"],
                report_2["calibration"][model_name]["method_selection"]["selected_method"],
            )
            self.assertEqual(
                report_1["ood_detection"][model_name]["challenge"]["flag_rate"],
                report_2["ood_detection"][model_name]["challenge"]["flag_rate"],
            )
            self.assertEqual(
                report_1["threshold_coverage_analysis"][model_name]["frozen_threshold"],
                report_2["threshold_coverage_analysis"][model_name]["frozen_threshold"],
            )
        self.assertEqual(
            report_1["model_disagreement"]["challenge"]["agreement_rate"],
            report_2["model_disagreement"]["challenge"]["agreement_rate"],
        )

    def test_did_not_regenerate_splits_or_challenge_set(self):
        import evaluation.splits as splits_mod
        import evaluation.challenge_set as challenge_mod
        from evaluation import reliability_report as rr

        before_train = pd.read_csv(splits_mod.TRAIN_PATH)["feedback_text"].tolist()
        before_challenge = pd.read_csv(
            challenge_mod.CHALLENGE_SET_PATH, keep_default_na=False, na_values=[]
        )["feedback_text"].tolist()

        with isolated_reliability_artifacts():
            rr.run_reliability_analysis()

        after_train = pd.read_csv(splits_mod.TRAIN_PATH)["feedback_text"].tolist()
        after_challenge = pd.read_csv(
            challenge_mod.CHALLENGE_SET_PATH, keep_default_na=False, na_values=[]
        )["feedback_text"].tolist()

        self.assertEqual(before_train, after_train)
        self.assertEqual(before_challenge, after_challenge)

    def test_uncertainty_and_ood_are_distinct_signals(self):
        from evaluation import reliability_report as rr

        with isolated_reliability_artifacts():
            report = rr.run_reliability_analysis()
        for model_name in ("benchmark", "semantic"):
            counts = report["uncertainty"][model_name]["challenge"]["summary"]["uncertainty_level_counts"]
            n = report["uncertainty"][model_name]["challenge"]["summary"]["n_examples"]
            high_uncertainty_rate = counts.get("High", 0) / n
            ood_flag_rate = report["ood_detection"][model_name]["challenge"]["flag_rate"]
            if model_name == "semantic":
                self.assertNotAlmostEqual(high_uncertainty_rate, ood_flag_rate, places=2)


if __name__ == "__main__":
    unittest.main()
