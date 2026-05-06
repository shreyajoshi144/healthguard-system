import pandas as pd
import numpy as np
import re
from utils.logger import get_logger
import nltk
from nltk.corpus import stopwords
from nltk.stem import WordNetLemmatizer

NLTK_RESOURCES = [
    ("corpora/stopwords", "stopwords"),
    ("tokenizers/punkt", "punkt"),
    ("corpora/wordnet", "wordnet"),
]

for path, resource in NLTK_RESOURCES:
    try:
        nltk.data.find(path)
    except LookupError:
        nltk.download(resource, quiet=True)
logger = get_logger(__name__)
STOP_WORDS = set(stopwords.words("english"))
lemmatizer = WordNetLemmatizer()

def clean_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""
    text = text.lower()
    text = re.sub(r"http\S+|www\S+", "", text)
    text = re.sub(r"[^a-z\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    tokens = text.split()
    tokens = [lemmatizer.lemmatize(t) for t in tokens if t not in STOP_WORDS and len(t) > 2]
    return " ".join(tokens)

def preprocess_data(df: pd.DataFrame) -> pd.DataFrame:
    text_col = _find_text_column(df)
    if text_col:
        df["clean_text"] = df[text_col].apply(clean_text)
        logger.info("Text preprocessing applied to column: %s", text_col)

    cat_cols = [c for c in df.select_dtypes(include="object").columns
                if c not in [text_col, "clean_text", "department"] and df[c].nunique() < 30]
    if cat_cols:
        df = pd.get_dummies(df, columns=cat_cols, drop_first=False, dtype=int)

    return df

def _find_text_column(df: pd.DataFrame):
    candidates = ["feedback_text", "comments", "feedback", "review",
                  "text", "response", "patient_comment"]
    for c in candidates:
        if c in df.columns:
            return c
    obj_cols = df.select_dtypes(include="object").columns.tolist()
    if obj_cols:
        avg_len = {c: df[c].dropna().astype(str).str.len().mean() for c in obj_cols}
        return max(avg_len, key=avg_len.get)
    return None
