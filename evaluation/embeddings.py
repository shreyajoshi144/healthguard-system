from __future__ import annotations
import time
import numpy as np
import pandas as pd

EMBEDDING_MODEL_NAME = "en_core_web_md"
EMBEDDING_MODEL_VERSION = "3.8.0"
EMBEDDING_MODEL_SOURCE = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_md-3.8.0/en_core_web_md-3.8.0-py3-none-any.whl"
)
EMBEDDING_DIM = 300
SPACY_DISABLED_COMPONENTS = [
    "tok2vec", "tagger", "parser", "attribute_ruler", "lemmatizer", "ner",
]

_nlp_cache = None


def load_embedder():
    """Load (and cache) the spaCy pipeline used purely for vector lookup.
    Raises a clear error if the model package isn't installed — this
    project does not vendor the model weights; they're a pip-installed
    dependency (see requirements.txt / README install step), the same
    pattern already used for NLTK's downloaded corpora in modules/
    preprocessor.py."""
    global _nlp_cache
    if _nlp_cache is None:
        try:
            import spacy
        except ImportError as exc:
            raise ImportError(
                "spaCy is not installed. Run `pip install spacy` then "
                f"`pip install {EMBEDDING_MODEL_SOURCE}` "
                "(en_core_web_md is distributed as a GitHub release wheel, "
                "not a normal PyPI package name)."
            ) from exc
        try:
            _nlp_cache = spacy.load(EMBEDDING_MODEL_NAME, disable=SPACY_DISABLED_COMPONENTS)
        except OSError as exc:
            raise OSError(
                f"spaCy model '{EMBEDDING_MODEL_NAME}' is not installed. Install with: "
                f"pip install {EMBEDDING_MODEL_SOURCE}"
            ) from exc
    return _nlp_cache


def embed_texts(texts: pd.Series) -> np.ndarray:
    """Embed a batch of texts into an (n_samples, 300) float32 array via
    mean-pooled static word vectors. Deterministic (no randomness): the
    same text always produces the same vector for a fixed model version.
    """
    nlp = load_embedder()
    texts_clean = texts.fillna("").astype(str).tolist()
    vectors = np.zeros((len(texts_clean), EMBEDDING_DIM), dtype=np.float32)
    for i, doc in enumerate(nlp.pipe(texts_clean)):
        vectors[i] = doc.vector
    return vectors


def measure_embedding_latency(texts: pd.Series, n_repeats: int = 3) -> dict:
    """
    Measures embedding latency two ways:
      - per_sample: embed each text one at a time in a loop (worst case —
        e.g. a live single-feedback /api/analyze-style request)
      - batch: embed the whole series at once via nlp.pipe (best case —
        e.g. processing an uploaded CSV)
    Averaged over n_repeats runs to reduce noise. Returns milliseconds.
    """
    nlp = load_embedder()
    texts_clean = texts.fillna("").astype(str).tolist()

    # Per-sample (one at a time)
    per_sample_times = []
    for _ in range(n_repeats):
        start = time.perf_counter()
        for t in texts_clean:
            _ = nlp(t).vector
        elapsed = time.perf_counter() - start
        per_sample_times.append(elapsed / len(texts_clean))

    # Batch (nlp.pipe)
    batch_times = []
    for _ in range(n_repeats):
        start = time.perf_counter()
        for doc in nlp.pipe(texts_clean):
            _ = doc.vector
        elapsed = time.perf_counter() - start
        batch_times.append(elapsed)

    return {
        "n_texts": len(texts_clean),
        "n_repeats": n_repeats,
        "per_sample_ms_mean": round(float(np.mean(per_sample_times)) * 1000, 4),
        "batch_total_ms_mean": round(float(np.mean(batch_times)) * 1000, 4),
        "batch_per_example_ms_mean": round(float(np.mean(batch_times)) / len(texts_clean) * 1000, 4),
    }
