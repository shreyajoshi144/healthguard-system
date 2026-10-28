from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.request import Request
from ai.provider_utils import post_json, provider_timeout, validate_prompt_size
import json

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
_SYNONYMS = {
    "blood": "blood", "sugar": "glucose", "glucose": "glucose", "bloodsugar": "glucose",
    "haemoglobin": "hemoglobin", "hemoglobin": "hemoglobin", "hb": "hemoglobin",
    "medicine": "medication", "medicines": "medication", "drug": "medication", "drugs": "medication",
    "scan": "imaging", "mri": "imaging", "ct": "imaging", "test": "investigation", "tests": "investigation",
    "prescription": "medication", "dose": "dose", "dosage": "dose", "result": "result", "results": "result",
}

class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

@dataclass(frozen=True)
class DeterministicEmbeddingProvider:
    dimensions: int = 128

    def _features(self, text: str) -> list[str]:
        raw = [t.lower() for t in _TOKEN_RE.findall(text)]
        features: list[str] = []
        for token in raw:
            canonical = _SYNONYMS.get(token, token)
            features.append(canonical)
            if len(canonical) >= 4:
                features.extend(f"{canonical[i:i+3]}#" for i in range(len(canonical)-2))
        return features

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors=[]
        for text in texts:
            vec=[0.0]*self.dimensions
            for feature in self._features(text):
                digest=hashlib.sha256(feature.encode()).digest()
                idx=int.from_bytes(digest[:4],"big") % self.dimensions
                sign=1.0 if digest[4] & 1 else -1.0
                vec[idx]+=sign
            norm=math.sqrt(sum(x*x for x in vec)) or 1.0
            vectors.append([x/norm for x in vec])
        return vectors

class EmbeddingProviderConfigurationError(RuntimeError): pass
class EmbeddingProviderResponseError(RuntimeError): pass

class OpenAICompatibleEmbeddingProvider:
    def __init__(self, *, base_url:str, api_key:str, model:str, timeout:int|None=None):
        self.base_url=base_url.rstrip('/'); self.api_key=api_key; self.model=model; self.timeout=timeout or provider_timeout()
    @classmethod
    def from_environment(cls):
        base=os.getenv('HEALTHGUARD_EMBEDDING_BASE_URL','').strip()
        key=os.getenv('HEALTHGUARD_EMBEDDING_API_KEY','').strip()
        model=os.getenv('HEALTHGUARD_EMBEDDING_MODEL','').strip()
        if not base or not key or not model:
            raise EmbeddingProviderConfigurationError('HEALTHGUARD_EMBEDDING_BASE_URL, HEALTHGUARD_EMBEDDING_API_KEY, and HEALTHGUARD_EMBEDDING_MODEL are required.')
        return cls(base_url=base,api_key=key,model=model)
    def embed(self,texts:list[str])->list[list[float]]:
        if len(texts)>100: raise EmbeddingProviderResponseError('Embedding batch is too large.')
        for text in texts: validate_prompt_size(text, field='Embedding input')
        req=Request(f'{self.base_url}/embeddings',data=json.dumps({'model':self.model,'input':texts}).encode(),headers={'Authorization':f'Bearer {self.api_key}','Content-Type':'application/json'},method='POST')
        try:
            body=post_json(req)
            data=body['data']
            if not isinstance(data,list) or len(data)!=len(texts): raise ValueError('Embedding provider returned an invalid batch.')
            vectors=[list(map(float,x['embedding'])) for x in sorted(data,key=lambda y:y['index'])]
            if any(len(v)>4096 for v in vectors): raise ValueError('Embedding dimensions exceed the configured limit.')
            return vectors
        except Exception as exc: raise EmbeddingProviderResponseError('Embedding provider request failed.') from exc

def cosine_similarity(a:list[float],b:list[float])->float:
    if not a or not b or len(a)!=len(b): return 0.0
    return max(-1.0,min(1.0,sum(x*y for x,y in zip(a,b))))

def get_embedding_provider() -> EmbeddingProvider:
    mode=os.getenv('HEALTHGUARD_EMBEDDING_PROVIDER','deterministic').strip().lower()
    if mode=='deterministic': return DeterministicEmbeddingProvider()
    if mode=='openai_compatible': return OpenAICompatibleEmbeddingProvider.from_environment()
    raise EmbeddingProviderConfigurationError(f'Unsupported embedding provider: {mode}')
