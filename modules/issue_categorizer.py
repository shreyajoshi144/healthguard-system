import pandas as pd
import re
from utils.logger import get_logger
from modules import ml_issue_classifier

logger = get_logger(__name__)

ISSUE_KEYWORDS = {
    "Wait Time":        ["wait", "long", "delay", "slow", "queue", "hour", "minutes"],
    "Staff Behavior":   ["rude", "unfriendly", "attitude", "unprofessional", "ignored",
                         "disrespectful", "helpful", "kind", "polite", "courteous"],
    "Cleanliness":      ["dirty", "clean", "hygiene", "smell", "messy", "sanitary",
                         "filthy", "tidy", "spotless"],
    "Communication":    ["explain", "told", "inform", "communicat", "understand",
                         "confus", "unclear", "notif"],
    "Treatment Quality":["treatment", "medicine", "doctor", "diagnosis", "care",
                         "incorrect", "wrong", "procedure", "surgery", "prescription"],
    "Billing":          ["bill", "cost", "insurance", "payment", "charge", "expensive",
                         "refund", "price"],
    "Facilities":       ["room", "bed", "equipment", "facility", "building", "parking",
                         "cafeteria", "restroom", "toilet"],
    "Appointment":      ["appointment", "schedule", "cancel", "reschedule", "booking",
                         "available", "slot"],
    "General Positive": ["excellent", "great", "amazing", "wonderful", "satisfied",
                         "happy", "good", "best", "perfect"],
}

def categorize_issues(df: pd.DataFrame) -> pd.DataFrame:
    """
    Categorise each row's feedback into an issue category.

    Primary path: the trained TF-IDF + Logistic Regression ML classifier
    (modules.ml_issue_classifier). Falls back to the rule-based keyword
    classifier per-row when:
      - the ML artifacts aren't available on disk (model not trained yet), or
      - the ML model's prediction confidence is below the configured
        threshold (utils.config.ISSUE_ML_CONFIDENCE_THRESHOLD), or
      - the text is empty (rule-based already returns "Uncategorized").
    """
    text_col = _find_text_column(df)
    if text_col is None:
        df["issue_category"] = "Uncategorized"
        df["issue_category_source"] = "none"
        return df

    rule_labels = df[text_col].apply(_classify_text)

    if ml_issue_classifier.is_available():
        try:
            ml_labels, confidences = ml_issue_classifier.predict(df[text_col])
            threshold = ml_issue_classifier.get_confidence_threshold()
            non_empty = df[text_col].astype(str).str.strip().ne("")
            use_ml = non_empty & (pd.Series(confidences, index=df.index) >= threshold)

            final_labels = rule_labels.copy()
            final_labels[use_ml] = pd.Series(ml_labels, index=df.index)[use_ml]

            df["issue_category"] = final_labels
            df["issue_category_confidence"] = pd.Series(confidences, index=df.index).round(4)
            df["issue_category_source"] = use_ml.map({True: "ml", False: "rule"})

            logger.info(
                "Issue categorisation complete (ML for %d/%d rows, rule fallback for %d). Top category: %s",
                int(use_ml.sum()), len(df), int((~use_ml).sum()),
                df["issue_category"].mode()[0] if len(df) > 0 else "N/A",
            )
            return df
        except Exception as exc:
            logger.exception("ML issue classifier failed, falling back to rule-based only: %s", exc)

    df["issue_category"] = rule_labels
    df["issue_category_source"] = "rule"
    logger.info("Issue categorisation complete (rule-based only — ML model unavailable). Top category: %s",
                df["issue_category"].mode()[0] if len(df) > 0 else "N/A")
    return df

def _classify_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return "Uncategorized"
    text_lower = text.lower()
    scores = {}
    for category, keywords in ISSUE_KEYWORDS.items():
        count = sum(1 for kw in keywords if re.search(r"\b" + kw, text_lower))
        if count > 0:
            scores[category] = count
    if scores:
        return max(scores, key=scores.get)
    return "Other"

def _find_text_column(df):
    for c in ["feedback_text","clean_text","comments","feedback","text"]:
        if c in df.columns:
            return c
    return None