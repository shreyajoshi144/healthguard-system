# HealthGuard — Phase 3C Report: Uncertainty, Calibration & OOD Foundation

Scope: determine when HealthGuard should trust a prediction and when it
should flag one as unreliable, for the **frozen** Phase 3A benchmark and
Phase 3B semantic model. This phase optimizes for reliability
characterization, not raw accuracy — no dataset, model, or artifact from
any prior phase was modified.

## 1. Files added/changed

**Added** (all under `evaluation/`, `models/uncertainty/`, `tests/`):
- `evaluation/frozen_models.py` — the single read-only access point to both frozen artifacts (joblib.load + transform/predict_proba only, never fit)
- `evaluation/calibration.py` — Brier score, ECE, reliability bins, confidence-bucket accuracy, validation-only calibration method selection
- `evaluation/uncertainty.py` — structured per-prediction records (confidence/margin/entropy/uncertainty_level), validation-derived thresholds
- `evaluation/ood_detection.py` — nearest-class-centroid cosine-distance OOD method, validation-only threshold
- `evaluation/threshold_analysis.py` — coverage/accuracy/F1/error/escalation curve, validation-selected frozen operating point
- `evaluation/disagreement.py` — benchmark-vs-semantic prediction comparison
- `evaluation/reliability_report.py` — orchestrator, writes the combined JSON report
- `models/uncertainty/calibrator_benchmark.joblib`, `calibrator_semantic.joblib` (generated — fitted calibrators)
- `models/uncertainty/ood_detector_benchmark.json`, `ood_detector_semantic.json` (generated — centroids + thresholds, plain JSON for inspectability)
- `evaluation/results/latest_reliability_report.json` + timestamped copy (generated)
- `tests/test_reliability.py` — 23 new tests
- `PHASE3C_UNCERTAINTY_CALIBRATION_OOD.md` (this report)

**Changed**: nothing. No existing file was modified — this phase is purely additive, read-only against every prior artifact. `requirements.txt` unchanged (only stdlib + already-installed `sklearn`/`numpy`/`pandas`/`joblib`/`scipy` were used).

## 2. Calibration method + metrics

**Method selection (validation only)**: for each frozen model, three
candidates — uncalibrated, sigmoid (Platt), isotonic — compared by
validation Brier score via `sklearn.calibration.CalibratedClassifierCV`
wrapping the frozen estimator (`sklearn.frozen.FrozenEstimator`, the
current sklearn 1.6+ replacement for the deprecated `cv="prefit"` API;
neither frozen classifier's own parameters are touched). **Isotonic won
for both models** on validation.

| | Benchmark | Semantic |
|---|---|---|
| Validation Brier (uncal / sigmoid / **isotonic**) | 0.0200 / 0.0046 / **0.0000** | 0.0028 / 0.0101 / **0.0012** |
| Validation ECE (selected) | 0.0000 | 0.0170 |

**This is reported honestly, not celebrated**: isotonic reaching Brier =
0.0000 on a 53-example validation set (with some classes as small as 4
examples — sklearn warned about this directly) is a textbook small-sample
overfitting signature, not genuine calibration quality. Applying the
selected calibration to test and challenge confirms it:

| | Benchmark test (Brier / ECE) | Benchmark challenge (Brier / ECE) | Semantic test (Brier / ECE) | Semantic challenge (Brier / ECE) |
|---|---|---|---|---|
| **Selected (isotonic)** | 0.0005 / 0.0157 | **0.0706 / 0.3459** | 0.0037 / 0.0538 | **0.0980 / 0.5368** |
| Uncalibrated baseline | 0.0199 / 0.3683 | **0.0559 / 0.0849** | 0.0031 / 0.0871 | **0.0748 / 0.3631** |

On test (same distribution as training), isotonic calibration looks like
a dramatic win. **On the challenge set — the actual generalization
test — it makes calibration measurably worse for both models** (ECE
roughly 4x and 1.5x worse respectively). Per the task's explicit rule
("if the uncalibrated probabilities are better, report that honestly
rather than forcing calibration"): **the honest conclusion is that
validation-selected calibration should not be trusted here** — it
overfits a small validation set and actively hurts out-of-distribution
reliability, even though the stated selection protocol (pick by
validation Brier score) was followed correctly. Both the selected
approach and the uncalibrated baseline are reported side by side in the
JSON output specifically so this comparison is visible, not hidden behind
a single "winning" number.

## 3. Uncertainty signals/results

Each prediction gets `{prediction, confidence, margin, entropy,
uncertainty_level}`. Thresholds are the 33rd/66th percentile of each
model's own validation confidence distribution — not arbitrary:

| | Benchmark | Semantic |
|---|---|---|
| Low-confidence-below (33rd pct.) | 0.527 | 0.916 |
| High-confidence-at-or-above (66th pct.) | 0.739 | 0.969 |

The gap between these two rows is itself a finding: the semantic model's
validation confidence is compressed into a much narrower, higher band —
consistent with the overconfidence already seen in Phase 3B.

**On the challenge set**, the bucketing loses most of its discriminative
power: benchmark puts **all 36/36** examples in "High" uncertainty;
semantic puts 34/36 in "High" and only 2 in "Medium." Accuracy within the
(nearly-universal) "High" bucket is 27.8% (benchmark) and 26.5%
(semantic) — the bucketing correctly identifies the whole challenge set as
risky in aggregate (consistent with the low actual accuracy there), but
because it's calibrated to an "easy" validation distribution, it can't
further discriminate *which* challenge examples are relatively safer
within that set. That's a real limitation, not a hidden one.

## 4. OOD method + results

**Method**: nearest-training-class-centroid cosine distance, in each
model's own existing feature space (TF-IDF for benchmark, embeddings for
semantic) — no new classifier trained. Centroids from `train.csv` only;
threshold from `val.csv` only (the score value flagging ~5% of genuinely
in-domain validation examples).

| | Benchmark (TF-IDF space) | Semantic (embedding space) |
|---|---|---|
| Validation-calibrated flag rate | 5.66% | 5.66% |
| **Test flag rate** | 5.66% | 3.77% |
| **Challenge flag rate (overall)** | **100%** | **19.4%** |

**Critical, honest finding — these two feature spaces disagree sharply,
and neither is simply "right":**

| challenge_type | Benchmark flag rate | Semantic flag rate |
|---|---|---|
| paraphrase | 100% | **0%** |
| ambiguous | 100% | 40% |
| multi_issue | 100% | **0%** |
| low_information | 100% | 80% |
| unfamiliar_wording | 100% | **0%** |
| out_of_domain | 100% | **0%** |
| edge_case | 100% | 17% |

The TF-IDF-space detector flags **literally every challenge example**,
including `paraphrase` (still on-topic, just reworded) — meaning it has
**zero discriminative power** here: any text that doesn't share the
training templates' exact vocabulary reads as "far from every centroid,"
whether or not it's actually a different topic. The embedding-space
detector is far more selective (19.4% overall) and picks out
`low_information` well (80%), but **completely misses `out_of_domain`
(0%)** — the one category that most obviously *should* be flagged (pizza
delivery, laptop reviews, a work email). Generic English sentences about
unrelated topics still land in a "moderately English-like" region of the
embedding space that isn't cosine-distant from the hospital-feedback
centroids. **Per the task's explicit instruction, this is reported
plainly: neither lightweight, explainable OOD method reliably identifies
genuine domain shift on this data** — the benchmark's method is
uselessly indiscriminate, and the semantic method has a specific, real
blind spot for exactly the category it should catch most easily.

## 5. Coverage/reliability trade-off

Validation curve swept thresholds 0.30–0.90; operating point selected as
the *lowest* threshold meeting a **95% validation accuracy bar**, frozen
before touching test/challenge.

| | Benchmark | Semantic |
|---|---|---|
| Frozen threshold | 0.30 (the lowest candidate tried) | 0.30 (the lowest candidate tried) |
| Test: coverage / accuracy / escalation | 98.1% / 100% / 1.9% | 100% / 100% / 0% |
| **Challenge: coverage / accuracy / escalation** | **2.8% / 100% (n=1) / 97.2%** | **94.4% / 26.5% / 5.6%** |

**Both models hit the validation accuracy bar at the lowest threshold
tried** — validation is simply too easy (near-ceiling accuracy
everywhere) to produce an interesting threshold curve; this is a genuine
limitation of using this synthetic validation set for threshold
selection, not a flaw in the method itself.

The consequence on the challenge set is stark and directly answers "how
much can we automate vs. how reliable is it": **the benchmark, at this
frozen threshold, refuses to trust almost anything on hard data (2.8%
coverage) but is exactly right about the one thing it does trust.
The semantic model trusts almost everything (94.4% coverage) and is
wrong 73.5% of the time on what it claims to trust.** A threshold picked
this way would make the benchmark safely useless and the semantic model
dangerously overconfident on exactly the kind of input the challenge set
represents. Neither is a usable operating point as-is; this quantifies
*why*, rather than asserting it.

## 6. Disagreement results

| Split | Agreement rate | Accuracy when agreeing | Benchmark acc. when disagreeing | Semantic acc. when disagreeing |
|---|---|---|---|---|
| Validation | 100% | 100% | n/a (never disagree) | n/a |
| Test | 100% | 100% | n/a | n/a |
| **Challenge** | **33.3%** | **50.0%** | **16.7%** | **16.7%** |

**Disagreement is a genuinely useful signal here**: error rate when the
two models disagree (83.3%, averaged) is far higher than when they agree
(50.0%) — a 33.3-point gap, and `disagreement_signal_useful` computes to
`True`. This does **not** mean either model is "more right" when they
disagree — both average only 16.7% accuracy on the disagreement subset —
it means **agreement between two structurally different models (lexical
vs. semantic features) is a meaningful, measured proxy for trustworthiness
independent of either model's own confidence**, which neither
model's calibration or threshold analysis (§2, §5) could provide on its
own.

## 7. Challenge-category analysis

| Category | Uncertainty (both models) | OOD flagged? | Disagreement | Interpretation |
|---|---|---|---|---|
| paraphrase | High (both) | Benchmark: yes; Semantic: no | Low agreement (16.7%), but each often right individually (bench 60%, sem 40% when disagreeing) | Genuinely in-domain; OOD detectors correctly split on this (semantic right to not flag it) |
| ambiguous | High (both) | Benchmark: yes; Semantic: 40% | Low agreement (20%), 0% accurate whenever they disagree or agree | Both classification and OOD signals correctly read this as hard/uncertain — no false confidence here |
| multi_issue | High (both) | Benchmark: yes; Semantic: no | Moderate agreement (60%), benchmark 50% right when disagreeing, semantic 0% | Benchmark's lexical features handle multi-topic text somewhat better than semantic's averaged embedding here |
| low_information | High (both) | **Both flag heavily** (100% / 80%) | Low agreement (20%), 0% accurate either way | The one category where OOD detection genuinely agrees with intuition for both methods — very short/vague text is correctly flagged as unusual in both feature spaces |
| unfamiliar_wording | High (both) | Benchmark: yes; **Semantic: no** | Moderate agreement (40%), semantic 66.7% right when disagreeing (its best result) | Semantic's real strength (per Phase 3B) — correctly NOT flagged as OOD, and where semantic actually adds value over benchmark |
| **out_of_domain** | High (both) | Benchmark: yes; **Semantic: no (0%)** | 50% agreement | **The clearest OOD-method failure**: the semantic detector's biggest blind spot is exactly the category most obviously not hospital feedback |
| edge_case | High (both) | Benchmark: yes; Semantic: 17% | Moderate agreement (33%), 100% accurate when agreeing, 0% when disagreeing | Sarcasm/negation is hard for both, but agreement is a strong reliability signal specifically here |

**Explicitly, per the task's instruction**: not every challenge category
is "true OOD" — `paraphrase`, `multi_issue`, and `unfamiliar_wording` are
difficult classification problems but arguably still in-domain (same
topic, different wording), and the semantic OOD detector's decision not
to flag them is defensible, not a bug. `out_of_domain` is the one category
genuinely meant to represent domain shift, and it is the specific case
the more selective (semantic) detector fails to catch.

## 8. Latency/dependency impact

No new dependencies were added — this phase's calibration, uncertainty,
OOD, and threshold code is pure `numpy`/`pandas`/`scikit-learn`, already
present from Phases 3A/3B. The one new computational cost is fitting two
small `CalibratedClassifierCV` wrappers on ~53 validation examples each
(sub-second) and computing 9 class centroids per model from the training
feature space (also sub-second — a single mean per class, not an
iterative training loop). None of this phase's artifacts are wired into
`app.py` or `backend/`, so there is no change to any live request latency.

## 9. Tests passed

`tests/test_reliability.py` — **23 new tests**, full suite **86 tests, all
passing**:

```
python -m unittest discover -s tests -v
# Ran 86 tests in ~17s — OK
```

| Test class | Count | Proves |
|---|---|---|
| `TestValidationOnlySignatures` | 4 | Every selection function's signature structurally cannot accept test/challenge data |
| `TestCalibrationFitsOnlyOnValidation` | 1 | Spy on `CalibratedClassifierCV.fit` — every call during method selection used exactly the validation set size |
| `TestThresholdSelectionUsesValidationOnly` | 2 | Operating-point selection picks correctly from a hand-built validation curve; the same frozen threshold behaves identically regardless of which split it's applied to |
| `TestUncertaintySignalsCorrectness` | 3 | Margin/entropy hand-computed and checked to 3 decimal places; bucket boundaries verified; validation-percentile threshold derivation checked against `np.percentile` directly |
| `TestOODDetectionCorrectness` | 5 | Identical vector → score 0; orthogonal vector → score 1; threshold targets the requested flag rate within tolerance; flagging respects the threshold; a full toy train→val→threshold→flag pipeline |
| `TestDisagreementCorrectness` | 2 | Hand-computed 4-example agree/disagree scenario matches exactly; usefulness-flag computed correctly on a constructed example |
| `TestFrozenArtifactIntegrity` | 2 | **All five frozen model files (production + benchmark + semantic) unchanged** after a full reliability run; **all four data files unchanged** |
| `TestReliabilityReportSchemaAndReproducibility` | 4 | Full output schema validated field-by-field; **two independent runs produce identical selected methods/thresholds/flag rates** (determinism); splits/challenge set not regenerated (full text-column diff); uncertainty and OOD are demonstrably distinct signals (not the same computation twice) |

## 10. Frozen artifact integrity

SHA-256 checksums of all five pre-existing model files, verified identical
before and after every run in this phase (component tests, full pipeline
runs, and the entire test suite):

```
models/issue_classifier/logreg_issue_classifier.joblib        f933aa52...
models/issue_classifier/tfidf_vectorizer.joblib                8b346e9b...
models/benchmark_issue_classifier/logreg_issue_classifier.joblib  7d86d021...
models/benchmark_issue_classifier/tfidf_vectorizer.joblib          29ea02b0...
models/semantic_issue_classifier/logreg_classifier.joblib          72e8a1e4...
```

`data/splits/{train,val,test}.csv` and `data/challenge_set.csv` — verified
identical (checksum and full text-column diff) before and after every run.

## 11. Limitations

- **Validation set is too small and too easy** (53 examples, some classes
  as few as 4) to support robust calibration-method selection or a
  meaningful threshold curve — both problems are documented as concrete
  findings above (§2's isotonic overfitting, §5's degenerate "lowest
  threshold wins" curve), not glossed over.
- **The OOD method is deliberately simple** (nearest-centroid cosine
  distance) per the task's "simplest defensible method" instruction. It
  is not a substitute for a proper density-based or learned OOD detector —
  §4 documents its concrete, specific failure (missing `out_of_domain`
  entirely in embedding space).
- **Calibration and OOD analysis both depend on the same small, synthetic
  challenge set** (n=36, per-category subgroups as small as n=4) used
  throughout Phases 3A–3B — findings here are directionally informative,
  not statistically precise.
- **Neither calibration nor the OOD detector is wired into the live
  application** — this phase is a foundation/diagnostic layer, exactly as
  scoped. No human-review routing, no automatic escalation, no UI changes.
- **Disagreement analysis has zero informative disagreement on val/test**
  (both models agree 100% of the time there) — the entire disagreement
  signal comes from the challenge set alone; this is expected given both
  models hit the same synthetic-data ceiling, but it means the "useful
  signal" finding in §6 rests on 24 disagreeing examples, not a large
  sample.

## 12. Final conclusion

**Can HealthGuard reliably identify predictions it should not trust?
Partially, and unevenly across methods — not yet as a complete, dependable
system.**

- **Confidence alone: no.** Both models are well-behaved on in-distribution
  data but the semantic model is specifically, measurably overconfident on
  hard data (94% "trusted" coverage on the challenge set at only 26.5%
  actual accuracy) — confidence thresholds calibrated on easy validation
  data do not transfer safely.
- **Calibration: not as currently selected.** The validation-driven
  choice (isotonic) actively worsens calibration on the one dataset that
  matters most (the challenge set) for both models. The honest
  recommendation, following the task's own rule, is to **not deploy the
  validation-selected calibrator as-is** — the uncalibrated probabilities
  are the safer choice on genuinely novel input, exactly the "if
  uncalibrated is better, report it" scenario this phase was designed to
  surface.
- **OOD detection: partially, with a known blind spot.** The lightweight
  centroid-distance method is either uselessly indiscriminate (TF-IDF
  space flags everything) or usefully selective but specifically blind to
  genuine domain shift (embedding space misses `out_of_domain` entirely).
  Neither is ready to gate automated decisions alone.
- **Model disagreement: yes, and this is the strongest signal found in
  this phase.** Agreement between the two structurally different frozen
  models tracks real reliability (50% accuracy when they agree vs. 16.7%
  when they don't) — a genuinely useful, measured uncertainty proxy that
  neither model's own confidence or calibration could provide alone, and
  the clearest actionable finding to carry into any future phase that
  considers acting on these signals.

Taken together: HealthGuard now has the *measurement infrastructure* to
characterize when its predictions are unreliable, and that infrastructure
has already surfaced concrete, specific weaknesses (overconfidence,
calibration overfitting, an OOD blind spot) rather than manufacturing a
false sense of readiness. It does not yet have a single trustworthy
"trust/don't trust" signal — the most defensible one identified is
inter-model disagreement, and even that is measured on a small sample.
