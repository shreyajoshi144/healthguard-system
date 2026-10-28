from __future__ import annotations
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import joblib

from evaluation import benchmark_model as benchmark_mod
from evaluation import semantic_model as semantic_mod
from evaluation import embeddings as embeddings_mod

MODEL_NAMES = ("benchmark", "semantic")


class FrozenModelHandle:
    name: str
    classes_: np.ndarray

    def features(self, texts: pd.Series) -> np.ndarray:
        raise NotImplementedError

    def proba(self, texts: pd.Series) -> np.ndarray:
        raise NotImplementedError


class _BenchmarkHandle(FrozenModelHandle):
    def __init__(self):
        self._vectorizer = joblib.load(benchmark_mod.BENCHMARK_VECTORIZER_PATH)
        self._clf = joblib.load(benchmark_mod.BENCHMARK_MODEL_PATH)
        self.name = "benchmark"
        self.classes_ = self._clf.classes_

    def features(self, texts: pd.Series):
        return self._vectorizer.transform(texts.fillna("").astype(str))

    def proba(self, texts: pd.Series) -> np.ndarray:
        return self._clf.predict_proba(self.features(texts))


class _SemanticHandle(FrozenModelHandle):
    def __init__(self):
        self._clf = joblib.load(semantic_mod.SEMANTIC_CLASSIFIER_PATH)
        self.name = "semantic"
        self.classes_ = self._clf.classes_

    def features(self, texts: pd.Series) -> np.ndarray:
        return embeddings_mod.embed_texts(texts)

    def proba(self, texts: pd.Series) -> np.ndarray:
        return self._clf.predict_proba(self.features(texts))


_handle_cache: dict[str, FrozenModelHandle] = {}


def get_handle(model_name: str) -> FrozenModelHandle:
    """Returns a cached, read-only handle to a frozen model's fitted
    vectorizer/classifier. model_name is 'benchmark' or 'semantic'."""
    if model_name not in _handle_cache:
        if model_name == "benchmark":
            _handle_cache[model_name] = _BenchmarkHandle()
        elif model_name == "semantic":
            _handle_cache[model_name] = _SemanticHandle()
        else:
            raise ValueError(f"Unknown model_name '{model_name}'. Expected one of {MODEL_NAMES}.")
    return _handle_cache[model_name]


def predict_proba(model_name: str, texts: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Returns (proba_matrix, classes_) for the given frozen model."""
    handle = get_handle(model_name)
    return handle.proba(texts), handle.classes_


def features_for(model_name: str, texts: pd.Series):
    """Returns the raw feature representation (TF-IDF sparse matrix for
    'benchmark', dense embedding array for 'semantic') — used by
    ood_detection.py, which needs the feature space itself, not just
    class probabilities."""
    handle = get_handle(model_name)
    return handle.features(texts)
