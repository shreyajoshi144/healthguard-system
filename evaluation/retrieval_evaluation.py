from __future__ import annotations

import json
from pathlib import Path
from ai.embedding_provider import DeterministicEmbeddingProvider, cosine_similarity
from domain.grounding import _tokens
from evaluation.retrieval_dataset import RETRIEVAL_EVALUATION_DATASET

OUT=Path("evaluation/results/sprint3_retrieval_evaluation.json")

def _rank(case, mode, k=3):
    q=case["query"]
    records=[]
    for raw in case["records"]:
        rid,text=raw.split("|",1); records.append((rid,text))
    qt=_tokens(q)
    emb=DeterministicEmbeddingProvider()
    vecs=emb.embed([q]+[t for _,t in records])
    qv=vecs[0]
    ranked=[]
    for i,(rid,text) in enumerate(records):
        tt=_tokens(text); overlap=len(qt & tt); exact=60 if q.lower() in text.lower() else 0
        deterministic=exact+min(36,overlap*9)
        semantic=max(0.0,cosine_similarity(qv,vecs[i+1]))
        if mode=="deterministic":
            score=deterministic
        elif mode=="semantic":
            score=semantic
        else:
            semantic_bonus=max(0.0, semantic-0.55)*18.0
            deterministic_strength=min(1.0, deterministic/60.0)
            gate=0.35+0.65*(1.0-deterministic_strength)
            score=deterministic+semantic_bonus*gate
        ranked.append((score,rid))
    ranked.sort(key=lambda x:(-x[0],x[1]))
    return [rid for score,rid in ranked[:k] if mode=="semantic" or score>0]


def _metrics(mode,k=3):
    recalls=[]; precisions=[]; hits=0
    for case in RETRIEVAL_EVALUATION_DATASET:
        pred=_rank(case,mode,k); rel=set(case["relevant"]); hit=set(pred)&rel
        if rel: recalls.append(len(hit)/len(rel))
        else: recalls.append(1.0 if not pred else 0.0)
        precisions.append(len(hit)/len(pred) if pred else (1.0 if not rel else 0.0))
        if hit: hits+=1
    n=len(RETRIEVAL_EVALUATION_DATASET)
    return {"k":k,"cases":n,"recall_at_k":round(sum(recalls)/n,4),"precision_at_k":round(sum(precisions)/n,4),"evidence_hit_rate":round(hits/n,4)}

def evaluate():
    result={"dataset":"RETRIEVAL_EVALUATION_DATASET","dataset_size":len(RETRIEVAL_EVALUATION_DATASET),"deterministic":_metrics("deterministic"),"semantic":_metrics("semantic"),"hybrid":_metrics("hybrid")}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(result,indent=2)); return result

if __name__=="__main__": print(json.dumps(evaluate(),indent=2))
