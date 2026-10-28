"""
evaluation/
──────────────
PHASE 3A — evaluation and data-foundation package for HealthGuard's ML
layer. Contains no model training logic; it evaluates the EXISTING
persisted baseline (modules/ml_issue_classifier.py) and defines the data
contract / dataset splits / challenge set that later phases (3B: semantic
models, 3C: LLM-based verification) will be compared against on equal
footing.

Modules:
    data_contract.py    — the FeedbackRecord schema every dataset/model
                            output is validated against.
    splits.py            — persisted, reproducible train/val/test split of
                            the existing labelled sample dataset.
    challenge_set.py     — hand-authored synthetic challenge/OOD examples.
    leakage_checks.py    — column-level and row-level leakage guards.
    multilabel_eval.py   — generic multi-label metric scaffold (not a
                            multi-label model).
    evaluate.py           — main entrypoint: runs everything above and
                            writes a machine-readable JSON report.
"""
