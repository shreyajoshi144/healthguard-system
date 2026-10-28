from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional

# Superset of modules/issue_categorizer.py's ISSUE_KEYWORDS categories, plus
# the two fallback labels the rule-based classifier already emits. Kept as a
# plain list (not imported from issue_categorizer) so this contract module
# has zero import-time dependency on the modelling code — it only needs to
# agree on the vocabulary, not the implementation.
ALLOWED_ISSUE_CATEGORIES = [
    "Wait Time", "Staff Behavior", "Cleanliness", "Communication",
    "Treatment Quality", "Billing", "Facilities", "Appointment",
    "General Positive", "Other", "Uncategorized",
]

ALLOWED_SENTIMENTS = ["Positive", "Neutral", "Negative"]
ALLOWED_SEVERITIES = ["Low", "Medium", "High", "Critical"]

REQUIRED_FIELDS = [
    "feedback_text", "primary_issue", "secondary_issues", "sentiment",
    "severity", "requires_human_review", "dataset_version",
]


@dataclass
class FeedbackRecord:
    """One record conforming to the Phase 3A data contract."""
    feedback_text: str
    primary_issue: str
    sentiment: str
    severity: str
    requires_human_review: bool
    dataset_version: str
    secondary_issues: list[str] = field(default_factory=list)
    # Optional, not part of the required contract — lets the challenge set
    # tag *why* an example is hard, for breakdown reporting. Never required.
    challenge_type: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def encode_secondary_issues(issues: list[str]) -> str:
    """List -> CSV-safe string. ["Billing", "Communication"] -> "Billing;Communication" """
    return ";".join(issues) if issues else ""


def decode_secondary_issues(raw: object) -> list[str]:
    """CSV string -> list, tolerant of NaN/empty/already-a-list input."""
    if isinstance(raw, list):
        return raw
    if raw is None or (isinstance(raw, float)):  # NaN from pandas
        return []
    raw = str(raw).strip()
    return [s.strip() for s in raw.split(";") if s.strip()] if raw else []


def validate_record(record: dict) -> list[str]:
    """
    Validate a single record (as a plain dict, e.g. one row of a DataFrame
    already converted with .to_dict()) against the contract. Returns a list
    of violation messages — empty list means the record is valid.
    """
    errors = []

    for field_name in REQUIRED_FIELDS:
        if field_name not in record or record[field_name] is None:
            errors.append(f"missing required field: {field_name}")

    if "feedback_text" in record:
        text = record["feedback_text"]
        if not isinstance(text, str) or not text.strip():
            errors.append("feedback_text must be a non-empty string")

    if "primary_issue" in record and record["primary_issue"] not in ALLOWED_ISSUE_CATEGORIES:
        errors.append(
            f"primary_issue '{record.get('primary_issue')}' not in ALLOWED_ISSUE_CATEGORIES"
        )

    if "secondary_issues" in record:
        secondary = record["secondary_issues"]
        if isinstance(secondary, str):
            secondary = decode_secondary_issues(secondary)
        if not isinstance(secondary, list):
            errors.append("secondary_issues must be a list (or semicolon-separated string)")
        else:
            bad = [s for s in secondary if s not in ALLOWED_ISSUE_CATEGORIES]
            if bad:
                errors.append(f"secondary_issues contains unknown categories: {bad}")
            if "primary_issue" in record and record["primary_issue"] in secondary:
                errors.append("primary_issue must not also appear in secondary_issues")

    if "sentiment" in record and record["sentiment"] not in ALLOWED_SENTIMENTS:
        errors.append(f"sentiment '{record.get('sentiment')}' not in ALLOWED_SENTIMENTS")

    if "severity" in record and record["severity"] not in ALLOWED_SEVERITIES:
        errors.append(f"severity '{record.get('severity')}' not in ALLOWED_SEVERITIES")

    if "requires_human_review" in record and not isinstance(record["requires_human_review"], (bool,)):
        # pandas/CSV round-trips often turn bools into numpy.bool_ or the
        # strings "True"/"False" — accept those explicitly rather than
        # silently coercing everything, so genuinely malformed values are
        # still caught.
        val = record["requires_human_review"]
        is_acceptable = (
            str(type(val)).endswith("bool_'>")
            or (isinstance(val, str) and val in ("True", "False"))
        )
        if not is_acceptable:
            errors.append("requires_human_review must be a boolean")

    return errors


def validate_dataframe(df) -> dict:
    """
    Validate every row of a DataFrame against the contract. Returns a
    summary dict: {"n_rows", "n_valid", "n_invalid", "violations": {row_index: [...]}}.
    Does not raise — callers decide what to do with invalid rows.
    """
    violations = {}
    for idx, row in df.iterrows():
        record = row.to_dict()
        if "secondary_issues" in record:
            record["secondary_issues"] = decode_secondary_issues(record["secondary_issues"])
        errs = validate_record(record)
        if errs:
            violations[int(idx)] = errs
    return {
        "n_rows": len(df),
        "n_valid": len(df) - len(violations),
        "n_invalid": len(violations),
        "violations": violations,
    }
