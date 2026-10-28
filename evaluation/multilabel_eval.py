from __future__ import annotations
from typing import Iterable

from sklearn.preprocessing import MultiLabelBinarizer
from sklearn.metrics import hamming_loss, precision_recall_fscore_support, accuracy_score


def evaluate_multilabel(
    y_true: Iterable[list[str]],
    y_pred: Iterable[list[str]],
    all_labels: list[str],
) -> dict:
    """
    Generic multi-label evaluation: given parallel lists of ground-truth
    label-sets and predicted label-sets (each a list[str] drawn from
    all_labels), returns:
      - subset_accuracy: fraction of examples where the predicted set
        exactly equals the true set (the strictest possible metric)
      - hamming_loss: fraction of individual label mismatches across all
        examples and labels (0 = perfect, 1 = every label wrong)
      - per_label: precision/recall/F1 for each label individually
      - macro_precision / macro_recall / macro_f1: averaged across labels

    all_labels must be provided explicitly (not inferred from the data) so
    a label that never appears in this particular batch still gets a
    defined (zero) score rather than silently vanishing from the report.
    """
    y_true = list(y_true)
    y_pred = list(y_pred)
    if len(y_true) != len(y_pred):
        raise ValueError("y_true and y_pred must be the same length")

    mlb = MultiLabelBinarizer(classes=all_labels)
    Y_true = mlb.fit_transform(y_true)
    Y_pred = mlb.transform(y_pred)

    subset_accuracy = accuracy_score(Y_true, Y_pred)  # exact set match per row
    h_loss = hamming_loss(Y_true, Y_pred)

    precision, recall, f1, support = precision_recall_fscore_support(
        Y_true, Y_pred, average=None, zero_division=0
    )
    per_label = {
        label: {
            "precision": round(float(p), 4),
            "recall": round(float(r), 4),
            "f1": round(float(f), 4),
            "support": int(s),
        }
        for label, p, r, f, s in zip(all_labels, precision, recall, f1, support)
    }

    macro_p, macro_r, macro_f, _ = precision_recall_fscore_support(
        Y_true, Y_pred, average="macro", zero_division=0
    )

    return {
        "scaffold_notice": (
            "This is a demonstration of the multi-label evaluation machinery, "
            "not a real multi-label model result. The existing baseline is "
            "single-label; predictions here are its top-1 primary_issue "
            "output treated as a length-1 set, compared against hand-labelled "
            "secondary_issues ground truth. Phase 3B will replace this with "
            "genuine multi-label predictions."
        ),
        "n_examples": len(y_true),
        "subset_accuracy": round(float(subset_accuracy), 4),
        "hamming_loss": round(float(h_loss), 4),
        "macro_precision": round(float(macro_p), 4),
        "macro_recall": round(float(macro_r), 4),
        "macro_f1": round(float(macro_f), 4),
        "per_label": per_label,
    }
