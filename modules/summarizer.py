import pandas as pd
import streamlit as st
from utils.logger import get_logger
import nltk

NLTK_RESOURCES = [
    ("tokenizers/punkt", "punkt"),
]

for path, resource in NLTK_RESOURCES:
    try:
        nltk.data.find(path)
    except LookupError:
        nltk.download(resource, quiet=True)

logger = get_logger(__name__)
@st.cache_data(show_spinner=False)
def _summarize_text(text: str, sentences: int = 3) -> str:
    """Summarize text with deduplication to avoid repetition."""
    if not text or len(text.split()) < 20:
        return text or "No feedback available."
    try:
        from sumy.parsers.plaintext import PlaintextParser
        from sumy.nlp.tokenizers import Tokenizer
        from sumy.summarizers.lsa import LsaSummarizer
        
        parser = PlaintextParser.from_string(text, Tokenizer("english"))
        summarizer = LsaSummarizer()
        
        # Get more sentences than needed to allow for deduplication
        summary_sentences = summarizer(parser.document, sentences * 2)
        
        # Deduplicate sentences (remove exact duplicates and very similar ones)
        unique_sentences = []
        seen_sentences = set()
        
        for sentence in summary_sentences:
            sentence_str = str(sentence).strip()
            sentence_lower = sentence_str.lower()
            
            # Skip if we've seen this exact sentence
            if sentence_lower in seen_sentences:
                continue
            
            # Skip if very similar to existing sentences (simple check)
            is_duplicate = False
            for seen in seen_sentences:
                # Check if sentences are very similar (>80% overlap)
                words_current = set(sentence_lower.split())
                words_seen = set(seen.split())
                if len(words_current) > 0 and len(words_seen) > 0:
                    overlap = len(words_current & words_seen) / len(words_current | words_seen)
                    if overlap > 0.8:
                        is_duplicate = True
                        break
            
            if not is_duplicate:
                unique_sentences.append(sentence_str)
                seen_sentences.add(sentence_lower)
            
            # Stop when we have enough unique sentences
            if len(unique_sentences) >= sentences:
                break
        
        result = " ".join(unique_sentences)
        return result if result.strip() else text[:400] + "..."
        
    except Exception as e:
        logger.warning("Sumy summarisation failed: %s. Falling back to truncation.", e)
        # Fallback: take first few sentences manually
        sentences_list = text.split('. ')[:sentences]
        return '. '.join(sentences_list) + "." if sentences_list else text[:400] + "..."

def summarize_feedback(df: pd.DataFrame) -> dict:
    """Return one summary string per department."""
    summaries = {}
    text_col = _find_text_column(df)

    if text_col is None:
        return {"Overall": "No text column found."}

    if "department" not in df.columns:
        all_text = " ".join(df[text_col].dropna().astype(str).tolist())
        summaries["Overall"] = _summarize_text(all_text, 4)
        return summaries

    for dept in df["department"].dropna().unique():
        texts = df[df["department"] == dept][text_col].dropna().astype(str).tolist()
        combined = " ".join(texts)
        summaries[dept] = _summarize_text(combined, 3)

    logger.info("Feedback summarisation complete for %d departments.", len(summaries))
    return summaries

def _find_text_column(df):
    for c in ["feedback_text","comments","feedback","review","text"]:
        if c in df.columns:
            return c
    return None
