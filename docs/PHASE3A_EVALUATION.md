# HealthGuard — Phase 3A Report: Evaluation & Data Foundation

Scope: build a rigorous, reusable evaluation/data foundation for the
**existing** Phase 1 baseline, so Phase 3B (semantic models) and Phase 3C
(LLM-based verification) can be compared against it on equal footing. No
application code, authentication, frontend, or deployment configuration was
touched. No new model was trained.

## 1. Repository inspection (before writing anything)

Before adding anything, I re-read the existing structure to reuse it
faithfully:
- `modules/ml_issue_classifier.py` — the baseline (TF-IDF + Logistic
  Regression), its `fit_and_evaluate()` training path and `predict()`
  inference path, and its existing metric set (accuracy, macro P/R/F1,
  confusion matrix, per-class report) — Phase 3A's own metric computation
  intentionally mirrors this exactly, for comparability.
- `training/generate_sample_data.py` — how the labelled synthetic dataset
  is constructed (a small fixed pool of sentence fragments per category —
  this turned out to matter, see §4).
- `modules/ml_satisfaction_model.py`'s `LEAKY_COLUMNS` — the existing
  precedent for an explicit, named leakage guard, which
  `evaluation/leakage_checks.py` extends rather than reinvents.
- `utils/config.py` — the project's existing single place for shared
  constants; extended additively (new section only) rather than
  restructured.
- `utils/logger.py` — reused as-is for all new modules' logging.

## 2. The data contract (task 1)

`evaluation/data_contract.py` defines `FeedbackRecord`:

| Field | Type | Notes |
|---|---|---|
| `feedback_text` | str | non-empty |
| `primary_issue` | str | one of `ALLOWED_ISSUE_CATEGORIES` (the existing 9 categories from `modules/issue_categorizer.py`'s `ISSUE_KEYWORDS`, plus `Other`/`Uncategorized`) |
| `secondary_issues` | list[str] | zero or more additional categories, contract-only for now (see §6) |
| `sentiment` | str | `Positive` / `Neutral` / `Negative` — matches `modules/sentiment.py`'s VADER label vocabulary exactly |
| `severity` | str | `Low` / `Medium` / `High` / `Critical` — same four tiers `modules/priority_scorer.py`'s `priority_label` already uses, as a general label independent of that specific weighted formula |
| `requires_human_review` | bool | routing flag |
| `dataset_version` | str | traces a record back to the dataset/version that produced it |

`validate_record()` / `validate_dataframe()` check every field (required
fields present, category vocabulary valid, `primary_issue` not duplicated
in `secondary_issues`, etc.) and are unit-tested (`TestDataContract`, 11
tests). `encode_secondary_issues()`/`decode_secondary_issues()` handle the
list↔CSV-string round-trip (`"Billing;Communication"`).

This is a validation/serialization layer only — it does not change
`app.py`, `modules/issue_categorizer.py`, or either ML module's runtime
behavior.

## 3. Baseline preserved, not retrained (task 2)

`modules/ml_issue_classifier.py` was **not modified**. `evaluation/evaluate.py`
calls only its public `is_available()` and `predict()` — the exact same
inference path `backend/pipeline.py` and `modules/issue_categorizer.py`
already use. No `fit()`/`fit_and_evaluate()` call appears anywhere in the
Phase 3A code. This was verified directly, not just by code inspection: the
persisted model files' SHA-256 hashes were checked before and after running
the full evaluation (and again after the whole test suite) and are
byte-identical every time (`f933aa52...` / `8b346e9b...`) — see §8.

## 4. Dataset separation (task 3) — and a real finding along the way

`evaluation/splits.py` builds a **separate, persisted** train/val/test split
from `data/sample_patient_survey.csv`, independent of the in-memory split
`ml_issue_classifier.fit_and_evaluate()` still performs internally on every
training run (that function and its splitting logic are untouched).

**Finding:** `training/generate_sample_data.py` builds each row by
combining fragments from a small fixed pool per category (see its
`CATEGORY_TEMPLATES`). Across 900 rows this means only **353 are
textually unique** — 547 rows are exact-duplicate `feedback_text`. A naive
random split ignores this and happily places the same exact sentence in
both train and test, which is genuine leakage (the model would be
recognizing memorized text, not generalizing). I did not paper over this:
`evaluation/splits.py` deduplicates on normalized text (case/punctuation-
insensitive) **before** splitting, and the manifest records exactly how
many rows were dropped and why (`n_dropped_duplicate_text_rows: 547`).
This dedup happens only inside the new Phase 3A split logic — the source
CSV, `training/generate_sample_data.py`, and the baseline's own internal
split are all untouched.

Resulting split (deterministic, seed 42, stratified):

| Split | Rows |
|---|---|
| Train | 247 |
| Validation | 53 |
| Test | 53 |
| Challenge/OOD | 36 (separately authored, see §5) |

Persisted to `data/splits/{train,val,test}.csv` + `data/splits/split_manifest.json`
(source file, seed, per-split row counts, and a SHA-256 of each split's
text — so a later re-run can prove it reproduced the identical split, not
just a similarly-sized one).

**Leakage is not just designed-in, it's checked.** `evaluate.py` runs
`leakage_checks.assert_no_split_leakage()` — pairwise normalized-text
overlap across all four sets — before trusting any metric, and the full
pairwise report is embedded in the JSON output. Current result: **zero
overlap** in all six pairs (train/val, train/test, train/challenge,
val/test, val/challenge, test/challenge).

## 5. The challenge/OOD set (task 4)

`evaluation/challenge_set.py` — **36 hand-authored, clearly synthetic**
examples (⚠ explicitly marked as such in the module docstring and this
report — none of it is a real patient record), independently worded from
`generate_sample_data.py`'s fragment pool so it's disjoint by construction,
not just disjoint by luck:

| `challenge_type` | Count | What it tests |
|---|---|---|
| `paraphrase` | 6 | Same categories, completely different wording |
| `ambiguous` | 5 | Genuinely unclear single category |
| `multi_issue` | 5 | Two+ categories in one response (exercises `secondary_issues`) |
| `low_information` | 5 | Very short/vague ("Meh.", "N/A") |
| `unfamiliar_wording` | 5 | Correct category, slang/informal phrasing |
| `out_of_domain` | 4 | Not hospital feedback at all (pizza delivery, laptop battery, hiking trail, a work email) |
| `edge_case` | 6 | Negation, sarcasm, mixed/contradictory sentiment |

Every record is validated against the data contract at write time
(`generate_challenge_set()` raises on any violation) and is confirmed
duplicate-free within itself and against every other split (§4, §8).

## 6. Multi-label evaluation support (task 6) — scaffold, not a new model

Per the task, the single-label baseline was **not** forced into multi-label.
`evaluation/multilabel_eval.py` provides generic, model-agnostic multi-label
metrics (subset accuracy, Hamming loss, per-label precision/recall/F1 via
`MultiLabelBinarizer`) that operate on any `(y_true, y_pred)` pair of
label-sets. `evaluate.py` exercises this machinery end-to-end by comparing
the challenge set's hand-labelled `primary_issue + secondary_issues` against
the baseline's top-1 prediction treated as a length-1 set — **explicitly
labelled a scaffold/demonstration in the output JSON**
(`multilabel_scaffold.scaffold_notice`), not presented as real multi-label
model performance. Result: subset_accuracy 0.222, Hamming loss 0.147 — this
number means "how often does treating a single-label prediction as if it
were the whole label-set match exactly," which is expected to be low by
construction and exists only to prove the metric functions themselves are
correct (verified further by `TestMultilabelEval`'s hand-computed
perfect/zero-match unit tests) before Phase 3B needs them for real.

## 7. Leakage checks (task 7)

`evaluation/leakage_checks.py` implements two distinct checks:

1. **Column-level**: `check_column_leakage()` / `assert_no_column_leakage()`
   — generic set-intersection check against `TARGET_DERIVED_COLUMNS` (which
   extends `modules/ml_satisfaction_model.py`'s existing `LEAKY_COLUMNS`
   concept with the new contract fields). `assert_baseline_feature_columns_allowed()`
   is the specific, applied check for this baseline: an **allowlist**
   (only `feedback_text`/`clean_text`), stricter than a denylist, since the
   issue classifier's only legitimate input is raw text.
2. **Row-level (split)**: `check_split_disjointness()` /
   `assert_no_split_leakage()` — described in §4.

Both are unit-tested (`TestLeakageChecks`, 9 tests) with intentionally
leaky inputs to confirm they actually fail loudly, not just pass silently.

## 8. Reproducibility metadata (task 8) & machine-readable output (task 9)

Every evaluation run writes `evaluation/results/evaluation_report_<UTC
timestamp>.json` (a permanent, timestamped record) and overwrites
`evaluation/results/latest_evaluation.json` (a stable pointer to the most
recent run). Every report includes:

```json
"reproducibility": {
  "evaluation_timestamp_utc": "...",
  "random_seed": 42,
  "python_version": "3.12.3",
  "scikit_learn_version": "1.8.0",
  "sample_dataset_version": "sample-v1.0",
  "challenge_dataset_version": "challenge-v1.0",
  "model": {
    "model_name": "tfidf_logistic_regression_issue_classifier",
    "model_file_sha256": "ababbf65...",
    "vectorizer_file_sha256": "e402b99a...",
    "classifier_params": {"C": 1.0, "class_weight": "balanced", ...},
    "vectorizer_params": {"ngram_range": [1, 2], "min_df": 2, ...},
    "confidence_threshold": 0.45
  }
}
```

The model/vectorizer hashes are read from the actual persisted `.joblib`
files (not hardcoded), so the report can prove exactly which trained
artifact produced these numbers. `classifier_params`/`vectorizer_params`
come from `get_params()` on the loaded objects, not from documentation
that could drift out of sync with the real hyperparameters.

## 9. Results (actual numbers — none fabricated, none tuned)

### Baseline on the held-out TEST split (n=53)

| Metric | Value |
|---|---|
| Accuracy | 1.0000 |
| Macro precision | 1.0000 |
| Macro recall | 1.0000 |
| Macro F1 | 1.0000 |
| Mean confidence | 0.808 |
| Low-confidence rate (<0.45) | 0.0% |

**Same disclaimer as Phase 1, because it's still true**: this test split is
held out from the same synthetic, templated dataset used for training.
Per-category vocabulary is distinct by construction, so a nearly-perfect
score here reflects that construction, not real-world accuracy. This is
exactly why the challenge set is evaluated separately, below, rather than
being averaged into one misleadingly reassuring number.

### Baseline on the CHALLENGE/OOD set (n=36) — reported separately

| Metric | Value |
|---|---|
| Accuracy | 0.3333 |
| Macro precision | 0.2532 |
| Macro recall | 0.4058 |
| Macro F1 | 0.2913 |
| Mean confidence | 0.227 |
| Low-confidence rate (<0.45) | **97.2%** |

By `challenge_type`:

| Type | n | Accuracy | Mean confidence |
|---|---|---|---|
| paraphrase | 6 | 0.833 | 0.226 |
| edge_case | 6 | 0.500 | 0.249 |
| multi_issue | 5 | 0.400 | 0.266 |
| unfamiliar_wording | 5 | 0.400 | 0.265 |
| ambiguous | 5 | 0.000 | 0.211 |
| low_information | 5 | 0.000 | 0.172 |
| out_of_domain | 4 | 0.000 | 0.190 |

This is a genuine, unforced result — I did not tune the challenge set to
hit a target number. It shows exactly the generalization gap you'd expect:
the baseline holds up reasonably on paraphrased-but-still-in-domain text
(0.83), degrades on multi-issue/slang phrasing (0.40), and fails outright
on ambiguous, low-information, and out-of-domain text (0.00) — text a
linear TF-IDF model has no real basis for classifying correctly, which is
precisely the kind of gap Phase 3B's semantic models and Phase 3C's LLM
verification exist to close.

**One reassuring cross-check**: the existing Phase 1 confidence-threshold
fallback (`ISSUE_ML_CONFIDENCE_THRESHOLD = 0.45`, wired into
`modules/issue_categorizer.py`) would catch 97.2% of these challenge-set
predictions as low-confidence and route them to the rule-based fallback in
the live app — the safety mechanism already built in Phase 1 is doing
almost exactly what it should here, even though this evaluation never
touched that code path directly.

### Multi-label scaffold (n=36, challenge set only — see §6 caveat)

subset_accuracy=0.222, hamming_loss=0.147, macro_f1=0.306 — explicitly
labelled non-representative of a real multi-label model in the JSON output
itself.

Full per-class reports and confusion matrices for both the test split and
the challenge set are in `evaluation/results/latest_evaluation.json`
(too large to reproduce in full here).

## 10. Tests (task 10)

`tests/test_evaluation.py`, stdlib `unittest` (no new test-framework
dependency): **35 tests, all passing.**

```
python -m unittest tests.test_evaluation -v
# Ran 35 tests in 0.24s — OK
```

| Test class | Count | Covers |
|---|---|---|
| `TestDataContract` | 11 | Valid/invalid records, category/sentiment/severity vocab, secondary-issue encode/decode round-trip, dataframe-level validation |
| `TestLeakageChecks` | 9 | Column-leakage detection (positive + negative), baseline allowlist, split-disjointness detection (positive + negative), text normalization |
| `TestChallengeSet` | 5 | Every record passes contract validation, all 7 challenge types present, no internal duplicates, valid categories, multi-issue records actually have secondary issues |
| `TestMultilabelEval` | 4 | Perfect/zero-match scoring by hand, scaffold notice present, mismatched-length input rejected |
| `TestSplitsReproducibility` | 2 | Same seed → identical split (byte-for-byte text comparison) on a small temp dataset; splits are pairwise disjoint |
| `TestEvaluatePipeline` | 4 | Metric computation shape/correctness on toy data, **model files unchanged after a full evaluation run** (explicit read-only guarantee), full report has all required sections, test-split and challenge-set metrics are computed and reported independently |

`TestEvaluatePipeline` auto-skips if no trained model is present (checks
`ml_issue_classifier.is_available()` in `setUpClass`), so the suite doesn't
hard-fail in an environment where `training/train_models.py` hasn't been
run yet — but in this environment it ran and passed.

## 11. Files changed / added / preserved

**Added**
- `evaluation/__init__.py`, `evaluation/data_contract.py`, `evaluation/splits.py`, `evaluation/challenge_set.py`, `evaluation/leakage_checks.py`, `evaluation/multilabel_eval.py`, `evaluation/evaluate.py`
- `evaluation/results/latest_evaluation.json` + one timestamped copy (generated output)
- `data/splits/train.csv`, `data/splits/val.csv`, `data/splits/test.csv`, `data/splits/split_manifest.json` (generated output)
- `data/challenge_set.csv` (generated output, from hand-authored records in `challenge_set.py`)
- `tests/__init__.py`, `tests/test_evaluation.py`
- `PHASE3A_EVALUATION.md` (this report)

**Modified**
- `utils/config.py` — additive only: a new `# Phase 3A` section with `SPLITS_DIR`, `CHALLENGE_SET_PATH`, `EVAL_RESULTS_DIR`, `SAMPLE_DATASET_VERSION`, `CHALLENGE_DATASET_VERSION`. No existing line changed.

**Preserved, byte-for-byte untouched** (verified via SHA-256 before/after,
§9 of the prior deployment-fix report and re-confirmed here):
- `modules/ml_issue_classifier.py`, `modules/ml_satisfaction_model.py`, every other file in `modules/`
- `training/generate_sample_data.py`, `training/train_models.py`
- `models/issue_classifier/*.joblib`, `models/satisfaction_model/*.joblib`
- `backend/*.py` (auth, main, pipeline, metrics) — no auth/API changes
- `frontend/*` — no UI changes
- `app.py`, `render.yaml`, `requirements.txt` — no deployment changes; **zero new dependencies** (only stdlib + already-installed `pandas`/`scikit-learn` were used)

## 12. Limitations / remaining issues

- **Both the training data and the challenge set are synthetic.** The
  challenge set is designed to be *harder and differently-worded*, not to
  simulate real patient feedback distributions — a real deployment would
  need a genuine held-out sample of real (de-identified) feedback for a
  trustworthy generalization estimate. This is stated plainly, not hidden:
  see the `dataset_notice`-style disclaimers throughout.
  - **Small challenge set (n=36).** Large enough to reveal a clear,
  consistent pattern (per-type accuracy ranging 0.0–0.83) but too small for
  tight confidence intervals on any single number — read it as a directional
  diagnostic, not a precise benchmark.
- **Multi-label evaluation is a scaffold, not a capability.** No model in
  this repository can currently predict `secondary_issues`; the numbers in
  §6 measure the evaluation code, not classification quality. Phase 3B is
  where a real multi-label (or semantic-similarity-based) approach would
  need to be added — explicitly out of scope here.
- **The 353-unique-row dedup (§4) shrinks the effective dataset** from 900
  rows to 353 before splitting. This is the correct fix for the leakage
  problem it solves, but it does mean train/val/test are each smaller than
  the raw row count might suggest — worth knowing if Phase 3B wants more
  training signal (the fix there is a richer/larger `generate_sample_data.py`
  fragment pool, not something Phase 3A should touch given its scope).
- **`evaluate.py`'s `_load()` call** into `ml_issue_classifier` uses that
  module's underscore-prefixed internal loader (`_load()`) to fetch the
  vectorizer/classifier objects for hashing and `get_params()` reporting —
  functionally read-only and side-effect-free (it's the same cache the
  module's own `predict()` already populates), but it does reach past that
  module's public API (`predict()`, `predict_one()`, `is_available()`).
  Flagging this for visibility rather than treating it as hidden coupling.
