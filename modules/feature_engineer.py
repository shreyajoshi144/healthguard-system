import pandas as pd
import numpy as np
from utils.logger import get_logger

logger = get_logger(__name__)

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Create derived features for modelling and visualisation."""
    if "clean_text" in df.columns:
        df["word_count"] = df["clean_text"].apply(lambda x: len(str(x).split()))
        df["char_count"] = df["clean_text"].apply(lambda x: len(str(x)))
        df["has_feedback"] = (df["word_count"] > 3).astype(int)

    rating_col = _find_rating_col(df)
    if rating_col:
        df["rating_band"] = pd.cut(
            df[rating_col],
            bins=[0, 2, 3, 4, 5],
            labels=["Poor", "Average", "Good", "Excellent"]
        )
        df["is_promoter"]  = (df[rating_col] >= 4).astype(int)
        df["is_detractor"] = (df[rating_col] <= 2).astype(int)
        df["nps_category"] = df[rating_col].apply(
            lambda x: "Promoter" if x >= 4 else ("Detractor" if x <= 2 else "Passive")
        )

    date_cols = df.select_dtypes(include="datetime64[ns]").columns.tolist()
    for dc in date_cols:
        df[f"{dc}_month"] = df[dc].dt.month
        df[f"{dc}_year"]  = df[dc].dt.year
        df[f"{dc}_dayofweek"] = df[dc].dt.dayofweek

    sub_ratings = [c for c in df.columns if "rating" in c and c != rating_col]
    numeric_sub = [c for c in sub_ratings if pd.api.types.is_numeric_dtype(df[c])]
    if numeric_sub:
        df["composite_score"] = df[numeric_sub].mean(axis=1).round(2)

    logger.info("Feature engineering complete. New features added.")
    return df

def _find_rating_col(df: pd.DataFrame):
    candidates = ["overall_rating", "rating", "satisfaction_score", "score"]
    for c in candidates:
        if c in df.columns and pd.api.types.is_numeric_dtype(df[c]):
            return c
    return None
