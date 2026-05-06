import pandas as pd
import re
from utils.logger import get_logger

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
    text_col = _find_text_column(df)
    if text_col is None:
        df["issue_category"] = "Uncategorized"
        return df
    df["issue_category"] = df[text_col].apply(_classify_text)
    logger.info("Issue categorisation complete. Top category: %s",
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