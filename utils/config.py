"""
utils/config.py
───────────────
Central configuration constants for the Patient Satisfaction platform.
"""

# ── Rating column name candidates ─────────────────────────────────────────────
RATING_CANDIDATES = ["overall_rating", "rating", "satisfaction_score", "score"]

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
