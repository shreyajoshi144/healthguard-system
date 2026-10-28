# HealthGuard — Phase 3B Report: Semantic ML Baseline

Scope: add exactly one semantic text-classification baseline (pretrained
sentence embeddings + one lightweight classifier) and compare it fairly
against the **frozen** Phase 3A benchmark. No application code, no new
datasets, no modified challenge examples, no auth/UI/deployment changes.

## 1. Repository inspection (before writing anything)

Re-read `evaluation/benchmark_model.py` in full before starting, to mirror
its structure and guarantees exactly: same fixed classifier hyperparameters
where applicable, same `_evaluate()` metric set, same train-only-fitting
discipline, same "frozen artifact, read-only comparison" pattern already
established for the production model in `evaluation/evaluate.py`. Also
re-checked `evaluation/leakage_checks.py`, `utils/config.py`, and the
existing `data/splits/`, `data/challenge_set.csv` — none were touched.

## 2. Exact semantic model used

**Embedding**: spaCy **`en_core_web_md` v3.8.0** — 300-dimensional static
word vectors (GloVe-family, trained on Common Crawl + Wikipedia), used as
a sentence embedding via mean-pooling over token vectors (`Doc.vector`).
**Classifier**: one **Logistic Regression** head (`class_weight="balanced"`,
same fixed choice as the benchmark; regularization strength `C` selected
via validation-only grid search over `[0.1, 1.0, 10.0]` — see §7).

**Why this embedding, not a transformer sentence-embedding model**: the
more typical modern choice (`sentence-transformers`, e.g.
`all-MiniLM-L6-v2`) was tried first. Its weights are hosted on the Hugging
Face Hub, and `huggingface.co` is unreachable from this project's
sandboxed environment — confirmed directly (`curl -I https://huggingface.co`
→ `HTTP 403 host_not_allowed`), not assumed. Rather than silently
substitute something weaker without saying so, `en_core_web_md` was
chosen and verified to actually work end-to-end here: its wheel is hosted
on GitHub Releases, which resolves through `github.com` /
`release-assets.githubusercontent.com` — both reachable — so this is a
genuinely reproducible choice, not just a name referenced but never run.
It is disclosed plainly in `evaluation/embeddings.py`'s module docstring
that this is a real constraint-driven substitution, and that
`en_core_web_md`'s vectors are older/weaker than modern transformer
sentence embeddings on general semantic-similarity benchmarks — the
comparison below answers "does a practical, easily-deployable static
embedding beat TF-IDF here," not "does the best possible embedding beat
TF-IDF."

Practicality: ~55MB on disk, no GPU, no `torch` dependency, disabled all
trainable spaCy pipeline components (tagger/parser/ner/etc. — none needed
for vector lookup), leaving pure tokenization + vector-table lookup:
measured at ~0.05ms/example.

## 3. Files changed / added

**Added**
- `evaluation/embeddings.py` — the embedding provider (§2), with the
  Hugging Face vs. spaCy decision documented in its docstring
- `evaluation/semantic_model.py` — training + evaluation, mirrors
  `benchmark_model.py`'s structure and guarantees
- `evaluation/comparison.py` — reads both frozen artifacts read-only,
  produces the side-by-side comparison report
- `models/semantic_issue_classifier/{logreg_classifier.joblib, metrics.json}` (generated)
- `evaluation/results/latest_semantic_evaluation.json` + timestamped copy (generated)
- `evaluation/results/latest_comparison.json` + timestamped copy (generated)
- `tests/test_semantic_model.py` — 14 new tests
- `PHASE3B_SEMANTIC_MODEL.md` (this report)

**Modified**
- `evaluation/evaluate.py` — additive only: merges `semantic_baseline` and
  `benchmark_vs_semantic_comparison` sections into the main JSON report
  (read-only, no training triggered from here), alongside the existing
  `results` (production) and `benchmark_baseline` sections from the
  Phase 3A correction. No existing field removed, renamed, or recomputed
  differently.
- `requirements.txt` — added `spacy==3.8.16`, with the `en_core_web_md`
  install command documented in a comment (it isn't a normal PyPI package
  name, same pattern as the NLTK-resource-download step already documented
  elsewhere in this project).

**Untouched, verified by SHA-256 before/after every training and test run
in this session**: `evaluation/benchmark_model.py`,
`models/benchmark_issue_classifier/*.joblib`,
`modules/ml_issue_classifier.py`, `models/issue_classifier/*.joblib`,
`data/splits/*.csv`, `data/challenge_set.csv`, `backend/*`, `frontend/*`,
`app.py`, `render.yaml`.

## 4. Methodology — how the data-usage rules were followed

| Split | Used for |
|---|---|
| `data/splits/train.csv` | The classifier's `.fit()` — called once per candidate `C`, always on train embeddings only |
| `data/splits/val.csv` | Model selection ONLY — score each candidate `C` by validation macro F1, keep the best. Never fit on. |
| `data/splits/test.csv` | Scored exactly once, after `C` is finalized — the final benchmark number |
| `data/challenge_set.csv` | Scored exactly once, after everything above — diagnostic only, never influenced tuning |

Proven, not just claimed: `tests/test_semantic_model.py` wraps
`LogisticRegression.fit` with a call-recording spy across the entire
training + model-selection pipeline and asserts **every single call**
used exactly `len(train)` examples — if validation, test, or challenge
data had leaked into any fit call, this test would fail.

## 5. Benchmark vs. semantic metrics

| Split | Metric | Benchmark (frozen) | Semantic | Δ (semantic − benchmark) |
|---|---|---|---|---|
| Validation (n=53) | Accuracy | 1.0000 | 1.0000 | 0.0000 |
| | Macro F1 | 1.0000 | 1.0000 | 0.0000 |
| Test (n=53) | Accuracy | 1.0000 | 1.0000 | 0.0000 |
| | Macro Precision | 1.0000 | 1.0000 | 0.0000 |
| | Macro Recall | 1.0000 | 1.0000 | 0.0000 |
| | Macro F1 | 1.0000 | 1.0000 | 0.0000 |
| **Challenge (n=36)** | **Accuracy** | **0.2778** | **0.2778** | **0.0000** |
| | Macro Precision | 0.1903 | 0.2018 | +0.0115 |
| | Macro Recall | 0.3701 | 0.3247 | **−0.0454** |
| | Macro F1 | 0.2343 | 0.2138 | **−0.0205** |
| | Mean confidence | 0.227 | 0.581 | **+0.354** |

Same caveat as every prior phase report: validation/test scores reflect
the training data's constructed vocabulary separation (see
`PHASE1_ML_UPGRADE.md`), not real-world accuracy — both models hit the
ceiling there for the same reason, telling us nothing about which
generalizes better. The challenge set is where they're actually
differentiated: **tied on raw accuracy, semantic slightly behind on macro
F1** — and semantic is **substantially more confident while being no more
correct** (0.581 vs. 0.227 mean confidence at equal accuracy), a real,
unflattering finding worth stating plainly rather than glossing over.

## 6. Challenge-category comparison

| Category | n | Benchmark acc. | Semantic acc. | Δ | Note |
|---|---|---|---|---|---|
| paraphrase | 6 | 0.667 | 0.500 | −0.167 | benchmark wins |
| ambiguous | 5 | 0.000 | 0.000 | 0.000 | both fail |
| multi_issue | 5 | 0.400 | 0.200 | −0.200 | benchmark wins |
| low_information | 5 | 0.000 | 0.000 | 0.000 | both fail |
| **unfamiliar_wording** | 5 | 0.400 | **0.800** | **+0.400** | **semantic wins, decisively** |
| out_of_domain | 4 | 0.000 | 0.000 | 0.000 | both fail |
| edge_case | 6 | 0.333 | 0.333 | 0.000 | tied |

(Benchmark's own per-category numbers were not previously recorded in its
frozen `metrics.json` — `evaluation/comparison.py` re-derives them
read-only from the already-fitted artifact, using identical methodology
to the semantic side; see that module's docstring for why this doesn't
violate "frozen.")

The one place semantic embeddings show a real, sizeable advantage is
exactly where theory predicts: **informal/slang phrasing that shares
meaning but not vocabulary** with the synthetic training templates
(`unfamiliar_wording`). Both models fail completely on `ambiguous`,
`low_information`, and `out_of_domain` text — neither representation gives
the classifier anything to work with when the text itself carries little
category signal.

## 7. Latency, model size, dependency impact

**Latency** (measured identically for both — per-sample loop and one-shot
batch, same 53 test-set texts):

| | Benchmark (TF-IDF+LogReg) | Semantic (spaCy+LogReg) |
|---|---|---|
| Per-sample (ms/example) | 0.292 | **0.047** (6× faster) |
| Batch, total (ms) | **1.30** (1.7× faster) | 2.24 |
| Batch, per-example (ms) | **0.025** | 0.042 |

Neither wins outright: TF-IDF's fully-vectorized sparse batch transform is
faster in bulk (e.g. processing an uploaded CSV), while spaCy's per-call
overhead is lower for a single request (e.g. a live `/api/analyze`-style
call) since there's no sparse-matrix construction step for one string.
Both are sub-millisecond-to-low-millisecond and would be a non-issue in
either the Streamlit app or the FastAPI backend's existing request
latencies.

**Model artifact size** (bytes):

| | Size |
|---|---|
| Benchmark total (vectorizer + classifier) | 58,972 (~57.6 KB) |
| **Semantic — trained classifier only** | **22,799 (~22.3 KB)** — smaller than the benchmark |
| Semantic + pretrained embedding package (installed dependency) | 56,567,477 (~54 MB) |

The genuinely project-trained artifact is smaller for the semantic model
(300 dense features vs. TF-IDF's large sparse vocabulary), but that's a
misleading headline number alone — the ~55MB embedding package is a fixed
cost that must be deployed for the classifier to mean anything, dwarfing
either trained artifact. Both numbers are reported, not just the flattering
one.

**Dependency impact**: `spacy==3.8.16` + `en_core_web_md` (~55MB, one
extra pip install pointed at a GitHub release wheel, documented in
`requirements.txt`). No GPU, no `torch`, no new network calls at
inference time (the model is loaded from disk once, cached in-process).
Meaningfully heavier than the benchmark's dependency footprint
(scikit-learn only) but still modest next to a transformer-based
alternative, which would add `torch` (typically 500MB+ even CPU-only) plus
`transformers`/`sentence-transformers`.

## 8. Tests passed

`tests/test_semantic_model.py` — **14 new tests**, plus all 49 from
Phase 3A / Phase 3A-correction — **63 total, all passing**:

```
python -m unittest discover -s tests -v
# Ran 63 tests in 2.6s — OK
```

| Test class | Count | Proves |
|---|---|---|
| `TestSemanticTrainsOnlyOnTrain` | 3 | Every `LogisticRegression.fit()` call across the whole pipeline used exactly `len(train)` examples (spy-based, not just structural); model-selection function signature structurally can't accept test/challenge data; classifier's learned classes match only train labels |
| `TestSemanticNoSplitLeakage` | 2 | Toy frames are disjoint; an intentionally-injected leak is caught (reusing the same `leakage_checks` the frozen benchmark is held to) |
| `TestArtifactSeparation` | 2 | Semantic path differs from both benchmark and production paths; **benchmark AND production files unchanged** (SHA-256 before/after) after running full semantic training |
| `TestSemanticEndToEnd` | 7 | `train_semantic()` completes with the expected report shape; model selection provably used validation only; reproducibility metadata complete; split sizes match real files on disk; challenge set/splits not regenerated (full text-column diff); **comparison report's deltas are independently recomputed and asserted correct**, not just present; `evaluate.py`'s merged report includes both new sections |

Tests requiring spaCy/`en_core_web_md` skip cleanly (not fail) if the
model isn't installed in a given environment; artifact-separation and
leakage tests that don't need it always run.

## 9. Limitations

- **Both datasets remain synthetic.** Same caveat as every prior phase —
  the challenge set is designed to be harder and differently-worded, not
  to simulate a real feedback distribution.
- **Small challenge set (n=36)**, and per-category subgroups as small as
  n=4–6. The `unfamiliar_wording` win (4/5 vs 2/5) and `paraphrase` loss
  (3/6 vs 4/6) are each a difference of one or two examples — directionally
  informative, not statistically precise. Read per-category deltas as
  "worth investigating further," not as settled conclusions.
- **A weaker embedding than the current state of the art.** As stated in
  §2, `en_core_web_md`'s static vectors are older technology than
  transformer sentence embeddings; this result should not be read as "semantic
  embeddings can't beat TF-IDF for this task," only "this specific,
  practically-deployable embedding didn't, here, on this data." A
  transformer-based sentence embedding might perform differently — that's
  explicitly out of scope for this phase (no LLMs, no new heavy
  dependencies beyond what was already added).
- **Only one hyperparameter was tuned** (`C`, via a 3-point grid). A wider
  search (regularization type, embedding pooling strategy, etc.) might
  shift results in either direction; kept minimal per "add ONE lightweight
  classifier."
- **Neither model handles `secondary_issues`.** Same scaffold-only
  multi-label situation as Phase 3A — unaffected by this phase.
- **Benchmark's per-challenge-type numbers were derived in this phase's
  code** (`evaluation/comparison.py`), not stored in the original frozen
  `benchmark_model.py` output. The underlying artifact is untouched and
  the derivation is read-only/inference-only, but it's worth naming
  plainly that this specific number didn't exist until this comparison
  script computed it.

## 10. Final conclusion — did semantic ML actually improve the frozen benchmark?

**No, not on this data, with this embedding.** On the metric that matters
most for a genuine generalization test — the challenge/OOD set — the
semantic model tied the benchmark on raw accuracy (0.2778 vs 0.2778) and
came in slightly *behind* on macro F1 (0.214 vs 0.234), while being
substantially *more* confident without being more correct (0.581 vs 0.227
mean confidence) — the kind of overconfidence that would be actively worse
for a downstream system relying on confidence thresholds (like the
existing production `categorize_issues()` fallback logic, which this
semantic model is not wired into, but which illustrates the concern).

The one genuine bright spot is **`unfamiliar_wording`** (0.80 vs 0.40
accuracy) — informal/slang phrasing is exactly the category semantic
embeddings should help with, and here they measurably did. That's a real,
useful signal, not nothing. But it's one category out of seven, on a
36-example set, and it doesn't outweigh the aggregate result.

Taken together: **a practical, cheaply-deployable static embedding does
not clearly beat a well-tuned TF-IDF + Logistic Regression baseline on
this dataset** — it trades a narrow, real win on informal phrasing for
losses on paraphrase and multi-issue detection, plus a confidence
calibration that's worse, not better. This does not close the door on
semantic or LLM-based approaches in general (a stronger embedding model
might well change the picture — see §9's limitation on embedding choice),
but it does mean this specific Phase 3B baseline should not be presented
as an improvement, and isn't.
