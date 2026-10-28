"""
utils/config.py
───────────────
Central configuration constants for the Patient Satisfaction platform.
"""

from pathlib import Path

# ── Rating column name candidates ─────────────────────────────────────────────
RATING_CANDIDATES = ["overall_rating", "rating", "satisfaction_score", "score"]

# ── ML: reproducibility ────────────────────────────────────────────────────────
RANDOM_SEED = 42

# ── ML: model persistence paths ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "models"
ISSUE_MODEL_DIR = MODELS_DIR / "issue_classifier"
SATISFACTION_MODEL_DIR = MODELS_DIR / "satisfaction_model"

# ── ML: issue classifier ───────────────────────────────────────────────────────
# Below this confidence the pipeline falls back to the rule-based classifier.
ISSUE_ML_CONFIDENCE_THRESHOLD = 0.45

# ── Text column name candidates ────────────────────────────────────────────────
TEXT_CANDIDATES   = ["feedback_text", "clean_text", "comments", "feedback",
                     "review", "text", "response", "patient_comment"]

# ── Priority scoring thresholds ────────────────────────────────────────────────
PRIORITY_CRITICAL = 7.0
PRIORITY_HIGH     = 5.0
PRIORITY_MEDIUM   = 3.0

# ── Sentiment thresholds (VADER compound) ────────────────────────────────────
SENTIMENT_POSITIVE_THRESHOLD = 0.05
SENTIMENT_NEGATIVE_THRESHOLD = -0.05

# ── Issue categories ───────────────────────────────────────────────────────────
CRITICAL_CATEGORIES = ["Treatment Quality", "Billing", "Cleanliness"]

# ── NPS rating thresholds (1-5 scale) ─────────────────────────────────────────
NPS_PROMOTER_MIN  = 4
NPS_DETRACTOR_MAX = 2

# ── Paths ──────────────────────────────────────────────────────────────────────
DATA_DIR       = "data"
PROCESSED_DIR  = "data/processed"
LOG_DIR        = "logs"
ASSETS_DIR     = "assets"

# ── Phase 3A: evaluation / data-contract paths ──────────────────────────────────
SPLITS_DIR = PROJECT_ROOT / "data" / "splits"
CHALLENGE_SET_PATH = PROJECT_ROOT / "data" / "challenge_set.csv"
EVAL_RESULTS_DIR = PROJECT_ROOT / "evaluation" / "results"

# Bumped only when the underlying labelled sample dataset or challenge set is
# regenerated/changed, so evaluation reports can be tied back to the exact
# data that produced them (see evaluation/data_contract.py).
SAMPLE_DATASET_VERSION = "sample-v1.0"
CHALLENGE_DATASET_VERSION = "challenge-v1.0"
