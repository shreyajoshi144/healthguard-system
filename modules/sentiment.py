import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import streamlit as st
from utils.logger import get_logger

logger = get_logger(__name__)
analyzer = SentimentIntensityAnalyzer()

def _vader_sentiment(text: str) -> dict:
    if not isinstance(text, str) or not text.strip():
        return {"compound": 0.0, "label": "Neutral"}
    scores = analyzer.polarity_scores(text)
    compound = scores["compound"]
    if compound >= 0.05:
        label = "Positive"
    elif compound <= -0.05:
        label = "Negative"
    else:
        label = "Neutral"
    return {"compound": round(compound, 4), "label": label}

def run_sentiment_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """Apply VADER sentiment to the feedback column."""
    text_col = _find_text_column(df)
    if text_col is None:
        st.warning("No text column found for sentiment analysis.")
        df["vader_compound"] = 0.0
        df["sentiment_label"] = "Neutral"
        return df

    results = df[text_col].apply(_vader_sentiment)
    df["vader_compound"]  = results.apply(lambda x: x["compound"])
    df["sentiment_label"] = results.apply(lambda x: x["label"])
    df["sentiment_score"] = df["vader_compound"].apply(
        lambda x: round((x + 1) / 2 * 5, 2)
    )
    logger.info("Sentiment analysis complete. Distribution: %s",
                df["sentiment_label"].value_counts().to_dict())
    return df

def _find_text_column(df):
    for c in ["feedback_text","clean_text","comments","feedback","review","text"]:
        if c in df.columns:
            return c
    obj_cols = df.select_dtypes(include="object").columns.tolist()
    if obj_cols:
        avg = {c: df[c].dropna().astype(str).str.len().mean() for c in obj_cols}
        return max(avg, key=avg.get)
    return None