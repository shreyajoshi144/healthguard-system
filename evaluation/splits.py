
from __future__ import annotations
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
from sklearn.model_selection import train_test_split

from evaluation.leakage_checks import _normalize_text
from utils.config import (
    PROJECT_ROOT, SPLITS_DIR, RANDOM_SEED, SAMPLE_DATASET_VERSION,
)
from utils.logger import get_logger

logger = get_logger(__name__)

SOURCE_CSV = PROJECT_ROOT / "data" / "sample_patient_survey.csv"
LABEL_COLUMN = "true_issue_category"
TEXT_COLUMN = "feedback_text"

TRAIN_PATH = SPLITS_DIR / "train.csv"
VAL_PATH = SPLITS_DIR / "val.csv"
TEST_PATH = SPLITS_DIR / "test.csv"
MANIFEST_PATH = SPLITS_DIR / "split_manifest.json"

TEST_SIZE = 0.15
VAL_SIZE = 0.15


def _sha256_of_series(series: pd.Series) -> str:
    joined = "\n".join(sorted(series.astype(str).tolist()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def build_splits(
    source_csv: Path = SOURCE_CSV,
    random_state: int = RANDOM_SEED,
    test_size: float = TEST_SIZE,
    val_size: float = VAL_SIZE,
    force: bool = False,
) -> dict:
    """
    Build (or load, if already built with an identical manifest and
    force=False) the persisted train/val/test split. Returns the manifest
    dict. Idempotent: re-running with the same inputs reproduces the exact
    same rows every time (verified by hashing each split's text).
    """
    if MANIFEST_PATH.exists() and not force:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if (
            existing.get("source_csv") == str(source_csv)
            and existing.get("random_state") == random_state
            and existing.get("test_size") == test_size
            and existing.get("val_size") == val_size
        ):
            logger.info("Splits already built with matching config — reusing %s", SPLITS_DIR)
            return existing
        logger.info("Existing split manifest doesn't match requested config — rebuilding.")

    if not source_csv.exists():
        raise FileNotFoundError(
            f"Source dataset not found at {source_csv}. Run "
            "`python training/generate_sample_data.py` first."
        )

    df = pd.read_csv(source_csv)
    if LABEL_COLUMN not in df.columns:
        raise ValueError(f"Expected label column '{LABEL_COLUMN}' not found in {source_csv}.")

    # Same rare-class-dropping rule as modules/ml_issue_classifier.py's
    # fit_and_evaluate(), so this split is comparable to what that function
    # would have produced from the same source data.
    class_counts = df[LABEL_COLUMN].value_counts()
    valid_classes = class_counts[class_counts >= 3].index
    dropped = (~df[LABEL_COLUMN].isin(valid_classes)).sum()
    if dropped:
        logger.warning("Dropping %d rows with rare classes (<3 samples) before splitting", dropped)
    df = df[df[LABEL_COLUMN].isin(valid_classes)].reset_index(drop=True)

    # DATA-QUALITY FINDING (Phase 3A): training/generate_sample_data.py
    # composes each row from a small fixed pool of sentence fragments
    # (see CATEGORY_TEMPLATES there), so a large fraction of rows share
    # EXACT duplicate feedback_text — out of 900 source rows, only ~353 are
    # textually unique. A naive random split ignores this and happily puts
    # the same exact sentence in both train and test, which is genuine
    # leakage (the model isn't generalizing to unseen text, it's
    # recognizing text it memorized verbatim during training). This is
    # deduplicated HERE, in the new Phase 3A split logic only — the source
    # CSV, training/generate_sample_data.py, and
    # modules/ml_issue_classifier.py's own internal split are all left
    # exactly as they were; this does not retrain or alter the existing
    # baseline, only how this SEPARATE evaluation split is built.
    df["_normalized_text"] = df[TEXT_COLUMN].apply(_normalize_text)
    n_before_dedup = len(df)
    df = df.drop_duplicates(subset="_normalized_text", keep="first").reset_index(drop=True)
    n_deduped = n_before_dedup - len(df)
    if n_deduped:
        logger.warning(
            "Dropped %d rows with duplicate (normalized) feedback_text before "
            "splitting, to guarantee train/val/test disjointness — %d unique "
            "texts remain out of %d source rows.",
            n_deduped, len(df), n_before_dedup,
        )
    df = df.drop(columns=["_normalized_text"])

    df_temp, df_test = train_test_split(
        df, test_size=test_size, random_state=random_state, stratify=df[LABEL_COLUMN]
    )
    relative_val = val_size / (1 - test_size)
    df_train, df_val = train_test_split(
        df_temp, test_size=relative_val, random_state=random_state, stratify=df_temp[LABEL_COLUMN]
    )

    SPLITS_DIR.mkdir(parents=True, exist_ok=True)
    df_train.to_csv(TRAIN_PATH, index=False)
    df_val.to_csv(VAL_PATH, index=False)
    df_test.to_csv(TEST_PATH, index=False)

    manifest = {
        "dataset_version": SAMPLE_DATASET_VERSION,
        "source_csv": str(source_csv),
        "label_column": LABEL_COLUMN,
        "text_column": TEXT_COLUMN,
        "random_state": random_state,
        "test_size": test_size,
        "val_size": val_size,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "n_source_rows_raw": int(dropped) + n_before_dedup,
        "n_rows_after_rare_class_filter": n_before_dedup,
        "n_rows_after_dedup": len(df),
        "n_dropped_rare_class_rows": int(dropped),
        "n_dropped_duplicate_text_rows": int(n_deduped),
        "splits": {
            "train": {"n_rows": len(df_train), "path": str(TRAIN_PATH),
                       "text_sha256": _sha256_of_series(df_train[TEXT_COLUMN])},
            "val": {"n_rows": len(df_val), "path": str(VAL_PATH),
                     "text_sha256": _sha256_of_series(df_val[TEXT_COLUMN])},
            "test": {"n_rows": len(df_test), "path": str(TEST_PATH),
                      "text_sha256": _sha256_of_series(df_test[TEXT_COLUMN])},
        },
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    logger.info(
        "Splits built: train=%d val=%d test=%d -> %s",
        len(df_train), len(df_val), len(df_test), SPLITS_DIR,
    )
    return manifest


def load_splits() -> dict[str, pd.DataFrame]:
    """Load the persisted train/val/test CSVs. Raises if build_splits()
    hasn't been run yet."""
    if not (TRAIN_PATH.exists() and VAL_PATH.exists() and TEST_PATH.exists()):
        raise FileNotFoundError(
            f"Splits not found under {SPLITS_DIR}. Run `python -m evaluation.splits` first."
        )
    return {
        "train": pd.read_csv(TRAIN_PATH),
        "val": pd.read_csv(VAL_PATH),
        "test": pd.read_csv(TEST_PATH),
    }


if __name__ == "__main__":
    manifest = build_splits(force=True)
    print(json.dumps(manifest, indent=2))
