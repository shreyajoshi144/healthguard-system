# HealthGuard — Phase 3A Correction: Benchmark Baseline

Scope: fix exactly one methodological issue in the completed Phase 3A
evaluation — the production baseline's train/val/test rows were not
provably disjoint from the Phase 3A `data/splits/` because it was trained
by a different pipeline (`training/train_models.py` → `fit_and_evaluate()`,
which performs its own internal split over the full source dataset) before
`evaluation/splits.py` existed. Nothing else was changed.

## 1. The fix

`evaluation/benchmark_model.py` (new) trains a **separate benchmark
artifact** using **only** `data/splits/train.csv`, then evaluates it on
`data/splits/val.csv`, `data/splits/test.csv`, and `data/challenge_set.csv`
— all three genuinely unseen during that training run, verified two ways:

1. **Structurally**: `fit_benchmark_from_frames()` calls
   `vectorizer.fit_transform()` and `clf.fit()` exactly once, on the train
   frame only. Val/test/challenge only ever go through `.transform()`
   (inside a separate `_evaluate()` helper that never fits anything).
2. **Empirically, by test**: `TestBenchmarkTrainsOnlyOnTrain` builds a toy
   dataset where validation and test each contain a nonsense word that
   cannot appear anywhere in train by coincidence, trains the benchmark on
   it, and asserts those words are absent from the fitted vectorizer's
   vocabulary. If training ever saw val/test text, this test would fail.

## 2. Configuration — kept identical to the production baseline

Per task requirement 4, `evaluation/benchmark_model.py` uses the exact same
hyperparameters as `modules/ml_issue_classifier.py`'s `fit_and_evaluate()`:

```python
TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_df=0.9, sublinear_tf=True)
LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42)
```

These are duplicated (not imported) rather than refactoring
`fit_and_evaluate()` to accept a pre-split train set — that function's
signature and the production module are both explicitly preserved
untouched, per scope. The module docstring flags this duplication and its
reason explicitly, so it doesn't read as an accidental drift risk.

## 3. Production baseline — untouched, verified

`modules/ml_issue_classifier.py` was not modified. SHA-256 checksums of
both production artifact files were captured before and after running the
benchmark training, the full evaluation pipeline, and the entire test
suite — identical every time:

```
logreg_issue_classifier.joblib:  f933aa5243033df6d5740daf082380bb (md5, for readability)
tfidf_vectorizer.joblib:         8b346e9bf5a9fe3b6f652d46d0faea91
```

This is also asserted automatically by
`TestArtifactSeparation.test_production_artifacts_unchanged_after_benchmark_training`.

## 4. Storage — separate artifact, separate metadata

| | Production | Benchmark |
|---|---|---|
| Path | `models/issue_classifier/` | `models/benchmark_issue_classifier/` |
| Trained by | `training/train_models.py` (full dataset, internal split) | `evaluation/benchmark_model.py` (`data/splits/train.csv` only) |
| Wired into app.py/backend/ | Yes | **No** — evaluation-only, not used for any live inference |

**Benchmark model path:** `models/benchmark_issue_classifier/logreg_issue_classifier.joblib`
(+ `tfidf_vectorizer.joblib`, `metrics.json` alongside it)

Recorded reproducibility metadata (task 5), in
`models/benchmark_issue_classifier/metrics.json` and mirrored in
`evaluation/results/latest_benchmark_evaluation.json`:

```json
{
  "model_identifier": "benchmark_tfidf_logistic_regression_issue_classifier_v1",
  "random_seed": 42,
  "train_dataset_version": "sample-v1.0",
  "challenge_dataset_version": "challenge-v1.0",
  "feature_configuration": {
    "vectorizer_params": {"ngram_range": [1, 2], "min_df": 2, "max_df": 0.9, "sublinear_tf": true},
    "classifier_params": {"max_iter": 1000, "class_weight": "balanced", "random_state": 42}
  },
  "split_sizes": {"train": 247, "validation": 53, "test": 53, "challenge": 36},
  "artifact_sha256": { "...": "..." },
  "evaluation_timestamp_utc": "..."
}
```

## 5. Machine-readable results — clearly separated (task 6)

`evaluation/evaluate.py` (the main Phase 3A report generator) was updated
**additively**: it still evaluates and reports the production model exactly
as before, now explicitly labelled `results.model_role: "production"`, and
— if a benchmark evaluation has been run — attaches it under a distinct
top-level key:

```json
{
  "results": {
    "model_role": "production",
    "test_split": { "accuracy": 1.0, "...": "..." },
    "challenge_set": { "accuracy": 0.3333, "...": "..." }
  },
  "benchmark_baseline": {
    "model_role": "benchmark",
    "note": "SEPARATE artifact trained ONLY on data/splits/train.csv ...",
    "reproducibility": { "model_identifier": "benchmark_tfidf_..._v1", "...": "..." },
    "results": {
      "validation": { "accuracy": 1.0, "...": "..." },
      "test": { "accuracy": 1.0, "...": "..." },
      "challenge": { "accuracy": 0.2778, "...": "..." }
    }
  }
}
```

If no benchmark evaluation exists yet, `benchmark_baseline` is simply
`null` and the rest of the report is unaffected — `evaluate.py` never
trains the benchmark itself, keeping the two steps decoupled.

## 6. Benchmark metrics (actual, unforced)

| Split | n | Accuracy | Macro P | Macro R | Macro F1 |
|---|---|---|---|---|---|
| Validation | 53 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| Test | 53 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| **Challenge/OOD** | 36 | **0.2778** | 0.1903 | 0.3701 | 0.2343 |

Same synthetic-dataset caveat as everywhere else in this project applies
to validation/test (see `PHASE1_ML_UPGRADE.md`, `PHASE3A_EVALUATION.md`):
near-perfect scores reflect the training data's constructed vocabulary
separation, not real-world accuracy.

**For context, not to imply either number is "the truth"**: the production
model (trained on the full 900-row dataset via its own internal split)
scored 33.33% on the same challenge set; this benchmark (trained on only
247 deduplicated rows) scored 27.78%. Both numbers are real, neither was
tuned to match the other, and the gap is a plausible, honest consequence of
training-set size — it is not evidence that either evaluation methodology
is "more correct" than the other, only that they now measure genuinely
different (and individually well-defined) things: the production number is
"how the deployed model performs," the benchmark number is "how this exact
configuration performs when its training set is provably disjoint from
what it's evaluated on."

## 7. Scope discipline (task 7) — confirmed untouched

- **Authentication**: `backend/auth.py` not opened.
- **Frontend**: nothing under `frontend/` touched.
- **Deployment**: `render.yaml`, `requirements.txt` unchanged — no new
  dependencies (only stdlib + already-installed `pandas`/`scikit-learn`/
  `joblib` used, same as the rest of `evaluation/`).
- **Phase 1 behavior**: `modules/*.py`, `training/*.py` unchanged.
- **Phase 2 behavior**: `backend/*.py` unchanged.
- **Challenge/OOD examples**: `data/challenge_set.csv` byte-identical
  before/after (verified by `test_did_not_regenerate_challenge_set_or_splits`,
  which diffs the full `feedback_text` column pre/post benchmark training).
- **Dataset splits**: `data/splits/*.csv` byte-identical; `split_manifest.json`'s
  `built_at` timestamp is unchanged from the original Phase 3A run
  (`evaluation/splits.py`'s `build_splits()` detects the matching existing
  manifest and reuses it rather than rebuilding).
- **Phase 3B+**: nothing beyond Phase 3A exists yet; nothing added here
  reaches into that territory (no embeddings, no LLM, no RAG, no agents,
  no database).

## 8. A necessary, minimal robustness fix (disclosed, not hidden)

While first running `evaluation.benchmark_model`, loading
`data/challenge_set.csv` raised `ValueError: np.nan is an invalid document`.
Cause: one of the existing (unmodified) challenge examples has
`feedback_text` literally `"N/A"` (a deliberate `low_information` test
case) — `pandas.read_csv`'s default `na_values` list treats the text
`"N/A"` as a missing-value marker and silently converts it to `NaN` on
read, in the pandas version installed in this environment. This is a
**read-side parsing behavior**, not a problem with the CSV file itself.

Fixed by reading the file with `keep_default_na=False, na_values=[]` in
both `evaluation/benchmark_model.py` (new) and `evaluation/evaluate.py`
(one-line change, same fix, needed there too since it reads the same file)
— the challenge-set CSV on disk was never modified; only how it's parsed.
Flagging this explicitly rather than folding it in silently, since it's the
one place this correction touched code outside the new benchmark module.

## 9. Tests (task 8)

`tests/test_benchmark_model.py` — **14 new tests**, plus the pre-existing
35 from Phase 3A (`tests/test_evaluation.py`) — **49 total, all passing**:

```
python -m unittest discover -s tests -v
# Ran 49 tests in 0.82s — OK
```

| Test class | Count | Proves |
|---|---|---|
| `TestBenchmarkTrainsOnlyOnTrain` | 7 | Vectorizer vocabulary excludes val-only and test-only words (direct proof of "unseen during training"); `n_train` matches the train frame; reported split sizes match input frames; `trained_on` field documents train-only; classifier's learned classes match only train labels |
| `TestArtifactSeparation` | 3 | Benchmark paths differ from production paths; **production files unchanged** after a real benchmark training run (SHA-256 before/after); benchmark artifact actually written to its own directory |
| `TestBenchmarkEndToEnd` | 5 | `train_benchmark()` completes and returns the expected report shape; reproducibility metadata present and correct; split sizes match the real files on disk; the main `evaluate.py` report includes the benchmark section, correctly separated from production; **challenge set and splits are not regenerated** (full text-column diff before/after) |

`TestArtifactSeparation` and `TestBenchmarkEndToEnd` skip gracefully (not
fail) if the production model or the Phase 3A data files aren't present in
a given environment, consistent with the skip pattern already established
in `tests/test_evaluation.py`.

## 10. Files changed / added

**Added**
- `evaluation/benchmark_model.py`
- `tests/test_benchmark_model.py`
- `models/benchmark_issue_classifier/{tfidf_vectorizer.joblib, logreg_issue_classifier.joblib, metrics.json}` (generated artifact)
- `evaluation/results/latest_benchmark_evaluation.json` + one timestamped copy (generated)
- `PHASE3A_BENCHMARK_CORRECTION.md` (this report)

**Modified**
- `evaluation/evaluate.py` — additive: `benchmark_baseline` section merged
  into the main report (read-only, no re-training triggered from here);
  `results.model_role`/`results.note` fields added for clarity; one-line
  CSV-read robustness fix (§8). No metric computation logic changed, no
  existing report field removed or renamed.

**Untouched**: everything else, including `evaluation/splits.py`,
`evaluation/challenge_set.py`, `evaluation/leakage_checks.py`,
`evaluation/data_contract.py`, `evaluation/multilabel_eval.py`,
`tests/test_evaluation.py`, `modules/*`, `training/*`, `backend/*`,
`frontend/*`, `app.py`, `render.yaml`, `requirements.txt`,
`models/issue_classifier/*`, `data/splits/*.csv`, `data/challenge_set.csv`.

## 11. Remaining limitations

- **Both datasets are still synthetic.** This correction changes what's
  provably unseen during training; it does not and cannot make the
  underlying data more representative of real patient feedback. Same
  disclaimer as every prior report in this project.
- **The benchmark and production models are not directly comparable as
  "before vs. after" numbers** — they were trained on different amounts of
  data (247 deduplicated rows vs. the full 900-row dataset via a different
  split), so a difference in their challenge-set scores (27.78% vs 33.33%)
  reflects that difference in training data, not a methodology fix
  "improving" or "worsening" the model. Reported as two distinct,
  individually-valid measurements — not folded into a single number or
  framed as one superseding the other.
- **The benchmark artifact is not used anywhere in the live application.**
  It exists solely to provide a provably-clean evaluation number; if a
  future phase wants the production model itself to be retrained with
  guaranteed train/val/test separation, that would mean changing
  `training/train_models.py`'s training data source — explicitly out of
  scope for this correction, which was told to leave the production
  baseline untouched.
- **Hyperparameter duplication** between `modules/ml_issue_classifier.py`
  and `evaluation/benchmark_model.py` (§2) needs to be kept in sync by hand
  if the production config ever changes. Flagged in both files' docstrings;
  not fixed here since fixing it would mean modifying the preserved
  production module's function signature.
