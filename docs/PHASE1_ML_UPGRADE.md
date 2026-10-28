# HealthGuard — Phase 1 ML Upgrade Report

Scope: audit the existing rule-based pipeline and add a genuine, evaluated ML
layer for issue classification (+ an optional satisfaction model), without
touching frontend, backend/API, auth, Docker, or deployment. Those are out of
scope for this phase.

## 1. Audit: what was actually ML vs. rule-based

Before this change, **nothing in the pipeline was machine learning**:

| Module | What it did | ML? |
|---|---|---|
| `data_cleaner.py`, `data_validator.py` | Deterministic cleaning/validation | No |
| `preprocessor.py` | NLTK tokenization, stopword removal, lemmatization | No (classic NLP, not ML) |
| `feature_engineer.py` | Derived columns (word count, rating bands, NPS) | No |
| `sentiment.py` | VADER — a lexicon/rule-based sentiment scorer | No (NLP heuristic, not trained) |
| `issue_categorizer.py` | Keyword regex matching against a hand-written dictionary | No |
| `priority_scorer.py` | Weighted formula over other columns | No |
| `recommender.py`, `action_mapper.py`, `insight_generator.py` | Static lookup tables / templated rules | No |

The README's "Insight Generation... No external LLM required. Fully
deterministic rule-based insight engine" was accurate — but so was
everything else in the pipeline. There was no trained model, no persisted
artifact, and no train/test evaluation anywhere in the repo. This phase adds
exactly one genuinely-trained, evaluated component (issue classification),
plus an optional second one (satisfaction prediction), while leaving VADER,
priority scoring, recommendations, and insights untouched — they don't need
to be ML to do their job, and the task explicitly says not to force ML in
where it isn't justified.

**No dataset shipped with the repo.** The app is designed for a user to
upload their own CSV at runtime; there was no `data/` sample and no labelled
issue-category data to train against. To do real (not fabricated) training
and evaluation, I built `training/generate_sample_data.py`, which generates
a **synthetic, clearly-labelled** demo dataset — see §3.

## 2. What changed (architecture)

```
data → clean → preprocess → feature_engineer → sentiment (VADER, unchanged)
     → ML issue classifier (NEW, TF-IDF+LogReg) → rule-based fallback (existing, kept)
     → priority_scorer (unchanged) → recommendations / actions / insights (unchanged)

training/train_models.py (NEW, offline) → models/*.joblib (persisted)
```

- The rule-based keyword classifier is **not removed**. It now serves as the
  fallback when the ML model is unavailable or its confidence is below
  `ISSUE_ML_CONFIDENCE_THRESHOLD` (0.45, in `utils/config.py`).
- The satisfaction model is trained and persisted but **not wired into
  `app.py`** — task instructions scoped ML *integration into the pipeline*
  to issue classification only (item 10); the satisfaction model is built,
  evaluated, and given a clean inference function so it's ready for a Phase
  2 UI surface.

## 3. The sample dataset (and why it's honest, not fabricated)

`training/generate_sample_data.py` creates `data/sample_patient_survey.csv`
(900 rows) with a fixed random seed (42) for reproducibility. Every row's
`true_issue_category` label is known by construction — it comes from the
template used to write that row's `feedback_text` — so it is genuine ground
truth, not a guess or a scrape. Ratings are drawn from a category-typical
range with 10% random noise so the satisfaction task isn't a trivial
function of category.

**This is clearly a synthetic/demo dataset, not real hospital data.** It
exists so the training pipeline can be built, run, and evaluated end-to-end
with real (not invented) metrics. Every number in §5 below comes from
actually running `training/train_models.py` against this CSV — nothing was
hand-picked or backfilled. Swapping in a real, de-identified, labelled
survey export requires no code changes — just `--data` and `--label-col`
flags (see §6).

## 4. Feature / target logic (leakage prevention)

**Issue classifier** — input is TF-IDF (1–2 grams) of the feedback text
only. No rating, sentiment, or other engineered column is used, so there is
no channel for target leakage into this model.

**Satisfaction model** — target is `rating_band` (Poor/Average/Good/
Excellent), which `feature_engineer.py` already derives from
`overall_rating`. Features are restricted to signals that are independent of
the rating itself:

- `vader_compound` — VADER sentiment of the feedback text
- `word_count`, `char_count` — feedback length
- `department`, `issue_category` — categorical context

Explicitly **excluded**: `overall_rating`, `rating_band` itself,
`is_promoter`, `is_detractor`, `nps_category`, `composite_score`,
`priority_score` — all of these are derived from the rating and would leak
the target. `modules/ml_satisfaction_model.py` hard-codes this feature list
and raises a `ValueError` if a caller ever tries to pass in a leaky column,
rather than silently training on it.

## 5. Actual results (from running the pipeline)

### Issue classifier — TF-IDF + Logistic Regression
9 classes, 630 train / 135 val / 135 test rows (stratified split, seed 42).

| Split | Accuracy | Macro Precision | Macro Recall | Macro F1 |
|---|---|---|---|---|
| Validation | 1.000 | 1.000 | 1.000 | 1.000 |
| Test | 1.000 | 1.000 | 1.000 | 1.000 |

**Important caveat:** this is a real, measured result — not fabricated —
but it should be read for what it is: the synthetic dataset's per-category
vocabulary is quite distinct by construction (each category draws from its
own template pool), so a linear TF-IDF classifier separates it almost
perfectly. Standalone inference on freshly-written, unseen phrasing (not
from the templates) worked correctly but with materially lower confidence
(0.22–0.50), which is exactly the regime the confidence-threshold fallback
is designed for. **Do not read 100% as an expectation of real-world
performance** — on genuine patient feedback (messier phrasing, category
overlap, sarcasm, mixed complaints) accuracy will be meaningfully lower.
Re-run training on real labelled data before trusting these numbers
operationally.

### Satisfaction model — RandomForest on non-rating features
4 classes (Poor/Average/Good/Excellent), same split sizes.

| Split | Accuracy | Macro Precision | Macro Recall | Macro F1 |
|---|---|---|---|---|
| Validation | 0.526 | 0.526 | 0.544 | 0.515 |
| Test | 0.481 | 0.468 | 0.520 | 0.480 |

Test confusion matrix (rows = true, cols = predicted; order Average,
Excellent, Good, Poor):

```
              Avg  Exc  Good Poor
Average    [   14,   0,   4,  18 ]
Excellent  [    0,  13,   2,   0 ]
Good       [    5,  12,   8,   2 ]
Poor       [   21,   0,   6,  30 ]
```

This is a meaningfully-better-than-random result (25% baseline for 4
balanced classes) but far from strong — expected, since it's deliberately
restricted to non-leaking signals (sentiment/length/category/department)
rather than the rating itself. "Excellent" is well separated (sentiment is
a strong signal for very positive feedback); "Average" vs. "Poor" confusion
is the main weak spot, consistent with sentiment score alone being a
noisier signal at the lower end of the synthetic data's rating noise.

## 6. Commands

```bash
# from the project root (same directory as app.py)
pip install -r requirements.txt
python -c "import nltk; nltk.download('punkt'); nltk.download('stopwords'); nltk.download('wordnet')"

# 1. Generate the reproducible synthetic sample dataset (only needed once,
#    or to regenerate it — same seed always produces the same data)
python training/generate_sample_data.py

# 2. Train both models end-to-end, evaluate, persist artifacts + metrics.json
python training/train_models.py
# Options:
#   --data <path.csv>        use a different (e.g. real) labelled CSV
#   --label-col <col>        ground-truth issue-category column name
#   --skip-satisfaction      train only the issue classifier

# 3. Run the app as before (unchanged)
streamlit run app.py
```

Inference without retraining (used internally by `issue_categorizer.py`,
and available standalone for Phase 2 wiring):

```python
from modules import ml_issue_classifier, ml_satisfaction_model

label, confidence = ml_issue_classifier.predict_one("The wait was way too long")
# -> ('Wait Time', 0.71)

# satisfaction model expects vader_compound/word_count/char_count/department/
# issue_category columns already present (i.e. run after the existing
# feature_engineer + sentiment + issue_categorizer pipeline stages)
preds = ml_satisfaction_model.predict(df)
```

## 7. Files created / modified

**Created**
- `training/generate_sample_data.py` — synthetic labelled dataset generator (seeded, reproducible)
- `training/train_models.py` — end-to-end training/evaluation entrypoint
- `modules/ml_issue_classifier.py` — TF-IDF + LogisticRegression training + inference
- `modules/ml_satisfaction_model.py` — RandomForest satisfaction model, leakage-safe feature set, training + inference
- `data/sample_patient_survey.csv` — generated synthetic dataset (900 rows)
- `models/issue_classifier/*.joblib`, `models/satisfaction_model/*.joblib`, `models/*/metrics.json` — persisted trained artifacts + metrics
- `PHASE1_ML_UPGRADE.md` — this report

**Modified**
- `modules/issue_categorizer.py` — added ML-first / rule-fallback logic (`categorize_issues`). The original `_classify_text` keyword function is unchanged and still runs as the fallback path; two new output columns (`issue_category_confidence`, `issue_category_source`) are added for transparency.
- `utils/config.py` — added `RANDOM_SEED`, `MODELS_DIR`/`ISSUE_MODEL_DIR`/`SATISFACTION_MODEL_DIR` paths, `ISSUE_ML_CONFIDENCE_THRESHOLD`.

**Untouched** (verified still working): `app.py`, `data_loader.py`,
`data_cleaner.py`, `data_validator.py`, `preprocessor.py`,
`feature_engineer.py`, `sentiment.py`, `priority_scorer.py`,
`action_mapper.py`, `recommender.py`, `summarizer.py`,
`insight_generator.py`, `eda.py`, `requirements.txt`, `render.yaml`,
`style.css`.

`requirements.txt` was **not** changed — `scikit-learn` and `joblib` were
already pinned in it.

## 8. Verification performed

- Ran `training/train_models.py` end-to-end; both models trained and
  metrics/confusion matrices generated (§5).
- Loaded persisted artifacts in a fresh process and ran inference on
  hand-written sentences not present in the training data — correct
  category predictions, confidence scores behaved sensibly (lower on
  paraphrased/unfamiliar phrasing than on template-like phrasing).
- Ran the full existing pipeline (`clean_data → preprocess_data →
  engineer_features → run_sentiment_analysis → categorize_issues →
  score_priorities → map_actions → generate_recommendations →
  summarize_feedback → generate_insights`) against a fresh slice of the
  sample data — completed without errors, all downstream modules (priority
  scoring, recommendations, action mapping, summaries, insights) still work
  unmodified against the ML-produced `issue_category` column.
- Verified the fallback path explicitly: with the `models/` directory
  temporarily removed, `categorize_issues` correctly falls back to
  100% rule-based classification with no errors.
- Verified the rule-based classifier function itself was not modified.

## 9. Limitations / issues for Phase 2

1. **Training data is synthetic.** These metrics validate that the ML
   plumbing (splits, evaluation, persistence, inference, fallback) is
   correct — they are not a claim about accuracy on real patient feedback.
   Before relying on this in production, retrain on a real, de-identified,
   labelled survey export via `--data`/`--label-col`.
2. **PDF upload is a stub.** `app.py`'s PDF branch (lines ~283–294) accepts
   a file and shows "processing can be added later" — it never actually
   runs the pipeline. Pre-existing, unrelated to this phase.
3. **Pre-existing NLTK resource gap in `summarizer.py`.** `sumy`'s
   `LsaSummarizer` requires the `punkt_tab` NLTK resource, which
   `summarizer.py`'s download list doesn't include (only `punkt` is listed).
   It fails closed gracefully (falls back to truncation, no crash) but never
   produces the intended LSA summary. Found during verification; not
   modified since summarization is outside this phase's ML scope, but worth
   a one-line fix in `NLTK_RESOURCES` in Phase 2.
4. **`preprocess_data()` one-hot-encodes any low-cardinality object column**
   it doesn't explicitly exclude. This is harmless for the current schema
   but means a real uploaded CSV with an unexpected categorical text column
   could silently get dummy-encoded. Pre-existing behavior, not changed.
5. **Issue classifier confidence threshold (0.45) is a reasonable starting
   default, not tuned against real data.** Revisit once real labelled
   feedback is available — you'll want to check the precision/recall
   trade-off at different thresholds (e.g. via a validation-set PR curve)
   rather than trusting this default.
6. **Satisfaction model is not yet exposed anywhere in the UI** (by design,
   per scope) — Phase 2 should decide where/how to surface predicted
   satisfaction tier alongside the existing rating.
7. **Class imbalance/rare classes:** both training functions drop classes
   with fewer than 3 samples before splitting (needed for stratification).
   On a real dataset, watch the training log for "Dropping N rows with rare
   classes" — that's a sign the label taxonomy needs consolidation or more
   data for that category.

## 10. Final targeted cleanup (this pass)

Scope of this pass was strictly cleanup of the seven items below — no
redesign, no new models, no frontend/backend/deployment work.

1. **Decoupled training/inference from the Streamlit UI.** `data_cleaner.py`
   and `sentiment.py` (the two pipeline modules on the training path that
   call `st.info`/`st.warning`) now check
   `utils/streamlit_compat.in_streamlit_context()` before making a UI call,
   falling back to the existing logger call when there's no active
   Streamlit session. `training/train_models.py` and both `modules/ml_*.py`
   modules already had no Streamlit dependency. Net effect: running
   `python training/train_models.py` no longer requires or touches the UI,
   and no longer prints Streamlit "missing ScriptRunContext" noise.
   Behavior inside the running app (`streamlit run app.py`) is byte-for-byte
   unchanged — verified with a headless smoke test (HTTP 200, clean logs).
2. **Synthetic dataset is now labelled in the artifacts themselves, not
   just the docs.** `metrics.json` for the issue classifier carries a
   `dataset_notice` field; when trained on the generated sample,
   `train_models.py` prints an explicit banner before and after training
   stating the 100% score is a pipeline sanity check on synthetic,
   vocabulary-distinct data — not real-world performance.
3. **Satisfaction model now explicitly marked experimental.** Its
   `metrics.json` carries `"model_status": "experimental"` and a
   `status_note` explaining the accuracy is above baseline but not strong
   enough to stand alone. The training script prints the same framing.
4. **Leakage re-verified for both models**, with an explicit, printed
   check in `train_models.py` (not just a comment): issue classifier input
   is confirmed to be text-only; the satisfaction model's feature list is
   checked against `LEAKY_COLUMNS` at training time and the script raises
   if any leaky column is present. Both checks passed on this run — see
   the "Leakage check" lines in the training output.
5. **Persisted-model loading re-verified** in a fresh Python process (no
   training code executed): both `ml_issue_classifier.predict_one()` and
   `ml_satisfaction_model.predict()` loaded joblib artifacts from disk and
   returned correct predictions.
6. **ML → confidence-threshold → rule fallback re-verified**: a
   low-signal/gibberish input fell through to the rule-based classifier
   (confidence below 0.45), and with `models/` temporarily removed,
   categorization ran 100% rule-based with no errors.
7. **Full existing app functionality re-verified unchanged**: `python -m
   py_compile` across all modules, a headless `streamlit run app.py` smoke
   test (200 OK, no errors), and a full pipeline run (clean → preprocess →
   feature_engineer → sentiment → categorize_issues → priority_scorer →
   action_mapper → recommender → summarizer → insight_generator) against a
   fresh slice of data — all completed without error.

**Files touched in this pass:** `modules/data_cleaner.py`,
`modules/sentiment.py` (Streamlit-call guards only), `modules/
ml_issue_classifier.py`, `modules/ml_satisfaction_model.py` (added
`dataset_notice`/`model_status`/`status_note` fields to persisted metrics),
`training/train_models.py` (disclaimers + explicit leakage-check prints),
and one new file, `utils/streamlit_compat.py`. No other files changed; no
new models, libraries, or scope added.
