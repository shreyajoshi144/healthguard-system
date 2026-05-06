import pandas as pd
import numpy as np
from utils.logger import get_logger

logger = get_logger(__name__)

def score_priorities(df: pd.DataFrame) -> pd.DataFrame:
    """
    Priority = weighted combination of:
      - Inverted rating  (low rating → high priority)
      - Negative VADER score
      - Word count       (longer feedback = more serious)
      - Critical category flag
    Score range: 0–10
    """
    score = pd.Series(np.zeros(len(df)), index=df.index)

    rating_col = _find_rating_col(df)
    if rating_col:
        inv_rating = (6 - df[rating_col]).clip(1, 5)
        score += inv_rating * 1.5

    if "vader_compound" in df.columns:
        neg_score = (-df["vader_compound"] + 1) / 2 * 3
        score += neg_score

    if "word_count" in df.columns:
        wc_norm = (df["word_count"] / df["word_count"].max()).fillna(0) * 1.5
        score += wc_norm

    critical_categories = ["Treatment Quality", "Billing", "Cleanliness"]
    if "issue_category" in df.columns:
        score += df["issue_category"].isin(critical_categories).astype(float) * 1.5

    if score.max() > score.min():
        score = (score - score.min()) / (score.max() - score.min()) * 10

    df["priority_score"] = score.round(2)
    df["priority_label"] = df["priority_score"].apply(
        lambda x: "🔴 Critical" if x >= 7 else ("🟠 High" if x >= 5 else ("🟡 Medium" if x >= 3 else "🟢 Low"))
    )
    logger.info("Priority scoring complete. Critical cases: %d", (df["priority_score"] >= 7).sum())
    return df

def _find_rating_col(df):
    for c in ["overall_rating","rating","satisfaction_score","score"]:
        if c in df.columns and pd.api.types.is_numeric_dtype(df[c]):
            return c
    return None
